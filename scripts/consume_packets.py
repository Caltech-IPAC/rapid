"""Read a run's packets back from the Kafka topic and compare them with the outbox.

Outside the pipeline, like ``read_packets.py``: it never writes to the
database or the topic, and exits non-zero on any failure. It reads every
partition of the topic, with no consumer group, from the first offset
whose timestamp is at or after the earliest ``ended`` of the run's
stamped packet jobs (a set is produced only after its job ended, so no
message of the run is earlier; ``offsets_for_times``) to its end offset
at start, keeps the messages whose key
``<job>:<sid>`` names one of the run's packet sets stamped
``kafka_published_at``, and for each kept message checks that:

- it decodes as a one-record Avro container whose record validates against
  the schema version its index row names (``read_packets.py``'s check);
- its bytes equal the outbox's for that key (the container header and the
  packet's block, :func:`rapidpipe.foundation.publish_kafka.messages`);

and that every packet of every stamped set was seen at least once. It
reads the topic in order and holds one set's outbox at a time and no
message after comparing it; what it keeps across the topic is the set of
packet keys expected and seen. The verdict is one line: messages read, kept, valid, byte-equal, packets
expected and seen, and the offset range read per partition. Zero kept
messages is a failure; a run with no stamped set reads nothing and says
so.

The client skips its record-batch CRC check: kafka-python without the
``crc32c`` C module computes CRC-32C in pure Python, which held the read
to about 50 messages a second (2026-10-23, a 600 thousand message topic:
three to four hours per run), and every kept message is compared byte
for byte with the outbox anyway.

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
from typing import Callable, Iterable, Iterator

import fastavro
import fastavro.validation

from rapidpipe.db.connection import connect
from rapidpipe.foundation import publish_kafka
from rapidpipe.science.alerts.assemble import load_schema

SHIPPED = Path(__file__).resolve().parents[1] / "rapidpipe" / "settings" / "foundation-thin.toml"


def earliest_ended(conn, jobs: list[int]):
    """The earliest ``ended`` of ``jobs``: no message of their sets was produced before it."""
    return publish_kafka._query(conn, "SELECT min(ended) AS t FROM jobs WHERE id = ANY(%s)",
                                (jobs,))[0]["t"]


def stamped_sets(conn, run: int) -> list[int]:
    return [r["job"] for r in publish_kafka._query(
        conn, "SELECT id AS job FROM jobs WHERE run = %s AND stage = 'packet'"
              " AND kafka_published_at IS NOT NULL ORDER BY id", (run,))]


def expected_keys(conn, jobs: list[int]) -> set[bytes]:
    """Every packet key ``<job>:<sid>`` of ``jobs``, from the index alone."""
    return {f"{r['job']}:{r['sid']}".encode() for r in publish_kafka._query(
        conn, "SELECT job, sid FROM packets WHERE job = ANY(%s)", (jobs,))}


def outbox(conn, job: int) -> tuple[dict[bytes, bytes], dict[bytes, str]]:
    """One set's outbox bytes and schema version for each of its packet keys."""
    index = publish_kafka._query(conn, "SELECT sid, filename, byte_offset, byte_length,"
                                       " schema_version FROM packets WHERE job = %s", (job,))
    return (dict(publish_kafka.messages(job, index)),
            {f"{job}:{r['sid']}".encode(): r["schema_version"] for r in index})


def check(messages: Iterable[tuple[bytes, bytes]], keys: set[bytes],
          load: Callable[[int], tuple[dict[bytes, bytes], dict[bytes, str]]]) -> tuple[dict, list[str]]:
    """Counts and failures for ``(key, value)`` messages against the outbox of the sets ``keys`` name.

    ``load(job)`` gives one set's outbox; one set is held at a time, loaded
    when a message of another set arrives (a set's messages are produced
    together, so each set is loaded once unless it was replayed), and no
    message is kept after it is compared.
    """
    jobs = {key.split(b":", 1)[0] for key in keys}
    counts = {"read": 0, "kept": 0, "valid": 0, "byte_equal": 0}
    seen, failures, held, values, versions = set(), [], None, {}, {}
    for key, value in messages:
        counts["read"] += 1
        if key is None or (job := key.split(b":", 1)[0]) not in jobs:
            continue
        counts["kept"] += 1
        if job != held:
            held = job
            values, versions = load(int(job))
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
    counts["expected"], counts["seen"] = len(keys), len(seen)
    failures += [f"{key.decode()}: never read" for key in sorted(keys - seen)]
    if counts["kept"] == 0:
        failures.append("no message of the run's stamped packet sets was read")
    return counts, failures


def read_topic(settings: dict, since_ms: int,
               ranges: dict[int, tuple[int, int]]) -> Iterator[tuple[bytes, bytes]]:
    """Every message of every partition from the first whose timestamp is at or after
    ``since_ms`` (milliseconds since the epoch) to the end offset at start, one at a time;
    ``ranges`` receives each partition's offsets read."""
    from kafka import KafkaConsumer, TopicPartition

    endpoint = publish_kafka.endpoint(settings)
    consumer = KafkaConsumer(group_id=None, enable_auto_commit=False, check_crcs=False,
                             max_partition_fetch_bytes=endpoint["max_request_bytes"],
                             fetch_max_bytes=endpoint["max_request_bytes"],
                             **publish_kafka.client_config(endpoint["bootstrap_servers"]))
    try:
        partitions = [TopicPartition(endpoint["topic"], p)
                      for p in sorted(consumer.partitions_for_topic(endpoint["topic"]) or ())]
        consumer.assign(partitions)
        last = consumer.end_offsets(partitions)
        found = consumer.offsets_for_times({tp: since_ms for tp in partitions})
        first = {tp: last[tp] if found[tp] is None else found[tp].offset for tp in partitions}
        ranges.update({tp.partition: (first[tp], last[tp] - 1) for tp in partitions})
        for tp in partitions:
            consumer.seek(tp, first[tp])
        pending = {tp for tp in partitions if last[tp] > first[tp]}
        while pending:
            for tp, batch in consumer.poll(timeout_ms=10000).items():
                for message in batch:
                    if message.offset < last[tp]:
                        yield message.key, message.value
                if consumer.position(tp) >= last[tp]:
                    pending.discard(tp)
    finally:
        consumer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--settings", type=Path, default=SHIPPED,
                        help="a TOML file whose [kafka] table names the SSM parameters")
    args = parser.parse_args(argv)
    settings = tomllib.loads(args.settings.read_text())["kafka"]
    ranges: dict[int, tuple[int, int]] = {}
    with connect(application_name="consume_packets") as conn:
        jobs = stamped_sets(conn, args.run)
        keys = expected_keys(conn, jobs)
        started = time.monotonic()
        if jobs:
            since_ms = int(earliest_ended(conn, jobs).timestamp() * 1000)
            messages = read_topic(settings, since_ms, ranges)
        else:
            print(f"Run {args.run} has no stamped packet set: nothing read")
            messages = iter(())
        counts, failures = check(messages, keys, lambda job: outbox(conn, job))
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
