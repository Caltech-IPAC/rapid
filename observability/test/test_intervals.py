"""
File:    test_intervals.py

Tests for the interval decomposition and timestamp-disagreement comparison.

No database: the decomposition reads record fields, so the tests build record
shapes directly.
"""

import datetime
import types
import unittest

from observability.intervals import (
    ALERT_PUBLISHED,
    Decomposition,
    compare_timestamps,
    decompose,
    stage_bounds,
)

UTC = datetime.timezone.utc


def at(second: float) -> datetime.datetime:
    return datetime.datetime(2026, 8, 4, 12, 0, tzinfo=UTC) + \
        datetime.timedelta(seconds=second)


def stage(name, start_second, duration_ms):
    return types.SimpleNamespace(stage_name=name, started_at=at(start_second),
                                 duration_ms=duration_ms)


def attempt(**overrides):
    base = dict(attempt_id=1, created_at=at(0), submitted_at=at(2),
                started_at=at(10), ended_at=at(40),
                scheduler_created_at=None, scheduler_started_at=None,
                scheduler_stopped_at=None, scheduler_attempt_index=None)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class StageBoundsTests(unittest.TestCase):
    def test_end_uses_duration_not_the_latest_start(self):
        # A long stage that started earlier ends after a short one that started
        # later; taking max(started_at) would report the wrong end.
        stages = [stage("long", 10, 20_000), stage("short", 15, 1_000)]
        first, last = stage_bounds(stages)
        self.assertEqual(first, at(10))
        self.assertEqual(last, at(30))

    def test_no_stages_yields_no_bounds(self):
        self.assertEqual(stage_bounds([]), (None, None))


class DecomposeTests(unittest.TestCase):
    def test_five_intervals_from_one_attempt(self):
        result = decompose(
            attempt(),
            stages=[stage("ingest", 12, 3_000), stage("difference", 15, 20_000)],
            alert_published_at=at(40))
        self.assertEqual(result.submission, 2.0)    # created -> submitted
        self.assertEqual(result.queue, 8.0)         # submitted -> started
        self.assertEqual(result.startup, 2.0)       # started -> first stage
        self.assertEqual(result.execution, 23.0)    # first start -> last end
        self.assertEqual(result.publication, 5.0)   # last end -> published

    def test_total_sums_a_complete_decomposition(self):
        result = decompose(
            attempt(),
            stages=[stage("ingest", 12, 3_000), stage("difference", 15, 20_000)],
            alert_published_at=at(40))
        self.assertEqual(result.total, 40.0)

    def test_missing_endpoint_yields_absent_interval_not_zero(self):
        # An attempt that has not started has no queue interval. Zero would
        # plot as an instantaneous queue, which is a lie.
        result = decompose(attempt(started_at=None), stages=[])
        self.assertIsNone(result.queue)
        self.assertIsNone(result.startup)

    def test_total_is_absent_when_any_interval_is(self):
        result = decompose(attempt(), stages=[], alert_published_at=None)
        self.assertIsNone(result.total)

    def test_unpublished_attempt_has_no_publication_interval(self):
        result = decompose(attempt(), stages=[stage("ingest", 12, 3_000)],
                           alert_published_at=None)
        self.assertIsNone(result.publication)
        self.assertIsNotNone(result.execution)

    def test_first_scheduler_attempt_queue_is_comparable(self):
        result = decompose(attempt(scheduler_attempt_index=0), stages=[])
        self.assertFalse(result.queue_interval_is_bounded_only)

    def test_scheduler_retry_marks_its_queue_interval_as_bounded_only(self):
        # Batch exposes no per-attempt creation time, so a retry's queue
        # interval fuses requeue+queue+startup. The flag says so rather than
        # letting the number pass as comparable.
        result = decompose(attempt(scheduler_attempt_index=1), stages=[])
        self.assertTrue(result.queue_interval_is_bounded_only)


class CompareTimestampsTests(unittest.TestCase):
    def test_agreement_within_tolerance_is_not_reported(self):
        row = attempt(started_at=at(10), scheduler_started_at=at(11))
        self.assertEqual(compare_timestamps(row, tolerance_seconds=5.0), [])

    def test_disagreement_beyond_tolerance_is_reported(self):
        row = attempt(started_at=at(10), scheduler_started_at=at(40))
        findings = compare_timestamps(row, tolerance_seconds=5.0)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].field, "started_at")
        self.assertEqual(findings[0].delta_seconds, 30.0)

    def test_comparison_never_overwrites_either_value(self):
        # Both values survive the comparison — the design's rule is that
        # disagreement is preserved, not resolved.
        row = attempt(started_at=at(10), scheduler_started_at=at(40))
        compare_timestamps(row, tolerance_seconds=5.0)
        self.assertEqual(row.started_at, at(10))
        self.assertEqual(row.scheduler_started_at, at(40))

    def test_one_sided_observation_is_not_a_disagreement(self):
        row = attempt(started_at=at(10), scheduler_started_at=None)
        self.assertEqual(compare_timestamps(row, tolerance_seconds=0.0), [])

    def test_all_three_pairs_are_compared(self):
        row = attempt(submitted_at=at(2), scheduler_created_at=at(100),
                      started_at=at(10), scheduler_started_at=at(200),
                      ended_at=at(40), scheduler_stopped_at=at(300))
        findings = compare_timestamps(row, tolerance_seconds=1.0)
        self.assertEqual({f.field for f in findings},
                         {"submitted_at", "started_at", "ended_at"})


if __name__ == "__main__":
    unittest.main()
