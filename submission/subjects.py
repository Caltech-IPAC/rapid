"""
File:    subjects.py

Typed work-unit identity: every job type's declared subject grain.

Integration review 2026-08, composite ruling 2 (rapid_plan/decisions.md):

    "Every job type declares its subject grain (exposure/SCA, date/SCA,
    date/field, field, or release-unit); deduplication and logical-job
    identity derive from the declared subject; the storage-path key
    (exposure/SCA) is retained only for product-producing job types
    (product keys embed it; database-effect types mint none)."

WHY A REGISTRY. The subject a unit is deduplicated and keyed by is a
property of its JOB TYPE. This module is the one place that mapping is
written down, so `ProcessingUnit.dedup_key` (manifest.py) and the
operator's gatherer registry (`pipeline/operator/gathering.py`) read the
same declaration rather than each re-deriving it.

THE V25 DEFECT THIS CLOSED. Before this module existed, deduplication was
`unit.key` for every job type — `f"{exposure:06d}/{sca:02d}"`. Crossmatch
gathering yielded one unit per FIELD, all carrying the processing date's
ordinal in `exposure` and a fixed `0` in `sca`, so every field of one date
produced the SAME storage-path key and the accumulator's `_pending_keys`
set (a plain `set[str]` of `.key` values, `submission/batching.py`)
silently dropped every field after the first. `dedup_key` was what
`ReadyWorkAccumulator` and `ProcessingUnit.logical_job_key` changed to use,
so two units are the same unit only when their DECLARED SUBJECT agrees.

**AND WHAT D3/D4 CHANGED (rule 11).** That first repair left the sentinel
representation in place and routed identity AROUND it: the real components
lived in an open `ProcessingUnit.fields` dict, which this module read with
`.get()`. Rule 11 prohibits both halves — the sentinel carrier and the
untyped parallel dict — so units now carry a typed, closed, per-job-type
payload (`submission.payloads`) that declares its own grain and components
and validates them at construction. A crossmatch unit no longer HAS an
`exposure` to put a date ordinal in. This registry survives as the
job-type -> grain mapping and as the place the two declarations are checked
against each other; what it no longer does is reconstruct identity from an
untyped dict.
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
        Whether this job type's units mint object keys. Only
        exposure/SCA-grain job types do — `product_prefix()`
        (`pipeline/stages/context.py`) embeds `unit.key`, and that is a
        real S3 object path, so only the two job types whose units ARE a
        real exposure/SCA product may use it. Every other grain is a
        database-effect job type (co-design ruling 2's other half): it
        declares an EMPTY product set and calls `product_prefix()` never.
    components : tuple of str
        The payload component names (in order) that make up this grain's
        subject, beyond the job type itself. Kept as a declaration here so
        the registry and the payload type can be CHECKED against each other
        (`subject_for` does exactly that) rather than one being derived from
        the other — two independent statements that must agree catch a
        mismatch that one statement cannot.
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

        **READ FROM THE UNIT'S TYPED PAYLOAD** (rule 11). This used to read
        `unit.fields.get(name)` — an open dictionary — which is the "parallel
        untyped fact carrier... duplicating typed state" the rule prohibits,
        and it meant a subject's components were validated here, at
        derivation time, rather than at the unit's creation. Now the payload
        validates its own components at construction
        (`submission.payloads`), so a unit that exists has a complete
        subject by construction and this method reads declared attributes
        rather than probing a dict.

        The fail-loud path is kept for the case that remains reachable: a
        unit whose payload declares a DIFFERENT job type than the one being
        asked for. That is a caller bug, and the old signature could not
        detect it at all.

        Raises
        ------
        SubjectError
            The unit's payload does not belong to this job type, or does not
            declare the components this grain needs. A subject with a
            missing component is not a degraded identity, it is no identity.
        """
        payload = getattr(unit, "payload", None)
        if payload is None:
            raise SubjectError(
                f"job type {self.job_type!r} needs a typed unit payload to "
                f"derive a subject; this unit carries none. Units written "
                f"against the pre-rule-11 representation carried an open "
                f"`fields` dict instead, and it is refused rather than read "
                f"(no compatibility parser rebuilds a typed subject from a "
                f"sentinel carrier).")
        if payload.JOB_TYPE != self.job_type:
            raise SubjectError(
                f"asked for a {self.job_type!r} subject from a "
                f"{payload.JOB_TYPE!r} unit; a payload declares its own job "
                f"type and the two must agree")
        if payload.GRAIN != self.grain:
            raise SubjectError(
                f"job type {self.job_type!r} declares grain {self.grain!r} "
                f"but its payload declares {payload.GRAIN!r}; the registry "
                f"and the payload type disagree")
        return payload.subject()


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
    # it mints no object keys (co-design ruling 2's parenthetical:
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
    """Whether this job type's units mint object keys.

    Product-producing types are exactly the two whose subject IS the
    storage-path key: science and reference-image. Everything else is a
    database-effect type per co-design ruling 2.
    """
    return subject_for(job_type).product_producing


# ---------------------------------------------------------------------------
# THE input_scope GRAMMAR (integration review, IR-13-a). ONE shared
# build/parse pair, not two grammars that could drift.
# ---------------------------------------------------------------------------
#
# `pipeline.seams._input_scope_for` already computes `work_units.input_scope`
# from a manifest unit at ATTACH time (drop the leading job_type element of
# `subject_for(job_type).subject_for(unit)`, join the rest with "/" — see
# that function's own v1-stringification docstring). The campaign gatherer
# (IR-13-a) needs the INVERSE at GATHER time: given a test-class work unit's
# stored `input_scope` string, recover the (exposure, sca) pair so it can
# build the same science-shaped `ProcessingUnit` `gather_science_units`
# would. And the mock transformer's W2-fallback campaign creator
# (`pipeline.mock.transformer.create_mock_campaign_from_staged`) needs the
# FORWARD direction at CREATE time, so a unit it creates and the unit
# `_attach_work_unit` later finds are the SAME row — one grammar, read both
# ways, or creation and attachment silently mint two different scope
# strings for what is supposed to be one unit.
#
# `build_input_scope`/`parse_input_scope` below are that one grammar,
# extracted from `pipeline.seams._input_scope_for` rather than duplicated:
# that function is amended to call `build_input_scope` (see its own
# docstring for the delegation), so there is exactly one place either
# direction is written down.


def input_scope_from_subject(subject: tuple) -> str:
    """`work_units.input_scope` from an already-computed subject tuple.

    The same grammar as `build_input_scope`, for callers that HAVE a
    subject but no unit. The mock transformer is the one such caller: it
    enumerates already-registered L2 rows and needs their input scopes to
    create work units, and it has the `(exposure, sca)` identity in hand
    without ever building a processing unit.

    It used to build a throwaway `ProcessingUnit` to pass to
    `build_input_scope`. That worked while a unit was two integers; since
    D4 a science payload requires its resolved facts, so the throwaway
    would have had to invent eleven values it does not have and does not
    use — inventing data to satisfy a validator is exactly what that
    validator exists to prevent. Splitting the subject-only path out is the
    honest fix: one grammar, two entry points, no fabricated facts.
    """
    return "/".join(str(component) for component in subject[1:])


def build_input_scope(job_type: str, unit: Any) -> str:
    """`work_units.input_scope` for one manifest unit — the FORWARD grammar.

    `subject_for(job_type).subject_for(unit)` computes the declared-subject
    tuple `(job_type, *values)`; this drops the leading `job_type` (the
    `work_units.job_type` column already carries it — the partial unique
    index is scoped by that column, so repeating it inside `input_scope`
    would be redundant) and joins the rest with `/`, matching the delimited
    shape `ProcessingUnit.key`/`logical_job_key` already use elsewhere
    (`"run-1:science/90000/1"`) rather than a second serialization
    convention for what is, in every declared grain, a short tuple of
    ints/strings with no nesting.

    **NO EXPOSURE/SCA FALLBACK** (rule 11). This used to catch
    `UnknownJobType` and fall back to `(exposure, sca)` — the storage-path
    shape — which was safe only while every unit carried that pair whatever
    its grain. With typed payloads a field-grained unit has no exposure at
    all, so the fallback would raise rather than quietly mis-scope; and a
    job type with no declared subject cannot build a payload, so no unit of
    one can reach here. The raise is the correct outcome, and it is the same
    removal `attempt_identity_fields` and `ProcessingUnit.dedup_key` made.
    """
    return input_scope_from_subject(subject_for(job_type).subject_for(unit))


def parse_exposure_sca_scope(input_scope: str) -> tuple[int, int]:
    """The (exposure, sca) pair an EXPOSURE_SCA-grain `input_scope` names —
    the REVERSE of `build_input_scope` for exactly the grain science and
    reference-image (and the campaign gatherer's science-shaped units) use.

    Only the exposure/SCA grain is invertible generically: `build_input_scope`
    drops `job_type` and joins the remaining subject components positionally,
    and for EXPOSURE_SCA those components are always exactly `(exposure,
    sca)` in that order — so parsing back is
    unambiguous — `ExposureScaPayload.COMPONENTS` is `("exposure", "sca")`
    in that order, and `subject()` emits them so. The other grains
    (date/SCA, date/field, field) are not
    parsed here: nothing in this v1 needs to invert them, and a generic
    inverse would have to know each grain's component NAMES, which only
    `SUBJECTS`' declarations carry, not the string itself.

    Raises
    ------
    SubjectError
        `input_scope` is not exactly two `/`-delimited integer components —
        a campaign work unit whose stored scope does not match the grammar
        this same module writes is a real defect (a hand-inserted row, a
        future grain reusing this v1's gatherer by mistake), not a value to
        coerce or skip.
    """
    parts = input_scope.split("/")
    if len(parts) != 2:
        raise SubjectError(
            f"input_scope {input_scope!r} is not an exposure/SCA scope "
            f"(expected exactly two '/'-delimited components, got "
            f"{len(parts)}); the campaign gatherer only reads exposure/SCA-"
            f"grain test-campaign units")
    try:
        exposure, sca = (int(part) for part in parts)
    except ValueError:
        raise SubjectError(
            f"input_scope {input_scope!r} does not parse as two integers "
            f"(exposure, sca)") from None
    return exposure, sca


def attempt_identity_fields(job_type: str, unit: Any) -> dict[str, Any]:
    """The applicable `AttemptIdentity` fields for one unit of this job type.

    Co-design ruling 2: "Attempt-record identifier columns carry only
    applicable identifiers — never a field number in an exposure/SCA
    sentinel." Which identifiers are REAL for an attempt depends on the job
    type's declared grain:

    * EXPOSURE_SCA (science, reference-image, alert-production) —
      `exposure_id`/`sca` are real; `field`/`processing_date` are absent.
    * DATE_SCA (catalog load) — `sca` and `processing_date` are real.
    * DATE_FIELD (crossmatch) — `field` and `processing_date` are real.
    * FIELD (statistics, the three sweeps) — `field` is real.

    **THE OMISSIONS ARE NOW STRUCTURAL** (rule 11). This function used to
    read every value out of the open `fields` dict and carefully omit the
    synthetic carriers — the date-ordinal in `exposure_id`, the fixed
    `sca=0` — because a unit carried them whether or not they meant
    anything. With typed payloads there is nothing to omit: a crossmatch
    payload has no `exposure` attribute at all, so writing one into an
    attempt record is no longer a discipline this function enforces but a
    shape the type system refuses. What survives is the MAPPING from grain
    to attempt-record column names, which is genuinely this module's job.

    Returns a dict of exactly the keyword arguments
    `observability.attempts.AttemptIdentity` should receive beyond
    `run_id`/`logical_job_id`.
    """
    declared = subject_for(job_type)
    payload = getattr(unit, "payload", None)
    if payload is None:
        raise SubjectError(
            f"a {job_type!r} unit needs a typed payload to derive its "
            f"attempt identity; this unit carries none")
    if declared.grain == GRAIN_EXPOSURE_SCA:
        return {"exposure_id": payload.exposure, "sca": payload.sca,
               "sky_tile": getattr(unit.facts, "rtid", None)}
    if declared.grain == GRAIN_DATE_SCA:
        return {"sca": payload.sca,
               "processing_date": payload.proc_date}
    if declared.grain == GRAIN_DATE_FIELD:
        return {"field": payload.field,
               "processing_date": payload.proc_date}
    if declared.grain == GRAIN_FIELD:
        return {"field": payload.field}
    raise SubjectError(
        f"job type {job_type!r} declares grain {declared.grain!r}, which "
        f"has no attempt-identity mapping yet")
