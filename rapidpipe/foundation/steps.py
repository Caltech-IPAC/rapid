"""A job's seconds by named step, each second counted once, for ``job_metrics`` (throughput-metrics).

A stage wraps a piece of its work in :func:`step`; :func:`metrics` gives
``step_<name>_s`` for every step the job entered, the wall seconds spent in
it and in no step nested inside it, so the steps' sum never exceeds the
job's own seconds and nothing is counted twice (entering ``s3_read`` inside
``read_inputs`` pauses ``read_inputs`` until it returns). :func:`add`
counts a quantity beside them (``s3_read_bytes``). The stage's own older
timers (``rb_s``, ``xsources_s``, the packet's ``packet_*_s``) stay as
they were; these sit beside them.

What no step covers is the job's remainder: ``container_seconds`` less the
steps' sum, which the throughput report states. ``run_job``
(:mod:`rapidpipe.foundation.local`) resets the record at a job's start and
writes it into the manifest's ``metrics``. One job runs per process, so the
record is this module's own state; a thread other than the stage's must not
enter a step.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

_totals: dict[str, float] = {}
_stack: list[list] = []          # [name, the moment it last resumed]


def reset() -> None:
    """Forget every step and count: a new job starts."""
    _totals.clear()
    _stack.clear()


def _charge(entry: list, now: float) -> None:
    name = f"step_{entry[0]}_s"
    _totals[name] = _totals.get(name, 0.0) + now - entry[1]
    entry[1] = now


@contextmanager
def step(name: str) -> Iterator[None]:
    """Count the wall seconds inside the block, less any nested step's, as ``step_<name>_s``."""
    now = time.perf_counter()
    if _stack:
        _charge(_stack[-1], now)
    entry = [name, now]
    _stack.append(entry)
    try:
        yield
    finally:
        now = time.perf_counter()
        _charge(entry, now)
        _stack.remove(entry)
        if _stack:
            _stack[-1][1] = now


def add(name: str, value: float) -> None:
    """Add ``value`` to the count ``name`` (``s3_read_bytes``, ``s3_write_bytes``)."""
    _totals[name] = _totals.get(name, 0.0) + value


def metrics() -> dict[str, float]:
    """Every step's seconds (3 decimals) and every count so far, the open step charged to now."""
    if _stack:
        _charge(_stack[-1], time.perf_counter())
    return {k: round(v, 3) if k.endswith("_s") else v for k, v in _totals.items()}
