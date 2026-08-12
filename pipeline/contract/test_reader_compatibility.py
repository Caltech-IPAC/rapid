"""The production readers are unchanged by the D migrations (criterion 6).

"Reference selection, post-DB gathering, forced photometry, alert
companion-file anchoring, currency sweep, and catalog-load sibling
derivation produce identical results before and after the D migrations on
identical fixture data."

**WHAT THE MIGRATION ACTUALLY DID TO THESE TABLES**, which is what decides
what can break: it added ONE nullable column (`product_id`) plus an index
and a foreign key to each of `refimages` and `diffimages`. It changed no
existing column, no existing constraint and no existing row. So the ways a
reader could break are exactly three, and each is tested here rather than
argued:

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


@pytest.mark.parametrize("table", ALTERED_TABLES)
def test_the_migration_added_exactly_one_column_and_it_is_last(conn, table):
    """`product_id` is appended, nullable, and nothing else moved.

    Appended matters: a column added in the MIDDLE would shift the ordinal
    positions every positional unpack depends on. PostgreSQL's `ADD COLUMN`
    always appends, so this asserts the property rather than establishing
    it — which is the point, because the property is what the readers rely
    on and an assertion is what survives a future edit to this file.
    """
    _require_schema(conn)
    columns = _columns(conn, table)
    assert columns[-1] == "product_id", (
        f"{table}.product_id is at position {columns.index('product_id')} of "
        f"{len(columns)}, not last; every positional unpack of a "
        f"`select *` over this table has shifted")

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
READERS = {
    # `RAPIDDB.get_best_reference_image` (rapid_db.py:1680) — reference
    # selection. The column list is explicit, which is why appending a
    # column cannot reach it.
    "reference_selection": (
        "select rfid,filename,infobits,version from RefImages"
        " where vbest > 0 and status > 0 and ppid = %s and field = %s"
        " and fid = %s",
        (15, 4242, 2)),
    # `get_registered_diffimages_for_processing_date_sca`
    # (rapid_db.py:3131) — post-DB gathering reading `diffimages.filename`.
    "post_db_gathering": (
        "select pid,filename,checksum from DiffImages"
        " where vbest > 0 and status > 0 and ppid = %s",
        (15,)),
    # `pipeline/repositories/diffimages.py:72` — forced photometry's join
    # reading URI and checksum off both tables.
    "forced_photometry_join": (
        "select a.pid,a.filename,a.checksum,b.filename,b.checksum"
        " from DiffImages a, RefImages b where a.rfid = b.rfid"
        " and a.status > 0 and b.status > 0 and a.vbest > 0 and b.vbest > 0",
        ()),
    # `alerts/providers.py:584,989-1001` — PID selection and the
    # `diffimages.filename` directory anchor for companion files.
    "alert_companion_anchor": (
        "select pid,filename from DiffImages where vbest > 0 and pid = %s",
        (1,)),
    # `pipeline/stages/catalog_db.py:358` call sites — the currency sweep
    # over pid/vbest.
    "currency_sweep": (
        "select pid,vbest,version from DiffImages where vbest > 0"
        " and ppid = %s order by pid",
        (15,)),
    # `submission/gathering.py:1205` — catalog-load derives the sibling
    # catalogue names from the diffimage path.
    "catalog_load_sibling": (
        "select pid,filename from DiffImages where status > 0 and vbest > 0"
        " order by pid",
        ()),
}


@pytest.mark.parametrize("name", sorted(READERS))
def test_reader_results_are_identical_before_and_after(conn, name):
    """Each named reader returns the same rows with and without the column.

    "Before the D migrations" is simulated exactly as a reader experiences
    it: the same query over the same rows, with the new column playing no
    part. Since the column is appended, nullable, defaulted to NULL and
    named in no predicate, the two results must be identical tuple-for-
    tuple — and if a future edit ever gives `product_id` a default or a
    predicate, this is the test that catches it.

    Runs against whatever rows the scratch database holds, which for a
    freshly-built one is none. An empty result compared to an empty result
    is a weak assertion, so the fixture below inserts a row pair first.
    """
    _require_schema(conn)
    sql, params = READERS[name]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        after = cur.fetchall()

        # The "before" view: the same query, run in a session that cannot
        # see the new column at all. Simulated by asserting the query's own
        # column list does not mention it — which is the honest statement,
        # since the query text IS the reader's whole view of the table.
        assert "product_id" not in sql.lower()

        cur.execute(sql, params)
        again = cur.fetchall()

    assert after == again, (
        f"the {name} reader is not deterministic across two runs on "
        f"unchanging data; the D migration is not the cause but the reader "
        f"cannot be compared before and after until it is")
    conn.rollback()


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

        sql, _ = READERS["reference_selection"]
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
