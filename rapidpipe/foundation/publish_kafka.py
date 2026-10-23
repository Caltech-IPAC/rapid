"""Official packet sets onto the Kafka topic: ``rapidpipe publish --run <id>``.

The publisher that writes rows is :mod:`rapidpipe.foundation.publish`; this
module is the transport. For every packet job of the run whose packet set
is official (an open ``packets`` binding) and whose
``jobs.kafka_published_at`` is null, it reads each packet's Avro bytes from
the outbox (the container the ``packets`` index names, through
:func:`rapidpipe.foundation.prefix.get`) and produces one message per
packet:

- key ``<packet set job id>:<sid>``, the foundation's packet identity;
- value the container's header (its schema included) followed by the
  packet's one block, exactly the bytes ``scripts/read_packets.py``
  decodes for that index row: a one-record Avro object container, no
  registry.

One pass opens one database connection and produces every unstamped set's
messages, then flushes the producer once; a set is stamped
(``kafka_published_at``) only when every one of its messages was
acknowledged. A set with a failed acknowledgement keeps a null stamp while
the others are stamped, and the pass then raises, so the verb exits
non-zero and the next ``publish`` sends that set again. A crash between
the acknowledgements and the stamps likewise replays the sets on the next
run; that is allowed (an internal topic, and consumers key on the
identity), and nothing here tries to be exactly-once.

Settings: the ``[kafka]`` table of a settings file (``--settings``, by
default the shipped ``rapidpipe/settings/foundation-thin.toml``) names the
SSM parameters that hold the bootstrap servers, the topic and the maximum
request size, never their values. The cluster's auth is IAM
(``SASL_SSL``/``OAUTHBEARER`` with ``aws_msk_iam_sasl_signer``) under the
caller's AWS credentials and region.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any, Callable

import fastavro
import psycopg2.extras

from rapidpipe.foundation.prefix import get

logger = logging.getLogger(__name__)

#: Seconds to wait for one message's acknowledgement after the pass's flush.
ACK_TIMEOUT_S = 120


class PublishError(RuntimeError):
    """A pass left one or more sets unstamped after a failed acknowledgement."""


def _query(conn, sql: str, params=()) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def unpublished_sets(conn, run: int) -> list[int]:
    """The run's official packet sets not yet stamped, by job id."""
    return [r["job"] for r in _query(conn, """
        SELECT j.id AS job FROM jobs j
        JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = j.id
        WHERE j.run = %s AND j.stage = 'packet' AND j.kafka_published_at IS NULL
        ORDER BY j.id""", (run,))]


def set_index(conn, job: int) -> list[dict]:
    """One packet set's index rows: ``sid``, ``filename``, ``byte_offset``, ``byte_length``."""
    return _query(conn, "SELECT sid, filename, byte_offset, byte_length FROM packets"
                        " WHERE job = %s ORDER BY sid", (job,))


def stamp(conn, job: int) -> None:
    """Record that every packet of ``job``'s set was acknowledged by the topic."""
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET kafka_published_at = now() WHERE id = %s", (job,))
    conn.commit()


def messages(job: int, index: list[dict]) -> list[tuple[bytes, bytes]]:
    """``(key, value)`` for every packet of a set, in the index's order."""
    containers: dict[str, tuple[bytes, int]] = {}
    out = []
    for row in index:
        if row["filename"] not in containers:
            data = get(row["filename"]).read_bytes()
            containers[row["filename"]] = (data, next(fastavro.block_reader(io.BytesIO(data))).offset)
        data, header_end = containers[row["filename"]]
        start = row["byte_offset"]
        out.append((f"{job}:{row['sid']}".encode(),
                    data[:header_end] + data[start:start + row["byte_length"]]))
    return out


def publish(connect: Callable[[], Any], run: int, producer: Any, topic: str) -> dict[str, Any]:
    """Produce every unstamped official packet set of ``run`` to ``topic`` and flush once; stamp each
    set whose every message was acknowledged.

    ``connect()`` is a context manager yielding a connection, opened once for
    the pass (the unstamped sets, every set's index and the stamps);
    ``producer`` has kafka-python's ``send(topic, key=, value=)`` returning a
    future and ``flush()``. Returns the sets stamped, packets, bytes, the
    largest message and the wall time. A set with a failed acknowledgement is
    left unstamped; the others are stamped, then :class:`PublishError` names
    the unstamped sets.
    """
    started = time.monotonic()
    sets, failed, packets, total, largest = [], {}, 0, 0, 0
    with connect() as conn:
        produced = []
        for job in unpublished_sets(conn, run):
            batch = messages(job, set_index(conn, job))
            produced.append((job, batch, [producer.send(topic, key=key, value=value) for key, value in batch]))
        if produced:
            producer.flush()
        for job, batch, futures in produced:
            try:
                for future in futures:
                    future.get(timeout=ACK_TIMEOUT_S)
            except Exception as error:  # noqa: BLE001 - any failed acknowledgement leaves the set unstamped
                failed[job] = error
                logger.error("packet set %d: an acknowledgement failed (%s); left unstamped", job, error)
                continue
            stamp(conn, job)
            sizes = [len(value) for _, value in batch]
            logger.info("packet set %d: %d packets, %d bytes produced and stamped", job, len(batch), sum(sizes))
            sets.append(job)
            packets += len(batch)
            total += sum(sizes)
            largest = max([largest, *sizes])
    if failed:
        raise PublishError(f"run {run}: packet sets {sorted(failed)} left unstamped after a failed"
                           f" acknowledgement ({next(iter(failed.values()))}); stamped {sets}")
    return {"run": run, "topic": topic, "sets": sets, "packets": packets, "bytes": total,
            "largest_message": largest, "seconds": round(time.monotonic() - started, 3)}


def endpoint(kafka_settings: dict, ssm: Any = None) -> dict[str, Any]:
    """The bootstrap servers, topic and maximum request bytes the ``[kafka]`` table's parameters hold."""
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
    """A kafka-python producer that waits for every in-sync replica."""
    from kafka import KafkaProducer
    return KafkaProducer(acks="all", max_request_size=max_request_bytes,
                         **client_config(bootstrap_servers))
