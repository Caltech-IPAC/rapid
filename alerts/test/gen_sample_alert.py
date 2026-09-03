#!/usr/bin/env python
"""Generate sample_data/alert.json for the current schema version.

Builds the alert dict from param_registry (null where nullable, plausible
dummies elsewhere, plus realism overrides for the key fields), writes it
into the current version's schema directory (from schema/latest.txt), and
verifies a fastavro round-trip. Re-run after a schema change to give
avro_producer.py sample data for the new schema.

The sample is synthetic (registry-derived, not from the database). For a
real alert, use the production path: `python -m alerts.cli <sid> --save
file.avro`.

Usage:
    python alerts/test/gen_sample_alert.py
"""

import io
import json
import sys
from pathlib import Path

import fastavro

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alerts.param_registry import RECORDS, VERSION, Status, is_nullable
from alerts.produce import load_schema, schema_paths
from alerts.providers import SS_CANDIDATE_SEP_ARCSEC

RECS = {r.name: r for r in RECORDS}

DUMMY = {"long": 0, "int": 0, "double": 0.0, "float": 0.0,
         "string": "", "boolean": False, "bytes": b""}


def build(record_name, overrides):
    """Build a sample dict for one schema record.

    Parameters
    ----------
    record_name : str
        Name of the record in the registry (e.g. "diaSource").
    overrides : dict
        Param name -> value for fields that should hold realistic values;
        everything else gets null (if nullable) or a type-based dummy.

    Returns
    -------
    dict
        One sample record, with NOT_USED params excluded.

    Raises
    ------
    KeyError
        If an override key is not a param of the record (e.g. renamed in
        the registry).
    """
    rec = RECS[record_name]
    unknown = set(overrides) - {p.name for p in rec.params}
    if unknown:
        raise KeyError(f"{record_name}: unknown override params "
                       f"{sorted(unknown)} (renamed in the registry?)")
    out = {}
    for p in rec.params:
        if p.status is Status.NOT_USED:
            continue
        if p.name in overrides:
            out[p.name] = overrides[p.name]
            continue
        t = p.avro
        if is_nullable(t):
            out[p.name] = None
        elif isinstance(t, str) and not t.startswith("@"):
            out[p.name] = DUMMY[t]
        else:
            out[p.name] = None  # non-nullable record/array: must be overridden
    return out


def main():
    dia_source = build("diaSource", {
        "diaSourceId": 210000001, "expId": 4001, "detector": 7,
        "diaObjectId": 310000001, "midpointMjd": 62310.25, "exposureTime": 139.8,
        "ra": 9.9024, "dec": -44.1355, "x": 2044.3, "y": 2088.1,
        "xErr": 0.05, "yErr": 0.05,
        "band": "F129", "psfFlux": 1520.0, "psfFluxErr": 60.0, "snr": 25.3,
        "isNegative": False, "psfFitFlags": 0,
        "centroid_flag": False, "psfFlux_flag": False,
        "field": 1, "hp6": 46893, "hp9": 3001185, "pid": 12345,
        "isSSCandidate": True,  # consistent with the ssMatches entry below
    })

    prv_dia_source = build("diaSource", {
        "diaSourceId": 210000000, "expId": 4000, "detector": 7,
        "diaObjectId": 310000001, "midpointMjd": 62305.25, "exposureTime": 139.8,
        "ra": 9.9024, "dec": -44.1355, "x": 2044.1, "y": 2087.9,
        "xErr": 0.05, "yErr": 0.05,
        "band": "F158", "psfFlux": 1310.0, "psfFluxErr": 58.0, "snr": 22.6,
        "isNegative": False, "psfFitFlags": 0,
        "centroid_flag": False, "psfFlux_flag": False,
        "field": 1, "hp6": 46893, "hp9": 3001185, "pid": 12290,
    })

    dia_object = build("diaObject", {
        "diaObjectId": 310000001, "ra0": 9.9024, "dec0": -44.1355,
        "raErr": 1.4e-5, "decErr": 1.4e-5, "nDiaSources": 2,
        "firstDiaSourceMjd": 62305.25, "lastDiaSourceMjd": 62310.25,
        "validityStartMjd": 62310.25,
    })

    # inside the candidate cut, so diaSource.isSSCandidate = True above
    # stays consistent if the cut is tuned
    sep = round(0.5 * SS_CANDIDATE_SEP_ARCSEC, 3)
    ss_match = build("ssMatch", {
        "designation": "2005 QP87",
        "ra": round(9.9024 + sep / 3600.0, 6), "dec": -44.1355,
        "sep": sep, "pa": 90.0, "predVMag": 21.7,
    })

    ref_star_match = build("refMatch", {
        "sourceId": "1042", "ra": 9.902511, "dec": -44.1355,
        "sep": 0.4, "pa": 90.0, "classStar": 0.97, "flags": 0,
        "magAuto": 18.5, "magErrAuto": 0.02, "elong": 1.05, "fwhm": 0.31,
        "halfLightRadius": 0.15, "kronRadius": 3.5,
    })
    ref_galaxy_match = build("refMatch", {
        "sourceId": "1077", "ra": 9.901817, "dec": -44.1355,
        "sep": 1.5, "pa": 270.0, "classStar": 0.03, "flags": 2,
        "magAuto": 21.2, "magErrAuto": 0.1, "elong": 1.6, "fwhm": 0.8,
        "halfLightRadius": 0.6, "kronRadius": 4.1,
    })

    alert = build("alert", {
        "schemaVersion": VERSION,
        "diaSourceId": dia_source["diaSourceId"],
        "diaSource": dia_source,
        "prvDiaSources": [prv_dia_source],
        "diaObject": dia_object,
        "ssMatches": [ss_match],
        "refStarMatches": [ref_star_match],
        "refGalaxyMatches": [ref_galaxy_match],
    })

    out_path = schema_paths()[0].parent / "sample_data" / "alert.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(alert, indent=2) + "\n")
    print("wrote", out_path)

    # Round-trip check (load_schema also verifies .avsc files against the
    # registry, so a stale schema fails here rather than in the producer)
    schema = load_schema()
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, json.loads(out_path.read_text()))
    buf.seek(0)
    back = fastavro.schemaless_reader(buf, schema)
    assert back["diaSourceId"] == alert["diaSourceId"]
    assert back["ssMatches"][0]["designation"] == ss_match["designation"]
    print(f"round-trip ok ({buf.getbuffer().nbytes} bytes)")


if __name__ == "__main__":
    main()
