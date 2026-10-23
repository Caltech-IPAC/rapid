"""scripts/consume_packets.py's check: what was read from the topic against the outbox.

Contracts: messages byte-equal to the outbox and covering every packet are
clean; a message whose bytes differ, a packet never read, or no kept
message at all is a failure; messages of other runs' sets are read and not
kept; each set's outbox is loaded when its first message arrives, once for
a set whose messages are contiguous; the topic is read from the offset the
time lookup gives for the run, not from the earliest.
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
CONTAINERS = {sid: _write(sid) for sid in (70, 71, 80)}
_container = CONTAINERS.__getitem__


OUTBOX = {7: {b"7:70": _container(70), b"7:71": _container(71)}}


def _check(monkeypatch, messages, loads=None):
    monkeypatch.setattr(consume_packets, "load_schema", lambda version: SCHEMA)

    def load(job):
        if loads is not None:
            loads.append(job)
        return OUTBOX[job], dict.fromkeys(OUTBOX[job], "00.06")

    return consume_packets.check(iter(messages), set(OUTBOX[7]), load)


def test_byte_equal_messages_covering_every_packet_are_clean(monkeypatch):
    other = (b"8:80", _container(80))
    counts, failures = _check(monkeypatch, [other, (b"7:70", _container(70)), (b"7:71", _container(71))])
    assert failures == []
    assert counts == {"read": 3, "kept": 2, "valid": 2, "byte_equal": 2, "expected": 2, "seen": 2}


def test_differing_bytes_and_a_packet_never_read_fail(monkeypatch):
    counts, failures = _check(monkeypatch, [(b"7:70", _container(71))])
    assert counts["byte_equal"] == 0
    assert failures == ["7:70: bytes differ from the outbox", "7:71: never read"]


def test_no_kept_message_fails(monkeypatch):
    _, failures = _check(monkeypatch, [(b"8:80", _container(80))])
    assert "no message of the run's stamped packet sets was read" in failures


def test_a_sets_outbox_is_loaded_once_when_its_first_message_arrives(monkeypatch):
    loads = []
    _check(monkeypatch, [(b"8:80", _container(80)), (b"7:70", _container(70)),
                         (b"7:71", _container(71))], loads)
    assert loads == [7]


class _FakeConsumer:
    """One partition of offsets 0..7 whose message at each offset is keyed by it."""

    END, FOUND = 8, 5

    def __init__(self, **config):
        self.config, self.positions, self.since = config, {}, None

    def partitions_for_topic(self, topic):
        return {0}

    def assign(self, partitions):
        pass

    def end_offsets(self, partitions):
        return dict.fromkeys(partitions, self.END)

    def offsets_for_times(self, timestamps):
        self.since = set(timestamps.values())
        return dict.fromkeys(timestamps, types.SimpleNamespace(offset=self.FOUND, timestamp=0))

    def seek(self, tp, offset):
        self.positions[tp] = offset

    def position(self, tp):
        return self.positions[tp]

    def poll(self, timeout_ms):
        batch = {tp: [types.SimpleNamespace(offset=o, key=str(o).encode(), value=b"")
                      for o in range(start, self.END)] for tp, start in self.positions.items()}
        self.positions = dict.fromkeys(self.positions, self.END)
        return batch

    def close(self):
        pass


def test_the_read_starts_at_the_offset_the_time_lookup_gives_not_the_earliest(monkeypatch):
    consumers = []

    def consumer(**config):
        consumers.append(_FakeConsumer(**config))
        return consumers[-1]

    monkeypatch.setattr(kafka, "KafkaConsumer", consumer)
    monkeypatch.setattr(consume_packets.publish_kafka, "endpoint",
                        lambda s: {"topic": "t", "bootstrap_servers": "b", "max_request_bytes": 1})
    monkeypatch.setattr(consume_packets.publish_kafka, "client_config", lambda servers: {})
    ranges = {}
    keys = [key for key, _ in consume_packets.read_topic({}, 1234, ranges)]
    assert consumers[0].since == {1234}
    assert keys == [b"5", b"6", b"7"]
    assert ranges == {0: (5, 7)}
