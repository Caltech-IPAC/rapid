"""
The complete alert-production path, from provider data to published bytes:

    provider.get_*()   ->  Source / ObjectRecord / ...  (providers.py)
    build_record()     ->  schema-conforming dicts, driven by the registry
    assemble_alert()   ->  one alert packet dict
    serialize_alert()  ->  Avro bytes (schema loaded from the .avsc files)
    publish_alert()    ->  Kafka topic

produce_alert() chains all of these for one source ID; batch_produce()
does the same for every source on one difference image -- one exposure +
SCA, keyed by its processing ID (diffimages.pid) -- letting the provider
prefetch that image's data once. This module knows nothing about where
the data lives -- it only talks to the AlertDataProvider interface and
the param registry.
"""

import dataclasses
import io
import logging
from pathlib import Path

import fastavro
import fastavro.schema

from .param_registry import (ALERT_PARAMS, DIA_FORCED_SOURCE_PARAMS, DIA_OBJECT_PARAMS,
                     DIA_SOURCE_PARAMS, RECORDS, VERSION, Status, is_nullable)
from .providers import PRV_WINDOW_DAYS, Source, ForcedPhot, ObjectRecord

logger = logging.getLogger(__name__)

# Generated .avsc files live in alerts/schema/<major>/<minor>/
SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schema"

# Which normalized record each schema record is built from. The top-level
# alert record is not listed because assemble_alert() fills it directly.
BUILDER_DATA_CLASSES = {
    "diaSource": Source,
    "diaForcedSource": ForcedPhot,
    "diaObject": ObjectRecord,
}


# ---------------------------------------------------------------------------
# Registry validation -- runs when this module is imported, so a bad
# declaration in param_registry.py fails loudly before any alert is built
# ---------------------------------------------------------------------------

def _available_attributes(data_cls):
    """Names a Param.attr may reference on this record class: its dataclass
    fields plus any properties (e.g. Source.snr)."""
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
        for p in record.params:
            if p.status is not Status.IMPLEMENTED:
                continue  # stubs are inactive; nothing to check
            if data_cls is None:
                problems.append(
                    f"{record.name}.{p.name} is IMPLEMENTED, but the "
                    f"{record.name} record has no builder data class")
            elif p.getter is None and (p.attr or p.name) not in _available_attributes(data_cls):
                problems.append(
                    f"{record.name}.{p.name} reads {data_cls.__name__}."
                    f"{p.attr or p.name}, which does not exist")
    if problems:
        raise ValueError("param_registry.py is inconsistent:\n  "
                         + "\n  ".join(problems))


_validate_registry()


# ---------------------------------------------------------------------------
# Record builders (registry-driven)
# ---------------------------------------------------------------------------

def build_record(param_list, data):
    """Build a schema-conforming dict, enforcing each param's status."""
    out = {}
    for p in param_list:
        if p.status is Status.NOT_USED:
            continue
        if p.status is not Status.IMPLEMENTED:
            out[p.name] = None  # stubs stay null even if attr/getter is staged
            continue
        try:
            if p.getter is not None:
                value = p.getter(data)
            else:
                value = getattr(data, p.attr or p.name)
        except Exception as exc:
            raise RuntimeError(
                f"getting param {p.name!r} from {type(data).__name__} "
                f"failed: {exc}") from exc
        if value is None and not is_nullable(p.avro):
            raise ValueError(
                f"param {p.name!r} is IMPLEMENTED and non-nullable but its "
                f"value is None (was the {type(data).__name__} populated by "
                f"the provider?)")
        out[p.name] = value
    return out


def build_dia_source(source):
    return build_record(DIA_SOURCE_PARAMS, source)


def build_dia_object(obj):
    return build_record(DIA_OBJECT_PARAMS, obj)


def build_dia_forced_source(forced_phot):
    return build_record(DIA_FORCED_SOURCE_PARAMS, forced_phot)


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
    return assemble_alert_for_source(provider, provider.get_detection(sid))


def assemble_alert_for_source(provider, source):
    """Assemble an alert packet for a Source already in hand.

    Used directly by the batch flow (batch_produce), where iter_sources()
    has already fetched every Source on the chip -- re-querying each one
    by sid would defeat the point of batching.
    """
    obj = provider.get_object_for_source(source)

    dia_object = None
    prv_dia_sources = None
    prv_dia_forced_sources = None

    if obj is not None:
        source.aid = obj.aid

        prv = provider.get_prv_detections(source, obj,
                                          window_days=PRV_WINDOW_DAYS)
        if prv:
            prv_dia_sources = [build_dia_source(p) for p in prv]

        mjds = [source.mjdobs] + [p.mjdobs for p in prv]
        obj.first_mjd = min(mjds)
        obj.last_mjd = max(mjds)
        obj.validity_mjd = source.mjdobs
        dia_object = build_dia_object(obj)

        forced = provider.get_forced_photometry(source, obj)
        if forced:
            prv_dia_forced_sources = [build_dia_forced_source(fp)
                                      for fp in forced]

    cutouts = provider.get_cutouts(source)

    alert = {
        "schemaVersion": VERSION,
        "pipelineVersion": None,
        "diaSourceId": source.sid,
        "diaSource": build_dia_source(source),
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
    # registry (fastavro would silently fill a forgotten nullable param with
    # its null default).
    expected = {p.name for p in ALERT_PARAMS if p.status is not Status.NOT_USED}
    if set(alert) != expected:
        raise RuntimeError(
            "assemble_alert() and ALERT_PARAMS in param_registry.py disagree: "
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
    """Load and parse the RAPID alert schema.

    With no explicit version (the production path), the .avsc files are
    first verified against param_registry.py and loading fails with a
    clear message if they have drifted. Alerts are built from the
    registry, so serializing them with stale files would otherwise
    surface as a cryptic fastavro error -- or worse, serialize "fine"
    with renamed/added params silently dropped or defaulted to null.
    An explicit version skips the check: that is for reading back
    alerts written under an older schema, not for producing new ones.
    """
    if version is None:
        from .gen_schema import schema_problems
        problems = schema_problems(schema_root=schema_root)
        if problems:
            raise RuntimeError(
                "Avro schema files are stale (out of sync with "
                "param_registry.py):\n  " + "\n  ".join(problems)
                + "\nRegenerate them with: python -m rapid_alerts.gen_schema")
        version = VERSION
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


def publish_alert(alert_bytes, producer, topic="alerts", flush=True):
    """Publish serialized alert bytes to a Kafka topic.

    Args:
        alert_bytes: Avro-serialized alert bytes.
        producer: confluent_kafka.Producer instance.
        topic: Kafka topic name.
        flush: wait for delivery before returning. Right for one-off alerts;
            batch callers (batch_produce) pass False and flush once at the
            end instead.
    """
    def delivery_callback(err, msg):
        if err:
            logger.error("Message delivery failed: %s", err)
        else:
            logger.info("Alert delivered to topic %s [%d]",
                        msg.topic(), msg.partition())

    producer.produce(topic, alert_bytes, callback=delivery_callback)
    if flush:
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


def batch_produce(provider, pid, producer=None, topic="alerts", schema=None):
    """Produce alerts for every source on one difference image -- the
    batch unit is one exposure + SCA (chip), which is exactly what one
    diffimages.pid identifies. Use DatabaseProvider.resolve_pid(expid,
    sca) to obtain the pid for an exposure + SCA pair.

    Batch counterpart of produce_alert(): the provider fetches the
    image's DB rows and pixels up front (see
    DatabaseProvider.iter_sources), and Kafka is flushed once at the end
    instead of per message.

    Args:
        provider: an AlertDataProvider instance that supports iter_sources().
        pid: processing ID of the difference image (diffimages.pid).
        producer: optional confluent_kafka.Producer instance.
        topic: Kafka topic name.
        schema: parsed fastavro schema (loaded if not provided).

    Returns:
        the number of alerts produced.
    """
    if schema is None:
        schema = load_schema()

    count = 0
    for source in provider.iter_sources(pid):
        alert_dict = assemble_alert_for_source(provider, source)
        alert_bytes = serialize_alert(alert_dict, schema=schema)
        if producer is not None:
            publish_alert(alert_bytes, producer, topic=topic, flush=False)
        count += 1

    if producer is not None:
        producer.flush()
    logger.info("pid=%s: %d alerts produced", pid, count)
    return count
