"""VARIANT 1 -- vanilla-pydantic (Annotated) style. Pydantic models are
the single source of truth; the .avsc is generated from them by a custom
walker. Kept only for style comparison: self-contained, NOT exercised by
any check script (v2 superseded it).

Everything is expressed with public pydantic vocabulary (Annotated
metadata, Field, validators), at the cost of visual noise the registry
never had.

Demonstrates the patterns we settled on:
  - Avro numeric width carried as ``Annotated`` metadata (``LONG``/``INT``/
    ``FLOAT``/``DOUBLE``) since Python int/float can't express int-vs-long /
    float-vs-double.
  - provenance (the registry's ``Param.source``) carried as ``Src`` metadata,
    so the implemented/stub inventory survives the move off the registry.
  - ``validation_alias`` for plain DB-column -> field renames, populated via
    ``model_validate(source, from_attributes=True)``.
  - a transformed field (``isNegative``): alias + ``BeforeValidator``. This is
    only safe while aliases are the sole accepted input (populate_by_name
    stays False, see RapidRecord): every input is then in the *source's*
    convention, so the transform applies exactly once. Reconstructing a model
    from its own ``model_dump()`` output fails loudly (field names are not
    accepted) instead of silently double-inverting.
  - a field read from a source *property* (``snr``) with no alias.
  - implementation ``Status`` as ``Annotated`` metadata (stays out of any
    schema). RapidRecord forces STUB fields to null after validation, even
    when an alias is staged -- same rule as produce.build_record.
  - params excluded from the schema (registry NOT_USED) are NOT model fields
    (a field would leak into model_dump() output); they live in the
    ``not_used`` ClassVar inventory instead.
"""

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, ClassVar, Optional

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field,
                      model_validator)

VERSION = "01.01"


@dataclass(frozen=True)
class AvroType:
    """Marker disambiguating Avro numeric width (int/long, float/double)."""

    name: str


@dataclass(frozen=True)
class Src:
    """Provenance marker (the registry's Param.source): where an implemented
    value comes from, or what work would fill in a stub."""

    text: str


@dataclass(frozen=True)
class NotUsed:
    """Inventory entry for a param excluded from the schema (registry
    NOT_USED). Deliberately not a model field: it must appear in neither
    the .avsc nor model_dump() output."""

    name: str
    doc: str
    source: str


class Status(str, Enum):
    # NOT_USED is intentionally absent: excluded params are not fields at
    # all -- declare them in a record's `not_used` ClassVar instead.
    IMPLEMENTED = "implemented"
    STUB = "stub"


LONG = AvroType("long")
INT = AvroType("int")
FLOAT = AvroType("float")
DOUBLE = AvroType("double")


def field_avro(field):
    """The AvroType width marker on a FieldInfo, or None."""
    return next((m for m in field.metadata if isinstance(m, AvroType)), None)


def field_status(field):
    return next((m for m in field.metadata if isinstance(m, Status)),
                Status.IMPLEMENTED)


def field_source(field):
    src = next((m for m in field.metadata if isinstance(m, Src)), None)
    return src.text if src else None


class RapidRecord(BaseModel):
    """Base class for schema records: shared config plus the two registry
    behaviors pydantic does not give us by itself (stub enforcement and
    the import-time source check)."""

    # populate_by_name must stay False (the pydantic default): transformed
    # fields (isNegative) rely on alias-only input to apply their transform
    # exactly once. Enabling it would let field-name input through, and a
    # model rebuilt from its own dump would silently double-transform.
    model_config = ConfigDict(from_attributes=True)

    avro_name: ClassVar[str]
    avro_doc: ClassVar[str]
    not_used: ClassVar[tuple] = ()  # NotUsed inventory entries

    @model_validator(mode="after")
    def _null_stub_fields(self):
        # produce.build_record's rule: a STUB param serializes as null even
        # if its attr/getter is already staged (diaForcedSource stages all
        # of them). Without this, a staged validation_alias would leak a
        # real provider value into the packet.
        for name, field in type(self).model_fields.items():
            if (field_status(field) is Status.STUB
                    and getattr(self, name) is not None):
                setattr(self, name, None)
        return self

    @classmethod
    def source_check(cls, data_cls):
        """Import-time guard, ported from produce._validate_registry():
        every IMPLEMENTED field must read an attribute or property that
        exists on ``data_cls`` (the provider record this model is built
        from). Returns a list of problem strings, empty when clean.

        This matters most for *nullable* implemented fields: a typo'd
        validation_alias there would not fail validation -- the field
        would silently default to None on every alert. Run this at import
        so the typo is caught before any alert is built.
        """
        available = {f.name for f in dataclasses.fields(data_cls)}
        available |= {name for name, value in vars(data_cls).items()
                      if isinstance(value, property)}
        problems = []
        for name, field in cls.model_fields.items():
            if field_status(field) is not Status.IMPLEMENTED:
                continue
            attr = field.validation_alias or name
            if attr not in available:
                problems.append(
                    f"{cls.avro_name}.{name} reads {data_cls.__name__}."
                    f"{attr}, which does not exist")
        return problems


class DiaSource(RapidRecord):
    avro_name: ClassVar[str] = "diaSource"
    avro_doc: ClassVar[str] = (
        "RAPID alert schema: individual source detection on a difference image"
    )
    not_used: ClassVar[tuple] = (
        NotUsed("timeProcessedMjd",
                "Time alert was processed (UTC scale) [MJD]",
                "set at assembly time"),
    )

    # NOTE: field declaration order IS the Avro wire order -- reordering
    # fields here is a schema change, exactly as in param_registry.py.
    diaSourceId: Annotated[int, LONG, Src("sources.sid")] = Field(
        validation_alias="sid",
        description="Unique identifier for this source detection")
    visit: Annotated[int, LONG, Src("sources.expid")] = Field(
        validation_alias="expid", description="Visit (exposure) identifier")
    detector: Annotated[int, INT, Src("sources.sca")] = Field(
        validation_alias="sca", description="Detector (SCA) number")
    diaObjectId: Annotated[Optional[int], LONG,
                           Src("merges_<field>.aid")] = Field(
        default=None, validation_alias="aid",
        description="Associated diaObject identifier")
    midpointMjd: Annotated[float, DOUBLE, Src("sources.mjdobs")] = Field(
        validation_alias="mjdobs",
        description="Effective mid-observation time (UTC scale) [MJD]")
    ra: Annotated[float, DOUBLE, Src("sources.ra")] = Field(
        description="Right ascension; ICRS [deg]")
    band: Annotated[Optional[str], Src("filters.filter")] = Field(
        default=None,
        description="Filter band name (F062, F087, F106, F129, F146, F158, "
                    "F184, F213)")
    psfFlux: Annotated[Optional[float], FLOAT,
                       Src("sources.fluxfit (instrumental; nJy calibration "
                           "pending)")] = Field(
        default=None, validation_alias="fluxfit",
        description="Flux from PSF-fit on difference image [nJy]")
    snr: Annotated[Optional[float], FLOAT,
                   Src("computed: fluxfit / fluxerr")] = Field(
        default=None, description="Signal-to-noise ratio (psfFlux / psfFluxErr)")
    # Transform via BeforeValidator: input is always in the source's
    # isdiffpos convention because the alias is the only accepted input
    # (see RapidRecord's populate_by_name note).
    isNegative: Annotated[bool, BeforeValidator(lambda v: not v),
                          Src("sources.isdiffpos (inverted; renamed per "
                              "schema spreadsheet)")] = Field(
        validation_alias="isdiffpos",
        description="true if source is from negative (ref minus sci) subtraction")
    # STUB with a *staged* alias: the provider value is already available,
    # but RapidRecord nulls it until the param is flipped to IMPLEMENTED
    # (mirrors the staged attrs on the diaForcedSource stubs).
    apFlux: Annotated[Optional[float], FLOAT, Status.STUB,
                      Src("aperture photometry (not in DB flow; SExtractor "
                          "MAG_AUTO in file flow)")] = Field(
        default=None, validation_alias="apflux",
        description="Aperture flux on difference image (stub) [nJy]")
    flags: Annotated[int, LONG, Src("sources.flags")] = Field(
        description="Bitmask of processing flags")


class Alert(RapidRecord):
    avro_name: ClassVar[str] = "alert"
    avro_doc: ClassVar[str] = "RAPID alert schema: top-level alert record"

    schemaVersion: Annotated[Optional[str],
                             Src("param_registry.VERSION (set in "
                                 "produce.py)")] = Field(
        default=VERSION,
        description="Version of the alert schema used to serialize this packet")
    diaSourceId: Annotated[int, LONG, Src("produce.assemble_alert()")] = Field(
        description="Identifier for the triggering diaSource")
    diaSource: Annotated[DiaSource, Src("produce.assemble_alert()")] = Field(
        description="Triggering source detection")
    prvDiaSources: Annotated[Optional[list[DiaSource]],
                             Src("produce.assemble_alert()")] = Field(
        default=None,
        description="Previous detections of the same object within 12 months")
    cutoutDifference: Annotated[Optional[bytes],
                                Src("provider get_cutouts()")] = Field(
        default=None, description="FITS cutout of difference image")


# Avro dependency order (referenced records first), like RECORDS in the registry.
RECORDS = (DiaSource, Alert)
