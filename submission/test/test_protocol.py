"""Stub-tier tests for `submission.protocol.resolve_submission_outcome`
(campaign C4): the unified read that answers "did this attempt reach the
scheduler", collapsing what used to be `pipeline.reconciler.service`'s
own `_submission_classification` plus a bare `attempts.lifecycle_state`
check. No live database needed — `resolve_submission_outcome` takes an
injected `execute(sql, params)` callable and never opens a connection of
its own, the same shape `pipeline.intent.writer` and `observability.
attempts` use for the identical reason (testable without a driver).

The join itself (`_SUBMISSION_FOR_ATTEMPT_SQL`'s real FK behaviour) is a
live-database property and stays covered by
`pipeline/contract/test_submission_protocol.py`; what this file pins is
the DECISION logic layered on top of whatever that join returns.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission.protocol import SubmissionOutcome, resolve_submission_outcome


class RecordingExecutor:
    """Answers one scripted `submission_for_attempt` result, or raises."""

    def __init__(self, submission_row=None, raises=None):
        self.calls = []
        self._submission_row = submission_row
        self._raises = raises

    def __call__(self, sql, params):
        self.calls.append((sql, params))
        if self._raises is not None:
            raise self._raises
        if self._submission_row is None:
            return []
        return [self._submission_row]


def attempt_row(**overrides):
    row = {"attempt_id": 1, "submission_id": None,
           "lifecycle_state": "submitted"}
    row.update(overrides)
    return row


class ContradictoryTakesPriorityTests(unittest.TestCase):
    """The row's OWN recorded lifecycle_state is checked FIRST, before any
    submission lookup — a row already flagged missing_or_contradictory is
    returned as CONTRADICTORY without a query, whatever submission_id it
    carries."""

    def test_no_query_is_issued_when_the_row_is_already_flagged(self):
        execute = RecordingExecutor()
        row = attempt_row(lifecycle_state="missing_or_contradictory",
                          submission_id=100)
        outcome = resolve_submission_outcome(execute, row)
        self.assertEqual(SubmissionOutcome.CONTRADICTORY, outcome)
        self.assertEqual([], execute.calls)

    def test_takes_priority_even_with_a_found_submission_row(self):
        # An already-contradictory row is not second-guessed by a submission
        # record that happens to say FOUND — the row's own recorded fact
        # wins; a query is not even issued to find out.
        execute = RecordingExecutor(
            submission_row=(100, "found", "job-1", "queue-1", None))
        row = attempt_row(lifecycle_state="missing_or_contradictory",
                          submission_id=100)
        self.assertEqual(SubmissionOutcome.CONTRADICTORY,
                         resolve_submission_outcome(execute, row))


class NoSubmissionIdTests(unittest.TestCase):
    def test_pending_with_no_query(self):
        execute = RecordingExecutor()
        row = attempt_row(submission_id=None)
        outcome = resolve_submission_outcome(execute, row)
        self.assertEqual(SubmissionOutcome.PENDING, outcome)
        self.assertEqual([], execute.calls,
                         "no submission_id means nothing to look up")


class FoundAndLostTests(unittest.TestCase):
    def test_found(self):
        execute = RecordingExecutor(
            submission_row=(100, "found", "job-1", "queue-1", None))
        row = attempt_row(submission_id=100)
        self.assertEqual(SubmissionOutcome.FOUND,
                         resolve_submission_outcome(execute, row))

    def test_lost(self):
        execute = RecordingExecutor(
            submission_row=(100, "lost", "job-1", "queue-1", None))
        row = attempt_row(submission_id=100)
        self.assertEqual(SubmissionOutcome.LOST,
                         resolve_submission_outcome(execute, row))

    def test_calling_is_pending(self):
        execute = RecordingExecutor(
            submission_row=(100, "calling", "job-1", "queue-1", None))
        row = attempt_row(submission_id=100)
        self.assertEqual(SubmissionOutcome.PENDING,
                         resolve_submission_outcome(execute, row))

    def test_unknown_is_pending(self):
        execute = RecordingExecutor(
            submission_row=(100, "unknown", "job-1", "queue-1", None))
        row = attempt_row(submission_id=100)
        self.assertEqual(SubmissionOutcome.PENDING,
                         resolve_submission_outcome(execute, row))

    def test_no_submission_row_found_by_the_join_is_pending(self):
        # submission_id is set but the join found nothing — the row-a-
        # submission-pass-could-not-attach case `submission_for_attempt`'s
        # own docstring names.
        execute = RecordingExecutor(submission_row=None)
        row = attempt_row(submission_id=100)
        self.assertEqual(SubmissionOutcome.PENDING,
                         resolve_submission_outcome(execute, row))


class ReadErrorsPropagateTests(unittest.TestCase):
    """C4's stated acceptance bar: a read error is now an error, never a
    silent downgrade to PENDING or any other conclusion."""

    def test_a_raising_lookup_propagates(self):
        execute = RecordingExecutor(raises=RuntimeError("db is unreachable"))
        row = attempt_row(submission_id=100)
        with self.assertRaises(RuntimeError):
            resolve_submission_outcome(execute, row)

    def test_a_raising_lookup_is_not_swallowed_into_contradictory_either(self):
        # A row that is NOT already missing_or_contradictory, whose lookup
        # then fails, must not be silently reported as any outcome at all —
        # confirmed by the exception propagating rather than a plausible-
        # looking return value.
        execute = RecordingExecutor(raises=ValueError("malformed row"))
        row = attempt_row(lifecycle_state="submitted", submission_id=100)
        with self.assertRaises(ValueError):
            resolve_submission_outcome(execute, row)


if __name__ == "__main__":
    unittest.main()
