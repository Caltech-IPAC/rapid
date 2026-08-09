"""
File:    subjects.py

Typed work-unit identity: every job type's declared subject grain.

Integration review 2026-08, composite ruling 2 (rapid_plan/decisions.md):

    "Every job type declares its subject grain (exposure/SCA, date/SCA,
    date/field, field, or release-unit); deduplication and logical-job
    identity derive from the declared subject; the storage-path key
    (exposure/SCA) is retained only for product-producing job types
    (product keys embed it; database-effect types mint none)."

WHY A REGISTRY AND NOT A METHOD ON `ProcessingUnit`. The subject a unit is
deduplicated and keyed by is a property of its JOB TYPE, not of the unit's
own exposure/sca fields — `ProcessingUnit(exposure=ordinal, sca=0, ...)`
for a crossmatch unit carries the processing-date ordinal in `exposure`
and a fixed sentinel in `sca` (`submission/gathering.py`,
`gather_crossmatch_units`), which is a STORAGE-KEY-SHAPED carrier the
array layer needs, not the unit's real identity. The real identity — the
declared subject — lives in `unit.fields`, and only the job type knows
which keys of `fields` it is. This module is the one place that mapping
is written down, so `ProcessingUnit.dedup_key` (manifest.py) and the
operator's gatherer registry (`pipeline/operator/gathering.py`) read the
same declaration rather than each re-deriving it.

THE V25 DEFECT THIS CLOSES. Before this module existed, deduplication was
`unit.key` for every job type — `f"{exposure:06d}/{sca:02d}"`. Crossmatch
gathering yields `ProcessingUnit(exposure=_proc_date_ordinal(proc_date),
sca=0, ...)` per FIELD, so every field of one processing date produced the
SAME storage-path key and the accumulator's `_pending_keys` set (a plain
`set[str]` of `.key` values, `submission/batching.py`) silently dropped
every field after the first. `dedup_key` below is what
`ReadyWorkAccumulator` and `ProcessingUnit.logical_job_key` are changed to
use instead, so two units are the same unit only when their DECLARED
SUBJECT agrees.
"""

import dataclasses
from typing import Any

from .routes import (JOB_TYPE_ALERT_PRODUCTION, JOB_TYPE_CATALOG_LOAD,
                     JOB_TYPE_CROSSMATCH, JOB_TYPE_MERGE_CURRENCY,
                     JOB_TYPE_MERGE_DEDUP, JOB_TYPE_REFERENCE_IMAGE,
                     JOB_TYPE_SCIENCE, JOB_TYPE_SOURCE_CURRENCY,
                     JOB_TYPE_STATISTICS)

#: The five grains the ruling names, verbatim.
GRAIN_EXPOSURE_SCA = "exposure_sca"
GRAIN_DATE_SCA = "date_sca"
GRAIN_DATE_FIELD = "date_field"
GRAIN_FIELD = "field"
GRAIN_RELEASE_UNIT = "release_unit"

GRAINS = (GRAIN_EXPOSURE_SCA, GRAIN_DATE_SCA, GRAIN_DATE_FIELD, GRAIN_FIELD,
         GRAIN_RELEASE_UNIT)


class SubjectError(ValueError):
    """A unit does not carry the fields its declared job type requires."""


class UnknownJobType(SubjectError):
    """The job type has no declared subject at all — not in the registry.

    Distinct from the base `SubjectError` a KNOWN job type raises when a
    unit is missing one of ITS declared components: that is a real defect
    in the unit and must propagate, while an unknown job type (registration,
    reprocessing — deliberately out of this registry's scope, see
    `SUBJECTS`) is the case `ProcessingUnit.dedup_key` falls back from.
    Catching the base class in that fallback would silently absorb the
    first kind of error too.
    """


@dataclasses.dataclass(frozen=True)
class JobTypeSubject:
    """One job type's declared subject grain and how to read it off a unit.

    Attributes
    ----------
    job_type : str
        The job type this declaration is for.
    grain : str
        One of the five declared grains.
    product_producing : bool
        Whether this job type's units mint product keys. Only
        exposure/SCA-grain job types do — `product_prefix()`
        (`pipeline/stages/context.py`) embeds `unit.key`, and that is a
        real S3 object path, so only the two job types whose units ARE a
        real exposure/SCA product may use it. Every other grain is a
        database-effect job type (co-design ruling 2's other half): it
        declares an EMPTY product set and calls `product_prefix()` never.
    components : tuple of str
        The `unit.fields` keys (in order) that make up this grain's
        subject, beyond the job type itself. Exposure/SCA-grain units
        need none named here — their subject is `(job_type, exposure,
        sca)`, read from the unit's own typed attributes, which is why
        the storage key and the dedup key coincide for exactly these two
        types.
    """

    job_type: str
    grain: str
    product_producing: bool
    components: tuple[str, ...] = ()

    def subject_for(self, unit: Any) -> tuple[Any, ...]:
        """The declared subject tuple for one unit of this job type.

        `(job_type, *values)` — job type is always the first component,
        because two job types sharing a grain (both DATE_FIELD, say)
        must never collide on subject identity.

        Raises
        ------
        SubjectError
            A declared component is absent from `unit.fields`. A subject
            with a missing component is not a degraded identity, it is no
            identity — silently omitting it would let two units with
            different missing components collide.
        """
        if self.grain == GRAIN_EXPOSURE_SCA:
            return (self.job_type, int(unit.exposure), int(unit.sca))
        values = []
        missing = []
        for name in self.components:
            value = unit.fields.get(name)
            if value is None:
                missing.append(name)
            else:
                values.append(value)
        if missing:
            raise SubjectError(
                f"job type {self.job_type!r} declares grain {self.grain!r} "
                f"needing {', '.join(self.components)}, but unit.fields is "
                f"missing {', '.join(missing)} (unit.fields={unit.fields!r})")
        return (self.job_type, *values)


# The declared set. One row per job type that gathering actually produces
# units for (co-design ruling 9 retired post-process: nothing live read what
# it stamped, so it carries no subject grain here rather than the job type
# itself being retired from the vocabulary the ruling was scoped to).
SUBJECTS: tuple[JobTypeSubject, ...] = (
    JobTypeSubject(JOB_TYPE_SCIENCE, GRAIN_EXPOSURE_SCA,
                  product_producing=True),
    JobTypeSubject(JOB_TYPE_REFERENCE_IMAGE, GRAIN_EXPOSURE_SCA,
                  product_producing=True),
    JobTypeSubject(JOB_TYPE_CATALOG_LOAD, GRAIN_DATE_SCA,
                  product_producing=False,
                  components=("proc_date", "sca")),
    JobTypeSubject(JOB_TYPE_CROSSMATCH, GRAIN_DATE_FIELD,
                  product_producing=False,
                  components=("proc_date", "field")),
    JobTypeSubject(JOB_TYPE_STATISTICS, GRAIN_FIELD,
                  product_producing=False,
                  components=("field",)),
    JobTypeSubject(JOB_TYPE_MERGE_CURRENCY, GRAIN_FIELD,
                  product_producing=False,
                  components=("field",)),
    JobTypeSubject(JOB_TYPE_SOURCE_CURRENCY, GRAIN_FIELD,
                  product_producing=False,
                  components=("field",)),
    JobTypeSubject(JOB_TYPE_MERGE_DEDUP, GRAIN_FIELD,
                  product_producing=False,
                  components=("field",)),
    # Alert production is exposure/SCA-shaped in ITS OWN unit (the unit is
    # the promoted attempt, keyed by the SCA attempt — see
    # `gather_alert_production_units`), but it is NOT product-producing:
    # it mints no product keys (co-design ruling 2's parenthetical:
    # "database-effect job types declare empty product sets and mint no
    # product keys" — alert production writes `alert_emissions` rows, not
    # S3 products). So its grain is EXPOSURE_SCA for dedup purposes (two
    # gathering passes over the same promoted attempt must collide) while
    # `product_producing=False` keeps it off `product_prefix()`.
    JobTypeSubject(JOB_TYPE_ALERT_PRODUCTION, GRAIN_EXPOSURE_SCA,
                  product_producing=False),
)

_BY_TYPE = {s.job_type: s for s in SUBJECTS}


def subject_for(job_type: str) -> JobTypeSubject:
    """The declared subject grain for one job type.

    Raises
    ------
    SubjectError
        The job type has no declared subject — either unknown, or (like
        registration) deliberately out of this registry's scope.
    """
    try:
        return _BY_TYPE[job_type]
    except KeyError:
        raise UnknownJobType(
            f"job type {job_type!r} has no declared subject grain; the "
            f"typed-identity registry covers "
            + ", ".join(sorted(_BY_TYPE))) from None


def is_product_producing(job_type: str) -> bool:
    """Whether this job type's units mint product keys.

    Product-producing types are exactly the two whose subject IS the
    storage-path key: science and reference-image. Everything else is a
    database-effect type per co-design ruling 2.
    """
    return subject_for(job_type).product_producing


def attempt_identity_fields(job_type: str, unit: Any) -> dict[str, Any]:
    """The applicable `AttemptIdentity` fields for one unit of this job type.

    Co-design ruling 2: "Attempt-record identifier columns carry only
    applicable identifiers — never a field number in an exposure/SCA
    sentinel." A `ProcessingUnit` always carries `exposure`/`sca` (the
    array layer's typed carrier, see `manifest.ProcessingUnit.key`'s
    docstring), but which of those, plus `field`/`processing_date`, is a
    REAL identifier of the attempt depends on the job type's declared
    grain:

    * EXPOSURE_SCA (science, reference-image, alert-production) —
      `exposure_id`/`sca` are real; `field`/`processing_date` are absent.
    * DATE_SCA (catalog load) — `sca` and `processing_date` are real;
      `exposure_id` is the synthetic date-ordinal carrier and is NOT an
      identifier — omitted here rather than written as if it were.
    * DATE_FIELD (crossmatch) — `field` and `processing_date` are real;
      `exposure_id`/`sca` are the synthetic carriers and are omitted.
    * FIELD (statistics, the three sweeps) — `field` is real; `exposure_id`
      (the field-as-exposure carrier, see `_per_field_units`) and `sca`
      are omitted.

    Returns a dict of exactly the keyword arguments
    `observability.attempts.AttemptIdentity` should receive beyond
    `run_id`/`logical_job_id` — never a key whose value is a synthetic
    carrier rather than a fact about the attempt.
    """
    declared = subject_for(job_type)
    if declared.grain == GRAIN_EXPOSURE_SCA:
        return {"exposure_id": unit.exposure, "sca": unit.sca,
               "sky_tile": getattr(unit.facts, "rtid", None)}
    if declared.grain == GRAIN_DATE_SCA:
        return {"sca": unit.fields.get("sca"),
               "processing_date": unit.fields.get("proc_date")}
    if declared.grain == GRAIN_DATE_FIELD:
        return {"field": unit.fields.get("field"),
               "processing_date": unit.fields.get("proc_date")}
    if declared.grain == GRAIN_FIELD:
        return {"field": unit.fields.get("field")}
    raise SubjectError(
        f"job type {job_type!r} declares grain {declared.grain!r}, which "
        f"has no attempt-identity mapping yet")
