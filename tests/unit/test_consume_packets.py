"""scripts/consume_packets.py's check: a run's sets read at their recorded offsets against the index and outbox.

Contracts: a set's messages are those keyed by its dataset whose headers
name it, among other sets' messages interleaved in its range; every
packet of every set read is clean; a packet never read, or a message
naming a packet the index lacks, is a failure; a sampled set's messages
are also compared byte for byte with the outbox and a differing one fails,
an unsampled set's are only counted; the sample is one set per field;
overlapping ranges of one partition are read once; the read seeks to each
range's first offset and stops after its last. A run with no stamped set,
or whose stamped sets hold no packet, produced nothing and fails.
"""

from __future__ import annotations

import importlib.util
import io
import types
from pathlib import Path

import fastavro
import kafka

_SPEC = importlib.util.spec_from_file_location(
    "consume_packets", Path(__file__).resolve().parents[2] / "scripts" / "consume_packets.py")
consume_packets = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(consume_packets)

SCHEMA = {"type": "record", "name": "P", "fields": [{"name": "sid", "type": "long"}]}


def _write(sid: int) -> bytes:
    stream = io.BytesIO()
    fastavro.writer(stream, fastavro.parse_schema(SCHEMA), [{"sid": sid}])
    return stream.getvalue()


#: One container per packet, written once: a container's sync marker is random.
CONTAINERS = {sid: _write(sid) for sid in (70, 71, 80, 81)}


def _message(job: int, sid: int, dataset: bytes = b"D", value: bytes | None = None):
    return (dataset, [("rapid-set", str(job).encode()), ("rapid-sid", str(sid).encode())],
            CONTAINERS[sid] if value is None else value)


def _check(monkeypatch, messages, sampled=(7,), loads=None):
    monkeypatch.setattr(consume_packets, "load_schema", lambda version: SCHEMA)
    sets = {7: {"dataset": "D", "sids": {70, 71}, "sampled": 7 in sampled},
            8: {"dataset": "D", "sids": {80, 81}, "sampled": 8 in sampled}}

    def load(job):
        if loads is not None:
            loads.append(job)
        return {sid: CONTAINERS[sid] for sid in sets[job]["sids"]}, dict.fromkeys(sets[job]["sids"], "00.06")

    return consume_packets.check(iter(messages), sets, load)


def test_interleaved_sets_counted_and_the_sampled_one_byte_equal_are_clean(monkeypatch):
    other_dataset = _message(7, 70, dataset=b"E", value=b"x")
    other_set = (b"D", [("rapid-set", b"99"), ("rapid-sid", b"1")], b"x")
    loads = []
    counts, failures = _check(monkeypatch, [_message(7, 70), other_dataset, _message(8, 80), other_set,
                                            _message(7, 71), _message(8, 81)], loads=loads)
    assert failures == []
    assert counts == {"read": 6, "kept": 4, "valid": 2, "byte_equal": 2, "expected": 4, "seen": 4}
    assert loads == [7]


def test_a_packet_never_read_and_a_packet_the_index_lacks_fail(monkeypatch):
    _, failures = _check(monkeypatch, [_message(7, 70), _message(8, 80), _message(7, 81)])
    assert failures == ["7:81: no such packet in the index", "7:71: never read", "8:81: never read"]


def test_a_sampled_sets_differing_bytes_fail_and_an_unsampled_sets_are_not_compared(monkeypatch):
    counts, failures = _check(monkeypatch, [_message(7, 70, value=CONTAINERS[71]), _message(7, 71),
                                            _message(8, 80, value=b"x"), _message(8, 81)])
    assert failures == ["7:70: bytes differ from the outbox"]
    assert counts["byte_equal"] == 1


def test_the_sample_is_the_latest_set_of_each_field():
    sets = [{"job": 1, "field": 10}, {"job": 4, "field": 10}, {"job": 2, "field": 11}]
    assert consume_packets.sample(sets) == {4, 2}


def test_overlapping_and_adjacent_ranges_of_a_partition_merge():
    sets = [{"partition": 0, "first": 5, "last": 9}, {"partition": 0, "first": 7, "last": 12},
            {"partition": 0, "first": 13, "last": 14}, {"partition": 0, "first": 20, "last": 21},
            {"partition": 1, "first": 3, "last": 3}]
    assert consume_packets.merged_ranges(sets) == {0: [(5, 14), (20, 21)], 1: [(3, 3)]}


class _FakeConsumer:
    """One partition of offsets 0..9 whose message at each offset is keyed by it, polled two at a time."""

    END = 10

    def __init__(self, **config):
        self.assigned, self.position_, self.seeks = None, 0, []

    def assign(self, partitions):
        self.assigned = partitions

    def seek(self, tp, offset):
        self.seeks.append(offset)
        self.position_ = offset

    def position(self, tp):
        return self.position_

    def poll(self, timeout_ms):
        start = self.position_
        self.position_ = min(self.END, start + 2)
        return {self.assigned[0]: [types.SimpleNamespace(offset=o, key=str(o).encode(), headers=[], value=b"")
                                   for o in range(start, self.position_)]}

    def close(self):
        pass


def test_the_read_seeks_to_each_range_and_stops_after_its_last_offset(monkeypatch):
    consumers = []

    def consumer(**config):
        consumers.append(_FakeConsumer(**config))
        return consumers[-1]

    monkeypatch.setattr(kafka, "KafkaConsumer", consumer)
    monkeypatch.setattr(consume_packets.publish_kafka, "endpoint",
                        lambda s: {"topic": "t", "bootstrap_servers": "b", "max_request_bytes": 1})
    monkeypatch.setattr(consume_packets.publish_kafka, "client_config", lambda servers: {})
    keys = [key for key, _, _ in consume_packets.read_ranges({}, {0: [(1, 2), (5, 7)]})]
    assert consumers[0].seeks == [1, 5]
    assert keys == [b"1", b"2", b"5", b"6", b"7"]


def test_a_run_with_no_stamped_set_or_no_packets_fails_and_one_with_packets_does_not():
    assert consume_packets.produced(0, 0)
    assert consume_packets.produced(3, 0)
    assert consume_packets.produced(3, 12) == []
