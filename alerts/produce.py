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

    Runs at import time so a bad declaration in param_registry.py (an attr
    naming a nonexistent record attribute, or an IMPLEMENTED param on a
    record with no builder class) fails loudly before any alert is built.

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
        The persistent object, with first_mjd/last_mjd/validity_mjd
        already filled in by assemble_alert_for_source().

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
        diaForcedSource param name -> value, per the registry (all None
        while the record is a stub).
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
    """Assemble an alert packet for a Source already in hand.

    Used directly by the batch flow (batch_produce), where iter_sources()
    has already fetched every Source on the chip -- re-querying each one
    by sid would defeat the point of batching.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Where the object, history, forced photometry, and cutouts come from.
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
        param_registry.py (guards the hand-written dict below against
        registry drift).
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

def schema_paths(version: str | None = None,
                 schema_root: str | Path = SCHEMA_ROOT) -> list[Path]:
    """Return the .avsc file paths for a schema version, in load order.

    Parameters
    ----------
    version : str, optional
        Schema version, e.g. ``"01.01"``. None (the default) means the
        version named by ``latest.txt``, falling back to the registry
        VERSION.
    schema_root : str or pathlib.Path, optional
        Directory holding ``<major>/<minor>/*.avsc`` and ``latest.txt``.

    Returns
    -------
    list of pathlib.Path
        One path per schema record, referenced records before the records
        that use them (the order fastavro needs).
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
    clear message if they have drifted. Alerts are built from the
    registry, so serializing them with stale files would otherwise
    surface as a cryptic fastavro error -- or worse, serialize "fine"
    with renamed/added params silently dropped or defaulted to null.
    An explicit version skips the check: that is for reading back
    alerts written under an older schema, not for producing new ones.

    Parameters
    ----------
    version : str, optional
        Schema version to load. None (the default) means the current
        production version, verified against the registry first.
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
    producer : confluent_kafka.Producer
        The Kafka producer to publish with.
    topic : str, optional
        Kafka topic name.
    flush : bool, optional
        Wait for delivery before returning. Right for one-off alerts;
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


@contextlib.contextmanager
def open_alert_archive(
        path: str | Path, schema: Schema | None = None,
        codec: str = "deflate") -> Iterator[fastavro.write.Writer]:
    """Open one run's alerts as one Avro object-container file.

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
    """End-to-end: assemble, serialize, and optionally publish an alert.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Where the alert's data comes from.
    sid : int
        Source ID (sources.sid).
    producer : confluent_kafka.Producer, optional
        If given, the alert is published to `topic`.
    topic : str, optional
        Kafka topic name.
    schema : dict, optional
        Parsed fastavro schema; loaded via load_schema() if not provided.
    archive : fastavro.write.Writer, optional
        Writer from open_alert_archive(); the alert is appended as one
        record.

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

    The batch unit is one exposure + SCA (chip), which is exactly what one
    diffimages.pid identifies. Use AlertDataProvider.resolve_pid(expid,
    sca) to obtain the pid for an exposure + SCA pair.

    Batch counterpart of produce_alert(): the provider fetches the
    image's DB rows and pixels up front (see
    AlertDataProvider.iter_sources), and Kafka is flushed once at the end
    instead of per message.

    Parameters
    ----------
    provider : providers.AlertDataProvider
        Must support iter_sources().
    pid : int
        Processing ID of the difference image (diffimages.pid).
    producer : confluent_kafka.Producer, optional
        If given, every alert is published to `topic`.
    topic : str, optional
        Kafka topic name.
    schema : dict, optional
        Parsed fastavro schema; loaded via load_schema() if not provided.
    archive : fastavro.write.Writer, optional
        Writer from open_alert_archive(); every alert of the run is
        appended to the one container file.

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
