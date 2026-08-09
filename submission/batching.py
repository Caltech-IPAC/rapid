"""
File:    batching.py

Cutting ready work into submittable batches.

design/compute.md § Submission sets the shape: "The submission unit is
the array job: one submission call per ready batch of work, each array
child one per-SCA processing unit, up to the 10,000-child ceiling. One
call is one throttle unit regardless of array size, which is what clears
the scheduler's fixed 50-per-second submission ceiling at 1,000-job
concurrency. Batching cadence — how much ready work accumulates per
submission — is owned by the orchestrator implementation, proportional to
arrival rate rather than exposure count."

Two things that sentence rules out, and this module therefore does not do:

It does not batch by exposure. An exposure is a fixed 18 SCAs; batching
on it would make submission rate track the exposure arrival rate, which
is exactly the coupling "proportional to arrival rate rather than
exposure count" forbids. Units enter the accumulator individually and a
batch may span exposures or split one.

It does not submit. `ReadyWorkAccumulator` decides *when* a batch is
ready and *what* is in it; submit.py turns that decision into an API
call. Keeping the two apart is what lets the cadence policy be unit-tested
against a clock with no AWS involved.

The cadence itself is deliberately conservative and parameterized: cut a
batch when enough work has accumulated (`max_batch_size`), or when the
oldest waiting unit has waited long enough (`max_wait_seconds`), whichever
comes first. Under a fast arrival rate the size trigger dominates and
submissions stay well under the throttle ceiling; under a slow drip the
age trigger bounds latency. Both values are operating parameters, set
from the pipeline parameter tree, not constants of the design.
"""

import dataclasses
import logging
from typing import Callable, Iterable, Iterator

from .manifest import MAX_ARRAY_SIZE, Manifest, ProcessingUnit
from .routes import JOB_TYPE_SCIENCE, route_for

logger = logging.getLogger(__name__)

# Defaults are starting points for the smoke run, not tuned values: the
# smoke run's measured arrival rate is what sets them for real. Chosen
# conservative — a batch well under the array ceiling, and a wait short
# enough that a slow drip still moves.
DEFAULT_MAX_BATCH_SIZE = 500
DEFAULT_MAX_WAIT_SECONDS = 60.0


@dataclasses.dataclass(frozen=True)
class Batch:
    """One submission's worth of work: a manifest plus why it was cut.

    `reason` is carried because the cadence is an operating parameter
    under active tuning — knowing whether batches are cut full or cut
    stale is the evidence that sets `max_batch_size` after the smoke run.
    """

    manifest: Manifest
    reason: str

    def __len__(self) -> int:
        return len(self.manifest)


class ReadyWorkAccumulator:
    """Accumulates ready processing units and cuts them into batches.

    The clock is injected so the age trigger is testable without waiting:
    tests pass a fake clock and step it.

    Parameters
    ----------
    max_batch_size : int, optional
        Cut a batch once this many units are waiting. Capped at Batch's
        array ceiling.
    max_wait_seconds : float, optional
        Cut a batch once the oldest waiting unit has waited this long,
        even if the batch is small.
    clock : callable, optional
        Returns monotonic seconds. Defaults to ``time.monotonic``.
    batch_id_factory : callable, optional
        Returns a unique id per cut batch, stamped into the manifest.
    job_type : str, optional
        What kind of work this accumulator batches. One accumulator holds
        one job type: the type fixes the route (class, queue, database
        lane), so units of two types cannot share an array job. Defaults
        to science, the only type the orchestrator batches today.
    """

    def __init__(self, max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
                 max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
                 clock: Callable[[], float] | None = None,
                 batch_id_factory: Callable[[], str] | None = None,
                 job_type: str = JOB_TYPE_SCIENCE):
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")
        if max_batch_size > MAX_ARRAY_SIZE:
            raise ValueError(
                f"max_batch_size {max_batch_size} exceeds Batch's "
                f"{MAX_ARRAY_SIZE}-child array ceiling")
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")

        self.max_batch_size = max_batch_size
        self.max_wait_seconds = max_wait_seconds
        # Validated here rather than at the first cut: an accumulator
        # built with an unknown job type should fail when it is built,
        # not an hour later when a batch is ready.
        self.job_type = route_for(job_type).job_type

        if clock is None:
            import time
            clock = time.monotonic
        self._clock = clock

        if batch_id_factory is None:
            import uuid
            batch_id_factory = lambda: uuid.uuid4().hex  # noqa: E731
        self._batch_id_factory = batch_id_factory

        self._waiting: list[ProcessingUnit] = []
        self._pending_keys: set[tuple] = set()
        self._oldest_at: float | None = None

    # -- accumulation ------------------------------------------------

    def add(self, unit: ProcessingUnit) -> None:
        """Offer one ready unit to the accumulator.

        A unit already waiting is dropped: ready-work queries overlap in
        practice (a poll re-reads rows a prior poll already returned), and
        a duplicate would put the same SCA under two array indices. Dedup
        is only within the waiting set — once a batch is cut, its keys are
        released, because a genuine reprocess of the same SCA is legitimate
        work that arrives as a new unit.

        **DEDUPS ON THE DECLARED SUBJECT, NOT `.key`** (co-design ruling 2,
        the V25 defect). `.key` is the exposure/SCA-shaped storage carrier
        every `ProcessingUnit` has, whatever its job type — a crossmatch
        unit puts the processing-date ordinal in `exposure` and a fixed `0`
        in `sca`, so every field of one date shared `.key` and every field
        after the first was silently dropped here. `unit.dedup_key(self.job_type)`
        (`submission.manifest.ProcessingUnit.dedup_key`, backed by
        `submission.subjects`) reads the real identity instead, so two
        units collide only when their declared subjects agree. Every
        accumulator holds one job type (this class's own contract, stated
        above), so `self.job_type` is always the right type to key with.
        """
        dedup_key = unit.dedup_key(self.job_type)
        if dedup_key in self._pending_keys:
            logger.debug("unit %s already waiting; dropped", unit.key)
            return
        self._waiting.append(unit)
        self._pending_keys.add(dedup_key)
        if self._oldest_at is None:
            self._oldest_at = self._clock()

    def extend(self, units: Iterable[ProcessingUnit]) -> None:
        for unit in units:
            self.add(unit)

    # -- state -------------------------------------------------------

    def __len__(self) -> int:
        return len(self._waiting)

    @property
    def waiting_seconds(self) -> float:
        """How long the oldest waiting unit has waited. 0.0 when empty."""
        if self._oldest_at is None:
            return 0.0
        return self._clock() - self._oldest_at

    def should_cut(self) -> bool:
        """Whether a batch is ready to submit under the cadence policy."""
        if not self._waiting:
            return False
        if len(self._waiting) >= self.max_batch_size:
            return True
        return self.waiting_seconds >= self.max_wait_seconds

    # -- cutting -----------------------------------------------------

    def cut(self, force: bool = False) -> Batch | None:
        """Cut the next batch, or return None if none is ready.

        Parameters
        ----------
        force : bool, optional
            Cut whatever is waiting regardless of the cadence triggers.
            For orchestrator shutdown: work already accumulated should go
            out rather than be dropped.

        Returns
        -------
        Batch or None
        """
        if not self._waiting:
            return None

        full = len(self._waiting) >= self.max_batch_size
        stale = self.waiting_seconds >= self.max_wait_seconds
        if not (full or stale or force):
            return None

        take = min(len(self._waiting), self.max_batch_size)
        units = self._waiting[:take]
        self._waiting = self._waiting[take:]
        for unit in units:
            self._pending_keys.discard(unit.dedup_key(self.job_type))

        # The remainder's wait restarts now: it was not the thing that
        # aged out, and charging it the old batch's age would cut the
        # next batch immediately at size 1 under a slow drip.
        self._oldest_at = self._clock() if self._waiting else None

        reason = "size" if full else ("age" if stale else "forced")
        manifest = Manifest(units, batch_id=self._batch_id_factory(),
                            job_type=self.job_type)
        logger.info("cut batch %s: %d units (%s)",
                    manifest.batch_id, len(units), reason)
        return Batch(manifest=manifest, reason=reason)

    def drain(self) -> Iterator[Batch]:
        """Cut every waiting unit into batches, ceiling-sized.

        For shutdown and for backlog: a backlog larger than the array
        ceiling has to become several submissions, and this is where that
        happens rather than in a caller's loop.
        """
        while self._waiting:
            batch = self.cut(force=True)
            if batch is None:      # unreachable while _waiting is non-empty
                break
            yield batch


def batch_units(units: Iterable[ProcessingUnit],
                max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
                batch_id_factory: Callable[[], str] | None = None
                ) -> list[Batch]:
    """Cut a known, finite work list into batches in one pass.

    The bulk/backfill counterpart to the accumulator's streaming path:
    when all the work is already known there is no cadence question, only
    the array ceiling.
    """
    accumulator = ReadyWorkAccumulator(max_batch_size=max_batch_size,
                                       batch_id_factory=batch_id_factory)
    accumulator.extend(units)
    return list(accumulator.drain())
