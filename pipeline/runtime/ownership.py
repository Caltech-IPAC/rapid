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
"""

import dataclasses
import datetime
from typing import Any

from pipeline.runtime.errors import DBError, RecordsError
from pipeline.runtime.logging_setup import get_logger

_logger = get_logger("ownership")


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
                      lifecycle_reader: Any = None) -> AttemptOwnership:
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
