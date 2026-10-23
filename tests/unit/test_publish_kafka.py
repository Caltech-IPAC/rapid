"""``rapidpipe publish``: official packet sets onto the Kafka topic, stamped after their acks.

Contracts: only the run's sets with a null stamp are sent; every packet of
every set is sent, keyed ``<job>:<sid>`` with the container header and its
block as the value, then flushed and stamped before the next set is sent;
one connection serves the pass; a failed acknowledgement leaves its set's
stamp null, the other sets stamped, and ``publish`` exits non-zero; a flush
that times out stops the pass, leaving that set and the later ones unstamped.
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
    def __init__(self, events: list, fail_on: bytes | None = None, stall_on: bytes | None = None):
        self.sent, self.fail_on, self.stall_on, self.events = [], fail_on, stall_on, events

    def send(self, topic, key, value):
        self.sent.append((topic, key, value))
        self.events.append(("send", key))
        return Future(RuntimeError("broker said no") if key == self.fail_on else None)

    def flush(self, timeout):
        self.events.append(("flush", None))
        if self.stall_on is not None and self.sent[-1][1] == self.stall_on:
            raise TimeoutError(f"flush not finished in {timeout} s")

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


class Connects:
    """A ``connect`` that counts the connections opened."""

    def __init__(self):
        self.opened = 0

    def __call__(self):
        self.opened += 1
        return contextlib.nullcontext()


def test_each_set_is_sent_flushed_and_stamped_before_the_next_on_one_connection(outbox):
    producer, connect = Producer(outbox), Connects()
    result = publish_kafka.publish(connect, 3, producer, "topic")
    assert outbox == [("send", b"7:70"), ("send", b"7:71"), ("send", b"7:72"), ("flush", None), ("stamp", 7),
                      ("send", b"9:90"), ("flush", None), ("stamp", 9)]
    assert connect.opened == 1
    assert (result["sets"], result["packets"]) == ([7, 9], 4)
    assert publish_kafka.publish(_connect, 4, producer, "topic")["packets"] == 0


def test_a_failed_acknowledgement_leaves_its_set_unstamped_and_the_others_stamped(outbox):
    with pytest.raises(publish_kafka.PublishError, match=r"\[7\]"):
        publish_kafka.publish(_connect, 3, Producer(outbox, fail_on=b"7:71"), "topic")
    assert [e for e in outbox if e[0] == "stamp"] == [("stamp", 9)]


def test_a_flush_that_times_out_stops_the_pass_with_that_set_and_the_later_ones_unstamped(outbox):
    with pytest.raises(publish_kafka.PublishError, match=r"\[7\]"):
        publish_kafka.publish(_connect, 3, Producer(outbox, stall_on=b"7:72"), "topic")
    assert not [e for e in outbox if e[0] == "stamp"]
    assert ("send", b"9:90") not in outbox


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
    assert ("stamp", 7) not in outbox
