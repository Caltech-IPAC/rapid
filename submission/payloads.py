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
    if value is None:
        return
    # NESTED SEQUENCES ARE NORMALIZED TOO. `coadd_input_identities` is a
    # sequence OF sequences — `((expid, sca, infobits), ...)` — and JSON
    # returns every level as a list, so freezing only the outer one would
    # leave `((1,2,3),)` and `([1,2,3],)` comparing unequal for the same
    # manifest written and read back.
    normalized = tuple(
        tuple(item) if isinstance(item, (list, tuple)) else item
        for item in value)
    if normalized != value:
        object.__setattr__(payload, name, normalized)


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

    def declares(self, name) -> bool:
        """Is `name` a component or invocation fact this payload declares?

        The question `StageContext.fact()` asks before reading. Under the
        old open dict there was nothing to ask: a name nobody had ever
        declared returned None exactly like a declared-but-unresolved one.
        """
        return name in self.COMPONENTS or name in self.INVOCATION_FACTS

    def require(self, *names: str) -> None:
        """Assert that named facts are present, naming every absent one.

        Carried over from `UnitFacts.require` unchanged in contract — a job
        type's startup calls it to turn "this job needs a science image"
        into one named failure instead of an AttributeError three stages
        later. What changed is that it can now also catch a name this job
        type does not declare AT ALL, which the old all-optional object
        reported as merely absent.
        """
        missing, undeclared = [], []
        for name in names:
            if not self.declares(name):
                undeclared.append(name)
            elif getattr(self, name, None) is None:
                missing.append(name)
        if undeclared:
            raise PayloadError(
                f"a {self.JOB_TYPE} unit does not declare "
                + ", ".join(undeclared)
                + f"; it declares {sorted(set(self.COMPONENTS) | set(self.INVOCATION_FACTS))}")
        if missing:
            raise ValueError(
                "the manifest does not carry required per-invocation facts: "
                + ", ".join(missing))

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
            # ABSENT FACTS ARE OMITTED, and an empty sequence counts as
            # absent. This is `UnitFacts.to_dict`'s adopted absent-not-
            # sentinel rule, carried over: a key that is not there is a fact
            # the submitter did not resolve, while a key present and empty
            # would be a claim that the value is known to be nothing. The
            # sequence members default to `()` rather than None so callers
            # can iterate them without a guard, which makes the emptiness
            # check necessary here rather than a None check sufficing.
            if value is None or (isinstance(value, tuple) and not value):
                continue
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
class ImagingPayload(ExposureScaPayload):
    """The per-invocation facts an L2-image job type resolves at submission.

    Shared by science and reference-image because ONE gathering helper
    resolves them for both — `submission.gathering.science_facts`, called
    with `make_references` deciding only which units are yielded, not what
    facts they carry. Two payload classes duplicating this list would be two
    places for it to drift.

    **THESE WERE `UnitFacts`, AND EVERY ONE WAS `X | None = None`.** Rule 11
    names that shape as prohibited alongside the open dict: "no parallel
    untyped fact carriers (all-optional fact objects or open field
    dictionaries duplicating typed state)". The distinction the blanket
    default destroyed is the one that matters here — "this job type does not
    have this fact" and "this job type needs this fact and the submitter did
    not resolve it" looked identical, so a unit missing an input it could not
    run without was built, submitted and scheduled, and failed in a stage.

    Below, the members the L2 lookup ALWAYS resolves are required and
    validated at construction. The ones that are legitimately absent are
    optional WITH A STATED REASON, per member — which is the standard rule 11
    sets, and the reason each comment exists.
    """

    # -- required: the L2 row's own identity and geometry ------------------
    # `science_facts` resolves every one of these from `get_l2filemeta_record`
    # and `get_info_for_l2file` before it returns, and raises `GatheringError`
    # if either row is missing. A unit that reached submission without them
    # has no science image to process.
    rid: int = None
    fid: int = None
    field: int = None
    rtid: int = None
    expid: int = None
    mjdobs: float = None
    exptime: float = None
    infobits: int = None
    science_image_uri: str = None
    sky_position: dict = None
    tile_position: dict = None

    # -- optional, each for its own stated reason --------------------------

    #: The filter's NAME, resolved from `fid` at submit time so the job does
    #: not re-query for a string. Optional because it is a convenience
    #: denormalization of `fid` (which IS required): a manifest written
    #: before it was resolved still names the filter by id, and the one
    #: reader (`reference_image.add_header_keywords`) is writing a FITS
    #: header comment, not making a decision.
    filter_name: str = None

    #: The best PSF for this SCA and filter. Optional because a unit whose
    #: SCA has no registered PSF is a real state — `science_facts` looks it
    #: up and leaves both absent when the lookup finds nothing — and the
    #: science stage's `download_inputs` is where that becomes a failure,
    #: with the message naming the PSF rather than the manifest.
    psfid: int = None
    psf_uri: str = None

    #: The reference image to subtract against. **ABSENCE IS THE BRANCH**,
    #: not an omission: `optional_fact("reference_image_id")` returning None
    #: is what selects build-a-reference over use-an-existing-one. This is
    #: the case `StageContext.optional_fact`'s own docstring cites as the
    #: clearest example of a legitimately-absent fact, and it is why that
    #: accessor exists at all.
    reference_image_id: int = None
    reference_image_uri: str = None
    reference_image_infobits: int = None
    reference_image_version: int = None
    #: Which pipeline produced the reference image. Carried because it is
    #: not derivable from the job's own type — a reference image may have
    #: been built by the dedicated reference pipeline or by an earlier
    #: science run. Optional exactly when `reference_image_id` is.
    reference_image_ppid: int = None

    #: The tessellation identifiers the science image overlaps. Optional
    #: because it is a submit-time convenience for the coadd path: only the
    #: reference-image build reads it, and only when it is building.
    overlapping_fields: tuple = ()
    reference_overlapping_fields: tuple = ()

    INVOCATION_FACTS = (
        "rid", "fid", "field", "rtid", "expid", "mjdobs", "exptime",
        "infobits", "science_image_uri", "sky_position", "tile_position",
        "filter_name", "psfid", "psf_uri", "reference_image_id",
        "reference_image_uri", "reference_image_infobits",
        "reference_image_version", "reference_image_ppid",
        "overlapping_fields", "reference_overlapping_fields",
    )

    #: The members above that a unit cannot be built without. Named as a
    #: tuple rather than checked inline so the required set is readable as
    #: one list and so subclasses can extend it.
    REQUIRED_FACTS = (
        "rid", "fid", "field", "rtid", "expid", "mjdobs", "exptime",
        "infobits", "science_image_uri", "sky_position", "tile_position",
    )

    def __post_init__(self):
        super().__post_init__()
        for name in self.REQUIRED_FACTS:
            _require(getattr(self, name), name, self.JOB_TYPE)
        _freeze(self, "overlapping_fields")
        _freeze(self, "reference_overlapping_fields")


@dataclasses.dataclass(frozen=True)
class SciencePayload(ImagingPayload):
    JOB_TYPE = JOB_TYPE_SCIENCE


@dataclasses.dataclass(frozen=True)
class ReferenceImagePayload(ImagingPayload):
    """A reference-image build: the imaging facts plus its coadd inputs.

    The three coadd members are REQUIRED here and absent from
    `SciencePayload`, which is the whole point of discriminating by job
    type: a reference-image unit that does not say what it coadds cannot
    build anything, while a science unit has no coadd inputs at all and
    would have carried three permanently-None members under the old shape.
    """

    JOB_TYPE = JOB_TYPE_REFERENCE_IMAGE

    #: The CSV listing the coadd inputs, the checksum of exactly the bytes
    #: that were published, and the inputs' own MISSION identities
    #: `((expid, sca, infobits), ...)` — the last being an input component
    #: of this product's deterministic key (rule 10).
    #:
    #: **OPTIONAL AT CONSTRUCTION, AND THE REASON IS THE TWO-STAGE GATHER.**
    #: Reference-image gathering is two passes over one unit:
    #: `gather_science_units(make_references=True)` yields CANDIDATES from
    #: the L2 rows, and `gather_reference_units` then aggregates each
    #: candidate's overlapping frames, publishes the CSV, and re-yields the
    #: unit with these three resolved. A candidate genuinely does not have
    #: them yet — the overlap query has not run — so requiring them here
    #: would make the first stage unable to build the unit the second stage
    #: exists to complete.
    #:
    #: They are required of a SUBMITTABLE unit, and that is enforced where
    #: it is true: `Manifest.require_facts` at submission, and
    #: `pipeline/registration/products_identity.py` refuses to compute a
    #: product key without `coadd_input_identities` rather than computing
    #: one over inputs it does not have. Optionality with a stated reason is
    #: exactly what rule 11 asks for in place of a blanket default; what it
    #: prohibits is `X | None = None` on everything under one rationale.
    coadd_inputs_uri: str = None
    coadd_inputs_checksum: str = None
    coadd_input_identities: tuple = ()

    INVOCATION_FACTS = ImagingPayload.INVOCATION_FACTS + (
        "coadd_inputs_uri", "coadd_inputs_checksum", "coadd_input_identities",
    )

    #: What a reference-image unit needs before it may be SUBMITTED, as
    #: distinct from what it needs to exist as a candidate. Named here so
    #: the submission-time check reads the declaration rather than
    #: repeating the list.
    SUBMITTABLE_FACTS = ("coadd_inputs_uri", "coadd_inputs_checksum",
                         "coadd_input_identities")

    def __post_init__(self):
        super().__post_init__()
        _freeze(self, "coadd_input_identities")


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

    #: The sky tile this unit's alerts fall in. Optional for a stated
    #: reason: `resolve_ownership` records it as the attempt's `sky_tile`
    #: when it is known, and an alert unit is identified by its promoted
    #: difference image rather than by the tile, so an absent one costs the
    #: attempt record a cross-reference and costs the job nothing.
    rtid: int = None

    INVOCATION_FACTS = ("promoted_attempt_id", "release_identity",
                        "difference_image_pid", "difference_image_product",
                        "role_resolved_from", "promotion_sequence", "rtid")

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

    **ONE EXCEPTION TYPE FOR ONE FAILURE.** A missing component raises
    `PayloadError` whichever way it is missing. Without this translation the
    caller saw two different exceptions for the same mistake: `TypeError`
    from the dataclass constructor when the keyword was OMITTED, and
    `PayloadError` from `__post_init__` when it was passed as `None`. That
    split is invisible until someone writes `except PayloadError` and it
    silently does not catch half the cases — which is exactly what a caller
    validating a submission would write. The original is chained, so the
    constructor's own message about which argument it wanted is not lost.
    """
    cls = payload_type(job_type)
    try:
        return cls(**components)
    except TypeError as exc:
        raise PayloadError(
            f"a {job_type} unit payload could not be built: {exc}. It "
            f"declares {sorted(set(cls.COMPONENTS) | set(cls.INVOCATION_FACTS))}"
        ) from exc


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
