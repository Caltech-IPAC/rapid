"""
File:    transformer.py

The schedule-to-generation transformer: the mission-mock harness's ONE new
component (design/operations.md § Mission mock and test campaigns,
integration review ruling 13).

    "Mock inputs enter through the same validation/ingest writer as real
    inputs: the harness's one new component transforms mission-schedule
    rows into staged generations -- exposure to per-SCA objects, wall time
    to MJD, manifest written last -- under simulated-substrate identity
    fixed at creation. Nothing else writes input rows."

**READ THIS FIRST: there is no existing staging reader to mirror.** The
task brief that produced this module instructed "find the staging reader
the science gatherer uses and produce exactly what it consumes". That
premise is false, verified against this repo rather than assumed:
`submission.gathering.gather_science_units` reads exclusively from
database rows (`l2filemeta`/`l2files`, through `rapid_db.RAPIDDB` query
methods like `get_l2files_records_for_datetime_range_field_fid`) — there
is no S3 object listing anywhere in the science-gathering path.
`pipeline.operator.inputs`'s own docstring phrase "the staged inputs
occupy 2027-10-01 to 2027-10-07" names a DATETIME WINDOW those database
rows fall inside, not an S3 prefix. This module therefore does not
reproduce a nonexistent reader; it defines and documents its own v1
staging layout below, and this deviation is reported prominently rather
than papered over.

**WHAT THIS MODULE ACTUALLY DOES.** `stage_generation` takes a sequence of
`ScheduleRow` (this module's own minimal mission-schedule row schema,
defined below because no existing reader's row shape could be reused —
`soc/apt/parse_schedule.py` is the SOC-side APT toolchain, format-
unrelated to anything the pipeline/submission layer consumes) and, per
exposure, fans it out to one `StagedSCA` reference-pointer object PER SCA
(1-18, or the row's declared subset), written through the injectable
`ObjectStore` boundary — never a byte copy of real pixel data (design: v1
"does not move real pixel data"; a `StagedSCA` cites an EXISTING g0001
simulation object by reference). A `GenerationManifest` is written LAST,
once every per-SCA object for the generation has landed — existence of
the manifest key IS the completion signal, exactly as the design states
("manifest written last").

**KEY GRAMMAR (v1, this module's own — no existing grammar to inherit)**::

    mock/<campaign_name>/<generation_id>/sca/<exposure>/<sca>.json
    mock/<campaign_name>/<generation_id>/manifest.json

`generation_id` is one per schedule ROW (one exposure's worth of SCA
fan-out) — the design's own vocabulary: "transforms mission-schedule rows
into staged generations". A campaign stages many generations, one per
scheduled exposure it replays.

**SUBSTRATE IDENTITY.** Every `StagedSCA` and the `GenerationManifest`
carry `"substrate": "simulated"` (design/operations.md § Continuous
validation's real|simulated, pristine|injected vocabulary — this v1
stamps substrate only; injection state is out of scope for the mock
harness, which replays UNINJECTED mission-schedule rows, so every staged
object is `simulated ∧ pristine` — still validation data, per the
science-gate rule that only real∧pristine is science, but not an
injection-testing object either).

**NAMING, TO AVOID A COLLISION** (task brief's own caution). This
module's `GenerationManifest` is the TRANSFORMER'S staging-completion
manifest — per generation, written to the key grammar above. It is a
DIFFERENT object from `submission.manifest.Manifest`, the array-submission
manifest `pipeline.seams.submit_units` publishes at SUBMIT time (one per
Batch array job, keyed under the submission layer's own prefix). The two
share no schema, no key prefix, and no writer. `GenerationManifest` is
named distinctly (not `StagingManifest`, to avoid even the visual
adjacency of "...Manifest" beside `submission.manifest.Manifest` in an
import list) and this paragraph exists so the distinction is never
assumed rather than stated.
"""

import dataclasses
import datetime
import json
import logging
from typing import Any, Protocol, Sequence

from pipeline.intent.writer import (
    READY,
    WRITER_VALIDATION_INGEST,
    CampaignWriter,
    WorkUnitIdentity,
    WorkUnitWriter,
)
from pipeline.runtime.boundaries import ObjectStore
from submission import payloads
from submission.manifest import ProcessingUnit
from submission.routes import JOB_TYPE_SCIENCE

logger = logging.getLogger("rapid.mock.transformer")

#: The data-class pair every staged object and the generation manifest
#: carry, fixed at creation (design § Continuous validation). The mock
#: harness replays uninjected mission-schedule rows, so every object it
#: creates is simulated ∧ pristine.
SUBSTRATE_SIMULATED = "simulated"
INJECTION_PRISTINE = "pristine"

#: Every SCA in a Roman exposure, 1-indexed, when a `ScheduleRow` does not
#: narrow its fan-out — "full SCA fan-out" is the design's stated v1
#: posture (§ Mission mock and test campaigns).
ALL_SCAS: tuple[int, ...] = tuple(range(1, 19))


@dataclasses.dataclass(frozen=True)
class ScheduleRow:
    """This module's own minimal mission-schedule row schema (v1 judgment
    call — no existing reader's row shape could be reused; see the module
    docstring's finding).

    Attributes
    ----------
    exposure_id : int
        Roman exposure identifier, the schedule's own unit of observation.
    wall_time : datetime.datetime
        The scheduled (or replayed) arrival timestamp — real arrival
        timestamps from the mission schedule, per the design's v1
        fidelity posture. Converted to MJD by `stage_generation`, never
        carried as MJD by the row itself: the row is what a mission
        schedule actually states, and a schedule states wall-clock time.
    field : int
        Roman tessellation field id — carried through to `StagedSCA` as
        the pointer's own field fact, and used to derive `input_scope` for
        the work unit `create_mock_campaign` creates over this generation.
    source_ref : str
        A reference to the EXISTING g0001 simulation object this row
        replays — v1 does not move real pixel data (design), so every
        `StagedSCA` this row fans out to points at the SAME source, one
        per SCA, rather than eighteen distinct source objects. An opaque
        string (an S3 URI or key) rather than a structured type: this
        module never reads or parses it, only carries it through.
    scas : tuple of int, optional
        The SCA fan-out. None (the default) means full fan-out
        (`ALL_SCAS`) — the design's v1 posture. A narrower tuple is
        accepted for a caller that wants a scale dial below full fan-out
        (design § Mission mock and test campaigns: "Fidelity is four
        independent dials -- scale, arrival cadence, sky distribution,
        data realism").
    """

    exposure_id: int
    wall_time: datetime.datetime
    field: int
    source_ref: str
    scas: tuple[int, ...] | None = None

    def sca_fanout(self) -> tuple[int, ...]:
        return self.scas if self.scas is not None else ALL_SCAS


@dataclasses.dataclass(frozen=True)
class StagedSCA:
    """One per-SCA staged object: a reference pointer, not a byte copy.

    `to_bytes` matches `pipeline.reconciler.closure.ClosureRecord`'s own
    JSON convention (`sort_keys=True`, compact separators, `ensure_ascii=
    False`) — the repo's one existing answer to "how do we serialize a
    staged JSON object deterministically" rather than a second policy
    invented here.
    """

    exposure_id: int
    sca: int
    field: int
    mjd: float
    source_ref: str
    substrate: str = SUBSTRATE_SIMULATED
    injection: str = INJECTION_PRISTINE

    @property
    def key_suffix(self) -> str:
        """The `sca/<exposure>/<sca>.json` portion of the key grammar."""
        return f"sca/{self.exposure_id}/{self.sca}.json"

    def to_bytes(self) -> bytes:
        body = {
            "exposure_id": self.exposure_id, "sca": self.sca,
            "field": self.field, "mjd": self.mjd,
            "source_ref": self.source_ref, "substrate": self.substrate,
            "injection": self.injection,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class GenerationManifest:
    """The staging-completion manifest for ONE generation (one schedule row).

    Existence of this object at its key IS the completion signal (design:
    "manifest written last") — `stage_generation` writes every `StagedSCA`
    object first and this object only once all of them have landed
    successfully. See the module docstring's naming section for why this
    is NOT `submission.manifest.Manifest`.
    """

    campaign_name: str
    generation_id: str
    exposure_id: int
    field: int
    mjd: float
    sca_keys: tuple[str, ...]
    substrate: str = SUBSTRATE_SIMULATED
    injection: str = INJECTION_PRISTINE

    def to_bytes(self) -> bytes:
        body = {
            "campaign_name": self.campaign_name,
            "generation_id": self.generation_id,
            "exposure_id": self.exposure_id, "field": self.field,
            "mjd": self.mjd, "sca_keys": list(self.sca_keys),
            "substrate": self.substrate, "injection": self.injection,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")


def generation_id_for(row: ScheduleRow) -> str:
    """The generation id for one schedule row: `<exposure_id>`.

    One generation per exposure (module docstring: "one per schedule ROW"),
    and the exposure id is already this repo's own stable per-exposure
    identifier (`ProcessingUnit.exposure`) — reusing it here rather than
    minting a second one keeps the mock harness's generation identity
    legible against the same exposure id the rest of the pipeline uses.
    """
    return str(row.exposure_id)


def wall_time_to_mjd(moment: datetime.datetime) -> float:
    """Wall-clock timestamp -> MJD, the one conversion this module needs.

    Mirrors `pipeline.operator.gathering.mjd_window`'s own isot-format
    conversion exactly (grepped for first — this repo's one existing
    wall-clock-to-MJD helper converts a WINDOW, `(start, end)`; this
    function is the single-timestamp form the transformer needs, sharing
    the same astropy call shape and the same float-not-numpy-scalar
    return type for the same reason `mjd_window` states: whatever
    downstream code eventually consumes this value should never receive a
    `numpy.float64` a database adapter cannot bind).
    """
    from astropy.time import Time

    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return float(Time(moment.isoformat(), format="isot", scale="utc").mjd)


def stage_generation(store: ObjectStore, campaign_name: str, row: ScheduleRow
                     ) -> GenerationManifest:
    """Stage one generation: per-SCA objects, then the completion manifest.

    Writes `len(row.sca_fanout())` `StagedSCA` objects through
    `store.put_if_absent` (idempotent by identity, matching this repo's
    house convention for every S3 write in the termination protocol —
    `pipeline.runtime.boundaries.ObjectStore`'s own contract), THEN the
    `GenerationManifest` — ordering is the point, asserted by this
    module's own tests via a store double that records write order, and
    is what makes "manifest present" mean "generation complete" for any
    reader.

    Returns the written `GenerationManifest`. Raises whatever `store`
    raises on a failed write — a partial generation (some SCAs staged, no
    manifest) is a valid, recoverable state: a retry of this same call is
    idempotent (every key is identity-keyed by exposure/SCA or by
    generation, and `put_if_absent` validates rather than duplicates on
    replay), and the ABSENCE of the manifest is itself the correct
    "incomplete" signal a caller reads.
    """
    mjd = wall_time_to_mjd(row.wall_time)
    prefix = f"mock/{campaign_name}/{generation_id_for(row)}"

    sca_keys = []
    for sca in row.sca_fanout():
        staged = StagedSCA(exposure_id=row.exposure_id, sca=sca,
                           field=row.field, mjd=mjd,
                           source_ref=row.source_ref)
        key = f"{prefix}/{staged.key_suffix}"
        store.put_if_absent(key, staged.to_bytes(),
                            content_type="application/json")
        sca_keys.append(key)

    manifest = GenerationManifest(
        campaign_name=campaign_name, generation_id=generation_id_for(row),
        exposure_id=row.exposure_id, field=row.field, mjd=mjd,
        sca_keys=tuple(sca_keys))
    manifest_key = f"{prefix}/manifest.json"
    store.put_if_absent(manifest_key, manifest.to_bytes(),
                        content_type="application/json")

    logger.info("staged generation %s for campaign %s: %d SCA object(s), "
               "manifest %s", manifest.generation_id, campaign_name,
               len(sca_keys), manifest_key)
    return manifest


def create_mock_campaign(execute: Any, campaign_writer: CampaignWriter,
                         work_writer: WorkUnitWriter, store: ObjectStore,
                         campaign_name: str, job_type: str,
                         schedule: Sequence[ScheduleRow],
                         definition_version: int = 1,
                         definition: dict | None = None,
                         now: datetime.datetime | None = None) -> int:
    """Create a test-class campaign, stage its schedule, create its work units.

    The campaign-integration function part 5 of the build spec asks for
    (a plain function, not a CLI — "a plain function is sufficient and
    testable, prioritize that", task brief). Given a campaign definition
    (name, route/job_type, schedule slice — the scale dial is expressed by
    the CALLER choosing how many `ScheduleRow`s to pass, and by each row's
    own `scas` fan-out subset, rather than a parameter this function
    interprets), this:

    1. Creates the campaign row via `CampaignWriter.create_campaign`
       (state='defined'), under the 'test' operational class.
    2. For each schedule row: stages its generation (`stage_generation`),
       then creates ONE work unit per generation — state='blocked' until
       the manifest exists, 'ready' once it does. Per the build spec:
       "state 'blocked' until staging completes / 'ready' once the
       manifest is written — i.e., the transformer's own run is what
       flips blocked->ready per unit once its per-SCA objects + manifest
       exist." This function stages BEFORE creating the row, so in
       practice every unit this function creates is born directly in
       'ready' (staging already succeeded by the time the row is
       written) — the 'blocked' state is what a unit would show if
       staging had failed AFTER a caller pre-created the row and BEFORE
       staging ran, which is not this function's own sequencing but is
       preserved as the documented state a partial run leaves visible
       (see the docstring note below on why blocked is not unreachable
       from this function despite never being written by it directly).
    3. Each created work unit carries `campaign_id` (migration 036's
       plain FK) and job_type=`job_type` — the campaign's declared route
       (design: "a test campaign declares its queue at campaign
       definition — bulk by default").

    `input_scope` for each work unit is `<exposure_id>/<field>` — the
    generation's own natural scope, distinct from `_input_scope_for`'s
    typed-subject derivation in `pipeline.seams` (which serves REAL
    science units keyed by the typed-identity registry) because a mock
    generation is not a `ProcessingUnit` and carries no `subject_for`
    entry; this is a second, deliberately simple v1 scope string for
    campaign-scoped test work, stated here as its own choice.

    Returns the created campaign's `campaign_id`.

    **RAISES, DOES NOT CATCH, THE DEFINITION-FK GAP** (unlike
    `pipeline.seams._attach_work_unit`, which SWALLOWS a
    `work_units_definition_fk` violation because the arrival-driven
    submission path must keep working today regardless of the intent
    layer's readiness). This function has no such backward-compatibility
    obligation — it is new, called only by a caller that has already
    decided to run a mock campaign — so it propagates the FK violation
    plainly: as of this writer, EVERY call to this function fails at work-
    unit creation, because no workflow_definitions row is loaded for any
    job type (see this package's `__init__.py` and the module's own
    report section on the V-phase gap). That failure is left visible
    rather than caught, because catching it here would hide the exact
    fact the V-phase validation plan needs to see.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    campaign_id = campaign_writer.create_campaign(
        campaign_name, "test", definition=definition, now=moment)

    for row in schedule:
        manifest = stage_generation(store, campaign_name, row)
        input_scope = f"{row.exposure_id}/{row.field}"
        identity = WorkUnitIdentity(
            job_type=job_type, input_scope=input_scope,
            operational_class="test",
            definition_version=definition_version)
        work_writer.create_work_unit(
            identity, writer=WRITER_VALIDATION_INGEST, state=READY,
            campaign_id=campaign_id,
            detail={"generation_id": manifest.generation_id,
                   "manifest_key": (
                       f"mock/{campaign_name}/{manifest.generation_id}"
                       f"/manifest.json")},
            now=moment)

    logger.info("created mock campaign %s (id=%s) with %d generation(s)",
               campaign_name, campaign_id, len(schedule))
    return campaign_id


# ---------------------------------------------------------------------------
# create_mock_campaign_from_staged — the W2-FALLBACK entry point
# (integration review, IR-13-a).
# ---------------------------------------------------------------------------
#
# `create_mock_campaign` above is the schedule-to-generation path this
# module's docstring describes: mission-schedule rows staged as reference
# pointers under simulated∧pristine substrate identity, then one work unit
# per generation. That path still needs `workflow_definitions` loaded (the
# `RAISES, DOES NOT CATCH` note on `create_mock_campaign` documents exactly
# this), and separately needs a `ScheduleRow` sequence somebody has to
# construct.
#
# The W2 fallback is a SECOND, simpler substrate for exactly the same
# work-unit shape: rather than staging NEW reference-pointer objects,
# campaign work units are created directly FROM ALREADY-REGISTERED
# simulation input rows — the L2Files/L2FileMeta rows a prior science-
# pipeline run (or a bulk simulation load) has already written. Nothing is
# staged and no `GenerationManifest` is written: the campaign's work units
# point at real, already-registered rows from the first creation, so the
# units this function yields are exactly the same shape
# `submission.gathering.gather_science_units` would build from those rows
# directly — "the yielded units are exactly science-shaped and the whole
# downstream chain ... runs unmodified" (the supervisor ruling, quoted in
# `submission.gathering.gather_campaign_units`'s own header).
#
# **THE V1 ROUTE RESTRICTION, ASSERTED HERE (creation time — the other half
# of "assert at campaign creation and at gathering").** There is no
# `job_type` PARAMETER on this function at all — every work unit it creates
# is hardcoded to `submission.routes.JOB_TYPE_SCIENCE`, which is a stronger
# guarantee than accept-and-validate: a caller cannot even ASK this
# function for a non-science test campaign. `gather_campaign_units`'s own
# gather-time re-assertion is what guards the OTHER path into a campaign
# work unit (a hand-built `WorkUnitWriter.create_work_unit` call bypassing
# this function entirely) — the two guards are independent by design, not
# redundant, per the ruling's own parenthetical.
class StagedInputSource(Protocol):
    """The two `rapid_db.RAPIDDB` methods this function needs to enumerate
    already-registered simulation rows and read each one's identity.

    Deliberately a NARROWER protocol than `submission.gathering.
    UnitSource` — this function does not gather a manifest's worth of
    science facts (no reference lookup, no PSF, no filter name — those are
    `science_facts`' job at GATHER time, not this function's job at
    CREATE time), it only needs enough of each row's identity to build a
    work unit and remember which L2 row backs it.
    """

    exit_code: int

    def get_l2files_records_for_datetime_range(
            self, start: Any, end: Any) -> Sequence[Any]: ...

    def get_info_for_l2file(self, rid: int) -> Sequence[Any]: ...


def create_mock_campaign_from_staged(
        execute: Any, campaign_writer: CampaignWriter,
        work_writer: WorkUnitWriter, handle: StagedInputSource,
        campaign_name: str, *, start: datetime.datetime,
        end: datetime.datetime, max_units: int,
        scas: tuple[int, ...] | None = None,
        exposure_ids: tuple[int, ...] | None = None,
        definition_version: int = 1, definition: dict | None = None,
        now: datetime.datetime | None = None) -> int:
    """Create a test-class campaign directly from already-registered L2 rows.

    The W2-fallback substrate (module section header above): enumerates
    `handle.get_l2files_records_for_datetime_range(start, end)` — the same
    query this repo already has for "every registered L2 row in a window",
    used unmodified rather than adding a parallel query — optionally
    narrowed to `scas`/`exposure_ids`, and creates one READY work unit per
    surviving row, each carrying that row's `(rid, field, fid)` in its
    creation-event detail under the SAME keys
    (`source_rid`/`source_field`/`source_fid`)
    `database.modules.utils.rapid_db.RAPIDDB.
    get_campaign_unit_source_l2_identity` reads back — the campaign
    gatherer's other half of this round trip.

    `input_scope` is built via `submission.subjects
    .input_scope_from_subject` — THE SHARED GRAMMAR, not a second
    convention (`build_input_scope` is the same grammar's unit-taking entry
    point, and delegates to it). Using the exact function
    `pipeline.seams._input_scope_for` delegates to is what guarantees a
    unit created here and the SAME unit later found by `pipeline.seams.
    _attach_work_unit` (when the accumulator submits it) agree on identity
    without either side re-deriving the other's string shape.

    **Unit-count safety (the V-phase budget guard).** `max_units` is
    REQUIRED, not optional with a large default: refuses outright (raises,
    creates nothing) if the enumerated row count exceeds it, before any
    campaign or work-unit row is written — a caller that wants a bigger
    campaign must say so explicitly, exactly as the operator service's own
    `--width`/`--max-width` pair refuses rather than clamps
    (`pipeline.operator.service._bounded`'s own doctrine, reused here for
    the same reason: a silent cap reads exactly like a complete run).

    Parameters
    ----------
    handle : StagedInputSource
        The narrow query surface (see that Protocol) — pass a
        `database.modules.utils.checked.CheckedHandle`-wrapped
        `RAPIDDB.borrowing(conn)` in production, a stub in tests.
    start, end : datetime.datetime
        The registration window to slice: "exposure ids or mjd window +
        sca subset" (task brief) — this is the mjd-window half, expressed
        as the same `dateobs` window `get_l2files_records_for_datetime_
        range` already takes.
    max_units : int
        The budget guard. No default: a caller must state it.
    scas : tuple of int, optional
        Narrows the enumerated rows to these SCAs. None means every SCA
        the window returns.
    exposure_ids : tuple of int, optional
        Narrows the enumerated rows to these exposures. None means every
        exposure the window returns — the "exposure ids" half of the
        slice spec, applied as a post-filter on the same window query
        rather than a second query, since `get_l2files_records_for_
        datetime_range` has no exposure-id parameter to push it into.

    Returns
    -------
    int
        The created campaign's `campaign_id`.

    Raises
    ------
    ValueError
        The enumerated unit count exceeds `max_units` (the budget guard).
        There is no `job_type` argument to validate — see this section's
        header for why the v1 route restriction is a hardcoded fact here,
        not a checked one.
    RuntimeError
        An enumerated `rid` (from `get_l2files_records_for_datetime_range`)
        has no matching row in `get_info_for_l2file`, or that row is
        missing `expid`/`field` — either means the two queries disagree
        about what "already-registered" means, which is a real defect in
        the database, not an unready state.
    """
    from submission.routes import JOB_TYPE_SCIENCE
    from submission.subjects import input_scope_from_subject

    moment = now or datetime.datetime.now(datetime.timezone.utc)

    rows = handle.get_l2files_records_for_datetime_range(start, end)
    candidates: list[tuple[int, int, int]] = []  # (rid, sca, fid)
    for row in rows or ():
        rid, sca = int(row[0]), int(row[1])
        if scas is not None and sca not in scas:
            continue
        candidates.append((rid, sca, int(row[2])))

    # exposure_ids narrows AFTER the window query (module docstring): no
    # existing query takes both a window and an exposure-id list, and
    # every candidate's info lookup below is needed regardless to recover
    # `expid`/`field` — so the exposure filter is applied against that
    # same lookup rather than issuing a second, narrower query first.
    enumerated: list[tuple[int, int, int, int, int]] = []  # (rid, sca, fid, expid, field)
    for rid, sca, fid in candidates:
        info = handle.get_info_for_l2file(rid)
        if info is None or len(info) < 4:
            raise RuntimeError(
                f"rid {rid} was enumerated by get_l2files_records_for_"
                f"datetime_range but get_info_for_l2file returned no row; "
                f"an already-registered row must be resolvable by both "
                f"queries")
        expid, field = _maybe_int(info[1]), _maybe_int(info[3])
        if expid is None or field is None:
            raise RuntimeError(
                f"rid {rid} has no expid/field in L2Files; a campaign work "
                f"unit cannot be built without both")
        if exposure_ids is not None and expid not in exposure_ids:
            continue
        enumerated.append((rid, sca, fid, expid, field))

    # THE BUDGET GUARD — refused, not clamped, and before any write.
    if len(enumerated) > max_units:
        raise ValueError(
            f"campaign {campaign_name!r} would enumerate {len(enumerated)} "
            f"work unit(s) from already-registered rows, exceeding the "
            f"stated max_units={max_units}; refusing to create anything. "
            f"Narrow the window, scas, or exposure_ids, or raise "
            f"max_units explicitly.")

    campaign_id = campaign_writer.create_campaign(
        campaign_name, "test", definition=definition, now=moment)

    for rid, sca, fid, expid, field in enumerated:
        # THE SUBJECT, NOT A UNIT. This built a throwaway `ProcessingUnit`
        # purely to hand `build_input_scope` something to read a subject
        # off. Since D4 a science payload validates its resolved facts at
        # construction, and this loop has none of them — it enumerates
        # already-registered L2 rows to CREATE work units, long before any
        # gathering pass resolves facts for them. Inventing eleven values to
        # satisfy the validator would defeat the validator; asking for the
        # scope of a subject this loop genuinely has is the honest call.
        input_scope = input_scope_from_subject(
            (JOB_TYPE_SCIENCE, int(expid), int(sca)))
        identity = WorkUnitIdentity(
            job_type=JOB_TYPE_SCIENCE, input_scope=input_scope,
            operational_class="test",
            definition_version=definition_version)
        work_writer.create_work_unit(
            identity, writer=WRITER_VALIDATION_INGEST, state=READY,
            campaign_id=campaign_id,
            # THE ROUND TRIP: source_rid/source_field/source_fid, read back
            # by database.modules.utils.rapid_db.RAPIDDB.
            # get_campaign_unit_source_l2_identity via
            # submission.gathering._campaign_unit_l2_identity — see both
            # docstrings for why this is a detail-keyed carry rather than a
            # reverse exposure/SCA -> rid query.
            detail={"source_rid": rid, "source_field": field,
                   "source_fid": fid},
            now=moment)

    logger.info(
        "created mock campaign %s (id=%s) from %d already-registered L2 "
        "row(s) [%s..%s]", campaign_name, campaign_id, len(enumerated),
        start.isoformat(), end.isoformat())
    return campaign_id


def _maybe_int(value: Any) -> int | None:
    """Same absent-not-sentinel helper `submission.gathering` defines.

    Duplicated rather than imported: the two modules are siblings under
    one convention (this repo's house rule for absent-vs-sentinel), not
    one depending on the other's internals, matching `pipeline.intent.
    writer`'s own stated reason for duplicating `_rowcount` rather than
    importing it across module boundaries.
    """
    return None if value is None else int(value)
