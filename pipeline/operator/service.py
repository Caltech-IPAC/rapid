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

The service plumbing this module used to carry on its own — logging
setup, the assumed-role session, endpoint/credential resolution, the
70/71 exit codes — now lives in `pipeline.runtime.service_kernel`, shared
with the reconciler's `pipeline.reconciler.main`. Adopting it also fixes
the one place this service had drifted from the reconciler's proven
shape: `_assumed_session` used to build a boto3 session from the three
literal strings a one-shot `sts.assume_role` returns, which is exactly
the shape the reconciler's own docstring records as a fixed production
incident (`service_kernel.assumed_session`'s docstring has the full
account). This service now uses the same `DeferredRefreshableCredentials`
mechanism.
"""

import argparse
import json
import logging
import os
import sys
import time

from pipeline.operator import classes as opclasses
from pipeline.operator import inputs as opinputs
from pipeline.operator.operator import Operator, build_accumulator_cadence
from pipeline.operator.submitters import LiveSubmitter, RehearsalSubmitter
from pipeline.runtime import service_kernel
from pipeline.runtime.environment import resolve_region
from pipeline.runtime.service_kernel import EXIT_START_FAILED, EXIT_UNHEALTHY

logger = logging.getLogger("rapid.operator.service")

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


_configure_logging = service_kernel.configure_logging


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

    Delegates to `service_kernel.assumed_session`. This used to build a
    session from the three literal strings a one-shot `sts.assume_role`
    call returns — the shape that expires in an hour with no way to
    renew, and that the reconciler's own docstring records as the fixed
    ExpiredToken crashloop incident. The kernel's `DeferredRefreshableCredentials`
    mechanism replaces it here too, so the operator's session refreshes
    in-process the same way the reconciler's does.
    """
    return service_kernel.assumed_session(
        role_arn, region, role_session_name="rapid-vpo")


_DB_ENDPOINT_PARAMETERS = service_kernel.DB_ENDPOINT_PARAMETERS


def _database_endpoint(parameters):
    """The endpoint to pass to `connect`: the ENVIRONMENT over the tree.

    Delegates to `service_kernel.database_endpoint`, shared with the
    reconciler. Per field: an explicitly-set variable wins, and the tree
    fills what is absent — an operator debugging against a replica sets
    DBSERVER in the unit and restarts, and a tree-first order would
    silently connect them to production.
    """
    return service_kernel.database_endpoint(parameters)


def _database_credentials(session):
    """Resolve the DB credential under the SERVICE role, not the host's.

    Delegates to `service_kernel.database_credentials`, shared with the
    reconciler. Returned and passed to `connect`, never written to
    `os.environ`: the password does not exist in this process's
    environment, so nothing it execs inherits it.

    **Called fresh at each connection open** (`_connection_factory`,
    `_execute_factory`, `_gather_connection_factory` below each call this
    per invocation rather than once) — the per-connection-open secret
    fetch the security design requires (design/security.md, "Database
    service credentials"), which this service previously violated: a
    credential resolved once in `main()` was closed over by every
    connection factory for the process's whole lifetime, so a rotated
    secret was picked up only on the next restart rather than the next
    connection.
    """
    return service_kernel.database_credentials(session, logger=logger)


def build_submission_context(session, parameters, operational_class):
    """Clients, buckets and the binding for one class's submissions.

    DELEGATES to `pipeline.operator.submission.submission_env`, which
    already owns this: it resolves the route's queue and definition from
    the tree, resolves the definition FAMILY to its one ACTIVE revision,
    and builds a real `SubmissionBinding` carrying the revision, image
    digest and release identity that the attempt rows record.

    This function briefly reimplemented that, and reimplemented it wrong:
    it put `active_definition`'s raw dict where a `SubmissionBinding` was
    expected, and the first live probe died at
    `binding.job_definition_arn` with "'dict' object has no attribute".
    Rebuilding a contract that already exists is how the two drift; the
    binding is the execution record's backbone (a wrong revision makes the
    reconciler report drift on every attempt), so borrowing the one
    implementation is the point rather than a convenience.

    Route-homogeneous by construction, which is what `submission_env`
    gives us: the queue and definition come from THIS class's route, so a
    context cannot describe two routes.
    """
    from pipeline.operator.submission import submission_env

    return submission_env(
        operational_class.route.job_type,
        parameters=parameters,
        batch_client=session.client("batch"),
        s3_client=session.client("s3"))


def _classes_for_pass(operational_class):
    """Every registered job type this class gathers, as its own `OperationalClass`.

    Co-design ruling 1: "the registry enumerates that class's job types for
    gathering" — the class axis (`to_run`, `pipeline.operator.inputs`) still
    gates which of the four declared classes runs this pass; THIS is what a
    running class fans out to. Reference construction fans out to its one
    job type exactly as before; prompt processing fans out to eight (science
    plus the six post-DB job types plus alert production) — the complete
    operator-scheduled chain the ADOPTED operations text describes.

    Returns `OperationalClass` instances, one per job type, built with
    `dataclasses.replace` over the running class: every consumer downstream
    (`Operator`, `LiveSubmitter`, `RehearsalSubmitter`, `build_submission_context`)
    already reads only `.name`, `.job_type`, `.route` and
    `.require_implemented()` — the exact `OperationalClass` contract — so a
    per-job-type instance of that same frozen dataclass needs no new type.
    `.name` becomes the job type string, which is what distinguishes one
    job type's accumulator, logging and run-id prefix from another's within
    one running class.
    """
    import dataclasses

    from pipeline.operator.gathering import job_types_for_class

    job_types = job_types_for_class(operational_class.name)
    if not job_types:
        # A declared, implemented class the registry has no job types for
        # would silently gather nothing every pass — refused here rather
        # than producing an operator that runs and does nothing.
        raise RuntimeError(
            f"operational class {operational_class.name!r} is implemented "
            f"and asked to run, but the gatherer registry names no job "
            f"type for it (pipeline.operator.gathering.REGISTRY)")
    return tuple(
        operational_class if job_type == operational_class.job_type
        else dataclasses.replace(operational_class, name=job_type,
                                 job_type=job_type)
        for job_type in job_types)


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
    # The bounded-probe pair, deliberately the same shape as
    # scripts/q8_ramp_probe.py's --width/--max-width: a width alone can be
    # mistyped into a runaway, so the caller must ALSO state the ceiling it
    # believes it is under, and a width above that ceiling is refused
    # rather than obeyed. That guard is what made the drip's submissions
    # exactly attributable, and a bounded live probe through this operator
    # needs the same property.
    parser.add_argument(
        "--width", type=int, default=None,
        help="cap the gathered units to this many before they reach the "
             "accumulator; for a bounded live probe. Requires --max-width.")
    parser.add_argument(
        "--max-width", type=int, default=None,
        help="refuse to run if --width exceeds this; state it explicitly")
    return parser.parse_args(argv)


def _bounded(gather, width, max_width, class_name):
    """Wrap a gatherer so it yields at most `width` units.

    Drops are LOGGED with their count. A silent cap reads exactly like a
    complete run, which is the failure the drip harness was built to make
    impossible ("a silent cap reads exactly like a complete run",
    smoke_run.rst).
    """
    def bounded_gather():
        units = list(gather())
        if len(units) > width:
            logger.info(
                "%s: capping to --width %d; dropping %d gathered unit(s)",
                class_name, width, len(units) - width)
        return units[:width]
    return bounded_gather


def main(argv=None):
    _configure_logging()
    args = parse_args(argv)

    running = service_kernel.install_stop_signal(logger)

    try:
        _refuse_retired_flag()

        # The bounded-probe guard, checked BEFORE anything is gathered or
        # any client is built: a width above the stated ceiling is refused,
        # never clamped, and a width with no stated ceiling is refused too.
        # Clamping would submit *something* under a number the caller did
        # not choose, which is the class of accident this pair exists to
        # prevent.
        if args.width is not None:
            if args.max_width is None:
                raise RuntimeError(
                    "--width requires --max-width: state the ceiling you "
                    "believe you are under, so a mistyped width is refused "
                    "rather than obeyed.")
            if args.width > args.max_width:
                raise RuntimeError(
                    f"--width {args.width} exceeds the stated --max-width "
                    f"{args.max_width}; refusing to run.")
            if args.width < 1:
                raise RuntimeError(f"--width {args.width} must be at least 1")

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
        # No credential is resolved here: each connection factory below
        # fetches its own, fresh, at the moment it opens a connection —
        # see `_database_credentials`.

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

        # One `Operator` per JOB TYPE, not per class (co-design ruling 1):
        # `_classes_for_pass` fans each running class out to every job type
        # the registry declares for it, so prompt processing's one `to_run`
        # entry becomes eight operators sharing the class's window and
        # dispositions while each keeps its own accumulator and submission
        # context — exactly "one job type, one queue, one definition per
        # array submission" (operations.md, ADOPTED), now honoured per job
        # type rather than per class.
        #
        # REGISTRATION STAYS ONE PASS PER POLL, NOT ONE PER JOB TYPE.
        # `Operator._register()` runs `pipeline.operator.registration.run_pass`,
        # which is a GLOBAL sweep of every outstanding registration
        # candidate — it is not scoped to a job type or an accumulator, so
        # giving every one of eight job-type operators its own connection
        # factory would run the identical global pass eight times a poll.
        # Harmless (a candidate registered by the first pass is not a
        # candidate for the second), but it is not the design and it is
        # eight times the log volume and eight times the table scan for
        # nothing. So only the FIRST operator of each running class carries
        # a connection factory / registrar; `Operator._register()` returns
        # None (skipped) for the rest, which is its documented behaviour
        # for "a probe or a test that is only exercising submission".
        operators = []
        for operational_class in to_run:
            for position, job_class in enumerate(
                    _classes_for_pass(operational_class)):
                gather = _gatherer(session, parameters, job_class.job_type,
                                   operator_input, endpoint)
                if args.width is not None:
                    gather = _bounded(gather, args.width, args.max_width,
                                      job_class.name)
                if rehearsing:
                    submitter = RehearsalSubmitter()
                else:
                    context = build_submission_context(session, parameters,
                                                       job_class)
                    submitter = LiveSubmitter(
                        context,
                        _execute_factory(session, endpoint),
                        max_batch_size=max_batch_size)
                registers_this_class = position == 0
                operators.append(Operator(
                    job_class, submitter, gather,
                    max_batch_size=max_batch_size,
                    max_wait_seconds=max_wait_seconds,
                    connection_factory=(
                        _connection_factory(session, endpoint)
                        if not rehearsing and registers_this_class else None),
                    # A LIVE pass registers for real. Passing None here makes
                    # `run_registration` pass `dry_run=True`, and a dry run
                    # reports `would_register=N` while writing no rows — the
                    # exact "registration reported success and wrote nothing"
                    # defect this codebase already fixed once in the
                    # consumer. Seen live on the first successful width-2
                    # probe: `would_register: 1087, registered: 0`.
                    #
                    # A rehearsal keeps None, deliberately: it has no
                    # connection either, and a rehearsal that wrote
                    # registration rows would be a rehearsal with effects.
                    registrar_factory=(
                        None if rehearsing or not registers_this_class
                        else _production_registrar())))

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


def _production_registrar():
    """The real registration callback factory: connection -> callback.

    Delegates to `pipeline.operator.registrar.production_registrar`,
    which owns this: it builds the S3 record store once and binds the
    registrar to each pass's OWN connection, so product rows and the
    registration watermark commit in one transaction. Two connections
    cannot be one transaction — that was round-3 finding #8, and
    rebuilding this here would be the third place to get it wrong.

    That function returns None when RAPID_VPO_DRY_RUN is set; this
    service refuses to start with that variable set at all
    (`_refuse_retired_flag`), so it cannot be the source of a silent None
    here.
    """
    from pipeline.operator.registrar import production_registrar

    return production_registrar()


def _connection_factory(session, endpoint):
    """A callable returning a fresh connection context manager.

    Delegates to `service_kernel.connection_factory`, which fetches the DB
    credential INSIDE the returned callable, at each call — not once here
    and closed over. A credential resolved once in `main()` and shared by
    every connection this factory opens for the rest of the process's life
    is exactly the "cached across connections" shape the security design
    forbids; fetching fresh per call is what makes a rotated secret take
    effect on the next connection rather than only the next restart.
    """
    return service_kernel.connection_factory(
        session, endpoint, "rapid-vpo", lane="transaction")


def _execute_factory(session, endpoint):
    """A context manager yielding the submission `execute` callable.

    Same per-call credential fetch as `_connection_factory`: the secret is
    resolved fresh inside `factory()`, not closed over from `main()`.
    """
    import contextlib

    from database.modules.utils.rapid_db_connect import (ConnectionExecutor,
                                                         connection)

    @contextlib.contextmanager
    def factory():
        credentials = service_kernel.database_credentials(session)
        with connection("rapid-vpo-submit", lane="transaction",
                        endpoint=endpoint, credentials=credentials) as conn:
            yield ConnectionExecutor(conn).execute
    return factory


def _gatherer(session, parameters, job_type, operator_input, endpoint):
    """The ready-work query for one JOB TYPE, bound to this invocation's window.

    Delegates to `pipeline.operator.gathering`, which carries the operator's
    own copies of `mjd_window` and `min_images_to_coadd`. Those used to be
    imported from `pipeline.virtualPipelineOperator` — and importing that
    module ran the old operator's startup, which read this process's argv
    and exited 64 demanding STARTDATETIME. See that module's header.

    Takes a job type rather than an operational class (co-design ruling 1):
    `gathering.gatherer_for` is now a registry lookup keyed by job type, and
    one operational class can register several job types (prompt processing
    registers eight) — see `_operators_for_class` below, this call's caller.
    """
    from pipeline.operator.gathering import gatherer_for

    return gatherer_for(
        job_type, operator_input, parameters,
        connection_factory=_gather_connection_factory(session, endpoint),
        s3_client=session.client("s3"))


def _gather_connection_factory(session, endpoint):
    """Same per-call credential fetch as `_connection_factory`, for gathering."""
    return service_kernel.connection_factory(
        session, endpoint, "rapid-vpo-gather", lane="transaction")


if __name__ == "__main__":
    sys.exit(main())
