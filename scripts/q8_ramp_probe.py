#!/usr/bin/env python3
"""Submit one bounded, width-capped science array for the Q8 ramp.

The ramp's steps -- a width-2 probe, then 180, then 540 -- differ only in
how many units they submit. This is that one operation, with the width as
its argument, so every step of the ramp goes through the identical code
path and a step's result is comparable with the step before it.

**Submission goes through `pipeline.seams.submit_gathered`, never a raw
`SubmitJob`.** The seam pre-creates the `logical_jobs` and attempt rows
BEFORE the scheduler can start a child; a submitter that skips it creates
children the runtime's resolver cannot attribute, and the registration
gate refuses them at startup with exit 70. That is not a hypothetical:
it cost two Batch children in an earlier probe (`q9_fix_round.rst`, "The
registration gate refused a probe, correctly"). The seam is the whole
reason this script is a thin wrapper rather than a `boto3` call.

**Width is a hard cap, enforced before anything is submitted.** The
rogue-VPO incident submitted 5,057 children in 35 seconds from a
dry-run that did not suppress submission, so this script never asks a
gathering pass how much work there is and then submits it: it truncates
the gathered list to `--width` and refuses to run at all if `--width`
exceeds `--max-width`, which the caller must state explicitly. The
truncation is logged with the number dropped, because a silent cap reads
exactly like a complete run.

The credentials must be the orchestrator's, already in the process
environment before boto3 builds its default session: rapid-admin's
instance role has no `GetObject`/`PutObject` on the products bucket and
no `GetSecretValue` on the orchestrator DB secret, and assuming the role
from inside Python leaves the database lookup on the host identity,
which is denied. This script therefore does no role-chaining of its own —
the caller chains and exports, and this reads what it is given.

Run it on rapid-admin, never the laptop.
"""

import argparse
import datetime
import json
import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--width", type=int, required=True,
                        help="number of array children to submit")
    parser.add_argument("--max-width", type=int, required=True,
                        help="refuse to run if --width exceeds this; state it "
                             "explicitly so a typo cannot widen the run")
    parser.add_argument("--start", required=True,
                        help="gathering window start (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end", required=True,
                        help="gathering window end (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--run-id", default=None,
                        help="run identity; defaults to ramp-<width>-<utc>")
    parser.add_argument("--db-secret-id",
                        default="rapid/db/service/orchestrator",
                        help="Secrets Manager id for the SUBMITTER's database "
                             "identity. Defaults to the orchestrator secret: "
                             "the tree's db/secret-id names the pipeline "
                             "secret, which is the children's identity and is "
                             "denied to the submitting role")
    parser.add_argument("--dry-run", action="store_true",
                        help="gather and report, submit nothing. Unlike the "
                             "VPO's dry run, this one genuinely does not "
                             "submit -- it returns before the seam is called")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    log = logging.getLogger("q8-ramp")

    if options.width > options.max_width:
        log.error("width %d exceeds the stated max-width %d; refusing",
                  options.width, options.max_width)
        return 2
    if options.width < 1:
        log.error("width must be at least 1")
        return 2

    # Imported after the argument check so a bad invocation fails before
    # the payload's heavy imports (numpy, boto3, psycopg2) run at all.
    # `submission_env` and `min_images_to_coadd` live in
    # virtualPipelineOperator, which is a SCRIPT: importing it runs a module
    # body that reads STARTDATETIME/ENDDATETIME and exits 64 when they are
    # unset. Restructuring that module is a separate, explicitly out-of-scope
    # job, so its preconditions are satisfied here instead of being worked
    # around by copying `submission_env` into this file — a copy would be a
    # second home for the route-and-binding resolution, which is exactly the
    # class of defect this ramp keeps finding.
    #
    # The values below are the gathering window this script was given. They
    # are what the VPO would have been started with, so the module sees a
    # consistent world rather than placeholders.
    os.environ.setdefault("STARTDATETIME", options.start)
    os.environ.setdefault("ENDDATETIME", options.end)

    # The same module body reads `sys.argv[1]` as its optional processing
    # date. Left alone it would read this script's `--width`, so argv is
    # emptied across the import and restored after — the module only looks
    # at it once, at import time.
    saved_argv = sys.argv
    sys.argv = sys.argv[:1]
    try:
        from pipeline.virtualPipelineOperator import (submission_env,
                                                      min_images_to_coadd)
    finally:
        sys.argv = saved_argv

    from submission import gathering, routes
    from pipeline.seams import submit_gathered
    from database.modules.utils.rapid_db_connect import (ConnectionExecutor,
                                                         connection)

    now = datetime.datetime.now(datetime.timezone.utc)
    run_id = options.run_id or f"ramp-{options.width}-{now:%Y%m%dT%H%M%S}"

    # The MJD window's authoritative value is release content
    # ([ref_image] start/end_refimage_mjdobs); the gathering pass takes it
    # as an argument, so it is read from the release here rather than
    # given a default in this script -- a default would be a fourth home
    # for a science-affecting fact.
    from pipeline.runtime import science_config
    release = science_config.load()
    ref_image = science_config.section(release, "ref_image")
    start_mjdobs = float(ref_image["start_refimage_mjdobs"])
    end_mjdobs = float(ref_image["end_refimage_mjdobs"])

    # The endpoint comes from the parameter tree, read once here and passed
    # explicitly. The alternative -- exporting DBSERVER/DBPORT/DBNAME into
    # the environment -- would put operational configuration in a second
    # home, which `endpoint_from_environment` exists precisely to avoid
    # ("it is operational configuration and must come from the parameter
    # tree, not a default compiled in here").
    from submission.startup import fetch_parameters
    from database.modules.utils.rapid_db_connect import Endpoint

    parameters = fetch_parameters()
    endpoint = Endpoint(host=parameters["db/server"],
                        port=parameters["db/port"],
                        dbname=parameters["db/name"])
    log.info("database endpoint %s:%s/%s from the parameter tree",
             endpoint.host, endpoint.port, endpoint.dbname)

    # `resolve_credentials` reads the secret id from the environment, so a
    # caller that has already chosen an identity sets it there.
    #
    # NOT the tree's `db/secret-id`. That names the PIPELINE secret, which
    # is the identity the Batch children run as; this script is the
    # SUBMITTER, running as rapid-orchestrator-role, and that role can read
    # the orchestrator secret and is denied the pipeline one — probed
    # directly rather than inferred: READABLE rapid/db/service/orchestrator,
    # DENIED rapid/db/service/pipeline. Using the tree value here fails at
    # the secret read, one layer below where the real mismatch is.
    os.environ.setdefault("RAPID_DB_SECRET_ID", options.db_secret_id)

    log.info("gathering science units in [%s, %s), mjdobs [%s, %s)",
             options.start, options.end, start_mjdobs, end_mjdobs)
    # `gather_science_units` takes a RAPIDDB handle, not a raw connection --
    # it calls the named query methods. The handle borrows this connection
    # (`conn=`), which is the mode that neither commits nor exits the
    # process out of library code; the gathering pass is read-only, so the
    # borrowing mode's "the caller owns the transaction" is exactly right.
    import database.modules.utils.rapid_db as rapid_db

    with connection("q8-ramp-gather", endpoint=endpoint) as conn:
        handle = rapid_db.RAPIDDB(conn=conn)
        units = list(gathering.gather_science_units(
            handle, options.start, options.end,
            start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
            min_images_to_coadd=min_images_to_coadd(),
            make_references=False))

    log.info("gathered %d unit(s)", len(units))
    if not units:
        log.error("no units gathered in the window; nothing to submit")
        return 3

    # The cap, applied here and reported. Never "submit what was gathered".
    if len(units) > options.width:
        log.info("capping to --width %d; dropping %d gathered unit(s)",
                 options.width, len(units) - options.width)
        units = units[:options.width]
    elif len(units) < options.width:
        log.warning("only %d unit(s) available, fewer than the requested "
                    "width %d; submitting what there is",
                    len(units), options.width)

    summary = {
        "run_id": run_id,
        "requested_width": options.width,
        "submitting": len(units),
        "units": [{"exposure": u.exposure, "sca": u.sca} for u in units[:10]],
    }
    print(json.dumps(summary, indent=2))

    if options.dry_run:
        log.info("dry run: nothing submitted")
        return 0

    # The tree is already in hand, so it is passed rather than re-fetched:
    # two reads could disagree, and the binding recorded in every attempt
    # row must describe the submission that actually went out.
    context = submission_env(routes.JOB_TYPE_SCIENCE, parameters=parameters)
    log.info("submitting %d child(ren) as run %s to queue %s (%s)",
             len(units), run_id, context["queue"], context["job_definition"])

    with connection("q8-ramp-submit", endpoint=endpoint) as conn:
        submitted = submit_gathered(
            units,
            job_type=routes.JOB_TYPE_SCIENCE,
            queue=context["queue"],
            job_definition=context["job_definition"],
            binding=context["binding"],
            manifest_bucket=context["manifest_bucket"],
            manifest_prefix=context["manifest_prefix"],
            s3_client=context["s3_client"],
            batch_client=context["batch_client"],
            execute=ConnectionExecutor(conn).execute,
            run_id=run_id)

    total = 0
    for submission, attempt_ids in submitted:
        total += submission.array_size
        print(json.dumps({
            "batch_id": submission.batch_id,
            "job_id": submission.job_id,
            "array_size": submission.array_size,
            "attempt_rows": len(attempt_ids),
        }, indent=2))

    log.info("SUBMITTED_CHILDREN=%d in %d batch(es) under run %s",
             total, len(submitted), run_id)
    print(f"SUBMITTED_CHILDREN={total}")
    print(f"RUN_ID={run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
