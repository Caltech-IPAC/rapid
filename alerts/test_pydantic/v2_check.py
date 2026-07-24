"""VARIANT 2 checks: prove the Pydantic + walker path reproduces today's
output.

Parity checks for a representative subset of diaSource + alert:
  1. generated .avsc matches rapid_alerts.gen_schema (per-field, per-record)
     -- including timeProcessedMjd, NOT_USED in the registry and commented
     out in schema.py: both paths must keep it out of schema and values
  2. the built record dict matches produce.build_record's logic
     (including the STUB-stays-null rule, via the staged apFlux attr)
  3. the alert serializes byte-identically with fastavro
  4. ParamSpec source provenance matches the registry
  5. source_check() is clean for DiaSource against the provider record

Failure-mode checks (does a broken declaration fail correctly?):
  6. IMPLEMENTED non-nullable param with a typo'd/missing attr
       -> ValidationError at build time (loud)
  7. IMPLEMENTED *nullable* param with a typo'd attr
       -> silently None at build time, BUT caught by source_check()
          (this is why source_check must run at import, like the registry)
  8. broken getter (property raising a real exception)
       -> propagates at build time (loud)
  9. KNOWN EDGE: property raising AttributeError looks "missing" to
     pydantic -> nullable param silently None; the registry's build_record
     would have raised. source_check can't see it (the property exists).
 10. a model rebuilt from its own model_dump() is rejected (aliases only),
     so the isNegative transform can never silently double-invert.

Run:  python -m test_pydantic.v2_check   (from the alerts/ directory)
"""

import difflib
import io
import json
from dataclasses import dataclass
from typing import Optional

import fastavro
import pydantic

from rapid_alerts import gen_schema
from rapid_alerts import param_registry as pr

from . import v2_avro as walker
from .v2_rapid_pydantic import IMPLEMENTED, RapidRecord, param
from .v2_registry_style import VERSION, Alert, DiaSource

NS = walker.namespace(VERSION)

# timeProcessedMjd is NOT_USED in the registry and commented out in
# schema.py: both must keep it out of the schema and the built values.
DIA_FIELDS = ["diaSourceId", "visit", "detector", "diaObjectId", "midpointMjd",
              "timeProcessedMjd", "ra", "band", "psfFlux", "snr", "isNegative",
              "apFlux", "flags"]
ALERT_FIELDS = ["schemaVersion", "diaSourceId", "diaSource", "prvDiaSources",
                "cutoutDifference"]

DIA_DOC = "RAPID alert schema: individual source detection on a difference image"
ALERT_DOC = "RAPID alert schema: top-level alert record"


@dataclass
class SourceStub:
    """Stand-in for providers.Source (only the attrs the subset reads)."""

    sid: int
    expid: int
    sca: int
    aid: Optional[int]
    mjdobs: float
    ra: float
    band: str
    fluxfit: Optional[float]
    fluxerr: Optional[float]
    isdiffpos: bool
    flags: int
    # staged provider value for the STUB apFlux param: must NOT leak into
    # the packet until the param is flipped to IMPLEMENTED
    apflux: Optional[float] = 998.25

    @property
    def snr(self):
        if self.fluxfit is not None and self.fluxerr:
            return self.fluxfit / self.fluxerr
        return None


def _filter(params, names):
    by_name = {p.name: p for p in params}
    return tuple(by_name[n] for n in names)


def reference_schema(name, doc, params):
    return gen_schema.record_schema(pr.Record(name, doc, params), VERSION, NS)


def build_ref(params, data):
    """Faithful copy of produce.build_record's IMPLEMENTED/STUB handling."""
    out = {}
    for p in params:
        if p.status is pr.Status.NOT_USED:
            continue
        if p.status is not pr.Status.IMPLEMENTED:
            out[p.name] = None
            continue
        out[p.name] = p.getter(data) if p.getter else getattr(data, p.attr or p.name)
    return out


def serialize(schema, named, value):
    parsed = fastavro.parse_schema(schema, named_schemas=named)
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, parsed, value)
    return buf.getvalue()


def _report(label, a, b, ok):
    if a == b:
        print(f"PASS  {label}")
        return ok
    print(f"FAIL  {label}")
    ja = json.dumps(a, indent=2, default=str).splitlines()
    jb = json.dumps(b, indent=2, default=str).splitlines()
    for line in difflib.unified_diff(ja, jb, "registry", "pydantic", lineterm=""):
        print("     ", line)
    return False


def _expect(label, cond, ok, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return ok and cond


# ---------------------------------------------------------------------------
# Broken-on-purpose records for the failure-mode checks
# ---------------------------------------------------------------------------

class BadRequired(RapidRecord, name="badRequired"):
    """IMPLEMENTED non-nullable param, attr typo'd (flagz != flags)."""
    flags = param("long", "typo'd attr", IMPLEMENTED, attr="flagz")


class BadNullable(RapidRecord, name="badNullable"):
    """IMPLEMENTED *nullable* param, attr typo'd (fluxfitt != fluxfit)."""
    psfFlux = param(["null", "float"], "typo'd attr", IMPLEMENTED,
                    attr="fluxfitt")


class SnrOnly(RapidRecord, name="snrOnly"):
    """Reads only the snr property; snr is required (non-nullable) here so
    a swallowed AttributeError would fail loudly too."""
    snr = param("float", "from property", IMPLEMENTED)


class SnrOptional(RapidRecord, name="snrOptional"):
    snr = param(["null", "float"], "from property", IMPLEMENTED)


@dataclass
class ExplodingSource:
    """Provider record whose snr property raises a real exception."""
    fluxfit: float = 1.0

    @property
    def snr(self):
        raise ZeroDivisionError("broken getter: fluxerr was zero")


@dataclass
class SwallowedSource:
    """Provider record whose snr property raises AttributeError -- pydantic
    treats that as 'attribute missing', not as an error."""
    fluxfit: float = 1.0

    @property
    def snr(self):
        raise AttributeError("renamed internal attr")


# ---------------------------------------------------------------------------


def main():
    src = SourceStub(sid=9001, expid=42, sca=7, aid=777, mjdobs=60500.5,
                     ra=268.09, band="F158", fluxfit=1234.5, fluxerr=56.7,
                     isdiffpos=True, flags=0)

    dia_params = _filter(pr.DIA_SOURCE_PARAMS, DIA_FIELDS)
    alert_params = _filter(pr.ALERT_PARAMS, ALERT_FIELDS)
    ok = True

    print("--- parity with the registry path " + "-" * 30)

    # 1) schema equality
    ref_dia = reference_schema("diaSource", DIA_DOC, dia_params)
    ref_alert = reference_schema("alert", ALERT_DOC, alert_params)
    pyd_dia = walker.record_schema(DiaSource, VERSION)
    pyd_alert = walker.record_schema(Alert, VERSION)
    ok = _report("diaSource .avsc == registry", ref_dia, pyd_dia, ok)
    ok = _report("alert .avsc     == registry", ref_alert, pyd_alert, ok)

    # 2) built-record value equality (includes the STUB rule: build_ref
    #    forces apFlux to None; the pydantic side must too, even though
    #    its attr is staged and src.apflux holds a real value)
    ref_dia_val = build_ref(dia_params, src)
    dia = DiaSource.model_validate(src, from_attributes=True)
    pyd_dia_val = dia.model_dump()
    ok = _report("diaSource values == build_record", ref_dia_val, pyd_dia_val, ok)
    ok = _expect("STUB apFlux nulled despite staged attr + provider value",
                 dia.apFlux is None and src.apflux is not None, ok,
                 f"src.apflux={src.apflux}")

    ref_alert_val = {
        "schemaVersion": VERSION,
        "diaSourceId": src.sid,
        "diaSource": ref_dia_val,
        "prvDiaSources": [ref_dia_val],
        "cutoutDifference": b"FITSBYTES",
    }
    pyd_alert_val = Alert(
        diaSourceId=src.sid,
        diaSource=DiaSource.model_validate(src, from_attributes=True),
        prvDiaSources=[DiaSource.model_validate(src, from_attributes=True)],
        cutoutDifference=b"FITSBYTES",
    ).model_dump()
    ok = _report("alert values     == hand-assembled", ref_alert_val, pyd_alert_val, ok)

    # 3) byte-identical Avro serialization
    named_ref = {}
    serialize(ref_dia, named_ref, ref_dia_val)          # register diaSource
    ref_bytes = serialize(ref_alert, named_ref, ref_alert_val)
    named_pyd = {}
    serialize(pyd_dia, named_pyd, pyd_dia_val)
    pyd_bytes = serialize(pyd_alert, named_pyd, pyd_alert_val)
    ok = _report(f"alert Avro bytes ({len(ref_bytes)} B) identical",
                 ref_bytes, pyd_bytes, ok)

    # 4) provenance parity
    for model, params, label in ((DiaSource, dia_params, "diaSource"),
                                 (Alert, alert_params, "alert")):
        ref_src = {p.name: p.source for p in params
                   if p.status is not pr.Status.NOT_USED}
        pyd_src = {name: spec.source
                   for name, spec in model.__rapid_params__.items()}
        ok = _report(f"{label} param source == registry source", ref_src,
                     pyd_src, ok)

    # 5) the import-time guard is clean for the real record
    problems = DiaSource.source_check(SourceStub)
    ok = _expect("source_check(DiaSource, SourceStub) clean", problems == [],
                 ok, "; ".join(problems))

    print("--- failure modes: broken declarations must fail correctly " + "-" * 5)

    # 6) non-nullable IMPLEMENTED param, typo'd attr -> loud at build
    try:
        BadRequired.model_validate(src, from_attributes=True)
        ok = _expect("typo'd attr on non-nullable param raises", False, ok)
    except pydantic.ValidationError as exc:
        ok = _expect("typo'd attr on non-nullable param raises", True, ok,
                     exc.errors()[0]["type"])

    # 7) nullable IMPLEMENTED param, typo'd attr -> silent None at build;
    #    source_check is what catches it (run it at import time!)
    silent = BadNullable.model_validate(src, from_attributes=True)
    ok = _expect("typo'd attr on nullable param is SILENT at build "
                 "(psfFlux=None)", silent.psfFlux is None, ok)
    problems = BadNullable.source_check(SourceStub)
    ok = _expect("...but source_check catches it", len(problems) == 1, ok,
                 "; ".join(problems))

    # 8) broken getter (property raises a real exception) -> propagates
    try:
        SnrOnly.model_validate(ExplodingSource(), from_attributes=True)
        ok = _expect("broken getter (property raises) propagates", False, ok)
    except Exception as exc:  # loud, with the param named
        ok = _expect("broken getter (property raises) propagates", True, ok,
                     f"{type(exc).__name__}: {exc}")

    # 9) KNOWN EDGE: AttributeError inside a property == "missing" to
    #    pydantic. Loud if the param is required; silent None if nullable
    #    (the registry's build_record would raise RuntimeError here).
    try:
        SnrOnly.model_validate(SwallowedSource(), from_attributes=True)
        ok = _expect("property raising AttributeError, required param", False, ok)
    except pydantic.ValidationError as exc:
        ok = _expect("property raising AttributeError, required param -> "
                     "'missing' error", True, ok, exc.errors()[0]["type"])
    swallowed = SnrOptional.model_validate(SwallowedSource(),
                                           from_attributes=True)
    print(f"EDGE  property raising AttributeError, nullable param -> "
          f"silently None (snr={swallowed.snr}); registry build_record "
          f"would raise. Keep provider properties trivial.")

    # 10) a dump can't be re-validated (aliases only) -> the isNegative
    #     transform can never double-invert silently
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
