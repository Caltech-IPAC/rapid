"""
The complete alert-production path, from provider data to published bytes:

    provider.get_*()   ->  Detection / ObjectRecord / ...  (providers.py)
    build_record()     ->  schema-conforming dicts, driven by the registry
    assemble_alert()   ->  one alert packet dict
    serialize_alert()  ->  Avro bytes (schema loaded from the .avsc files)
    publish_alert()    ->  Kafka topic

produce_alert() chains all of these for one source ID. This module knows
nothing about where the data lives -- it only talks to the
AlertDataProvider interface and the field registry.
"""

import dataclasses
import io
import logging
from pathlib import Path

import fastavro
import fastavro.schema

from .fields import (ALERT_FIELDS, DIA_FORCED_SOURCE_FIELDS, DIA_OBJECT_FIELDS,
                     DIA_SOURCE_FIELDS, RECORDS, VERSION, Status, is_nullable)
from .providers import Detection, ForcedPhot, ObjectRecord

logger = logging.getLogger(__name__)

# Generated .avsc files live in alerts/schema/<major>/<minor>/
SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schema"

PRV_WINDOW_DAYS = 365.25  # look-back window for previous detections

# Which normalized record each schema record is built from. The top-level
# alert record is not listed because assemble_alert() fills it directly.
BUILDER_DATA_CLASSES = {
    "diaSource": Detection,
    "diaForcedSource": ForcedPhot,
    "diaObject": ObjectRecord,
}


# ---------------------------------------------------------------------------
# Registry validation -- runs when this module is imported, so a bad
# declaration in fields.py fails loudly before any alert is built
# ---------------------------------------------------------------------------

def _available_attributes(data_cls):
    """Names a Field.attr may reference on this record class: its dataclass
    fields plus any properties (e.g. Detection.snr)."""
    field_names = {f.name for f in dataclasses.fields(data_cls)}
    property_names = {name for name, value in vars(data_cls).items()
                      if isinstance(value, property)}
    return field_names | property_names


def _validate_registry():
    problems = []
    for record in RECORDS:
        if record.name == "alert":
            continue  # filled directly by assemble_alert(), checked there
        data_cls = BUILDER_DATA_CLASSES.get(record.name)
        for f in record.fields:
            if f.status is not Status.IMPLEMENTED:
                continue  # stubs are inactive; nothing to check
            if data_cls is None:
                problems.append(
                    f"{record.name}.{f.name} is IMPLEMENTED, but the "
                    f"{record.name} record has no builder data class")
            elif f.getter is None and (f.attr or f.name) not in _available_attributes(data_cls):
                problems.append(
                    f"{record.name}.{f.name} reads {data_cls.__name__}."
                    f"{f.attr or f.name}, which does not exist")
    if problems:
        raise ValueError("fields.py registry is inconsistent:\n  "
                         + "\n  ".join(problems))


_validate_registry()


# ---------------------------------------------------------------------------
# Record builders (registry-driven)
# ---------------------------------------------------------------------------

def build_record(field_list, data):
    """Build a schema-conforming dict, enforcing each field's status."""
    out = {}
    for f in field_list:
        if f.status is Status.NOT_USED:
            continue
        if f.status is not Status.IMPLEMENTED:
            out[f.name] = None  # stubs stay null even if attr/getter is staged
            continue
        try:
            if f.getter is not None:
                value = f.getter(data)
            else:
                value = getattr(data, f.attr or f.name)
        except Exception as exc:
            raise RuntimeError(
                f"getting field {f.name!r} from {type(data).__name__} "
                f"failed: {exc}") from exc
        if value is None and not is_nullable(f.avro):
            raise ValueError(
                f"field {f.name!r} is IMPLEMENTED and non-nullable but its "
                f"value is None (was the {type(data).__name__} populated by "
                f"the provider?)")
        out[f.name] = value
    return out


def build_dia_source(detection):
    return build_record(DIA_SOURCE_FIELDS, detection)


def build_dia_object(obj):
    return build_record(DIA_OBJECT_FIELDS, obj)


def build_dia_forced_source(forced_phot):
    return build_record(DIA_FORCED_SOURCE_FIELDS, forced_phot)


# ---------------------------------------------------------------------------
# Alert assembly
# ---------------------------------------------------------------------------

def assemble_alert(provider, sid):
    """Assemble a complete alert packet for a given source ID.

    Args:
        provider: an AlertDataProvider instance.
        sid: source ID to build the alert for.

    Returns:
        dict conforming to the rapid alert schema.
    """
    detection = provider.get_detection(sid)
    obj = provider.get_object_for_source(detection)

    dia_object = None
    prv_dia_sources = None
    prv_dia_forced_sources = None

    if obj is not None:
        detection.aid = obj.aid

        prv = provider.get_prv_detections(detection, obj,
                                          window_days=PRV_WINDOW_DAYS)
        if prv:
            prv_dia_sources = [build_dia_source(p) for p in prv]

        mjds = [detection.mjdobs] + [p.mjdobs for p in prv]
        obj.first_mjd = min(mjds)
        obj.last_mjd = max(mjds)
        obj.validity_mjd = detection.mjdobs
        dia_object = build_dia_object(obj)

        forced = provider.get_forced_photometry(detection, obj)
        if forced:
            prv_dia_forced_sources = [build_dia_forced_source(fp)
                                      for fp in forced]

    cutouts = provider.get_cutouts(detection)

    alert = {
        "schemaVersion": VERSION,
        "pipelineVersion": None,
        "diaSourceId": detection.sid,
        "diaSource": build_dia_source(detection),
        "prvDiaSources": prv_dia_sources,
        "diaObject": dia_object,
        "prvDiaForcedSources": prv_dia_forced_sources,
        "ssSource": None,
        "mpc_orbits": None,
        "cutoutDifference": cutouts.difference,
        "cutoutScience": cutouts.science,
        "cutoutTemplate": cutouts.template,
        "observation_reason": None,
        "target_name": None,
    }

    # The dict above is written by hand; make sure it stays in sync with the
    # registry (fastavro would silently fill a forgotten nullable field with
    # its null default).
    expected = {f.name for f in ALERT_FIELDS if f.status is not Status.NOT_USED}
    if set(alert) != expected:
        raise RuntimeError(
            "assemble_alert() and ALERT_FIELDS in fields.py disagree: "
            f"missing keys {sorted(expected - set(alert))}, "
            f"unexpected keys {sorted(set(alert) - expected)}")

    return alert


# ---------------------------------------------------------------------------
# Serialization and publishing
# ---------------------------------------------------------------------------

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
