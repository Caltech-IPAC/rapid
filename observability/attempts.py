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
SCHEMA_VERSION = 1


class LifecycleState(str, enum.Enum):
    """The five lifecycle states. Values match the DDL's CHECK vocabulary."""

    SUBMITTED = "submitted"
    STARTED = "started"
    TERMINAL_AFTER_START = "terminal_after_start"
    TERMINAL_WITHOUT_START = "terminal_without_start"
    MISSING_OR_CONTRADICTORY = "missing_or_contradictory"


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

    def create_submitted(self, identity: AttemptIdentity, created_at: Any,
                         submitted_at: Any,
                         scheduler_job_id: str | None = None) -> int:
        """Create one `submitted` row and return its attempt_id.

        `created_at` is logical-job creation; `submitted_at` is the moment the
        submitter issued the submission, not Batch acceptance. Both are
        application-authored — the scheduler's own view of these times lands in
        the reconciler-written columns instead, so the two are comparable
        afterwards rather than one overwriting the other.

        `scheduler_job_id` is optional because an array child does not have one
        yet at creation time; `backfill_scheduler_job_ids` fills it in.
        """
        sql = (
            "INSERT INTO attempts ("
            "  schema_version, run_id, logical_job_id, scheduler_job_id,"
            "  exposure_id, sca, sky_tile, lifecycle_state,"
            "  created_at, submitted_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING attempt_id"
        )
        params = [
            self.schema_version, identity.run_id, identity.logical_job_id,
            scheduler_job_id, identity.exposure_id, identity.sca,
            identity.sky_tile, LifecycleState.SUBMITTED.value,
            created_at, submitted_at,
        ]
        rows = self._execute(sql, params)
        attempt_id = _single_value(rows)
        logger.info("created submitted attempt %s for %s/%s", attempt_id,
                    identity.run_id, identity.logical_job_id)
        return attempt_id

    def create_submitted_for_submission(self, submission: Any, run_id: str,
                                        created_at: Any, submitted_at: Any,
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
            identity = AttemptIdentity(
                run_id=run_id,
                logical_job_id=f"{submission.batch_id}:{index}",
                exposure_id=unit.exposure,
                sca=unit.sca,
            )
            attempt_ids.append(self.create_submitted(
                identity, created_at=created_at, submitted_at=submitted_at,
                scheduler_job_id=submission.child_job_id(index)))
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
        """
        sql = ("UPDATE attempts SET scheduler_job_id = %s"
               " WHERE attempt_id = %s AND scheduler_job_id IS NULL")
        updated = 0
        for attempt_id, scheduler_job_id in assignments:
            self._execute(sql, [scheduler_job_id, attempt_id])
            updated += 1
        logger.info("backfilled %d scheduler job ids", updated)
        return updated

    # -- lifecycle transitions ----------------------------------------------

    def mark_started(self, attempt_id: int, started_at: Any,
                     provenance: Provenance,
                     scheduler_job_id: str | None = None) -> None:
        """Advance a row to `started`.

        Provenance is required here, not optional: the DDL will reject a
        `started` row without it. `scheduler_job_id` may be supplied for a row
        that was created without one and never backfilled — it is never absent
        once a row reaches `started`.
        """
        sql = (
            "UPDATE attempts SET lifecycle_state = %s, started_at = %s,"
            "  source_sha = %s, container_digest = %s,"
            "  job_definition_rev = %s, config_digest = %s,"
            "  scheduler_job_id = COALESCE(%s, scheduler_job_id)"
            " WHERE attempt_id = %s"
        )
        self._execute(sql, [
            LifecycleState.STARTED.value, started_at,
            provenance.source_sha, provenance.container_digest,
            provenance.job_definition_rev, provenance.config_digest,
            scheduler_job_id, attempt_id,
        ])
        logger.info("attempt %s started", attempt_id)

    def mark_terminal_after_start(self, attempt_id: int, ended_at: Any,
                                  process_exit_code: int,
                                  rapid_outcome: RapidOutcome,
                                  product_disposition: ProductDisposition,
                                  error_category: str | None = None,
                                  scheduler_state: str | None = None) -> None:
        """Close a started attempt.

        `rapid_outcome` is the application's own verdict and is deliberately
        independent of `scheduler_state`: SUCCEEDED with rapid_outcome=failure
        is a representable, expected combination — the 2026-07-22 failure mode
        the taxonomy exists to expose. Callers pass what actually happened; this
        method never infers one field from the other.
        """
        _validate_scheduler_state(scheduler_state)
        sql = (
            "UPDATE attempts SET lifecycle_state = %s, ended_at = %s,"
            "  process_exit_code = %s, rapid_outcome = %s,"
            "  product_disposition = %s, error_category = %s,"
            "  scheduler_state = COALESCE(%s, scheduler_state)"
            " WHERE attempt_id = %s"
        )
        self._execute(sql, [
            LifecycleState.TERMINAL_AFTER_START.value, ended_at,
            process_exit_code, _value(rapid_outcome),
            _value(product_disposition), error_category,
            scheduler_state, attempt_id,
        ])
        logger.info("attempt %s terminal after start (exit %s, outcome %s)",
                    attempt_id, process_exit_code, _value(rapid_outcome))

    def mark_terminal_without_start(self, attempt_id: int, ended_at: Any,
                                    scheduler_state: str,
                                    error_category: str | None = None) -> None:
        """Close an attempt that never ran.

        No exit code, no application outcome, no product disposition — nothing
        ran, so none of those facts exist. They are left NULL rather than
        zero-filled, and the DDL forbids them in this state.
        """
        _validate_scheduler_state(scheduler_state)
        sql = (
            "UPDATE attempts SET lifecycle_state = %s, ended_at = %s,"
            "  scheduler_state = %s, error_category = %s"
            " WHERE attempt_id = %s"
        )
        self._execute(sql, [
            LifecycleState.TERMINAL_WITHOUT_START.value, ended_at,
            scheduler_state, error_category, attempt_id,
        ])
        logger.info("attempt %s terminal without start (%s)",
                    attempt_id, scheduler_state)

    def mark_abrupt_loss(self, attempt_id: int, ended_at: Any,
                         scheduler_state: str,
                         error_category: str,
                         product_disposition: ProductDisposition
                         = ProductDisposition.NONE,
                         process_exit_code: int | None = None) -> None:
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
        """
        _validate_scheduler_state(scheduler_state)
        exit_code = (process_exit_code if process_exit_code is not None
                     else _SIGKILL_EXIT_CODE)
        self.mark_terminal_after_start(
            attempt_id, ended_at=ended_at, process_exit_code=exit_code,
            rapid_outcome=RapidOutcome.FAILURE,
            product_disposition=product_disposition,
            error_category=error_category, scheduler_state=scheduler_state)
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
        sql = (
            "UPDATE attempts SET lifecycle_state = %s,"
            "  reconciliation_class = %s, reconciliation_sources = %s,"
            "  reconciliation_detected_at = %s"
            " WHERE attempt_id = %s"
        )
        self._execute(sql, [
            LifecycleState.MISSING_OR_CONTRADICTORY.value,
            _value(reconciliation_class), list(reconciliation_sources),
            detected_at, attempt_id,
        ])
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
        self._execute(sql, [scheduler_state, created_at, started_at,
                            stopped_at, attempt_index, attempt_id])

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
