"""``rapidpipe.foundation.steps``: each second counted once, in the innermost step it was spent in.

Contracts: a nested step's seconds are not its parent's; a step entered
twice sums; a count adds; an open step is charged up to the read; reset
forgets everything.
"""

from __future__ import annotations

import pytest

from rapidpipe.foundation import steps


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A clock the test moves by hand."""
    now = [0.0]
    monkeypatch.setattr(steps.time, "perf_counter", lambda: now[0])
    steps.reset()
    yield now
    steps.reset()


def test_a_nested_step_pauses_its_parent(clock):
    with steps.step("outer"):
        clock[0] += 2
        with steps.step("inner"):
            clock[0] += 5
        clock[0] += 1
    assert steps.metrics() == {"step_outer_s": 3.0, "step_inner_s": 5.0}


def test_a_step_entered_twice_sums_and_counts_add(clock):
    for _ in range(2):
        with steps.step("read"):
            clock[0] += 1.5
        steps.add("s3_read_bytes", 10)
    assert steps.metrics() == {"step_read_s": 3.0, "s3_read_bytes": 20}


def test_an_open_step_is_charged_to_the_read(clock):
    with steps.step("upload"):
        clock[0] += 4
        assert steps.metrics() == {"step_upload_s": 4.0}
        clock[0] += 1
    assert steps.metrics() == {"step_upload_s": 5.0}


def test_reset_forgets_every_step(clock):
    with steps.step("x"):
        clock[0] += 1
    steps.reset()
    assert steps.metrics() == {}
