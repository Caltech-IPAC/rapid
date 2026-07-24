"""VARIANT 3 -- .avsc-first. The committed .avsc files are the wire truth
(LSST's arrangement); these models are ordinary, idiomatic pydantic that is
*checked for compatibility* against them, not a generator of them.

Two sources of truth, mechanically reconciled -- the same philosophy as
the registry's gen_schema --check gate, with the arrow reversed:

  - schema_consistency_problems() verifies every model against its .avsc:
    same field names in the same order, Optional exactly where the schema
    has ["null", ...], and a compatible Python type (int for int-or-long,
    float for float-or-double). Run it at import or in CI; it fails with
    a clear message on drift.
  - pydantic validates every packet at assembly: a missing or None
    non-nullable, or a wrong type, is a ValidationError naming the field.
  - fastavro serializes with the parsed .avsc, so exact numeric widths
    are enforced by the layer that owns them.

Because the models only promise *compatibility*, all of v1/v2's machinery
disappears: no width markers (int covers both int and long), no metaclass,
no walker. Field docs live only in the .avsc (no duplication); the
DB-column mapping is the validation_alias, which doubles as provenance for
implemented params. Stubs are simply unaliased fields defaulting to null
-- nothing populates them, so no stub-nulling rule is needed; implementing
a param means adding its alias (the .avsc does not change).

The isNegative alias+transform relies on populate_by_name staying False
(the default): the alias is then the only accepted input, so the transform
applies exactly once and a model rebuilt from its own dump fails loudly.

This covers the FULL current schema (all 6 records; skeleton generated
from the .avsc files once, kept in sync by the consistency check).
Checked against the real committed files by v3_check.py.
"""

import dataclasses
import json
import types
from pathlib import Path
from typing import Annotated, ClassVar, Optional, Union, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schema"


class AvscRecord(BaseModel):
    """Base for schema records: config + the import-time source guard."""

    model_config = ConfigDict(from_attributes=True)

    avro_name: ClassVar[str]

    @classmethod
    def source_check(cls, data_cls):
        """Every field read from the provider record (aliased or not, so
        long as it isn't an unaliased stub defaulting to null... in this
        variant: every *aliased* field, plus unaliased required ones) must
        exist on data_cls. Mirrors produce._validate_registry(); catches a
        typo'd alias on a nullable field, which pydantic would otherwise
        silently turn into None on every alert."""
        available = {f.name for f in dataclasses.fields(data_cls)}
        available |= {name for name, value in vars(data_cls).items()
                      if isinstance(value, property)}
        problems = []
        for name, field in cls.model_fields.items():
            if field.validation_alias is None and not field.is_required():
                continue  # unaliased with default: stub or property-fed null
            attr = field.validation_alias or name
            if attr not in available:
                problems.append(
                    f"{cls.avro_name}.{name} reads {data_cls.__name__}."
                    f"{attr}, which does not exist")
        return problems


# ---------------------------------------------------------------------------
# The records. Docs live in the .avsc; the alias IS the provenance for
# implemented params; "# stub" marks params nothing fills in yet.
# ---------------------------------------------------------------------------

class DiaSource(AvscRecord):
    avro_name: ClassVar[str] = "diaSource"

    # --- Identifiers & associations -------------------------------------
    diaSourceId: int = Field(validation_alias="sid")
    visit: int = Field(validation_alias="expid")
    detector: int = Field(validation_alias="sca")
    diaObjectId: Optional[int] = Field(None, validation_alias="aid")
    ssObjectId: Optional[int] = None  # stub
    # --- Time ------------------------------------------------------------
    midpointMjd: float = Field(validation_alias="mjdobs")
    exposureTime: Optional[float] = Field(None, validation_alias="exptime")
    # --- Position (sky & pixel) ------------------------------------------
    ra: float
    dec: float
    x: float = Field(validation_alias="xfit")
    y: float = Field(validation_alias="yfit")
    xErr: Optional[float] = Field(None, validation_alias="xerr")
    yErr: Optional[float] = Field(None, validation_alias="yerr")
    raErr: Optional[float] = None  # stub
    decErr: Optional[float] = None  # stub
    # --- Photometry --------------------------------------------------------
    band: Optional[str] = None
    psfFlux: Optional[float] = Field(None, validation_alias="fluxfit")
    psfFluxErr: Optional[float] = Field(None, validation_alias="fluxerr")
    snr: Optional[float] = None  # Source.snr property
    isNegative: Annotated[bool, BeforeValidator(lambda v: not v)] = Field(
        validation_alias="isdiffpos")  # inverted; see module docstring
    apFlux: Optional[float] = None  # stub
    apFluxErr: Optional[float] = None  # stub
    scienceFlux: Optional[float] = None  # stub
    scienceFluxErr: Optional[float] = None  # stub
    templateFlux: Optional[float] = None  # stub
    templateFluxErr: Optional[float] = None  # stub
    diffimglimmag: Optional[float] = None  # stub
    # --- PSF-fit quality (photutils) ---------------------------------------
    psfQfit: Optional[float] = Field(None, validation_alias="qfit")
    psfCfit: Optional[float] = Field(None, validation_alias="cfit")
    psfRChi2: Optional[float] = Field(None, validation_alias="redchi")
    psfNdata: Optional[int] = Field(None, validation_alias="npixfit")
    sharpness: Optional[float] = None
    roundness1: Optional[float] = None
    roundness2: Optional[float] = None
    peak: Optional[float] = None
    # --- Classification / trail / dipole / moments (all stubs) --------------
    extendedness: Optional[float] = None  # stub
    reliability: Optional[float] = None  # stub
    reliabilityVersion: Optional[str] = None  # stub
    trailFlux: Optional[float] = None  # stub
    trailFluxErr: Optional[float] = None  # stub
    trailLength: Optional[float] = None  # stub
    trailAngle: Optional[float] = None  # stub
    dipoleMeanFlux: Optional[float] = None  # stub
    dipoleFluxErr: Optional[float] = None  # stub
    dipoleLength: Optional[float] = None  # stub
    dipoleAngle: Optional[float] = None  # stub
    ixx: Optional[float] = None  # stub
    iyy: Optional[float] = None  # stub
    ixy: Optional[float] = None  # stub
    ixxErr: Optional[float] = None  # stub
    iyyErr: Optional[float] = None  # stub
    ixyErr: Optional[float] = None  # stub
    elong: Optional[float] = None  # stub
    # --- Flags -----------------------------------------------------------
    flags: int
    pixelFlags_saturated: Optional[bool] = None  # stub
    pixelFlags_bad: Optional[bool] = None  # stub
    pixelFlags_edge: Optional[bool] = None  # stub
    pixelFlags_cr: Optional[bool] = None  # stub
    centroid_flag: Optional[bool] = None  # stub
    apFlux_flag: Optional[bool] = None  # stub
    psfFlux_flag: Optional[bool] = None  # stub
    # --- Nearest reference-image source (all stubs) --------------------------
    distnr: Optional[float] = None  # stub
    ranr: Optional[float] = None  # stub
    decnr: Optional[float] = None  # stub
    magnr: Optional[float] = None  # stub
    sigmagnr: Optional[float] = None  # stub
    chinr: Optional[float] = None  # stub
    sharpnr: Optional[float] = None  # stub
    # --- Roman-specific identifiers & tiling --------------------------------
    field: int
    hp6: int
    hp9: int
    pid: int


class DiaForcedSource(AvscRecord):
    """Entire record is a stub: no provider supplies ForcedPhot yet, so
    these fields carry no aliases. When forced photometry lands, add the
    aliases (forced_id, aid, expid, sca, flux, fluxerr, mjdobs,
    time_processed -- see the registry's attr column)."""

    avro_name: ClassVar[str] = "diaForcedSource"

    diaForcedSourceId: int
    diaObjectId: int
    visit: int
    detector: int
    ra: float
    dec: float
    band: Optional[str] = None
    psfFlux: Optional[float] = None
    psfFluxErr: Optional[float] = None
    scienceFlux: Optional[float] = None
    scienceFluxErr: Optional[float] = None
    midpointMjd: float
    timeProcessedMjd: float


class DiaObject(AvscRecord):
    avro_name: ClassVar[str] = "diaObject"

    # --- Identifier & position -------------------------------------------
    diaObjectId: int = Field(validation_alias="aid")
    ra: float = Field(validation_alias="ra0")
    dec: float = Field(validation_alias="dec0")
    raErr: Optional[float] = Field(None, validation_alias="stdevra")
    decErr: Optional[float] = Field(None, validation_alias="stdevdec")
    # --- Source history ----------------------------------------------------
    nDiaSources: int = Field(validation_alias="nsources")
    firstDiaSourceMjd: Optional[float] = Field(None, validation_alias="first_mjd")
    lastDiaSourceMjd: Optional[float] = Field(None, validation_alias="last_mjd")
    validityStartMjd: float = Field(validation_alias="validity_mjd")
    ncovhist: Optional[int] = None  # stub
    firstRefMjd: Optional[float] = None  # stub
    lastRefMjd: Optional[float] = None  # stub
    # --- Per-filter flux statistics (all stubs; need nJy calibration) -------
    F062PsfFluxMean: Optional[float] = None  # stub
    F062PsfFluxMeanErr: Optional[float] = None  # stub
    F062PsfFluxSigma: Optional[float] = None  # stub
    F062PsfFluxNdata: Optional[int] = None  # stub
    F062PsfFluxMin: Optional[float] = None  # stub
    F062PsfFluxMax: Optional[float] = None  # stub
    F062PsfFluxMaxSlope: Optional[float] = None  # stub
    F062PsfFluxErrMean: Optional[float] = None  # stub
    F087PsfFluxMean: Optional[float] = None  # stub
    F087PsfFluxMeanErr: Optional[float] = None  # stub
    F087PsfFluxSigma: Optional[float] = None  # stub
    F087PsfFluxNdata: Optional[int] = None  # stub
    F087PsfFluxMin: Optional[float] = None  # stub
    F087PsfFluxMax: Optional[float] = None  # stub
    F087PsfFluxMaxSlope: Optional[float] = None  # stub
    F087PsfFluxErrMean: Optional[float] = None  # stub
    F106PsfFluxMean: Optional[float] = None  # stub
    F106PsfFluxMeanErr: Optional[float] = None  # stub
    F106PsfFluxSigma: Optional[float] = None  # stub
    F106PsfFluxNdata: Optional[int] = None  # stub
    F106PsfFluxMin: Optional[float] = None  # stub
    F106PsfFluxMax: Optional[float] = None  # stub
    F106PsfFluxMaxSlope: Optional[float] = None  # stub
    F106PsfFluxErrMean: Optional[float] = None  # stub
    F129PsfFluxMean: Optional[float] = None  # stub
    F129PsfFluxMeanErr: Optional[float] = None  # stub
    F129PsfFluxSigma: Optional[float] = None  # stub
    F129PsfFluxNdata: Optional[int] = None  # stub
    F129PsfFluxMin: Optional[float] = None  # stub
    F129PsfFluxMax: Optional[float] = None  # stub
    F129PsfFluxMaxSlope: Optional[float] = None  # stub
    F129PsfFluxErrMean: Optional[float] = None  # stub
    F146PsfFluxMean: Optional[float] = None  # stub
    F146PsfFluxMeanErr: Optional[float] = None  # stub
    F146PsfFluxSigma: Optional[float] = None  # stub
    F146PsfFluxNdata: Optional[int] = None  # stub
    F146PsfFluxMin: Optional[float] = None  # stub
    F146PsfFluxMax: Optional[float] = None  # stub
    F146PsfFluxMaxSlope: Optional[float] = None  # stub
    F146PsfFluxErrMean: Optional[float] = None  # stub
    F158PsfFluxMean: Optional[float] = None  # stub
    F158PsfFluxMeanErr: Optional[float] = None  # stub
    F158PsfFluxSigma: Optional[float] = None  # stub
    F158PsfFluxNdata: Optional[int] = None  # stub
    F158PsfFluxMin: Optional[float] = None  # stub
    F158PsfFluxMax: Optional[float] = None  # stub
    F158PsfFluxMaxSlope: Optional[float] = None  # stub
    F158PsfFluxErrMean: Optional[float] = None  # stub
    F184PsfFluxMean: Optional[float] = None  # stub
    F184PsfFluxMeanErr: Optional[float] = None  # stub
    F184PsfFluxSigma: Optional[float] = None  # stub
    F184PsfFluxNdata: Optional[int] = None  # stub
    F184PsfFluxMin: Optional[float] = None  # stub
    F184PsfFluxMax: Optional[float] = None  # stub
    F184PsfFluxMaxSlope: Optional[float] = None  # stub
    F184PsfFluxErrMean: Optional[float] = None  # stub
    F213PsfFluxMean: Optional[float] = None  # stub
    F213PsfFluxMeanErr: Optional[float] = None  # stub
    F213PsfFluxSigma: Optional[float] = None  # stub
    F213PsfFluxNdata: Optional[int] = None  # stub
    F213PsfFluxMin: Optional[float] = None  # stub
    F213PsfFluxMax: Optional[float] = None  # stub
    F213PsfFluxMaxSlope: Optional[float] = None  # stub
    F213PsfFluxErrMean: Optional[float] = None  # stub


class SsSource(AvscRecord):
    """Entire record is a stub (solar-system processing not run)."""

    avro_name: ClassVar[str] = "ssSource"

    ssSourceId: int
    diaSourceId: int
    ssObjectId: Optional[int] = None
    heliocentricX: Optional[float] = None
    heliocentricY: Optional[float] = None
    heliocentricZ: Optional[float] = None
    phaseAngle: Optional[float] = None
    heliocentricDist: Optional[float] = None
    topocentricDist: Optional[float] = None


class MpcOrbits(AvscRecord):
    """Entire record is a stub (MPC orbit ingest not run)."""

    avro_name: ClassVar[str] = "mpc_orbits"

    id: str
    a: Optional[float] = None
    e: Optional[float] = None
    incl: Optional[float] = None
    Omega: Optional[float] = None
    omega: Optional[float] = None
    M: Optional[float] = None
    epoch: Optional[float] = None
    H: Optional[float] = None
    G: Optional[float] = None


class Alert(AvscRecord):
    avro_name: ClassVar[str] = "alert"

    schemaVersion: Optional[str] = None  # produce sets this, like today
    pipelineVersion: Optional[str] = None  # stub
    diaSourceId: int
    diaSource: DiaSource
    prvDiaSources: Optional[list[DiaSource]] = None
    diaObject: Optional[DiaObject] = None
    prvDiaForcedSources: Optional[list[DiaForcedSource]] = None  # stub
    ssSource: Optional[SsSource] = None  # stub
    mpc_orbits: Optional[MpcOrbits] = None  # stub
    cutoutDifference: Optional[bytes] = None
    cutoutScience: Optional[bytes] = None
    cutoutTemplate: Optional[bytes] = None
    observation_reason: Optional[str] = None  # stub
    target_name: Optional[str] = None  # stub


# Avro dependency order (referenced records first), matching the registry.
RECORD_ORDER = ("diaSource", "diaForcedSource", "diaObject", "ssSource",
                "mpc_orbits", "alert")
RECORDS = {model.avro_name: model
           for model in (DiaSource, DiaForcedSource, DiaObject, SsSource,
                         MpcOrbits, Alert)}


# ---------------------------------------------------------------------------
# The consistency check: models vs the committed .avsc files
# ---------------------------------------------------------------------------

# Avro base type -> the one Python annotation compatible with it. int covers
# both int and long, float covers both float and double: exact widths are
# fastavro's job at serialization time, not the model's.
_COMPAT = {"long": int, "int": int, "double": float, "float": float,
           "boolean": bool, "string": str, "bytes": bytes}


def load_avsc(version=None, schema_root=SCHEMA_ROOT):
    """Load the raw .avsc dicts for a version (default: latest.txt).

    Returns ({record name: schema dict}, version)."""
    schema_root = Path(schema_root)
    if version is None:
        version = (schema_root / "latest.txt").read_text().strip()
    major, minor = version.split(".")
    ns = f"rapid.v{major}_{minor}"
    out = {}
    for name in RECORD_ORDER:
        path = schema_root / major / minor / f"{ns}.{name}.avsc"
        if path.exists():
            record = json.loads(path.read_text())
            out[record["name"]] = record
    return out, version


def _is_optional(annotation):
    return (get_origin(annotation) in (Union, types.UnionType)
            and type(None) in get_args(annotation))


def _non_none(annotation):
    return next(a for a in get_args(annotation) if a is not type(None))


def _compat_problem(annotation, avro):
    """None if the Python annotation is compatible with the Avro type,
    else a description of the mismatch."""
    nullable = isinstance(avro, list) and bool(avro) and avro[0] == "null"
    optional = _is_optional(annotation)
    if nullable != optional:
        return (f"schema {'is' if nullable else 'is NOT'} nullable but the "
                f"model annotation {annotation!r} "
                f"{'is not Optional' if nullable else 'is Optional'}")
    base_avro = next(t for t in avro if t != "null") if nullable else avro
    base_ann = _non_none(annotation) if optional else annotation
    if isinstance(base_avro, dict):  # {"type": "array", "items": ...}
        if get_origin(base_ann) is not list:
            return f"schema has an array but the model has {base_ann!r}"
        return _compat_problem(get_args(base_ann)[0], base_avro["items"])
    if base_avro in _COMPAT:
        if base_ann is not _COMPAT[base_avro]:
            return (f"schema type {base_avro!r} needs "
                    f"{_COMPAT[base_avro].__name__}, model has {base_ann!r}")
        return None
    # namespaced record reference, e.g. "rapid.v01_01.diaSource"
    short = base_avro.rsplit(".", 1)[-1]
    expected = RECORDS.get(short)
    if expected is None:
        return f"schema references unknown record {base_avro!r}"
    if base_ann is not expected:
        return f"schema references {short} but the model has {base_ann!r}"
    return None


def model_schema_problems(model, avsc_record):
    """Compare one model against one raw .avsc dict. Empty list == in sync."""
    problems = []
    model_names = list(model.model_fields)
    avsc_names = [f["name"] for f in avsc_record["fields"]]
    for name in avsc_names:
        if name not in model_names:
            problems.append(f"{model.avro_name}.{name} is in the .avsc but "
                            f"not the model")
    for name in model_names:
        if name not in avsc_names:
            problems.append(f"{model.avro_name}.{name} is in the model but "
                            f"not the .avsc")
    if not problems and model_names != avsc_names:
        problems.append(f"{model.avro_name}: field order differs from the "
                        f".avsc (order is the Avro wire order)")
    for avsc_field in avsc_record["fields"]:
        field = model.model_fields.get(avsc_field["name"])
        if field is None:
            continue
        problem = _compat_problem(field.annotation, avsc_field["type"])
        if problem:
            problems.append(f"{model.avro_name}.{avsc_field['name']}: "
                            f"{problem}")
    return problems


def schema_consistency_problems(version=None, schema_root=SCHEMA_ROOT):
    """Check every model against every committed .avsc record. This is the
    drift gate: run it at import in produce (like gen_schema.schema_problems
    today) and/or as a test. Empty list == everything in sync."""
    avsc, _ = load_avsc(version, schema_root)
    problems = []
    for name in RECORDS:
        if name not in avsc:
            problems.append(f"model {name!r} has no .avsc record on disk")
    for name, record in avsc.items():
        if name not in RECORDS:
            problems.append(f".avsc record {name!r} has no model")
        else:
            problems.extend(model_schema_problems(RECORDS[name], record))
    return problems
