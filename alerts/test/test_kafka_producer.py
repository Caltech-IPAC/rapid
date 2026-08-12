"""Producer tests: Glue framing and the injected-producer seam.

Everything here runs with no broker, no registry, and no AWS
credentials — that is the point of the dependency-injection seam, and
these tests are what proves the seam actually holds. If a change makes
`GlueFramingProducer` reach for boto3 or a socket at construction time,
these tests fail rather than hanging.
"""

import struct
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alerts.kafka_producer import (DEFAULT_REGISTRY, GLUE_HEADER_LEN,
                                   GLUE_HEADER_VERSION_BYTE,
                                   GlueFramingProducer, GlueSchemaRegistry,
                                   current_schema_version, frame_alert,
                                   schema_name_for_topic, unframe_alert)
from alerts.produce import publish_alert

SCHEMA_VERSION_ID = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"


class FakeTransport:
    """Stands in for kafka-python's KafkaProducer."""

    def __init__(self):
        self.sent = []          # (topic, wire_bytes)
        # The message keys, recorded alongside. `Transport.send` gained an
        # optional `key` in brief E — the publisher sets every message's key to
        # its `alert_id` so at-least-once delivery stays deduplicable — and a
        # double that silently dropped it could not tell whether a caller had
        # set one.
        self.keys = []
        self.flushes = 0
        self.closed = False

    def send(self, topic, value, key=None):
        self.sent.append((topic, value))
        self.keys.append(key)
        return FakeFuture()

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class FakeFuture:
    """kafka-python's FutureRecordMetadata, only as far as callbacks go."""

    def __init__(self):
        self.callbacks = []
        self.errbacks = []

    def add_callback(self, fn):
        self.callbacks.append(fn)

    def add_errback(self, fn):
        self.errbacks.append(fn)


class FakeRegistry:
    """Resolves any schema name to one fixed version id, and counts calls."""

    def __init__(self, version_id=SCHEMA_VERSION_ID):
        self.version_id = version_id
        self.lookups = []

    def schema_version_id(self, schema_name):
        self.lookups.append(schema_name)
        return self.version_id


@pytest.fixture
def producer():
    transport = FakeTransport()
    return GlueFramingProducer(transport=transport, registry=FakeRegistry(),
                               schema_version="00.01")


# ---------------------------------------------------------------------------
# Framing: the wire format the archive sink has to be able to read back
# ---------------------------------------------------------------------------

def test_frame_prepends_glue_header():
    framed = frame_alert(b"payload", SCHEMA_VERSION_ID)
    version_byte, compression, raw_uuid = struct.unpack(">BB16s",
                                                        framed[:GLUE_HEADER_LEN])
    assert version_byte == GLUE_HEADER_VERSION_BYTE
    assert compression == 0x00
    assert uuid.UUID(bytes=raw_uuid) == uuid.UUID(SCHEMA_VERSION_ID)
    assert framed[GLUE_HEADER_LEN:] == b"payload"


def test_frame_unframe_round_trips():
    version_id, payload = unframe_alert(frame_alert(b"alert-bytes",
                                                    SCHEMA_VERSION_ID))
    assert version_id == uuid.UUID(SCHEMA_VERSION_ID)
    assert payload == b"alert-bytes"


def test_frame_accepts_uuid_object_and_string_identically():
    as_string = frame_alert(b"x", SCHEMA_VERSION_ID)
    as_uuid = frame_alert(b"x", uuid.UUID(SCHEMA_VERSION_ID))
    assert as_string == as_uuid


def test_unframe_rejects_unframed_bytes():
    # A message the archive sink cannot attribute to a schema is an error
    # to surface, never bytes to guess at.
    with pytest.raises(ValueError, match="too short"):
        unframe_alert(b"short")


def test_unframe_rejects_foreign_header_version():
    bad = bytes([0x99, 0x00]) + uuid.UUID(SCHEMA_VERSION_ID).bytes + b"payload"
    with pytest.raises(ValueError, match="header version"):
        unframe_alert(bad)


# ---------------------------------------------------------------------------
# Schema version: framed against the tree's current version, not a constant
# ---------------------------------------------------------------------------

def test_current_schema_version_reads_latest_txt(tmp_path):
    (tmp_path / "latest.txt").write_text("07.03\n")
    assert current_schema_version(tmp_path) == "07.03"


def test_current_schema_version_tracks_the_repo_tree():
    # The real tree, so a schema bump that forgets latest.txt is caught
    # here rather than at publish time.
    root = Path(__file__).resolve().parents[1] / "schema"
    assert current_schema_version(root) == (root / "latest.txt").read_text().strip()


def test_missing_latest_txt_is_an_error_not_a_default(tmp_path):
    with pytest.raises(FileNotFoundError):
        current_schema_version(tmp_path)


def test_schema_name_is_the_topic_name():
    assert schema_name_for_topic("rapid.internal.alerts.v1") \
        == "rapid.internal.alerts.v1"


# ---------------------------------------------------------------------------
# The seam: produce.py's publish path drives this producer unchanged
# ---------------------------------------------------------------------------

def test_produce_frames_and_sends(producer):
    producer.produce("rapid.test.alerts", b"avro-bytes")
    (topic, wire), = producer.transport.sent
    assert topic == "rapid.test.alerts"
    version_id, payload = unframe_alert(wire)
    assert payload == b"avro-bytes"
    assert version_id == uuid.UUID(SCHEMA_VERSION_ID)


def test_produce_looks_the_schema_up_by_topic(producer):
    producer.produce("rapid.internal.alerts.wide", b"x")
    assert producer.registry.lookups == ["rapid.internal.alerts.wide"]


def test_produce_passes_the_message_key_through(producer):
    # Brief E: `Transport.send` gained a key, because at-least-once delivery
    # is only deduplicable if every copy of a packet arrives under the same
    # one. Asserted at the TRANSPORT, which is where the key either reaches
    # the wire or is quietly dropped.
    producer.produce("rapid.test.alerts", b"avro-bytes", key=b"sha256:abc")
    assert producer.transport.keys == [b"sha256:abc"]


def test_produce_without_a_key_sends_none(producer):
    # The pre-E call sites pass no key and must keep working unchanged —
    # kafka-python's own default. A fake that required one would make this
    # widening look like a breaking change.
    producer.produce("rapid.test.alerts", b"avro-bytes")
    assert producer.transport.keys == [None]


def test_publish_alert_drives_the_producer_unchanged(producer):
    # publish_alert is produce.py's, untouched by the client swap: this is
    # the assertion that the DI seam held across the migration.
    publish_alert(b"serialized-alert", producer, topic="rapid.test.alerts")
    assert len(producer.transport.sent) == 1
    assert producer.transport.flushes == 1


def test_publish_alert_without_flush_defers(producer):
    publish_alert(b"a", producer, topic="t", flush=False)
    assert producer.transport.flushes == 0


def test_batch_publish_flushes_once(producer):
    for i in range(5):
        publish_alert(f"alert-{i}".encode(), producer, topic="t", flush=False)
    producer.flush()
    assert len(producer.transport.sent) == 5
    assert producer.transport.flushes == 1


class ResolvingFuture:
    """A future that reports its outcome, as kafka-python's does.

    FakeFuture deliberately does not implement succeeded()/exception —
    it stands in for the callback surface only. These tests need the
    delivery-outcome surface, which is what flush() now inspects.
    """

    def __init__(self, error=None):
        self.error = error
        self.exception = error

    def succeeded(self):
        return self.error is None


class ResolvingTransport(FakeTransport):
    """FakeTransport whose sends resolve to a stated outcome."""

    def __init__(self, error=None):
        super().__init__()
        self.error = error

    def send(self, topic, value, key=None):
        self.sent.append((topic, value))
        self.keys.append(key)
        return ResolvingFuture(self.error)


def _resolving_producer(error=None):
    return GlueFramingProducer(transport=ResolvingTransport(error),
                               registry=FakeRegistry(),
                               schema_version="00.01")


def test_flush_raises_when_delivery_failed():
    # The regression this guards: flush() used to drain the futures and
    # discard them unexamined, so a run in which EVERY send failed
    # reported a clean publish. Found live 2026-08-04 — the producer
    # reported publishing its alerts while the topic's end offset stayed
    # at 0 (ClusterAuthorizationFailedError on InitProducerId).
    producer = _resolving_producer(error=RuntimeError("broker said no"))
    producer.produce("rapid.test.alerts", b"payload")
    with pytest.raises(RuntimeError, match="1 of 1 alert"):
        producer.flush()


def test_flush_reports_how_many_of_how_many_failed():
    producer = _resolving_producer(error=RuntimeError("broker said no"))
    for _ in range(3):
        producer.produce("rapid.test.alerts", b"payload")
    with pytest.raises(RuntimeError, match="3 of 3 alert"):
        producer.flush()


def test_flush_is_quiet_when_every_delivery_succeeded():
    producer = _resolving_producer()
    producer.produce("rapid.test.alerts", b"payload")
    producer.flush()          # must not raise
    assert producer.transport.flushes == 1


def test_flush_clears_pending_so_a_failure_is_reported_once():
    # A second flush with nothing newly produced must be quiet — otherwise
    # one failure would raise on every subsequent flush of the process.
    producer = _resolving_producer(error=RuntimeError("broker said no"))
    producer.produce("rapid.test.alerts", b"payload")
    with pytest.raises(RuntimeError):
        producer.flush()
    producer.flush()          # must not raise


def test_delivery_callback_is_attached_to_the_future(producer):
    # kafka-python has no per-message callback argument; produce.py passes
    # one, so it must land on the future or delivery errors go unreported.
    delivered = []
    producer.produce("t", b"x", callback=lambda err, msg: delivered.append((err, msg)))
    future = producer._pending[0]
    assert len(future.callbacks) == 1 and len(future.errbacks) == 1
    future.callbacks[0]("metadata")
    future.errbacks[0](RuntimeError("boom"))
    assert delivered[0] == (None, "metadata")
    assert isinstance(delivered[1][0], RuntimeError)


def test_close_releases_the_transport(producer):
    with producer:
        producer.produce("t", b"x")
    assert producer.transport.closed


# ---------------------------------------------------------------------------
# Registry: lookups cached, auto-registration off
# ---------------------------------------------------------------------------

class FakeGlueClient:
    def __init__(self, version_id=SCHEMA_VERSION_ID, raise_missing=False):
        self.version_id = version_id
        self.raise_missing = raise_missing
        self.calls = []

    def get_schema_version(self, SchemaId, SchemaVersionNumber):  # noqa: N803
        self.calls.append(SchemaId)
        if self.raise_missing:
            raise type("EntityNotFoundException", (Exception,), {})()
        return {"SchemaVersionId": self.version_id}


def test_registry_caches_lookups():
    client = FakeGlueClient()
    registry = GlueSchemaRegistry(client=client)
    assert registry.schema_version_id("topic-a") == SCHEMA_VERSION_ID
    assert registry.schema_version_id("topic-a") == SCHEMA_VERSION_ID
    assert len(client.calls) == 1          # immutable version, one lookup


def test_registry_queries_the_contract_registry():
    client = FakeGlueClient()
    GlueSchemaRegistry(client=client).schema_version_id("topic-a")
    assert client.calls[0] == {"RegistryName": DEFAULT_REGISTRY,
                               "SchemaName": "topic-a"}


def test_unregistered_schema_raises_rather_than_registering():
    # Auto-registration is off by contract; an unknown schema is a
    # deployment fault, and the producer must not create it.
    client = FakeGlueClient(raise_missing=True)
    registry = GlueSchemaRegistry(client=client)
    with pytest.raises(KeyError, match="not registered"):
        registry.schema_version_id("never-registered")


def test_constructing_a_producer_makes_no_aws_call():
    # Guards the seam itself: a lazy client that stops being lazy would
    # make every unit test require credentials.
    registry = GlueSchemaRegistry()
    assert registry._client is None
    GlueFramingProducer(transport=FakeTransport(), registry=registry,
                        schema_version="00.01")
    assert registry._client is None
