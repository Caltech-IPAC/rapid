"""Read a run's packets back from the Kafka topic and compare them with the outbox.

Outside the pipeline, like ``read_packets.py``: it never writes to the
database or the topic, and exits non-zero on any failure. It reads every
partition of the topic from its earliest offset to its end offset at
start, with no consumer group, keeps the messages whose key
``<job>:<sid>`` names one of the run's packet sets stamped
``kafka_published_at``, and for each kept message checks that:

- it decodes as a one-record Avro container whose record validates against
  the schema version its index row names (``read_packets.py``'s check);
- its bytes equal the outbox's for that key (the container header and the
  packet's block, :func:`rapidpipe.foundation.publish_kafka.messages`);

and that every packet of every stamped set was seen at least once. The
verdict is one line: messages read, kept, valid, byte-equal, packets
expected and seen, and the offset range read per partition. Zero kept
messages is a failure.

Usage::

    python scripts/consume_packets.py --run N [--settings FILE]

with the database named as for every rapidpipe command and the topic as
``rapidpipe publish`` names it (the ``[kafka]`` table of ``--settings``,
by default the shipped ``foundation-thin.toml``).
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import tomllib
from pathlib import Path

import fastavro
import fastavro.validation

from rapidpipe.db.connection import connect
from rapidpipe.foundation import publish_kafka
from rapidpipe.science.alerts.assemble import load_schema

SHIPPED = Path(__file__).resolve().parents[1] / "rapidpipe" / "settings" / "foundation-thin.toml"


def stamped_sets(conn, run: int) -> list[int]:
    return [r["job"] for r in publish_kafka._query(
        conn, "SELECT id AS job FROM jobs WHERE run = %s AND stage = 'packet'"
              " AND kafka_published_at IS NOT NULL ORDER BY id", (run,))]


def expected(conn, jobs: list[int]) -> tuple[dict[bytes, bytes], dict[bytes, str]]:
    """The outbox's bytes and the schema version for every packet key of ``jobs``."""
    values, versions = {}, {}
    for job in jobs:
        index = publish_kafka._query(conn, "SELECT sid, filename, byte_offset, byte_length,"
                                           " schema_version FROM packets WHERE job = %s", (job,))
        values.update(publish_kafka.messages(job, index))
        versions.update({f"{job}:{r['sid']}".encode(): r["schema_version"] for r in index})
    return values, versions


def check(messages, values: dict[bytes, bytes], versions: dict[bytes, str]) -> tuple[dict, list[str]]:
    """Counts and failures for ``(key, value)`` messages against the outbox's ``values``."""
    jobs = {key.split(b":", 1)[0] for key in values}
    counts = {"read": 0, "kept": 0, "valid": 0, "byte_equal": 0}
    seen, failures = set(), []
    for key, value in messages:
        counts["read"] += 1
        if key is None or key.split(b":", 1)[0] not in jobs:
            continue
        counts["kept"] += 1
        if key not in values:
            failures.append(f"{key.decode()}: no such packet in the outbox")
            continue
        seen.add(key)
        try:
            records = list(fastavro.reader(io.BytesIO(value)))
        except Exception as error:  # noqa: BLE001 - an undecodable message is the failure reported
            failures.append(f"{key.decode()}: does not decode ({error})")
            records = []
        if len(records) == 1 and fastavro.validation.validate(
                records[0], load_schema(versions[key]), raise_errors=False):
            counts["valid"] += 1
        elif records:
            failures.append(f"{key.decode()}: {len(records)} records, or not valid {versions[key]}")
        if value == values[key]:
            counts["byte_equal"] += 1
        else:
            failures.append(f"{key.decode()}: bytes differ from the outbox")
    counts["expected"], counts["seen"] = len(values), len(seen)
    failures += [f"{key.decode()}: never read" for key in sorted(set(values) - seen)]
    if counts["kept"] == 0:
        failures.append("no message of the run's stamped packet sets was read")
    return counts, failures


def read_topic(settings: dict) -> tuple[list[tuple[bytes, bytes]], dict[int, tuple[int, int]]]:
    """Every message of every partition, earliest to the end offset at start."""
    from kafka import KafkaConsumer, TopicPartition

    endpoint = publish_kafka.endpoint(settings)
    consumer = KafkaConsumer(group_id=None, enable_auto_commit=False,
                             max_partition_fetch_bytes=endpoint["max_request_bytes"],
                             fetch_max_bytes=endpoint["max_request_bytes"],
                             **publish_kafka.client_config(endpoint["bootstrap_servers"]))
    try:
        partitions = [TopicPartition(endpoint["topic"], p)
                      for p in sorted(consumer.partitions_for_topic(endpoint["topic"]) or ())]
        consumer.assign(partitions)
        first = consumer.beginning_offsets(partitions)
        last = consumer.end_offsets(partitions)
        consumer.seek_to_beginning(*partitions)
        messages = []
        pending = {tp for tp in partitions if last[tp] > first[tp]}
        while pending:
            for tp, batch in consumer.poll(timeout_ms=10000).items():
                for message in batch:
                    if message.offset < last[tp]:
                        messages.append((message.key, message.value))
                if consumer.position(tp) >= last[tp]:
                    pending.discard(tp)
        return messages, {tp.partition: (first[tp], last[tp] - 1) for tp in partitions}
    finally:
        consumer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--settings", type=Path, default=SHIPPED,
                        help="a TOML file whose [kafka] table names the SSM parameters")
    args = parser.parse_args(argv)
    settings = tomllib.loads(args.settings.read_text())["kafka"]
    with connect(application_name="consume_packets") as conn:
        values, versions = expected(conn, stamped_sets(conn, args.run))
    started = time.monotonic()
    messages, ranges = read_topic(settings)
    counts, failures = check(messages, values, versions)
    seconds = time.monotonic() - started
    verdict = "CLEAN" if not failures else f"{len(failures)} FAILURES"
    offsets = ", ".join(f"p{p} {a}..{b}" for p, (a, b) in sorted(ranges.items()))
    print(f"Consumer check run {args.run}: {verdict}; read {counts['read']}, kept {counts['kept']},"
          f" valid {counts['valid']}, byte-equal {counts['byte_equal']}, packets expected"
          f" {counts['expected']} seen {counts['seen']}; partitions {offsets}; {seconds:.1f} s")
    for failure in failures:
        print(f"- {failure}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
