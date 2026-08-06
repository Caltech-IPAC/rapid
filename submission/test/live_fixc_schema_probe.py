"""Query-only probe: what the real rapid-db holds for post-process gathering.

Answers three questions the unit suite cannot, because they are facts about
the deployed database rather than about this code:

  1. What columns does Jobs actually have? `get_job_record` has to select real
     ones, and the review found it does not exist on RAPIDDB at all.
  2. Are there real g0001 rows to gather against, and how many?
  3. Is the PSFs table populated? The reference-image live probe is gated on
     that: no PSF data means unit-test only.

Writes nothing.
"""

import json

from database.modules.utils.rapid_db_connect import connection


def rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def main():
    out = {}
    with connection("rapid-fixc-schema-probe", lane="transaction") as conn:
        with conn.cursor() as cur:
            out["jobs_columns"] = [
                list(r) for r in rows(cur,
                    "SELECT column_name, data_type FROM information_schema.columns"
                    " WHERE table_name = 'jobs' ORDER BY ordinal_position")]

            out["diffimages_columns"] = [
                list(r) for r in rows(cur,
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'diffimages' ORDER BY ordinal_position")]

            out["refimages_columns"] = [
                list(r) for r in rows(cur,
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'refimages' ORDER BY ordinal_position")]

            out["refimcatalogs_columns"] = [
                list(r) for r in rows(cur,
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'refimcatalogs' ORDER BY ordinal_position")]

            out["diffimmeta_columns"] = [
                list(r) for r in rows(cur,
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'diffimmeta' ORDER BY ordinal_position")]

            for table in ("l2files", "l2filemeta", "psfs", "refimages",
                          "diffimages", "jobs", "fields", "filters"):
                try:
                    count = rows(cur, f"SELECT count(*) FROM {table}")[0][0]
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    count = f"ERROR: {exc}"
                out[f"count_{table}"] = count

            # The g0001 simulation rows the prompt names.
            try:
                out["l2files_sample"] = [
                    list(r) for r in rows(cur,
                        "SELECT rid, filename, expid, sca, field, mjdobs,"
                        " exptime, infobits, status, vbest, version"
                        " FROM l2files ORDER BY rid LIMIT 3")]
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                out["l2files_sample"] = f"ERROR: {exc}"

            try:
                out["psfs_sample"] = [
                    list(r) for r in rows(cur,
                        "SELECT * FROM psfs LIMIT 2")]
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                out["psfs_sample"] = f"ERROR: {exc}"

            try:
                out["jobs_sample"] = [
                    list(r) for r in rows(cur, "SELECT * FROM jobs LIMIT 2")]
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                out["jobs_sample"] = f"ERROR: {exc}"

        conn.rollback()

    # Counts and the Jobs shape FIRST: they are the answers this probe exists
    # for, and remote output is truncated.
    print("PROBE-SUMMARY-START")
    for key in sorted(k for k in out if k.startswith("count_")):
        print(f"{key} = {out[key]}")
    print("jobs_columns =",
          [c[0] for c in out["jobs_columns"]]
          if isinstance(out["jobs_columns"], list) else out["jobs_columns"])
    print("diffimages_columns =", out["diffimages_columns"])
    print("refimages_columns =", out["refimages_columns"])
    print("refimcatalogs_columns =", out["refimcatalogs_columns"])
    print("diffimmeta_columns =", out["diffimmeta_columns"])
    print("PROBE-SUMMARY-END")

    print("PROBE-JSON-START")
    print(json.dumps(out, indent=1, default=str))
    print("PROBE-JSON-END")


if __name__ == "__main__":
    main()
