"""VARIANT 3 checks -- run against the REAL committed .avsc files and the
real provider record classes (no subset, no stubs standing in for Source).

  1. consistency: every model matches its committed .avsc (names, order,
     nullability, type compatibility) -- the drift gate
  2. drift demo: a deliberately wrong model is caught with a clear message
  3. build parity: DiaSource/DiaObject from real providers records match
     produce.build_dia_source / build_dia_object exactly
  4. a full 14-field alert serializes byte-identically to the produce path
  5. source_check() is clean against providers.Source / ObjectRecord
  6. failure loudness: None in a non-nullable -> ValidationError naming
     the field; a dump can't be re-validated (no silent double-invert of
     isNegative)

Run:  python -m test_pydantic.v3_check   (from the alerts/ directory)
"""

import dataclasses
import io
import json
from typing import ClassVar, Optional

import fastavro
import fastavro.schema
import pydantic

try:
    import fitsio  # noqa: F401  (providers.py needs it at import time)
except ModuleNotFoundError:
    # providers imports fitsio for cutout reading; these checks never touch
    # cutout code, so shim it when the env (e.g. astroconda) lacks it.
    import sys
    import types

    sys.modules["fitsio"] = types.ModuleType("fitsio")

from rapid_alerts import produce
from rapid_alerts.providers import ObjectRecord, Source

from .v3_avsc_first import (RECORD_ORDER, RECORDS, SCHEMA_ROOT, Alert,
                            DiaObject, DiaSource, load_avsc,
                            model_schema_problems,
                            schema_consistency_problems)


def _expect(label, cond, ok, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return ok and cond


def _report_values(label, ref, pyd, ok):
    if ref == pyd:
        print(f"PASS  {label}")
        return ok
    print(f"FAIL  {label}")
    for key in sorted(set(ref) | set(pyd)):
        if ref.get(key) != pyd.get(key):
            print(f"      {key}: produce={ref.get(key)!r} pydantic={pyd.get(key)!r}")
    return False


class DriftedDiaSource(DiaSource):
    """Deliberately wrong on purpose: psfFlux non-nullable, detector float,
    and an extra field the schema has never heard of."""
    psfFlux: float = 0.0
    detector: float = 0.0
    notInSchema: Optional[int] = None


def main():
    ok = True

    avsc, version = load_avsc()
    print(f"checking models against committed schema version {version} "
          f"({len(avsc)} .avsc records under {SCHEMA_ROOT})")

    # 1) the drift gate, against the real files
    problems = schema_consistency_problems()
    total_fields = sum(len(r["fields"]) for r in avsc.values())
    ok = _expect(f"all {len(RECORDS)} models consistent with committed "
                 f".avsc ({total_fields} fields)", problems == [], ok)
    for p in problems:
        print("      ", p)

    # 2) a drifted model IS caught, with readable messages
    drift = model_schema_problems(DriftedDiaSource, avsc["diaSource"])
    ok = _expect("deliberately drifted model caught", len(drift) == 3, ok,
                 f"{len(drift)} problems")
    for p in drift:
        print("       e.g.", p)

    # 3) build parity from REAL provider records
    src = Source(sid=9001, expid=42, sca=7, mjdobs=60500.5, ra=268.09,
                 dec=-28.71, xfit=101.5, yfit=202.5, band="F158", aid=777,
                 xerr=0.02, yerr=0.03, fluxfit=1234.5, fluxerr=56.7,
                 flags=0, field=3, hp6=1234, hp9=56789, pid=4242,
                 isdiffpos=True, qfit=0.11, cfit=0.02, redchi=1.3,
                 npixfit=25, sharpness=0.42, roundness1=0.05,
                 roundness2=-0.04, peak=850.0, exptime=139.8)
    ref_dia = produce.build_dia_source(src)
    pyd_dia = DiaSource.model_validate(src).model_dump()
    ok = _report_values("diaSource (72 fields) == produce.build_dia_source",
                        ref_dia, pyd_dia, ok)

    obj = ObjectRecord(aid=777, ra0=268.0901, dec0=-28.7099, stdevra=0.05,
                       stdevdec=0.04, nsources=3, first_mjd=60480.1,
                       last_mjd=60500.5, validity_mjd=60500.5)
    ref_obj = produce.build_dia_object(obj)
    pyd_obj = DiaObject.model_validate(obj).model_dump()
    ok = _report_values("diaObject (76 fields) == produce.build_dia_object",
                        ref_obj, pyd_obj, ok)

    # 4) full alert: byte-identical to the produce-path dict, serialized
    #    with the committed schema exactly as produce does
    ref_alert = {
        "schemaVersion": version,
        "pipelineVersion": None,
        "diaSourceId": src.sid,
        "diaSource": ref_dia,
        "prvDiaSources": [ref_dia],
        "diaObject": ref_obj,
        "prvDiaForcedSources": None,
        "ssSource": None,
        "mpc_orbits": None,
        "cutoutDifference": b"FITSBYTES",
        "cutoutScience": None,
        "cutoutTemplate": None,
        "observation_reason": None,
        "target_name": None,
    }
    pyd_alert = Alert(
        schemaVersion=version,
        diaSourceId=src.sid,
        diaSource=DiaSource.model_validate(src),
        prvDiaSources=[DiaSource.model_validate(src)],
        diaObject=DiaObject.model_validate(obj),
        cutoutDifference=b"FITSBYTES",
    ).model_dump()
    ok = _report_values("alert (14 fields)   == produce-style assembly",
                        ref_alert, pyd_alert, ok)

    major, minor = version.split(".")
    paths = [str(SCHEMA_ROOT / major / minor
                 / f"rapid.v{major}_{minor}.{name}.avsc")
             for name in RECORD_ORDER]
    schema = fastavro.schema.load_schema_ordered(paths)
    buf_ref, buf_pyd = io.BytesIO(), io.BytesIO()
    fastavro.schemaless_writer(buf_ref, schema, ref_alert)
    fastavro.schemaless_writer(buf_pyd, schema, pyd_alert)
    ok = _expect(f"alert Avro bytes ({buf_ref.tell()} B) identical",
                 buf_ref.getvalue() == buf_pyd.getvalue(), ok)

    # 5) import-time source guard against the REAL provider classes
    for model, data_cls in ((DiaSource, Source), (DiaObject, ObjectRecord)):
        problems = model.source_check(data_cls)
        ok = _expect(f"source_check({model.__name__}, {data_cls.__name__}) "
                     f"clean", problems == [], ok, "; ".join(problems))

    # 6) failure loudness
    try:
        DiaSource.model_validate(dataclasses.replace(src, dec=None))
        ok = _expect("None in non-nullable dec raises", False, ok)
    except pydantic.ValidationError as exc:
        err = exc.errors()[0]
        ok = _expect("None in non-nullable dec raises", True, ok,
                     f"{err['loc'][0]}: {err['msg']}")
    dia = DiaSource.model_validate(src)
    ok = _expect("isNegative inverted from isdiffpos exactly once",
                 dia.isNegative is False and src.isdiffpos is True, ok)
    try:
        DiaSource.model_validate(dia.model_dump())
        ok = _expect("model_dump() round-trip rejected (no silent "
                     "double-invert)", False, ok)
    except pydantic.ValidationError as exc:
        ok = _expect("model_dump() round-trip rejected (no silent "
                     "double-invert)", True, ok,
                     f"{exc.error_count()} missing-alias errors")

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
