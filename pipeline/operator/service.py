"""The VPO's service entry point: `python3 -m pipeline.operator.service`.

What the systemd unit runs, and deliberately the same shape as
`pipeline.reconciler.main` — that unit is the house pattern and this
service has the same obligations: chain into the orchestrator role, read
its configuration from the parameter tree, pass the endpoint and
credential to the connection helper rather than exporting them, exit on
a signal, and exit distinguishably when it cannot do its work so the
supervisor restarts it.

Exit codes, matching the reconciler's vocabulary:

* 0  — stopped cleanly on a signal
* 70 — could not start
* 71 — was working and stopped being able to (health)
* 66 — a pass registered partially; the service keeps running, this is
       reported for a one-shot invocation only

REHEARSAL. `--rehearse` builds a `RehearsalSubmitter` — an object with
no Batch client and no route to the submission seam — instead of a
`LiveSubmitter`. The mode is decided HERE, once, and what a rehearsal
cannot do it cannot do because it does not hold the capability, not
because a flag is checked later. `RAPID_VPO_REHEARSE` is accepted as the
environment form (plumbing whose default is production, which the
environment policy allows in those words), and both converge on the same
one decision.

The old `RAPID_VPO_DRY_RUN` is REFUSED rather than honoured or ignored:
a run that sets it is asking for the semantics that submitted 5,057 real
children, and the safe answer to that request is to refuse to start.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time

from pipeline.operator import classes as opclasses
from pipeline.operator import inputs as opinputs
from pipeline.operator.operator import Operator, build_accumulator_cadence
from pipeline.operator.submitters import LiveSubmitter, RehearsalSubmitter
from pipeline.runtime.environment import resolve_region

logger = logging.getLogger("rapid.operator.service")

EXIT_START_FAILED = 70
EXIT_UNHEALTHY = 71
EXIT_PARTIAL_REGISTRATION = 66

#: Poll cadence for the service loop. The accumulator's age trigger is
#: what bounds submission latency; this only bounds how often the
#: operator looks for new work, and must be well under it.
DEFAULT_POLL_SECONDS = 15

#: Consecutive failing polls after which the service exits for restart.
#: The reconciler's threshold and the same reasoning: a stale connection
#: or a rotated credential is re-established by a fresh process.
POLL_FAILURE_THRESHOLD = 5


class OperatorUnhealthy(RuntimeError):
    """The service is running but cannot do its work."""


def _configure_logging():
    logging.basicConfig(
        level=os.environ.get("RAPID_LOG_LEVEL", "INFO"),
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        stream=sys.stdout)


def _refuse_retired_flag():
    """The old rehearsal flag is a hazard, not a synonym.

    `RAPID_VPO_DRY_RUN` suppressed registration writes and left
    submission running. Anything still setting it believes it is asking
    for a rehearsal and would get a live submission run, which is the
    2026-08-07 exhibit exactly. Refusing to start is the only response
    that cannot be misread.
    """
    if os.environ.get("RAPID_VPO_DRY_RUN"):
        raise RuntimeError(
            "RAPID_VPO_DRY_RUN is set and is RETIRED. It suppressed only "
            "registration writes while submission ran unguarded — the run "
            "that submitted 5,057 real children while reporting itself a "
            "dry run (2026-08-07). Use --rehearse (or RAPID_VPO_REHEARSE=1), "
            "which selects a submitter that holds no Batch client at all.")


def _rehearsal_requested(args):
    """One decision, from either surface."""
    if getattr(args, "rehearse", False):
        return True
    return os.environ.get("RAPID_VPO_REHEARSE", "").lower() in (
        "1", "true", "yes")


def _assumed_session(role_arn, region):
    """A boto3 session under the orchestrator role, or the ambient one.

    The service chains into its own role so every submission is
    attributable to `rapid-orchestrator-role` in CloudTrail rather than
    to whatever else runs on this host. Same as the reconciler.
    """
    import boto3

    if not role_arn:
        return boto3.Session(region_name=region)

    sts = boto3.client("sts", region_name=region)
    assumed = sts.assume_role(RoleArn=role_arn,
                              RoleSessionName="rapid-vpo")
    credentials = assumed["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region)


_DB_ENDPOINT_PARAMETERS = (
    ("db/server", "DBSERVER"),
    ("db/port", "DBPORT"),
    ("db/name", "DBNAME"),
)


def _database_endpoint(parameters):
    """The endpoint to pass to `connect`: the ENVIRONMENT over the tree.

    Per field, exactly as the reconciler resolves it — an operator
    debugging against a replica sets DBSERVER in the unit and restarts,
    and a tree-first order would silently connect them to production.
    """
    from database.modules.utils.rapid_db_connect import Endpoint

    values = []
    for parameter, variable in _DB_ENDPOINT_PARAMETERS:
        value = os.environ.get(variable) or parameters.get(parameter)
        values.append(str(value) if value else None)
    return Endpoint(*values)


def _database_credentials(session):
    """Resolve the DB credential under the SERVICE role, not the host's.

    Returned and passed to `connect`, never written to `os.environ`: the
    password does not exist in this process's environment, so nothing it
    execs inherits it. Same as the reconciler, for the same reason.
    """
    secret_id = os.environ.get("RAPID_DB_SECRET_ID")
    if not secret_id or os.environ.get("DBUSER"):
        return None
    from database.modules.utils.rapid_db_connect import Credentials

    secret = session.client("secretsmanager").get_secret_value(
        SecretId=secret_id)
    credential = json.loads(secret["SecretString"])
    logger.info("database credential resolved under the service role from %s",
                secret_id)
    return Credentials(credential["username"], credential["password"])


def build_submission_context(session, parameters, operational_class):
    """Clients, buckets and the binding for one class's submissions.

    Route-homogeneous by construction: the queue and definition come from
    THIS class's route, so a context cannot describe two routes and a
    batch cut against it is one job type, one queue, one definition.
    """
    from pipeline.virtualPipelineOperator import active_definition

    route = operational_class.route
    batch_client = session.client("batch")
    definition = parameters[route.definition_parameter]
    return {
        "queue": parameters[route.queue_parameter],
        "job_definition": definition,
        "binding": active_definition(batch_client,
                                     str(definition).split(":", 1)[0]),
        "manifest_bucket": parameters["s3/products-bucket"],
        "manifest_prefix": parameters["s3/manifest-prefix"],
        "s3_client": session.client("s3"),
        "batch_client": batch_client,
    }


def run_forever(operators, poll_seconds, should_continue,
                sleep=time.sleep,
                failure_threshold=POLL_FAILURE_THRESHOLD):
    """Poll until told to stop, or until the service cannot work.

    A single failed poll never exits — the conditions the operator has to
    survive (a database blip, a throttled API) are exactly the ones that
    make one throw. Consecutive failures past the threshold are different:
    the process is alive and the service is not working, and saying so by
    exiting is what lets the supervisor act.
    """
    consecutive_failures = 0
    while should_continue():
        started = time.monotonic()
        try:
            for operator in operators:
                result = operator.run_pass()
                logger.info("pass: %s", result.as_dict())
        except Exception:  # noqa: BLE001 - retried, but counted
            consecutive_failures += 1
            logger.exception(
                "operator poll failed (%d consecutive, threshold %d)",
                consecutive_failures, failure_threshold)
            if consecutive_failures >= failure_threshold:
                raise OperatorUnhealthy(
                    f"{consecutive_failures} consecutive poll failures "
                    f"(threshold {failure_threshold}); the operator is "
                    f"running but operating nothing. Exiting so the "
                    f"supervisor restarts it.")
        else:
            if consecutive_failures:
                logger.info("operator recovered after %d failed poll(s)",
                            consecutive_failures)
            consecutive_failures = 0

        elapsed = time.monotonic() - started
        remaining = poll_seconds - elapsed
        if remaining > 0 and should_continue():
            sleep(remaining)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="pipeline.operator.service",
        description="The Virtual Pipeline Operator, as a supervised service.")
    opinputs.add_arguments(parser)
    parser.add_argument(
        "--rehearse", action="store_true",
        help="run a full pass that CANNOT submit: the rehearsal submitter "
             "holds no Batch client and has no path to the submission seam")
    parser.add_argument(
        "--once", action="store_true",
        help="run one pass and exit, rather than polling")
    parser.add_argument(
        "--poll-seconds", type=int,
        default=int(os.environ.get("RAPID_VPO_POLL_SECONDS",
                                   DEFAULT_POLL_SECONDS)))
    parser.add_argument(
        "--force-cut", action="store_true",
        help="cut whatever has accumulated without waiting for the cadence "
             "triggers; for a bounded probe")
    return parser.parse_args(argv)


def main(argv=None):
    _configure_logging()
    args = parse_args(argv)

    running = {"go": True}

    def stop(signum, _frame):
        logger.info("caught signal %s; finishing the current pass", signum)
        running["go"] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        _refuse_retired_flag()
        operator_input = opinputs.from_namespace(args)
        rehearsing = _rehearsal_requested(args)

        region = resolve_region()
        role_arn = os.environ.get("RAPID_ORCHESTRATOR_ROLE_ARN")
        session = _assumed_session(role_arn, region)

        from database.modules.utils.rapid_db_connect import connection
        from submission.startup import fetch_parameters

        parameters = fetch_parameters(client=session.client("ssm"))
        max_batch_size, max_wait_seconds = build_accumulator_cadence(parameters)
        endpoint = _database_endpoint(parameters)
        credentials = _database_credentials(session)

        logger.info(
            "VPO starting: mode=%s window=%s..%s dispositions=%s "
            "cadence=(%s units, %ss)",
            "REHEARSAL" if rehearsing else "live",
            operator_input.start.isoformat(), operator_input.end.isoformat(),
            json.dumps(operator_input.dispositions),
            max_batch_size, max_wait_seconds)

        if rehearsing:
            logger.info(
                "REHEARSAL MODE: this process holds no submitting capability. "
                "It cuts batches and reports them; no Batch client is built.")

        to_run = operator_input.to_run
        if not to_run and args.once:
            # A one-shot invocation with nothing to run has done its job.
            logger.info("no class has disposition 'run'; nothing to do")
            return 0
        if not to_run:
            # A SERVICE with nothing to run IDLES; it does not exit.
            #
            # Found live on rapid-admin, 2026-08-08, on the first enabled
            # deploy: both classes were deployed on `hold`, this path
            # returned 0, and systemd's Restart=always turned "nothing to
            # do" into a restart loop — start, exit 0, restart 15 s later,
            # forever, at a restart counter climbing on every pass.
            #
            # Exiting 0 is right for `--once` and wrong for a supervised
            # service: `hold` on every class is a legitimate and expected
            # operating state (it is this stack's own default), and a
            # service must sit in it quietly rather than flapping. The
            # supervisor's restart is for a service that CANNOT work, not
            # one with nothing to do — and a unit that restarts every 15
            # seconds while nominal destroys the signal that restart
            # frequency is supposed to carry (operations.md: a health
            # check "must be quiet under nominal operation — its trigger
            # rate is part of its correctness").
            #
            # Idling rather than refusing to start, because the dispositions
            # are deploy parameters: an operator flipping one to `run` and
            # re-converging should find a service already up.
            logger.info(
                "no class has disposition 'run'; the operator is idle. "
                "Dispositions are %s — flip one to 'run' and re-converge "
                "to give it work.",
                json.dumps(operator_input.dispositions))
            while running["go"]:
                time.sleep(min(args.poll_seconds, 60))
            logger.info("operator stopped cleanly while idle")
            return 0

        operators = []
        for operational_class in to_run:
            gather = _gatherer(session, parameters, operational_class,
                               operator_input, endpoint, credentials)
            if rehearsing:
                submitter = RehearsalSubmitter()
            else:
                context = build_submission_context(session, parameters,
                                                   operational_class)
                submitter = LiveSubmitter(
                    context,
                    _execute_factory(endpoint, credentials),
                    max_batch_size=max_batch_size)
            operators.append(Operator(
                operational_class, submitter, gather,
                max_batch_size=max_batch_size,
                max_wait_seconds=max_wait_seconds,
                connection_factory=_connection_factory(
                    endpoint, credentials) if not rehearsing else None,
                registrar_factory=None))

        if args.once:
            worst = 0
            for operator in operators:
                result = operator.run_pass(force_cut=args.force_cut)
                logger.info("pass: %s", json.dumps(result.as_dict()))
                if result.registration:
                    worst = max(worst, result.registration.exit_code)
            if rehearsing:
                total = sum(op.submitter.would_submit_units
                            for op in operators)
                logger.info("REHEARSAL complete: %d unit(s) would have been "
                            "submitted; 0 were", total)
            return worst

        run_forever(operators, poll_seconds=args.poll_seconds,
                    should_continue=lambda: running["go"])

    except OperatorUnhealthy:
        logger.exception("the operator is unhealthy and is exiting so the "
                         "supervisor restarts it")
        return EXIT_UNHEALTHY
    except Exception:  # noqa: BLE001 - a start failure is worth exiting for
        logger.exception("the operator could not start")
        return EXIT_START_FAILED

    logger.info("operator stopped cleanly")
    return 0


def _connection_factory(endpoint, credentials):
    """A callable returning a fresh connection context manager."""
    from database.modules.utils.rapid_db_connect import connection

    def factory():
        return connection("rapid-vpo", lane="transaction",
                          endpoint=endpoint, credentials=credentials)
    return factory


def _execute_factory(endpoint, credentials):
    """A context manager yielding the submission `execute` callable."""
    import contextlib

    from database.modules.utils.rapid_db_connect import (ConnectionExecutor,
                                                         connection)

    @contextlib.contextmanager
    def factory():
        with connection("rapid-vpo-submit", lane="transaction",
                        endpoint=endpoint, credentials=credentials) as conn:
            yield ConnectionExecutor(conn).execute
    return factory


def _gatherer(session, parameters, operational_class, operator_input,
              endpoint, credentials):
    """The ready-work query for one class, bound to this invocation's window.

    Delegates to `pipeline.operator.gathering`, which carries the operator's
    own copies of `mjd_window` and `min_images_to_coadd`. Those used to be
    imported from `pipeline.virtualPipelineOperator` — and importing that
    module ran the old operator's startup, which read this process's argv
    and exited 64 demanding STARTDATETIME. See that module's header.
    """
    from pipeline.operator.gathering import gatherer_for

    return gatherer_for(
        operational_class, operator_input, parameters,
        connection_factory=_gather_connection_factory(endpoint, credentials),
        s3_client=session.client("s3"))


def _gather_connection_factory(endpoint, credentials):
    from database.modules.utils.rapid_db_connect import connection

    def factory():
        return connection("rapid-vpo-gather", lane="transaction",
                          endpoint=endpoint, credentials=credentials)
    return factory


if __name__ == "__main__":
    sys.exit(main())
