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

The service plumbing this module used to carry on its own — logging setup,
the refreshable assumed-role session, endpoint/credential resolution, the
exit-code constants — now lives in `pipeline.runtime.service_kernel`, shared
with the operator's `pipeline.operator.service`. `_database_endpoint` and
`_database_credentials` stay here as thin wrappers rather than moving the
call sites to the kernel names directly, so this module's own test suite
(`pipeline/reconciler/test/test_main.py`) keeps working unchanged.

**A FRESH CONNECTION, AND FRESH CREDENTIALS, PER POLL** (kernel contract:
`service_kernel.database_credentials`'s "call this immediately before each
connection open, not once at startup", citing `rapid_plan design/security.md`
"Database service credentials"). This module used to resolve credentials
once and open ONE connection for the whole `run_forever` loop — the exact
"cached across connections" shape the operator's own `main()` was found
violating and fixed (`pipeline.operator.service._connection_factory`,
`_execute_factory`: a fresh connection per pass, credentials fetched inside
the factory at each call). The reconciler now does the same at its own
granularity, a fresh poll rather than a fresh pass: `_poll_cycle` below opens
one connection, builds one `ReconcilerService` around it, runs exactly one
`poll_once`, and closes it, so a rotated secret takes effect on the very
next poll rather than only on the next process restart. `ReconcilerService`
itself already documents that it "owns no state between polls beyond its
connections" — this is that design realized at the process level, not a
change to the service's own per-poll body.
"""

import logging
import os
import sys
import time

from pipeline.reconciler.service import (POLL_FAILURE_THRESHOLD, POLL_SECONDS,
                                         ReconcilerService, ReconcilerUnhealthy,
                                         run_forever)
from pipeline.runtime import service_kernel
from pipeline.runtime.boundaries import S3ObjectStore
from pipeline.runtime.environment import resolve_region
from pipeline.runtime.service_kernel import EXIT_START_FAILED, EXIT_UNHEALTHY

logger = logging.getLogger("rapid.reconciler.main")

_configure_logging = service_kernel.configure_logging


def _assumed_session(role_arn, region):
    """A boto3 session under the orchestrator role, or the ambient one.

    Delegates to `service_kernel.assumed_session`, which carries the
    refreshable-credential mechanism this docstring used to document in
    full: an earlier version called `assume_role` once and built a session
    from the three literal strings it returned, which expire in an hour and
    cannot renew, so every reconciliation call began failing `ExpiredToken`
    about an hour in — a fixed production incident, and the reason the
    kernel's session builder uses `DeferredRefreshableCredentials` rather
    than literals for both services now.
    """
    return service_kernel.assumed_session(
        role_arn, region, role_session_name="rapid-reconciler")


def build_service(session, parameters, conn, poll_seconds=None):
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
        # The real poll cadence, purely for `reconciler_runs.poll_seconds` —
        # never assigned before this fix (`ReconcilerService.__init__` never
        # took it, `write_heartbeat` read a getattr default that was always
        # None). `main`'s outer loop knows the true interval even though
        # `run_forever` is called once per poll with `poll_seconds=0` here
        # (see `run_forever`'s own docstring for why it does not stamp this
        # itself) — so this is where the real value is threaded through.
        poll_seconds=poll_seconds,
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


# The parameter-tree names holding the database endpoint. Found live
# 2026-08-06 (W8), the first time the service ran as a service: DBSERVER
# is not set, exit 70, every 15 seconds — the bridge from tree to
# `connect()` was missing entirely.
#
# The payload does not need this because Batch job definitions carry the
# same facts as container environment; a systemd unit has no equivalent,
# and hardcoding them into the unit would put the endpoint in a second
# home — the drift the tree exists to prevent.
_DB_ENDPOINT_PARAMETERS = service_kernel.DB_ENDPOINT_PARAMETERS


def _database_endpoint(parameters):
    """The endpoint to pass to `connect`: the ENVIRONMENT over the tree.

    Delegates to `service_kernel.database_endpoint`, shared with the
    operator. Per field: an explicitly-set variable wins, and the tree
    fills what is absent — an operator debugging against a replica sets
    DBSERVER in the unit and restarts, and a tree-first order would
    silently connect them to production instead while they believed
    otherwise. Missing on both sides raises from `Endpoint`, naming the
    field.
    """
    return service_kernel.database_endpoint(parameters)


def _database_credentials(session):
    """Resolve the DB credential under the SERVICE role, not the host's.

    Delegates to `service_kernel.database_credentials`, shared with the
    operator. `rapid_db.get_db_credentials` fetches RAPID_DB_SECRET_ID
    through boto3's default credential chain, which inside this container
    is the host's instance role — and that role is deliberately NOT
    granted the orchestrator secret (rapid-db-instance-role reads it only
    on the DB host, for the association pass). So the fetch failed with
    AccessDenied and the service crashlooped on "could not resolve
    database credentials", found live 2026-08-06 (W8) the first time it
    ran as a service.

    The session passed here is already chained into
    RAPID_RECONCILER_ROLE_ARN, which is the identity that may read the
    secret. The resolved credential is RETURNED and passed to `connect`,
    never written to `os.environ`: the password does not exist in this
    process's environment, so nothing it execs inherits it and no
    environment dump can print it.

    Returns None when the secret id is unset or DBUSER/DBPASS are already
    present, leaving the helper's boundary read to serve an operator
    running against another credential.
    """
    return service_kernel.database_credentials(session, logger=logger)


def _preflight_schema(conn):
    """Assert the deployed migration state satisfies this build (rule 18).

    A thin wrapper over `pipeline.intent.schema_contract`, kept here rather
    than calling the checker inline for the same reason `_database_endpoint`
    is a wrapper: this module's own test suite patches at this seam.

    The executor is built over the service's own connection — the check is
    one read-only SELECT on `schema_migrations`, so it costs a round trip
    and needs no privileges beyond what the service already holds.
    """
    from database.modules.utils.rapid_db_connect import ConnectionExecutor
    from pipeline.intent.application_contract import (
        verify_application_contract)
    from pipeline.intent.schema_contract import verify_schema_contract

    verified = verify_schema_contract(ConnectionExecutor(conn).execute)
    logger.info("schema preflight passed: %s required migrations present",
                verified)

    # THE APPLICATION HALF (rule 18: the "application/schema contract" is
    # two halves). `require_image_digest` stays TRUE here, unlike
    # `rapidctl`'s call — that relaxation is for an operator tool run from a
    # shell, which has no container digest to know. The reconciler is a
    # deployed SERVICE whose unit genuinely supplies both variables:
    # `rapid-reconciler-service.yaml` passes `-e RAPID_IMAGE_DIGEST` and
    # `-e RAPID_RELEASE_IDENTITY` into the container (CR-R1 landed; an
    # earlier revision of this comment — and of the publisher's docstring,
    # corrected the same day — claimed otherwise from a stale reading of a
    # unit file that no longer exists). So this check passes on a correct
    # deployment and refuses a misdeployment by naming the missing variable.
    #
    # The executor is the same one-callable executor the schema half took,
    # so the registration check runs on this preflight connection and costs
    # one further read-only SELECT.
    verify_application_contract(ConnectionExecutor(conn).execute)
    return verified


def _carry_health_counters(previous, service):
    """Move the running health tally onto a freshly rebuilt service.

    `_poll_cycle` below opens a fresh connection and a fresh
    `ReconcilerService` every poll (the per-connection-open credential fetch
    the kernel contract requires — see this module's docstring). The health
    counters `run_forever` reads (`consecutive_poll_failures`,
    `consecutive_unproductive_polls`, and the informational ones `health()`
    reports) live on the SERVICE instance, not the process, so rebuilding it
    from scratch each poll would silently reset them every cycle — a
    reconciler that failed every one of its last four polls would look
    freshly healthy on poll five, and `POLL_FAILURE_THRESHOLD`/
    `CLOSURE_FAILURE_POLL_THRESHOLD` would never be reached no matter how
    persistent the fault. `previous` is `None` on the very first poll.
    """
    if previous is None:
        return service
    service.consecutive_poll_failures = previous.consecutive_poll_failures
    service.consecutive_unproductive_polls = \
        previous.consecutive_unproductive_polls
    service._closure_failures = previous._closure_failures
    service._binding_drift = previous._binding_drift
    service._missing_bundles = previous._missing_bundles
    service._reconstructed_bundles = previous._reconstructed_bundles
    return service


def _poll_cycle(session, parameters, previous_service, poll_seconds):
    """One poll: fresh connection, fresh credentials, one `run_forever` pass.

    Delegates the actual cycle to `service.run_forever` UNCHANGED — that
    function's health-threshold logic is shared and tested
    (`pipeline/reconciler/test/test_service.py`) against a single
    long-lived `service` argument, so this does not reshape it: a fresh
    `ReconcilerService` is built here and handed to `run_forever` for
    exactly one iteration (`should_continue` stops it after the first call).

    Opening THIS poll's connection can itself fail — a network blip, a
    credential briefly rejected — and that failure is deliberately let
    PROPAGATE out of this function rather than absorbed here: `main`'s own
    loop (below) counts it, exactly mirroring `run_forever`'s own
    consecutive-failure counting but at the connect step, which is now
    genuinely outside `run_forever`'s reach (it used to be inside — a query
    against an already-broken long-lived connection failed inside
    `poll_once` and was counted there — so this preserves the same
    "transient connection trouble is retried in-process, not treated as a
    start failure" behavior at its new location).
    """
    from database.modules.utils.rapid_db_connect import connection

    endpoint = _database_endpoint(parameters)
    # FRESH EVERY POLL (kernel contract: `service_kernel.database_credentials`
    # — "call this immediately before each connection open, not once at
    # startup"). Resolved here, inside the per-poll cycle, rather than once
    # in `main` and closed over — see this module's docstring for the
    # incident this replaces (the reconciler previously matched it).
    credentials = _database_credentials(session)

    with connection("rapid-reconciler", lane="transaction",
                    endpoint=endpoint, credentials=credentials) as conn:
        service = build_service(session, parameters, conn,
                                poll_seconds=poll_seconds)
        _carry_health_counters(previous_service, service)

        ran = {"once": False}

        def _stop_after_one():
            first_call = not ran["once"]
            ran["once"] = True
            return first_call

        run_forever(service, poll_seconds=0, sleep=lambda _seconds: None,
                    should_continue=_stop_after_one)
    return service


def main():
    _configure_logging()

    role_arn = os.environ.get("RAPID_RECONCILER_ROLE_ARN")
    poll_seconds = int(os.environ.get("RAPID_RECONCILER_POLL_SECONDS",
                                      POLL_SECONDS))

    running = service_kernel.install_stop_signal(logger)

    try:
        from database.modules.utils.rapid_db_connect import connection
        from submission.startup import fetch_parameters

        # Inside the try so a missing region exits 70 with the journal line
        # the operator needs, rather than an unhandled traceback.
        region = resolve_region()
        session = _assumed_session(role_arn, region)
        parameters = fetch_parameters(client=session.client("ssm"))
        logger.info("reconciler starting: poll=%ss records=%s diagnostics=%s",
                    poll_seconds, parameters["s3/records-bucket"],
                    parameters["s3/diagnostics-bucket"])

        # THE SCHEMA PREFLIGHT (rule 18), fail-closed, before the first poll.
        # A one-time startup gate, on its own short connection — mirroring
        # the operator's `_verify_work_streams`, which is the same kind of
        # gate for the same reason: the check's entire value is refusing to
        # START against a schema this build's SQL does not fit, and it needs
        # to run once before polling begins, not on every reconnect. Its own
        # connection is fine to hold only for this one preflight query and
        # discard — the per-poll rule this module's docstring describes
        # governs the polling connections below, not this startup gate.
        with connection("rapid-reconciler-preflight", lane="transaction",
                        endpoint=_database_endpoint(parameters),
                        credentials=_database_credentials(session)) as conn:
            _preflight_schema(conn)

        service = None
        # Consecutive failures to even OPEN this poll's connection — distinct
        # from `service.consecutive_poll_failures`, which counts failures of
        # an already-open `poll_once` and is carried on `service` itself
        # (`_carry_health_counters`). A connect failure happens before any
        # service exists to carry a counter, so `main` tracks this one, at
        # the same threshold and with the same verdict `run_forever` reaches
        # for its own counter — a connect failure is exactly the "stale
        # connection, rotated credential" case `run_forever`'s docstring
        # already names as retried in-process up to that threshold.
        consecutive_connect_failures = 0
        while running["go"]:
            started = time.monotonic()
            try:
                service = _poll_cycle(session, parameters, service,
                                      poll_seconds)
            except ReconcilerUnhealthy:
                raise
            except Exception:  # noqa: BLE001 - retried, but counted
                consecutive_connect_failures += 1
                logger.exception(
                    "reconciler could not open this poll's connection "
                    "(%d consecutive, threshold %d)",
                    consecutive_connect_failures, POLL_FAILURE_THRESHOLD)
                if consecutive_connect_failures >= POLL_FAILURE_THRESHOLD:
                    raise ReconcilerUnhealthy(
                        f"{consecutive_connect_failures} consecutive "
                        f"failures opening a poll connection (threshold "
                        f"{POLL_FAILURE_THRESHOLD}); exiting so the "
                        f"supervisor restarts it.") from None
            else:
                if consecutive_connect_failures:
                    logger.info(
                        "reconciler reconnected after %d failed attempt(s)",
                        consecutive_connect_failures)
                consecutive_connect_failures = 0
            elapsed = time.monotonic() - started
            remaining = poll_seconds - elapsed
            if running["go"] and remaining > 0:
                time.sleep(remaining)
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
