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

It is deliberately additive and self-contained: it creates its own
temporary table in a schema of its own, runs the two queries against it,
and drops the schema. It never reads or writes L2Files or any other
operational table — the fixture below carries only the columns this one
query names, which is all that is needed to prove the SQL parses, binds
and executes.

Run it on rapid-admin (team policy: never the laptop), inside the pipeline
image, with DBSERVER/DBPORT/DBNAME and RAPID_DB_SECRET_ID set:

    python3 -m submission.test.live_fixe_overlap_sql

Exit code is the result: 0 both branches proven, 1 something failed.
"""

import datetime
import os
import sys
import uuid

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


#: The columns the overlap query names, and nothing else. q3c is an
#: extension the operational database has; this fixture is queried through
#: the SAME method, so whether q3c is present is answered by the run
#: rather than assumed here.
FIXTURE_DDL = """
create table {schema}.l2files (
    rid        bigint primary key,
    fid        integer,
    ra0        double precision, dec0 double precision,
    ra1        double precision, dec1 double precision,
    ra2        double precision, dec2 double precision,
    ra3        double precision, dec3 double precision,
    ra4        double precision, dec4 double precision,
    filename   text,
    expid      bigint,
    sca        integer,
    field      integer,
    mjdobs     double precision,
    exptime    double precision,
    infobits   integer,
    status     integer,
    vbest      integer,
    version    integer
)
"""


def main():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    schema = f"fixe_overlap_{stamp}_{uuid.uuid4().hex[:6]}".lower()

    print("=== live: both branches of the overlap exclusion clause ===")
    print(f">> schema  {schema}")
    print(f">> sentinel REFERENCE_OVERLAP_NO_EXCLUSION = "
          f"{REFERENCE_OVERLAP_NO_EXCLUSION!r}")

    # The sentinel itself is half the finding: a string here is what made
    # the SQL invalid, and None is what makes the clause disappear.
    check("the no-exclusion sentinel is None, not a string",
          None, REFERENCE_OVERLAP_NO_EXCLUSION)

    with dbc.connection("fixe-overlap-sql", lane="transaction") as conn:
        _run(conn, schema)

    print()
    if failures:
        print(f"!! {len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("FIXE-OVERLAP-SQL-OK")
    return 0


def _run(conn, schema):
    from database.modules.utils import rapid_db
    from database.modules.utils.rapid_db_connect import qualified_identifier

    created = False
    try:
        with conn.cursor() as cur:
            # `qualified_identifier` is the only sanctioned way to put a
            # name into SQL text in this repo (rapid_db_connect), so the
            # generated schema name goes through it rather than a format.
            cur.execute("create schema " + qualified_identifier(schema).as_string(conn))
            created = True
            cur.execute(FIXTURE_DDL.format(
                schema=qualified_identifier(schema).as_string(conn)))

            # Three frames on one tile in one filter. Their absolute sky
            # position does not matter — the cone/polygon predicates are
            # given the same centre the rows carry, so all three are inside
            # it and the EXCLUSION is the only thing that varies between
            # the two branches.
            for rid, mjd in ((9001, 60000.0), (9002, 60001.0),
                             (9003, 60002.0)):
                cur.execute(
                    "insert into " + qualified_identifier(schema).as_string(conn)
                    + ".l2files (rid, fid, ra0, dec0, ra1, dec1, ra2, dec2, "
                      "ra3, dec3, ra4, dec4, filename, expid, sca, field, "
                      "mjdobs, exptime, infobits, status, vbest, version) "
                      "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                      "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (rid, 1,
                     10.0, 20.0, 10.01, 20.01, 10.01, 19.99,
                     9.99, 19.99, 9.99, 20.01,
                     f"f{rid}.fits", rid, 1, 1, mjd, 100.0, 0, 1, 1, 1))
        conn.commit()
        print(">> fixture: 3 rows on one tile, filter 1")

        handle = rapid_db.RAPIDDB.borrowing(conn)

        # The method reads the table by an unqualified name, so the
        # fixture schema is put in front of the search path for this
        # connection. Nothing operational is reachable ahead of it.
        with conn.cursor() as cur:
            cur.execute("set search_path to "
                        + qualified_identifier(schema).as_string(conn)
                        + ", public")
        conn.commit()

        # TEN values: the tile CENTRE (ra0/dec0) then the four corners, which
        # is how the method spells its signature. The centre anchors the cone
        # search; the corners are what the polygon predicates test.
        corners = (10.0, 20.0,
                   10.01, 20.01, 10.01, 19.99, 9.99, 19.99, 9.99, 20.01)

        # -- BRANCH 1: no exclusion (the branch that could not parse) ----

        rows = handle.get_overlapping_l2files(
            REFERENCE_OVERLAP_NO_EXCLUSION, 1,
            REFERENCE_OVERLAP_OPEN_MJDOBS, *corners,
            radius_of_initial_cone_search=0.18)
        code = getattr(handle, "exit_code", None)

        check("open branch: the server accepted the SQL (exit_code 0)",
              0, code)
        if rows is None:
            fail("open branch: rows returned",
                 "None — the query failed, which is the finding itself")
        else:
            check("open branch: excludes nothing (all 3 rows)",
                  3, len(rows))

        # -- BRANCH 2: a real rid is excluded ---------------------------

        rows = handle.get_overlapping_l2files(
            9002, 1, REFERENCE_OVERLAP_OPEN_MJDOBS, *corners,
            radius_of_initial_cone_search=0.18)
        code = getattr(handle, "exit_code", None)

        check("exclusion branch: the server accepted the SQL (exit_code 0)",
              0, code)
        if rows is None:
            fail("exclusion branch: rows returned", "None — the query failed")
        else:
            check("exclusion branch: drops exactly the named rid",
                  2, len(rows))
            returned = sorted(int(row[0]) for row in rows)
            check("exclusion branch: the excluded rid is the one asked for",
                  [9001, 9003], returned)

    except Exception as exc:  # noqa: BLE001 - reported, then cleaned up
        fail("the probe itself", f"{type(exc).__name__}: {exc}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if created:
            try:
                with conn.cursor() as cur:
                    cur.execute("set search_path to public")
                    cur.execute(
                        "drop schema "
                        + qualified_identifier(schema).as_string(conn)
                        + " cascade")
                conn.commit()
                print(f">> dropped schema {schema}")
            except Exception as exc:  # noqa: BLE001
                print(f"!! could not drop schema {schema}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
