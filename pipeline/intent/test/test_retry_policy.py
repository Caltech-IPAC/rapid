"""The run's retry budget at disposition (migration 122).

`retry_policy` performs no I/O and reads no row — its own docstring calls it
"exhaustively testable without a database" — so this is the stub-tier module
that takes it at its word. The DB-backed contract suite under
`pipeline/contract/` continues to cover the transitions themselves.

THE RULING THESE PIN (Ben, 2026-09-13 13:01, delegated: "Do not accept my
words. Set retries as you think best"): at most 3 attempts in total for
TRANSIENT failures, none for DETERMINISTIC ones, Spot reclaims NEVER counted,
the count the pipeline's own from its attempt rows with Batch's 10 as the
outer bound, and a per-unit wall-clock budget of 3 x the lane's attempt
timeout.

The wall-clock half is not decoration. The 2026-09-13 overnight run's body
finished at 06:30 and its clock ran past 11:00 on a retry loop: an attempt
COUNT alone did not stop it, because each re-release began a fresh attempt
series and reset the count while the wall-clock kept running.
"""

import datetime
import unittest

from pipeline.intent import retry_policy


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class TransientBudgetCountTests(unittest.TestCase):
    """The attempt-count bound."""

    def _disposition(self, count, attempts=3):
        return retry_policy.disposition_for_terminal_attempt(
            succeeded=False, error_category="db_unavailable",
            transient_failure_count=count, retry_attempts=attempts)

    def test_a_transient_failure_under_the_budget_retries(self):
        self.assertEqual(retry_policy.RETRY_READY, self._disposition(1))

    def test_the_last_attempt_under_the_budget_still_retries(self):
        self.assertEqual(retry_policy.RETRY_READY, self._disposition(2))

    def test_budget_reached_closes_the_unit_failed(self):
        # An exhausted unit — the term every record uses.
        self.assertEqual(retry_policy.CLOSE_FAILED, self._disposition(3))

    def test_budget_exceeded_closes_the_unit_failed(self):
        self.assertEqual(retry_policy.CLOSE_FAILED, self._disposition(9))

    def test_a_run_with_no_budget_parks_exactly_as_before(self):
        # A run predating 122, or a unit with no run row. Inventing a budget
        # for a run that never declared one would start re-running work
        # nobody asked to have re-run.
        self.assertEqual(
            retry_policy.PARK_BLOCKED,
            retry_policy.disposition_for_terminal_attempt(
                succeeded=False, error_category="db_unavailable",
                transient_failure_count=0, retry_attempts=None))


class TransientBudgetWallclockTests(unittest.TestCase):
    """The wall-clock bound, measured from the unit's FIRST submission."""

    def _disposition(self, first_submitted_at, now, wallclock=129600):
        return retry_policy.disposition_for_terminal_attempt(
            succeeded=False, error_category="db_unavailable",
            transient_failure_count=0, retry_attempts=3,
            first_submitted_at=first_submitted_at,
            retry_wallclock_s=wallclock, now=now)

    def test_inside_the_wallclock_retries(self):
        self.assertEqual(
            retry_policy.RETRY_READY,
            self._disposition(utc(2026, 9, 14, 6, 0, 0),
                              utc(2026, 9, 14, 8, 0, 0)))

    def test_past_the_wallclock_closes_even_with_attempts_to_spare(self):
        # THE 2026-09-13 SHAPE: `transient_failure_count` is 0, so the count
        # says "three attempts left", while the unit has been trying for
        # fourteen hours. On the prompt lane (14400 s attempt timeout, so a
        # 43200 s budget) the clock has long since run out, and it wins —
        # which is the whole reason the second bound exists. The overnight
        # run's body finished at 06:30 and its clock ran past 11:00 precisely
        # because a count alone could not say this.
        self.assertEqual(
            retry_policy.CLOSE_FAILED,
            self._disposition(utc(2026, 9, 13, 18, 0, 0),
                              utc(2026, 9, 14, 8, 0, 0), wallclock=43200))

    def test_exactly_at_the_wallclock_closes(self):
        self.assertEqual(
            retry_policy.CLOSE_FAILED,
            self._disposition(utc(2026, 9, 14, 6, 0, 0),
                              utc(2026, 9, 14, 6, 0, 10), wallclock=10))

    def test_a_missing_first_submission_disables_only_the_clock(self):
        # Absent data is not evidence the budget is spent, and closing a unit
        # failed on a NULL timestamp would fail work for a reason that has
        # nothing to do with the work. The COUNT still applies.
        self.assertEqual(
            retry_policy.RETRY_READY,
            self._disposition(None, utc(2026, 9, 14, 8, 0, 0)))

    def test_a_naive_first_submission_raises_rather_than_comparing(self):
        # Every timestamp in this system is stored timestamptz and read back
        # aware. Comparing a naive one would raise deep inside the
        # disposition; raising here says which value was wrong.
        with self.assertRaises(ValueError):
            retry_policy.disposition_for_terminal_attempt(
                succeeded=False, error_category="db_unavailable",
                transient_failure_count=0, retry_attempts=3,
                first_submitted_at=datetime.datetime(2026, 9, 14, 6, 0, 0),
                retry_wallclock_s=100, now=utc(2026, 9, 14, 8, 0, 0))


class ReclaimsAreNeverCountedTests(unittest.TestCase):
    """"Spot reclaims never counted" — the ruling, verbatim."""

    def test_a_reclaim_takes_the_scheduler_path_not_the_run_budget(self):
        # A reclaim says nothing about the work. Spending the run's budget on
        # it would let a bad afternoon on the Spot market exhaust units whose
        # science never once failed.
        self.assertEqual(
            retry_policy.RETRY_READY,
            retry_policy.disposition_for_terminal_attempt(
                succeeded=False, error_category="scheduler_reclaimed",
                scheduler_loss_count=19,
                transient_failure_count=99, retry_attempts=3,
                first_submitted_at=utc(2026, 9, 13, 1, 0, 0),
                retry_wallclock_s=10, now=utc(2026, 9, 14, 8, 0, 0)))

    def test_a_reclaim_still_answers_to_its_own_ceiling(self):
        self.assertEqual(
            retry_policy.CLOSE_FAILED,
            retry_policy.disposition_for_terminal_attempt(
                succeeded=False, error_category="scheduler_reclaimed",
                scheduler_loss_count=retry_policy.SCHEDULER_RETRY_CEILING,
                retry_attempts=3))

    def test_the_scheduler_ceiling_is_unchanged(self):
        self.assertEqual(20, retry_policy.SCHEDULER_RETRY_CEILING)

    def test_no_reconciler_category_is_in_the_transient_set(self):
        from observability.attempts import RECONCILER_ERROR_CATEGORIES
        self.assertEqual(
            frozenset(),
            RECONCILER_ERROR_CATEGORIES
            & retry_policy.TRANSIENT_APPLICATION_CATEGORIES)


class DeterministicFailuresStillParkTests(unittest.TestCase):
    """"none for deterministic ones" — the other half of the same sentence."""

    def test_every_non_transient_application_category_parks(self):
        from observability.attempts import APPLICATION_ERROR_CATEGORIES
        deterministic = (APPLICATION_ERROR_CATEGORIES
                         - retry_policy.TRANSIENT_APPLICATION_CATEGORIES)
        self.assertTrue(deterministic)
        for category in sorted(deterministic):
            with self.subTest(category=category):
                self.assertEqual(
                    retry_policy.PARK_BLOCKED,
                    retry_policy.disposition_for_terminal_attempt(
                        succeeded=False, error_category=category,
                        transient_failure_count=0, retry_attempts=3),
                    "%s is deterministic: the same unit, the same inputs and "
                    "the same code fail the same way, so a retry is waste"
                    % category)

    def test_an_absent_category_still_parks(self):
        self.assertEqual(
            retry_policy.PARK_BLOCKED,
            retry_policy.disposition_for_terminal_attempt(
                succeeded=False, error_category=None,
                transient_failure_count=0, retry_attempts=3))

    def test_success_is_untouched_by_any_budget(self):
        self.assertEqual(
            retry_policy.CLOSE_COMPLETE,
            retry_policy.disposition_for_terminal_attempt(
                succeeded=True, error_category=None,
                transient_failure_count=99, retry_attempts=1))


if __name__ == "__main__":
    unittest.main()
