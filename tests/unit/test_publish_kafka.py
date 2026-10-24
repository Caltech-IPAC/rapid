"""Official packet sets onto the Kafka topic, stamped with their acknowledgements' offsets.

Contracts: only the run's sets with a null stamp are sent; every packet of
every set is sent, keyed by the run's dataset, its set and sid in its
headers, the container header and its block as the value, then flushed and
stamped with the partition and the first and last acknowledged offsets
before the next set is sent; one connection serves the pass; a failed
acknowledgement leaves its set's stamp null, the other sets stamped, and
``publish`` exits non-zero; a flush that times out stops the pass, leaving
that set and the later ones unstamped; a packet job that publishes and
binds sends its own set, a failure to send it leaves the job exit 0 and the
set unstamped, and a job that did not bind sends nothing; a set the
quality gate holds is neither sent nor stamped, by the sweep or by its own
packet job, and the others are; the producer waits for every replica and
is idempotent.
"""

from __future__ import annotations

import contextlib
import io
import types

import fastavro
import kafka
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
    def __init__(self, offset, error=None):
        self.offset, self.error = offset, error

    def get(self, timeout):
        if self.error:
            raise self.error
        return types.SimpleNamespace(partition=2, offset=self.offset)


class Producer:
    def __init__(self, events: list, fail_on: bytes | None = None, stall_on: bytes | None = None):
        self.sent, self.fail_on, self.stall_on, self.events = [], fail_on, stall_on, events

    def send(self, topic, key, value, headers):
        packet = b"%s:%s" % (dict(headers)["rapid-set"], dict(headers)["rapid-sid"])
        self.sent.append((topic, key, value, packet))
        self.events.append(("send", packet))
        return Future(100 + len(self.sent), RuntimeError("broker said no") if packet == self.fail_on else None)

    def flush(self, timeout):
        self.events.append(("flush", None))
        if self.stall_on is not None and self.sent[-1][3] == self.stall_on:
            raise TimeoutError(f"flush not finished in {timeout} s")

    def close(self, timeout=None):
        self.events.append(("close", None))


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    """Run 3's unstamped sets are 7 and 9; sends, flushes and stamps land in one event list."""
    index = {7: _outbox(tmp_path, 7, [70, 71, 72]), 9: _outbox(tmp_path, 9, [90])}
    events = []
    monkeypatch.setattr(publish_kafka, "unpublished_sets",
                        lambda conn, run: [7, 9] if run == 3 else [])
    monkeypatch.setattr(publish_kafka, "settling_sets", lambda conn, run: [11] if run == 3 else [])
    monkeypatch.setattr(publish_kafka, "late_sets", lambda conn, run: 2 if run == 3 else 0)
    monkeypatch.setattr(publish_kafka, "set_index", lambda conn, job: index[job])
    monkeypatch.setattr(publish_kafka, "set_run", lambda conn, job: {"dataset": "r00342-x", "gate": None})
    monkeypatch.setattr(publish_kafka.gate, "decide", lambda conn, job, settings: {
        "send": job not in HELD, "reason": "n_sources_snr_gt20 off-scale"})
    monkeypatch.setattr(publish_kafka, "stamp", lambda conn, job, *coordinates: events.append(("stamp", job, *coordinates)))
    return events


#: The packet sets the faked quality gate holds.
HELD: set[int] = set()


@pytest.fixture()
def held():
    HELD.clear()
    yield HELD
    HELD.clear()


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
    assert outbox == [("send", b"7:70"), ("send", b"7:71"), ("send", b"7:72"), ("flush", None),
                      ("stamp", 7, 2, 101, 103), ("send", b"9:90"), ("flush", None), ("stamp", 9, 2, 104, 104)]
    assert connect.opened == 1
    assert (result["sets"], result["packets"], result["waiting"], result["late"]) == ([7, 9], 4, [11], 2)
    assert {key for _, key, _, _ in producer.sent} == {b"r00342-x"}
    assert publish_kafka.publish(_connect, 4, producer, "topic")["packets"] == 0


def test_a_failed_acknowledgement_leaves_its_set_unstamped_and_the_others_stamped(outbox):
    with pytest.raises(publish_kafka.PublishError, match=r"\[7\]"):
        publish_kafka.publish(_connect, 3, Producer(outbox, fail_on=b"7:71"), "topic")
    assert [e[:2] for e in outbox if e[0] == "stamp"] == [("stamp", 9)]


def test_a_flush_that_times_out_stops_the_pass_with_that_set_and_the_later_ones_unstamped(outbox):
    with pytest.raises(publish_kafka.PublishError, match=r"\[7\]"):
        publish_kafka.publish(_connect, 3, Producer(outbox, stall_on=b"7:72"), "topic")
    assert not [e for e in outbox if e[0] == "stamp"]
    assert ("send", b"9:90") not in outbox


def test_each_message_is_a_one_record_container_of_its_packet(outbox):
    producer = Producer(outbox)
    publish_kafka.publish(_connect, 3, producer, "topic")
    for _, _, value, packet in producer.sent:
        (record,) = fastavro.reader(io.BytesIO(value))
        assert packet.decode().endswith(f":{record['sid']}")


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
    assert not [e for e in outbox if e[:2] == ("stamp", 7)]


@pytest.fixture()
def packet_job(outbox, monkeypatch):
    """``job run --job 7``: a packet job whose stage and publication are faked."""
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "file:///nowhere")
    monkeypatch.setattr(cli, "connect", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr(cli.foundation, "job_show", lambda conn, job: {"stage": "packet", "run": 3})
    monkeypatch.setattr(cli.foundation, "run_show", lambda conn, run: {"settings": {"kafka": {}}})
    monkeypatch.setattr(cli, "_stage_run", lambda stage: None)
    monkeypatch.setattr(publish_kafka, "endpoint", lambda settings: {
        "bootstrap_servers": ["b:9098"], "topic": "topic", "max_request_bytes": 1 << 20})
    published = {"job": 7, "attempt": 1, "published": True, "group": 5, "refusal": None, "live": True}
    monkeypatch.setattr(cli.foundation, "run_job", lambda *a, **kw: dict(published))
    return published


def test_a_packet_job_that_binds_sends_its_own_set_and_stamps_it(packet_job, outbox, monkeypatch):
    producer = Producer(outbox)
    monkeypatch.setattr(publish_kafka, "producer", lambda servers, size: producer)
    assert cli.main(["job", "run", "--job", "7"]) == ExitCode.SUCCESS
    assert outbox[-2:] == [("stamp", 7, 2, 101, 103), ("close", None)]
    assert [e for e in outbox if e[0] == "send"] == [("send", b"7:70"), ("send", b"7:71"), ("send", b"7:72")]


def test_a_packet_job_whose_send_fails_exits_0_with_its_set_unstamped(packet_job, outbox, monkeypatch):
    monkeypatch.setattr(publish_kafka, "producer", lambda servers, size: Producer(outbox, fail_on=b"7:71"))
    assert cli.main(["job", "run", "--job", "7"]) == ExitCode.SUCCESS
    assert not [e for e in outbox if e[0] == "stamp"]


def test_a_packet_job_that_did_not_bind_sends_nothing(packet_job, outbox, monkeypatch):
    packet_job["group"] = None
    monkeypatch.setattr(publish_kafka, "producer", lambda servers, size: Producer(outbox))
    assert cli.main(["job", "run", "--job", "7"]) == ExitCode.SUCCESS
    assert outbox == []


def test_the_producer_waits_for_every_replica_and_is_idempotent(monkeypatch):
    built = {}
    monkeypatch.setattr(kafka, "KafkaProducer", lambda **config: built.update(config))
    monkeypatch.setattr(publish_kafka, "client_config", lambda servers: {})
    publish_kafka.producer(["b:9098"], 1 << 20)
    assert built["acks"] == "all" and built["enable_idempotence"] is True


def test_a_held_set_is_neither_sent_nor_stamped_by_the_sweep_and_the_others_are(outbox, held):
    held.add(7)
    producer = Producer(outbox)
    result = publish_kafka.publish(_connect, 3, producer, "topic")
    assert (result["sets"], result["held"]) == ([9], [7])
    assert [e[:2] for e in outbox if e[0] in ("send", "stamp")] == [("send", b"9:90"), ("stamp", 9)]


def test_a_held_set_is_not_sent_by_its_own_packet_job(packet_job, outbox, held, monkeypatch):
    held.add(7)
    monkeypatch.setattr(publish_kafka, "producer", lambda servers, size: Producer(outbox))
    assert cli.main(["job", "run", "--job", "7"]) == ExitCode.SUCCESS
    assert not [e for e in outbox if e[0] in ("send", "stamp")]
