"""`run_forever`'s consecutive-failure threshold, with every boundary stubbed.

Mirrors `pipeline/reconciler/test/test_service.py`'s `HealthTests` for the
same property: a single cycle failure is retried in place, but consecutive
failures past the threshold must raise so the process exits and a supervisor
restarts it, rather than staying up and delivering nothing forever.
"""

import unittest

from pipeline.publisher import service


def _build_cycle(explode):
    """A `build_cycle` callable whose `run_once` either explodes or reports."""
    class Cycle:
        def run_once(self):
            return explode()
    return lambda: Cycle()


class RunForeverThresholdTests(unittest.TestCase):

    def test_survives_a_failing_cycle_below_threshold(self):
        cycles = {"n": 0}

        def explode():
            cycles["n"] += 1
            raise RuntimeError("broker unreachable")

        # A high threshold: this is about surviving transients, which is
        # still the behaviour below the threshold.
        service.run_forever(
            _build_cycle(explode), poll_seconds=0, sleep=lambda _: None,
            should_continue=lambda: cycles["n"] < 3, failure_threshold=99)

        self.assertEqual(3, cycles["n"])

    def test_consecutive_failures_past_the_threshold_exit_the_loop(self):
        calls = {"n": 0}

        def explode():
            calls["n"] += 1
            raise RuntimeError("the database is gone")

        with self.assertRaises(service.PublisherUnhealthy) as caught:
            service.run_forever(_build_cycle(explode), poll_seconds=0,
                                sleep=lambda _: None, failure_threshold=3)

        self.assertEqual(3, calls["n"])
        self.assertIn("delivering nothing", str(caught.exception))

    def test_a_successful_cycle_resets_the_failure_count(self):
        # A transient must not accumulate toward the threshold across
        # unrelated cycles, or a publisher that works fine will eventually
        # exit for reasons long past.
        calls = {"n": 0}
        empty_counts = {"claimed": 0, "sent": 0, "resend": 0, "refused": 0,
                        "held": 0, "reclaimed": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] % 2:
                raise RuntimeError("transient")
            return dict(empty_counts)

        service.run_forever(
            _build_cycle(flaky), poll_seconds=0, sleep=lambda _: None,
            should_continue=lambda: calls["n"] < 6, failure_threshold=3)

        self.assertEqual(6, calls["n"])

    def test_a_sent_packet_does_not_sleep_the_idle_interval(self):
        # Not a failure-threshold case, but the cheapest place to pin
        # run_forever's other branch: a productive cycle must not idle-sleep,
        # or a busy outbox would drain at the idle cadence instead of back to
        # back.
        slept = []
        counts = {"claimed": 1, "sent": 1, "resend": 0, "refused": 0,
                 "held": 0, "reclaimed": 0}
        calls = {"n": 0}

        def productive():
            calls["n"] += 1
            return counts

        service.run_forever(
            _build_cycle(productive), poll_seconds=5, sleep=slept.append,
            should_continue=lambda: calls["n"] < 2)

        self.assertEqual([], slept)


if __name__ == "__main__":
    unittest.main()
