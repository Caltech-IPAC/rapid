#!/usr/bin/env python3

"""
db_backfill_l2files_overlapfields.py — populate `l2files.overlapfields`,
the per-image sky-tile footprint added by rapid_systems migration
`101-l2files-overlapfields.sql`.

WHAT IT COMPUTES.  The EXACT set of sky tiles the science image overlaps,
via `database.modules.utils.overlapping_fields.overlapping_fields` — see
that module for the geometry.  Every input is already a column on the row
(`crval1`, `crval2`, `crpix1`, `crpix2`, `cd11`, `cd12`, `cd21`, `cd22`),
so nothing is read from S3 and no FITS file is opened.

WHY NOT THE THREE METHODS THAT WERE ALREADY AVAILABLE.  Measured against
every-pixel ground truth over 1,200 random pointings spanning |dec| 0 to
89.5 and all rotations:

  * `get_overlapping_rtids` takes the RA/Dec BOUNDING BOX of the corners —
    its own docstring says "Assumes the image is axis-aligned" — so a
    rotated SCA over-reports by +21% of tiles on average, worst case
    +100%.
  * `get_all_neighboring_rtids` returns the centre tile plus its neighbour
    ring: rotation-blind and shape-blind by construction.
  * GRID SAMPLING (method 3 of `scripts/compare_methods_overlapping_fields.py`,
    which this script previously used) can only return tiles containing a
    sample point, so it UNDER-reports: it missed at least one genuinely
    overlapped tile in 221 of those 1,200 pointings — 18% — for 246 tiles
    missed in total.

Under-reporting is the dangerous direction: a missing field is an image
silently absent from that field's stack, with nothing to notice it.  The
exact method made zero errors on the same 1,200 pointings.

MINIMUM OVERLAP IS A STATED NUMBER, NOT AN ACCIDENT.  A tile overlapped by
a third of a pixel is real geometry and useless science — the narrowest
real overlap found in the sweep was 0.038 arcsec, 0.35 of a pixel.
`--min-overlap-pixels` insets the image rectangle before the test, so the
cutoff is chosen and recorded.  Sampling had such a cutoff too, but an
accidental one that varied with rotation and could not be stated.

The default is 25 px (laher, 2026-09-08) = 2.75 arcsec at the 0.11
arcsec/px plate scale, about 1% of a ~250 arcsec tile edge.  It is NOT
restated here: it is read from
`overlapping_fields.DEFAULT_MIN_OVERLAP_PIXELS`, so this backfill and
the registration path that will later write the same column cannot drift
into two definitions of one column.  Changing the threshold means
re-running with `--recompute`, since already-written rows keep the
threshold they were computed under.

IDEMPOTENT AND RESUMABLE.  Default scope is rows still carrying the
migration's `{}` default, so an interrupted run resumes by being re-run.
`--recompute` re-derives every row in scope instead — what to use after
changing `--min-overlap-pixels`.  Batches are keyset-paginated on `rid`
and committed one batch at a time: an interruption loses at most one
batch, never leaves a partial array, and holds no long transaction
against a table the pipeline is writing to.

ORDERING.  Run AFTER `101-l2files-overlapfields.sql` and BEFORE
`103-l2files-overlapfields-index.sql` — 103 creates the GIN index and
refuses to apply while any row is still empty, and an unindexed
`overlapfields` is what lets these UPDATEs take PostgreSQL's HOT path
(HOT requires that no INDEXED column change).

WHERE TO RUN IT.  See the runbook: the credential is the constraint, not
the host.  `rapid_read`/`rapid_operator` cannot UPDATE `l2files`.

Exit codes follow the `RAPIDDB` family: 64 cannot connect / cannot
configure, 65 preflight refused, 67 a database operation failed, 0 clean.
"""

import argparse
import os
import sys
import time

import numpy as np
import psycopg2.extras

import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as tessellation
from database.modules.utils.overlapping_fields import (
    DEFAULT_MIN_OVERLAP_PIXELS, overlapping_fields)

swname = "db_backfill_l2files_overlapfields.py"
swvers = "2.0"

#: The migration that must be recorded in `schema_migrations` before this
#: script can do anything.  Same floor-not-equality reading as
#: `pipeline/intent/schema_contract.py`: a database carrying migrations this
#: script has never heard of is fine, a database missing this one is not.
REQUIRED_MIGRATION = "101-l2files-overlapfields.sql"

#: Columns the computation needs.  `field` is read both to union into the
#: result and to cross-check the geometry — see `preflight_centre_tile`.
SCAN_COLUMNS = ("rid", "field",
                "crval1", "crval2", "crpix1", "crpix2",
                "cd11", "cd12", "cd21", "cd22")


def _footprint(row, naxis1, naxis2, min_overlap_pixels, union_field=True):

    """The overlapping-field set for one `l2files` row, as a sorted list.

    `field` is unioned in by default.  It was computed at registration
    from the image's CENTRE sky position through astropy's WCS (SIP
    included), while this computes from the CD matrix alone, so the two
    can in principle disagree about which tile the exact centre falls in
    when the centre sits on a tile boundary.  Unioning costs nothing and
    makes `field = ANY(overlapfields)` true by construction, which is what
    104's check asserts.  `union_field=False` is the preflight's handle on
    the un-unioned geometry.
    """

    (rid, field, crval1, crval2, crpix1, crpix2,
     cd11, cd12, cd21, cd22) = row

    return overlapping_fields(
        float(crval1), float(crval2), float(crpix1), float(crpix2),
        float(cd11), float(cd12), float(cd21), float(cd22),
        naxis1, naxis2,
        field=(int(field) if union_field else None),
        min_overlap_pixels=min_overlap_pixels)


# ---------------------------------------------------------------------------
# Preflight.
# ---------------------------------------------------------------------------

def preflight_schema(cur):

    """Refuse unless the column exists AND its migration is recorded.

    Both, not either: `schema_migrations` records what the APPLIER ran
    (`apply-db-migrations.sh`, never the migration files themselves), so a
    column present without a recorded migration means someone hand-applied
    DDL outside the applier — worth refusing loudly rather than
    backfilling into an unrecorded schema.
    """

    cur.execute("""
        select 1
        from information_schema.columns
        where table_schema = 'public'
        and table_name = 'l2files'
        and column_name = 'overlapfields';
    """)

    if cur.fetchone() is None:
        print(f"*** Error: l2files.overlapfields does not exist; apply "
              f"{REQUIRED_MIGRATION} first; quitting...")
        return 65

    cur.execute("select 1 from schema_migrations where filename = %s;",
                (REQUIRED_MIGRATION,))

    if cur.fetchone() is None:
        print(f"*** Error: the column exists but {REQUIRED_MIGRATION} is not "
              f"recorded in schema_migrations — the schema was changed "
              f"outside apply-db-migrations.sh; quitting...")
        return 65

    return 0


def preflight_tessellation(closed_form):

    """Refuse unless this code's constants match the release's pin.

    `overlapping_fields` reads `roman_tessellation`'s ring/bin structure
    directly, so the footprints this backfill writes are only meaningful
    against the tessellation the release says it is using.  A mismatch
    means the rows would be tiled differently from every product already
    made — exactly what `check_version` exists to refuse, and the reason
    `[tessellation]` is release content rather than a mutable parameter.
    """

    try:
        from pipeline.runtime import science_config
        content = science_config.load()
        pin = science_config.section(content, "tessellation")
    except Exception as error:
        print(f"*** Error: could not read the release's [tessellation] pin "
              f"({error}); quitting...")
        return 65

    ok = closed_form.check_version(version=pin.get("version"),
                                  digest=pin.get("digest"),
                                  nside=pin.get("nside"),
                                  nrows=pin.get("nrows"))

    if not ok or closed_form.exit_code >= 64:
        print(f"*** Error: this code's tessellation constants disagree with "
              f"the release pin {pin.get('version')!r}; quitting...")
        return 65

    print(f">> tessellation pin accepted: {pin.get('version')} "
          f"(nside {pin.get('nside')}, {pin.get('nrows')} tiles)")
    return 0


def preflight_centre_tile(cur, naxis1, naxis2, min_overlap_pixels, nrows):

    """Cross-check the new geometry against data already in the database.

    Every row already carries `field`, the tile containing the image
    centre, computed at registration by a DIFFERENT code path (astropy
    WCS, SIP included) from the one this script uses (`tan_proj2`, CD
    matrix only).  The image centre is the deepest interior point of the
    image, so its tile must appear in the image's own footprint — for ANY
    correct projection convention, at any rotation, at any declination.

    That makes this a real check rather than a tautology: it is exactly
    what a `crpix` off-by-one, a transposed CD matrix, or a degrees/radians
    slip would break, and it costs one query.  Refuses on ANY failure —
    there is no rate at which this invariant is allowed to fail.

    Deliberately computed with `union_field=False`: unioning `field` in
    first would make the assertion vacuous.
    """

    if nrows <= 0:
        print(">> centre-tile cross-check skipped (--check-rows 0) — the "
              "projection convention is UNVERIFIED for this run")
        return 0

    cur.execute(f"""
        select {", ".join(SCAN_COLUMNS)}
        from l2files
        order by rid
        limit %s;
    """, (nrows,))

    rows = cur.fetchall()

    if not rows:
        print(">> centre-tile cross-check: l2files is empty; nothing to "
              "verify or backfill")
        return 0

    bad = []
    sizes = []

    for row in rows:
        rid, field = row[0], int(row[1])
        fp = _footprint(row, naxis1, naxis2, min_overlap_pixels,
                        union_field=False)
        sizes.append(len(fp))
        if field not in fp:
            bad.append((rid, field, fp))

    if bad:
        print(f"*** Error: centre-tile cross-check FAILED on {len(bad)} of "
              f"{len(rows)} row(s) — the stored `field` is not in the "
              f"computed footprint, so the projection convention is wrong; "
              f"quitting...")
        for rid, field, fp in bad[:5]:
            print(f"      rid={rid} field={field} footprint={fp}")
        return 65

    arr = np.asarray(sizes)
    print(f">> centre-tile cross-check passed on {len(rows)} row(s): every "
          f"stored `field` lies in its own computed footprint "
          f"(footprint size min={arr.min()} max={arr.max()})")
    return 0


# ---------------------------------------------------------------------------
# Backfill.
# ---------------------------------------------------------------------------

def scope_clause(args):

    """The WHERE fragment for the rows in scope.

    Clause order fixes parameter order for every caller: `rid > %s` first
    (the keyset cursor), then the parameterless cardinality filter, then
    the optional bounds.
    """

    clauses = ["rid > %s"]

    if not args.recompute:
        clauses.append("cardinality(overlapfields) = 0")

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


def backfill(conn, cur, args, naxis1, naxis2):

    """Keyset-paginated, one committed transaction per batch."""

    select_sql = (f"select {', '.join(SCAN_COLUMNS)} "
                  f"from l2files where {scope_clause(args)} "
                  f"order by rid limit %s;")

    update_sql = "update l2files set overlapfields = %s where rid = %s;"

    last_rid = 0
    n_seen = 0
    n_written = 0
    cardinalities = []

    start = time.time()

    while True:

        params = [last_rid] + bound_params(args) + [args.batch_size]

        try:
            cur.execute(select_sql, tuple(params))
            rows = cur.fetchall()
        except Exception as error:
            print(f"*** Error scanning l2files ({error}); quitting...")
            return 67, n_written

        if not rows:
            break

        updates = []

        for row in rows:

            rid = row[0]
            last_rid = rid
            n_seen += 1

            fp = _footprint(row, naxis1, naxis2, args.min_overlap_pixels)
            cardinalities.append(len(fp))

            updates.append((fp, rid))

            if args.verbose:
                print(f"rid={rid} field={row[1]} overlapfields={fp}")

        if not args.dry_run:
            try:
                psycopg2.extras.execute_batch(cur, update_sql, updates,
                                              page_size=len(updates))
                conn.commit()
                n_written += len(updates)
            except Exception as error:
                conn.rollback()
                print(f"*** Error updating l2files ({error}); rolled back "
                      f"this batch at rid<={last_rid}; quitting...")
                return 67, n_written

        elapsed = time.time() - start
        rate = n_seen / elapsed if elapsed > 0 else 0.0
        print(f">> {n_seen} row(s) computed, {n_written} written, "
              f"through rid={last_rid} ({rate:.0f} rows/s)")

        if args.limit is not None and n_seen >= args.limit:
            print(f">> stopping at --limit {args.limit}")
            break

    if cardinalities:
        arr = np.asarray(cardinalities)
        print(f">> footprint size: min={arr.min()} "
              f"median={int(np.median(arr))} mean={arr.mean():.2f} "
              f"max={arr.max()}")
        if arr.min() < 1:
            print("*** Error: a computed footprint was empty — impossible, "
                  "since `field` is always unioned in; quitting...")
            return 67, n_written

    if args.dry_run:
        print(f">> --dry-run: {n_seen} row(s) would have been written, "
              f"nothing was")

    return 0, n_written


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def parse_args(argv=None):

    p = argparse.ArgumentParser(
        description="Backfill l2files.overlapfields (exact footprint).")

    p.add_argument("--batch-size", type=int, default=5000,
                   help="rows per read/update/commit batch (default 5000)")
    p.add_argument("--min-overlap-pixels", type=float,
                   default=DEFAULT_MIN_OVERLAP_PIXELS,
                   help="inset the image rectangle by this many pixels "
                        "before the overlap test, so a tile clipped by less "
                        "than this is not counted (default %(default)s, read "
                        "from overlapping_fields.DEFAULT_MIN_OVERLAP_PIXELS "
                        "so this script cannot drift from the registration "
                        "path); 0.0 is pure geometric exactness")
    p.add_argument("--recompute", action="store_true",
                   help="re-derive every row in scope, not only rows still "
                        "carrying the migration's empty default")
    p.add_argument("--dry-run", action="store_true",
                   help="compute and report, write nothing")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after roughly this many rows (whole batches)")
    p.add_argument("--rid-min", type=int, default=None)
    p.add_argument("--rid-max", type=int, default=None)
    p.add_argument("--naxis1", type=int, default=None,
                   help="override the release's naxis1_sciimage")
    p.add_argument("--naxis2", type=int, default=None,
                   help="override the release's naxis2_sciimage")
    p.add_argument("--check-rows", type=int, default=20,
                   help="rows to cross-check the projection convention "
                        "against their stored `field` before writing "
                        "anything; 0 disables (default 20)")
    p.add_argument("--vacuum", action="store_true",
                   help="VACUUM ANALYZE l2files after a successful run")
    p.add_argument("--verbose", action="store_true")

    return p.parse_args(argv)


def detector_size(args):

    """naxis1/naxis2 from release content, never a literal in this file.

    The image dimensions can alter a science product, so their home is
    `cdf/science/pipeline.toml` read through
    `pipeline.runtime.science_config` (that file's own placement rule).
    Falling back to the master .ini keeps this script runnable in the
    pre-cutover environment where the .ini is still the live copy;
    `--naxis1/--naxis2` is the last resort and prints that it was used.
    """

    if args.naxis1 is not None and args.naxis2 is not None:
        print(f">> detector size from command line: "
              f"{args.naxis1}x{args.naxis2}")
        return args.naxis1, args.naxis2

    try:
        from pipeline.runtime import science_config
        content = science_config.load()
        n1 = int(science_config.value(content, "INSTRUMENT", "naxis1_sciimage"))
        n2 = int(science_config.value(content, "INSTRUMENT", "naxis2_sciimage"))
        print(f">> detector size from release content "
              f"(cdf/science/pipeline.toml): {n1}x{n2}")
        return n1, n2
    except Exception as error:
        print(f">> release content unavailable ({error}); falling back to "
              f"the master .ini")

    import configparser

    rapid_sw = os.getenv("RAPID_SW")
    if rapid_sw is None:
        print("*** Error: env. var. RAPID_SW not set and release content "
              "unavailable; pass --naxis1/--naxis2; quitting...")
        return None, None

    cfg = configparser.ConfigParser()
    cfg.read(rapid_sw + "/cdf/"
             "awsBatchSubmitJobs_launchSingleSciencePipeline.ini")

    n1 = int(cfg["INSTRUMENT"]["naxis1_sciimage"])
    n2 = int(cfg["INSTRUMENT"]["naxis2_sciimage"])

    print(f">> detector size from the master .ini: {n1}x{n2}")

    return n1, n2


def main(argv=None):

    args = parse_args(argv)

    print("swname =", swname)
    print("swvers =", swvers)
    print("min_overlap_pixels =", args.min_overlap_pixels)

    if args.min_overlap_pixels < 0.0:
        print("*** Error: --min-overlap-pixels must be >= 0; quitting...")
        return 65

    naxis1, naxis2 = detector_size(args)
    if naxis1 is None:
        return 64

    if args.min_overlap_pixels >= min(naxis1, naxis2) / 2.0:
        print(f"*** Error: --min-overlap-pixels {args.min_overlap_pixels} "
              f"exceeds half the detector ({min(naxis1, naxis2)/2.0}); "
              f"quitting...")
        return 65

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        print(f"*** Error: could not open the database connection "
              f"(exit_code={dbh.exit_code}); quitting...")
        return dbh.exit_code

    closed_form = tessellation.RomanTessellationClosedForm()

    rc = 0

    try:
        conn = dbh.conn
        cur = dbh.cur

        rc = preflight_schema(cur)
        if rc:
            return rc

        rc = preflight_tessellation(closed_form)
        if rc:
            return rc

        rc = preflight_centre_tile(cur, naxis1, naxis2,
                                   args.min_overlap_pixels, args.check_rows)
        if rc:
            return rc

        total = count_in_scope(cur, args)
        print(f">> {total} row(s) in scope "
              f"({'all rows' if args.recompute else 'rows still empty'})")

        if total == 0:
            print(">> nothing to do")
            return 0

        rc, n_written = backfill(conn, cur, args, naxis1, naxis2)

        if rc == 0 and args.vacuum and not args.dry_run:
            print(">> VACUUM ANALYZE l2files")
            dbh.vacuum_analyze_table("l2files")

        if rc == 0:
            cur.execute("select count(*) from l2files "
                        "where cardinality(overlapfields) = 0;")
            remaining = cur.fetchone()[0]
            print(f">> {n_written} row(s) written; "
                  f"{remaining} row(s) still carry the empty default")
            if remaining and not args.dry_run and args.limit is None \
                    and args.rid_min is None and args.rid_max is None:
                print("*** Warning: rows remain unpopulated after an "
                      "unrestricted run — re-run to converge before "
                      "applying 103")

    finally:
        closed_form.close()
        dbh.close()

    return rc


if __name__ == "__main__":
    sys.exit(main())
