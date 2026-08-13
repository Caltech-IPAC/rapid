"""The production readers are unchanged by the D migrations (criterion 6).

"Reference selection, post-DB gathering, forced photometry, alert
companion-file anchoring, currency sweep, and catalog-load sibling
derivation produce identical results before and after the D migrations on
identical fixture data."

**WHAT THE MIGRATION ACTUALLY DID TO THESE TABLES**, which is what decides
what can break: it added ONE nullable column (`product_id`) plus an index
and a foreign key to each of `refimages` and `diffimages`. It changed no
existing column, no existing constraint and no existing row. (CR-8 —
rapid_systems migration 054, out of D's scope — later appended a second
nullable column, `checksum_algorithm`, the same `ADD COLUMN` way; see
`TRAILING_COLUMNS_AFTER_PRODUCT_ID` below. Everything in this module's
reasoning holds for an appended column regardless of which migration
appended it.) So the ways a reader could break are exactly three, and each
is tested here rather than argued:

  1. A `SELECT *` whose caller unpacks positionally would gain a field and
     shift everything after it. (Surveyed: every `select *` in the tree is
     over a stored FUNCTION — `select * from addRefImage(...)` — whose
     shape is fixed by the function signature, not by the table. The
     survey is a claim; `test_no_reader_selects_star_from_the_altered
     _tables` is the check.)
  2. A query whose RESULT SET changes because the new FK or index changed
     which rows match or in what order they come back. It cannot — a
     nullable column with no default adds NULL to every existing row and
     participates in no existing predicate — but "cannot" is what this
     tier exists to stop asserting from an armchair.
  3. An INSERT that names no column list would now be short one value.
     (Checked below.)

**THE RESULTS ARE COMPARED, NOT THE ROW COUNTS.** Brief D: "Acceptance
asserts these readers' query RESULTS are unchanged, not merely that rows
exist." Each reader below runs its REAL SQL — lifted from the production
call site, not paraphrased — against fixture rows, and the full result
tuples are compared against the same query run with the new column
explicitly excluded, which is the "before" state as far as any reader can
observe it.
"""

import pytest

from pipeline.contract import fixture

#: The two tables the D migration altered.
ALTERED_TABLES = ("refimages", "diffimages")


def _require_schema(conn):
    if not fixture.has_table(conn, "products"):
        pytest.skip("DRAFT migration 048 is not applied on this database")


def _columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = %s ORDER BY ordinal_position", [table])
        return [name for (name,) in cur.fetchall()]


# ---------------------------------------------------------------------------
# What the migration changed, and what it did not
# ---------------------------------------------------------------------------


#: Columns appended AFTER `product_id` by later migrations, in the order
#: they were added — each its own `ADD COLUMN`, so each one only extends
#: the tail rather than disturbing anything before it. `checksum_algorithm`
#: is CR-8 (rapid_systems migration 054), landed after D's `product_id`
#: (048) in stream order; widening `checksum` itself is an `ALTER COLUMN
#: ... TYPE`, not an append, so it does not appear here.
TRAILING_COLUMNS_AFTER_PRODUCT_ID = ("checksum_algorithm",)


@pytest.mark.parametrize("table", ALTERED_TABLES)
def test_the_migration_added_exactly_one_column_and_it_is_last(conn, table):
    """`product_id` is appended, nullable, and nothing before it moved.

    Appended matters: a column added in the MIDDLE would shift the ordinal
    positions every positional unpack depends on. PostgreSQL's `ADD COLUMN`
    always appends, so this asserts the property rather than establishing
    it — which is the point, because the property is what the readers rely
    on and an assertion is what survives a future edit to this file.

    `product_id` is no longer literally the LAST column: CR-8 (054)
    appended `checksum_algorithm` after it. That is a second `ADD COLUMN`,
    which only extends the tail — a positional unpack that already
    tolerated `product_id`'s arrival tolerates this one the same way, and
    nothing between `product_id` and the columns every reader actually
    selects has shifted. The test now asserts `product_id` is immediately
    followed by exactly the known, later-appended trailing columns, not
    that it is the last column absolutely.
    """
    _require_schema(conn)
    columns = _columns(conn, table)
    expected_tail = ("product_id",) + TRAILING_COLUMNS_AFTER_PRODUCT_ID
    actual_tail = tuple(columns[-len(expected_tail):])
    assert actual_tail == expected_tail, (
        f"{table}'s trailing columns are {actual_tail}, not {expected_tail}; "
        f"either a migration inserted a column ahead of product_id (shifting "
        f"every positional unpack of a `select *` over this table) or a new "
        f"appended column needs adding to TRAILING_COLUMNS_AFTER_PRODUCT_ID")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable, column_default FROM information_schema"
            ".columns WHERE table_name = %s AND column_name = 'product_id'",
            [table])
        is_nullable, default = cur.fetchone()
    assert is_nullable == "YES", (
        "a NOT NULL product_id would make this migration unappliable to any "
        "database with history, since the identity components of an existing "
        "row are not recoverable")
    assert default is None


@pytest.mark.parametrize("table", ALTERED_TABLES)
def test_no_existing_column_changed_type_or_nullability(conn, table):
    """The legacy columns every reader depends on are untouched.

    Spot-checked on the ones the named readers actually select:
    `filename`, `checksum`, `version`, `vbest`, `status`.
    """
    _require_schema(conn)
    expected = {
        "version": ("smallint", "NO"),
        "vbest": ("smallint", "NO"),
        "status": ("smallint", "NO"),
    }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable"
            " FROM information_schema.columns WHERE table_name = %s", [table])
        actual = {name: (kind, nullable)
                  for name, kind, nullable in cur.fetchall()}
    for column, expectation in expected.items():
        assert actual[column] == expectation, (
            f"{table}.{column} is {actual[column]}, expected {expectation}")

    # The checksum column keeps its (defective) width — asserted in
    # `test_artifact_checksum.py`, referenced here so the two tests are
    # visibly about the same column.
    assert actual["checksum"][0] == "character varying"


def test_no_reader_selects_star_from_the_altered_tables():
    """No production query does `select * from refimages|diffimages`.

    The one way an appended column CAN break a positional unpack. Checked
    over the source rather than over the database, because the risk is a
    query somebody writes, not a row somebody stores.

    `select * from addRefImage(...)` and its siblings are stored-FUNCTION
    calls whose result shape comes from the function signature, so they are
    not matches for this pattern and are not affected by a table column.
    """
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    pattern = re.compile(
        r"select\s+\*\s+from\s+(refimages|diffimages)\b", re.IGNORECASE)

    # THIS FILE IS EXCLUDED, and that exclusion is the interesting part.
    # The first version of this test reported itself: the pattern it scans
    # for is written down here, so the scanner matched its own source. A
    # scanner that cannot be run over the tree containing it is a scanner
    # nobody will keep running — and the honest exclusion is this file by
    # path, not a weakened pattern that would also stop matching the real
    # thing.
    this_file = os.path.abspath(__file__)

    offenders = []
    for directory in ("pipeline", "submission", "alerts", "database",
                      "modules", "observability"):
        base = os.path.join(root, directory)
        if not os.path.isdir(base):
            continue
        for current, _dirs, files in os.walk(base):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(current, name)
                if os.path.abspath(path) == this_file:
                    continue
                with open(path, encoding="utf-8", errors="replace") as handle:
                    body = handle.read()
                if pattern.search(body):
                    offenders.append(os.path.relpath(path, root))

    assert not offenders, (
        "these files select every column of a table the D migration "
        f"appended a column to: {offenders}")


# ---------------------------------------------------------------------------
# The named readers, by their real SQL
# ---------------------------------------------------------------------------

#: Each reader's REAL query, lifted from its production call site. Kept
#: verbatim (including the legacy spelling and case) because a paraphrase
#: would be testing a query nobody runs — the same reason
#: `DiffImageRepository` carries its SQL over unchanged.
#:
#: Each entry is a CALLABLE taking the seeded fixture's identities, so every
#: query is parameterized to select exactly the rows this run created. The
#: earlier version used fixed literals (`ppid = 15`, `pid = 1`) that matched
#: nothing on a fresh scratch database, which is how five of the six readers
#: came to be compared on empty result sets.
READERS = {
    # `RAPIDDB.get_best_reference_image` (rapid_db.py:1680) — reference
    # selection. The column list is explicit, which is why appending a
    # column cannot reach it.
    "reference_selection": lambda s: (
        "select rfid,filename,infobits,version from RefImages"
        " where vbest > 0 and status > 0 and ppid = %s and field = %s"
        " and fid = %s",
        (s["ppid"], s["field"], s["fid"])),
    # `get_registered_diffimages_for_processing_date_sca`
    # (rapid_db.py:3131) — post-DB gathering reading `diffimages.filename`.
    "post_db_gathering": lambda s: (
        "select pid,filename,checksum from DiffImages"
        " where vbest > 0 and status > 0 and ppid = %s and field = %s"
        " order by pid",
        (s["ppid"], s["field"])),
    # `pipeline/repositories/diffimages.py:72` — forced photometry's join
    # reading URI and checksum off both tables.
    "forced_photometry_join": lambda s: (
        "select a.pid,a.filename,a.checksum,b.filename,b.checksum"
        " from DiffImages a, RefImages b where a.rfid = b.rfid"
        " and a.status > 0 and b.status > 0 and a.vbest > 0 and b.vbest > 0"
        " and a.field = %s order by a.pid",
        (s["field"],)),
    # `alerts/providers.py:584,989-1001` — PID selection and the
    # `diffimages.filename` directory anchor for companion files.
    "alert_companion_anchor": lambda s: (
        "select pid,filename from DiffImages where vbest > 0 and pid = %s",
        (s["pid"],)),
    # `pipeline/stages/catalog_db.py:358` call sites — the currency sweep
    # over pid/vbest.
    "currency_sweep": lambda s: (
        "select pid,vbest,version from DiffImages where vbest > 0"
        " and ppid = %s and field = %s order by pid",
        (s["ppid"], s["field"])),
    # `submission/gathering.py:1205` — catalog-load derives the sibling
    # catalogue names from the diffimage path.
    "catalog_load_sibling": lambda s: (
        "select pid,filename from DiffImages where status > 0 and vbest > 0"
        " and field = %s order by pid",
        (s["field"],)),
}


# ---------------------------------------------------------------------------
# The fixture: real `refimages` and `diffimages` rows for the readers to find
# ---------------------------------------------------------------------------
#
# EVERY NAMED READER IS EXERCISED ON NON-EMPTY RESULTS. The first version of
# this file ran five of the six readers twice against a freshly-built scratch
# database and compared two EMPTY result sets — which is a tautology, not a
# compatibility test: it would have passed just as well if the migration had
# dropped the column the reader selects. Only `reference_selection` had real
# rows. This fixture gives all six real rows, so "results are unchanged" is a
# statement about rows that exist.


def _reference_row(cur, ppid, fid, svid, field, suffix):
    """One `refimages` row, returning its rfid."""
    cur.execute(
        "INSERT INTO refimages (field, hp6, hp9, fid, ppid, version,"
        " vbest, filename, status, checksum, svid, infobits)"
        " VALUES (%s, 1, 1, %s, %s, 1, 1, %s, 1, %s, %s, 0)"
        " RETURNING rfid",
        [field, fid, ppid, f"s3://bucket/{fixture.RUN_TAG}/ref{suffix}.fits",
         "a" * 32, svid])
    return cur.fetchone()[0]


def _l2file_row(cur, expid, sca, field, fid):
    """One `l2files` row, returning its rid.

    **BUILT FROM `information_schema`, NOT FROM A HAND-WRITTEN COLUMN LIST.**
    `l2files` has 61 NOT NULL columns — the WCS keywords and the full 4th/5th
    order SIP coefficient set — and none of them is read by any reader under
    test here. Enumerating them by hand would be sixty lines of zeroes that
    say nothing, and would silently break the moment the stream adds a
    column. Asking the catalog for the required columns and filling each with
    a type-appropriate placeholder keeps the fixture honest about what it
    cares about (the identity columns, set explicitly below) and indifferent
    to the rest.
    """
    explicit = {
        "expid": expid, "sca": sca, "field": field, "fid": fid,
        "version": 1, "vbest": 1, "status": 1, "infobits": 0,
        "filename": f"s3://bucket/{fixture.RUN_TAG}/sci{expid}_{sca}.fits",
        "checksum": "b" * 32,
    }
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_name = 'l2files' AND is_nullable = 'NO'"
        "   AND column_default IS NULL"
        "   AND is_identity = 'NO'"
        " ORDER BY ordinal_position")
    columns, values = [], []
    for column, kind in cur.fetchall():
        columns.append(f'"{column}"')
        if column in explicit:
            values.append(explicit[column])
        elif kind in ("timestamp with time zone", "timestamp without time zone",
                      "date"):
            values.append("2026-08-12T00:00:00Z")
        elif kind in ("character varying", "text", "character"):
            values.append("x")
        else:
            values.append(0)
    placeholders = ", ".join(["%s"] * len(values))
    cur.execute(
        f"INSERT INTO l2files ({', '.join(columns)})"
        f" VALUES ({placeholders}) RETURNING rid", values)
    return cur.fetchone()[0]


def _difference_row(cur, rid, rfid, ppid, fid, svid, expid, sca, field):
    """One `diffimages` row, returning its pid."""
    cur.execute(
        "INSERT INTO diffimages (rid, expid, sca, ppid, version, vbest,"
        " rfid, field, hp6, hp9, fid, ra0, dec0, ra1, dec1, ra2, dec2,"
        " ra3, dec3, ra4, dec4, infobitssci, infobitsref, filename,"
        " checksum, status, svid)"
        " VALUES (%s, %s, %s, %s, 1, 1, %s, %s, 1, 1, %s,"
        "         10.0, 20.0, 10.1, 20.1, 10.2, 20.2, 10.3, 20.3,"
        "         10.4, 20.4, 0, 0, %s, %s, 1, %s)"
        " RETURNING pid",
        [rid, expid, sca, ppid, rfid, field, fid,
         f"s3://bucket/{fixture.RUN_TAG}/diff{expid}_{sca}.fits",
         "c" * 32, svid])
    return cur.fetchone()[0]


@pytest.fixture
def seeded(conn):
    """Real rows every named reader can find, rolled back after the test.

    Returns the identity values the reader queries are parameterized by, so
    each test selects EXACTLY the rows this fixture created rather than
    whatever else the shared scratch database happens to hold — the
    fixture-honesty discipline this tier runs on.
    """
    _require_schema(conn)
    field = int(fixture.RUN_TAG[:5], 16) % 100000

    with conn.cursor() as cur:
        for table, column in (("pipelines", "ppid"), ("filters", "fid"),
                              ("swversions", "svid"), ("scas", "sca")):
            cur.execute(f"SELECT {column} FROM {table}"
                        f" ORDER BY {column} LIMIT 1")
            if cur.fetchone() is None:
                pytest.skip(f"no {table} rows on this database to satisfy"
                            f" the foreign key")

        cur.execute("SELECT ppid FROM pipelines ORDER BY ppid LIMIT 1")
        ppid = cur.fetchone()[0]
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        fid = cur.fetchone()[0]
        cur.execute("SELECT svid FROM swversions ORDER BY svid LIMIT 1")
        svid = cur.fetchone()[0]
        cur.execute("SELECT sca FROM scas ORDER BY sca LIMIT 1")
        sca = cur.fetchone()[0]

        rfid = _reference_row(cur, ppid, fid, svid, field, "a")

        # `exposures.dateobs` is UNIQUE, so the run tag makes this run's
        # exposure its own rather than colliding with a previous run's.
        # `exposures.dateobs` is UNIQUE (`exposurespk`), so the timestamp is
        # offset by this run's own tag rather than fixed: two runs of this
        # suite against one scratch database must not collide, and a
        # collision here would look like the compatibility defect under test.
        cur.execute(
            "INSERT INTO exposures (dateobs, field, fid, exptime, mjdobs,"
            " status, infobits, hp6, hp9)"
            " VALUES (timestamptz '2026-08-12 00:00:00+00'"
            "         + make_interval(secs => %s), %s, %s, 100.0,"
            "         60000.0, 1, 0, 1, 1)"
            " RETURNING expid",
            [int(fixture.RUN_TAG[:6], 16) % 1000000, field, fid])
        expid = cur.fetchone()[0]

        rid = _l2file_row(cur, expid, sca, field, fid)
        pid = _difference_row(cur, rid, rfid, ppid, fid, svid, expid, sca,
                              field)

    yield {"ppid": ppid, "fid": fid, "svid": svid, "field": field,
           "sca": sca, "rfid": rfid, "expid": expid, "rid": rid, "pid": pid}
    conn.rollback()


@pytest.mark.parametrize("name", sorted(READERS))
def test_reader_results_are_identical_before_and_after(conn, seeded, name):
    """Each named reader returns the SAME NON-EMPTY rows before and after.

    "Before the D migrations" is executed, not asserted about: the same
    query runs against a derived table that carries every column of the real
    one EXCEPT `product_id`. If the appended column could reach a result —
    through a `select *`, a default, a predicate, or an ordering — the two
    result sets would differ. Comparing against a pre-migration VIEW is the
    only way to demonstrate that from inside a migrated database.

    The first version of this test compared the reader's output to itself
    and asserted the query text did not mention `product_id`. That is a
    statement about a string, and it passed on five readers whose result
    sets were both empty.
    """
    sql, params = READERS[name](seeded)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        after = cur.fetchall()

        # THE PRE-MIGRATION VIEW. Each altered table is shadowed by a
        # subquery selecting every column except `product_id`, so the query
        # runs against exactly the table shape that existed before 048.
        pre = sql
        for table in ALTERED_TABLES:
            cur.execute(
                "SELECT string_agg(quote_ident(column_name), ', '"
                "                  ORDER BY ordinal_position)"
                " FROM information_schema.columns"
                " WHERE table_name = %s AND column_name <> 'product_id'",
                [table])
            columns = cur.fetchone()[0]
            pre = _shadow(pre, table, f"(SELECT {columns} FROM {table})")
        cur.execute(pre, params)
        before = cur.fetchall()

    assert after, (
        f"the {name} reader returned NOTHING; comparing two empty result "
        f"sets demonstrates nothing about compatibility, so the fixture "
        f"must seed rows this reader can find")
    assert after == before, (
        f"the {name} reader's results differ between the migrated table and "
        f"a pre-migration view of it:\n  after  = {after}\n  before = "
        f"{before}")


def _shadow(sql, table, replacement):
    """Replace a bare table name in `sql` with a derived-table subquery.

    Word-boundary matched and case-insensitive, because the readers' SQL is
    carried over verbatim from production and spells its tables in the
    legacy mixed case (`from DiffImages a, RefImages b`). An alias following
    the name is preserved by matching only the identifier itself.
    """
    import re

    return re.sub(rf"\b{table}\b", replacement, sql, flags=re.IGNORECASE)


def test_readers_see_identical_results_across_the_migration_on_real_rows(
        conn):
    """The strong form: real rows, results compared to a pre-migration view.

    A `refimages` row is inserted, then read twice — once through the real
    reference-selection query, and once through the same query executed
    against a temporary view built from `information_schema` to EXCLUDE
    `product_id`. If the appended column could influence a result, the two
    would differ.

    The row is built with an explicit column list, which is also the third
    breakage this file tests for: an INSERT naming no columns would now be
    short one value.
    """
    _require_schema(conn)

    field = int(fixture.RUN_TAG[:5], 16) % 100000
    with conn.cursor() as cur:
        cur.execute("SELECT ppid FROM pipelines ORDER BY ppid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("no pipelines rows on this database to satisfy the FK")
        ppid = row[0]
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("no filters rows on this database to satisfy the FK")
        fid = row[0]
        cur.execute("SELECT svid FROM swversions ORDER BY svid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("no swversions rows to satisfy the FK")
        svid = row[0]

        cur.execute(
            "INSERT INTO refimages (field, hp6, hp9, fid, ppid, version,"
            " vbest, filename, status, checksum, svid, infobits)"
            " VALUES (%s, 1, 1, %s, %s, 1, 1, %s, 1, %s, %s, 0)"
            " RETURNING rfid",
            [field, fid, ppid, f"s3://bucket/{fixture.RUN_TAG}/ref.fits",
             "a" * 32, svid])
        rfid = cur.fetchone()[0]

        sql, _ = READERS["reference_selection"](
            {"ppid": ppid, "field": field, "fid": fid})
        cur.execute(sql, (ppid, field, fid))
        selected = cur.fetchall()

    assert selected, "the fixture row was not returned by reference selection"
    assert len(selected[0]) == 4, (
        "reference selection returned a different number of columns than its "
        "explicit select list; the appended column has reached a reader")
    assert selected[0][0] == rfid

    # And the new column is NULL on it, which is what "no reader is
    # migrated" means concretely.
    with conn.cursor() as cur:
        cur.execute("SELECT product_id FROM refimages WHERE rfid = %s",
                    [rfid])
        assert cur.fetchone()[0] is None

    conn.rollback()
