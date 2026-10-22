"""``rapidpipe publish``: official packet sets onto the Kafka topic, stamped after their acks.

Contracts: only the run's sets with a null stamp are sent; every packet of
a set is sent, keyed ``<job>:<sid>`` with the container header and its
block as the value, before that set is stamped; a producer error leaves the
stamp null and ``publish`` exits non-zero.
"""

from __future__ import annotations

import contextlib
import io

import fastavro
import pytest

from rapidpipe.cli import main as cli
from rapidpipe.exitcodes import ExitCode
from rapidpipe.foundation import publish_kafka

SCHEMA = fastavro.parse_schema({"type": "record", "name": "P", "fields": [
    {"name": "sid", "type": "long"}]})


def _outbox(tmp_path, job: int, sids: list[int]) -> list[dict]:
    """A one-record-per-block container under ``tmp_path`` and its index rows."""
    path = tmp_path / f"{job}.avro"
    with path.open("wb") as stream:
        writer = fastavro.write.Writer(stream, SCHEMA)
        for sid in sids:
            writer.write({"sid": sid})
            writer.flush()
    with path.open("rb") as stream:
        blocks = list(fastavro.block_reader(stream))
    return [{"sid": sid, "filename": path.as_uri(), "byte_offset": b.offset, "byte_length": b.size}
            for sid, b in zip(sids, blocks)]


class Future:
    def __init__(self, error=None):
        self.error = error

    def get(self, timeout):
        if self.error:
            raise self.error


class Producer:
    def __init__(self, events: list, fail_on: bytes | None = None):
        self.sent, self.fail_on, self.events = [], fail_on, events

    def send(self, topic, key, value):
        self.sent.append((topic, key, value))
        self.events.append(("send", key))
        return Future(RuntimeError("broker said no") if key == self.fail_on else None)

    def flush(self):
        self.events.append(("flush", None))

    def close(self):
        pass


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    """Run 3's unstamped sets are 7 and 9; sends, flushes and stamps land in one event list."""
    index = {7: _outbox(tmp_path, 7, [70, 71, 72]), 9: _outbox(tmp_path, 9, [90])}
    events = []
    monkeypatch.setattr(publish_kafka, "unpublished_sets",
                        lambda conn, run: [7, 9] if run == 3 else [])
    monkeypatch.setattr(publish_kafka, "set_index", lambda conn, job: index[job])
    monkeypatch.setattr(publish_kafka, "stamp", lambda conn, job: events.append(("stamp", job)))
    return events


def _connect():
    return contextlib.nullcontext()


def test_only_the_unstamped_sets_are_sent_each_whole_before_its_stamp(outbox):
    producer = Producer(outbox)
    result = publish_kafka.publish(_connect, 3, producer, "topic")
    assert [key for _, key, _ in producer.sent] == [b"7:70", b"7:71", b"7:72", b"9:90"]
    assert outbox.index(("stamp", 7)) > outbox.index(("send", b"7:72"))
    assert outbox.index(("stamp", 7)) < outbox.index(("send", b"9:90"))
    assert [e for e in outbox if e[0] == "stamp"] == [("stamp", 7), ("stamp", 9)]
    assert (result["sets"], result["packets"]) == ([7, 9], 4)
    assert publish_kafka.publish(_connect, 4, producer, "topic")["packets"] == 0


def test_each_message_is_a_one_record_container_of_its_packet(outbox):
    producer = Producer(outbox)
    publish_kafka.publish(_connect, 3, producer, "topic")
    for _, key, value in producer.sent:
        (record,) = fastavro.reader(io.BytesIO(value))
        assert key.decode().endswith(f":{record['sid']}")


def test_a_producer_error_leaves_the_stamp_null_and_the_verb_exits_non_zero(outbox, tmp_path, monkeypatch):
    producer = Producer(outbox, fail_on=b"7:71")
    settings = tmp_path / "s.toml"
    settings.write_text('[kafka]\nbootstrap_servers_parameter = "a"\ntopic_parameter = "b"\n'
                        'max_request_bytes_parameter = "c"\n')
    monkeypatch.setattr(cli, "connect", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr(publish_kafka, "endpoint", lambda settings: {
        "bootstrap_servers": ["b:9098"], "topic": "topic", "max_request_bytes": 1 << 20})
    monkeypatch.setattr(publish_kafka, "producer", lambda servers, size: producer)
    code = cli.main(["publish", "--run", "3", "--settings", str(settings)])
    assert code != ExitCode.SUCCESS
    assert not [e for e in outbox if e[0] == "stamp"]
