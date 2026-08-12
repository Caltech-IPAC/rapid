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
`Restart=always` and an operator's journal read the same way for all three.

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

from pipeline.runtime import service_kernel

logger = logging.getLogger("rapid.publisher")

#: Seconds between cycles when the last one found nothing to do.
#:
#: NOTHING-TO-DO IS AN IDLE, NEVER AN EXIT. An empty outbox is the ordinary
#: steady state between exposures, and a process that exited on it would be
#: restarted by systemd in a loop that reads as a crash loop in the journal.
POLL_SECONDS = 5

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


def run_forever(build_cycle, poll_seconds=POLL_SECONDS,
                should_continue=lambda: True, sleep=time.sleep):
    """Cycle until signalled. Returns the number of cycles run.

    `build_cycle` is a callable returning a fresh `PublisherCycle` — fresh
    because each cycle opens its own connection, which is what makes the
    per-connection credential fetch real (a rotated secret is picked up by the
    next cycle, with no restart).

    A CYCLE THAT RAISES DOES NOT KILL THE PROCESS. The outbox is durable and
    the claim has a lease, so a failed cycle costs at most one lease interval
    of latency on the rows it held. Exiting instead would turn a transient
    database blip into a restart, and the rows would wait exactly as long.
    """
    cycles = 0
    while should_continue():
        cycles += 1
        try:
            counts = build_cycle().run_once()
        except Exception:                                   # noqa: BLE001
            logger.exception(
                "publisher cycle failed; the claimed rows keep their lease "
                "and are reclaimed after it expires")
            sleep(poll_seconds)
            continue
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
        future = self.producer.transport.send(topic, wire_bytes, key=key)
        self.producer.transport.flush()
        if hasattr(future, "succeeded") and not future.succeeded():
            # The transport reported a definite outcome and it was a failure.
            # Classified as ambiguous unless the transport says otherwise:
            # kafka-python's error taxonomy does not cleanly separate "the
            # broker refused" from "the send did not complete", and the
            # conservative reading resends (consumers deduplicate) rather than
            # dropping a packet that may never have been rejected.
            raise RuntimeError(f"send did not succeed: {future.exception!r}")
        return _metadata(future)


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
