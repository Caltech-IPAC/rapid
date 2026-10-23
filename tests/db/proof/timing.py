"""The wall time of one change group over N keys (foundation plan v5, Part 4, milestone 0).

A script, not a test; CI does not run it. Against a database that
``database/schema.sql`` was applied to (it adds rows and deletes none, so
point it at a throwaway database), using the ``PG*`` environment:

    python tests/db/proof/timing.py 10000

builds, by SQL, two jobs, N ``catalogs`` rows for each and N open bindings
to the first job's rows, analyses them, then applies one change group that closes every
binding and opens the second job's row on the same key, through
:func:`rapidpipe.foundation.change`, and prints N and the group's wall time.
"""

from __future__ import annotations

import os
import sys
import time

import psycopg2

from rapidpipe.foundation import change, create_job, create_run


def main(n: int) -> None:
    conn = psycopg2.connect(host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
                            dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
                            password=os.environ.get("PGPASSWORD", ""))
    run = create_run(conn, "timing", f"change group over {n} keys", "synthetic", {}, "T", "T")
    old, new = (create_job(conn, run, "timing", {"n": n}) for _ in range(2))
    tag = f"timing-{old}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO catalogs (job, dataset, name, version, location)"
                    " SELECT j, 'T', %s || g, 'v1', 'file:///dev/null'"
                    " FROM generate_series(1, %s) g, unnest(ARRAY[%s, %s]::bigint[]) j",
                    (tag, n, old, new))
        cur.execute("INSERT INTO official (product, key, job, opened_group, opened_by, opened_why)"
                    " SELECT 'catalogs', jsonb_build_object('dataset', 'T', 'name', %s || g, 'version', 'v1'),"
                    " %s, nextval('change_groups'), 'timing', 'setup'"
                    " FROM generate_series(1, %s) g RETURNING id, key", (tag, old, n))
        bindings = cur.fetchall()
        # Bulk-loaded tables have no statistics until autovacuum reaches them; without
        # them the planner answers the open-binding check by (product, job) and the
        # group is quadratic in N. Measure the analysed state a live table is in.
        cur.execute("ANALYZE official")
        cur.execute("ANALYZE catalogs")
    conn.commit()
    group = [{"close": binding, "open": {"product": "catalogs", "key": key, "job": new}}
             for binding, key in bindings]

    start = time.monotonic()
    change(conn, group, "timing", f"one group over {n} keys")
    elapsed = time.monotonic() - start
    print(f"N={n} change_group_seconds={elapsed:.2f}")
    conn.close()


if __name__ == "__main__":
    main(int(sys.argv[1]))
