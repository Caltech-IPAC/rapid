"""
File:    manifest.py

The submission manifest: the binding from array index to SCA identity.

Why a manifest exists at all. Batch gives an array child exactly one
thing to identify itself with — ``AWS_BATCH_JOB_ARRAY_INDEX``, an integer
in ``[0, size)``. The work the child must do is a per-SCA processing unit.
Something has to carry the mapping between the two, and design/compute.md
§ Submission names it: "The child's array index binds to its SCA identity
through the submission manifest the orchestrator builds to size the
array."

Two properties follow from that sentence and are enforced here.

The manifest sizes the array, so the manifest is authoritative: the array
size is ``len(manifest)``, never a separately-tracked number that could
drift from it.

The binding is immutable once submitted. A retried child keeps its
array-index identity (design/compute.md § Retry), so the same index must
resolve to the same SCA on the retry as on the first attempt. The
manifest is therefore built once, written once, and read — never rebuilt
from a re-query of ready work, which could return a different ordering.

What the manifest carries, since W4
-----------------------------------

The manifest is the third configuration home (batch-payload co-design,
Principles 4): the per-invocation facts, as distinct from the parameter
tree's operational configuration and the image's release-versioned
science content. Two additions make it that:

**The job type, and with it the whole route.** The manifest names what
kind of work this submission is; `submission.routes` binds that to a
workload class, a queue, a job definition, and a database lane as one
validated tuple. The entrypoint (W5) revalidates at startup and refuses
an incompatible route, so a right-definition/wrong-queue submission fails
before it can run rather than running on the wrong hardware.

**The per-invocation facts that used to ride a per-job .ini.** The
launcher queried the database at submit time, wrote the answers into a
749-line .ini re-serialized per job, and put that file on S3 for the
container to fetch. Those DB-derived answers are per-invocation facts and
belong here — carried in the manifest the job already reads, checksummed
with it, one object per submission rather than one per job.
`ProcessingUnit.fields` held an open dict for exactly this and `UnitFacts`
an all-optional object beside it; rule 11 prohibits both shapes, and D4
replaced them with one typed, closed, per-job-type payload
(`submission.payloads`) that declares its own facts and validates the
required ones at construction.

The .ini path itself is not deleted here — that is W5's switch and W6's
fence. This establishes where the facts live so the switch has somewhere
to switch to.

**The enumerated science overrides, since O1.** The manifest is also the
SOLE carrier of a per-run override of a science-affecting value
(design/compute.md § Job definitions). The sole enumerated field is the
reference-image observation window, whose authoritative value is release
content; it arrived here when the environment policy retired
`STARTREFIMMJDOBS`/`ENDREFIMMJDOBS`, on the rule that nothing able to
alter a science product is reachable from the environment. Because the
manifest and its checksum are bound into the attempt record, an override
is recorded by construction, which is what lets a promotion gate refuse
a product built under one.
"""

import dataclasses
import hashlib
import json
from typing import Any, Iterable, Iterator

from . import payloads
from .routes import (IMPLEMENTED_JOB_TYPES, JOB_TYPE_SCIENCE, Route,
                     RouteError, route_for, validate_route)
from .subjects import GRAIN_EXPOSURE_SCA, SubjectError

# Batch's hard ceiling on array children (design/compute.md § Submission).
MAX_ARRAY_SIZE = 10000

# An array job needs at least two children; Batch rejects size 1. A
# one-unit batch is submitted as a plain (non-array) job instead — see
# submit.py, which is where that distinction is acted on.
MIN_ARRAY_SIZE = 2


#: The manifest's enumerated science-override fields, and nothing else.
#:
#: design/compute.md § Job definitions: "The submission manifest is the sole
#: carrier for a per-run override of a science-affecting value: override
#: fields are enumerated in the manifest schema — the sole enumerated field
#: is the reference-image observation window, whose authoritative value is
#: release content."
#:
#: Enumerated, not an open dict, for the same reason the unit payloads name
#: their facts: an open bag of overrides is a second configuration surface
#: with no
#: schema, and "science-affecting" would stop being a decidable property of
#: a manifest. Adding an override is a deliberate schema change reviewed as
#: one.
OVERRIDE_REFERENCE_WINDOW = "reference_observation_window"
OVERRIDE_FIELDS = (OVERRIDE_REFERENCE_WINDOW,)


@dataclasses.dataclass(frozen=True)
class ReferenceObservationWindow:
    """A per-run override of the reference image's observation window.

    Half-open in MJD of observation, `[start, end)`, matching the overlap
    query's own bounds. Both ends are required: a window with one end
    supplied and the other defaulted is the shape the retired environment
    path had, where setting only `STARTREFIMMJDOBS` was a caught error but
    setting neither silently produced a different window than either.

    A product built under this override is not promotable to a community
    surface (design/compute.md § Job definitions). Nothing enforces that
    bar yet because no community promotion path exists; the manifest and
    its checksum are bound into the attempt record, so the override is
    recorded by construction and the bar binds when the gate arrives.
    """

    start_mjdobs: float
    end_mjdobs: float

    def __post_init__(self):
        for name in ("start_mjdobs", "end_mjdobs"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"{name} must be a number of days (MJD); got {value!r}")
        if self.end_mjdobs <= self.start_mjdobs:
            raise ValueError(
                f"the reference observation window is empty: "
                f"[{self.start_mjdobs}, {self.end_mjdobs}) — the window is "
                "half-open and its end must exceed its start")

    def to_dict(self) -> dict[str, float]:
        return {"start_mjdobs": float(self.start_mjdobs),
                "end_mjdobs": float(self.end_mjdobs)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReferenceObservationWindow":
        known = {"start_mjdobs", "end_mjdobs"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                "reference_observation_window carries unknown keys "
                + ", ".join(sorted(unknown)))
        missing = known - set(raw)
        if missing:
            raise ValueError(
                "reference_observation_window is incomplete; missing: "
                + ", ".join(sorted(missing))
                + ". Both ends are required — a half-specified window is the "
                "defect the environment path had.")
        return cls(start_mjdobs=raw["start_mjdobs"],
                   end_mjdobs=raw["end_mjdobs"])


# `UnitFacts` LIVED HERE AND IS RETIRED (rule 11, brief D item D4).
#
# It was a frozen dataclass of thirty members, every one `X | None = None`
# under one blanket rationale — the "all-optional fact object" rule 11
# names as prohibited beside the open `fields` dict. The blanket default
# destroyed the distinction that matters: "this job type has no such fact"
# and "this job type needs this fact and the submitter did not resolve it"
# were the same value, so a unit missing an input it could not run without
# was built, submitted, scheduled, and failed in a stage rather than at
# construction.
#
# Its members now live on the per-job-type payloads in
# `submission.payloads`, required where the job type requires them and
# optional only with a per-member stated reason. Two were dropped rather
# than moved — `images_to_coadd` and `reference_position` were declared and
# documented but never written by any gatherer and never read by any
# consumer, so carrying them forward would have carried the shape without
# the content.

@dataclasses.dataclass(frozen=True)
class ProcessingUnit:
    """One per-SCA unit of work — what a single array child processes.

    Frozen because a unit's identity is fixed the moment it enters a
    manifest: the retry contract binds an array index to an SCA identity
    for the life of the batch, and a mutable record would let that
    binding be edited out from under a retried child.

    Attributes
    ----------
    payload : UnitPayload
        The unit's TYPED, CLOSED, per-job-type payload
        (`submission.payloads`) — its grain plus the named components that
        grain declares. This replaced two things at once (rule 11): the
        `exposure: int` / `sca: int` pair every unit carried regardless of
        whether it had them, and the open `fields: dict[str, Any]` that fed
        subject derivation. A crossmatch unit no longer carries a
        date-ordinal in `exposure` and a `0` in `sca`; it carries a
        `CrossmatchPayload(proc_date=..., field=...)` and nothing else.

        Since D4 it is ALSO where the per-invocation facts live — what the
        launcher used to write into a per-job `.ini`, and what `UnitFacts`
        carried as thirty all-optional members until rule 11 retired that
        shape. `facts` below is an alias onto this one object, so a unit has
        exactly one carrier rather than a typed one beside an untyped one.
    """

    payload: Any

    @property
    def facts(self):
        """The per-invocation facts — WHICH ARE THE PAYLOAD.

        An alias, not a second object. `UnitFacts` used to be a separate
        member, and having two carriers is precisely what rule 11 calls a
        "parallel untyped fact carrier": the same fact could live in either,
        and which one a reader consulted decided what it saw.

        Kept as a NAME because `StageContext.fact()` and `optional_fact()`
        read facts by string name off `unit.facts`, and every science and
        reference-image stage calls them. Those call sites are asking the
        right question — "what did the submitter resolve for this unit?" —
        and the answer is now the typed payload. Retargeting the alias
        migrates all of them without touching one, while `fact()` itself
        gained the check that the name is one the payload DECLARES, so a
        typo is a named failure instead of a silent None.
        """
        return self.payload

    @property
    def job_type(self) -> str:
        """The job type this unit's payload declares.

        A unit now KNOWS its own job type, because its payload type is
        per-job-type. That removes the reason `dedup_key` took a `job_type`
        parameter — the parameter survives for callers that still pass one,
        and disagreeing with the payload is an error rather than a silent
        preference.
        """
        return self.payload.JOB_TYPE

    @property
    def exposure(self) -> int:
        """The exposure, for exposure/SCA-grain units ONLY.

        Raises for every other grain rather than returning a sentinel. That
        is the whole of rule 11's first clause: a crossmatch unit has no
        exposure, and the old representation answered that question with a
        processing-date ordinal.
        """
        return self._component("exposure")

    @property
    def sca(self) -> int:
        """The SCA, for grains that declare one (exposure/SCA, date/SCA)."""
        return self._component("sca")

    def _component(self, name):
        if name not in self.payload.COMPONENTS:
            raise SubjectError(
                f"a {self.payload.JOB_TYPE!r} unit is {self.payload.GRAIN!r}"
                f"-grained and has no {name!r}; it declares "
                f"{list(self.payload.COMPONENTS)}. Asking for one is the "
                f"sentinel-carrier defect rule 11 prohibits — the old "
                f"representation answered with a placeholder value.")
        return getattr(self.payload, name)

    @property
    def key(self) -> str:
        """Stable string identity for this unit, for storage paths and logs.

        NOT a database identity: it names a processing unit, and the same
        subject is processed by every run that reprocesses it. Use
        `logical_job_key` for anything that has to be unique across runs.

        **DERIVED FROM THE TYPED SUBJECT COMPONENTS** (rule 11). It used to
        be `f"{exposure:06d}/{sca:02d}"` for every job type, which is why a
        crossmatch unit needed a date ordinal in `exposure` — the key was
        built from fields that unit did not have. Now each grain renders its
        own declared components, so the key of a non-exposure grain is built
        from what that grain actually is.

        Exposure/SCA keys keep their exact previous spelling — zero-padded
        per the storage design's key schema (§ Key schema, component law:
        exposure 6 digits, SCA 2 digits) — because that string is embedded
        in every product key ever written under `product_prefix()`, and
        changing its shape would orphan every existing object.
        """
        payload = self.payload
        if payload.GRAIN == GRAIN_EXPOSURE_SCA:
            return f"{payload.exposure:06d}/{payload.sca:02d}"
        return "/".join(str(getattr(payload, name)).replace("/", "-")
                        for name in payload.COMPONENTS)

    def dedup_key(self, job_type: str = None) -> tuple[Any, ...]:
        """This unit's declared-subject identity.

        THE V25 DEFECT'S FIX, now structural rather than compensatory.
        `ReadyWorkAccumulator` (batching.py) used to dedup on `.key` for
        every job type, so two crossmatch units for different FIELDS of one
        processing date — both `exposure=<date ordinal>, sca=0` — carried
        the same `.key` and the second silently vanished from the waiting
        set. The first repair read the real identity out of `unit.fields`
        through the subject registry; this one removes the collision at its
        source, because the two units no longer share a representation to
        collide in. Both `.key` and this now distinguish them.

        `job_type` is accepted for the callers that still pass one and is
        CHECKED against the payload rather than trusted: a caller asking for
        a science subject from a crossmatch unit has a bug, and the old
        signature could not tell.

        **NO FALLBACK REMAINS.** The old code fell back to
        `(job_type, exposure, sca)` for job types outside the typed-identity
        registry — the path brief D requires re-examined. Every gathered job
        type is now in `payloads.PAYLOAD_TYPES`, and a job type without a
        payload cannot construct a unit at all, so the fallback has nothing
        left to catch and its removal cannot silently re-shape an identity.
        """
        if job_type is not None and job_type != self.payload.JOB_TYPE:
            raise SubjectError(
                f"asked for a {job_type!r} dedup key from a "
                f"{self.payload.JOB_TYPE!r} unit; a unit's payload declares "
                f"its job type and the two must agree")
        return self.payload.subject()

    def logical_job_key(self, run_id: Any, job_type: str) -> str:
        """This unit's RUN-SCOPED logical-job identity (review finding #3).

        `logical_jobs.logical_job_id` is a global primary key, so keying it on
        `key` alone collided across runs — and collided SILENTLY, because
        `create_logical_job` writes `ON CONFLICT DO NOTHING` precisely so that
        a replayed submission cannot rewrite a binding a running attempt
        believes in. Both halves are correct alone; together they meant
        reprocessing an exposure/SCA under a second run retained the FIRST
        run's execution binding, and a scheduler retry copied that stale
        manifest, image, release and run identity onto the new run's row.

        Defined here, on the unit, because BOTH sides must agree on it: the
        submitter writing the pre-created row and the runtime claiming it
        through the resolver. A second copy of this format string elsewhere is
        exactly how the two would drift apart.

        **KEYED BY THE DEDUP SUBJECT, NOT `.key`** (co-design ruling 2,
        continuing the fix above): two crossmatch units for the same
        processing date but different fields must resolve to two distinct
        logical jobs, not one — `.key` alone would collide them exactly as
        it collided them in the accumulator.
        """
        subject = "/".join(str(part) for part in self.dedup_key(job_type))
        return f"{run_id}:{subject}"

    def to_dict(self) -> dict[str, Any]:
        """The wire form: ONE typed payload, and nothing else.

        Rule 11's wire-format half, and the acceptance suite asserts it over
        the serialized JSON rather than over this code:

          * NO `fields` key, at any schema version this method can produce.
          * NO `facts` key either — that was the all-optional `UnitFacts`
            object, retired in D4. Its members are payload members now, so
            they serialize inside `payload` where their job type declares
            them, and a job type that does not declare one emits no key for
            it rather than emitting null.
          * No `exposure` or `sca` key for a grain that does not declare
            one: the payload renders exactly its own components.
        """
        return {"payload": self.payload.to_dict()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any], job_type: str = None
                  ) -> "ProcessingUnit":
        """Rebuild a unit from its wire form.

        **NO COMPATIBILITY PARSER.** A version-3 payload — the sentinel
        `{"exposure": ..., "sca": ..., "fields": {...}}` shape — is refused
        by `Manifest.from_dict` before reaching here, and nothing in this
        method reconstructs a typed subject from a sentinel exposure/SCA.
        Brief D is explicit that such a parser must not exist: it would
        keep the prohibited representation alive as an accepted input
        forever, which is how a "temporary" compatibility path becomes the
        format.

        `job_type` selects the payload type. It comes from the manifest,
        which names it once for all its units, rather than from each unit —
        a per-unit job type would let one manifest carry two, which nothing
        supports and the batch layer could not submit.
        """
        payload_raw = raw.get("payload")
        if payload_raw is None:
            raise ValueError(
                "a processing unit carries no `payload`; units written at "
                "manifest schema version 3 carried `exposure`/`sca`/`fields` "
                "instead, and those are refused rather than translated "
                "(rule 11 — no compatibility parser rebuilding typed "
                "subjects from sentinel carriers)")
        if job_type is None:
            raise ValueError(
                "rebuilding a processing unit needs its manifest's job "
                "type, which selects the payload type")
        if "facts" in raw:
            # A `facts` key means a manifest written before D4 retired the
            # all-optional carrier. Refused rather than merged into the
            # payload for the same reason a version-3 unit is refused: a
            # reader that accepts both shapes keeps the prohibited one alive
            # as a supported input, and the two disagree about which member
            # is authoritative whenever both carry one.
            raise ValueError(
                "a processing unit carries a `facts` key; the all-optional "
                "UnitFacts carrier was retired in D4 (rule 11) and its "
                "members are payload members now. Such a manifest predates "
                "schema version 4 and is refused, not translated.")
        return cls(payload=payloads.from_dict(job_type, payload_raw))


class Manifest:
    """An ordered index -> ProcessingUnit binding for one submission.

    Construction fixes the order; the index of a unit is its position.
    Since W4 it also names the job type, which fixes the route: the
    workload class that may run it, the queue it belongs on, and the
    database lane its transactions take.
    """

    # 2 (W4): units gained `facts`, the manifest gained `job_type`.
    # 3 (O1): the enumerated science-override fields. Version 1 and 2
    # manifests are refused rather than read on a guess — a version-1
    # manifest names no job type, and a job type is not something to
    # default; a version-2 manifest predates the override vocabulary, and
    # reading one as version 3 would silently claim it carried no override
    # when the concept did not exist to carry.
    # 4 (D3): units carry a TYPED PAYLOAD instead of `exposure`/`sca`/
    # `fields`. A version-3 unit is `{"exposure": ..., "sca": ...,
    # "fields": {...}}` — the sentinel carrier and the open dict rule 11
    # prohibits — and is REFUSED rather than translated. There is
    # deliberately NO compatibility parser rebuilding a typed subject from a
    # sentinel exposure/SCA: such a parser would keep the prohibited
    # representation alive as an accepted input indefinitely, and a
    # crossmatch unit's version-3 form does not contain the information to
    # rebuild from (its `field` is in `fields`, its `exposure` is a date
    # ordinal that means nothing to the typed payload). Manifests are
    # per-run artifacts written and read within one submission's lifetime,
    # so refusing the old shape strands nothing that is still running.
    SCHEMA_VERSION = 4

    def __init__(self, units: Iterable[ProcessingUnit],
                 batch_id: str | None = None,
                 job_type: str = JOB_TYPE_SCIENCE,
                 reference_observation_window: (
                     "ReferenceObservationWindow | None") = None):
        self.units: tuple[ProcessingUnit, ...] = tuple(units)
        self.batch_id = batch_id
        # Validated at construction, so an invalid job type cannot reach
        # S3 in a written manifest — the entrypoint's startup check is
        # the second line, not the first.
        self.route = route_for(job_type)
        # IMPLEMENTED, NOT MERELY IN THE MATRIX (wave-E finding #9).
        # `route_for` above only checks routes-matrix membership — the
        # matrix names reprocessing, catalog-load and crossmatch as job
        # types the design intends, but `IMPLEMENTED_JOB_TYPES` is the
        # narrower "a payload actually exists for this" fact
        # (`routes.validate_route`'s own comment, review finding #12: "The
        # matrix accepts [them]... but no payload implements them"). Until
        # now that check ran in exactly two places, both AFTER attempt rows
        # already exist: `routes.validate_route`, called by
        # `Manifest.validate_for` at CONTAINER STARTUP (after `SubmitJob`,
        # after `resolve_attempt` claimed the row — `routes.py:350-357`
        # documents exactly this ordering hazard for the class/queue checks
        # it does perform this early), and `job.py:615` inside the payload
        # itself, later still. A manifest naming an unimplemented-but-known
        # type therefore created real attempt rows and a real `SubmitJob`
        # call before either check ran, for a submission that could never
        # succeed. Checking here, at manifest CONSTRUCTION on the
        # submission side, rejects it before any row is claimed — the same
        # design principle `IMPLEMENTED_JOB_TYPES`'s existing call sites
        # already state, applied at the earliest point it can be.
        if job_type not in IMPLEMENTED_JOB_TYPES:
            raise RouteError(
                f"job type {job_type!r} is in the route matrix but has no "
                f"implementation in this image; implemented job types are: "
                + ", ".join(sorted(IMPLEMENTED_JOB_TYPES))
                + ". Rejected at manifest construction, before any attempt "
                "is claimed, for a submission that cannot run.")
        self.job_type = job_type
        # The sole enumerated science override. None means "no override":
        # the window's authoritative value is release content, and the
        # gathering layer reads it there.
        self.reference_observation_window = reference_observation_window
        if not self.units:
            raise ValueError("a manifest needs at least one processing unit")
        if len(self.units) > MAX_ARRAY_SIZE:
            raise ValueError(
                f"{len(self.units)} units exceeds Batch's {MAX_ARRAY_SIZE}-child "
                "array ceiling; the batcher must cut smaller batches")
        duplicates = self._duplicate_subjects()
        if duplicates:
            # Two children processing the same declared subject would write
            # the same products (product-producing types) or the same
            # database effect twice, from two attempts with different
            # identities.
            raise ValueError(
                "duplicate processing units in one manifest: "
                + ", ".join(sorted(str(d) for d in duplicates)))

    def _duplicate_subjects(self) -> set:
        """Units colliding on their DECLARED SUBJECT, not on `.key`.

        Co-design ruling 2. `.key` is the exposure/SCA-shaped storage
        carrier every unit has regardless of job type; a manifest of
        crossmatch units for one processing date shares one `.key` shape
        across every field it carries (`exposure` is the date ordinal,
        `sca` is a fixed `0` — see `gather_crossmatch_units`), so checking
        `.key` here would reject a perfectly good multi-field crossmatch
        manifest. `unit.dedup_key(self.job_type)` is the real per-unit
        identity for this manifest's job type; two units collide here only
        when it agrees.
        """
        seen: set = set()
        dupes: set = set()
        for unit in self.units:
            subject = unit.dedup_key(self.job_type)
            if subject in seen:
                dupes.add(subject)
            seen.add(subject)
        return dupes

    def __len__(self) -> int:
        return len(self.units)

    def __iter__(self) -> Iterator[ProcessingUnit]:
        return iter(self.units)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Manifest):
            return NotImplemented
        return (self.units == other.units
                and self.batch_id == other.batch_id
                and self.job_type == other.job_type
                and (self.reference_observation_window
                     == other.reference_observation_window))

    @property
    def has_science_override(self) -> bool:
        """Whether this submission carries any enumerated science override.

        What a promotion gate asks: a product built under any override is
        barred from a community surface. One property rather than a check
        per field, so adding an override field cannot leave the bar behind.
        """
        return any(getattr(self, name) is not None for name in OVERRIDE_FIELDS)

    def overrides_to_dict(self) -> dict[str, Any]:
        """The enumerated overrides that are actually set, serializable."""
        window = self.reference_observation_window
        return ({OVERRIDE_REFERENCE_WINDOW: window.to_dict()} if window
                else {})

    @property
    def workload_class(self) -> str:
        """The class of job definition that may run this submission."""
        return self.route.workload_class

    @property
    def db_lane(self) -> str:
        """The pooled lane this job type's transactions belong on."""
        return self.route.db_lane

    @property
    def ppid(self) -> int | None:
        """Pipeline identifier for the rows this submission writes."""
        return self.route.ppid

    def validate_for(self, workload_class: str,
                     queue_name: str | None = None,
                     queue_names: dict[str, str] | None = None) -> Route:
        """Check this manifest's route against a running container's own.

        What the entrypoint (W5) calls at startup: the class comes from
        its job definition's fixed command, the queue from
        ``AWS_BATCH_JQ_NAME``. A manifest whose job type does not belong
        to that class, or whose route does not run on that queue, is
        rejected before any work begins.

        Raises
        ------
        RouteError
            The route is not one the matrix allows.
        """
        return validate_route(self.job_type, workload_class,
                              queue_name=queue_name, queue_names=queue_names)

    def require_facts(self, *names: str) -> None:
        """Assert that every unit carries the named per-invocation facts.

        Called at startup by a job type that needs a fact for all of its
        units. Reports the offending indices rather than only the first,
        so one startup failure names the whole problem.

        Raises
        ------
        ValueError
            Naming the indices whose facts are incomplete.
        """
        bad: list[str] = []
        for index, unit in enumerate(self.units):
            try:
                unit.payload.require(*names)
            except ValueError as exc:
                bad.append(f"index {index} ({unit.key}): {exc}")
        if bad:
            raise ValueError(
                f"{len(bad)} of {len(self.units)} units are missing required "
                "per-invocation facts:\n  " + "\n  ".join(bad[:10])
                + ("\n  ..." if len(bad) > 10 else ""))

    @property
    def array_size(self) -> int:
        """The array size this manifest implies.

        The manifest sizes the array — this property is the only place
        the number comes from.
        """
        return len(self.units)

    @property
    def is_array(self) -> bool:
        """Whether this batch submits as an array job.

        Batch rejects an array of size 1, so a single-unit batch goes out
        as a plain job. The manifest is still built and still binds index
        0 to that unit, so the startup path is identical either way.
        """
        return len(self.units) >= MIN_ARRAY_SIZE

    def unit_for_index(self, index: int) -> ProcessingUnit:
        """Resolve one array child's own processing unit.

        Parameters
        ----------
        index : int
            The child's ``AWS_BATCH_JOB_ARRAY_INDEX``.

        Raises
        ------
        IndexError
            If the index falls outside the manifest. This is a real fault
            — a child of an array sized by a different manifest — and
            surfaces rather than silently processing the wrong SCA.
        """
        if not 0 <= index < len(self.units):
            raise IndexError(
                f"array index {index} is outside manifest of "
                f"{len(self.units)} units; the job's array was sized by a "
                "different manifest")
        return self.units[index]

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, written for the jobs to read back."""
        overrides = self.overrides_to_dict()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "job_type": self.job_type,
            "array_size": self.array_size,
            # Omitted when empty, per the absent-not-sentinel rule the unit
            # facts follow: no `overrides` key and an empty one would
            # otherwise be two spellings of the same absence, and the
            # checksum would distinguish them.
            **({"overrides": overrides} if overrides else {}),
            "units": [unit.to_dict() for unit in self.units],
        }

    def to_json(self, indent: int | None = None) -> str:
        # sort_keys so the serialized form is byte-stable: the manifest is
        # checksummed and compared across the submit/startup boundary.
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Manifest":
        version = raw.get("schema_version")
        if version != cls.SCHEMA_VERSION:
            raise ValueError(
                f"manifest schema_version {version!r} is not "
                f"{cls.SCHEMA_VERSION}; refusing to guess the layout")
        job_type = raw.get("job_type")
        if not job_type:
            # A schema-2 manifest always names its job type; without one
            # there is no route to validate, and defaulting to science
            # would route unknown work onto the prompt queue.
            raise ValueError(
                "manifest does not name a job_type; the job type fixes the "
                "route (class, queue, database lane) and is not defaultable")
        raw_overrides = raw.get("overrides") or {}
        unknown = set(raw_overrides) - set(OVERRIDE_FIELDS)
        if unknown:
            # An override this schema version does not know is refused, not
            # dropped: dropping it would run the job WITHOUT an override its
            # author asked for, and silently produce a promotable-looking
            # product from a run that was meant to be barred.
            raise ValueError(
                "manifest carries unknown override fields "
                + ", ".join(sorted(unknown))
                + "; overrides are enumerated in the schema and a newer "
                "submitter's override cannot be honoured by this reader")
        window_raw = raw_overrides.get(OVERRIDE_REFERENCE_WINDOW)
        window = (ReferenceObservationWindow.from_dict(window_raw)
                  if window_raw is not None else None)

        # The job type is threaded into every unit's reconstruction: it
        # selects the payload type. A manifest names its job type once, for
        # all its units — a per-unit job type would let one manifest carry
        # two, which the batch layer could not submit as one array.
        manifest = cls((ProcessingUnit.from_dict(u, job_type)
                        for u in raw["units"]),
                       batch_id=raw.get("batch_id"),
                       job_type=job_type,
                       reference_observation_window=window)
        # The recorded size is checked against the reconstructed one: a
        # mismatch means the array was sized from something other than
        # the units actually listed.
        recorded = raw.get("array_size")
        if recorded is not None and recorded != manifest.array_size:
            raise ValueError(
                f"manifest records array_size {recorded} but carries "
                f"{manifest.array_size} units")
        return manifest

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls.from_dict(json.loads(text))

    def checksum(self) -> str:
        """Content hash of the manifest, for submit/startup agreement.

        Lets a starting job prove it read the manifest the submitter
        wrote, rather than a truncated or superseded copy.
        """
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
