#!/usr/bin/env python
"""
RAPID Alert Assembly and Production Module

Queries the RAPID database for a source detection, assembles an LSST-compatible
alert packet, serializes it with fastavro, and optionally publishes to Kafka.

Uses the rapid.v01_00 schema (LSST alert_packet v10.0 compatible).
"""

import io
import json
import os
import sys
import struct
import logging
from pathlib import Path

import fastavro
import fastavro.schema

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Schema directory relative to this file
SCHEMA_DIR = Path(__file__).parent / "schema" / "01" / "00"

# Ordered list of schema files for fastavro
SCHEMA_FILES = [
    SCHEMA_DIR / "rapid.v01_00.diaSource.avsc",
    SCHEMA_DIR / "rapid.v01_00.diaForcedSource.avsc",
    SCHEMA_DIR / "rapid.v01_00.diaObject.avsc",
    SCHEMA_DIR / "rapid.v01_00.ssSource.avsc",
    SCHEMA_DIR / "rapid.v01_00.mpc_orbits.avsc",
    SCHEMA_DIR / "rapid.v01_00.alert.avsc",
]

# Roman filter names
ROMAN_FILTERS = ["F062", "F087", "F106", "F129", "F146", "F158", "F184", "F213"]


def load_schema():
    """Load and parse the RAPID v1.0 alert schema."""
    schema_paths = [str(p) for p in SCHEMA_FILES]
    return fastavro.schema.load_schema_ordered(schema_paths)


def build_dia_source(row, filter_name=None):
    """Build a diaSource record from a database row dict.

    Args:
        row: dict with keys from the sources/exposures/diffimages join.
        filter_name: string filter name (e.g. "F158").

    Returns:
        dict conforming to rapid.v01_00.diaSource.
    """
    snr = None
    if row.get("fluxfit") is not None and row.get("fluxerr") is not None:
        if row["fluxerr"] != 0:
            snr = row["fluxfit"] / row["fluxerr"]

    return {
        "diaSourceId": row["sid"],
        "visit": int(row.get("expid", 0)),
        "detector": int(row.get("sca", 0)),
        "diaObjectId": row.get("aid"),
        "ssObjectId": None,
        "parentDiaSourceId": None,
        "midpointMjdTai": float(row["mjdobs"]),
        "ra": float(row["ra"]),
        "dec": float(row["dec"]),
        "raErr": None,
        "decErr": None,
        "x": float(row.get("xfit", 0.0)),
        "y": float(row.get("yfit", 0.0)),
        "xErr": row.get("xerr"),
        "yErr": row.get("yerr"),
        "band": filter_name,
        "psfFlux": row.get("fluxfit"),
        "psfFluxErr": row.get("fluxerr"),
        "snr": snr,
        "extendedness": None,
        "reliability": None,
        "flags": int(row.get("flags", 0)),
        # LSST stubs
        "apFlux": None, "apFluxErr": None,
        "trailFlux": None, "trailFluxErr": None,
        "trailLength": None, "trailAngle": None,
        "scienceFlux": None, "scienceFluxErr": None,
        "templateFlux": None, "templateFluxErr": None,
        "dipoleMeanFlux": None, "dipoleFluxErr": None,
        "dipoleLength": None, "dipoleAngle": None,
        "ixx": None, "iyy": None, "ixy": None,
        "ixxErr": None, "iyyErr": None, "ixyErr": None,
        "pixelFlags_saturated": None,
        "pixelFlags_bad": None,
        "pixelFlags_edge": None,
        "pixelFlags_cr": None,
        "timeProcessedMjdTai": None,
        "timeWithdrawnMjdTai": None,
        # Roman-specific
        "sca": int(row.get("sca", 0)),
        "field": int(row.get("field", 0)),
        "hp6": int(row.get("hp6", 0)),
        "hp9": int(row.get("hp9", 0)),
        "pid": int(row.get("pid", 0)),
        "expid": int(row.get("expid", 0)),
        "isdiffpos": bool(row.get("isdiffpos", True)),
        "qfit": row.get("qfit"),
        "cfit": row.get("cfit"),
        "redchi": row.get("redchi"),
        "npixfit": row.get("npixfit"),
        "sharpness": row.get("sharpness"),
        "roundness1": row.get("roundness1"),
        "roundness2": row.get("roundness2"),
        "peak": row.get("peak"),
    }


def build_dia_forced_source(row, object_ra, object_dec, filter_name=None):
    """Build a diaForcedSource record from a forced-photometry row dict.

    Args:
        row: dict with forced photometry columns.
        object_ra: right ascension of the parent object.
        object_dec: declination of the parent object.
        filter_name: string filter name.

    Returns:
        dict conforming to rapid.v01_00.diaForcedSource.
    """
    return {
        "diaForcedSourceId": int(row.get("forced_id", 0)),
        "diaObjectId": int(row["aid"]),
        "visit": int(row.get("expid", 0)),
        "detector": int(row.get("sca", 0)),
        "ra": float(object_ra),
        "dec": float(object_dec),
        "band": filter_name,
        "psfFlux": row.get("forcediffimflux"),
        "psfFluxErr": row.get("forcediffimfluxunc"),
        "scienceFlux": None,
        "scienceFluxErr": None,
        "midpointMjdTai": float(row["mjdobs"]),
        "timeProcessedMjdTai": float(row.get("time_processed", row["mjdobs"])),
        "timeWithdrawnMjdTai": None,
    }


def build_dia_object(row, first_mjd=None, last_mjd=None, validity_mjd=0.0):
    """Build a diaObject record from an astroobjects row dict.

    Args:
        row: dict with astroobjects table columns.
        first_mjd: earliest MJD from source history (computed by caller).
        last_mjd: latest MJD from source history (computed by caller).
        validity_mjd: MJD the alert is valid from (triggering source time).

    Returns:
        dict conforming to rapid.v01_00.diaObject.
    """
    obj = {
        "diaObjectId": int(row["aid"]),
        "ra": float(row["ra0"]),
        "dec": float(row["dec0"]),
        "raErr": None,
        "decErr": None,
        "nDiaSources": int(row.get("nsources", 0)),
        "firstDiaSourceMjdTai": first_mjd,
        "lastDiaSourceMjdTai": last_mjd,
        "validityStartMjdTai": float(validity_mjd),
    }
    # Initialize all per-filter fields to null
    for filt in ROMAN_FILTERS:
        obj[f"{filt}PsfFluxMean"] = None
        obj[f"{filt}PsfFluxSigma"] = None
        obj[f"{filt}PsfFluxNdata"] = None
        obj[f"{filt}PsfFluxMin"] = None
        obj[f"{filt}PsfFluxMax"] = None
    return obj


def load_cutout(filepath):
    """Load a FITS cutout file as bytes, or return None if not found."""
    if filepath is None:
        return None
    try:
        with open(filepath, "rb") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return None


def assemble_alert(db, sid, cutout_dir=None):
    """Assemble a complete alert packet for a given source ID.

    Args:
        db: RAPIDDB instance (from rapid.database.modules.utils.rapid_db).
        sid: source ID to build the alert for.
        cutout_dir: optional directory containing cutout FITS files.

    Returns:
        dict conforming to rapid.v01_00.alert.
    """
    # Query the triggering source with filter name
    cur = db.conn.cursor()
    cur.execute("""
        SELECT s.*, f.filter as filter_name
        FROM sources s
        JOIN filters f ON s.fid = f.fid
        WHERE s.sid = %s
    """, (sid,))
    columns = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Source {sid} not found")
    source_row = dict(zip(columns, row))
    filter_name = source_row.get("filter_name")
    field = int(source_row["field"])

    # Get diaObjectId via per-field merges/astroobjects tables
    cur.execute(f"""
        SELECT m.aid, a.*
        FROM merges_{field} m
        JOIN astroobjects_{field} a ON m.aid = a.aid
        WHERE m.sid = %s
    """, (sid,))
    columns = [desc[0] for desc in cur.description]
    obj_row = cur.fetchone()

    dia_object = None
    dia_object_id = None
    if obj_row is not None:
        obj_dict = dict(zip(columns, obj_row))
        dia_object_id = obj_dict["aid"]
        source_row["aid"] = dia_object_id

    # Build triggering diaSource
    dia_source = build_dia_source(source_row, filter_name)
    triggering_mjd = dia_source["midpointMjdTai"]

    # Query previous detections for the same object (12-month window)
    prv_dia_sources = None
    prv_dia_forced_sources = None
    if dia_object_id is not None:
        cur.execute(f"""
            SELECT s.*, f.filter as filter_name
            FROM sources s
            JOIN merges_{field} m ON s.sid = m.sid
            JOIN filters f ON s.fid = f.fid
            WHERE m.aid = %s AND s.sid != %s
              AND s.mjdobs >= %s
            ORDER BY s.mjdobs
        """, (dia_object_id, sid, triggering_mjd - 365.25))
        columns = [desc[0] for desc in cur.description]
        prv_rows = cur.fetchall()
        if prv_rows:
            prv_dia_sources = []
            for prow in prv_rows:
                pdict = dict(zip(columns, prow))
                pdict["aid"] = dia_object_id
                prv_dia_sources.append(
                    build_dia_source(pdict, pdict.get("filter_name"))
                )

        # Compute MJD range from source history for diaObject
        all_mjds = [triggering_mjd]
        if prv_dia_sources:
            all_mjds.extend(s["midpointMjdTai"] for s in prv_dia_sources)
        first_mjd = min(all_mjds)
        last_mjd = max(all_mjds)

        dia_object = build_dia_object(
            obj_dict, first_mjd=first_mjd, last_mjd=last_mjd,
            validity_mjd=triggering_mjd,
        )

        # Forced photometry in RAPID produces FITS files, not DB records;
        # integration with alert packets is not yet implemented.
        prv_dia_forced_sources = None
        logger.info("Forced photometry not yet available for alert assembly")

    cur.close()

    # Load cutouts
    cutout_diff = None
    cutout_sci = None
    cutout_tmpl = None
    if cutout_dir is not None:
        cutout_diff = load_cutout(os.path.join(cutout_dir, f"{sid}_diff.fits.gz"))
        cutout_sci = load_cutout(os.path.join(cutout_dir, f"{sid}_sci.fits.gz"))
        cutout_tmpl = load_cutout(os.path.join(cutout_dir, f"{sid}_tmpl.fits.gz"))

    return {
        "diaSourceId": source_row["sid"],
        "observation_reason": None,
        "target_name": None,
        "diaSource": dia_source,
        "prvDiaSources": prv_dia_sources,
        "prvDiaForcedSources": prv_dia_forced_sources,
        "diaObject": dia_object,
        "ssSource": None,
        "mpc_orbits": None,
        "cutoutDifference": cutout_diff,
        "cutoutScience": cutout_sci,
        "cutoutTemplate": cutout_tmpl,
    }


def serialize_alert(alert_dict, schema=None):
    """Serialize an alert dict to Avro bytes.

    Args:
        alert_dict: dict conforming to rapid.v01_00.alert.
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


def produce_alert(db, sid, producer=None, topic="alerts", cutout_dir=None):
    """End-to-end: assemble, serialize, and optionally publish an alert.

    Args:
        db: RAPIDDB instance.
        sid: source ID.
        producer: optional confluent_kafka.Producer instance.
        topic: Kafka topic name.
        cutout_dir: optional directory containing cutout FITS files.

    Returns:
        bytes containing the serialized alert.
    """
    schema = load_schema()
    alert_dict = assemble_alert(db, sid, cutout_dir=cutout_dir)
    alert_bytes = serialize_alert(alert_dict, schema=schema)
    logger.info("Alert for sid=%d serialized (%d bytes)", sid, len(alert_bytes))

    if producer is not None:
        publish_alert(alert_bytes, producer, topic=topic)

    return alert_bytes


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <source_id> [--kafka]")
        sys.exit(1)

    source_id = int(sys.argv[1])
    use_kafka = "--kafka" in sys.argv

    # Import RAPIDDB
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from database.modules.utils.rapid_db import RAPIDDB

    db = RAPIDDB()

    producer = None
    if use_kafka:
        from confluent_kafka import Producer
        producer = Producer({
            "bootstrap.servers": os.environ.get("KAFKA_BROKER", "localhost:9092"),
            "message.max.bytes": "15728640",
        })

    alert_bytes = produce_alert(db, source_id, producer=producer)
    print(f"Alert produced: {len(alert_bytes)} bytes")
