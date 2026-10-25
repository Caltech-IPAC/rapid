"""Read a run's packets back from the Kafka topic at their recorded offsets and check them.

Outside the pipeline, like ``read_packets.py``: it never writes to the
database or the topic, and exits non-zero on any failure. Each packet set
of the run stamped ``kafka_published_at`` carries the partition and the
first and last offsets its acknowledgements gave
(``rapidpipe.foundation.publish_kafka.deliver``); the check reads exactly
those ranges, no consumer group, one partition assigned at a time and its
overlapping ranges merged and read once (a poll that brings nothing ends
the range), and identifies a set's own messages within a range
by the message key (the run's dataset) and the ``rapid-set`` and
``rapid-sid`` headers: concurrent sets of one dataset interleave in a
partition, so a range's length is not a count. For every set it checks
that each of its index's packets was read in its range and no message
names a packet the index lacks; for a sample of one set per field (the
latest stamped of the field), it also checks that each message decodes as
a one-record Avro container whose record validates against the schema
version its index row names (``read_packets.py``'s check) and that its
bytes equal the outbox's (the container header and the packet's block).
That is the per-run contract: the acknowledgements, every set counted,
and the sample compared byte for byte (byte equality of every packet was
milestone 8's proof). A stamped set without recorded offsets (stamped
before they were recorded) is a failure, never read.

The verdict is one line: messages read, kept, valid, byte-equal, packets
expected and seen, sets and sets sampled, and the offset ranges read per
partition. A run with no stamped set, or whose stamped sets hold no
packets, produced nothing to check and fails (:func:`produced`).

The client skips its record-batch CRC check: kafka-python without the
``crc32c`` C module computes CRC-32C in pure Python, about 50 messages a
second (2026-10-23), and every sampled message is compared byte for byte
with the outbox anyway.

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


def stamped_sets(conn, run: int) -> list[dict]:
    """The run's stamped packet sets: ``job``, ``dataset``, ``partition``, ``first``, ``last``, ``field``."""
    return publish_kafka._query(conn, """
        SELECT j.id AS job, r.dataset, j.kafka_partition AS partition,
               j.kafka_first_offset AS first, j.kafka_last_offset AS last,
               (SELECT min(l.field) FROM l2files l WHERE l.dataset = r.dataset
                  AND l.expid = (j.scope->>'expid')::integer AND l.sca = (j.scope->>'sca')::smallint
                  AND l.fid = (j.scope->>'fid')::smallint) AS field
        FROM jobs j JOIN runs r ON r.id = j.run
        WHERE j.run = %s AND j.stage = 'packet' AND j.kafka_published_at IS NOT NULL
        ORDER BY j.id""", (run,))


def sample(sets: list[dict]) -> set[int]:
    """One set per field, the latest stamped (the highest job id) of each."""
    latest: dict = {}
    for s in sets:
        latest[s["field"]] = max(latest.get(s["field"], 0), s["job"])
    return set(latest.values())


def index_sids(conn, jobs: list[int]) -> dict[int, set[int]]:
    """Every packet ``sid`` of each of ``jobs``, from the index alone."""
    out: dict[int, set[int]] = {job: set() for job in jobs}
    for r in publish_kafka._query(conn, "SELECT job, sid FROM packets WHERE job = ANY(%s)", (jobs,)):
        out[r["job"]].add(r["sid"])
    return out


def outbox(conn, job: int) -> tuple[dict[int, bytes], dict[int, str]]:
    """One set's outbox bytes and schema version for each of its packets, by ``sid``."""
    index = publish_kafka._query(conn, "SELECT sid, filename, byte_offset, byte_length,"
                                       " schema_version FROM packets WHERE job = %s ORDER BY sid", (job,))
    return dict(publish_kafka.messages(index)), {r["sid"]: r["schema_version"] for r in index}


def merged_ranges(sets: list[dict]) -> dict[int, list[tuple[int, int]]]:
    """Per partition, the sets' inclusive offset ranges with overlapping or adjacent ones merged."""
    by: dict[int, list[tuple[int, int]]] = {}
    for s in sorted(sets, key=lambda s: (s["partition"], s["first"])):
        spans = by.setdefault(s["partition"], [])
        if spans and s["first"] <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(spans[-1][1], s["last"]))
        else:
            spans.append((s["first"], s["last"]))
    return by


def check(messages: Iterable[tuple[bytes, list, bytes]], sets: dict[int, dict],
          load: Callable[[int], tuple[dict[int, bytes], dict[int, str]]]) -> tuple[dict, list[str]]:
    """Counts and failures for ``(key, headers, value)`` messages against the sets ``sets`` names.

    ``sets`` maps a job to its ``dataset``, its index's ``sids`` and whether
    it is ``sampled``. A message is the set's when its key is the set's
    dataset and its headers name the set. ``load(job)`` gives a sampled
    set's outbox, loaded at its first message and dropped once every one of
    its packets has been seen.
    """
    counts = {"read": 0, "kept": 0, "valid": 0, "byte_equal": 0}
    seen: dict[int, set[int]] = {job: set() for job in sets}
    failures: list[str] = []
    held: dict[int, tuple[dict[int, bytes], dict[int, str]]] = {}
    for key, headers, value in messages:
        counts["read"] += 1
        found = publish_kafka.identity(headers)
        if found is None or found[0] not in sets or key != sets[found[0]]["dataset"].encode():
            continue
        job, sid = found
        counts["kept"] += 1
        if sid not in sets[job]["sids"]:
            failures.append(f"{job}:{sid}: no such packet in the index")
            continue
        seen[job].add(sid)
        if not sets[job]["sampled"]:
            continue
        if job not in held:
            held[job] = load(job)
        values, versions = held[job]
        try:
            records = list(fastavro.reader(io.BytesIO(value)))
        except Exception as error:  # noqa: BLE001 - an undecodable message is the failure reported
            failures.append(f"{job}:{sid}: does not decode ({error})")
            records = []
        if len(records) == 1 and fastavro.validation.validate(
                records[0], load_schema(versions[sid]), raise_errors=False):
            counts["valid"] += 1
        elif records:
            failures.append(f"{job}:{sid}: {len(records)} records, or not valid {versions[sid]}")
        if value == values[sid]:
            counts["byte_equal"] += 1
        else:
            failures.append(f"{job}:{sid}: bytes differ from the outbox")
        if seen[job] == sets[job]["sids"]:
            del held[job]
    counts["expected"] = sum(len(s["sids"]) for s in sets.values())
    counts["seen"] = sum(len(s) for s in seen.values())
    failures += [f"{job}:{sid}: never read" for job in sorted(sets)
                 for sid in sorted(sets[job]["sids"] - seen[job])]
    return counts, failures


def produced(stamped: int, expected: int) -> list[str]:
    """The failure of a run that produced nothing: no stamped set, or ``stamped`` sets holding no ``expected`` packet."""
    if not stamped:
        return ["no stamped packet set: the run produced nothing to read"]
    if not expected:
        return [f"{stamped} stamped packet sets hold no packets: the run produced nothing to read"]
    return []


def read_ranges(settings: dict, ranges: dict[int, list[tuple[int, int]]]) -> Iterator[tuple[bytes, list, bytes]]:
    """``(key, headers, value)`` of every message in each partition's inclusive ``ranges``, in order."""
    from kafka import KafkaConsumer, TopicPartition

    endpoint = publish_kafka.endpoint(settings)
    consumer = KafkaConsumer(group_id=None, enable_auto_commit=False, check_crcs=False,
                             max_partition_fetch_bytes=endpoint["max_request_bytes"],
                             fetch_max_bytes=endpoint["max_request_bytes"],
                             **publish_kafka.client_config(endpoint["bootstrap_servers"]))
    try:
        for partition, spans in sorted(ranges.items()):
            tp = TopicPartition(endpoint["topic"], partition)
            consumer.assign([tp])
            for first, last in spans:
                consumer.seek(tp, first)
                while (position := consumer.position(tp)) <= last:
                    batch = consumer.poll(timeout_ms=10000).get(tp, [])
                    for message in batch:
                        if first <= message.offset <= last:
                            yield message.key, message.headers, message.value
                    if not batch and consumer.position(tp) == position:
                        break       # nothing more arrives: what was not read is reported as never read
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
        stamped = stamped_sets(conn, args.run)
        located = [s for s in stamped if s["partition"] is not None and s["first"] is not None]
        failures = [f"packet set {s['job']}: stamped without acknowledgement offsets"
                    for s in stamped if s not in located]
        sampled, sids = sample(located), index_sids(conn, [s["job"] for s in located])
        sets = {s["job"]: {"dataset": s["dataset"], "sids": sids[s["job"]], "sampled": s["job"] in sampled}
                for s in located}
        ranges = merged_ranges(located)
        started = time.monotonic()
        messages = read_ranges(settings, ranges) if ranges else iter(())
        counts, more = check(messages, sets, lambda job: outbox(conn, job))
        failures += more + produced(len(stamped), counts["expected"])
        seconds = time.monotonic() - started
    verdict = "CLEAN" if not failures else f"{len(failures)} FAILURES"
    offsets = ", ".join(f"p{p} {a}..{b}" for p, spans in sorted(ranges.items()) for a, b in spans)
    print(f"Consumer check run {args.run}: {verdict}; read {counts['read']}, kept {counts['kept']},"
          f" valid {counts['valid']}, byte-equal {counts['byte_equal']}, packets expected"
          f" {counts['expected']} seen {counts['seen']}; sets {len(stamped)} sampled {len(sampled)};"
          f" ranges {offsets or 'none'}; {seconds:.1f} s")
    for failure in failures:
        print(f"- {failure}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
