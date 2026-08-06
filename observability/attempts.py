"""
File:    attempts.py

Writing attempt records: the rows migration 011 defines.

design/observability.md § Attempt record makes the attempt record "the
authoritative, queryable account of every processing attempt". This module is
the write side of that — the only place in the pipeline that inserts or
advances an `attempts` row.

Three things shape the code more than anything else:

**A retry is a new row.** Nothing here ever updates a prior attempt's identity.
`create_submitted` is the only way a row is born, and the lifecycle transitions
only ever move one row forward through its own states.

**Array children are rows at submission time.** They are created before Batch
assigns child identifiers, then backfilled by `backfill_scheduler_job_ids`. That
ordering is the point: a child whose identifier never resolves is left as a
detectable reconciliation case rather than never existing at all. Creating rows
after the scheduler answered would make an unresolved child a silent gap, which
is the failure mode the design names.

**Absent means NULL.** Every writer here omits fields the lifecycle state has
not reached rather than filling a sentinel. The DDL's derived CHECK set enforces
that, so a transition that tried to write a field too early fails loudly at the
database rather than producing a plausible-looking row.

The database boundary is a single injected `execute(sql, params)` callable. That
is what lets the whole module be tested without a database — and the tests do
exactly that, per the workstream's rule that tests never point at the live
database.
"""

import dataclasses
import enum
import logging
from typing import Any, Callable, Iterable, Protocol, Sequence

logger = logging.getLogger(__name__)

# The schema version these writers produce. Bumped with the migration that
# changes the record shape; producers and consumers declare what they support
# (design/observability.md: "the record schema is versioned").
#
# Version 2 is migration 013's amended shape: the application-observed attempt
# index, the atomic claim-or-create resolver, the logical-job execution
# binding, the application-closed state with the intended/observed exit split,
# and the error-category allowlist. Migration 013's constraints gate their new
# requirements on schema_version >= 2, so a writer that declares 2 is a writer
# that must supply them.
SCHEMA_VERSION = 2


class LifecycleState(str, enum.Enum):
    """The six lifecycle states. Values match the DDL's CHECK vocabulary."""

    SUBMITTED = "submitted"
    STARTED = "started"
    # Amended in (D:batch-payload-co-design): the application has written its
    # outcome, product disposition, intended exit and terminal-record
    # reference, but the scheduler-observed facts are not yet known. The
    # reconciler's arrival with those facts is what advances the row to
    # TERMINAL_AFTER_START.
    APPLICATION_CLOSED = "application_closed"
    TERMINAL_AFTER_START = "terminal_after_start"
    TERMINAL_WITHOUT_START = "terminal_without_start"
    MISSING_OR_CONTRADICTORY = "missing_or_contradictory"


# The v1 error-category allowlist (13 categories), mirroring migration 013's
# attempt_error_categories table. Held here so a producer can validate before
# the round trip; the database remains the authority — this is a copy for
# early failure, never a second source of truth.
APPLICATION_ERROR_CATEGORIES = frozenset({
    "tool_failure", "input_missing", "input_invalid", "config_invalid",
    "reference_missing", "db_unavailable", "db_error", "storage_error",
    "records_error", "resource_exhausted", "internal_error",
})
RECONCILER_ERROR_CATEGORIES = frozenset({
    "scheduler_reclaimed", "scheduler_provisioning",
})
ERROR_CATEGORIES = APPLICATION_ERROR_CATEGORIES | RECONCILER_ERROR_CATEGORIES


class RapidOutcome(str, enum.Enum):
    """Application-level result — never the scheduler's view."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class ProductDisposition(str, enum.Enum):
    PUBLISHED = "published"
    WITHHELD = "withheld"
    SUPERSEDED = "superseded"
    NONE = "none"


class StageOutcome(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


class ReconciliationClass(str, enum.Enum):
    MISSING = "missing"
    CONTRADICTORY = "contradictory"


# Scheduler states, mirroring Batch's job states. Kept as a frozenset rather
# than an enum: these are the scheduler's vocabulary, not ours, and we only ever
# validate values we were handed.
SCHEDULER_STATES = frozenset({
    "SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING",
    "SUCCEEDED", "FAILED",
})


class Executor(Protocol):
    """The database boundary.

    One method, so a test can substitute a recorder and assert on the exact SQL
    and parameters. Implementations are expected to be parameterized-query
    based; nothing here ever interpolates a value into SQL text.
    """

    def __call__(self, sql: str, params: Sequence[Any]) -> Any:
        ...


@dataclasses.dataclass(frozen=True)
class AttemptIdentity:
    """The identity triple plus the processing-unit scope.

    Frozen: identity is fixed when the row is created. A retry constructs a new
    identity, it does not edit this one.
    """

    run_id: str
    logical_job_id: str
    exposure_id: int | None = None
    sca: int | None = None
    sky_tile: str | None = None


@dataclasses.dataclass(frozen=True)
class Provenance:
    """What a started attempt records about the code and config it ran.

    All four are required at `started` by the DDL — the design makes the attempt
    record the provenance query surface, so a started row without provenance is
    not a representable state.
    """

    source_sha: str
    container_digest: str
    job_definition_rev: str
    config_digest: str


@dataclasses.dataclass(frozen=True)
class ExecutionBinding:
    """The complete submission-time execution binding.

    Authored ONCE at logical-job scope by the submitter — it is provenance
    only the submitter knows, and it outlives every attempt of the job — then
    copied into each attempt row at creation, so a retry row and a
    reconciler-authored record always carry it (D:batch-payload-co-design).

    Deliberately distinct from `Provenance`, which is the runtime's own
    startup observation of what it is executing. The two are separate writers
    of separate facts: disagreement between `image_digest` here and
    `container_digest` there is a reconciliation signal, not a duplicate.

    **COMPLETE means complete (review finding #11).** `job_definition_rev`
    and `release_identity` were optional, so a submission lacking either was
    accepted, its retries preserved the incomplete binding, and reconciliation
    recorded agreement instead of drift — there was nothing to disagree with.
    The design calls this "the COMPLETE submission-time execution binding: the
    exact job-definition ARN and revision, its pinned image digest, the
    release identity, and the manifest checksum", and every one of those is a
    fact the submitter knows at submission time.

    They are validated here rather than left to the DDL so the failure names
    the missing field at the submitter, before any row exists, instead of
    arriving as a constraint violation on the first insert.
    """

    job_definition_arn: str
    image_digest: str
    manifest_checksum: str
    job_definition_rev: int | None = None
    release_identity: str | None = None

    def __post_init__(self) -> None:
        missing = [name for name in
                   ("job_definition_arn", "image_digest", "manifest_checksum",
                    "job_definition_rev", "release_identity")
                   if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(
                "the submission-time execution binding is incomplete; "
                "missing: " + ", ".join(missing)
                + ". Every field is a fact the submitter knows at submission "
                "time, and an incomplete binding is copied onto every attempt "
                "row and every retry — so reconciliation has nothing to "
                "cross-check against and records agreement where it cannot "
                "actually tell.")

    @property
    def definition_identity(self) -> str:
        """`<arn>:<revision>` — what the scheduler reports as `jobDefinition`.

        The reconciler compares its observation against this (#11). Batch
        reports the definition ARN with its revision suffix, and the ARN
        recorded at submission may or may not already carry one, so this
        normalizes to the compared form in one place.
        """
        arn = self.job_definition_arn
        base, _, suffix = arn.rpartition(":")
        if base and suffix.isdigit():
            return arn
        return f"{arn}:{self.job_definition_rev}"


@dataclasses.dataclass(frozen=True)
class Stage:
    """One completed stage execution — span-shaped, written once.

    `duration_ms` comes from a monotonic timer, never from subtracting wall
    clocks. `started_at` is for correlation and display only.
    """

    stage_name: str
    started_at: Any
    duration_ms: float
    outcome: StageOutcome

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError(
                f"stage {self.stage_name!r} has negative duration "
                f"{self.duration_ms}; a monotonic timer cannot run backwards")


class AttemptWriter:
    """Writes and advances attempt records.

    Parameters
    ----------
    execute : callable
        ``execute(sql, params)`` against the attempt-record tables. Expected to
        return rows for statements with a RETURNING clause.
    schema_version : int, optional
        The record schema version to stamp on new rows.
    """

    def __init__(self, execute: Executor, schema_version: int = SCHEMA_VERSION):
        self._execute = execute
        self.schema_version = schema_version

    # -- creation -----------------------------------------------------------

    def create_logical_job(self, logical_job_id: str, run_id: str,
                           binding: ExecutionBinding,
                           scheduler_job_id: str | None = None) -> None:
        """Record the logical job and its execution binding.

        Called ONCE per logical job by the submitter, before its attempt rows
        are created — `resolve_attempt` copies the binding from here, so a row
        created before this exists has nothing to copy and is flagged as a
        reconciliation case rather than fabricated.

        Idempotent by identity: re-submitting the same logical job does not
        overwrite a binding already recorded. The binding is written once and
        never updated, so a replayed submission cannot silently rewrite what a
        running attempt believes it is executing.

        **A conflict is checked, not ignored (review finding #3).** The
        `ON CONFLICT DO NOTHING` above is the right write — a replayed
        submission must not rewrite a live binding — but combined with a
        logical-job key that was not run-scoped it meant reprocessing an
        exposure/SCA under a second run silently kept the FIRST run's binding,
        and a scheduler retry copied that stale manifest, image, release and
        run identity forward. The key is run-scoped now
        (`ProcessingUnit.logical_job_key`), so a conflict can only mean a
        genuine replay of the same submission — and this verifies that is what
        it is, rather than trusting it.

        A conflict whose recorded binding DISAGREES with the one offered is a
        `LogicalJobConflict`: two different submissions have claimed one
        identity, and continuing would attach attempts to a binding that does
        not describe them.
        """
        sql = (
            "INSERT INTO logical_jobs ("
            "  logical_job_id, run_id, job_definition_arn, job_definition_rev,"
            "  image_digest, release_identity, manifest_checksum,"
            "  scheduler_job_id"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (logical_job_id) DO NOTHING"
            " RETURNING logical_job_id"
        )
        inserted = self._execute(sql, [
            logical_job_id, run_id, binding.job_definition_arn,
            binding.job_definition_rev, binding.image_digest,
            binding.release_identity, binding.manifest_checksum,
            scheduler_job_id,
        ])

        if _rowcount(inserted, "create_logical_job") == 0:
            self._verify_logical_job(logical_job_id, run_id, binding)
            return

        logger.info("recorded logical job %s (image %s)",
                    logical_job_id, binding.image_digest)

    def _verify_logical_job(self, logical_job_id: str, run_id: str,
                            binding: ExecutionBinding) -> None:
        """Confirm an existing logical job is the one we meant to write.

        Reached only when the insert conflicted. A replay of the same
        submission agrees on every binding field and is fine; anything else is
        two submissions contending for one identity, which the run-scoped key
        is supposed to make impossible — so if it happens, it is a defect
        somewhere upstream and must not be absorbed.
        """
        rows = self._execute(
            "SELECT run_id, job_definition_arn, image_digest,"
            "       manifest_checksum, release_identity"
            " FROM logical_jobs WHERE logical_job_id = %s",
            [logical_job_id])
        if not rows:
            raise LogicalJobConflict(
                f"logical job {logical_job_id!r} conflicted on insert but "
                f"cannot be read back; the row was created and removed "
                f"concurrently, or the executor does not return result sets")

        row = rows[0]
        existing = (row if isinstance(row, dict) else {
            name: value for name, value in zip(
                ("run_id", "job_definition_arn", "image_digest",
                 "manifest_checksum", "release_identity"), row)})

        offered = {
            "run_id": run_id,
            "job_definition_arn": binding.job_definition_arn,
            "image_digest": binding.image_digest,
            "manifest_checksum": binding.manifest_checksum,
            "release_identity": binding.release_identity,
        }
        disagreements = {
            field: (existing.get(field), value)
            for field, value in offered.items()
            if existing.get(field) != value
        }
        if disagreements:
            detail = "; ".join(
                f"{field}: recorded {recorded!r}, offered {new!r}"
                for field, (recorded, new) in sorted(disagreements.items()))
            raise LogicalJobConflict(
                f"logical job {logical_job_id!r} already exists with a "
                f"different execution binding ({detail}). Two submissions "
                f"have claimed one logical-job identity; attempts created "
                f"under this id would carry a binding that does not describe "
                f"them.")

        logger.info("logical job %s already recorded with an identical "
                    "binding; treating as a replayed submission",
                    logical_job_id)

    def resolve_attempt(self, identity: AttemptIdentity, created_at: Any,
                        submitted_at: Any,
                        scheduler_job_id: str | None = None,
                        application_attempt_index: int | None = None,
                        scheduler_attempt_index: int | None = None) -> int:
        """Claim-or-create this attempt's row atomically; return its attempt_id.

        The ONLY sanctioned way for the runtime or the reconciler to acquire an
        attempt row (D:batch-payload-co-design). Neither ever bare-INSERTs:
        acquisition goes through migration 013's `resolve_attempt` database
        function, where a transaction-scoped advisory lock per logical job, a
        post-lock recheck, and two partial unique indexes make a scheduler
        retry, a reconciler-discovered retry, and a late-starting runtime all
        resolve to one row.

        Attempt indexes are ONE-BASED, the stored convention: the runtime
        passes what it read from AWS_BATCH_JOB_ATTEMPT unchanged, and the
        reconciler normalizes its start-time-ordered derivation to the same
        origin. At least one index must be supplied — a caller that knows
        neither is not identifying an attempt.
        """
        if application_attempt_index is None and scheduler_attempt_index is None:
            raise ValueError(
                "resolve_attempt needs at least one attempt index: an "
                "application-observed index (the runtime's own "
                "AWS_BATCH_JOB_ATTEMPT) or a scheduler-observed one (the "
                "reconciler's normalized derivation)")
        for name, index in (("application", application_attempt_index),
                            ("scheduler", scheduler_attempt_index)):
            if index is not None and index < 1:
                raise ValueError(
                    f"attempt indexes are one-based; got {name} index {index}")

        # Every argument is explicitly cast. psycopg2 sends a Python None as
        # an untyped NULL, which PostgreSQL reports as `unknown`, and a
        # function call whose arguments are half `unknown` cannot be resolved
        # to an overload — "function resolve_attempt(unknown, unknown, ...)
        # does not exist", which is what this looked like live before the
        # casts. Casting here rather than adding a second overload keeps one
        # function with one signature.
        sql = (
            "SELECT resolve_attempt("
            "  %s::text, %s::text, %s::text,"
            "  %s::integer, %s::integer,"
            "  %s::timestamptz, %s::timestamptz,"
            "  %s::integer, %s::smallint, %s::text, %s::smallint"
            ")"
        )
        rows = self._execute(sql, [
            identity.run_id, identity.logical_job_id, scheduler_job_id,
            application_attempt_index, scheduler_attempt_index,
            created_at, submitted_at,
            identity.exposure_id, identity.sca, identity.sky_tile,
            self.schema_version,
        ])
        attempt_id = _single_value(rows)
        logger.info("resolved attempt %s for %s/%s (app index %s, sched index %s)",
                    attempt_id, identity.run_id, identity.logical_job_id,
                    application_attempt_index, scheduler_attempt_index)
        return attempt_id

    def create_submitted(self, identity: AttemptIdentity, created_at: Any,
                         submitted_at: Any,
                         scheduler_job_id: str | None = None,
                         binding: ExecutionBinding | None = None) -> int:
        """Create one `submitted` row and return its attempt_id.

        `created_at` is logical-job creation; `submitted_at` is the moment the
        submitter issued the submission, not Batch acceptance. Both are
        application-authored — the scheduler's own view of these times lands in
        the reconciler-written columns instead, so the two are comparable
        afterwards rather than one overwriting the other.

        `scheduler_job_id` is optional because an array child does not have one
        yet at creation time; `backfill_scheduler_job_ids` fills it in.

        `binding` is REQUIRED at schema_version >= 2: migration 013's amended
        submitted-state constraint demands the execution binding, and a writer
        declaring version 2 is one that must supply it. It is checked here
        rather than left to the database so the failure names the missing
        thing instead of arriving as a constraint violation.
        """
        if binding is None and self.schema_version >= 2:
            raise ValueError(
                "an execution binding is required to create a submitted "
                "attempt at schema_version >= 2: the submitted state carries "
                "the submission-time binding (job-definition ARN, image "
                "digest, manifest checksum), copied onto every attempt row at "
                "creation")

        sql = (
            "INSERT INTO attempts ("
            "  schema_version, run_id, logical_job_id, scheduler_job_id,"
            "  exposure_id, sca, sky_tile, lifecycle_state,"
            "  created_at, submitted_at,"
            "  binding_job_definition_arn, binding_job_definition_rev,"
            "  binding_image_digest, binding_release_identity,"
            "  binding_manifest_checksum"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            "          %s, %s, %s, %s, %s)"
            " RETURNING attempt_id"
        )
        params = [
            self.schema_version, identity.run_id, identity.logical_job_id,
            scheduler_job_id, identity.exposure_id, identity.sca,
            identity.sky_tile, LifecycleState.SUBMITTED.value,
            created_at, submitted_at,
            binding.job_definition_arn if binding else None,
            binding.job_definition_rev if binding else None,
            binding.image_digest if binding else None,
            binding.release_identity if binding else None,
            binding.manifest_checksum if binding else None,
        ]
        rows = self._execute(sql, params)
        attempt_id = _single_value(rows)
        logger.info("created submitted attempt %s for %s/%s", attempt_id,
                    identity.run_id, identity.logical_job_id)
        return attempt_id

    def create_submitted_for_submission(self, submission: Any, run_id: str,
                                        created_at: Any, submitted_at: Any,
                                        binding: ExecutionBinding | None = None,
                                        ) -> list[int]:
        """Create one attempt row per array child of a `Submission`.

        Takes `submission.Submission` from the submission package — the object
        that module deliberately returns instead of writing rows itself. One row
        per manifest unit, in index order, each scoped to that unit's
        exposure/SCA, each carrying the child job id derived from the parent.

        The scheduler job ids are derivable here (Batch names children
        ``<parent>:<index>``), so they are set at creation. A caller that cannot
        derive them — a submission path where the parent id is not yet known —
        creates rows without them and backfills.
        """
        attempt_ids: list[int] = []
        for index, unit in enumerate(submission.manifest.units):
            logical_job_id = f"{submission.batch_id}:{index}"
            identity = AttemptIdentity(
                run_id=run_id,
                logical_job_id=logical_job_id,
                exposure_id=unit.exposure,
                sca=unit.sca,
            )
            # The logical job is recorded first, so the binding exists before
            # any attempt row can need to copy it — and so a runtime that
            # resolves its own row (rather than claiming this pre-created one)
            # finds a binding to copy rather than being flagged as an orphan.
            if binding is not None:
                self.create_logical_job(
                    logical_job_id, run_id, binding,
                    scheduler_job_id=submission.child_job_id(index))
            attempt_ids.append(self.create_submitted(
                identity, created_at=created_at, submitted_at=submitted_at,
                scheduler_job_id=submission.child_job_id(index),
                binding=binding))
        logger.info("created %d attempt rows for batch %s",
                    len(attempt_ids), submission.batch_id)
        return attempt_ids

    def backfill_scheduler_job_ids(self,
                                   assignments: Iterable[tuple[int, str]],
                                   ) -> int:
        """Fill in scheduler job ids for rows created before the scheduler answered.

        The array-child ordering the design requires: rows exist first, ids
        arrive second. Only rows still missing an id are touched — the guard is
        in the WHERE clause, so a re-run cannot overwrite an id that is already
        recorded, and a child that never resolves simply keeps its NULL and is
        found by reconciliation.

        Returns the number of rows actually updated, NOT the number of
        statements issued (W2, closing the charge-4 looseness recorded in
        docs/source/dev/attempt_writer_review.rst). The two differ exactly
        when the guard does its job — a row whose id is already recorded, or
        an attempt id that does not exist — and the old count reported
        success for both. A caller reconciling "I had 40 children, 40 were
        backfilled" needs the row count to mean that.

        An executor that returns no usable count (a stub, or a driver that
        does not report one) makes this raise rather than guess: an
        unverifiable backfill is not a backfill.
        """
        sql = ("UPDATE attempts SET scheduler_job_id = %s"
               " WHERE attempt_id = %s AND scheduler_job_id IS NULL")
        updated = 0
        issued = 0
        for attempt_id, scheduler_job_id in assignments:
            result = self._execute(sql, [scheduler_job_id, attempt_id])
            issued += 1
            updated += _rowcount(result, "backfill_scheduler_job_ids")
        if issued != updated:
            logger.warning(
                "backfilled %d of %d scheduler job ids; %d row(s) already "
                "carried an id or did not exist",
                updated, issued, issued - updated)
        else:
            logger.info("backfilled %d scheduler job ids", updated)
        return updated

    # -- lifecycle transitions ----------------------------------------------

    def mark_started(self, attempt_id: int, started_at: Any,
                     provenance: Provenance,
                     scheduler_job_id: str | None = None,
                     application_attempt_index: int | None = None,
                     config_snapshot_key: str | None = None) -> None:
        """Advance a row to `started`. A real compare-and-set.

        Provenance is required here, not optional: the DDL will reject a
        `started` row without it. `scheduler_job_id` may be supplied for a row
        that was created without one and never backfilled — it is never absent
        once a row reaches `started`.

        **This is the transition that binds the configuration snapshot**
        (review finding #10). The adopted design: "The attempt→snapshot
        binding is a single database write: the same compare-and-set that
        marks the attempt started carries the digest and snapshot key, so
        there is no bound-but-unpersisted or worked-but-unbound state."
        `start_attempt` passed the key and it was only logged — the row bound
        the digest and not the object holding it, so the key had to be
        re-derived from the mutable records prefix afterwards. Migration 017
        adds the column; this writes it.

        **It is a compare-and-set, not an unconditional UPDATE** (review
        finding #10). The statement used to match on `attempt_id` alone, so two
        startup writers could both "start" one row and the later one would
        overwrite the first's start time and provenance. The WHERE clause now
        requires the row to still be in a state a start may leave — `submitted`
        — so exactly one writer wins and a second gets `AttemptNotFound`
        rather than silently clobbering.

        **`application_attempt_index` is written HERE, not at claim time**
        (review finding #9). It is the DDL's evidence that the application
        ran: `terminal_without_start` forbids it. Migration 017 moved the
        resolver's claim into `application_claim_index` so that a container
        killed between claim and start leaves a row that can still be closed
        as never-started — the specification's own legal window. This
        transition is where the claim becomes a start.
        """
        sql = (
            "UPDATE attempts SET lifecycle_state = %s, started_at = %s,"
            "  source_sha = %s, container_digest = %s,"
            "  job_definition_rev = %s, config_digest = %s,"
            "  config_snapshot_key = %s,"
            "  scheduler_job_id = COALESCE(%s, scheduler_job_id),"
            "  application_attempt_index = COALESCE("
            "    %s, application_claim_index, application_attempt_index)"
            " WHERE attempt_id = %s AND lifecycle_state = %s"
        )
        result = self._execute(sql, [
            LifecycleState.STARTED.value, started_at,
            provenance.source_sha, provenance.container_digest,
            provenance.job_definition_rev, provenance.config_digest,
            config_snapshot_key,
            scheduler_job_id, application_attempt_index, attempt_id,
            LifecycleState.SUBMITTED.value,
        ])
        _require_one_row(result, "mark_started", attempt_id,
                         expected_state=LifecycleState.SUBMITTED.value)
        logger.info("attempt %s started (snapshot %s)",
                    attempt_id, config_snapshot_key)

    def mark_application_closed(self, attempt_id: int, ended_at: Any,
                                application_intended_exit: int,
                                rapid_outcome: RapidOutcome,
                                product_disposition: ProductDisposition,
                                terminal_record_key: str,
                                terminal_record_sequence: int = 0,
                                terminal_record_checksum: str | None = None,
                                error_category: str | None = None,
                                reconciler_materialized: bool = False) -> None:
        """Close the application-authored half of an attempt.

        The termination protocol's final database step
        (D:batch-payload-co-design): the S3 terminal record has ALREADY been
        written — this transition cites it — so a crash between the two leaves
        a started row beside a valid record, which the reconciler materializes
        rather than a closed row citing a record that does not exist.

        The scheduler-observed facts are deliberately not written here: they
        are not yet known, which is what the state means. The reconciler's
        `mark_terminal_after_start` supplies them.

        `application_intended_exit` is an INTENT, not an observation — the
        process has not exited yet when this is written. Under the fail-loud
        posture a classified application failure still intends exit 0: a
        nonzero exit is reserved for the unrecordable.

        `reconciler_materialized` is set only by the reconciler, when it
        projects this transition from a validated S3 record — the one
        sanctioned projection of application facts by another writer.

        **A real compare-and-set (review finding #10).** The statement used to
        match on `attempt_id` alone, so a replayed or concurrent close could
        overwrite an already-closed row's outcome. It now requires the row to
        still be `started` — the only state an application-close may leave —
        so a second closer gets `AttemptNotFound` instead of silently
        rewriting the first one's account.
        """
        if terminal_record_sequence < 0:
            raise ValueError(
                f"terminal record sequence is monotonic from 0; got "
                f"{terminal_record_sequence}")
        # APPLICATION-authored, so the application's half of the vocabulary
        # is the allowlist here — not the union. `scheduler_reclaimed` and
        # `scheduler_provisioning` describe things only the scheduler
        # observer can know, and an application claiming one would be
        # inventing an observation it never made. Found by W8's battery,
        # 2026-08-06: `_validate_error_category` checks the union, so this
        # writer accepted a reconciler category and the design's "no field
        # has two writers" held only by convention on this path. A
        # reconciler-materialized close is the same rule — it carries the
        # APPLICATION's category, copied verbatim from the record.
        _validate_application_error_category(error_category)

        sql = (
            "UPDATE attempts SET lifecycle_state = %s, ended_at = %s,"
            "  application_intended_exit = %s, rapid_outcome = %s,"
            "  product_disposition = %s, error_category = %s,"
            "  terminal_record_key = %s, terminal_record_sequence = %s,"
            "  terminal_record_checksum = %s, reconciler_materialized = %s"
            " WHERE attempt_id = %s AND lifecycle_state = %s"
        )
        result = self._execute(sql, [
            LifecycleState.APPLICATION_CLOSED.value, ended_at,
            application_intended_exit, _value(rapid_outcome),
            _value(product_disposition), error_category,
            terminal_record_key, terminal_record_sequence,
            terminal_record_checksum, reconciler_materialized, attempt_id,
            LifecycleState.STARTED.value,
        ])
        _require_one_row(result, "mark_application_closed", attempt_id,
                         expected_state=LifecycleState.STARTED.value)
        logger.info(
            "attempt %s application-closed (intended exit %s, outcome %s, "
            "record %s seq %s%s)",
            attempt_id, application_intended_exit, _value(rapid_outcome),
            terminal_record_key, terminal_record_sequence,
            ", reconciler-materialized" if reconciler_materialized else "")

    def mark_terminal_after_start(self, attempt_id: int, ended_at: Any,
                                  scheduler_observed_exit: int,
                                  scheduler_state: str,
                                  rapid_outcome: RapidOutcome | None = None,
                                  product_disposition: ProductDisposition | None = None,
                                  application_intended_exit: int | None = None,
                                  error_category: str | None = None,
                                  terminal_record_key: str | None = None,
                                  terminal_record_sequence: int | None = None,
                                  terminal_record_checksum: str | None = None,
                                  ) -> None:
        """Close an attempt fully — the RECONCILER's transition.

        Amended (D:batch-payload-co-design): this is no longer the
        application's closing step. The application closes its own half with
        `mark_application_closed`; this adds the scheduler-observed facts the
        application could not know — the scheduler end state and the exit code
        the container actually produced — and is therefore reconciler-authored.

        `scheduler_observed_exit` and `scheduler_state` are required. The
        application-authored fields are optional because the normal path
        already wrote them at application-close; they are accepted here for
        the case where the reconciler is closing an attempt that never wrote
        its own record, and are applied with COALESCE so a reconciler pass
        never overwrites what the application authored.

        `rapid_outcome` is the application's own verdict and is deliberately
        independent of `scheduler_state`: SUCCEEDED with rapid_outcome=failure
        is a representable, expected combination — the 2026-07-22 failure mode
        the taxonomy exists to expose. Callers pass what actually happened; this
        method never infers one field from the other.

        The three `terminal_record_*` fields are ONE CITATION and move
        together. They are deliberately NOT on the
        `COALESCE(existing, new)` side of the rule above: the reconciler's
        own record supersedes the application's, so a supplied value wins
        (`COALESCE(new, existing)`), exactly as the key already did.
        Omitting the checksum here is what left a row citing a sequence-1
        key beside the sequence-0 checksum — a pair no reader can
        validate, because folding the predecessor's FACTS in verbatim does
        not make the two records' BYTES equal, and it is the bytes a
        consumer checksums. The registrar verified that pair and refused
        every materialized attempt (round-3 finding #1).
        """
        _validate_scheduler_state(scheduler_state)
        if scheduler_state is None:
            raise ValueError(
                "scheduler_state is required to reach terminal_after_start: "
                "it is the scheduler-observed fact that distinguishes this "
                "state from application_closed")
        _validate_error_category(error_category)

        sql = (
            "UPDATE attempts SET lifecycle_state = %s, ended_at = %s,"
            "  scheduler_observed_exit = %s, scheduler_state = %s,"
            "  application_intended_exit ="
            "    COALESCE(application_intended_exit, %s),"
            "  rapid_outcome = COALESCE(rapid_outcome, %s),"
            "  product_disposition = COALESCE(product_disposition, %s),"
            "  error_category = COALESCE(error_category, %s),"
            "  terminal_record_key = COALESCE(%s, terminal_record_key),"
            "  terminal_record_sequence ="
            "    COALESCE(%s, terminal_record_sequence),"
            "  terminal_record_checksum ="
            "    COALESCE(%s, terminal_record_checksum)"
            " WHERE attempt_id = %s"
        )
        result = self._execute(sql, [
            LifecycleState.TERMINAL_AFTER_START.value, ended_at,
            scheduler_observed_exit, scheduler_state,
            application_intended_exit, _value(rapid_outcome),
            _value(product_disposition), error_category,
            terminal_record_key, terminal_record_sequence,
            terminal_record_checksum, attempt_id,
        ])
        _require_one_row(result, "mark_terminal_after_start", attempt_id)
        logger.info(
            "attempt %s terminal after start (scheduler exit %s, state %s, "
            "record %s seq %s)",
            attempt_id, scheduler_observed_exit, scheduler_state,
            terminal_record_key, terminal_record_sequence)

    def mark_terminal_without_start(self, attempt_id: int, ended_at: Any,
                                    scheduler_state: str,
                                    error_category: str | None = None,
                                    closure_record_key: str | None = None,
                                    closure_record_sequence: int | None = None,
                                    ) -> None:
        """Close an attempt that never ran.

        No exit code, no application outcome, no product disposition — nothing
        ran, so none of those facts exist. They are left NULL rather than
        zero-filled, and the DDL forbids them in this state.

        **The closure record IS cited (review finding #14).** "The reconciler
        closes *every* attempt with a closure record" — including this one.
        013 forbade `terminal_record_key` here, correctly: that column is the
        APPLICATION's sequence-0 record, which a never-started attempt indeed
        never wrote. But it left the RECONCILER's closure record with nowhere
        to be cited from, so the published object was unreferenced from the
        row it accounts for and findable only by reconstructing its key.
        Migration 017 adds the reconciler-authored pair; this writes it.
        """
        _validate_scheduler_state(scheduler_state)
        if (closure_record_key is None) != (closure_record_sequence is None):
            raise ValueError(
                "a closure record is cited by key AND sequence or by neither; "
                f"got key={closure_record_key!r} "
                f"sequence={closure_record_sequence!r}")
        if closure_record_sequence is not None and closure_record_sequence < 1:
            raise ValueError(
                f"the application owns sequence 0; a reconciler closure "
                f"record is sequence >= 1, got {closure_record_sequence}")

        sql = (
            "UPDATE attempts SET lifecycle_state = %s, ended_at = %s,"
            "  scheduler_state = %s, error_category = %s,"
            "  closure_record_key = %s, closure_record_sequence = %s"
            " WHERE attempt_id = %s"
        )
        result = self._execute(sql, [
            LifecycleState.TERMINAL_WITHOUT_START.value, ended_at,
            scheduler_state, error_category,
            closure_record_key, closure_record_sequence, attempt_id,
        ])
        _require_one_row(result, "mark_terminal_without_start", attempt_id)
        logger.info("attempt %s terminal without start (%s, closure %s)",
                    attempt_id, scheduler_state, closure_record_key)

    def mark_abrupt_loss(self, attempt_id: int, ended_at: Any,
                         scheduler_state: str,
                         error_category: str,
                         product_disposition: ProductDisposition
                         = ProductDisposition.NONE,
                         scheduler_observed_exit: int | None = None,
                         terminal_record_key: str | None = None,
                         terminal_record_sequence: int | None = None) -> None:
        """Close a started attempt that died without reporting — OOM kill, Spot
        reclaim, host death.

        The job never got to write its own terminal record, so the reconciler
        closes the row from Batch state plus the CloudWatch safety stream. It is
        still `terminal_after_start`: the attempt did start, and its provenance
        is already on the row.

        `rapid_outcome` is `failure` — that much is known. The exit code is the
        honest problem: an abruptly-killed process may have none observable. The
        DDL requires one in this state, so a caller that has a real code (Batch
        reports 137 for an OOM kill) passes it, and one that does not gets the
        conventional 128+SIGKILL rather than a fabricated zero. Passing a code
        that says "killed" is a statement about what happened; passing 0 would
        be a statement that it succeeded.

        Amended (D:batch-payload-co-design): the exit code written here is the
        SCHEDULER-observed one, because the reconciler is the writer and the
        scheduler is where the observation came from. The
        application-intended exit stays absent — the application never got to
        state an intent, and NULL says exactly that where a fabricated value
        would not. At schema_version >= 2 the reconciler supplies its
        reconciler-first record's key and sequence, since a
        terminal_after_start row must cite the record that accounts for it.
        """
        _validate_scheduler_state(scheduler_state)
        exit_code = (scheduler_observed_exit if scheduler_observed_exit is not None
                     else _SIGKILL_EXIT_CODE)
        self.mark_terminal_after_start(
            attempt_id, ended_at=ended_at,
            scheduler_observed_exit=exit_code,
            scheduler_state=scheduler_state,
            rapid_outcome=RapidOutcome.FAILURE,
            product_disposition=product_disposition,
            error_category=error_category,
            terminal_record_key=terminal_record_key,
            terminal_record_sequence=terminal_record_sequence)
        logger.warning("attempt %s closed as abrupt loss (%s, exit %s)",
                       attempt_id, error_category, exit_code)

    def mark_missing_or_contradictory(self, attempt_id: int,
                                      reconciliation_class: ReconciliationClass,
                                      reconciliation_sources: Sequence[str],
                                      detected_at: Any) -> None:
        """Flag a row whose observations are missing or disagree.

        All three reconciliation fields are required together — the sources list
        included, so a flagged row always records which stores were compared.
        """
        if not reconciliation_sources:
            raise ValueError(
                "reconciliation_sources cannot be empty: a flagged row must "
                "record which stores were compared")
        # `reconciler_materialized` is cleared with the transition, and that
        # is required rather than tidy: migration 013's
        # attempts_reconciler_materialized_check permits the flag ONLY in
        # application_closed or terminal_after_start, so leaving it set while
        # moving to missing_or_contradictory violates the constraint and the
        # row can never be classified at all.
        #
        # Found live 2026-08-06 (W8), on the running service: an attempt that
        # had been legitimately materialized from its record, and whose
        # scheduler observation later disagreed, failed EVERY poll with
        # CheckViolation — permanently unclassifiable, and counted as a poll
        # error forever.
        #
        # Clearing it is also the honest value. The flag says "this row's
        # application facts were projected from the record by another
        # writer"; a row being flagged missing-or-contradictory is precisely
        # the case where that projection is no longer what the row asserts.
        sql = (
            "UPDATE attempts SET lifecycle_state = %s,"
            "  reconciliation_class = %s, reconciliation_sources = %s,"
            "  reconciliation_detected_at = %s,"
            "  reconciler_materialized = false"
            " WHERE attempt_id = %s"
        )
        result = self._execute(sql, [
            LifecycleState.MISSING_OR_CONTRADICTORY.value,
            _value(reconciliation_class), list(reconciliation_sources),
            detected_at, attempt_id,
        ])
        _require_one_row(result, "mark_missing_or_contradictory", attempt_id)
        logger.warning("attempt %s flagged %s from sources %s", attempt_id,
                       _value(reconciliation_class),
                       ",".join(reconciliation_sources))

    # -- reconciler-written scheduler columns --------------------------------

    def record_scheduler_observation(self, attempt_id: int,
                                     scheduler_state: str | None = None,
                                     created_at: Any = None,
                                     started_at: Any = None,
                                     stopped_at: Any = None,
                                     attempt_index: int | None = None) -> None:
        """Write the scheduler-observed columns from `DescribeJobs`.

        These columns have exactly one writer — the reconciler — and they sit
        beside the application-authored timestamps rather than replacing them.
        That is the whole point of the amendment: disagreement between the two
        is preserved so reconciliation can compute and flag it, never resolved
        silently by a last-write.

        An array child's Batch `createdAt` is child-spawn time, not array-call
        acceptance, so comparing it against the application's `submitted_at`
        includes normal spawn delay — the tolerance that governs the comparison
        has to absorb that, and it is not this writer's job to adjust for it.
        """
        _validate_scheduler_state(scheduler_state)
        if attempt_index is not None and attempt_index < 0:
            raise ValueError(f"scheduler attempt index {attempt_index} is negative")
        sql = (
            "UPDATE attempts SET"
            "  scheduler_state = COALESCE(%s, scheduler_state),"
            "  scheduler_created_at = COALESCE(%s, scheduler_created_at),"
            "  scheduler_started_at = COALESCE(%s, scheduler_started_at),"
            "  scheduler_stopped_at = COALESCE(%s, scheduler_stopped_at),"
            "  scheduler_attempt_index = COALESCE(%s, scheduler_attempt_index)"
            " WHERE attempt_id = %s"
        )
        result = self._execute(sql, [scheduler_state, created_at, started_at,
                                     stopped_at, attempt_index, attempt_id])
        _require_one_row(result, "record_scheduler_observation", attempt_id)

    # -- stages and milestones ----------------------------------------------

    def record_stage(self, attempt_id: int, stage: Stage) -> None:
        """Append one completed stage span. Written once, never updated."""
        sql = (
            "INSERT INTO attempt_stages ("
            "  attempt_id, stage_name, started_at, duration_ms, outcome"
            ") VALUES (%s, %s, %s, %s, %s)"
        )
        self._execute(sql, [attempt_id, stage.stage_name, stage.started_at,
                            stage.duration_ms, _value(stage.outcome)])

    def record_stages(self, attempt_id: int, stages: Iterable[Stage]) -> int:
        count = 0
        for stage in stages:
            self.record_stage(attempt_id, stage)
            count += 1
        return count

    def record_milestone(self, milestone_name: str, reached_at: Any,
                         exposure_id: int | None = None,
                         sca: int | None = None,
                         sky_tile: str | None = None,
                         producing_attempt_id: int | None = None) -> None:
        """Record a named SCA-to-alert boundary.

        Scoped to the processing unit, not to the attempt: a unit may reach a
        milestone through more than one attempt across retries, so the producing
        attempt is carried for traceability only and is never the join key.
        """
        if exposure_id is None and sca is None and sky_tile is None:
            raise ValueError(
                f"milestone {milestone_name!r} needs a processing-unit scope: "
                "at least one of exposure_id, sca, sky_tile")
        sql = (
            "INSERT INTO milestones ("
            "  milestone_name, exposure_id, sca, sky_tile, reached_at,"
            "  producing_attempt_id"
            ") VALUES (%s, %s, %s, %s, %s, %s)"
        )
        self._execute(sql, [milestone_name, exposure_id, sca, sky_tile,
                            reached_at, producing_attempt_id])


# Conventional exit code for a SIGKILLed process (128 + 9). Batch reports 137
# for an OOM-killed container; this is the fallback when no code was observed.
_SIGKILL_EXIT_CODE = 137


class LogicalJobConflict(RuntimeError):
    """Two submissions claimed one logical-job identity with different bindings.

    Raised by `create_logical_job` when its insert conflicted and the recorded
    binding disagrees with the one offered (W-FixA, review finding #3). Under
    the run-scoped logical-job key this should be unreachable — which is why it
    raises rather than warns: reaching it means the key stopped being unique
    per submission, and every attempt created afterwards would carry a binding
    that does not describe it.
    """


class AttemptNotFound(RuntimeError):
    """A lifecycle transition matched no row.

    Raised where a transition's UPDATE affected zero rows (W2, closing the
    charge-4 looseness). A transition against a nonexistent attempt id is a
    caller bug every time — there is no legitimate path that advances a row
    that is not there — and it must not be able to look like success. The old
    behaviour returned None from the executor and logged "attempt N started"
    for an attempt that did not exist.
    """


def _rowcount(result: Any, operation: str) -> int:
    """Read the affected-row count from an executor result.

    The executor contract (``rapid_db_connect.ConnectionExecutor``) returns
    rows for a statement with a result set and ``cursor.rowcount`` — an int —
    for one without. This helper accepts the int, and also accepts a
    result-set shape for the case of an ``UPDATE ... RETURNING``, where the
    number of returned rows IS the affected-row count.

    A result that is neither (notably ``None``, which is what an executor
    that does not report counts returns) raises rather than being read as
    zero or as one. Guessing here would reintroduce exactly the ambiguity
    this change removes: "no count available" and "no rows matched" are
    different facts, and only one of them is a bug in the caller.
    """
    if isinstance(result, bool):
        raise TypeError(
            f"{operation}: executor returned a bool where a row count was "
            f"expected")
    if isinstance(result, int):
        if result < 0:
            raise RuntimeError(
                f"{operation}: executor reported row count {result}; a "
                f"negative count means the driver did not track the "
                f"statement, which cannot be distinguished from no rows "
                f"matching")
        return result
    if isinstance(result, (list, tuple)):
        return len(result)
    raise RuntimeError(
        f"{operation}: executor returned {type(result).__name__}, which "
        f"carries no affected-row count. A lifecycle transition must be able "
        f"to tell 'advanced one row' from 'matched nothing'; an executor that "
        f"cannot say is not usable for one.")


def _require_one_row(result: Any, operation: str, attempt_id: Any,
                     expected_state: str | None = None) -> None:
    """Assert that a transition advanced exactly the one row it named.

    `expected_state` names the lifecycle state a compare-and-set transition
    required. Where one is given, a zero-row result has two possible causes —
    the attempt does not exist, or it is no longer in the state the transition
    may leave — and the message says both, because the second is the
    interesting one: it means another writer got there first, which is the
    compare-and-set doing its job rather than a caller bug.
    """
    count = _rowcount(result, operation)
    if count == 0:
        if expected_state is not None:
            raise AttemptNotFound(
                f"{operation}: no attempt row with attempt_id={attempt_id!r} "
                f"in lifecycle state {expected_state!r}. Either the attempt "
                f"does not exist, or it has already left that state — a "
                f"concurrent or replayed writer reached it first. Nothing was "
                f"written, which is the compare-and-set holding: this "
                f"transition never overwrites another writer's account.")
        raise AttemptNotFound(
            f"{operation}: no attempt row with attempt_id={attempt_id!r}. A "
            f"lifecycle transition against a nonexistent attempt is a caller "
            f"bug; nothing was written.")
    if count > 1:
        raise RuntimeError(
            f"{operation}: {count} rows matched attempt_id={attempt_id!r}, "
            f"which is impossible under the primary key — the statement did "
            f"not filter on the attempt id it claimed to")


def _validate_error_category(category: str | None) -> None:
    """Reject a category outside migration 013's v1 allowlist.

    The database's foreign key is the authority; this is an early, local
    failure so a typo names itself instead of arriving as a 23503 after a
    round trip. NULL is allowed — a successful attempt has no category.
    """
    if category is not None and category not in ERROR_CATEGORIES:
        raise ValueError(
            f"{category!r} is not in the v1 error-category allowlist; "
            f"expected one of " + ", ".join(sorted(ERROR_CATEGORIES))
            + " (extending the vocabulary is a schema-versioned change)")


def _validate_application_error_category(category: str | None) -> None:
    """Reject a category the APPLICATION is not entitled to author.

    The union allowlist above is right for the reconciler's own writes and
    for the schema's foreign key, which must admit every category. It is
    wrong for an application-authored transition: the two reconciler
    categories are scheduler OBSERVATIONS, and the whole one-author-per-field
    rule is that the application does not make them.
    """
    if category is not None and category in RECONCILER_ERROR_CATEGORIES:
        raise ValueError(
            f"{category!r} is reconciler-authored and cannot be set by an "
            f"application-closed transition; the application's categories "
            f"are " + ", ".join(sorted(APPLICATION_ERROR_CATEGORIES)))
    _validate_error_category(category)


def _validate_scheduler_state(state: str | None) -> None:
    if state is not None and state not in SCHEDULER_STATES:
        raise ValueError(
            f"{state!r} is not a Batch job state; expected one of "
            + ", ".join(sorted(SCHEDULER_STATES)))


def _value(member: Any) -> Any:
    """Unwrap an enum member to the string the DDL's CHECK expects."""
    return member.value if isinstance(member, enum.Enum) else member


def _single_value(rows: Any) -> Any:
    """Pull the one value out of a RETURNING result."""
    if rows is None:
        raise RuntimeError("INSERT ... RETURNING produced no result")
    if isinstance(rows, (list, tuple)):
        if not rows:
            raise RuntimeError("INSERT ... RETURNING produced no rows")
        first = rows[0]
        if isinstance(first, (list, tuple)):
            return first[0]
        if isinstance(first, dict):
            return next(iter(first.values()))
        return first
    return rows
