"""W8: submit ONE real registration job through the production seam.

Uses `submission.seams.submit_units` — the same path production takes — so
what is proven is the whole chain: manifest published to S3, attempt rows
pre-created BEFORE SubmitJob (finding #2's order), scheduler ids backfilled
after, one real Batch array job, and the reconciler closing it.

Registration is the job type this database can actually support today: it
consumes reconciled outcomes and needs no PSF or coadd inputs, neither of
which exist for g0001 (PSFs and RefImages are both empty). The science and
reference-image types are blocked on that data, not on this layer.
"""

import datetime
import json
import logging
import os
import sys

import boto3

from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import ExecutionBinding
from pipeline import seams
from submission import payloads
from submission.manifest import ProcessingUnit, UnitFacts
from submission.routes import JOB_TYPE_REGISTRATION, JOB_TYPE_SCIENCE, route_for

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("w8.live")

RUN = f"w8-live-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"


def main():
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    session = boto3.Session(region_name=region)
    s3 = session.client("s3")
    batch = session.client("batch")

    definition = os.environ["RAPID_JOB_DEFINITION"]
    queue = os.environ["RAPID_JOB_QUEUE"]
    manifest_bucket = os.environ.get("RAPID_PRODUCTS_BUCKET",
                                     "roman-rapid-products")
    manifest_prefix = os.environ.get("RAPID_MANIFEST_PREFIX", "submissions")

    described = batch.describe_job_definitions(
        jobDefinitionName=definition.split(":")[0], status="ACTIVE")
    latest = described["jobDefinitions"][-1]
    binding = ExecutionBinding(
        job_definition_arn=latest["jobDefinitionArn"],
        image_digest=latest["containerProperties"]["image"].split("@", 1)[-1],
        manifest_checksum="pending",
        job_definition_rev=int(latest["revision"]),
        release_identity=f"rapid-pipeline@{latest['revision']}")

    # One unit. Registration's unit is a pass, not a per-SCA image, but it
    # still keys like every other unit so the run-scoped logical-job identity
    # and the array-index binding work unchanged.
    #
    # FLAGGED (payload migration, see the migration report): JOB_TYPE_REGISTRATION
    # has NO entry in `payloads.PAYLOAD_TYPES` — it is deliberately out of the
    # typed-payload registry's scope (submission/subjects.py's own docstring
    # names "registration, reprocessing" as excluded). There is therefore no
    # payload type this call can legitimately declare JOB_TYPE_REGISTRATION
    # under. Built here as a SciencePayload only to keep this live probe's
    # `ProcessingUnit` construction syntactically valid post-migration; the
    # payload's declared job_type ("science") does NOT match the job_type this
    # probe submits under (JOB_TYPE_REGISTRATION, below). This is very likely
    # to raise downstream: `seams.submit_units` -> `_attach_work_unit` ->
    # `_input_scope_for` -> `subjects.build_input_scope` calls
    # `subjects.subject_for(JOB_TYPE_REGISTRATION)`, and registration has no
    # entry in `subjects.SUBJECTS` either — so this probe appears to already
    # be broken against the current `submit_units`/`subjects` contract,
    # independent of this construction fix. Not resolved here: no non-test
    # file may be touched by this task, and the fix belongs to whoever owns
    # the registration-submission contract.
    unit = ProcessingUnit(
        payload=payloads.build(JOB_TYPE_SCIENCE, exposure=999200, sca=1),
        facts=UnitFacts(rid=None))

    print(f"=== W8 live registration, run {RUN} ===")
    print(f"    definition {latest['jobDefinitionArn']}")
    print(f"    queue      {queue}")
    print(f"    image      {binding.image_digest}")

    with dbc.connection("rapid-w8-live", lane="transaction") as conn:
        submission = seams.submit_units(
            [unit],
            job_type=JOB_TYPE_REGISTRATION,
            queue=queue,
            job_definition=latest["jobDefinitionArn"],
            binding=binding,
            manifest_bucket=manifest_bucket,
            manifest_prefix=manifest_prefix,
            s3_client=s3,
            batch_client=batch,
            execute=dbc.ConnectionExecutor(conn),
            run_id=RUN,
            reason="w8-live-proof")

    print(json.dumps({"run_id": RUN,
                      "submission": str(submission)[:400]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
