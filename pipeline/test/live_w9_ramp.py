"""W9: the validation ramp — real g0001 work through the production VPO path.

One ramp STEP per invocation. The step's size is the number of array
children it submits; the ramp is 18 -> 90 -> 270 across three runs, each
gated on the previous step's evidence before the next is launched.

What makes this the production path rather than a test harness that
resembles one: the binding comes from `submission_env`, which resolves
the ACTIVE revisioned job-definition ARN per route class and records the
same value it submits (the round-5 fix); units come from
`submission.gathering`, the same functions the VPO calls; submission goes
through `pipeline.seams.submit_gathered`, which pre-creates the attempt
rows before SubmitJob. Nothing here reimplements any of that.

The one thing this adds over the VPO's own `__main__` is a CHILD CAP. The
VPO submits whatever a window makes ready — 109 fields here, every time —
and a ramp needs 18, then 90, then 270. The cap is applied to the gathered
unit list, taking units in a stable order so a larger step is a superset
of a smaller one and the steps stay comparable.

Usage (inside the pinned image, on rapid-admin):

    python3.11 -m pipeline.test.live_w9_ramp <phase> <cap> [run-tag]

`phase` is `reference` or `science`. Prints a JSON summary on stdout as
its last line; exits non-zero if submission failed.
"""

import datetime
import json
import logging
import os
import sys

from database.modules.utils import rapid_db_connect as dbc
from database.modules.utils import rapid_db as db
from pipeline import seams
from submission import gathering, routes
from submission.startup import fetch_parameters

# The g0001 staged subset's own window. Reference readiness is computed
# over the whole window (a reference image is every good frame of a tile),
# so both phases see the same one.
START = os.environ.get("W9_START", "2027-10-01 00:00:00")
END = os.environ.get("W9_END", "2027-10-08 00:00:00")

# `submission_env`, `mjd_window` and `min_images_to_coadd` all live in the
# operator package now (IR-1a extraction) and are importable with no
# side effects, so this script no longer needs to fake
# STARTDATETIME/ENDDATETIME to satisfy `virtualPipelineOperator`'s old
# module-scope startup before importing it.
from pipeline.operator.gathering import mjd_window, min_images_to_coadd
from pipeline.operator.submission import submission_env

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("w9.ramp")


def _capped(units, cap):
    """The first `cap` units in gathering order.

    Gathering yields (field, filter) pairs in query order and the frames
    within a pair in mjdobs/SCA order, both stable — so step N+1 gathers a
    superset of step N and the ramp measures the same work getting wider,
    not a different sample each time.
    """
    out = []
    for unit in units:
        out.append(unit)
        if len(out) >= cap:
            break
    return out


def main():
    if len(sys.argv) < 3:
        print("usage: live_w9_ramp.py <reference|science> <cap> [run-tag]",
              file=sys.stderr)
        return 2
    phase = sys.argv[1]
    cap = int(sys.argv[2])
    tag = sys.argv[3] if len(sys.argv) > 3 else ""

    stamp = f"{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
    run_id = f"w9-ramp-{phase}-{cap}-{stamp}" + (f"-{tag}" if tag else "")

    if phase == "reference":
        job_type = routes.JOB_TYPE_REFERENCE_IMAGE
    elif phase == "science":
        job_type = routes.JOB_TYPE_SCIENCE
    else:
        print(f"unknown phase {phase!r}", file=sys.stderr)
        return 2

    # The production resolver: ACTIVE revisioned ARN per route class, and
    # the binding records what it submits.
    parameters = fetch_parameters()
    context = submission_env(job_type, parameters=parameters)

    print(f"=== W9 ramp step: phase={phase} cap={cap} run={run_id} ===")
    print(f"    definition {context['job_definition']}")
    print(f"    queue      {context['queue']}")
    print(f"    binding    rev={context['binding'].job_definition_rev} "
          f"arn={context['binding'].job_definition_arn}")

    dbh = db.RAPIDDB()
    if dbh.exit_code >= 64:
        print(f"!! database handle unusable: exit_code={dbh.exit_code}",
              file=sys.stderr)
        return dbh.exit_code

    start_mjd, end_mjd = mjd_window(START, END)
    min_coadd = min_images_to_coadd()
    print(f"    window     {START} .. {END} (mjd {start_mjd:.5f}..{end_mjd:.5f})")
    print(f"    min_coadd  {min_coadd}")

    if phase == "reference":
        units = gathering.gather_reference_units(
            dbh, START, END, start_mjdobs=start_mjd, end_mjdobs=end_mjd,
            min_images_to_coadd=min_coadd,
            s3_client=context["s3_client"],
            # The coadd-input list goes to the PRODUCTS bucket, not the
            # staged-input bucket. The list is authored by this submission
            # -- it is not upstream data -- and the staged-input bucket is
            # read-only for service identities by design (the shared
            # permissions boundary's `S3StagedInputRead`) as well as sealed
            # create-once. The consuming stages read the bucket out of
            # `coadd_inputs_uri` itself, so the location is the submitter's
            # to choose.
            job_bucket=context["manifest_bucket"],
            run_id=run_id)
    else:
        units = gathering.gather_science_units(
            dbh, START, END, start_mjdobs=start_mjd, end_mjdobs=end_mjd,
            min_images_to_coadd=min_coadd)

    capped = _capped(units, cap)
    print(f"    gathered   {len(capped)} unit(s) (cap {cap})")
    if not capped:
        print(json.dumps({"run_id": run_id, "phase": phase, "cap": cap,
                          "submitted_units": 0, "batches": 0,
                          "note": "nothing ready in the window"}))
        return 0

    submitted_at = datetime.datetime.now(datetime.timezone.utc)
    with dbc.connection("rapid-w9-ramp", lane="transaction") as conn:
        results = seams.submit_gathered(
            capped,
            job_type=job_type,
            queue=context["queue"],
            job_definition=context["job_definition"],
            binding=context["binding"],
            manifest_bucket=context["manifest_bucket"],
            manifest_prefix=context["manifest_prefix"],
            s3_client=context["s3_client"],
            batch_client=context["batch_client"],
            execute=dbc.ConnectionExecutor(conn).execute,
            run_id=run_id,
            reason="w9-ramp")

    batches = []
    total_children = 0
    for submission, attempt_ids in results:
        total_children += len(attempt_ids)
        batches.append({
            # `job_id`, not `scheduler_job_id`: the submission result
            # (`submission/submit.py`) names the field `job_id`, so the old
            # `getattr(submission, "scheduler_job_id", None)` matched nothing
            # and silently produced null in every summary this harness has
            # ever printed — the getattr default turning a wrong field name
            # into a plausible-looking value rather than an AttributeError.
            # The id itself was never missing: `attempts.scheduler_job_id` is
            # populated correctly on the same submission, which is how the
            # job had to be recovered by name after the fact.
            "scheduler_job_id": submission.job_id,
            "array_size": submission.array_size,
            "attempt_ids": list(attempt_ids),
        })

    summary = {
        "run_id": run_id,
        "phase": phase,
        "cap": cap,
        "submitted_units": len(capped),
        "children": total_children,
        "batches": batches,
        "submitted_at": submitted_at.isoformat(),
        "job_definition_arn": context["binding"].job_definition_arn,
        "job_definition_rev": context["binding"].job_definition_rev,
        "image_digest": context["binding"].image_digest,
    }
    print("W9-RAMP-SUMMARY " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
