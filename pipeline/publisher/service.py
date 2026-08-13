"""
File:    service.py

The `rapid-publisher` process: the ONLY component that talks to the broker.

Rule 14's second half — "a separate publisher delivers them at-least-once with
identical bytes on resend" — and §2.3's "Controller and publisher". Before this
module the alert-production Batch job constructed a producer and sent in-job,
which meant delivery was inside the job's lifetime, a broker outage failed
science attempts, and a resend was whatever the next attempt happened to
serialize. After it, the job's delivery obligation ends at an outbox row and
this process owns the wire.

**SAME SHAPE AS THE OTHER TWO SUPERVISED SERVICES** (`pipeline/reconciler/
main.py`, `pipeline/operator/service.py`), through the same kernel: configure
logging, install the stop signal, resolve the parameter tree under an assumed
role, resolve the endpoint and the credential, preflight fail-closed, then loop
until signalled. Exit codes are the kernel's shared vocabulary so systemd's
`Restart=always` and an operator's journal read the same way for all three —
including `EXIT_UNHEALTHY`: a single failed cycle is retried in place, but
`CONSECUTIVE_FAILURE_THRESHOLD` in a row raises `PublisherUnhealthy` and exits
for a restart, the same bounded mechanism `pipeline/reconciler/service.py`
uses for the identical symptom (a process alive and doing nothing).

**IT CONNECTS DIRECTLY AS `rapid_publisher` — NEVER `SET ROLE`.** The publisher
is transaction-mode pooled (minimal viable target §2.2), and `SET ROLE` needs a
session lane: PgBouncer's transaction lane hands the underlying server
connection to whoever needs it next between statements, which would drop the
role somewhere mid-cycle and could leave it set on a connection handed to a
stranger. `pipeline/operatorctl/session.py` records this exact reasoning for
rapidctl — which is why rapidctl takes the SESSION lane and this cannot.

**FAIL-CLOSED WITHOUT CREDENTIALS** (B's pattern). No broker configuration, no
database endpoint, no schema: the process exits `EXIT_START_FAILED` naming what
was missing, rather than starting and discovering it per-packet. A publisher
that starts without a broker looks healthy while delivering nothing, which is
the 2026-08-04 Q7 finding's shape exactly.

**SINGLE-INSTANCE, BUT NOT BECAUSE IT HAS TO BE.** One instance is deployed
(§2.3), and the row-claim protocol in `outbox.py` is nevertheless active-active
safe: the claim is an atomic CAS with `SKIP LOCKED`, so a second instance takes
different rows rather than duplicating the first's. The single-instance
deployment is an operational choice; correctness does not rest on it.
"""

import logging
import os
import sys
import time

from pipeline.publisher.classification import classify
from pipeline.publisher.cycle import DefiniteRefusal
from pipeline.runtime import service_kernel

logger = logging.getLogger("rapid.publisher")

#: Seconds between cycles when the last one found nothing to do.
#:
#: NOTHING-TO-DO IS AN IDLE, NEVER AN EXIT. An empty outbox is the ordinary
#: steady state between exposures, and a process that exited on it would be
#: restarted by systemd in a loop that reads as a crash loop in the journal.
POLL_SECONDS = 5

#: Consecutive cycle failures after which the publisher reports itself
#: unhealthy and exits `EXIT_UNHEALTHY`, mirroring the reconciler's
#: `POLL_FAILURE_THRESHOLD` (`pipeline/reconciler/service.py`). 60 at the
#: publisher's 5s cycle period is five minutes — the same interval
#: `outbox.CLAIM_LEASE` already treats as the bound between a crash and its
#: rows becoming visibly stuck, so a dead connection or a rotated credential
#: surfaces to the supervisor on the same timescale an operator would
#: already notice the outbox itself.
CONSECUTIVE_FAILURE_THRESHOLD = 60

#: The application_name pooled connections carry, so `pg_stat_activity` and the
#: pooler both attribute this process's connections to it by name.
APPLICATION_NAME = "rapid-publisher"

#: The DIRECT role. Not a `SET ROLE` target — see the module docstring.
PUBLISHER_ROLE = "rapid_publisher"


def _preflight_schema(conn):
    """Refuse to start against a database with no outbox (rule 18's pattern).

    The reconciler's schema preflight refuses to start against a schema its SQL
    does not fit, for the reason its own comment gives: without it a missing
    migration surfaces as an UndefinedTable from whichever query runs first,
    hours later, attributed to that query rather than to the deployment. The
    publisher's whole subject is two tables, so their absence is exactly that
    kind of deployment fault.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema = 'public'"
            "   AND table_name IN ('alert_outbox', 'delivery_policies')")
        present = cur.fetchone()[0]
    if present != 2:
        raise RuntimeError(
            "the publisher's schema is not deployed: alert_outbox and "
            "delivery_policies are required and only %d of the 2 are present. "
            "DRAFT 050 is the migration that adds them" % present)
    logger.info("schema preflight passed: alert_outbox and delivery_policies "
                "are present")

    # THE APPLICATION HALF (rule 18). This module's schema check above is a
    # narrower probe than the other services' — the publisher's subject is two
    # tables, so it asks for those rather than running the migration contract —
    # but the application half is not narrower for anyone: a process that
    # cannot say which release it is cannot have its published alerts
    # attributed to a release, and the publisher is the component whose output
    # LEAVES the system. Fail-closed here, before the send loop is built.
    #
    # Called with the ConnectionExecutor the other four use rather than this
    # module's local `_executor`: that one COMMITS each statement, which is
    # right for the repository's short transactions and wrong for a read-only
    # startup probe.
    #
    # **THIS UNIT DOES NOT YET SUPPLY WHAT THIS CHECK READS** — see CR-R1 in
    # `notes-r-change-requests.md`. `rapid-alert-publication.yaml` sets only
    # `AWS_REGION` and `PYTHONUNBUFFERED`. Until that CR lands, deploying this
    # branch makes the publisher refuse to start, naming both missing
    # variables — a diagnosable start failure with a one-line fix, which is
    # the intended behaviour of a fail-closed check meeting an incomplete
    # deployment.
    from database.modules.utils.rapid_db_connect import ConnectionExecutor
    from pipeline.intent.application_contract import (
        verify_application_contract)

    identity = verify_application_contract(ConnectionExecutor(conn).execute)
    logger.info("application preflight passed: release %s",
                identity["release_identity"])


def _require_brokers(parameters):
    """The broker list, or a named start failure.

    THE PUBLISHER IS THE ONLY COMPONENT READING BROKER CONFIGURATION after E2.
    The brokers are a PARAMETER, never an environment variable read at import:
    the environment policy puts nothing that selects a destination in the
    environment, and a misread broker would publish to the wrong cluster
    silently — the same reasoning the alert-production job's own producer
    factory carried before this package removed it.
    """
    brokers = parameters.get("kafka/bootstrap-servers")
    if not brokers:
        raise RuntimeError(
            "the parameter tree does not carry kafka/bootstrap-servers; the "
            "publisher delivers to the broker and has no default. Refusing to "
            "start rather than idling as a healthy-looking process that "
            "delivers nothing")
    return brokers


def _topic_guard(prefixes):
    """The internal-topic guard, as a predicate over a row's stored topic.

    MOVED FROM THE ALERT-PRODUCTION JOB (brief E2). The guard's purpose is
    unchanged — the mission/public stream must not be reachable by
    reconfiguration, and the publication policy grants `rapid.internal.alerts.*`
    (plus `rapid.test.*`) and nothing else — but its LOCATION had to move with
    the send: after E the job has no destination to guard, and the publisher's
    per-row topic is the last point where the check still means something.
    """
    def allowed(topic):
        return bool(topic) and any(topic.startswith(p) for p in prefixes)
    return allowed


class PublisherUnhealthy(RuntimeError):
    """Consecutive cycles failed past the threshold: the publisher cannot work.

    Raised out of `run_forever` so the process EXITS and its supervisor
    restarts it — the same shape as the reconciler's `ReconcilerUnhealthy`
    (`pipeline/reconciler/main.py`). Without this, a publisher wedged against
    a dead connection or a rotated credential stays up, logging exceptions
    forever, while systemd sees a healthy process and `EXIT_UNHEALTHY` — which
    already exists in the shared kernel — is never reached.
    """


def run_forever(build_cycle, poll_seconds=POLL_SECONDS,
                should_continue=lambda: True, sleep=time.sleep,
                failure_threshold=CONSECUTIVE_FAILURE_THRESHOLD):
    """Cycle until signalled. Returns the number of cycles run.

    `build_cycle` is a callable returning a fresh `PublisherCycle` — fresh
    because each cycle opens its own connection, which is what makes the
    per-connection credential fetch real (a rotated secret is picked up by the
    next cycle, with no restart).

    A CYCLE THAT RAISES DOES NOT KILL THE PROCESS BY ITSELF. The outbox is
    durable and the claim has a lease, so a failed cycle costs at most one
    lease interval of latency on the rows it held. Exiting on the first
    failure would turn a transient database blip into a restart, and the rows
    would wait exactly as long.

    CONSECUTIVE FAILURES ARE DIFFERENT. Past `failure_threshold` in a row the
    publisher is not delivering anything and staying up says otherwise — the
    process looked alive while every packet queued behind a connection that
    was never coming back. `PublisherUnhealthy` is raised so the caller can
    exit `EXIT_UNHEALTHY` and let the supervisor restart into a fresh
    connection and a fresh credential fetch, exactly as the reconciler does
    for the same symptom.
    """
    cycles = 0
    consecutive_failures = 0
    while should_continue():
        cycles += 1
        try:
            counts = build_cycle().run_once()
        except Exception:                                   # noqa: BLE001
            consecutive_failures += 1
            logger.exception(
                "publisher cycle failed (%d consecutive, threshold %d); the "
                "claimed rows keep their lease and are reclaimed after it "
                "expires", consecutive_failures, failure_threshold)
            if consecutive_failures >= failure_threshold:
                raise PublisherUnhealthy(
                    f"{consecutive_failures} consecutive cycle failures "
                    f"(threshold {failure_threshold}); the publisher is "
                    f"running but delivering nothing. Exiting so the "
                    f"supervisor restarts it — a stale connection or a "
                    f"rotated credential is re-established by a fresh "
                    f"process, and staying up would mean no packet is sent "
                    f"for as long as this lasts.")
            sleep(poll_seconds)
            continue
        if consecutive_failures:
            logger.info("publisher recovered after %d failed cycle(s)",
                       consecutive_failures)
        consecutive_failures = 0
        if counts["claimed"] or counts["reclaimed"]:
            logger.info(
                "cycle: %d claimed, %d sent, %d resend, %d refused, %d held, "
                "%d reclaimed", counts["claimed"], counts["sent"],
                counts["resend"], counts["refused"], counts["held"],
                counts["reclaimed"])
        if not counts["sent"]:
            # IDLE, NOT EXIT. See POLL_SECONDS.
            sleep(poll_seconds)
    return cycles


def main():
    service_kernel.configure_logging()

    role_arn = os.environ.get("RAPID_PUBLISHER_ROLE_ARN")
    poll_seconds = int(os.environ.get("RAPID_PUBLISHER_POLL_SECONDS",
                                      POLL_SECONDS))
    running = service_kernel.install_stop_signal(logger)

    try:
        from database.modules.utils.rapid_db_connect import connection
        from pipeline.publisher.cycle import PublisherCycle
        from pipeline.publisher.outbox import OutboxRepository
        from submission.startup import fetch_parameters

        from pipeline.runtime.environment import resolve_region

        # Inside the try so a missing region exits EXIT_START_FAILED with the
        # journal line the operator needs, rather than an unhandled traceback
        # — the reconciler's own comment on the same line.
        region = resolve_region()
        session = service_kernel.assumed_session(role_arn, region,
                                                 APPLICATION_NAME)
        parameters = fetch_parameters(client=session.client("ssm"))
        brokers = _require_brokers(parameters)
        endpoint = service_kernel.database_endpoint(parameters)
        logger.info("publisher starting: poll=%ss brokers=%s",
                    poll_seconds, brokers)

        from alerts.kafka_producer import make_producer
        producer = make_producer(brokers)

        prefixes = tuple(
            (parameters.get("kafka/internal-topic-prefixes")
             or "rapid.internal.,rapid.test.").split(","))
        guard = _topic_guard(prefixes)

        # The preflight runs on its own connection, before the loop, so a
        # missing schema is a START failure rather than a per-cycle exception.
        credentials = service_kernel.database_credentials(session, logger)
        with connection(APPLICATION_NAME, lane="transaction",
                        endpoint=endpoint, credentials=credentials) as conn:
            _preflight_schema(conn)

        claim_token = f"{APPLICATION_NAME}-{os.getpid()}"

        def build_cycle():
            """A cycle over a FRESH connection and a FRESH credential."""
            fresh = service_kernel.database_credentials(session, logger)
            ctx = connection(APPLICATION_NAME, lane="transaction",
                             endpoint=endpoint, credentials=fresh)
            conn = ctx.__enter__()
            repository = OutboxRepository(_executor(conn))
            return _ClosingCycle(
                PublisherCycle(repository, _ProducerSender(producer),
                               claim_token, topic_guard=guard),
                ctx)

        run_forever(build_cycle, poll_seconds=poll_seconds,
                    should_continue=lambda: running["go"])
    except PublisherUnhealthy:
        # NOT a start failure. `PublisherUnhealthy` subclasses RuntimeError,
        # so the handler below would catch it and tell the journal "the
        # publisher could not start" about a process that had been running
        # and cycling for however long it took to exhaust the threshold —
        # the reconciler's own `ReconcilerUnhealthy` handling
        # (`pipeline/reconciler/main.py`) draws the same distinction, for the
        # same reason: the restart is right, but an operator reading the
        # journal deserves to know it was a running process going unhealthy,
        # not a process that never came up.
        logger.exception("the publisher is unhealthy and is exiting so the "
                         "supervisor restarts it")
        return service_kernel.EXIT_UNHEALTHY
    except Exception:                                       # noqa: BLE001
        logger.exception("the publisher could not start")
        return service_kernel.EXIT_START_FAILED

    logger.info("publisher stopped cleanly")
    return 0


def _executor(conn):
    """`execute(sql, params)` over a real cursor, committing each statement.

    Each of the repository's statements IS one short transaction (see
    `outbox.py`), so the commit belongs here rather than at a wrapping
    boundary: the claim must be visible to other publishers the moment it
    returns, and the finalization must survive the send that preceded it.
    """
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            result = cur.fetchall() if cur.description is not None \
                else cur.rowcount
        conn.commit()
        return result
    return execute


class _ClosingCycle:
    """A cycle that closes its connection when the cycle ends."""

    def __init__(self, cycle, context):
        self.cycle = cycle
        self.context = context

    def run_once(self):
        try:
            return self.cycle.run_once()
        finally:
            self.context.__exit__(None, None, None)


class _ProducerSender:
    """The production send path: frame-free, key-carrying, flushed per packet.

    The producer's own framing is BYPASSED deliberately — `GlueFramingProducer.
    produce` looks the registry up to frame, and the publisher must frame from
    the row's pinned schema version instead (see `cycle.py`). So this reaches
    the transport underneath it with bytes that are already framed.

    Flushed per packet rather than per batch: the outcome of THIS packet
    decides THIS row's finalization, and a batched flush would report a
    per-batch outcome the row-level state machine cannot use.
    """

    def __init__(self, producer):
        self.producer = producer

    def send(self, topic, wire_bytes, key=None):
        """Send one framed packet, classifying any failure before raising.

        THE CLASSIFICATION IS THE POINT OF THIS METHOD. The cycle distinguishes
        a definite refusal (terminal, `REFUSED`) from an ambiguous outcome
        (resend), and it does so by exception TYPE: `DefiniteRefusal` versus
        anything else. So this is the one place that can decide which a real
        broker error is — and until this method classified them, it raised a
        bare `RuntimeError` for every failure, which made `DefiniteRefusal`
        unreachable in production and left a message the broker would never
        accept retrying forever at the head of the queue.

        Both paths raise; the cycle catches. Nothing is swallowed here, because
        a send whose outcome this method cannot report is exactly the case the
        at-least-once contract exists for.
        """
        try:
            future = self.producer.transport.send(topic, wire_bytes, key=key)
            self.producer.transport.flush()
        except Exception as exc:                            # noqa: BLE001
            # A SYNCHRONOUS raise from the transport — kafka-python raises
            # `MessageSizeTooLargeError` from `send()` itself when the record
            # exceeds the configured `max_request_size`, without ever reaching
            # a broker, so the definite classes are not only found on futures.
            self._raise_classified(exc, topic)

        if hasattr(future, "succeeded") and not future.succeeded():
            # THE ASYNCHRONOUS path: the send was accepted for delivery and
            # its future resolved to a failure. `future.exception` is the
            # broker's own error, which is what carries the taxonomy.
            self._raise_classified(getattr(future, "exception", None), topic)
        return _metadata(future)

    def _raise_classified(self, error, topic):
        """Re-raise `error` as a definite refusal or as an ambiguous failure.

        `DefiniteRefusal` is the cycle's terminal signal; every other exception
        it sees is treated as ambiguous, so an ambiguous outcome is re-raised
        as itself (or wrapped, when there is nothing to re-raise) rather than
        being converted into a second vocabulary.
        """
        verdict, reason = classify(error)
        if verdict == "definite":
            logger.error(
                "packet for topic %s was DEFINITELY refused: %s", topic,
                reason)
            raise DefiniteRefusal(reason) from error
        logger.warning("send to topic %s failed ambiguously: %s", topic,
                       reason)
        if isinstance(error, BaseException):
            raise error
        raise RuntimeError(reason)


#: The production sender, under a name the tests can name.
#:
#: `_ProducerSender` stays private-by-convention because nothing outside this
#: module CONSTRUCTS one in production — `main` does, and that is the whole
#: intended use. But brief E's fix round requires the contract tests to drive
#: the REAL classification path rather than a re-implementation of it, and a
#: test reaching through an underscore is a test that will be "tidied" away by
#: someone who reasonably reads the underscore as "not part of the surface".
#: The alias says the classification path IS part of the surface, for the
#: tests, deliberately.
ProducerSender = _ProducerSender


def _metadata(future):
    """Whatever the transport reported about the acknowledgement.

    A RECORD, never control state: nothing reads it back to decide anything.
    Best-effort by design — a transport that reports nothing useful yields an
    empty record rather than failing a send that succeeded.
    """
    try:
        value = future.value if hasattr(future, "value") else None
        if value is None:
            return None
        return {"topic": getattr(value, "topic", None),
                "partition": getattr(value, "partition", None),
                "offset": getattr(value, "offset", None)}
    except Exception:                                       # noqa: BLE001
        return None


if __name__ == "__main__":
    sys.exit(main())
