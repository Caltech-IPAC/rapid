#!/usr/bin/env python3

"""
db_backfill_l2files_limmag.py — populate `l2files.limmag`, the 5-sigma
point-source limiting magnitude, for rows registered before the column
existed or before the registration path computed it.

Unlike `db_backfill_l2files_overlapfields.py`, which derives its value
from columns already on the row, this backfill has to measure the
background noise of the image itself, so every row in scope costs one L2
FITS download (~66 MB) from the S3 bucket named in `l2files.filename`,
plus one PSF download per distinct (fid, sca) pair, cached for the run.
Expect the wall clock to be dominated by the downloads.

The PSF comes from the PSFs database table via `get_best_psf`, the same
source the science pipeline uses, so a backfilled limiting magnitude is
computed with the very PSF the photometry will later use.  A row whose
(fid, sca) has no registered PSF, or whose image cannot be measured, is
left NULL and counted; the run does not stop for it.  Re-running picks
those rows up again once the missing PSFs are registered.

Data units: the L2 data are in DN while ZPTMAG is the zeropoint for DN/s.
`modules.utils.rapid_data_analysis` reads BUNIT and EXPTIME and does that
conversion itself, so nothing here scales the pixels.

Idempotent and resumable: the default scope is rows still NULL, batches
are keyset-paginated on `rid` and committed one at a time, and
`--recompute` re-measures every row in scope.  `--rid-min/--rid-max`
shard a large backfill across several concurrently running invocations,
which is how to parallelize it; a single run is single-threaded by
design, so one operator mistake cannot saturate the bucket.

Requires the migration that adds `l2files.limmag`:

    alter table l2files add column limmag real;

Exit codes follow the `RAPIDDB` family: 64 cannot connect / cannot
configure, 65 preflight refused, 67 a database operation failed, 0 clean.
"""

import argparse
import os
import sys
import time

import numpy as np
import psycopg2.extras

import modules.utils.rapid_pipeline_subs as util
import modules.utils.rapid_data_analysis as rda
import database.modules.utils.rapid_db as db

swname = "db_backfill_l2files_limmag.py"
swvers = "1.0"

#: Columns the computation needs.  `filename` is the S3 URL of the L2
#: file; `fid` and `sca` select the PSF.
SCAN_COLUMNS = ("rid", "filename", "fid", "sca")


# ---------------------------------------------------------------------------
# PSF and image acquisition.
# ---------------------------------------------------------------------------

def download_from_s3(s3_full_name, local_filename):

    """True if `s3_full_name` is now at `local_filename`."""

    download_cmd = ["aws", "s3", "cp", s3_full_name, local_filename]

    exitcode = util.execute_command(download_cmd)

    return exitcode == 0 and os.path.exists(local_filename)


def get_psf_file(dbh, fid, sca, work_dir, psf_cache):

    """Local path of the science-image PSF for a filter and SCA, or None.

    Cached for the run, since one PSF serves every image from the same
    filter and SCA, and there are far fewer (fid, sca) pairs than rows.
    """

    key = (fid, sca)

    if key in psf_cache:
        return psf_cache[key]

    exit_code_before = dbh.exit_code

    psfid, s3_full_name_psf = dbh.get_best_psf(sca, fid)

    # A filter and SCA with no registered PSF is a normal condition here,
    # not a database failure, so do not let it leak into the run's fate.

    dbh.exit_code = exit_code_before

    if psfid is None or s3_full_name_psf is None:
        print(f"*** Warning: no PSF registered for fid,sca = {fid},{sca}; "
              f"those rows will be left NULL")
        psf_cache[key] = None
        return None

    local_psf_filename = os.path.join(work_dir, f"psf_fid{fid}_sca{sca}.fits")

    if not download_from_s3(s3_full_name_psf, local_psf_filename):
        print(f"*** Warning: could not download PSF {s3_full_name_psf}; "
              f"rows for fid,sca = {fid},{sca} will be left NULL")
        psf_cache[key] = None
        return None

    psf_cache[key] = local_psf_filename

    return local_psf_filename


def limmag_for_row(dbh, row, args, psf_cache):

    """The limiting magnitude for one `l2files` row, or None.

    Returns None rather than raising, so that one unmeasurable image
    cannot end a backfill of thousands.
    """

    rid, filename, fid, sca = row

    psf_filename = get_psf_file(dbh, int(fid), int(sca), args.work_dir,
                                psf_cache)

    if psf_filename is None:
        return None

    local_img_filename = os.path.join(args.work_dir, f"l2file_rid{rid}.fits")

    try:

        if not download_from_s3(filename, local_img_filename):
            print(f"*** Warning: could not download {filename} for rid={rid}; "
                  f"leaving it NULL")
            return None

        try:
            limmag_dict = rda.compute_limiting_magnitude_for_l2_image(
                local_img_filename,
                psf_filename,
                n_sigma_limit=args.n_sigma_limit,
                n_clip_sigma=args.n_clip_sigma)
        except Exception as error:
            print(f"*** Warning: could not compute the limiting magnitude for "
                  f"rid={rid} ({error}); leaving it NULL")
            return None

        if args.verbose:
            print(f"rid={rid} fid={fid} sca={sca} "
                  f"limmag={limmag_dict['maglimit']} "
                  f"bkgsig={limmag_dict['bkgsig']} "
                  f"poissonratio={limmag_dict['poissonratio']}")

        return limmag_dict["maglimit"]

    finally:

        if not args.keep_downloads and os.path.exists(local_img_filename):
            os.remove(local_img_filename)


# ---------------------------------------------------------------------------
# Preflight.
# ---------------------------------------------------------------------------

def preflight_schema(cur):

    """Refuse unless `l2files.limmag` exists."""

    cur.execute("""
        select 1
        from information_schema.columns
        where table_schema = 'public'
        and table_name = 'l2files'
        and column_name = 'limmag';
    """)

    if cur.fetchone() is None:
        print("*** Error: l2files.limmag does not exist; apply the "
              "column-adding migration first; quitting...")
        return 65

    return 0


def preflight_work_dir(args):

    """Refuse unless the work directory is usable, since every row needs it."""

    if not os.path.isdir(args.work_dir):
        print(f"*** Error: work directory {args.work_dir} does not exist; "
              f"pass --work-dir; quitting...")
        return 65

    if not os.access(args.work_dir, os.W_OK):
        print(f"*** Error: work directory {args.work_dir} is not writable; "
              f"quitting...")
        return 65

    print(f">> work directory: {args.work_dir}")

    return 0


def preflight_psf_coverage(dbh, cur, args):

    """Report which (fid, sca) pairs in scope have no PSF, before downloading.

    Every row of an uncovered pair is destined to stay NULL, and a
    download per row would be spent finding that out one row at a time.
    Reporting it up front lets the operator register the missing PSFs
    first instead.
    """

    cur.execute(f"""
        select fid, sca, count(*)
        from l2files
        where {scope_clause(args)}
        group by fid, sca
        order by fid, sca;
    """, tuple([0] + bound_params(args)))

    pairs = cur.fetchall()

    if not pairs:
        return 0, {}

    psf_cache = {}
    n_covered = 0
    n_uncovered = 0

    for fid, sca, nrows in pairs:

        if get_psf_file(dbh, int(fid), int(sca), args.work_dir, psf_cache) \
                is None:
            n_uncovered += nrows
        else:
            n_covered += nrows

    print(f">> PSF coverage over {len(pairs)} (fid,sca) pair(s) in scope: "
          f"{n_covered} row(s) have a PSF, {n_uncovered} row(s) do not and "
          f"will stay NULL")

    if n_covered == 0:
        print("*** Error: no row in scope has a registered PSF, so the run "
              "would download images only to discard them; register the "
              "PSFs first; quitting...")
        return 65, psf_cache

    return 0, psf_cache


# ---------------------------------------------------------------------------
# Backfill.
# ---------------------------------------------------------------------------

def scope_clause(args):

    """The WHERE fragment for the rows in scope.

    Clause order fixes parameter order for every caller: `rid > %s` first
    (the keyset cursor), then the parameterless NULL filter, then the
    optional bounds.
    """

    clauses = ["rid > %s"]

    if not args.recompute:
        clauses.append("limmag is null")

    if args.rid_min is not None:
        clauses.append("rid >= %s")

    if args.rid_max is not None:
        clauses.append("rid <= %s")

    return " and ".join(clauses)


def bound_params(args):

    params = []
    if args.rid_min is not None:
        params.append(args.rid_min)
    if args.rid_max is not None:
        params.append(args.rid_max)
    return params


def count_in_scope(cur, args):

    cur.execute(f"select count(*) from l2files where {scope_clause(args)};",
                tuple([0] + bound_params(args)))

    return cur.fetchone()[0]


def backfill(dbh, conn, cur, args, psf_cache):

    """Keyset-paginated, one committed transaction per batch."""

    select_sql = (f"select {', '.join(SCAN_COLUMNS)} "
                  f"from l2files where {scope_clause(args)} "
                  f"order by rid limit %s;")

    update_sql = "update l2files set limmag = %s where rid = %s;"

    last_rid = 0
    n_seen = 0
    n_written = 0
    n_null = 0
    limmags = []

    start = time.time()

    while True:

        params = [last_rid] + bound_params(args) + [args.batch_size]

        try:
            cur.execute(select_sql, tuple(params))
            rows = cur.fetchall()
        except Exception as error:
            print(f"*** Error scanning l2files ({error}); quitting...")
            return 67, n_written, n_null

        if not rows:
            break

        updates = []

        for row in rows:

            rid = row[0]
            last_rid = rid
            n_seen += 1

            limmag = limmag_for_row(dbh, row, args, psf_cache)

            if limmag is None:
                n_null += 1
            else:
                limmags.append(limmag)

            # A row that could not be measured is skipped rather than
            # written as NULL: it is already NULL, and skipping keeps it
            # in scope for a later run once its PSF is registered.

            if limmag is not None:
                updates.append((float(limmag), rid))

        if updates and not args.dry_run:
            try:
                psycopg2.extras.execute_batch(cur, update_sql, updates,
                                              page_size=len(updates))
                conn.commit()
                n_written += len(updates)
            except Exception as error:
                conn.rollback()
                print(f"*** Error updating l2files ({error}); rolled back "
                      f"this batch at rid<={last_rid}; quitting...")
                return 67, n_written, n_null

        elapsed = time.time() - start
        rate = n_seen / elapsed if elapsed > 0 else 0.0
        print(f">> {n_seen} row(s) measured, {n_written} written, "
              f"{n_null} left NULL, through rid={last_rid} "
              f"({rate:.2f} rows/s)")

        if args.limit is not None and n_seen >= args.limit:
            print(f">> stopping at --limit {args.limit}")
            break

    if limmags:
        arr = np.asarray(limmags)
        print(f">> limiting magnitude [AB mag]: min={arr.min():.3f} "
              f"median={np.median(arr):.3f} mean={arr.mean():.3f} "
              f"max={arr.max():.3f}")

    if args.dry_run:
        print(f">> --dry-run: {len(limmags)} row(s) would have been written, "
              f"nothing was")

    return 0, n_written, n_null


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def parse_args(argv=None):

    p = argparse.ArgumentParser(
        description="Backfill l2files.limmag (5-sigma point-source limiting "
                    "magnitude).")

    p.add_argument("--batch-size", type=int, default=50,
                   help="rows per read/update/commit batch (default 50, far "
                        "smaller than a pure-SQL backfill because each row "
                        "costs an image download)")
    p.add_argument("--work-dir", default=os.getenv("RAPID_WORK", "."),
                   help="directory for downloaded images and PSFs (default "
                        "$RAPID_WORK, else the current directory)")
    p.add_argument("--keep-downloads", action="store_true",
                   help="do not delete each L2 image after measuring it")
    p.add_argument("--n-sigma-limit", type=float, default=5.0,
                   help="signal-to-noise ratio defining the limit "
                        "(default %(default)s)")
    p.add_argument("--n-clip-sigma", type=float, default=3.0,
                   help="sigmas for the clipping of the background estimate "
                        "(default %(default)s)")
    p.add_argument("--recompute", action="store_true",
                   help="re-measure every row in scope, not only rows still "
                        "NULL")
    p.add_argument("--dry-run", action="store_true",
                   help="measure and report, write nothing")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after roughly this many rows (whole batches)")
    p.add_argument("--rid-min", type=int, default=None,
                   help="lowest rid in scope; with --rid-max, shards a large "
                        "backfill across concurrent invocations")
    p.add_argument("--rid-max", type=int, default=None)
    p.add_argument("--skip-psf-preflight", action="store_true",
                   help="do not survey PSF coverage before starting")
    p.add_argument("--vacuum", action="store_true",
                   help="VACUUM ANALYZE l2files after a successful run")
    p.add_argument("--verbose", action="store_true")

    return p.parse_args(argv)


def main(argv=None):

    args = parse_args(argv)

    print("swname =", swname)
    print("swvers =", swvers)
    print("n_sigma_limit =", args.n_sigma_limit)
    print("n_clip_sigma =", args.n_clip_sigma)

    if args.n_sigma_limit <= 0.0:
        print("*** Error: --n-sigma-limit must be > 0; quitting...")
        return 65

    if args.n_clip_sigma <= 0.0:
        print("*** Error: --n-clip-sigma must be > 0; quitting...")
        return 65

    rc = preflight_work_dir(args)
    if rc:
        return rc

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        print(f"*** Error: could not open the database connection "
              f"(exit_code={dbh.exit_code}); quitting...")
        return dbh.exit_code

    rc = 0

    try:
        conn = dbh.conn
        cur = dbh.cur

        rc = preflight_schema(cur)
        if rc:
            return rc

        total = count_in_scope(cur, args)
        print(f">> {total} row(s) in scope "
              f"({'all rows' if args.recompute else 'rows still NULL'})")

        if total == 0:
            print(">> nothing to do")
            return 0

        psf_cache = {}

        if not args.skip_psf_preflight:
            rc, psf_cache = preflight_psf_coverage(dbh, cur, args)
            if rc:
                return rc

        rc, n_written, n_null = backfill(dbh, conn, cur, args, psf_cache)

        if rc == 0 and args.vacuum and not args.dry_run:
            print(">> VACUUM ANALYZE l2files")
            dbh.vacuum_analyze_table("l2files")

        if rc == 0:
            cur.execute("select count(*) from l2files where limmag is null;")
            remaining = cur.fetchone()[0]
            print(f">> {n_written} row(s) written, {n_null} row(s) left NULL "
                  f"this run; {remaining} row(s) in the table are still NULL")
            if n_null:
                print(">> rows left NULL stay in scope; re-run once their "
                      "PSFs are registered")

    finally:
        dbh.close()

    return rc


if __name__ == "__main__":
    sys.exit(main())
