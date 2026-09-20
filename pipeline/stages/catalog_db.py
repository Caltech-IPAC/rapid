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
    # (pid, id, isdiffpos) — migration 041 (mission mock, live 2026-08-09):
    # `id` is a PER-FILE ordinal and a product has two files (positive and
    # negative), so the sign is part of the identity. The entry was missing
    # entirely before 041, so the sources upsert ran without ON CONFLICT
    # and the first genuine rerun-or-collision aborted the load; naming
    # (pid, id) alone would instead have silently dropped every negative
    # detection.
    "sources": ("pid", "id", "isdiffpos"),
}

# Child-table names are `<prototype>_<field>` or `<prototype>_<date>_<sca>`.
# Validated rather than trusted: these are composed into DDL, and a name
# arriving from a manifest is submitter input like any other.
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9]*_[0-9]+$")
_DATE_SCA_NAME = re.compile(r"^[a-z][a-z0-9]*_[0-9]{8}_[0-9]+$")

# The same attempt-to-run join rapid_systems migration 132's
# `derived.scratch_run_for_attempt` documents and applies server-side
# (`attempts.run_id` is TEXT and is the run's NAME, possibly with a
# split-pass `-<n>` suffix — 108's own comment, 131's own resolution logic
# in the reverse direction). Read here, client-side, for the ONE thing SQL
# cannot decide for Python: which of two entirely different call paths to
# take BEFORE any statement is issued — call the invoker-rights function a
# production attempt has always called, unchanged, or call migration 132's
# `derived.scratch_*` wrapper, which is the only thing a scratch attempt's
# identity (`rapid_scratch_pipeline`, no table-level grant anywhere) is
# privileged to call at all. The wrapper re-derives and re-fences the same
# run from the same attempt_id independently once called — this probe is
# Python's own dispatch decision, not a substitute for that fence.
_SCRATCH_RUN_PROBE = (
    "SELECT r.kind FROM public.attempts a"
    " JOIN public.runs r ON a.run_id LIKE r.name || '%%'"
    " WHERE a.attempt_id = %s"
)


def is_scratch_attempt(cursor, attempt_id) -> bool:
    """Does `attempt_id` belong to a `kind = 'scratch'` run?

    False for a production attempt in BOTH the ways that can happen: no
    `runs` row matches at all (the ordinary case — a run exists only as a
    string prefix on `attempts.run_id` for every production attempt and the
    301 historical run identifiers that predate the registry, 108's own
    header), or a `runs` row matches with `kind = 'production'`. Either way
    the caller takes today's unchanged code path. Only an exact
    `kind = 'scratch'` match routes to a `derived.scratch_*` wrapper.

    One attempt can only ever belong to one run, so at most one row can
    match; `fetchone()` is enough.
    """
    cursor.execute(_SCRATCH_RUN_PROBE, (attempt_id,))
    row = cursor.fetchone()
    return row is not None and row[0] == "scratch"


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


def require_table(cursor, tablename: str, prototype: str) -> None:
    """Refuse, as `input_missing`, when a declared target does not exist.

    **A DECLARED UNIT WHOSE TARGET IS ABSENT IS A SUBMISSION FACT, NOT A
    CRASH** — and this exists because the first live probe proved the
    difference. A merge-dedup unit was submitted for a field with no
    `merges_<field>` clone; the query raised a bare
    `psycopg2.errors.UndefinedTable`, which is not in the runtime taxonomy,
    so the attempt closed `internal_error`. That reads as "the pipeline is
    broken" when what actually happened is "the submitter named a unit whose
    table has not been created yet" — an ordinary, expected state for a field
    that has never been crossmatched.

    `input_missing` is the honest classification: the manifest is this
    invocation's input, and the declared target it names is not there. The
    reconciler and the problems path treat the two categories differently,
    so the distinction is not cosmetic.

    Checked BEFORE the statement rather than by catching the driver error
    after, so the message names the unit and the prototype rather than
    quoting a SQL fragment.
    """
    cursor.execute(
        "SELECT 1 FROM pg_catalog.pg_tables"
        " WHERE schemaname = 'public' AND tablename = %s", (tablename,))
    if cursor.fetchone() is None:
        raise InputError(
            f"the declared target table {tablename!r} does not exist. The "
            f"unit was enumerated at submission against the {prototype!r} "
            f"prototype's clones; either the clone has not been created yet "
            f"(a field that has never been crossmatched is the ordinary "
            f"case) or the manifest named a field that never had one. This "
            f"is a submission-input fact, not a pipeline fault.",
            table=tablename, prototype=prototype)


def create_child_table(cursor, tablename: str, prototype: str,
                       inherit: bool = False) -> bool:
    """Create one per-field or per-date child table, carrying the prototype's
    indexes. Returns True if it was created, False if it already existed.

    **DB-HARDENING: routed through `derived.create_child_table()` (rapid_
    systems migration 072), not composed here.** `rapid_pipeline_write` no
    longer holds `CREATE ON SCHEMA public` (migration 073 revokes the grant
    003 issued) — this SECURITY DEFINER function is now the only way this
    role can create a table in `public`, and it re-validates the prototype
    (against the same four-name allowlist) and the child-table name/
    ownership shape independently of `validate_child_name` below, so this
    call is not the sole enforcement point even though it still runs first.
    The function's own body carries the `INCLUDING INDEXES`/`INHERIT`/
    grant-copy logic this docstring used to describe directly; see 072's
    header in rapid_systems for the full account of what moved and why.

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

    cursor.execute(
        "SELECT derived.create_child_table(%s, %s, %s)",
        (tablename, prototype, inherit))
    created = cursor.fetchone()[0]

    logger.info("child table %s ready (prototype %s, indexes carried)",
                tablename, prototype)
    return created


def load_through_staging(cursor, csv_path: str, tablename: str,
                         prototype: str, columns, attempt_id=None) -> dict:
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

    `attempt_id`, SCRATCH DISPATCH (rapid_systems migration 132). `None` —
    every existing caller before this parameter existed, and every
    production caller from here on — takes exactly today's path: this
    connection authenticates as `rapid_pipeline`, which holds a direct
    table INSERT grant, so the raw upsert below runs as it always has, byte
    for byte. Only `prototype == "sources"` has a matching wrapper
    (`derived.scratch_load_sources_staged` takes one already-populated
    staging table and one target, which is exactly this function's own
    grain); passed a non-None `attempt_id` for that prototype, the CREATE
    TEMP TABLE and COPY above still run exactly as for production — a SQL
    function cannot open a local CSV or drive `COPY FROM STDIN` (132's file
    header) — and only the final INSERT...SELECT...ON CONFLICT is replaced
    by the wrapper call, which re-derives and re-fences the run from
    `attempt_id` itself before touching the target table.

    `prototype in ("astroobjects", "merges")` has NO per-call match: the
    migration's sibling wrapper, `derived.scratch_load_associations_staged`,
    takes BOTH staging tables and BOTH targets and writes them together in
    one transaction, because a scratch identity with no table grant can only
    reach the shared tables through a function call, and this function is
    called once PER TABLE by `crossmatch_sources` (`post_db.py`). Forcing a
    two-table-atomic wrapper into a one-table-at-a-time call here would
    either drop the atomicity the wrapper exists to keep or require this
    function to know about its sibling call — both worse than leaving the
    mismatch visible. `crossmatch_sources` is therefore NOT wired to scratch
    by this change; see its own call sites for the same note.
    """
    validate_child_name(tablename, prototype)

    conflict = CONFLICT_TARGETS.get(prototype)
    staging = f"staging_{tablename}"

    started = time.monotonic()

    # `LIKE ... INCLUDING DEFAULTS` and nothing else: no indexes, no
    # constraints. Staging must accept whatever the CSV holds — resolving
    # what is duplicate is the upsert's job, and a constraint here would
    # abort the COPY instead, which is the behaviour being replaced.
    #
    # Unconditional, scratch or production: a SQL function cannot open the
    # local CSV or drive a client-side `COPY FROM STDIN` (132's file
    # header), so the caller does this step itself either way — only the
    # THIRD step below (the shared-table write) is what a scratch identity
    # cannot do directly and needs the wrapper for.
    cursor.execute(
        sql.SQL("CREATE TEMP TABLE {staging} "
                "(LIKE {target} INCLUDING DEFAULTS) ON COMMIT DROP").format(
            staging=sql.Identifier(staging),
            target=sql.Identifier(tablename)))

    with open(csv_path, "r") as handle:
        cursor.copy_from(handle, staging, sep=",", null="\\N",
                         columns=tuple(columns))
    rows_staged = cursor.rowcount

    if (attempt_id is not None and prototype == "sources"
            and is_scratch_attempt(cursor, attempt_id)):
        # THROUGH THE WRAPPER, NOT THE RAW UPSERT (scratch run kind).
        # `derived.scratch_load_sources_staged` re-validates p_target's own
        # shape and re-confirms p_staging is a TEMP relation visible in this
        # session before it runs the identical `INSERT ... ON CONFLICT (pid,
        # id, isdiffpos) DO NOTHING` this branch's production sibling issues
        # directly below — see 132 §5. `rows_written` is its own return
        # value (ROW_COUNT off the same statement), not re-derived here.
        cursor.execute(
            "SELECT derived.scratch_load_sources_staged(%s, %s, %s)",
            (attempt_id, staging, tablename))
        rows_written = cursor.fetchone()[0]
    else:
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
            # resolve and no target to name. `ON CONFLICT` with no target
            # would silently swallow a violation of ANY constraint,
            # including one a later migration adds for a reason this code
            # knows nothing about.
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

    require_table(cursor, tablename, prototype)

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


def delete_superseded_merge_rows(cursor, tablename: str) -> int:
    """Remove a field's merge rows whose SOURCE's difference image is not current.

    **The identity spaces.** A `merges_<field>` row is `(aid, sid)`; `sid` is
    a `sources` row's primary key, and the image it derives from is reached
    through `sources.pid` -> `diffimages.pid`. The generic
    `delete_superseded_rows` compares one column of the child with one
    column of an identity table, which is the wrong model here: called with
    `join_column="sid", identity_table="diffimages", identity_column="pid"`
    it compares a source id with an image id — two unrelated sequences — and
    deletes whichever rows happen to collide. Verified on PostgreSQL 18 with
    colliding ids (2026-09-15): the one-hop form deleted a row under a
    pinned image and a current row and KEPT the demoted one; this two-hop
    form deleted exactly the demoted one, in either sweep order.

    `sources` is the inheritance parent, so the join reaches every
    `sources_<date>_<sca>` child. `vbest IN (1, 2)` is what "current" means
    (1 current-best, 2 a locked operator pin). A merge whose source row has
    itself been swept is superseded by the same rule and goes too.

    Dev's `pruneNotBestMerges.py` (0d67e4ea) built a temporary table of
    not-best sids from `sources JOIN diffimages ON pid` and deleted merges
    by `sid IN (...)`; this is that intent as one statement.
    """
    validate_child_name(tablename, "merges")
    require_table(cursor, tablename, "merges")

    cursor.execute(
        sql.SQL(
            "DELETE FROM {child} AS m WHERE NOT EXISTS ("
            "  SELECT 1 FROM sources AS s"
            "  JOIN diffimages AS d ON d.pid = s.pid"
            "  WHERE s.sid = m.sid AND d.vbest IN (1, 2))").format(
                child=sql.Identifier(tablename)))
    removed = cursor.rowcount or 0

    logger.info("merge currency sweep on %s removed %d superseded row(s)",
                tablename, removed)
    return removed


def delete_superseded_source_rows(cursor, field: int) -> int:
    """Remove a field's SOURCE rows whose difference image is not current.

    This is the sweep dev's `pruneNotBestSources.py` performed
    (`DELETE FROM sources_<child> AS a USING diffimages AS b WHERE a.pid =
    b.pid AND b.vbest = 0`, 0d67e4ea) and that nothing on smdc performed
    until now: `sweep_source_currency` deleted from `merges_<field>` — the
    same table as the merge sweep — through `merges.sid = l2files.rid`, two
    unrelated identity spaces. On the same PostgreSQL check as above that
    predicate deleted every row of the table.

    The unit is per FIELD (`gather_source_currency_units`), and the sources
    children are per (date, SCA), so the rows of one field live across many
    children. The DELETE therefore targets the inheritance PARENT with a
    `field` predicate; PostgreSQL routes it to every child (EXPLAIN shows a
    Delete on each child), using the per-child `field` index migration 007
    carries. `vbest IN (1, 2)` is what "current" means, as everywhere else.
    """
    field = int(field)

    cursor.execute(
        sql.SQL(
            "DELETE FROM sources AS s WHERE s.field = %s AND NOT EXISTS ("
            "  SELECT 1 FROM diffimages AS d"
            "  WHERE d.pid = s.pid AND d.vbest IN (1, 2))"),
        (field,))
    removed = cursor.rowcount or 0

    logger.info("source currency sweep for field %d removed %d superseded "
                "row(s)", field, removed)
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

    require_table(cursor, tablename, prototype)

    keys = sql.SQL(", ").join(sql.Identifier(c) for c in conflict)
    cursor.execute(
        sql.SQL("SELECT count(*) FROM (SELECT {keys} FROM {child} "
                "GROUP BY {keys} HAVING count(*) > 1) AS duplicates").format(
            keys=keys, child=sql.Identifier(tablename)))
    row = cursor.fetchone()
    return int(row[0]) if row else 0
