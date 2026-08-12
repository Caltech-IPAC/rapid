"""Batching tests: the cadence policy, on a fake clock.

The cadence is "proportional to arrival rate rather than exposure count"
(design/compute.md § Submission). These tests drive both triggers — a
fast arrival that fills a batch, and a slow drip that ages one out — and
assert the properties that make the policy safe: no unit is lost, no unit
is duplicated, and no batch exceeds the array ceiling.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission import payloads
from submission.batching import (ReadyWorkAccumulator, batch_units)
from submission.manifest import MAX_ARRAY_SIZE, ProcessingUnit
from submission.routes import JOB_TYPE_CROSSMATCH, JOB_TYPE_SCIENCE
from submission.test import payload_fixtures as fixtures


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def unit(exposure, sca):
    return ProcessingUnit(
        payload=fixtures.science_payload(exposure=exposure, sca=sca))


def units(count, exposure=90210):
    return [unit(exposure, i + 1) for i in range(count)]


@pytest.fixture
def clock():
    return FakeClock()


def make_accumulator(clock, **kwargs):
    kwargs.setdefault("max_batch_size", 10)
    kwargs.setdefault("max_wait_seconds", 60.0)
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"batch-{counter['n']}"

    return ReadyWorkAccumulator(clock=clock, batch_id_factory=next_id, **kwargs)


# ---------------------------------------------------------------------------
# The size trigger: a fast arrival rate fills batches
# ---------------------------------------------------------------------------

def test_no_batch_before_either_trigger(clock):
    acc = make_accumulator(clock)
    acc.extend(units(3))
    assert acc.should_cut() is False
    assert acc.cut() is None


def test_batch_cut_when_full(clock):
    acc = make_accumulator(clock)
    acc.extend(units(10))
    batch = acc.cut()
    assert batch is not None
    assert len(batch) == 10 and batch.reason == "size"
    assert len(acc) == 0


def test_a_full_batch_never_exceeds_max_batch_size(clock):
    acc = make_accumulator(clock, max_batch_size=10)
    acc.extend(units(25))
    batch = acc.cut()
    assert len(batch) == 10
    assert len(acc) == 15          # remainder stays waiting


def test_batching_is_not_by_exposure(clock):
    # 18 SCAs per exposure; a batch of 10 splits one exposure and a batch
    # boundary falls mid-exposure. That is the point of the design rule.
    acc = make_accumulator(clock, max_batch_size=10)
    acc.extend(units(18, exposure=1))
    first = acc.cut()
    assert [u.sca for u in first.manifest] == list(range(1, 11))
    acc.extend(units(18, exposure=2))
    second = acc.cut()
    assert {u.exposure for u in second.manifest} == {1, 2}


# ---------------------------------------------------------------------------
# The age trigger: a slow drip still moves
# ---------------------------------------------------------------------------

def test_batch_cut_when_the_oldest_unit_ages_out(clock):
    acc = make_accumulator(clock, max_wait_seconds=60.0)
    acc.extend(units(2))
    clock.advance(59.0)
    assert acc.should_cut() is False
    clock.advance(2.0)
    batch = acc.cut()
    assert len(batch) == 2 and batch.reason == "age"


def test_the_remainders_wait_restarts_after_a_cut(clock):
    # Otherwise a slow drip after a big batch cuts single-unit batches
    # immediately, one submission per unit.
    acc = make_accumulator(clock, max_batch_size=10, max_wait_seconds=60.0)
    acc.extend(units(15))
    clock.advance(90.0)
    acc.cut()                      # size-triggered, 10 units
    assert len(acc) == 5
    assert acc.should_cut() is False        # the leftover 5 are not stale
    clock.advance(61.0)
    assert acc.should_cut() is True


def test_waiting_seconds_is_zero_when_empty(clock):
    assert make_accumulator(clock).waiting_seconds == 0.0


# ---------------------------------------------------------------------------
# Dedup and drain
# ---------------------------------------------------------------------------

def test_a_unit_already_waiting_is_dropped(clock):
    # Overlapping ready-work polls re-return rows; a duplicate would put
    # one SCA under two array indices.
    acc = make_accumulator(clock)
    acc.add(unit(1, 1))
    acc.add(unit(1, 1))
    assert len(acc) == 1


def test_two_fields_crossmatch_units_do_not_collide_v25(clock):
    # THE V25 DEFECT (co-design ruling 2). Crossmatch gathering used to
    # yield `ProcessingUnit(exposure=<date ordinal>, sca=0, fields={...})`
    # for EVERY field of one processing date — so every field shared one
    # `.key`, and deduping on `.key` (as this accumulator did before the
    # fix) silently dropped every field after the first. Now a crossmatch
    # unit carries a `CrossmatchPayload(proc_date=..., field=...)` with no
    # exposure/SCA sentinel at all, and dedup keys on the declared subject
    # (job type, proc_date, field). Two different fields of the same date
    # must both survive.
    acc = make_accumulator(clock, job_type=JOB_TYPE_CROSSMATCH)
    proc_date = "20260808"

    def crossmatch_unit(field):
        return ProcessingUnit(payload=payloads.build(
            JOB_TYPE_CROSSMATCH, proc_date=proc_date, field=field,
            target_tables=("catalog",)))

    acc.add(crossmatch_unit(101))
    acc.add(crossmatch_unit(202))
    assert len(acc) == 2

    # And a genuine re-offer of the SAME field still dedups, exactly as
    # before this ruling — the fix narrows what counts as "the same unit",
    # it does not turn dedup off.
    acc.add(crossmatch_unit(101))
    assert len(acc) == 2


def test_a_unit_can_return_after_its_batch_is_cut(clock):
    # A genuine reprocess is legitimate new work.
    acc = make_accumulator(clock, max_batch_size=1)
    acc.add(unit(1, 1))
    acc.cut()
    acc.add(unit(1, 1))
    assert len(acc) == 1


def test_force_cuts_a_batch_below_both_triggers(clock):
    acc = make_accumulator(clock)
    acc.extend(units(3))
    batch = acc.cut(force=True)
    assert len(batch) == 3 and batch.reason == "forced"


def test_drain_empties_the_accumulator(clock):
    acc = make_accumulator(clock, max_batch_size=10)
    acc.extend(units(25))
    batches = list(acc.drain())
    assert [len(b) for b in batches] == [10, 10, 5]
    assert len(acc) == 0


def test_drain_loses_no_units_and_duplicates_none(clock):
    acc = make_accumulator(clock, max_batch_size=7)
    original = units(30)
    acc.extend(original)
    drained = [u for batch in acc.drain() for u in batch.manifest]
    assert drained == original


def test_each_batch_gets_its_own_id(clock):
    acc = make_accumulator(clock, max_batch_size=5)
    acc.extend(units(15))
    ids = [b.manifest.batch_id for b in acc.drain()]
    assert len(set(ids)) == 3


# ---------------------------------------------------------------------------
# The one-pass helper, and the ceiling
# ---------------------------------------------------------------------------

def test_batch_units_splits_a_known_work_list():
    batches = batch_units(units(12), max_batch_size=5)
    assert [len(b) for b in batches] == [5, 5, 2]


def test_a_backlog_over_the_ceiling_becomes_several_submissions():
    batches = batch_units(units(MAX_ARRAY_SIZE + 500),
                          max_batch_size=MAX_ARRAY_SIZE)
    assert [len(b) for b in batches] == [MAX_ARRAY_SIZE, 500]
    assert all(len(b) <= MAX_ARRAY_SIZE for b in batches)


def test_max_batch_size_cannot_exceed_the_array_ceiling():
    with pytest.raises(ValueError, match="ceiling"):
        ReadyWorkAccumulator(max_batch_size=MAX_ARRAY_SIZE + 1)


def test_degenerate_cadence_settings_are_rejected():
    with pytest.raises(ValueError):
        ReadyWorkAccumulator(max_batch_size=0)
    with pytest.raises(ValueError):
        ReadyWorkAccumulator(max_wait_seconds=0)
