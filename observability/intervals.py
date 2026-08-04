"""
File:    intervals.py

SCA-to-alert latency, decomposed.

design/observability.md § Attempt record: latency "decomposes into submission,
queue, startup, execution, and publication intervals, each authored by exactly
one source ... and assembled by joining the records — no per-interval bespoke
convention."

That sentence is the whole design of this module. Each interval is a difference
between two timestamps that already exist in the record, each written by exactly
one party:

    submission   created_at            -> submitted_at            application
    queue        submitted_at          -> started_at              application/scheduler
    startup      started_at            -> first stage started_at  scheduler/application
    execution    first stage start     -> last stage end          attempt_stages
    publication  last stage end        -> alert_published         milestones

Nothing here invents a timestamp, and nothing averages two sources. Where the
application and the scheduler both observed the same event, they are in separate
columns and `compare_timestamps` reports the disagreement — flagged, per the
policy, never silently resolved.

The stated limitation, carried in code rather than left to folklore: the full
five-interval decomposition is defined for a job's FIRST scheduler attempt.
Batch's `AttemptDetail` carries start/stop only — there is no per-attempt
creation or requeue time — so for a scheduler-level retry the queue interval is
bounded only as prior-attempt-stop to this-attempt-start, which fuses requeue,
queue and startup. `decompose` says so in its result rather than returning a
number that looks like the others but is not comparable to them.
"""

import dataclasses
import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# The milestone that ends the publication interval.
ALERT_PUBLISHED = "alert_published"

# Interval names, in pipeline order.
SUBMISSION = "submission"
QUEUE = "queue"
STARTUP = "startup"
EXECUTION = "execution"
PUBLICATION = "publication"

INTERVAL_ORDER = (SUBMISSION, QUEUE, STARTUP, EXECUTION, PUBLICATION)


@dataclasses.dataclass(frozen=True)
class Decomposition:
    """The five intervals for one attempt, in seconds.

    An interval is `None` when the record does not yet carry both of its
    endpoints — absent, not zero. A caller charting latency skips a `None`; it
    must never be plotted as a fast interval.
    """

    attempt_id: int
    submission: float | None = None
    queue: float | None = None
    startup: float | None = None
    execution: float | None = None
    publication: float | None = None
    #: True when this row is a scheduler-level retry, whose `queue` fuses
    #: requeue + queue + startup and is therefore not comparable with a first
    #: attempt's queue interval.
    queue_interval_is_bounded_only: bool = False

    @property
    def total(self) -> float | None:
        """End-to-end latency, or None if any interval is missing.

        Deliberately strict: summing the intervals that happen to be present
        would report a total that silently omits a phase.
        """
        parts = [self.submission, self.queue, self.startup, self.execution,
                 self.publication]
        if any(part is None for part in parts):
            return None
        return sum(parts)

    def as_dict(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in INTERVAL_ORDER}


def _seconds(start: Any, end: Any) -> float | None:
    """Elapsed seconds between two wall clocks, or None if either is absent."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def stage_bounds(stages: Sequence[Any]) -> tuple[Any | None, Any | None]:
    """First stage start and last stage end across an attempt's stages.

    The end is `started_at + duration_ms`, not the largest `started_at`: the
    monotonic duration is authoritative for elapsed time, and the longest stage
    is not necessarily the one that started last.
    """
    if not stages:
        return None, None
    import datetime

    first_start = min(stage.started_at for stage in stages)
    last_end = max(
        stage.started_at + datetime.timedelta(milliseconds=float(stage.duration_ms))
        for stage in stages)
    return first_start, last_end


def decompose(attempt: Any, stages: Sequence[Any] = (),
              alert_published_at: Any = None) -> Decomposition:
    """Assemble the interval decomposition for one attempt.

    Parameters
    ----------
    attempt : object
        Anything exposing the `attempts` columns as attributes — a row object, a
        dataclass, a namespace. Read-only here.
    stages : sequence
        That attempt's `attempt_stages` rows (`started_at`, `duration_ms`).
    alert_published_at : optional
        `reached_at` of the attempt's `alert_published` milestone, if reached.
    """
    first_stage_start, last_stage_end = stage_bounds(stages)
    is_retry = bool(getattr(attempt, "scheduler_attempt_index", None))

    return Decomposition(
        attempt_id=attempt.attempt_id,
        submission=_seconds(attempt.created_at, attempt.submitted_at),
        queue=_seconds(attempt.submitted_at, attempt.started_at),
        startup=_seconds(attempt.started_at, first_stage_start),
        execution=_seconds(first_stage_start, last_stage_end),
        publication=_seconds(last_stage_end, alert_published_at),
        queue_interval_is_bounded_only=is_retry,
    )


@dataclasses.dataclass(frozen=True)
class TimestampDisagreement:
    """One application-vs-scheduler timestamp pair that differs beyond tolerance."""

    attempt_id: int
    field: str
    application_value: Any
    scheduler_value: Any
    delta_seconds: float


#: Pairs of (application column, scheduler column) that observe the same event.
COMPARABLE_PAIRS = (
    ("submitted_at", "scheduler_created_at"),
    ("started_at", "scheduler_started_at"),
    ("ended_at", "scheduler_stopped_at"),
)


def compare_timestamps(attempt: Any, tolerance_seconds: float,
                       ) -> list[TimestampDisagreement]:
    """Report application/scheduler timestamp pairs that disagree beyond tolerance.

    Returns findings; it does not write, and it never picks a winner. The
    tolerance is an open parameter of the ratified design — the caller supplies
    it, this module does not assume one.

    Note the `submitted_at` / `scheduler_created_at` pair: for an array child the
    scheduler value is child-spawn time, so a normal delay appears here as a real
    difference. A tolerance for that pair must absorb spawn delay or it will
    report every array child.
    """
    findings: list[TimestampDisagreement] = []
    for app_field, sched_field in COMPARABLE_PAIRS:
        app_value = getattr(attempt, app_field, None)
        sched_value = getattr(attempt, sched_field, None)
        if app_value is None or sched_value is None:
            # Only one party observed it — nothing to disagree about.
            continue
        delta = abs((sched_value - app_value).total_seconds())
        if delta > tolerance_seconds:
            findings.append(TimestampDisagreement(
                attempt_id=attempt.attempt_id, field=app_field,
                application_value=app_value, scheduler_value=sched_value,
                delta_seconds=delta))
    if findings:
        logger.warning("attempt %s: %d timestamp disagreements beyond %ss",
                       attempt.attempt_id, len(findings), tolerance_seconds)
    return findings
