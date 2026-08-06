"""Live proof that BOTH branches of the overlap query are valid SQL.

Round-4 finding #3. `get_overlapping_l2files` builds its exclusion clause
in two branches, and the "no exclusion" one was asked for by passing the
STRING 'null'. That selected `a.rid is not %s`; once the query was
parameterized the string was bound through the placeholder, so PostgreSQL
received ``a.rid IS NOT 'null'`` — a syntax error. The whole overlap query
failed, `exit_code` became 67, and the reference stage gathered nothing.

WHY THIS IS NOT A UNIT TEST. The defect is entirely in what the DATABASE
makes of the text, and every existing test of this path stubs the database
method — none parses or executes the SQL, which is exactly how a query
that cannot parse survived a green suite. A stub cannot fail this way and
so cannot prove the fix; only a server can. This script therefore EXECUTES
both branches against a real PostgreSQL and reports what the server said.

READ-ONLY, and against the REAL ``l2files``. An earlier shape of this
probe built its own fixture schema; ``rapid_pipeline`` has no CREATE
privilege on the database (correctly — it is a least-privilege service
role), and the fixture was never the point. What is under test is whether
PostgreSQL ACCEPTS each branch, and the deployed table with the deployed
q3c extension answers that better than a fixture would: it is the actual
schema the query names, so a column or an operator that has drifted shows
up here rather than being reproduced faithfully in a stand-in.

It writes NOTHING. Both calls are SELECTs through the real method, and the
transaction is rolled back before closing. The row COUNTS are whatever the
table happens to hold — the assertions are about the server accepting the
SQL and about the two branches differing in the documented direction, not
about how much data exists.

Run it on rapid-admin (team policy: never the laptop), inside the pipeline
image, with DBSERVER/DBPORT/DBNAME and RAPID_DB_SECRET_ID set:

    python3 -m submission.test.live_fixe_overlap_sql

Exit code is the result: 0 both branches proven, 1 something failed.
"""

import sys

from database.modules.utils import rapid_db_connect as dbc
from submission.gathering import (
    REFERENCE_OVERLAP_NO_EXCLUSION,
    REFERENCE_OVERLAP_OPEN_MJDOBS,
)

failures = []


def check(name, expected, got):
    if expected == got:
        print(f"PASS  {name}  (expected={expected!r} got={got!r})")
    else:
        print(f"FAIL  {name}  (expected={expected!r} got={got!r})")
        failures.append(name)


def fail(name, detail):
    print(f"FAIL  {name}  ({detail})")
    failures.append(name)




#: Half-width of the box put around a chosen row, in degrees. Small enough
#: to be a plausible tile and large enough that a frame's own corners fall
#: inside it, which is what makes the polygon predicates select the row.
_BOX = 0.01

#: Where to look when the table has no rows to point at. The assertions
#: about PARSEABILITY hold at any position; only the exclusion semantics
#: need real data.
_FALLBACK = (10.0, 20.0)


def _populated_position(conn):
    """A sky position the deployed table actually has rows near, and its fid.

    Chosen from the data rather than hard-coded, so this probe exercises
    the EXCLUSION as well as the parse wherever it is run and whatever the
    survey has produced by then. Returns (ra, dec, fid) or None.

    The same predicates the query uses decide what counts as usable —
    ``status > 0 and vbest > 0`` — so a position picked here is one the
    method can genuinely return rows for.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select ra0, dec0, a.fid from L2FileMeta a, L2Files b "
            "where a.rid = b.rid and status > 0 and vbest > 0 "
            "order by a.rid limit 1")
        row = cur.fetchone()
    if row is None:
        return None
    return float(row[0]), float(row[1]), int(row[2])


def main():
    print("=== live: both branches of the overlap exclusion clause ===")
    print(f">> sentinel REFERENCE_OVERLAP_NO_EXCLUSION = "
          f"{REFERENCE_OVERLAP_NO_EXCLUSION!r}")

    # The sentinel itself is half the finding: a string here is what made
    # the SQL invalid, and None is what makes the clause disappear.
    check("the no-exclusion sentinel is None, not a string",
          None, REFERENCE_OVERLAP_NO_EXCLUSION)

    with dbc.connection("fixe-overlap-sql", lane="transaction") as conn:
        try:
            _run(conn)
        finally:
            # Nothing was written; the rollback is belt-and-braces so this
            # can never be the probe that left a transaction open.
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    print()
    if failures:
        print(f"!! {len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("FIXE-OVERLAP-SQL-OK")
    return 0


def _run(conn):
    from database.modules.utils import rapid_db

    handle = rapid_db.RAPIDDB.borrowing(conn)

    found = _populated_position(conn)
    if found is None:
        ra, dec = _FALLBACK
        fid = 1
        print(f">> no usable L2 rows in the table; asking at "
              f"({ra}, {dec}) fid {fid}. Both branches are still proven to "
              f"PARSE, which is the whole of finding #3.")
    else:
        ra, dec, fid = found
        print(f">> asking at ({ra}, {dec}) fid {fid}, taken from a real row")

    # The centre, then a box of four corners around it — ten values, which
    # is how the method spells its signature.
    position = (ra, dec,
                ra + _BOX, dec + _BOX, ra + _BOX, dec - _BOX,
                ra - _BOX, dec - _BOX, ra - _BOX, dec + _BOX)

    # -- BRANCH 1: no exclusion — the branch that could not parse --------

    open_rows = handle.get_overlapping_l2files(
        REFERENCE_OVERLAP_NO_EXCLUSION, fid,
        REFERENCE_OVERLAP_OPEN_MJDOBS, *position,
        radius_of_initial_cone_search=0.18)
    open_code = getattr(handle, "exit_code", None)

    # THE ASSERTION THE DEFECT FAILS. Before the fix the server refused to
    # parse `a.rid IS NOT 'null'`, `get_overlapping_l2files` caught the
    # DatabaseError, set exit_code 67 and returned None — so gathering saw
    # "no overlapping files" for every field, forever.
    check("open branch: the server accepted the SQL (exit_code 0)",
          0, open_code)
    if open_rows is None:
        fail("open branch: a result set was returned",
             "None — the query failed, which is the finding itself")
        return
    print(f">> open branch returned {len(open_rows)} row(s)")

    # -- BRANCH 2: a real rid is excluded --------------------------------

    # Take the exclusion target from what the open branch actually
    # returned, so this works against whatever the deployed table holds.
    # With no rows there is nothing to exclude and the branch is exercised
    # for PARSEABILITY alone, which is still the property under test.
    excluded = int(open_rows[0][0]) if open_rows else 0

    excl_rows = handle.get_overlapping_l2files(
        excluded, fid, REFERENCE_OVERLAP_OPEN_MJDOBS, *position,
        radius_of_initial_cone_search=0.18)
    excl_code = getattr(handle, "exit_code", None)

    check("exclusion branch: the server accepted the SQL (exit_code 0)",
          0, excl_code)
    if excl_rows is None:
        fail("exclusion branch: a result set was returned",
             "None — the query failed")
        return
    print(f">> exclusion branch (rid {excluded}) returned "
          f"{len(excl_rows)} row(s)")

    # -- and that the two branches genuinely differ ----------------------

    if not open_rows:
        print(">> no rows near this position: both branches proven to "
              "PARSE and execute, but the exclusion itself is untested "
              "here. The unit suite covers the emitted shape.")
        return

    check("the exclusion branch drops exactly the rid it was given",
          len(open_rows) - 1, len(excl_rows))
    check("the excluded rid is absent from the exclusion branch",
          False, excluded in {int(row[0]) for row in excl_rows})
    check("the open branch excluded nothing",
          True, excluded in {int(row[0]) for row in open_rows})


if __name__ == "__main__":
    sys.exit(main())
