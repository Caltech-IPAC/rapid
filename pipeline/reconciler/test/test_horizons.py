"""The two horizons — and the fact that they are anchored to different clocks."""

import unittest

from pipeline.reconciler import horizons
from pipeline.reconciler.test.stubs import utc


class GraceHorizonTests(unittest.TestCase):
    def test_inside_the_grace_period_the_answer_is_no(self):
        # The job may be writing its bundle and record right now. Classifying
        # here would race a healthy termination.
        self.assertFalse(horizons.beyond_grace_horizon(
            utc(2026, 8, 6, 10, 0, 0), now=utc(2026, 8, 6, 10, 5, 0)))

    def test_exactly_at_the_horizon_counts_as_beyond(self):
        self.assertTrue(horizons.beyond_grace_horizon(
            utc(2026, 8, 6, 10, 0, 0), now=utc(2026, 8, 6, 10, 10, 0)))

    def test_past_the_horizon(self):
        self.assertTrue(horizons.beyond_grace_horizon(
            utc(2026, 8, 6, 10, 0, 0), now=utc(2026, 8, 6, 10, 30, 0)))

    def test_an_unknown_stop_time_is_not_past_the_horizon(self):
        # An attempt the scheduler has not reported stopped is not inside or
        # past this horizon — it is not subject to it at all.
        self.assertFalse(horizons.beyond_grace_horizon(
            None, now=utc(2026, 8, 6, 10, 30, 0)))

    def test_the_starting_value_is_ten_minutes(self):
        self.assertEqual(600, horizons.GRACE_HORIZON_SECONDS)


class SubmissionHorizonTests(unittest.TestCase):
    def test_a_child_still_inside_its_horizon_is_left_alone(self):
        # A slow queue must not be read as a lost child.
        self.assertFalse(horizons.beyond_submission_horizon(
            utc(2026, 8, 6, 10, 0, 0), now=utc(2026, 8, 6, 10, 20, 0)))

    def test_past_the_submission_horizon(self):
        self.assertTrue(horizons.beyond_submission_horizon(
            utc(2026, 8, 6, 10, 0, 0), now=utc(2026, 8, 6, 10, 31, 0)))

    def test_an_unknown_submission_time_has_no_anchor(self):
        self.assertFalse(horizons.beyond_submission_horizon(
            None, now=utc(2026, 8, 6, 10, 30, 0)))

    def test_the_starting_value_is_thirty_minutes(self):
        self.assertEqual(1800, horizons.SUBMISSION_HORIZON_SECONDS)

    def test_it_is_longer_than_the_grace_horizon(self):
        # Deliberately so: it covers queue time, where the grace horizon only
        # covers the gap between a scheduler stop and a record appearing.
        self.assertGreater(horizons.SUBMISSION_HORIZON_SECONDS,
                           horizons.GRACE_HORIZON_SECONDS)


class NaiveTimestampTests(unittest.TestCase):
    def test_a_naive_timestamp_raises_rather_than_assuming_a_zone(self):
        import datetime

        naive = datetime.datetime(2026, 8, 6, 10, 0, 0)
        with self.assertRaises(ValueError):
            horizons.beyond_grace_horizon(
                naive, now=utc(2026, 8, 6, 10, 30, 0))


if __name__ == "__main__":
    unittest.main()
