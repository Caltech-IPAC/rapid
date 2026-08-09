"""
File:    live_catalog_load_probe.py

ONE catalog-load unit through the production submission path, to measure the
load rate the design asks for by name.

This is the successor to `live_catalog_load_rate.py`, which was committed
UNRUN because the gatherers it drove read `Jobs` — a table with zero rows —
and the loader it exercised built `<proc_date>/jid<N>/` keys against a prefix
that does not exist. Both are re-sourced now, so the measurement is reachable:
this drives the same production loader against the attempt-scoped products the
catalog actually holds.

WHAT IT MEASURES, and why the number matters: the database design admits that
trading durability for load speed is "an argued-for regression requiring
measurements", and the staging-plus-upsert shape is what the argument would
have to be made against. `load_through_staging` already records
`rows_written`, `seconds` and `rate` into the attempt's provenance; this run
is what puts a real number in them.

It submits ONE unit — the bound is the point, not an accident of the probe.

Usage (inside the pipeline image, with the submission environment set):

    python3.11 -m pipeline.registration.test.live_catalog_load_probe \
        --proc-date 20260809 --sca 7
"""

import argparse
import base64
import json
import os
import sys
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proc-date", required=True,
                        help="processing date, yyyymmdd")
    parser.add_argument("--sca", type=int, required=True,
                        help="the one SCA to submit")
    parser.add_argument("--dry-run", action="store_true",
                        help="gather and report, submit nothing")
    parser.add_argument("--parameters-json",
                        help="the parameter tree as JSON, relative-keyed. "
                             "Injected when the identity running this probe "
                             "cannot read /rapid/pipeline itself — the tree "
                             "belongs to the Batch job role, and widening "
                             "the admin host's grant to run a probe would be "
                             "an IAM change made to answer a measurement "
                             "question. `submission_env` already takes the "
                             "values by injection for the same reason.")
    args = parser.parse_args(argv)

    from database.modules.utils.rapid_db import RAPIDDB
    from submission.gathering import gather_catalog_load_units
    from submission.routes import JOB_TYPE_CATALOG_LOAD

    handle = RAPIDDB()
    if getattr(handle, "conn", None) is None or handle.exit_code >= 64:
        print("*** cannot reach the database; quitting")
        return 64

    units = [unit for unit in gather_catalog_load_units(handle, args.proc_date)
             if int(unit.fields["sca"]) == args.sca]
    if not units:
        print("*** no catalog-load unit for {} SCA {}: the gatherer found "
              "nothing, which after the re-source means the date genuinely "
              "has no registered science products for that SCA".format(
                  args.proc_date, args.sca))
        return 65

    unit = units[0]
    inputs = unit.fields.get("product_inputs") or []
    print(">> unit {}: target_table={} product_inputs={}".format(
        unit.key, unit.fields["target_table"], len(inputs)))
    for item in inputs[:3]:
        print("   pid={} attempt={} uri={}".format(
            item["pid"], item["attempt_id"], item["difference_image_uri"]))
    if len(inputs) > 3:
        print("   ... and {} more".format(len(inputs) - 3))

    if args.dry_run:
        print(">> --dry-run: gathered only, nothing submitted")
        return 0

    from pipeline import seams, virtualPipelineOperator as vpo

    # `submission_env` returns a dict and does NOT carry an `execute`: the
    # attempt-row writer binds to a CONNECTION, and which connection is the
    # caller's decision — the operator opens a fresh one per phase so a
    # submission's rows and its registration pass cannot share a transaction.
    parameters = None
    if args.parameters_json:
        parameters = json.loads(args.parameters_json)
    elif os.environ.get("RAPID_PARAMETERS_B64"):
        parameters = json.loads(
            base64.b64decode(os.environ["RAPID_PARAMETERS_B64"]))
    if parameters:
        print(">> parameter tree injected: {} keys".format(len(parameters)))
    env = vpo.submission_env(JOB_TYPE_CATALOG_LOAD, parameters=parameters)
    run_id = "catalog-load-probe-{}".format(uuid.uuid4().hex[:12])
    print(">> run_id: {}".format(run_id))
    print(">> queue={} job_definition={}".format(
        env["queue"], env["job_definition"]))

    from database.modules.utils.rapid_db_connect import ConnectionExecutor

    executor = ConnectionExecutor(handle.conn)

    results = seams.submit_gathered(
        [unit], job_type=JOB_TYPE_CATALOG_LOAD,
        queue=env["queue"], job_definition=env["job_definition"],
        binding=env["binding"],
        manifest_bucket=env["manifest_bucket"],
        manifest_prefix=env["manifest_prefix"],
        s3_client=env["s3_client"], batch_client=env["batch_client"],
        execute=executor.execute, run_id=run_id,
        reason="catalog-load-probe")

    for submission, attempt_ids in results:
        print(">> submitted job {} ({} children), attempt rows: {}".format(
            submission.job_id, submission.array_size, attempt_ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
