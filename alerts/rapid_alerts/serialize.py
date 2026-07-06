"""
Avro serialization and Kafka publishing for RAPID alerts.
"""

import io
import logging
from pathlib import Path

import fastavro
import fastavro.schema

from .assemble import assemble_alert
from .fields import RECORDS, VERSION

logger = logging.getLogger(__name__)

# Generated .avsc files live in alerts/schema/<major>/<minor>/
SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schema"


def schema_paths(version=None, schema_root=SCHEMA_ROOT):
    """Return the .avsc file paths for a schema version, in load order."""
    if version is None:
        latest = Path(schema_root) / "latest.txt"
        version = latest.read_text().strip() if latest.exists() else VERSION
    major, minor = version.split(".")
    namespace = f"rapid.v{major}_{minor}"
    schema_dir = Path(schema_root) / major / minor
    return [schema_dir / f"{namespace}.{record.name}.avsc"
            for record in RECORDS]


def load_schema(version=None, schema_root=SCHEMA_ROOT):
    """Load and parse the RAPID alert schema."""
    paths = [str(p) for p in schema_paths(version, schema_root)]
    return fastavro.schema.load_schema_ordered(paths)


def serialize_alert(alert_dict, schema=None):
    """Serialize an alert dict to Avro bytes.

    Args:
        alert_dict: dict conforming to the rapid alert schema.
        schema: parsed fastavro schema (loaded if not provided).

    Returns:
        bytes containing the Avro-serialized alert.
    """
    if schema is None:
        schema = load_schema()
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, alert_dict)
    return buf.getvalue()


def publish_alert(alert_bytes, producer, topic="alerts"):
    """Publish serialized alert bytes to a Kafka topic.

    Args:
        alert_bytes: Avro-serialized alert bytes.
        producer: confluent_kafka.Producer instance.
        topic: Kafka topic name.
    """
    def delivery_callback(err, msg):
        if err:
            logger.error("Message delivery failed: %s", err)
        else:
            logger.info("Alert delivered to topic %s [%d]",
                        msg.topic(), msg.partition())

    producer.produce(topic, alert_bytes, callback=delivery_callback)
    producer.flush()


def produce_alert(provider, sid, producer=None, topic="alerts", schema=None):
    """End-to-end: assemble, serialize, and optionally publish an alert.

    Args:
        provider: an AlertDataProvider instance.
        sid: source ID.
        producer: optional confluent_kafka.Producer instance.
        topic: Kafka topic name.
        schema: parsed fastavro schema (loaded if not provided).

    Returns:
        bytes containing the serialized alert.
    """
    if schema is None:
        schema = load_schema()
    alert_dict = assemble_alert(provider, sid)
    alert_bytes = serialize_alert(alert_dict, schema=schema)
    logger.info("Alert for sid=%d serialized (%d bytes)", sid, len(alert_bytes))

    if producer is not None:
        publish_alert(alert_bytes, producer, topic=topic)

    return alert_bytes
