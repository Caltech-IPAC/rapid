"""Reading the scheduler, in batches, and deriving what it does not tell us.

Two things this module exists to get right.

**Batched describes.** The VPO polls `describe_jobs` one job per call, which is
one API call per in-flight job per poll. Batch accepts 100 identifiers per
call; the open set is chunked accordingly.

**The attempt ordinal Batch does not expose.** `describe_jobs` returns an
`attempts` list per job, and the elements carry no ordinal. The scheduler
numbers attempts from 1, so the index is derivable from start-time ordering —
but that derivation is an inference about an API's behaviour, not a documented
contract, which is why the design flagged it for live probing before anything
relies on it. `derive_attempt_indices` is that derivation, isolated in one
tested function so the probe has a single thing to confirm and a single thing
to change if the API ever grows a real ordinal.
"""

import dataclasses
import datetime
import logging
from typing import Any

logger = logging.getLogger("rapid.reconciler.scheduler")

# Batch's documented ceiling for describe_jobs.
DESCRIBE_CHUNK = 100

# The scheduler's own terminal states.
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED"})

# Everything the scheduler can report. A state outside this set is a scheduler
# change we have not accounted for, and is surfaced rather than defaulted.
KNOWN_STATES = frozenset({
    "SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING",
    "SUCCEEDED", "FAILED",
})

# Status-reason fragments that mean the attempt never ran, mapped to the
# reconciler-authored category that says so. Order matters: the first match
# wins, so the more specific reclaim reasons precede the generic pull failures.
_PROVISIONING_MARKERS = (
    "CannotPullContainerError",
    "CannotCreateContainerError",
    "CannotInspectContainerError",
    "ResourceInitializationError",
)
_RECLAIM_MARKERS = (
    "Host EC2",
    "Spot instance termination",
    "spot interruption",
)


@dataclasses.dataclass(frozen=True)
class DescribeBatch:
    """One chunk of identifiers and the jobs the scheduler returned for it.

    `missing` are identifiers the scheduler did not return at all — Batch drops
    unknown ids silently rather than erroring, so the difference is computed
    and carried rather than assumed empty.
    """

    requested: tuple
    jobs: tuple
    missing: tuple


@dataclasses.dataclass(frozen=True)
class SchedulerObservation:
    """What the scheduler says about one attempt. Reconciler-authored, all of it."""

    scheduler_job_id: str
    state: str
    created_at: Any = None
    started_at: Any = None
    stopped_at: Any = None
    exit_code: Any = None
    status_reason: str | None = None
    attempt_index: int | None = None
    container_reason: str | None = None
    log_stream: str | None = None
    job_definition: str | None = None

    @property
    def is_terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def never_ran(self):
        """Did this attempt fail before the application could ever execute?

        The discriminator is the absent start time, not the state and not the
        exit code. Two reasons it cannot be the state:

        - `state` is the *job's* status. A job that was retried and eventually
          succeeded reports SUCCEEDED while its first attempt never started at
          all — reading the job's state per-attempt would hide exactly the
          retry the reconciler exists to record.
        - A container that never ran has no exit code, so absence there is
          ambiguous with a killed process whose code was never collected.

        An attempt that is merely still running also has no stop time, so it is
        not yet anything; `is_terminal` gates the callers that care.
        """
        if self.started_at is not None:
            return False
        if self.attempt_index is not None:
            # Scoped to a real attempt from the history: no start means it
            # never ran, whatever the job as a whole went on to do.
            return True
        return self.state == "FAILED"

    def reconciler_category(self):
        """The reconciler-authored category for an attempt that never ran.

        None where the application ran and is responsible for its own
        classification — this function never invents a category for an attempt
        that had the chance to author one.
        """
        if not self.never_ran:
            return None
        haystack = " ".join(
            part for part in (self.status_reason, self.container_reason) if part)
        for marker in _RECLAIM_MARKERS:
            if marker.lower() in haystack.lower():
                return "scheduler_reclaimed"
        for marker in _PROVISIONING_MARKERS:
            if marker.lower() in haystack.lower():
                return "scheduler_provisioning"
        # It never started and the scheduler did not say why in terms we
        # recognise. Provisioning is the honest default: the attempt did not
        # get as far as running, which is precisely what that category means.
        return "scheduler_provisioning"


def _millis_to_datetime(value):
    """Batch reports epoch milliseconds; the schema stores timestamptz."""
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value / 1000.0,
                                           tz=datetime.timezone.utc)


def describe_in_batches(client, job_ids, chunk=DESCRIBE_CHUNK):
    """Describe an arbitrary number of jobs, `chunk` at a time.

    Yields a `DescribeBatch` per call so a caller can act on each chunk as it
    arrives rather than accumulating the whole open set in memory.
    """
    identifiers = [job_id for job_id in job_ids if job_id]
    for start in range(0, len(identifiers), chunk):
        window = identifiers[start:start + chunk]
        response = client.describe_jobs(jobs=window)
        jobs = tuple(response.get("jobs", ()))
        returned = {job.get("jobId") for job in jobs}
        missing = tuple(i for i in window if i not in returned)
        if missing:
            logger.warning("describe_jobs did not return %d of %d requested ids",
                           len(missing), len(window))
        yield DescribeBatch(requested=tuple(window), jobs=jobs, missing=missing)


def derive_attempt_indices(attempts):
    """Number a job's attempt history one-based, in start-time order.

    THE FLAGGED API DEPENDENCY. Batch's attempt history carries no ordinal, so
    the index is inferred from ordering. Two properties make the inference
    safe, and both are asserted by the tests:

    - Attempts of one job cannot overlap in time — the scheduler starts a retry
      only after the prior attempt has stopped — so start-time order is a total
      order on a real history.
    - An attempt that never started has no `startedAt`. Those sort *after* the
      started ones rather than being dropped, because a never-started attempt
      still consumed an ordinal from the scheduler's point of view, and the
      whole point of numbering is to agree with the scheduler.

    Ties (equal or absent start times) keep the scheduler's own list order,
    which `sorted` guarantees by being stable. Returns a list of
    (index, attempt) pairs, index from 1.
    """
    if not attempts:
        return []

    def sort_key(item):
        position, attempt = item
        started = attempt.get("startedAt")
        # Absent start times sort last, keeping list order among themselves.
        return (started is None, started if started is not None else 0, position)

    ordered = sorted(enumerate(attempts), key=sort_key)
    return [(index, attempt) for index, (_, attempt) in enumerate(ordered, start=1)]


def observation_from_job(job, attempt_index=None, attempt=None):
    """Build the observation for one job, optionally scoped to one attempt.

    Where `attempt` is supplied, the per-attempt facts (its own start/stop, its
    container's exit code and reason) come from it and the job supplies only
    what is job-scoped. Where it is not, the job's own top-level view is used —
    correct for a job with a single attempt, which is the common case.
    """
    state = job.get("status")
    if state not in KNOWN_STATES:
        logger.warning("unknown scheduler state %r for job %s",
                       state, job.get("jobId"))

    container = (attempt or job).get("container", {}) or {}
    exit_code = container.get("exitCode")

    if attempt is not None:
        started_at = _millis_to_datetime(attempt.get("startedAt"))
        stopped_at = _millis_to_datetime(attempt.get("stoppedAt"))
        status_reason = attempt.get("statusReason") or job.get("statusReason")
    else:
        started_at = _millis_to_datetime(job.get("startedAt"))
        stopped_at = _millis_to_datetime(job.get("stoppedAt"))
        status_reason = job.get("statusReason")

    return SchedulerObservation(
        scheduler_job_id=job.get("jobId"),
        state=state,
        created_at=_millis_to_datetime(job.get("createdAt")),
        started_at=started_at,
        stopped_at=stopped_at,
        exit_code=exit_code,
        status_reason=status_reason,
        attempt_index=attempt_index,
        container_reason=container.get("reason"),
        log_stream=container.get("logStreamName"),
        job_definition=job.get("jobDefinition"),
    )


def observations_for_job(job):
    """Every attempt of one job as its own observation, correctly numbered.

    A job with a retry history produces one observation per attempt — they are
    separate attempt rows in the schema, and conflating them would lose the
    retry. A job with no history produces one observation from the job itself.
    """
    attempts = job.get("attempts") or []
    if not attempts:
        return [observation_from_job(job)]
    return [observation_from_job(job, attempt_index=index, attempt=attempt)
            for index, attempt in derive_attempt_indices(attempts)]
