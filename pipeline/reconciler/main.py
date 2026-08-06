"""The reconciler's service entry point: `python3 -m pipeline.reconciler.main`.

What the systemd unit runs. Everything configurable comes from the parameter
tree or the environment the unit sets; nothing is a constant here.

The loop never exits on error — `run_forever` logs a failed cycle and carries
on — so the only paths out of this module are a signal or a failure to start
at all. A start-time failure exits nonzero and systemd's `Restart=always`
retries it with backoff, which is the right behaviour for the case that
actually happens: the database or the parameter tree is briefly unreachable.
"""

import logging
import os
import signal
import sys

from pipeline.reconciler.service import POLL_SECONDS, ReconcilerService, run_forever
from pipeline.runtime.boundaries import S3ObjectStore

logger = logging.getLogger("rapid.reconciler.main")

EXIT_START_FAILED = 70


def _configure_logging():
    logging.basicConfig(
        level=os.environ.get("RAPID_LOG_LEVEL", "INFO"),
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        stream=sys.stdout)


def _assumed_session(role_arn, region):
    """A boto3 session under the orchestrator role, or the ambient one.

    The service chains into its own role rather than using the host's instance
    role directly, so every reconciliation call is attributable to
    `rapid-orchestrator-role` in CloudTrail rather than to whatever else runs
    on this host.
    """
    import boto3

    if not role_arn:
        return boto3.Session(region_name=region)

    sts = boto3.client("sts", region_name=region)
    assumed = sts.assume_role(RoleArn=role_arn,
                              RoleSessionName="rapid-reconciler")
    credentials = assumed["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region)


def build_service(session, parameters, conn):
    s3 = session.client("s3")
    records_bucket = parameters["s3/records-bucket"]
    diagnostics_bucket = parameters["s3/diagnostics-bucket"]
    return ReconcilerService(
        conn=conn,
        batch_client=session.client("batch"),
        records_store=S3ObjectStore(records_bucket, client=s3),
        diagnostics_store=S3ObjectStore(diagnostics_bucket, client=s3),
        s3_client=s3,
        records_prefix=parameters["s3/records-prefix"],
        diagnostics_bucket=diagnostics_bucket,
    )


def main():
    _configure_logging()

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    role_arn = os.environ.get("RAPID_RECONCILER_ROLE_ARN")
    poll_seconds = int(os.environ.get("RAPID_RECONCILER_POLL_SECONDS",
                                      POLL_SECONDS))

    running = {"go": True}

    def stop(signum, _frame):
        logger.info("caught signal %s; finishing the current cycle", signum)
        running["go"] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        from database.modules.utils.rapid_db_connect import connection
        from submission.startup import fetch_parameters

        session = _assumed_session(role_arn, region)
        parameters = fetch_parameters(client=session.client("ssm"))
        logger.info("reconciler starting: poll=%ss records=%s diagnostics=%s",
                    poll_seconds, parameters["s3/records-bucket"],
                    parameters["s3/diagnostics-bucket"])

        with connection("rapid-reconciler", lane="transaction") as conn:
            service = build_service(session, parameters, conn)
            run_forever(service, poll_seconds=poll_seconds,
                        should_continue=lambda: running["go"])
    except Exception:  # noqa: BLE001 - a start failure is worth exiting for
        logger.exception("the reconciler could not start")
        return EXIT_START_FAILED

    logger.info("reconciler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
