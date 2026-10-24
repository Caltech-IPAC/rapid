"""Official packet sets onto the Kafka topic: the packet job's last act, and ``rapidpipe publish``.

The publisher that writes rows is :mod:`rapidpipe.foundation.publish`; this
module is the transport, and :func:`deliver` is its one send boundary,
where the quality gate (:func:`rapidpipe.foundation.gate.decide`, the run's
``[gate]`` table) answers first: a held set is not sent by either path,
stays official and unstamped, and is sent by a later sweep whose answer is
send. A
packet job that its publisher has just loaded and bound sends its own set
there as its last act (``job run``, ``job local``); ``rapidpipe publish
--run <id>``, and the loop's per-pass publish step, call it for every
official packet set of the run (an open ``packets`` binding) whose
``jobs.kafka_published_at`` is null: the re-publication verb, and the
recovery sweep for a container that died between its database publish and
its acknowledgements, so the set is sent without rerunning its science.
The sweep leaves a set alone for :data:`SETTLE_S` after its job ended,
the container's own window to send it, so the two paths do not both send
one set; a set inside that window is reported as ``waiting``.

Each packet of a set is read from the outbox (the container the
``packets`` index names, through :func:`rapidpipe.foundation.prefix.get`)
and sent as one message:

- key the run's dataset, so a dataset's messages share one partition
  and keep their order there;
- headers ``rapid-set`` (the packet set's job id) and ``rapid-sid`` (the
  packet's ``sid``), the foundation's packet identity, as decimal ASCII;
- value the container's header (its schema included) followed by the
  packet's one block, exactly the bytes ``scripts/read_packets.py``
  decodes for that index row: a one-record Avro object container, no
  registry.

A set's messages are produced, the producer flushed, every acknowledgement
read, and only then is the set stamped: ``kafka_published_at`` with the
partition and the first and last offsets its acknowledgements gave
(``kafka_partition``, ``kafka_first_offset``, ``kafka_last_offset`` on the
set's job row), which ``scripts/consume_packets.py`` seeks to. Concurrent
sets of one dataset interleave in the partition, so the range's length is
not the set's count. No message waits in the producer behind another set
(2026-10-23: five loops' passes of thousands of packets each, queued
before one flush, outwaited the client's delivery timeout, and
kafka-python 3.0.11's expiry path then killed its sender thread and left
``flush()`` waiting for ever). A failed acknowledgement leaves the set's
stamp null; a flush that does not finish in :data:`ACK_TIMEOUT_S` stops a
``publish`` pass there; either way ``publish`` then raises, so the verb
exits non-zero and the next pass sends the unstamped sets again. A crash
between the acknowledgements and the stamp likewise replays the set, and
the stamp records the replay's offsets; that is allowed (an internal
topic, and consumers key on the identity), and nothing here tries to be
exactly-once.

Settings: the ``[kafka]`` table of a settings file (``--settings``, by
default the shipped ``rapidpipe/settings/foundation-thin.toml``; the run's
own body for a packet job) names the SSM parameters that hold the
bootstrap servers, the topic and the maximum request size, never their
values. The cluster's auth is IAM (``SASL_SSL``/``OAUTHBEARER`` with
``aws_msk_iam_sasl_signer``) under the caller's AWS credentials and region.
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any, Callable

import fastavro
import psycopg2.extras

from rapidpipe.foundation import gate
from rapidpipe.foundation.prefix import get

logger = logging.getLogger(__name__)

#: Seconds to wait for one set's flush, and then for each of its acknowledgements.
ACK_TIMEOUT_S = 120
#: Seconds after a packet job ended during which the sweep leaves its set to the job's own send:
#: the endpoint read and the connection, one flush and its acknowledgements, with a margin.
SETTLE_S = 2 * ACK_TIMEOUT_S + 60


class PublishError(RuntimeError):
    """A pass left one or more sets unstamped after a failed acknowledgement."""


class FlushStalled(RuntimeError):
    """The producer's flush did not finish: it is no longer draining."""


def _query(conn, sql: str, params=()) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


_UNSTAMPED = """
    SELECT j.id AS job, j.ended IS NOT NULL AND j.ended > now() - make_interval(secs => %s) AS settling
    FROM jobs j
    JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = j.id
    WHERE j.run = %s AND j.stage = 'packet' AND j.kafka_published_at IS NULL
    ORDER BY j.id"""


def unpublished_sets(conn, run: int, settle_s: float = SETTLE_S) -> list[int]:
    """The run's official packet sets not yet stamped whose job ended more than ``settle_s`` ago."""
    return [r["job"] for r in _query(conn, _UNSTAMPED, (settle_s, run)) if not r["settling"]]


def settling_sets(conn, run: int, settle_s: float = SETTLE_S) -> list[int]:
    """The run's official packet sets not yet stamped whose job ended within ``settle_s``."""
    return [r["job"] for r in _query(conn, _UNSTAMPED, (settle_s, run)) if r["settling"]]


def set_index(conn, job: int) -> list[dict]:
    """One packet set's index rows: ``sid``, ``filename``, ``byte_offset``, ``byte_length``."""
    return _query(conn, "SELECT sid, filename, byte_offset, byte_length FROM packets"
                        " WHERE job = %s ORDER BY sid", (job,))


def set_run(conn, job: int) -> dict:
    """``job``'s run's ``dataset`` (the messages' key) and ``[gate]`` table (``gate``, or ``None``)."""
    return _query(conn, "SELECT r.dataset, r.settings -> 'gate' AS gate FROM jobs j"
                        " JOIN runs r ON r.id = j.run WHERE j.id = %s", (job,))[0]


def stamp(conn, job: int, partition: int, first: int, last: int) -> None:
    """Record that every packet of ``job``'s set was acknowledged, in ``partition`` from ``first`` to ``last``."""
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET kafka_published_at = now(), kafka_partition = %s,"
                    " kafka_first_offset = %s, kafka_last_offset = %s WHERE id = %s",
                    (partition, first, last, job))
    conn.commit()


def headers(job: int, sid: int) -> list[tuple[str, bytes]]:
    """A packet message's identity headers."""
    return [("rapid-set", str(job).encode()), ("rapid-sid", str(sid).encode())]


def identity(message_headers) -> tuple[int, int] | None:
    """``(set, sid)`` from a message's headers, or ``None`` when it carries no identity."""
    found = dict(message_headers or ())
    try:
        return int(found["rapid-set"]), int(found["rapid-sid"])
    except (KeyError, ValueError):
        return None


def messages(index: list[dict]) -> list[tuple[int, bytes]]:
    """``(sid, value)`` for every packet of a set, in the index's order."""
    containers: dict[str, tuple[bytes, int]] = {}
    out = []
    for row in index:
        if row["filename"] not in containers:
            data = get(row["filename"]).read_bytes()
            containers[row["filename"]] = (data, next(fastavro.block_reader(io.BytesIO(data))).offset)
        data, header_end = containers[row["filename"]]
        start = row["byte_offset"]
        out.append((row["sid"], data[:header_end] + data[start:start + row["byte_length"]]))
    return out


def deliver(conn, job: int, producer: Any, topic: str) -> dict[str, Any]:
    """Unless the quality gate holds it, send packet set ``job``, flush, read every
    acknowledgement and stamp the set.

    Returns the set's ``packets``, ``bytes``, ``largest`` message,
    ``partition``, ``first`` and ``last`` offsets; for a held set, nothing
    sent, ``held`` with the gate's answer. A flush that does not
    finish raises :class:`FlushStalled`, and a failed acknowledgement its
    own error, with nothing stamped.
    """
    run = set_run(conn, job)
    answer = gate.decide(conn, job, run["gate"])
    if not answer["send"]:
        logger.warning("packet set %d: held by the quality gate (%s); not sent", job, answer["reason"])
        return {"job": job, "held": answer}
    dataset = run["dataset"].encode()
    batch = messages(set_index(conn, job))
    futures = [producer.send(topic, key=dataset, value=value, headers=headers(job, sid))
               for sid, value in batch]
    try:
        producer.flush(timeout=ACK_TIMEOUT_S)
    except Exception as error:  # noqa: BLE001 - whatever the client raised, it is not draining
        raise FlushStalled(f"packet set {job}: the flush did not finish ({error})") from error
    acks = [future.get(timeout=ACK_TIMEOUT_S) for future in futures]
    partitions = {ack.partition for ack in acks}
    if len(partitions) > 1:
        raise RuntimeError(f"packet set {job}: acknowledged in partitions {sorted(partitions)}, not one")
    sizes = [len(value) for _, value in batch]
    result = {"job": job, "packets": len(batch), "bytes": sum(sizes), "largest": max(sizes, default=0),
              "partition": next(iter(partitions), None),
              "first": min((a.offset for a in acks), default=None),
              "last": max((a.offset for a in acks), default=None)}
    stamp(conn, job, result["partition"], result["first"], result["last"])
    logger.info("packet set %d: %d packets, %d bytes acknowledged in partition %s offsets %s..%s; stamped",
                job, result["packets"], result["bytes"], result["partition"], result["first"], result["last"])
    return result


def publish(connect: Callable[[], Any], run: int, producer: Any, topic: str) -> dict[str, Any]:
    """Send every unstamped official packet set of ``run`` to ``topic`` through :func:`deliver`,
    one set at a time, except those inside their job's :data:`SETTLE_S`.

    ``connect()`` is a context manager yielding a connection, opened once for
    the pass; ``producer`` has kafka-python's ``send(topic, key=, value=,
    headers=)`` returning a future and ``flush(timeout=)``. Returns the sets
    stamped, packets, bytes, the largest message, the sets left ``waiting``
    in their settle window, the sets the quality gate ``held`` and the wall time. A set with a failed
    acknowledgement is left unstamped and the next set is tried; a flush
    that times out leaves its set and every later one unstamped; then
    :class:`PublishError` names the sets that failed.
    """
    started = time.monotonic()
    sets, held, failed, packets, total, largest = [], [], {}, 0, 0, 0
    with connect() as conn:
        waiting = settling_sets(conn, run)
        for job in unpublished_sets(conn, run):
            try:
                sent = deliver(conn, job, producer, topic)
            except FlushStalled as error:
                failed[job] = error
                logger.error("%s; the pass stops here", error)
                break
            except Exception as error:  # noqa: BLE001 - any failed acknowledgement leaves the set unstamped
                failed[job] = error
                logger.error("packet set %d: an acknowledgement failed (%s); left unstamped", job, error)
                continue
            if "held" in sent:
                held.append(job)
                continue
            sets.append(job)
            packets += sent["packets"]
            total += sent["bytes"]
            largest = max(largest, sent["largest"])
    if failed:
        raise PublishError(f"run {run}: packet sets {sorted(failed)} left unstamped after a failed"
                           f" acknowledgement ({next(iter(failed.values()))}); stamped {sets}")
    return {"run": run, "topic": topic, "sets": sets, "packets": packets, "bytes": total,
            "largest_message": largest, "waiting": waiting, "held": held,
            "seconds": round(time.monotonic() - started, 3)}


#: The job environment that stands for the ``[kafka]`` table's three SSM parameters, as a
#: launcher passes them (:func:`environment`) so a Batch job never reads SSM.
ENVIRONMENT = {"bootstrap_servers_parameter": "RAPIDPIPE_KAFKA_BOOTSTRAP_SERVERS",
               "topic_parameter": "RAPIDPIPE_KAFKA_TOPIC",
               "max_request_bytes_parameter": "RAPIDPIPE_KAFKA_MAX_REQUEST_BYTES"}


def environment(kafka_settings: dict, ssm: Any = None) -> dict[str, str]:
    """The ``[kafka]`` parameters' values as the environment :func:`endpoint` reads first."""
    found = endpoint(kafka_settings, ssm)
    return {ENVIRONMENT["bootstrap_servers_parameter"]: ",".join(found["bootstrap_servers"]),
            ENVIRONMENT["topic_parameter"]: found["topic"],
            ENVIRONMENT["max_request_bytes_parameter"]: str(found["max_request_bytes"])}


def endpoint(kafka_settings: dict, ssm: Any = None) -> dict[str, Any]:
    """The bootstrap servers, topic and maximum request bytes the ``[kafka]`` table's parameters hold.

    From the environment :data:`ENVIRONMENT` names when all three are set (a job its launcher
    passed them to), else from SSM.
    """
    if all(os.environ.get(name) for name in ENVIRONMENT.values()):
        return {"bootstrap_servers": os.environ[ENVIRONMENT["bootstrap_servers_parameter"]].split(","),
                "topic": os.environ[ENVIRONMENT["topic_parameter"]],
                "max_request_bytes": int(os.environ[ENVIRONMENT["max_request_bytes_parameter"]])}
    if ssm is None:
        import boto3
        ssm = boto3.client("ssm")

    def value(name: str) -> str:
        return ssm.get_parameter(Name=kafka_settings[name])["Parameter"]["Value"]

    return {"bootstrap_servers": value("bootstrap_servers_parameter").split(","),
            "topic": value("topic_parameter"),
            "max_request_bytes": int(value("max_request_bytes_parameter"))}


def client_config(bootstrap_servers: list[str]) -> dict[str, Any]:
    """kafka-python keyword arguments for the cluster's IAM auth, in this process's AWS region."""
    import boto3
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
    from kafka.net.sasl.oauth import AbstractTokenProvider

    region = boto3.session.Session().region_name

    class _Token(AbstractTokenProvider):
        def token(self):
            return MSKAuthTokenProvider.generate_auth_token(region)[0]

    return {"bootstrap_servers": bootstrap_servers, "security_protocol": "SASL_SSL",
            "sasl_mechanism": "OAUTHBEARER", "sasl_oauth_token_provider": _Token()}


def producer(bootstrap_servers: list[str], max_request_bytes: int):
    """A kafka-python producer that waits for every in-sync replica, not idempotent.

    kafka-python 3.0 is idempotent by default, and its producer-id request
    needs ``kafka-cluster:WriteDataIdempotently``, which the Batch job role's
    grant does not hold (2026-10-24, run 57: ``ClusterAuthorizationFailedError``
    on ``InitProducerIdRequest`` in every packet job). Nothing here is
    exactly-once, and a retried batch's duplicate is a replay consumers
    already tolerate, so the producer asks for ``WriteData`` alone.
    """
    from kafka import KafkaProducer
    return KafkaProducer(acks="all", enable_idempotence=False, max_request_size=max_request_bytes,
                         **client_config(bootstrap_servers))
