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
from pipeline.runtime.environment import resolve_region

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
        #
        # PER ATTEMPT, because there is no single right answer: the two
        # class-fixed job definitions log to two different groups, so one
        # service-wide name would read attempts of the other class from a
        # group that does not hold their streams. `_log_group_for` derives it
        # from the definition the row was submitted under; this supplies the
        # name for each, keyed by job-definition name exactly as the queue and
        # definition names are keyed.
        logs_client=session.client("logs"),
        log_groups=_log_groups_from(parameters),
        log_group=parameters.get("logs/job-log-group", "/aws/batch/job"),
    )


# Job-definition parameter key -> the log-group parameter key beside it. Both
# halves live in the tree for the same reason the queue names do: the
# definition owns the group, and a second home for the name could disagree.
_LOG_GROUP_PARAMETERS = (
    ("batch/job-definition-science", "logs/job-log-group-prompt"),
    ("batch/job-definition-bulk", "logs/job-log-group-bulk"),
)


def _log_groups_from(parameters):
    """Map each configured job-definition NAME to its log group.

    Keyed by name rather than by workload class because that is what the row's
    `binding_job_definition_arn` yields, and matching on the recorded fact
    avoids re-deriving a class the reconciler would otherwise have to infer.
    A pair whose parameters are not both present is omitted rather than
    guessed: the caller falls back and says so.
    """
    groups = {}
    for definition_key, group_key in _LOG_GROUP_PARAMETERS:
        definition = parameters.get(definition_key)
        group = parameters.get(group_key)
        if definition and group:
            # The tree holds `name:revision` for a pinned definition; the group
            # is the same for every revision of one definition.
            groups[str(definition).split(":", 1)[0]] = group
    return groups


# The parameter-tree names holding the database endpoint. The helper
# deliberately refuses to compile in a default — "it is operational
# configuration and must come from the parameter tree" — and the tree is
# exactly where these live. What was missing is the bridge between them:
# the reconciler fetched the tree, then called `connection()`, which read
# an environment nobody had populated. Found live 2026-08-06 (W8), the
# first time the service ran as a service: DBSERVER is not set, exit 70,
# every 15 seconds.
#
# The bridge was `os.environ[...] = ...`, and that is what the environment
# policy retired: the tree's values are now PASSED to `connect`, so this
# process's environment carries no endpoint it wrote for itself to read
# back. An operator's explicitly-set variable still wins — see
# `_database_endpoint`, which preserves that order deliberately.
#
# The payload does not need this because Batch job definitions carry the
# same facts as container environment; a systemd unit has no equivalent,
# and hardcoding them into the unit would put the endpoint in a second
# home — the drift the tree exists to prevent.
_DB_ENDPOINT_PARAMETERS = (
    ("db/server", "DBSERVER"),
    ("db/port", "DBPORT"),
    ("db/name", "DBNAME"),
)


def _database_endpoint(parameters):
    """The endpoint to pass to `connect`: the ENVIRONMENT over the tree.

    Per field: an explicitly-set variable wins, and the tree fills what is
    absent. That order is the one the old `_bind_database_environment`
    had — it wrote a tree value only `if not os.environ.get(variable)` —
    and it is load-bearing, not incidental: an operator debugging against
    a replica sets DBSERVER in the unit and restarts, and a tree-first
    order would silently connect them to production instead while they
    believed otherwise.

    Missing on both sides raises from `Endpoint`, naming the field. That
    is a different raiser from the helper's own `_require_env` but the
    same operator-facing outcome: the absent field is named.
    """
    from database.modules.utils.rapid_db_connect import Endpoint

    values = []
    for parameter, variable in _DB_ENDPOINT_PARAMETERS:
        value = os.environ.get(variable) or parameters.get(parameter)
        values.append(str(value) if value else None)
    return Endpoint(*values)


def _database_credentials(session):
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
    secret. The resolved credential is RETURNED and passed to `connect`,
    where it was previously written to `os.environ` as DBUSER/DBPASS. The
    unit-file and journal properties that write was protecting still hold,
    and the one it could not protect now does too: the password no longer
    exists in this process's environment, so nothing this service execs
    inherits it and no environment dump can print it.

    Returns None when the secret id is unset or DBUSER/DBPASS are already
    present, leaving the helper's boundary read to serve an operator
    running against another credential.
    """
    secret_id = os.environ.get("RAPID_DB_SECRET_ID")
    if not secret_id or os.environ.get("DBUSER"):
        return None
    from database.modules.utils.rapid_db_connect import Credentials

    secret = session.client("secretsmanager").get_secret_value(
        SecretId=secret_id)
    credential = json.loads(secret["SecretString"])
    logger.info("database credential resolved under %s from secret %s",
                "the service role", secret_id)
    return Credentials(credential["username"], credential["password"])


def main():
    _configure_logging()

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

        # Inside the try so a missing region exits 70 with the journal line
        # the operator needs, rather than an unhandled traceback.
        region = resolve_region()
        session = _assumed_session(role_arn, region)
        parameters = fetch_parameters(client=session.client("ssm"))
        endpoint = _database_endpoint(parameters)
        credentials = _database_credentials(session)
        logger.info("reconciler starting: poll=%ss records=%s diagnostics=%s",
                    poll_seconds, parameters["s3/records-bucket"],
                    parameters["s3/diagnostics-bucket"])

        with connection("rapid-reconciler", lane="transaction",
                        endpoint=endpoint,
                        credentials=credentials) as conn:
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
