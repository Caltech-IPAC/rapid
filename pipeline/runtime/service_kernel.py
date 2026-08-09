"""Shared plumbing for the two supervised services: the reconciler and the
virtual pipeline operator (VPO).

The two systemd units are the same shape — chain into the orchestrator role,
read configuration from the parameter tree, pass the endpoint and credential
to the connection helper rather than exporting them, exit on a signal, exit
distinguishably when they cannot do their work — and until this module they
each carried their own ~100-line copy of that shape. The copies had already
drifted: the operator's `_assumed_session` built a boto3 session from the
three literal strings a one-shot `sts.assume_role` returns, which is exactly
the shape the reconciler's own docstring records as a fixed production
incident (see `assumed_session` below). One kernel used by both is what makes
that drift impossible to reintroduce silently.

Health models are NOT here. Poll cadences, health thresholds and health
semantics are service-specific by design — the reconciler's two-axis health
and the operator's poll-failure counter stay in their own service modules.
This module owns only what both services need identically: logging, signal
handling, the database endpoint and credential resolution, the refreshable
assumed-role session, and the shared exit-code vocabulary.

**Credentials are fetched at connection open, not cached across
connections** (rapid_plan design/security.md, "Database service
credentials": "Consumers fetch at connection time and cache nothing across
connections, so rotation is a password change plus a new secret version,
with no restart, redeploy, or job-definition change"). `database_credentials`
below does the Secrets Manager fetch; a service calls it immediately before
each `connection(...)` open rather than once at startup and closing over the
result, so a rotated secret is picked up by the next connection a running
service opens rather than only by its next restart.
"""

import json
import logging
import os
import signal
import sys

EXIT_START_FAILED = 70
EXIT_UNHEALTHY = 71


def configure_logging():
    logging.basicConfig(
        level=os.environ.get("RAPID_LOG_LEVEL", "INFO"),
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        stream=sys.stdout)


def install_stop_signal(logger, on_signal=None):
    """Wire SIGTERM/SIGINT to a `{"go": True}` flag a poll loop can read.

    Returns the flag dict; `should_continue=lambda: flag["go"]` is the
    contract both `run_forever` loops already expect. `on_signal`, if given,
    runs after the flag flips — the operator's one-shot idle-wait path logs
    through the same callback the poll loop does, so both services log the
    same "caught signal %s" line rather than two independently worded ones.
    """
    flag = {"go": True}

    def stop(signum, _frame):
        logger.info("caught signal %s; finishing the current cycle", signum)
        flag["go"] = False
        if on_signal is not None:
            on_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return flag


def assumed_session(role_arn, region, role_session_name):
    """A boto3 session under `role_arn`, or the ambient one when unset.

    The service chains into its own role so every call is attributable to
    that role in CloudTrail rather than to whatever else runs on this host.

    **THE CREDENTIAL REFRESHES IN-LOOP.** An earlier version of this
    (formerly duplicated per service; the operator's copy was the one still
    live) called `sts.assume_role` once and built a session from the three
    literal strings it returned. Those expire — one hour by default — and a
    session built from literals cannot renew them, so every call through it
    began failing `ExpiredToken` about an hour in. The service then exited
    unhealthy and systemd restarted it, which worked, but meant an hourly
    restart of a long-running service AS ITS CREDENTIAL MECHANISM:
    NRestarts climbed forever, and a genuine crashloop was indistinguishable
    from the ordinary hourly churn in the one number an operator watches.

    `RefreshableCredentials` is botocore's own answer: it re-invokes the
    refresh callable when the credential nears expiry, inside the running
    process. The assume_role call below therefore happens on the first use
    and again shortly before each expiry, rather than once ever — and the
    process stays up, so NRestarts means what it is supposed to mean.

    Deliberately botocore's mechanism rather than a hand-rolled timer: the
    expiry arithmetic, the advisory-vs-mandatory refresh window and the
    thread-safety are exactly the parts that are easy to get subtly wrong,
    and they are already written and exercised here.
    """
    import boto3

    if not role_arn:
        return boto3.Session(region_name=region)

    from botocore.credentials import (DeferredRefreshableCredentials,
                                      create_assume_role_refresher)
    from botocore.session import get_session

    sts = boto3.client("sts", region_name=region)

    botocore_session = get_session()
    botocore_session._credentials = DeferredRefreshableCredentials(
        refresh_using=create_assume_role_refresher(
            sts, {"RoleArn": role_arn,
                  "RoleSessionName": role_session_name}),
        method="sts-assume-role")
    botocore_session.set_config_variable("region", region)
    return boto3.Session(botocore_session=botocore_session,
                         region_name=region)


# The parameter-tree names holding the database endpoint, beside the
# environment variable that overrides each. Both services key off the same
# three fields — `_DB_ENDPOINT_PARAMETERS` was a literal duplicate in each.
DB_ENDPOINT_PARAMETERS = (
    ("db/server", "DBSERVER"),
    ("db/port", "DBPORT"),
    ("db/name", "DBNAME"),
)


def database_endpoint(parameters):
    """The endpoint to pass to `connect`: the ENVIRONMENT over the tree.

    Per field, an explicitly-set variable wins and the tree fills what is
    absent — an operator debugging against a replica sets DBSERVER in the
    unit and restarts, and a tree-first order would silently connect them
    to production instead while they believed otherwise.

    Missing on both sides raises from `Endpoint`, naming the field.
    """
    from database.modules.utils.rapid_db_connect import Endpoint

    values = []
    for parameter, variable in DB_ENDPOINT_PARAMETERS:
        value = os.environ.get(variable) or parameters.get(parameter)
        values.append(str(value) if value else None)
    return Endpoint(*values)


def database_credentials(session, logger=None):
    """Resolve the DB credential under the SERVICE role, not the host's.

    Returned and passed to `connect`, never written to `os.environ`: the
    password does not exist in this process's environment, so nothing it
    execs inherits it.

    **Call this immediately before each connection open, not once at
    startup.** The security design's per-connection fetch rule exists so a
    secret rotation is picked up by the next connection a running service
    opens, with no restart; a value resolved once in `main()` and closed
    over by every connection factory defeats that even though each
    connection still authenticates successfully — it just authenticates
    with the credential that was current when the process started, not the
    one current now.

    Returns None when the secret id is unset or DBUSER/DBPASS are already
    present, leaving the helper's own boundary read to serve an operator
    running against another credential.
    """
    secret_id = os.environ.get("RAPID_DB_SECRET_ID")
    if not secret_id or os.environ.get("DBUSER"):
        return None
    from database.modules.utils.rapid_db_connect import Credentials

    secret = session.client("secretsmanager").get_secret_value(
        SecretId=secret_id)
    credential = json.loads(secret["SecretString"])
    if logger is not None:
        logger.info(
            "database credential resolved under the service role from %s",
            secret_id)
    return Credentials(credential["username"], credential["password"])


def connection_factory(session, endpoint, application_name, lane="transaction"):
    """A callable returning a fresh `connection(...)` context manager.

    The credential fetch happens INSIDE the returned callable, at each call,
    not once when this factory is built — that is what makes the per-
    connection-open rule real rather than cosmetic. A factory built by
    closing over a `credentials` value resolved once in `main()` would open
    many connections that all authenticate with one process-lifetime
    credential, which is the shape this replaces.
    """
    from database.modules.utils.rapid_db_connect import connection

    def factory():
        credentials = database_credentials(session)
        return connection(application_name, lane=lane,
                          endpoint=endpoint, credentials=credentials)
    return factory
