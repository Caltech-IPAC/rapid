"""
File:    catalog_db.py

The database primitives the post-DB science chain's job types are built from.

Four operations recur across the six converted scripts — create a per-field or
per-date child table, bulk-load rows into it, delete rows that are no longer
current, and count what happened. Each one carried a defect the co-design
ruled on, and each is corrected once here rather than six times in the stage
modules:

**Clones carry the prototype's indexes.** `LIKE ... INCLUDING DEFAULTS
INCLUDING CONSTRAINTS` copies neither indexes nor the unique index migration
027 put on `merges (aid, sid)`. The old code then created a hand-written list
of indexes per table, and that list did not include the unique one — it could
not, because it predates it. So every per-field clone was born without the
constraint the design requires, and "a clone path that silently drops them is
a defect" (database design, § Integrity and durability). `INCLUDING INDEXES`
is what carries them, and `create_child_table` is the only sanctioned way to
make one of these tables.

**Loads land through a staging table and an upsert.** The old path was a raw
`COPY` straight into the target. With a uniqueness constraint now present, a
rerun of a unit that partially loaded would abort on the first duplicate row —
the load could never converge, and a failed unit could never be retried, which
is precisely what the individually-retryable ruling requires. `COPY` into an
unconstrained staging table followed by `INSERT ... ON CONFLICT DO NOTHING`
converges instead: rerunning a unit re-loads the same rows and writes zero of
them the second time. The measured rate of that shape is recorded by the
caller (ruling 6: "the load rate of that shape is measured at implementation").

**Nothing here is UNLOGGED.** The four unconditional `SET UNLOGGED` sites the
evidence pack names (§3.3) are gone, and `assert_no_unlogged_statements` in
the test suite is what keeps them gone. Unlogged tables lose their contents on
crash recovery and are not replicated; the migration baseline left the
prototypes LOGGED deliberately, and the pipeline setting every CHILD unlogged
at runtime meant every table holding real data was unlogged anyway. Trading
durability for load speed is "an argued-for regression requiring measurements,
never a default" — so the default is gone and the measurement is recorded.

**Every statement is parameterized or composed through the driver's identifier
quoting.** Table names here are built from integers (a field, an SCA, a
processing date) that this module validates before composing, and composed
through `psycopg2.sql.Identifier` rather than by f-string. The database
design's "parameterized SQL only" rule has no exception for names the code
believes it controls.
"""

import logging
import re
import time

from psycopg2 import sql

from pipeline.runtime.errors import ConfigError, InputError

logger = logging.getLogger(__name__)

# The prototypes the chain clones, and what makes a clone of each unique.
#
# The conflict target is what the upsert uses to recognise a row it already
# has. It is NOT a free choice: it must match a unique index that exists on
# the table, and these are the ones migration 027 established (merges) or the
# prototype already carried (astroobjects' primary key on aid). A conflict
# target naming a column set with no unique index raises at the database
# rather than silently inserting duplicates, which is the failure this map
# exists to prevent.
CONFLICT_TARGETS = {
    "merges": ("aid", "sid"),
    "astroobjects": ("aid",),
}

# Child-table names are `<prototype>_<field>` or `<prototype>_<date>_<sca>`.
# Validated rather than trusted: these are composed into DDL, and a name
# arriving from a manifest is submitter input like any other.
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9]*_[0-9]+$")
_DATE_SCA_NAME = re.compile(r"^[a-z][a-z0-9]*_[0-9]{8}_[0-9]+$")


def validate_child_name(tablename: str, prototype: str) -> str:
    """Check a child table's name before it is composed into DDL.

    Raises
    ------
    InputError
        If the name is not one of the two shapes this chain creates, or does
        not belong to the prototype it claims. `input_missing`-class rather
        than a config fault: the name comes from the manifest, so a bad one
        means the submission described a unit that does not exist.
    """
    if not (_FIELD_NAME.match(tablename) or _DATE_SCA_NAME.match(tablename)):
        raise InputError(
            f"{tablename!r} is not a per-field or per-date child table name; "
            f"expected <prototype>_<field> or <prototype>_<yyyymmdd>_<sca>")
    if not tablename.startswith(prototype + "_"):
        raise InputError(
            f"child table {tablename!r} does not belong to prototype "
            f"{prototype!r}")
    return tablename


def create_child_table(cursor, tablename: str, prototype: str,
                       inherit: bool = False) -> bool:
    """Create one per-field or per-date child table, carrying the prototype's
    indexes. Returns True if it was created, False if it already existed.

    **`INCLUDING INDEXES` IS THE FIX** (co-design ruling 6). The old clone
    path used `LIKE <prototype> INCLUDING DEFAULTS INCLUDING CONSTRAINTS`,
    which copies column defaults and CHECK constraints and nothing else — no
    indexes, and critically no UNIQUE index. `crossMatchSources.py:966-967`
    then created four indexes by hand, a list written before migration 027
    added `merges_aid_sid_unique`, so every clone this pipeline has ever made
    lacked the uniqueness the design requires. Migration 027 says so
    explicitly: the per-field constraints "land with the conversion's
    staging-plus-upsert load path".

    `INCLUDING INDEXES` carries every index the prototype has, including the
    unique one, and keeps carrying them if a later migration adds another —
    which the hand-written list could not do.

    NO `SET UNLOGGED`. The old path issued it unconditionally right here, and
    for the crossmatch pair it issued it on EVERY pass, outside the creation
    guard (evidence §3.3). See this module's header.
    """
    validate_child_name(tablename, prototype)

    # `IF NOT EXISTS` rather than a prior existence probe: the probe was a
    # separate round trip whose answer could be stale by the time the CREATE
    # ran, and two units of the same field racing is a real shape here.
    cursor.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {child} "
                "(LIKE {proto} INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
                "INCLUDING INDEXES)").format(
            child=sql.Identifier(tablename), proto=sql.Identifier(prototype)))
    created = cursor.rowcount != 0 if cursor.rowcount is not None else True

    if inherit:
        # The sources children inherit the prototype so a query against
        # `sources` sees them. Guarded by catalog lookup because ALTER TABLE
        # ... INHERIT has no IF NOT EXISTS and re-issuing it on an already
        # inheriting child raises.
        cursor.execute(
            "SELECT 1 FROM pg_inherits i"
            " JOIN pg_class c ON c.oid = i.inhrelid"
            " JOIN pg_class p ON p.oid = i.inhparent"
            " WHERE c.relname = %s AND p.relname = %s",
            (tablename, prototype))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("ALTER TABLE {child} INHERIT {proto}").format(
                    child=sql.Identifier(tablename),
                    proto=sql.Identifier(prototype)))

    logger.info("child table %s ready (prototype %s, indexes carried)",
                tablename, prototype)
    return created


def load_through_staging(cursor, csv_path: str, tablename: str,
                         prototype: str, columns) -> dict:
    """Bulk-load a CSV into a child table through a staging table and an
    upsert. Returns `{"rows_staged", "rows_written", "seconds", "rate"}`.

    **THE SHAPE THE DESIGN ADOPTED** (database design § Integrity and
    durability): "Bulk loads land through a staging table and an upsert so a
    rerun cannot produce duplicate rows; the load rate of that shape is
    measured at implementation."

    Three properties this has and a raw `COPY` into the target does not:

    1. **A rerun converges.** `ON CONFLICT DO NOTHING` writes the rows the
       target lacks and skips the ones it has. A unit that died halfway
       through its load can simply be re-submitted. Against a constrained
       target, the old raw COPY would instead abort on the first row it had
       already written — the retry contract the co-design requires would have
       been unusable.
    2. **The duplicate is refused, not absorbed.** The conflict target names
       the real unique index, so two different rows claiming the same identity
       do not both land. Without a conflict target — or against a table whose
       clone dropped the index — they would.
    3. **The effect is countable.** `rows_written` is what the INSERT actually
       wrote, which is the number the attempt record carries. A raw COPY
       reports how many rows it read, which after a partial rerun is not the
       same question.

    The staging table is `TEMP`, so it is dropped when the connection ends
    and never appears in `pg_tables` for a sweep to find. It is deliberately
    UNCONSTRAINED: staging is where duplicates are allowed to arrive, and the
    upsert is where they are resolved.
    """
    validate_child_name(tablename, prototype)

    conflict = CONFLICT_TARGETS.get(prototype)
    staging = f"staging_{tablename}"

    started = time.monotonic()

    # `LIKE ... INCLUDING DEFAULTS` and nothing else: no indexes, no
    # constraints. Staging must accept whatever the CSV holds — resolving
    # what is duplicate is the upsert's job, and a constraint here would
    # abort the COPY instead, which is the behaviour being replaced.
    cursor.execute(
        sql.SQL("CREATE TEMP TABLE {staging} "
                "(LIKE {target} INCLUDING DEFAULTS) ON COMMIT DROP").format(
            staging=sql.Identifier(staging),
            target=sql.Identifier(tablename)))

    with open(csv_path, "r") as handle:
        cursor.copy_from(handle, staging, sep=",", null="\\N",
                         columns=tuple(columns))
    rows_staged = cursor.rowcount

    column_list = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    if conflict:
        statement = sql.SQL(
            "INSERT INTO {target} ({cols}) SELECT {cols} FROM {staging} "
            "ON CONFLICT ({conflict}) DO NOTHING").format(
                target=sql.Identifier(tablename), cols=column_list,
                staging=sql.Identifier(staging),
                conflict=sql.SQL(", ").join(
                    sql.Identifier(c) for c in conflict))
    else:
        # No unique index on this prototype, so there is no conflict to
        # resolve and no target to name. `ON CONFLICT` with no target would
        # silently swallow a violation of ANY constraint, including one a
        # later migration adds for a reason this code knows nothing about.
        statement = sql.SQL(
            "INSERT INTO {target} ({cols}) SELECT {cols} FROM {staging}"
        ).format(target=sql.Identifier(tablename), cols=column_list,
                 staging=sql.Identifier(staging))

    cursor.execute(statement)
    rows_written = cursor.rowcount

    elapsed = time.monotonic() - started
    rate = (rows_written / elapsed) if elapsed > 0 else 0.0

    logger.info("loaded %s: %d staged, %d written in %.2fs (%.0f rows/s)",
                tablename, rows_staged, rows_written, elapsed, rate)

    return {"rows_staged": rows_staged, "rows_written": rows_written,
            "seconds": round(elapsed, 3), "rate": round(rate, 1)}


def delete_superseded_rows(cursor, tablename: str, prototype: str,
                           join_column: str, identity_table: str,
                           identity_column: str) -> int:
    """Remove rows whose parent image is no longer current. Returns the count.

    **Row currency is a DERIVED property** (operations design § Post-DB
    science chain): "a row is current while the image it derives from holds
    best status, and the currency sweeps remove rows whose image has been
    demoted". This is that removal, expressed as one set-based statement.

    `pruneNotBestMerges.py` did this row by row: it selected every row, ran
    `SELECT vbest FROM diffimages WHERE pid = %s` per row, and issued a
    single-row DELETE for each that had been demoted. On a field with
    millions of merges that is millions of round trips to answer a question
    one join answers.

    `vbest IN (1, 2)` is what "current" means — 1 is current-best and 2 is a
    locked operator pin (catalog design § Promotion). Reading only `= 1`
    would delete rows under a pinned image, which is the one thing a pin
    exists to prevent.
    """
    validate_child_name(tablename, prototype)

    if not identity_column.isidentifier() or not join_column.isidentifier():
        raise ConfigError(
            f"column names must be identifiers; got {join_column!r} and "
            f"{identity_column!r}")

    cursor.execute(
        sql.SQL(
            "DELETE FROM {child} WHERE NOT EXISTS ("
            "  SELECT 1 FROM {identity} WHERE {identity}.{idcol} = "
            "  {child}.{joincol} AND {identity}.vbest IN (1, 2))").format(
                child=sql.Identifier(tablename),
                identity=sql.Identifier(identity_table),
                idcol=sql.Identifier(identity_column),
                joincol=sql.Identifier(join_column)))
    removed = cursor.rowcount or 0

    logger.info("currency sweep on %s removed %d superseded row(s)",
                tablename, removed)
    return removed


def count_duplicate_groups(cursor, tablename: str, prototype: str) -> int:
    """How many duplicate identity groups a per-field table holds.

    **The should-find-nothing check** (co-design ruling 6). With
    `merges_aid_sid_unique` on the prototype and the clone path carrying it,
    a duplicate cannot be inserted — so this counts a condition that should
    be structurally impossible, and a nonzero answer is a defect report about
    the constraint rather than rows to quietly delete.

    Which is why this COUNTS and does not DELETE. The old
    `pruneRedundantMerges.py` deleted, because before the constraint existed
    deletion was the only remedy; deleting now would hide exactly the
    evidence that something is wrong with prevention.
    """
    validate_child_name(tablename, prototype)

    conflict = CONFLICT_TARGETS.get(prototype)
    if not conflict:
        raise ConfigError(
            f"prototype {prototype!r} declares no identity columns, so "
            f"'duplicate' is undefined for it; known: "
            + ", ".join(sorted(CONFLICT_TARGETS)))

    keys = sql.SQL(", ").join(sql.Identifier(c) for c in conflict)
    cursor.execute(
        sql.SQL("SELECT count(*) FROM (SELECT {keys} FROM {child} "
                "GROUP BY {keys} HAVING count(*) > 1) AS duplicates").format(
            keys=keys, child=sql.Identifier(tablename)))
    row = cursor.fetchone()
    return int(row[0]) if row else 0
