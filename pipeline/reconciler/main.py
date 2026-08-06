"""The reconciler's service entry point: `python3 -m pipeline.reconciler.main`.

What the systemd unit runs. Everything configurable comes from the parameter
tree or the environment the unit sets; nothing is a constant here.

A single failed cycle never exits — `run_forever` logs it and carries on,
because the failures the reconciler exists to catch are the ones likely to
make one throw. There are three ways out: a signal, a failure to start at all
(exit 70), and the service becoming unable to do its work (exit 71) — either
consecutive poll exceptions or polls that keep reaching attempts and
classifying none. systemd's `Restart=always` retries all of them with backoff,
which is the right behaviour for the cases that actually happen: the database
or the parameter tree briefly unreachable, a stale connection, a rotated
credential.
"""

import json
import logging
import os
import signal
import sys

from pipeline.reconciler.service import (POLL_SECONDS, ReconcilerService,
                                         ReconcilerUnhealthy, run_forever)
from pipeline.runtime.boundaries import S3ObjectStore

logger = logging.getLogger("rapid.reconciler.main")

EXIT_START_FAILED = 70
# Distinct from a start failure so the journal, and anything reading exit
# codes, can tell "never got going" from "was working and stopped being able
# to" — two different operator responses (round-3 finding #6).
EXIT_UNHEALTHY = 71


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
        # CloudWatch, for reconstructing a record with no predecessor (#16).
        # The log group is a parameter rather than a constant because it is
        # the Batch job definition's, and the definition owns that name.
        logs_client=session.client("logs"),
        log_group=parameters.get("logs/job-log-group", "/aws/batch/job"),
    )


# The parameter-tree names holding the database endpoint, and the
# environment variables `rapid_db_connect` reads them from. That helper
# deliberately refuses to compile in a default — "it is operational
# configuration and must come from the parameter tree" — and the tree is
# exactly where these live. What was missing is the bridge between them:
# the reconciler fetched the tree, then called `connection()`, which read
# an environment nobody had populated. Found live 2026-08-06 (W8), the
# first time the service ran as a service: DBSERVER is not set, exit 70,
# every 15 seconds.
#
# The payload does not need this because Batch job definitions carry the
# same facts as container environment; a systemd unit has no equivalent,
# and hardcoding them into the unit would put the endpoint in a second
# home — the drift the tree exists to prevent.
_DB_ENVIRONMENT = (
    ("db/server", "DBSERVER"),
    ("db/port", "DBPORT"),
    ("db/name", "DBNAME"),
)


def _bind_database_environment(parameters):
    """Publish the tree's database endpoint into the connection helper's env.

    Only fills what is absent: an explicitly-set variable wins, so an
    operator debugging against another endpoint is not silently overridden.
    Missing names are left alone rather than defaulted — the helper's own
    fail-loud check is the one that should report them, by name.
    """
    for parameter, variable in _DB_ENVIRONMENT:
        value = parameters.get(parameter)
        if value and not os.environ.get(variable):
            os.environ[variable] = str(value)


def _bind_database_credentials(session):
    """Resolve the DB credential under the SERVICE role, not the host's.

    `rapid_db.get_db_credentials` fetches RAPID_DB_SECRET_ID through boto3's
    default credential chain, which inside this container is the host's
    instance role — and that role is deliberately NOT granted the
    orchestrator secret (rapid-db-instance-role reads it only on the DB host,
    for the association pass). So the fetch failed with AccessDenied and the
    service crashlooped on "could not resolve database credentials", found
    live 2026-08-06 (W8) the first time it ran as a service.

    The session passed here is already chained into
    RAPID_RECONCILER_ROLE_ARN, which is the identity that may read the
    secret. Resolving through it and handing the result to the helper as
    DBUSER/DBPASS keeps the credential inside this process: it is never in
    the unit file, never in the container's declared environment, and never
    in the journal — the properties the unit's own "no credentials in the
    environment" note is protecting.

    Left alone when the secret id is unset or DBUSER/DBPASS are already
    present, so an operator running against another credential still wins.

    RAPID_DB_SECRET_ID is CLEARED once the credential is in hand, and that
    is load-bearing rather than tidying: `get_db_credentials` takes the
    secret branch whenever that variable is set at all, and would re-fetch
    through the ambient chain — the very failure this works around —
    ignoring the DBUSER/DBPASS it just received. Clearing it selects the
    already-resolved branch.
    """
    secret_id = os.environ.get("RAPID_DB_SECRET_ID")
    if not secret_id or os.environ.get("DBUSER"):
        return
    secret = session.client("secretsmanager").get_secret_value(
        SecretId=secret_id)
    credential = json.loads(secret["SecretString"])
    os.environ["DBUSER"] = credential["username"]
    os.environ["DBPASS"] = credential["password"]
    os.environ.pop("RAPID_DB_SECRET_ID", None)
    logger.info("database credential resolved under %s from secret %s",
                "the service role", secret_id)


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
        _bind_database_environment(parameters)
        _bind_database_credentials(session)
        logger.info("reconciler starting: poll=%ss records=%s diagnostics=%s",
                    poll_seconds, parameters["s3/records-bucket"],
                    parameters["s3/diagnostics-bucket"])

        with connection("rapid-reconciler", lane="transaction") as conn:
            service = build_service(session, parameters, conn)
            run_forever(service, poll_seconds=poll_seconds,
                        should_continue=lambda: running["go"])
    except ReconcilerUnhealthy:
        # NOT a start failure (round-3 finding #6). `ReconcilerUnhealthy`
        # subclasses RuntimeError, so the handler below caught it and told the
        # journal "the reconciler could not start" about a service that had
        # been running and polling for hours. The restart was right; the
        # explanation an operator reads was wrong, and it pointed at
        # credentials and the parameter tree instead of at whatever is
        # actually blocking classification.
        logger.exception("the reconciler is unhealthy and is exiting so the "
                         "supervisor restarts it")
        return EXIT_UNHEALTHY
    except Exception:  # noqa: BLE001 - a start failure is worth exiting for
        logger.exception("the reconciler could not start")
        return EXIT_START_FAILED

    logger.info("reconciler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
