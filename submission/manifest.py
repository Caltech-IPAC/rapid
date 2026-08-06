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
`ProcessingUnit.fields` held an open dict for exactly this; `UnitFacts`
gives the recurring ones names and types, while `fields` stays open for
what a job type needs beyond them.

The .ini path itself is not deleted here — that is W5's switch and W6's
fence. This establishes where the facts live so the switch has somewhere
to switch to.
"""

import dataclasses
import hashlib
import json
from typing import Any, Iterable, Iterator

from .routes import JOB_TYPE_SCIENCE, Route, route_for, validate_route

# Batch's hard ceiling on array children (design/compute.md § Submission).
MAX_ARRAY_SIZE = 10000

# An array job needs at least two children; Batch rejects size 1. A
# one-unit batch is submitted as a plain (non-array) job instead — see
# submit.py, which is where that distinction is acted on.
MIN_ARRAY_SIZE = 2


@dataclasses.dataclass(frozen=True)
class UnitFacts:
    """The per-invocation facts a job used to read from its own .ini.

    Inventoried from the launcher's job_config writer
    (awsBatchSubmitJobs_launchSingleSciencePipeline.py, the sections it
    fills from database queries rather than from the master .ini). These
    are the FIELDS, not the mechanism: what the job needs to know about
    its own unit, however it arrives.

    Every one of them is per-invocation by the placement criterion — they
    identify *this* SCA's inputs and are different for every child of an
    array. None of them is science tuning (that is release content) and
    none is operational configuration (that is the parameter tree).

    All are optional because job types need different subsets: a
    registration job has no science image and a reference-image job has
    no reference image yet. A job type that needs one and finds it absent
    fails at startup with a named missing fact, which is a better failure
    than a KeyError deep in a stage.

    Attributes
    ----------
    rid : int, optional
        Row identifier of the L2 file this unit processes (L2Files.rid) —
        the launcher's ``RID``, and the anchor for every other lookup.
    fid : int, optional
        Filter identifier (L2FileMeta.fid).
    filter_name : str, optional
        Filter name (Filters.filter), resolved from `fid` at submit time
        so the job does not re-query for a string.
    field : int, optional
        Sky-tile identifier the science image falls in (L2Files.field).
    rtid : int, optional
        Roman tessellation identifier from the tessellation database.
        Equal to `field` by construction; carried explicitly because the
        two come from different stores and a disagreement is a real
        fault worth being able to see.
    expid : int, optional
        Exposure identifier (L2Files.expid).
    mjdobs : float, optional
        Observation MJD (L2Files.mjdobs).
    exptime : float, optional
        Exposure time (L2Files.exptime).
    infobits : int, optional
        Quality bits on the science image (L2Files.infobits).
    status : int, optional
        Row status (L2Files.status).
    science_image_uri : str, optional
        S3 location of the science image (L2Files.filename).
    psfid : int, optional
        Identifier of the best PSF for this SCA and filter (PSFs.psfid).
    psf_uri : str, optional
        S3 location of that PSF (PSFs.filename).
    reference_image_id : int, optional
        Identifier of the best reference image (RefImages.rfid), or None
        when none exists and one must be built.
    reference_image_uri : str, optional
        S3 location of that reference image (RefImages.filename).
    reference_image_infobits : int, optional
        Quality bits on the reference image (RefImages.infobits).
    reference_image_ppid : int, optional
        Which pipeline produced the reference image — the dedicated
        reference-image pipeline or the science pipeline. Not derivable
        from the job's own type, which is why it is carried.
    images_to_coadd : int, optional
        How many overlapping images the reference-image build will
        coadd, counted at submit time. -1 where an existing reference
        image is being reused.
    coadd_inputs_uri : str, optional
        S3 location of the CSV listing those inputs.
    sky_position : dict, optional
        The science image's own centre and corners:
        ``{"ra0":..., "dec0":..., "ra1".."ra4":..., "dec1".."dec4":...}``
        (L2FileMeta). A nested mapping rather than eighteen flat keys,
        because they are only ever read together.
    tile_position : dict, optional
        The sky tile's centre and corners, same shape, from the
        tessellation database.
    reference_position : dict, optional
        The reference image's centre and corners, same shape, computed at
        submit time by tangent-plane projection.
    overlapping_fields : list, optional
        Tessellation identifiers the science image overlaps.
    reference_overlapping_fields : list, optional
        Tessellation identifiers the reference image overlaps.
    reference_image_version : int, optional
        Version of the reference image (RefImages.version), stamped into the
        reference image's RFIMVER header by post-process.
    pid : int, optional
        Identifier of the difference image this unit closes out
        (DiffImages.pid). Post-process only.
    difference_image_uri : str, optional
        S3 URI of the difference image (DiffImages.filename). Post-process
        only.
    difference_image_version : int, optional
        Version of the difference image (DiffImages.version), stamped into its
        DIFIMVER header.

    The last four are the post-process job type's own facts. They were absent
    from this vocabulary while `pipeline/stages/post_process.py` required
    `pid` and `difference_image_uri` through `context.fact` — so the facts the
    stage demanded had no home to be carried in, and post-process gathering
    could not have supplied them even had it tried.
    """

    rid: int | None = None
    fid: int | None = None
    filter_name: str | None = None
    field: int | None = None
    rtid: int | None = None
    expid: int | None = None
    mjdobs: float | None = None
    exptime: float | None = None
    infobits: int | None = None
    status: int | None = None
    science_image_uri: str | None = None
    psfid: int | None = None
    psf_uri: str | None = None
    reference_image_id: int | None = None
    reference_image_uri: str | None = None
    reference_image_infobits: int | None = None
    reference_image_ppid: int | None = None
    reference_image_version: int | None = None
    pid: int | None = None
    difference_image_uri: str | None = None
    difference_image_version: int | None = None
    images_to_coadd: int | None = None
    coadd_inputs_uri: str | None = None
    sky_position: dict[str, float] | None = None
    tile_position: dict[str, float] | None = None
    reference_position: dict[str, float] | None = None
    overlapping_fields: list[int] | None = None
    reference_overlapping_fields: list[int] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, absent facts omitted.

        Omitted rather than written as null, per the adopted
        absent-not-sentinel rule: a fact that was never resolved and a
        fact resolved to nothing are different, and only the first is
        what an omitted key means here.
        """
        return {name: value
                for name, value in dataclasses.asdict(self).items()
                if value is not None}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UnitFacts":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(raw) - known
        if unknown:
            # A fact this schema version does not know is not silently
            # dropped: it means the manifest was written by a newer
            # submitter, and guessing at the rest would be worse than
            # refusing.
            raise ValueError(
                "unit facts carry unknown keys "
                + ", ".join(sorted(unknown))
                + "; the manifest was written against a different schema")
        return cls(**raw)

    def require(self, *names: str) -> None:
        """Assert that named facts are present.

        What a job type's startup calls to turn "this job needs a science
        image" into one named failure instead of an AttributeError three
        stages later.

        Raises
        ------
        ValueError
            Naming every absent fact at once, not just the first.
        """
        missing = [name for name in names if getattr(self, name, None) is None]
        if missing:
            raise ValueError(
                "the manifest does not carry required per-invocation facts: "
                + ", ".join(missing))


@dataclasses.dataclass(frozen=True)
class ProcessingUnit:
    """One per-SCA unit of work — what a single array child processes.

    Frozen because a unit's identity is fixed the moment it enters a
    manifest: the retry contract binds an array index to an SCA identity
    for the life of the batch, and a mutable record would let that
    binding be edited out from under a retried child.

    Attributes
    ----------
    exposure : int
        Roman exposure identifier.
    sca : int
        Sensor chip assembly number within the exposure.
    facts : UnitFacts
        The per-invocation facts for this unit — what the launcher used
        to write into a per-job .ini. See `UnitFacts`.
    fields : dict
        Any additional per-unit values a job type needs beyond the named
        facts. Kept open deliberately: `UnitFacts` names what recurs, and
        this stays for what does not, so a new job type does not need a
        schema change to carry one extra value.
    """

    exposure: int
    sca: int
    facts: UnitFacts = dataclasses.field(default_factory=UnitFacts)
    fields: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable string identity for this unit, for dedup and logging.

        NOT a database identity: it names a processing unit, and the same
        exposure/SCA is processed by every run that reprocesses it. Use
        `logical_job_key` for anything that has to be unique across runs.
        """
        return f"{self.exposure}/{self.sca}"

    def logical_job_key(self, run_id: Any) -> str:
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
        """
        return f"{run_id}:{self.key}"

    def to_dict(self) -> dict[str, Any]:
        facts = self.facts.to_dict()
        return {"exposure": self.exposure, "sca": self.sca,
                **({"facts": facts} if facts else {}),
                **({"fields": self.fields} if self.fields else {})}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProcessingUnit":
        return cls(exposure=int(raw["exposure"]), sca=int(raw["sca"]),
                   facts=UnitFacts.from_dict(raw.get("facts", {})),
                   fields=raw.get("fields", {}))


class Manifest:
    """An ordered index -> ProcessingUnit binding for one submission.

    Construction fixes the order; the index of a unit is its position.
    Since W4 it also names the job type, which fixes the route: the
    workload class that may run it, the queue it belongs on, and the
    database lane its transactions take.
    """

    # 2 (W4): units gained `facts`, the manifest gained `job_type`.
    # Version 1 manifests are refused rather than read on a guess — a
    # version-1 manifest names no job type, and a job type is not
    # something to default.
    SCHEMA_VERSION = 2

    def __init__(self, units: Iterable[ProcessingUnit],
                 batch_id: str | None = None,
                 job_type: str = JOB_TYPE_SCIENCE):
        self.units: tuple[ProcessingUnit, ...] = tuple(units)
        self.batch_id = batch_id
        # Validated at construction, so an invalid job type cannot reach
        # S3 in a written manifest — the entrypoint's startup check is
        # the second line, not the first.
        self.route = route_for(job_type)
        self.job_type = job_type
        if not self.units:
            raise ValueError("a manifest needs at least one processing unit")
        if len(self.units) > MAX_ARRAY_SIZE:
            raise ValueError(
                f"{len(self.units)} units exceeds Batch's {MAX_ARRAY_SIZE}-child "
                "array ceiling; the batcher must cut smaller batches")
        duplicates = self._duplicate_keys()
        if duplicates:
            # Two children processing the same SCA would write the same
            # products from two attempts with different identities.
            raise ValueError(
                "duplicate processing units in one manifest: "
                + ", ".join(sorted(duplicates)))

    def _duplicate_keys(self) -> set[str]:
        seen: set[str] = set()
        dupes: set[str] = set()
        for unit in self.units:
            if unit.key in seen:
                dupes.add(unit.key)
            seen.add(unit.key)
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
                and self.job_type == other.job_type)

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
                unit.facts.require(*names)
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
        return {
            "schema_version": self.SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "job_type": self.job_type,
            "array_size": self.array_size,
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
        manifest = cls((ProcessingUnit.from_dict(u) for u in raw["units"]),
                       batch_id=raw.get("batch_id"),
                       job_type=job_type)
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
