"""Measure the catalog bulk-load rate the database design asks for by name.

"Bulk loads land through a staging table and an upsert so a rerun cannot
produce duplicate rows; **the load rate of that shape is measured at
implementation**" (database design § Integrity and durability). The
conversion recorded the measurement as owed for want of real source data;
the role binding populated `diffimages`, and the science attempts behind
those rows published real SFFT PSF catalogues, so the data exists now.

WHY THIS IS NOT A BATCH UNIT, AND WHY IT DOES NOT RUN YET. Three
independent facts, each measured live on 2026-08-09, block a real
catalog-load measurement — and none of them is this script's to fix:

1. `gather_catalog_load_units` enumerates from the legacy `jobs` table
   (`get_scas_with_science_jobs_for_processing_date`). `jobs` holds ZERO
   rows, so the gatherer yields no units at all: there is nothing to
   submit.
2. `download_psf_catalogs` reads `<proc_date>/jid<N>/<name>`. The product
   bucket has no such prefix — every product written since the submission
   restructure is attempt-scoped, `science/<run>/<field>/<sca>/attempt-N/`.
   A unit that did get submitted would download nothing and record
   `rows_written=0`: a real outcome, but not a rate.
3. Running the loader OUTSIDE a Batch job cannot reach the bytes either.
   The catalogues are readable by `rapid-batch-job-role`, which trusts
   only `ecs-tasks.amazonaws.com`; the orchestrator role's S3 grant stops
   at the submission-manifest prefix, and the rapid-admin instance role
   gets 403 on science products. Widening any of those to take a
   measurement would be an IAM change made to answer a question.

So the load rate stays UNMEASURED, and it is recorded as a named fork
rather than faked with synthetic rows — a rate measured against invented
data would answer a different question than the design asked. The real
dependency is migrating the post-DB gatherers and loader off `jids` onto
attempt-scoped products; when that lands, this script measures the shape
the design named, through the production loader, against production
bytes. It is kept, unrun, for that day.

Catalogues are staged into /tmp/catalog-load-rate by the caller and named
here by basename:

    python3 -m pipeline.registration.test.live_catalog_load_rate <name>...
"""

import os
import sys
import time

from database.modules.utils import rapid_db
from database.modules.utils.rapid_db_connect import transaction
from pipeline.stages import catalog_db
from pipeline.stages.post_db import SOURCES_COLUMNS, _write_sources_csv


class _Facts:
    """The per-source identity values, constant down the file.

    Real values from the registered difference image this catalogue belongs
    to (`diffimages` pid 1086 / field 4637678 / fid 8 / sca 7), not
    synthetic ones: the CSV has to be the shape production writes, or the
    rate measures a different table's worth of work.
    """

    def __init__(self, pid, field, fid, expid, mjdobs):
        self.pid = pid
        self.field = field
        self.fid = fid
        self.expid = expid
        self.mjdobs = mjdobs


class _Unit:
    def __init__(self, facts):
        self.facts = facts


class _Context:
    """The little the CSV writer needs, without the stage machinery."""

    def __init__(self, scratch_dir, logger, unit):
        self._scratch = scratch_dir
        self.logger = logger
        self.unit = unit

    def scratch(self, name):
        return os.path.join(self._scratch, name)


class _Logger:
    def info(self, message, *args):
        print(message % args if args else message, flush=True)

    warning = error = info


def main(argv):
    if not argv:
        print("usage: live_catalog_load_rate <s3-uri>...")
        return 64

    scratch = "/tmp/catalog-load-rate"
    os.makedirs(scratch, exist_ok=True)
    logger = _Logger()

    # The catalogues are staged by the CALLER, into this directory. The
    # database identity this runs under is the orchestrator role, whose S3
    # grant is scoped to the submission-manifest prefix — it cannot read
    # science products, and giving it that read to run a probe would be a
    # grant change to answer a measurement question.
    catalogs = []
    for name in argv:
        target = name if os.path.isabs(name) else os.path.join(scratch, name)
        if not os.path.exists(target):
            print("*** missing staged catalogue {}".format(target))
            return 64
        print("staged {} ({:.1f} MB)".format(
            target, os.path.getsize(target) / 1e6), flush=True)
        catalogs.append(target)

    dbh = rapid_db.RAPIDDB()
    if dbh.conn is None or dbh.exit_code >= 64:
        print("*** cannot reach the database; quitting")
        return 64

    # A probe-scoped child table, named for the shape the loader validates.
    table = "sources_20260809_99"
    try:
        with transaction(dbh.conn) as cursor:
            created = catalog_db.create_child_table(cursor, table, "sources")
            print("child table {} ready (created={})".format(table, created),
                  flush=True)

        facts = _Facts(pid=1086, field=4637678, fid=8, expid=4637678,
                       mjdobs=61000.0)
        context = _Context(scratch, logger, _Unit(facts))

        csv_path = os.path.join(scratch, table + ".csv")
        started = time.monotonic()
        rows = _write_sources_csv(context, catalogs, csv_path)
        print("CSV_ROWS={} csv_seconds={:.2f}".format(
            rows, time.monotonic() - started), flush=True)

        with transaction(dbh.conn) as cursor:
            result = catalog_db.load_through_staging(
                cursor, csv_path, table, "sources", SOURCES_COLUMNS)
        print("ROWS_STAGED={rows_staged} ROWS_WRITTEN={rows_written} "
              "SECONDS={seconds:.2f} RATE_ROWS_PER_SECOND={rate:.0f}".format(
                  **result), flush=True)

        # THE RERUN THE SHAPE EXISTS FOR: the same CSV again must write
        # nothing and still converge, which is what makes a half-dead unit
        # simply re-submittable.
        with transaction(dbh.conn) as cursor:
            again = catalog_db.load_through_staging(
                cursor, csv_path, table, "sources", SOURCES_COLUMNS)
        print("RERUN_ROWS_WRITTEN={rows_written} "
              "RERUN_RATE={rate:.0f}".format(**again), flush=True)
        if again["rows_written"] != 0:
            print("*** the rerun wrote rows; the upsert did not converge")
            return 1
        print("LOAD_RATE_OK", flush=True)
        return 0
    finally:
        dbh.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
