"""
File:    produce.py
Author:  Emily Everetts
Date:    07/26

The complete alert-production path, from provider data to published bytes:

    provider.get_*()   ->  Source / ObjectRecord / ...  (providers.py)
    build_record()     ->  schema-conforming dicts, driven by the registry
    assemble_alert()   ->  one alert packet dict
    serialize_alert()  ->  Avro bytes (schema loaded from the .avsc files)
    publish_alert()    ->  Kafka topic

NOTE: Does not interface with data directly, only AlertDataProvider and param_registry.

Usage:

    from alerts.cli import make_provider
    from alerts.produce import batch_produce, open_alert_archive, produce_alert

    provider = make_provider()
    produce_alert(provider, sid)    # one alert, by source ID (sources.sid)
    batch_produce(provider, pid)    # every source on one difference image
                                    # (diffimages.pid)

    # Optionally publish to Kafka and/or archive the run to one Avro
    # object-container file:
    with open_alert_archive("run.avro") as archive:
        batch_produce(provider, pid, producer=producer, archive=archive)
"""

import contextlib
import dataclasses
import io
import logging
from pathlib import Path
from typing import Any, Iterator, Sequence

import fastavro
import fastavro.schema
import fastavro.write
from fastavro.types import Schema

from .param_registry import (ALERT_PARAMS, DIA_FORCED_SOURCE_PARAMS, DIA_OBJECT_PARAMS,
                     DIA_SOURCE_PARAMS, RECORDS, VERSION, Param, Status, is_nullable)
from .providers import (PRV_WINDOW_DAYS, AlertDataProvider, Source,
                        ForcedPhot, ObjectRecord)

logger = logging.getLogger(__name__)

# Generated .avsc files live in alerts/schema/<major>/<minor>/
SCHEMA_ROOT = Path(__file__).resolve().parent / "schema"

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

def _available_attributes(data_cls: type) -> set[str]:
    """Collect the names a Param.attr may reference on a record class.

    Parameters
    ----------
    data_cls : type
        A normalized-record dataclass from providers.py (Source, ...).

    Returns
    -------
    set of str
        The class's dataclass field names plus any property names
        (e.g. ``Source.snr``).
    """
    field_names = {f.name for f in dataclasses.fields(data_cls)}
    property_names = {name for name, value in vars(data_cls).items()
                      if isinstance(value, property)}
    return field_names | property_names


def _validate_registry() -> None:
    """Check every IMPLEMENTED param against its builder data class.

    Runs at import time so a bad declaration in param_registry.py fails
    before any alert is built.

    Raises
    ------
    ValueError
        Listing every inconsistent param, if there are any.
    """
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

def build_record(param_list: Sequence[Param], data: Any) -> dict[str, Any]:
    """Build a schema-conforming dict, enforcing each param's status.

    Parameters
    ----------
    param_list : tuple of param_registry.Param
        The params of one schema record, in wire order.
    data : object
        The normalized record (providers.Source, ObjectRecord, ...) the
        param values are read from.

    Returns
    -------
    dict
        Param name -> value. STUB params are always None; NOT_USED params
        are omitted.

    Raises
    ------
    RuntimeError
        If reading an IMPLEMENTED param from `data` fails.
    ValueError
        If an IMPLEMENTED, non-nullable param comes out None.
    """
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


def build_dia_source(source: Source) -> dict[str, Any]:
    """Build a diaSource record dict from a providers.Source.

    Parameters
    ----------
    source : providers.Source
        One difference-image detection.

    Returns
    -------
    dict
        diaSource param name -> value, per the registry.
    """
    return build_record(DIA_SOURCE_PARAMS, source)


def build_dia_object(obj: ObjectRecord) -> dict[str, Any]:
    """Build a diaObject record dict from a providers.ObjectRecord.

    Parameters
    ----------
    obj : providers.ObjectRecord
        The persistent AstroObject.

    Returns
    -------
    dict
        diaObject param name -> value, per the registry.
    """
    return build_record(DIA_OBJECT_PARAMS, obj)


def build_dia_forced_source(forced_phot: ForcedPhot) -> dict[str, Any]:
    """Build a diaForcedSource record dict from a providers.ForcedPhot.

    Parameters
    ----------
    forced_phot : providers.ForcedPhot
        One forced-photometry measurement.

    Returns
    -------
    dict
        diaForcedSource param name -> value, per the registry.
    """
    return build_record(DIA_FORCED_SOURCE_PARAMS, forced_phot)


# ---------------------------------------------------------------------------
# Alert assembly
# ---------------------------------------------------------------------------

def assemble_alert(provider: AlertDataProvider, sid: int) -> dict[str, Any]:
    """Assemble a complete alert packet for a given source ID.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Where the detection, object, history, and cutouts come from.
    sid : int
        Source ID (sources.sid) to build the alert for.

    Returns
    -------
    dict
        Alert packet conforming to the rapid alert schema.
    """
    return assemble_alert_for_source(provider, provider.get_detection(sid))


def assemble_alert_for_source(provider: AlertDataProvider,
                              source: Source) -> dict[str, Any]:
    """Assemble an alert packet for a Source data object.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Data provider for object, history, forced photometry, and cutouts.
    source : providers.Source
        The triggering detection.

    Returns
    -------
    dict
        Alert packet conforming to the rapid alert schema.

    Raises
    ------
    RuntimeError
        If the assembled packet's keys disagree with ALERT_PARAMS in
        param_registry.py (guards against registry drift).
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

        # get_prv_detections() guarantees every p.mjdobs < source.mjdobs
        # (strict prior, ruled 2026-08-13), so last_mjd == source.mjdobs
        # here and can never postdate validity_mjd below.
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
        "cutoutReference": cutouts.template,
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

def schema_paths(version: str | None = None,
                 schema_root: str | Path = SCHEMA_ROOT) -> list[Path]:
    """Return the .avsc file paths for a schema version, in load order.

    Parameters
    ----------
    version : str, optional
        Schema version, e.g. ``"01.01"``. Defaults to ``latest.txt``.
    schema_root : str or pathlib.Path, optional
        Directory holding ``<major>/<minor>/*.avsc`` and ``latest.txt``.

    Returns
    -------
    list of pathlib.Path
        One path per schema record, in the correct referenced order.
    """
    if version is None:
        latest = Path(schema_root) / "latest.txt"
        version = latest.read_text().strip() if latest.exists() else VERSION
    major, minor = version.split(".")
    namespace = f"rapid.v{major}_{minor}"
    schema_dir = Path(schema_root) / major / minor
    return [schema_dir / f"{namespace}.{record.name}.avsc"
            for record in RECORDS]


def load_schema(version: str | None = None,
                schema_root: str | Path = SCHEMA_ROOT) -> Schema:
    """Load and parse the RAPID alert schema.

    With no explicit version (the production path), the .avsc files are
    first verified against param_registry.py and loading fails with a
    clear message if they have drifted. An explicit version skips the
    check: that is for reading back older alerts, not producing new ones.

    Parameters
    ----------
    version : str, optional
        Schema version to load. Defaults to current production version,
        verified against the registry.
    schema_root : str or pathlib.Path, optional
        Directory holding ``<major>/<minor>/*.avsc`` and ``latest.txt``.

    Returns
    -------
    dict
        Parsed fastavro schema for the top-level alert record.

    Raises
    ------
    RuntimeError
        If the production .avsc files are out of sync with
        param_registry.py.
    """
    if version is None:
        from .gen_schema import schema_problems
        problems = schema_problems(schema_root=schema_root)
        if problems:
            raise RuntimeError(
                "Avro schema files are stale (out of sync with "
                "param_registry.py):\n  " + "\n  ".join(problems)
                + "\nRegenerate them with: python -m alerts.gen_schema")
        version = VERSION
    paths = [str(p) for p in schema_paths(version, schema_root)]
    return fastavro.schema.load_schema_ordered(paths)


def serialize_alert(alert_dict: dict[str, Any],
                    schema: Schema | None = None) -> bytes:
    """Serialize an alert dict to Avro bytes.

    Parameters
    ----------
    alert_dict : dict
        Alert packet conforming to the rapid alert schema.
    schema : dict, optional
        Parsed fastavro schema; loaded via load_schema() if not provided.

    Returns
    -------
    bytes
        The Avro-serialized alert (schemaless encoding: no embedded
        schema, as sent over Kafka).
    """
    if schema is None:
        schema = load_schema()
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, alert_dict)
    return buf.getvalue()


def publish_alert(alert_bytes: bytes, producer: Any, topic: str = "alerts",
                  flush: bool = True) -> None:
    """Publish serialized alert bytes to a Kafka topic.

    Parameters
    ----------
    alert_bytes : bytes
        Avro-serialized alert.
    producer : kafka_producer.GlueFramingProducer
        The Kafka producer to publish with.
    topic : str, optional
        Kafka topic name.
    flush : bool, optional
        Wait for delivery before returning. True for one-off alerts,
        False for batch callers.
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


@contextlib.contextmanager
def open_alert_archive(
        path: str | Path, schema: Schema | None = None,
        codec: str = "deflate") -> Iterator[fastavro.write.Writer]:
    """Open one run's batch alerts as an Avro object-container file.

    The container format embeds the schema (and codec) in the file header,
    so the file is self-describing: read it back with
    ``fastavro.reader(open(path, "rb"))`` and no .avsc files.

    Parameters
    ----------
    path : str or pathlib.Path
        File to write.
    schema : dict, optional
        Parsed fastavro schema; loaded via load_schema() if not provided.
    codec : str, optional
        Block compression: "deflate" (the default, and part of the MAST
        delivery contract) or "null" for raw storage.

    Yields
    ------
    fastavro.write.Writer
        Pass it to produce_alert()/batch_produce() as `archive` and each
        alert is appended as one record. Flushed on exit even after an
        error, so completed blocks stay readable.
    """
    if schema is None:
        schema = load_schema()
    # TODO: level 1 keeps most of deflate's size win at a fraction of the
    # CPU (level 9ish default measured ~12 ms/alert vs ~1.3 uncompressed);
    # revisit codec and level when the MAST delivery requirements are
    # nailed down.
    with open(path, "wb") as f:
        writer = fastavro.write.Writer(f, schema, codec=codec,
                                       compression_level=1)
        try:
            yield writer
        finally:
            writer.flush()  # even on error: completed blocks stay readable


def produce_alert(provider: AlertDataProvider, sid: int,
                  producer: Any = None, topic: str = "alerts",
                  schema: Schema | None = None,
                  archive: fastavro.write.Writer | None = None) -> bytes:
    """Assemble, serialize, and optionally publish an alert.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Data provider object.
    sid : int
        Source ID (sources.sid).
    producer : kafka_producer.GlueFramingProducer, optional
        If given, the alert is published to `topic`.
    topic : str, optional
        Kafka topic name.
    schema : dict, optional
        Parsed fastavro schema; loaded via load_schema() if not provided.
    archive : fastavro.write.Writer, optional
        Alert writer; the alert is appended as one record.

    Returns
    -------
    bytes
        The serialized alert.
    """
    if schema is None:
        schema = load_schema()
    alert_dict = assemble_alert(provider, sid)
    alert_bytes = serialize_alert(alert_dict, schema=schema)
    logger.info("Alert for sid=%d serialized (%d bytes)", sid, len(alert_bytes))

    if producer is not None:
        publish_alert(alert_bytes, producer, topic=topic)
    if archive is not None:
        archive.write(alert_dict)

    return alert_bytes


def batch_produce(provider: AlertDataProvider, pid: int,
                  producer: Any = None, topic: str = "alerts",
                  schema: Schema | None = None,
                  archive: fastavro.write.Writer | None = None) -> int:
    """Produce alerts for every source on one difference image.

    Batch counterpart of produce_alert(): the provider fetches the
    image's DB rows and pixels up front (see AlertDataProvider.iter_sources),
    and Kafka is flushed once at the end instead of per message.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Must support iter_sources().
    pid : int
        Processing ID of the difference image (diffimages.pid).
    producer : kafka_producer.GlueFramingProducer, optional
        If given, every alert is published to `topic`.
    topic : str, optional
        Kafka topic name.
    schema : dict, optional
        Parsed fastavro schema; loaded via load_schema() if not provided.
    archive : fastavro.write.Writer, optional
        Alert writer; every alert of the run is appended to a container file.

    Returns
    -------
    int
        The number of alerts produced.
    """
    if schema is None:
        schema = load_schema()

    count = 0
    for source in provider.iter_sources(pid):
        alert_dict = assemble_alert_for_source(provider, source)
        alert_bytes = serialize_alert(alert_dict, schema=schema)
        if producer is not None:
            publish_alert(alert_bytes, producer, topic=topic, flush=False)
        if archive is not None:
            archive.write(alert_dict)
        count += 1

    if producer is not None:
        producer.flush()
    logger.info("pid=%s: %d alerts produced", pid, count)
    return count
