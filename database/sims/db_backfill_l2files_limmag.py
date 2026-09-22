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

Only current rows are measured: `vbest > 0 and status > 0`, the same
conditions `get_best_psf` applies to the PSFs table.  A superseded
version or a row flagged bad is never read by the pipeline, so measuring
it would spend an image download on a value nothing consults.

Idempotent and resumable: the default scope is rows still NULL, batches
are keyset-paginated on `rid` and committed one at a time, and
`--recompute` re-measures every row in scope.

`--num-cores N` measures a batch across N worker processes, which is
worth doing because a row costs roughly 3 s of download and 3 s of
single-threaded numpy.  The parent keeps the database to itself: it
resolves the PSFs and performs every write, so workers need no connection
and the run keeps one transaction stream and one commit per batch.  Each
worker holds a whole image, about 1.3 GB, so memory is the ceiling and
the preflight refuses a setting past 80% of the machine.  The default
stays 1, so nothing saturates the bucket by accident.  `--rid-min` and
`--rid-max` still shard a backfill across separate invocations, which is
how to spread it over several machines.

Requires the migration that adds `l2files.limmag`:

    alter table l2files add column limmag real;

Exit codes follow the `RAPIDDB` family: 64 cannot connect / cannot
configure, 65 preflight refused, 67 a database operation failed, 0 clean.
"""

import argparse
import contextlib
import io
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

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


def make_job(row, psf_filename, args):

    """The picklable unit of work for one row.

    A plain tuple rather than the row and the argparse namespace, so that
    what crosses to a worker process is explicit and small.
    """

    rid, filename, fid, sca = row

    return (int(rid), filename, int(fid), int(sca), psf_filename,
            args.work_dir, args.n_sigma_limit, args.n_clip_sigma,
            args.keep_downloads, args.verbose)


def measure_one(job):

    """Download and measure one row.  Returns (rid, limmag or None, messages).

    Runs in a worker process when --num-cores > 1, so it touches no
    database handle and prints nothing: output from several processes at
    once interleaves into an unreadable log, so everything written here --
    including what the download command and rapid_data_analysis print --
    is captured and returned for the parent to print in row order.
    Returns None rather than raising, so that one unmeasurable image
    cannot end a backfill of thousands.
    """

    captured = io.StringIO()

    with contextlib.redirect_stdout(captured):
        rid, limmag, messages = _measure_one(job)

    return rid, limmag, captured.getvalue().splitlines() + messages


def _measure_one(job):

    """The body of measure_one, with its output still going to stdout."""

    (rid, filename, fid, sca, psf_filename, work_dir, n_sigma_limit,
     n_clip_sigma, keep_downloads, verbose) = job

    messages = []

    local_img_filename = os.path.join(work_dir, f"l2file_rid{rid}.fits")

    try:

        if not download_from_s3(filename, local_img_filename):
            messages.append(f"*** Warning: could not download {filename} for "
                            f"rid={rid}; leaving it NULL")
            return rid, None, messages

        try:
            limmag_dict = rda.compute_limiting_magnitude_for_l2_image(
                local_img_filename,
                psf_filename,
                n_sigma_limit=n_sigma_limit,
                n_clip_sigma=n_clip_sigma)
        except Exception as error:
            messages.append(f"*** Warning: could not compute the limiting "
                            f"magnitude for rid={rid} ({error}); leaving it "
                            f"NULL")
            return rid, None, messages

        if verbose:
            messages.append(f"rid={rid} fid={fid} sca={sca} "
                            f"limmag={limmag_dict['maglimit']} "
                            f"bkgsig={limmag_dict['bkgsig']} "
                            f"poissonratio={limmag_dict['poissonratio']}")

        return rid, limmag_dict["maglimit"], messages

    finally:

        if not keep_downloads and os.path.exists(local_img_filename):
            os.remove(local_img_filename)


def limmag_for_row(dbh, row, args, psf_cache):

    """The limiting magnitude for one `l2files` row, or None.

    The single-process path: resolve the PSF, then do the same work a
    worker would.
    """

    rid, filename, fid, sca = row

    psf_filename = get_psf_file(dbh, int(fid), int(sca), args.work_dir,
                                psf_cache)

    if psf_filename is None:
        return None

    rid, limmag, messages = measure_one(make_job(row, psf_filename, args))

    for message in messages:
        print(message)

    return limmag


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


#: Peak resident memory of one measurement, measured on a 4088x4088 L2
#: image: the pixels as float64 plus the copies sigma clipping makes.

GIGABYTES_PER_WORKER = 1.3


def preflight_num_cores(args):

    """Resolve --num-cores and refuse a setting the machine cannot feed.

    Each worker holds a whole image, so the ceiling here is memory rather
    than CPU: oversubscribing does not merely slow the run down, it
    invites the OOM killer partway through a batch.
    """

    if args.num_cores == 0:
        args.num_cores = os.cpu_count() or 1
        print(f">> --num-cores 0: using every core ({args.num_cores})")

    if args.num_cores < 1:
        print(f"*** Error: --num-cores {args.num_cores} must be >= 0; "
              f"quitting...")
        return 65

    print(f">> num_cores = {args.num_cores}")

    if args.num_cores == 1:
        return 0

    try:
        total_gb = (os.sysconf("SC_PHYS_PAGES") *
                    os.sysconf("SC_PAGE_SIZE")) / 1024 ** 3
    except (AttributeError, ValueError, OSError):
        return 0

    needed_gb = args.num_cores * GIGABYTES_PER_WORKER

    print(f">> about {needed_gb:.1f} GB of {total_gb:.1f} GB of memory will "
          f"be in use while a batch is measured")

    if needed_gb > 0.8 * total_gb:
        print(f"*** Error: {args.num_cores} workers need about "
              f"{needed_gb:.1f} GB, more than 80% of this machine's "
              f"{total_gb:.1f} GB; lower --num-cores; quitting...")
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

    Restricted to `vbest > 0 and status > 0`, the same pair of conditions
    `get_best_psf` applies to the PSFs table: a superseded or bad row is
    not one the pipeline will ever read, so measuring it would spend an
    image download on a value nothing consults.

    Clause order fixes parameter order for every caller: `rid > %s` first
    (the keyset cursor), then the parameterless filters, then the
    optional bounds.
    """

    clauses = ["rid > %s", "vbest > 0", "status > 0"]

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


def measure_batch_in_parallel(dbh, rows, args, psf_cache):

    """Measure one batch across worker processes; returns [(rid, limmag)].

    The parent keeps the database to itself: it resolves and downloads the
    PSFs here, before the fan-out, so a worker needs no connection of its
    own and the run keeps its single transaction stream.  Results come
    back in row order, so the log reads the same as a single-process run.
    """

    jobs = []
    results = []

    for row in rows:

        rid, filename, fid, sca = row

        psf_filename = get_psf_file(dbh, int(fid), int(sca), args.work_dir,
                                    psf_cache)

        if psf_filename is None:
            results.append((int(rid), None))
            continue

        jobs.append(make_job(row, psf_filename, args))

    if jobs:

        with ProcessPoolExecutor(max_workers=args.num_cores) as executor:

            for rid, limmag, messages in executor.map(measure_one, jobs):

                for message in messages:
                    print(message)

                results.append((rid, limmag))

    return sorted(results, key=lambda result: result[0])


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


        # Measured values for this batch, as (rid, limmag or None).  A row
        # that could not be measured is skipped rather than written as NULL:
        # it is already NULL, and skipping keeps it in scope for a later run
        # once its PSF is registered.

        if args.num_cores == 1:

            results = []

            for row in rows:
                results.append((row[0], limmag_for_row(dbh, row, args,
                                                       psf_cache)))

        else:

            results = measure_batch_in_parallel(dbh, rows, args, psf_cache)

        for rid, limmag in results:

            n_seen += 1

            if limmag is None:
                n_null += 1
            else:
                limmags.append(limmag)
                updates.append((float(limmag), rid))


        # The cursor advances past every row of the batch, measured or not,
        # so an unmeasurable row cannot stall the scan.

        last_rid = max(row[0] for row in rows)

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
    p.add_argument("--num-cores", type=int, default=1,
                   help="worker processes measuring rows concurrently "
                        "(default 1, single process; 0 means every core).  "
                        "Each worker downloads and holds one image, so it "
                        "costs about 1.3 GB of memory and one concurrent S3 "
                        "transfer")
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

    rc = preflight_num_cores(args)
    if rc:
        return rc

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

        if args.batch_size < args.num_cores:
            print(f"*** Warning: --batch-size {args.batch_size} is smaller "
                  f"than --num-cores {args.num_cores}, so workers will idle; "
                  f"raising the batch size to {args.num_cores}")
            args.batch_size = args.num_cores

        total = count_in_scope(cur, args)
        print(f">> {total} row(s) in scope (vbest > 0 and status > 0"
              f"{'' if args.recompute else ', limmag still NULL'})")

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
            cur.execute("select count(*) from l2files "
                        "where limmag is null and vbest > 0 and status > 0;")
            remaining = cur.fetchone()[0]
            print(f">> {n_written} row(s) written, {n_null} row(s) left NULL "
                  f"this run; {remaining} current row(s) (vbest > 0 and "
                  f"status > 0) are still NULL")
            if n_null:
                print(">> rows left NULL stay in scope; re-run once their "
                      "PSFs are registered")

    finally:
        dbh.close()

    return rc


if __name__ == "__main__":
    sys.exit(main())
