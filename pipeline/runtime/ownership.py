"""
File:    ownership.py

Attempt ownership at startup: resolving this process's attempt row before any
work happens.

The proposal: "The runtime resolves its attempt row before any work, without
touching any reconciler-authored column." Acquisition goes through migration
013's `resolve_attempt` database function, reached via W1's `AttemptWriter`
over a `ConnectionExecutor`. This module is the runtime's side of that — the
part that reads the environment, normalizes the attempt index, calls the
resolver, and refuses to proceed when the answer is not usable.

**Attempt 1 claims; N > 1 creates.** Both through the one resolver, which is
the point: the resolver's post-lock recheck and its two partial uniqueness
constraints are what make a scheduler retry, a reconciler-created row, and a
late-starting runtime converge on one row. The runtime never bare-INSERTs and
never decides for itself whether a row exists — it states its identity and the
resolver answers. The claim-vs-create distinction visible here is therefore
descriptive (what the resolver did) rather than a branch this code takes.

**Numbering is normalized once, at the edge.** `AWS_BATCH_JOB_ATTEMPT` is
one-based per Batch, and W1's stored convention is one-based, so the
normalization is the identity function — but it is written down as a named
function with a test rather than left implicit, because "both are one-based"
is exactly the kind of fact that is true until someone changes one side.
`environment.read_environment` has already range-checked it; this module is
where the convention itself is documented.

**A `missing_or_contradictory` resolution is a hard stop.** The resolver
creates a row in that state when no logical job exists to copy the execution
binding from — an attempt Batch knows about whose submission was never
recorded. Continuing would mean doing science work whose provenance cannot be
completed, so the runtime raises `RecordsError` and exits nonzero, leaving the
flagged row for the reconciler. This is the fail-loud posture's "records path
unreachable" case: the row exists, so the failure is visible, but it is not
one the application can record an outcome into.

**WHY THIS MODULE DOES NOT ALSO CONSULT `submissions.state` (campaign C4).**
`submission.protocol.resolve_submission_outcome` is the one function this
codebase now uses to answer "did this attempt reach the scheduler",
consolidating what used to be three independently-read vocabularies
(`submissions.state`, `attempts.lifecycle_state ==
missing_or_contradictory`, and the reconciler's `never_resolved` closure
classification) — see that function's own docstring. `pipeline.reconciler.
service._reconcile_unresolved` calls it; this module deliberately does NOT,
and the reason is reachability, not an oversight. `resolve_ownership` runs
INSIDE a container Batch has already started — the scheduler had to invoke
this process for `resolve_ownership` to execute at all. A submission
resolves `LOST` only from a NEGATIVE re-query of Batch's own job listing,
past its resolution deadline (`submission.protocol.resolve`'s own
docstring: "Found. The job exists... Not found, past the deadline.
Recorded as lost"). Those two facts cannot coexist: a running container
under a submission is positive proof the job exists, so `resolve()` would
find it and mark the submission `FOUND`, never `LOST`. There is no
`LOST`-submission state this module could observe for its OWN attempt that
is not already evidence something upstream is badly wrong in a way a
submission-outcome read cannot diagnose. The `missing_or_contradictory`
check above remains the correct, and sufficient, guard for this module's
actual reachable risk — a fact about the ATTEMPT ROW ITSELF, synchronously
readable at claim time, not a fact about a scheduler call this process's
own existence already answers.
"""

import dataclasses
import datetime
from typing import Any

from pipeline.runtime.errors import DBError, RecordsError
from pipeline.runtime.logging_setup import get_logger

_logger = get_logger("ownership")


class AttemptAlreadySucceeded(Exception):
    """A predecessor attempt of this logical job already succeeded.

    Not a failure, and deliberately not a `RuntimeErrorBase`: nothing went
    wrong. It is the control-flow signal for adoption — the terminal record
    was already written, so this retry exits 0 without running a stage (the
    2026-09-13 13:01 ruling, "retry only when the terminal record was not
    written").

    Raised rather than returned because it must unwind the whole ownership
    path: every caller of `resolve_ownership` goes straight on to persist a
    snapshot and mark the row started, and a return value would have to be
    checked at each of those steps to stop it.
    """

    def __init__(self, attempt_id, logical_job_id, index):
        self.attempt_id = attempt_id
        self.logical_job_id = logical_job_id
        self.index = index
        super().__init__(
            "adopting attempt %s: terminal record already written for "
            "logical job %s by a lower-indexed attempt; this is attempt %s "
            "and it has nothing to do"
            % (attempt_id, logical_job_id, index))


def normalize_attempt_index(scheduler_value: Any) -> int:
    """Map `AWS_BATCH_JOB_ATTEMPT` onto the stored one-based convention.

    Batch numbers attempts from 1, and W1's recorded convention stores them
    one-based, so this is the identity function on valid input. It exists as a
    named, tested function because the two conventions agreeing is a fact
    about two independent systems, not a tautology: if either side ever moves,
    this is the one place that changes, and its test is what fails first.

    Raises `ValueError` on anything that is not a positive integer — a
    normalization that quietly produced a wrong index would key the resolver
    to the wrong row, which is worse than not starting.
    """
    try:
        value = int(scheduler_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"attempt index {scheduler_value!r} is not an integer; "
            f"AWS_BATCH_JOB_ATTEMPT is the scheduler's one-based attempt "
            f"number") from exc
    if value < 1:
        raise ValueError(
            f"attempt index {value} is not one-based; Batch numbers attempts "
            f"from 1 and the stored convention (W1) is one-based")
    return value


@dataclasses.dataclass(frozen=True)
class AttemptOwnership:
    """The resolved attempt row and how it was resolved."""

    attempt_id: int
    run_id: str
    logical_job_id: str
    scheduler_job_id: str
    attempt_index: int
    #: True when this is the scheduler's first attempt, which is the case that
    #: claims the submission layer's pre-created row. Descriptive: the
    #: resolver decided, this records what it decided.
    claimed_precreated: bool

    def __str__(self) -> str:
        how = "claimed pre-created row" if self.claimed_precreated \
            else "created for retry"
        return (f"attempt {self.attempt_id} "
                f"({self.logical_job_id} index {self.attempt_index}, {how})")


def resolve_ownership(writer: Any, job_env: Any, run_id: str,
                      logical_job_id: str,
                      identity_extra: dict | None = None,
                      now: datetime.datetime | None = None,
                      lifecycle_reader: Any = None,
                      predecessor_outcome_reader: Any = None
                      ,
                      is_scratch: bool = False) -> AttemptOwnership:
    """Resolve this process's attempt row. Raises `RecordsError` on failure.

    `writer` is an `observability.attempts.AttemptWriter` over a live
    executor; `job_env` is a `JobEnvironment` from `environment.read_environment`.
    `identity_extra` carries the processing-unit scope (exposure, SCA, sky
    tile) that the manifest supplies — passed through to the resolver so the
    row it creates for a retry is scoped like the row it would have claimed.

    `lifecycle_reader(attempt_id) -> str | None` is how the runtime learns the
    lifecycle state the resolver left the row in. Injected rather than queried
    here because this module has no SQL of its own — every statement in the
    ownership path belongs to W1's writer or to the caller's executor, so
    there is one place where the attempt tables' SQL lives.

    `predecessor_outcome_reader(logical_job_id, index) -> str | None` answers
    "did a lower-indexed attempt of this same logical job already finish
    successfully", and is injected for the same reason. When it says yes, this
    raises `AttemptAlreadySucceeded` and the entrypoint exits 0 without
    running a stage — see the adoption block below.
    """
    from observability.attempts import AttemptIdentity

    moment = now or datetime.datetime.now(datetime.timezone.utc)
    index = normalize_attempt_index(job_env.attempt_index)
    extra = identity_extra or {}

    identity = AttemptIdentity(
        run_id=run_id,
        logical_job_id=logical_job_id,
        exposure_id=extra.get("exposure_id"),
        sca=extra.get("sca"),
        sky_tile=extra.get("sky_tile"),
    )

    _logger.info(
        "resolving attempt ownership: job=%s index=%s logical_job=%s",
        job_env.scheduler_job_id, index, logical_job_id)

    try:
        attempt_id = writer.resolve_attempt(
            identity,
            created_at=moment,
            submitted_at=moment,
            scheduler_job_id=job_env.scheduler_job_id,
            application_attempt_index=index,
            # A SCRATCH ATTEMPT IS CREATED THROUGH THE WRAPPER (rapid_systems
            # migration 135). `public.resolve_attempt` is invoker-rights and
            # inserts with the caller's own privileges, which
            # `rapid_scratch_pipeline` deliberately does not have; the
            # SECURITY DEFINER wrapper is the only route that works for it,
            # and it refuses any run whose kind is not `scratch`. False --
            # every production caller -- takes the statement unchanged.
            is_scratch=is_scratch,
        )
    except Exception as exc:  # noqa: BLE001 - translated to the records category
        # Any failure here is the records path being unreachable before any
        # work has happened, which the fail-loud posture sends to a nonzero
        # exit: there is no row to record an outcome into.
        raise RecordsError(
            f"could not resolve the attempt row for {logical_job_id} "
            f"(job {job_env.scheduler_job_id}, index {index}): {exc}",
            logical_job_id=logical_job_id,
            scheduler_job_id=job_env.scheduler_job_id,
            attempt_index=index) from exc

    if attempt_id is None:
        raise RecordsError(
            f"the attempt resolver returned no attempt id for "
            f"{logical_job_id} (job {job_env.scheduler_job_id}, index "
            f"{index}); the row cannot be identified and no outcome can be "
            f"recorded against it",
            logical_job_id=logical_job_id,
            scheduler_job_id=job_env.scheduler_job_id)

    if lifecycle_reader is not None:
        _refuse_unusable_state(lifecycle_reader, attempt_id, logical_job_id,
                               job_env)

    if index > 1 and predecessor_outcome_reader is not None:
        # ADOPTION: THE TERMINAL RECORD WAS ALREADY WRITTEN, SO DO NOT REDO
        # THE WORK (the 2026-09-13 13:01 ruling — "retry only when the
        # terminal record was not written").
        #
        # A retry exists because Batch started attempt N > 1. Batch's reason
        # for doing so is its own — an exit code, a reclaim, a host loss — and
        # it is not evidence that the work is unfinished. When a PREDECESSOR
        # row of this same logical job is terminal with `rapid_outcome =
        # 'success'`, the science ran, its products were written and its
        # record published: repeating it would spend a container recomputing
        # an answer that already exists, and would write a second terminal
        # record for one logical job.
        #
        # MEASURED, NOT HYPOTHETICAL. The memory-profile probe run of
        # 2026-09-14 produced exactly this: attempts 55044, 55045 and 55048
        # (units science/669/7, science/675/7, science/693/7) were retries
        # driven by phantom FAILED rows, and each of those three units ALSO
        # carried a genuinely successful attempt. Three real re-executions of
        # completed work. The reconciler fix stops most such retries being
        # started; this stops the ones that are started from redoing anything.
        #
        # Exit 0, not an error: the work IS done, and the correct report to
        # Batch for a child whose logical job succeeded is success.
        adopted = _adopt_if_already_succeeded(
            predecessor_outcome_reader, attempt_id, logical_job_id, index)
        if adopted:
            raise AttemptAlreadySucceeded(attempt_id, logical_job_id, index)

    ownership = AttemptOwnership(
        attempt_id=attempt_id,
        run_id=run_id,
        logical_job_id=logical_job_id,
        scheduler_job_id=job_env.scheduler_job_id,
        attempt_index=index,
        claimed_precreated=(index == 1),
    )
    _logger.info("attempt ownership resolved: %s", ownership)
    return ownership


def _refuse_unusable_state(lifecycle_reader: Any, attempt_id: int,
                           logical_job_id: str, job_env: Any) -> None:
    """Stop if the resolver left the row in a state work cannot proceed from.

    Two states are refused:

    `missing_or_contradictory` — the resolver's reconciler-first branch: no
    logical job existed, so there is no execution binding to copy, and the
    provenance this attempt would eventually have to record cannot be
    completed. The row is flagged for the reconciler; the runtime does not add
    science work on top of an attempt whose submission was never recorded.

    Any terminal state — the row has already been closed, by this attempt's
    predecessor or by the reconciler. Starting work against a closed row would
    produce products no record accounts for.
    """
    from observability.attempts import LifecycleState

    try:
        state = lifecycle_reader(attempt_id)
    except Exception as exc:  # noqa: BLE001 - translated
        raise DBError(
            f"could not read the lifecycle state of attempt {attempt_id}: "
            f"{exc}", attempt_id=attempt_id) from exc

    if state is None:
        raise RecordsError(
            f"attempt {attempt_id} has no lifecycle state; the resolver "
            f"returned an id for a row that cannot be read back",
            attempt_id=attempt_id)

    if state == LifecycleState.MISSING_OR_CONTRADICTORY.value:
        raise RecordsError(
            f"attempt {attempt_id} resolved to a "
            f"{LifecycleState.MISSING_OR_CONTRADICTORY.value} row: Batch "
            f"knows about job {job_env.scheduler_job_id} but no logical job "
            f"{logical_job_id} was ever recorded, so the row carries no "
            f"execution binding and this attempt's provenance could never be "
            f"completed. The row is flagged for the reconciler; this process "
            f"exits without doing work.",
            attempt_id=attempt_id, lifecycle_state=state,
            logical_job_id=logical_job_id)

    terminal = {
        LifecycleState.APPLICATION_CLOSED.value,
        LifecycleState.TERMINAL_AFTER_START.value,
        LifecycleState.TERMINAL_WITHOUT_START.value,
    }
    if state in terminal:
        raise RecordsError(
            f"attempt {attempt_id} is already {state}; work against a closed "
            f"attempt would produce products no record accounts for",
            attempt_id=attempt_id, lifecycle_state=state)


def lifecycle_reader_for(execute: Any) -> Any:
    """Build a `lifecycle_reader` over an executor.

    The one SELECT the ownership path needs, kept here beside its only caller
    and parameterized like everything else. Returns None for an attempt id
    that does not exist, which `_refuse_unusable_state` treats as a hard
    failure rather than an absence.
    """

    def read(attempt_id: int) -> Any:
        rows = execute(
            "SELECT lifecycle_state FROM attempts WHERE attempt_id = %s",
            [attempt_id])
        if not rows:
            return None
        first = rows[0]
        if isinstance(first, (list, tuple)):
            return first[0]
        if isinstance(first, dict):
            return next(iter(first.values()))
        return first

    return read


def _adopt_if_already_succeeded(reader: Any, attempt_id: int,
                                logical_job_id: str, index: int) -> bool:
    """Did a lower-indexed attempt of this logical job already succeed?

    A reader that raises is treated as "do not know", and the retry proceeds.
    That is the conservative direction: failing to adopt costs one redundant
    execution, while adopting on a bad read would report success for work that
    never ran.
    """
    try:
        outcome = reader(logical_job_id, index)
    except Exception:  # noqa: BLE001 - "do not know" is not "no"
        _logger.warning(
            "could not read a predecessor outcome for logical job %s "
            "(attempt %s, index %s); proceeding with the retry rather than "
            "adopting on an unanswered question",
            logical_job_id, attempt_id, index, exc_info=True)
        return False
    if outcome != "success":
        return False
    _logger.info(
        "adopting attempt %s: terminal record already written",
        attempt_id)
    return True


def predecessor_outcome_reader_for(execute: Any) -> Any:
    """Build a `predecessor_outcome_reader` over an executor.

    The one SELECT adoption needs, beside `lifecycle_reader_for` and built the
    same way. Answers with the `rapid_outcome` of the newest TERMINAL
    lower-indexed attempt of the same logical job, or None where there is
    none.

    `lifecycle_state LIKE 'terminal%'` and a non-NULL `rapid_outcome` are both
    required: a row still running has no outcome to adopt, and a row flagged
    `missing_or_contradictory` is precisely the one whose account is in
    dispute — adopting from it would inherit the phantom-FAILED defect's own
    conclusion.
    """

    def read(logical_job_id: str, index: int) -> Any:
        rows = execute(
            "SELECT rapid_outcome FROM attempts"
            " WHERE logical_job_id = %s"
            "   AND rapid_outcome IS NOT NULL"
            "   AND lifecycle_state LIKE 'terminal%%'"
            "   AND COALESCE(application_attempt_index,"
            "                application_claim_index) < %s"
            " ORDER BY COALESCE(application_attempt_index,"
            "                   application_claim_index) DESC,"
            "          attempt_id DESC"
            " LIMIT 1",
            [logical_job_id, index])
        if not rows:
            return None
        first = rows[0]
        if isinstance(first, (list, tuple)):
            return first[0]
        if isinstance(first, dict):
            return next(iter(first.values()))
        return first

    return read
