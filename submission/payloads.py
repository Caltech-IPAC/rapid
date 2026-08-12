"""
File:    payloads.py

Closed, discriminated per-job-type unit payloads — what replaces the open
`fields` dict and the sentinel exposure/SCA carrier (rule 11).

**The two defects this closes**, both named by rule 11 verbatim: "no
sentinel exposure/SCA values" and "no parallel untyped fact carriers
(all-optional fact objects or open field dictionaries duplicating typed
state)".

  * `ProcessingUnit` carried `exposure: int` and `sca: int` for EVERY job
    type. A crossmatch unit has neither — it is a (processing date, field)
    unit — so gathering put the date's ordinal in `exposure` and a fixed
    `0` in `sca`, and the class docstring said so out loud: "NOT this
    unit's identity, only its shape". That is a sentinel by the rule's
    plainest reading, and it has already caused one real defect: before
    the typed-subject layer existed, every crossmatch field of one date
    collided on `.key` and silently vanished from the ready set (the V25
    defect, `manifest.dedup_key`'s docstring).

  * `ProcessingUnit.fields: dict[str, Any]` was an open dictionary, kept
    open deliberately "so a new job type does not need a schema change to
    carry one extra value" — and it fed subject derivation, so the
    identity of a non-exposure/SCA unit was read out of an untyped dict
    with `.get()`. Rule 11 forbids exactly that trade, and D4 surrenders
    the convenience it bought: **a new job type henceforth declares its
    payload type.**

**THE GRAIN IS THE DISCRIMINANT.** Each payload class declares one grain
and the named, typed components that grain is made of. `exposure` exists
only on exposure/SCA-grain payloads; `sca` exists only where the grain
declares it — exposure/SCA and date/SCA. There is no payload on which a
non-applicable component is present-but-sentinel, and none carries a
parallel generic dictionary duplicating its typed members.

**REQUIRED MEANS REQUIRED, VALIDATED AT CONSTRUCTION.** Every component is
checked in `__post_init__`, failing loud with the component's name. A
subject derived from a payload therefore cannot be missing a component —
the payload could not have been built.

**OPTIONALITY IS ARGUED, NOT CONVENTIONAL.** `UnitFacts` made every member
`X | None = None`, so "absent" carried no information: a fact that does not
apply to this job type and a fact that applies and was not resolved looked
identical. Here the components are required. Where something is genuinely
optional it says why in its own comment — the standard rule 11 sets is that
optionality has a stated reason rather than a blanket default.

**CLASS CONSTANTS ARE PLAIN CLASS ATTRIBUTES, NOT DATACLASS FIELDS.**
`JOB_TYPE`, `GRAIN` and `COMPONENTS` are declared without annotations
precisely so `@dataclass` does not treat them as fields: as fields they
would join `__init__`'s signature, be overridable by a caller passing a
keyword, and — because they carry defaults — impose ordering constraints
on every subclass's real fields.
"""

import dataclasses
from typing import Any

from .routes import (JOB_TYPE_ALERT_PRODUCTION, JOB_TYPE_CATALOG_LOAD,
                     JOB_TYPE_CROSSMATCH, JOB_TYPE_MERGE_CURRENCY,
                     JOB_TYPE_MERGE_DEDUP, JOB_TYPE_REFERENCE_IMAGE,
                     JOB_TYPE_SCIENCE, JOB_TYPE_SOURCE_CURRENCY,
                     JOB_TYPE_STATISTICS)
from .subjects import (GRAIN_DATE_FIELD, GRAIN_DATE_SCA, GRAIN_EXPOSURE_SCA,
                       GRAIN_FIELD)


class PayloadError(ValueError):
    """A payload is missing a component its grain declares.

    Raised at CONSTRUCTION, which is the point: rule 11 says subjects are
    "typed and validated at creation", and a payload that could be built
    without its components would push the failure to whichever stage first
    read a missing one — three stages and one Batch submission later,
    reported as an AttributeError about a dict.
    """


class UnknownPayloadType(PayloadError):
    """No payload type is declared for this job type.

    FAILS LOUD rather than falling back to an exposure/SCA shape. That
    fallback is what the old `dedup_key` did for job types outside the
    typed-identity registry, and brief D requires the choice re-examined.
    The choice taken: bring every gathered job type into the registry (all
    nine are below) and make absence an error for anything else, so a new
    job type cannot quietly acquire a sentinel identity by omission.
    """

    def __init__(self, job_type):
        super().__init__(
            f"no unit payload type is declared for job type {job_type!r}; "
            f"every job type declares its payload (rule 11), so this is "
            f"either a typo or a job type added without one")
        self.job_type = job_type


def _require(value, name, job_type):
    if value is None:
        raise PayloadError(
            f"a {job_type} unit payload requires {name!r}; it is a declared "
            f"component of this job type's subject grain and a unit without "
            f"it has no identity")
    return value


def _freeze(payload, name):
    """Normalize a sequence member to a tuple, in place on a frozen dataclass.

    **WHY THIS IS NEEDED AT ALL.** These payloads are frozen and compared by
    value, and they round-trip through JSON — where every sequence comes back
    a `list`. So a payload built with `target_tables=("a", "b")` and the same
    payload parsed back from its own wire form compared UNEQUAL: tuple vs
    list. That is not a cosmetic difference; the units are deduplicated and
    matched by value, so a manifest written and read back described units the
    submitter would not recognise as the ones it wrote. Caught by the
    round-trip test in `submission/test/test_manifest_wire.py`.

    Normalizing on the way IN — rather than comparing leniently on the way
    out — is the fix that holds everywhere, because it means only one
    representation ever exists inside the process.

    `object.__setattr__` is how a frozen dataclass's `__post_init__` sets a
    field; the freeze is against later mutation by callers, not against the
    constructor completing its own initialization.
    """
    value = getattr(payload, name, None)
    if value is not None and not isinstance(value, tuple):
        object.__setattr__(payload, name, tuple(value))


class UnitPayload:
    """Base for the per-job-type payloads.

    Not itself a dataclass: it holds no fields, only the behaviour every
    payload shares and the three class constants each subclass sets.
    """

    #: Set by each concrete subclass. Deliberately un-annotated — see the
    #: module docstring's last paragraph.
    JOB_TYPE = ""
    GRAIN = ""
    COMPONENTS = ()

    #: Per-invocation facts this job type carries beyond its identity
    #: components. Empty for every current job type: the per-invocation
    #: facts all live on `UnitFacts`, which is narrowed rather than
    #: duplicated here. Declared so a job type that genuinely needs one has
    #: a typed place to put it instead of reaching for an open dict.
    INVOCATION_FACTS = ()

    def components(self) -> tuple:
        """This payload's declared components, in declaration order."""
        return tuple(getattr(self, name) for name in self.COMPONENTS)

    def subject(self) -> tuple:
        """The typed subject: job type first, then the declared components.

        Job type first for per-job-type collision avoidance — the same rule
        `submission.subjects.JobTypeSubject.subject_for` applies, and
        deliberately the same SHAPE, because the two must produce equal
        tuples for the dedup and logical-job keys to keep meaning what they
        meant before this change.
        """
        return (self.JOB_TYPE, *self.components())

    def to_dict(self) -> dict[str, Any]:
        """The wire form: the grain, then exactly the declared components.

        NO `fields` KEY, and no key for a component this grain does not
        declare — that absence is the wire-format half of rule 11, and the
        acceptance suite asserts it directly rather than trusting this
        comment.
        """
        payload = {"grain": self.GRAIN}
        for name in self.COMPONENTS:
            payload[name] = getattr(self, name)
        for name in self.INVOCATION_FACTS:
            value = getattr(self, name, None)
            if value is not None:
                payload[name] = value
        return payload


@dataclasses.dataclass(frozen=True)
class ExposureScaPayload(UnitPayload):
    """An exposure/SCA unit: science, reference-image, alert-production.

    The one grain for which `exposure` and `sca` are REAL identity rather
    than transport shape — which is why they live here and on no other
    payload.
    """

    exposure: int
    sca: int

    GRAIN = GRAIN_EXPOSURE_SCA
    COMPONENTS = ("exposure", "sca")

    def __post_init__(self):
        _require(self.exposure, "exposure", self.JOB_TYPE)
        _require(self.sca, "sca", self.JOB_TYPE)


@dataclasses.dataclass(frozen=True)
class SciencePayload(ExposureScaPayload):
    JOB_TYPE = JOB_TYPE_SCIENCE


@dataclasses.dataclass(frozen=True)
class ReferenceImagePayload(ExposureScaPayload):
    JOB_TYPE = JOB_TYPE_REFERENCE_IMAGE


@dataclasses.dataclass(frozen=True)
class AlertProductionPayload(ExposureScaPayload):
    """Alert production's unit IS the promoted attempt, keyed exposure/SCA.

    Exposure/SCA-grained for dedup — two gathering passes over the same
    promoted attempt must collide — while producing no products at all.

    Its per-invocation facts describe WHICH promotion this unit is drawing
    from. Every one is required and none is defaulted: a unit that cannot
    say which promoted difference image it is producing alerts for has
    nothing to produce alerts from, and the old open dict let it be built
    that way and fail four stages later.
    """

    JOB_TYPE = JOB_TYPE_ALERT_PRODUCTION

    #: The promoted attempt this unit draws from, and the release under
    #: which the promotion was recorded.
    promoted_attempt_id: int = None
    release_identity: str = None
    difference_image_pid: int = None
    #: The published NAME that filled the difference-image role, and where
    #: that binding was resolved from ("record" or "release"). Optional for
    #: a stated reason: `role_product` resolves the binding from the RECORD
    #: for every modern attempt and from the running release only for
    #: attempts authored before bindings were recorded at all, so a
    #: promotion of such an attempt genuinely has no product name to carry.
    difference_image_product: str = None
    role_resolved_from: str = None
    #: The alert watermark position this promotion was read at. Optional
    #: because the watermark is initialized separately and a first pass may
    #: legitimately precede it.
    promotion_sequence: int = None

    INVOCATION_FACTS = ("promoted_attempt_id", "release_identity",
                        "difference_image_pid", "difference_image_product",
                        "role_resolved_from", "promotion_sequence")

    def __post_init__(self):
        super().__post_init__()
        _require(self.promoted_attempt_id, "promoted_attempt_id",
                 self.JOB_TYPE)
        _require(self.release_identity, "release_identity", self.JOB_TYPE)
        _require(self.difference_image_pid, "difference_image_pid",
                 self.JOB_TYPE)


@dataclasses.dataclass(frozen=True)
class CatalogLoadPayload(UnitPayload):
    """A (processing date, SCA) unit.

    `sca` is a real component here — the grain declares it — while
    `exposure` is not, and is absent from this payload entirely rather than
    present as a date ordinal.
    """

    proc_date: str
    sca: int

    #: The table this unit loads. A submission fact: naming it in the
    #: manifest is what stops the job building a table name from its own
    #: environment and hoping it matches.
    target_table: str = None
    #: Which registered products this unit's catalogues come from. Optional
    #: for a stated reason, and the reason is a real disposition rather than
    #: a convenience: a unit with NO product inputs loads nothing and
    #: records that through its effect counts — the empty-product-set
    #: disposition, which is a legitimate outcome and not an error. An
    #: absent list and an empty list mean the same thing here, so the
    #: default is the empty list rather than None.
    product_inputs: tuple = ()

    JOB_TYPE = JOB_TYPE_CATALOG_LOAD
    GRAIN = GRAIN_DATE_SCA
    COMPONENTS = ("proc_date", "sca")
    INVOCATION_FACTS = ("target_table", "product_inputs")

    def __post_init__(self):
        _require(self.proc_date, "proc_date", self.JOB_TYPE)
        _require(self.sca, "sca", self.JOB_TYPE)
        _require(self.target_table, "target_table", self.JOB_TYPE)
        _freeze(self, "product_inputs")


@dataclasses.dataclass(frozen=True)
class CrossmatchPayload(UnitPayload):
    """A (processing date, field) unit — the V25 defect's home ground.

    NEITHER `exposure` NOR `sca` exists on this payload. That is the whole
    repair: the old representation put the date's ordinal in `exposure` and
    `0` in `sca`, so every field of one date shared a storage key.
    """

    proc_date: str
    field: int

    #: The per-field tables this unit crossmatches into. Required: a
    #: crossmatch unit with no declared targets would run and write nowhere.
    target_tables: tuple = ()

    JOB_TYPE = JOB_TYPE_CROSSMATCH
    GRAIN = GRAIN_DATE_FIELD
    COMPONENTS = ("proc_date", "field")
    INVOCATION_FACTS = ("target_tables",)

    def __post_init__(self):
        _require(self.proc_date, "proc_date", self.JOB_TYPE)
        _require(self.field, "field", self.JOB_TYPE)
        if not self.target_tables:
            raise PayloadError(
                f"a {self.JOB_TYPE} unit requires 'target_tables'; a unit "
                f"with no declared targets would run and write nowhere")
        _freeze(self, "target_tables")


@dataclasses.dataclass(frozen=True)
class FieldPayload(UnitPayload):
    """A field-grained unit: statistics, the merge and currency sweeps."""

    field: int

    #: The per-field table this sweep rebuilds or dedups. Required for the
    #: same reason as crossmatch's: the target is a submission fact.
    target_table: str = None

    GRAIN = GRAIN_FIELD
    COMPONENTS = ("field",)
    INVOCATION_FACTS = ("target_table",)

    def __post_init__(self):
        _require(self.field, "field", self.JOB_TYPE)
        _require(self.target_table, "target_table", self.JOB_TYPE)


@dataclasses.dataclass(frozen=True)
class StatisticsPayload(FieldPayload):
    JOB_TYPE = JOB_TYPE_STATISTICS


@dataclasses.dataclass(frozen=True)
class MergeCurrencyPayload(FieldPayload):
    JOB_TYPE = JOB_TYPE_MERGE_CURRENCY


@dataclasses.dataclass(frozen=True)
class SourceCurrencyPayload(FieldPayload):
    JOB_TYPE = JOB_TYPE_SOURCE_CURRENCY


@dataclasses.dataclass(frozen=True)
class MergeDedupPayload(FieldPayload):
    JOB_TYPE = JOB_TYPE_MERGE_DEDUP


#: Every declared payload type, by job type. A job type absent from this
#: mapping has no payload type and therefore cannot be gathered — which is
#: D4's surrendered convenience made concrete: adding a job type is now a
#: schema change here, deliberately, rather than one more key in an open
#: dict nobody reviews.
PAYLOAD_TYPES = {
    JOB_TYPE_SCIENCE: SciencePayload,
    JOB_TYPE_REFERENCE_IMAGE: ReferenceImagePayload,
    JOB_TYPE_ALERT_PRODUCTION: AlertProductionPayload,
    JOB_TYPE_CATALOG_LOAD: CatalogLoadPayload,
    JOB_TYPE_CROSSMATCH: CrossmatchPayload,
    JOB_TYPE_STATISTICS: StatisticsPayload,
    JOB_TYPE_MERGE_CURRENCY: MergeCurrencyPayload,
    JOB_TYPE_SOURCE_CURRENCY: SourceCurrencyPayload,
    JOB_TYPE_MERGE_DEDUP: MergeDedupPayload,
}


def payload_type(job_type):
    """The payload class for one job type, or `UnknownPayloadType`."""
    try:
        return PAYLOAD_TYPES[job_type]
    except KeyError:
        raise UnknownPayloadType(job_type) from None


def build(job_type, **components):
    """One payload of the type this job type declares.

    The single construction entry point, so gathering never names a payload
    class directly and a job type's payload shape can change in one place.
    """
    return payload_type(job_type)(**components)


def from_dict(job_type, raw):
    """Rebuild a payload from its wire form.

    **UNKNOWN KEYS ARE REFUSED**, not dropped. A key this payload type does
    not declare means the manifest was written by a submitter with a
    different schema, and quietly ignoring it would run the job against a
    subset of what the submitter described. `UnitFacts.from_dict` already
    took this position for facts; this is the same rule for components.

    The `grain` key is consumed here rather than passed to the constructor:
    it is a discriminant written for readers of the wire format, and the
    payload class already knows its own grain — accepting it as a
    constructor argument would let a manifest assert a grain the class
    disagrees with. A DISAGREEMENT is an error rather than a silent
    preference for either side.
    """
    cls = payload_type(job_type)
    values = dict(raw)
    declared_grain = values.pop("grain", None)
    if declared_grain is not None and declared_grain != cls.GRAIN:
        raise PayloadError(
            f"the manifest declares grain {declared_grain!r} for a "
            f"{job_type!r} unit, but that job type's payload is "
            f"{cls.GRAIN!r}; the manifest and this release disagree about "
            f"what kind of thing this unit is")

    known = set(cls.COMPONENTS) | set(cls.INVOCATION_FACTS)
    unknown = set(values) - known
    if unknown:
        raise PayloadError(
            f"a {job_type!r} unit payload carries unknown keys "
            + ", ".join(sorted(unknown))
            + f"; this job type declares {sorted(known)}")
    return cls(**values)
