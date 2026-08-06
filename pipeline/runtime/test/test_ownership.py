"""
File:    test_ownership.py

Attempt ownership at startup: numbering normalization, resolution through W1's
atomic resolver, and the states the runtime refuses to work from.
"""

import unittest

from observability.attempts import AttemptWriter, LifecycleState
from pipeline.runtime.errors import RecordsError
from pipeline.runtime.ownership import (
    lifecycle_reader_for,
    normalize_attempt_index,
    resolve_ownership,
)
from pipeline.runtime.test.stubs import RecordingExecutor, make_job_environment


class TestNumberingNormalization(unittest.TestCase):
    """The stored convention is one-based, and so is Batch's.

    That agreement is a fact about two independent systems, not a tautology.
    These tests are what fails first if either side moves.
    """

    def test_batch_one_based_maps_to_stored_one_based(self):
        self.assertEqual(normalize_attempt_index(1), 1)
        self.assertEqual(normalize_attempt_index(3), 3)

    def test_a_string_from_the_environment_is_accepted(self):
        self.assertEqual(normalize_attempt_index("2"), 2)

    def test_zero_is_refused(self):
        """A zero index would key the resolver to the wrong row."""
        with self.assertRaises(ValueError):
            normalize_attempt_index(0)

    def test_negative_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_attempt_index(-1)

    def test_non_numeric_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_attempt_index("first")

    def test_none_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_attempt_index(None)


class TestResolveOwnership(unittest.TestCase):

    def setUp(self):
        self.executor = RecordingExecutor()
        self.writer = AttemptWriter(self.executor)
        self.executor.logical_jobs["batch-1:0"] = ["batch-1:0"]

    def _resolve(self, **overrides):
        job_env = make_job_environment(**overrides)
        return resolve_ownership(self.writer, job_env, run_id="run-1",
                                 logical_job_id="batch-1:0")

    def test_resolution_goes_through_the_resolver_function(self):
        """Never a bare INSERT — the resolver is the only acquisition path,
        and its advisory lock plus partial uniqueness indexes are what make a
        retry, a reconciler-created row, and a late runtime converge on one
        row."""
        self._resolve()
        self.assertTrue(self.executor.statements_matching("resolve_attempt("),
                        "acquisition must call the resolver function")
        self.assertFalse(
            [c for c in self.executor.calls
             if c[0].strip().upper().startswith("INSERT INTO ATTEMPTS")],
            "the runtime must never bare-INSERT an attempt row")

    def test_attempt_one_is_recorded_as_claiming_the_precreated_row(self):
        ownership = self._resolve(attempt_index=1)
        self.assertTrue(ownership.claimed_precreated)
        self.assertEqual(ownership.attempt_index, 1)

    def test_a_retry_is_recorded_as_created(self):
        ownership = self._resolve(attempt_index=2)
        self.assertFalse(ownership.claimed_precreated)
        self.assertEqual(ownership.attempt_index, 2)

    def test_the_application_index_is_what_is_passed_to_the_resolver(self):
        self._resolve(attempt_index=3)
        call = self.executor.statements_matching("resolve_attempt(")[0]
        # Parameter order per attempts.resolve_attempt: run, logical job,
        # scheduler job, application index, scheduler index, ...
        self.assertEqual(call[1][3], 3)
        self.assertIsNone(call[1][4],
                          "the runtime never writes the scheduler-observed "
                          "index — that column has one writer, the reconciler")

    def test_the_resolved_row_is_returned_with_its_identity(self):
        ownership = self._resolve()
        self.assertEqual(ownership.run_id, "run-1")
        self.assertEqual(ownership.logical_job_id, "batch-1:0")
        self.assertEqual(ownership.scheduler_job_id, "job-abc123")
        self.assertIsNotNone(ownership.attempt_id)

    def test_a_resolver_failure_is_a_records_error(self):
        """Nothing has run yet and there is no row to record an outcome into,
        so this is the fail-loud posture's unrecordable case."""
        self.executor.fail_on["resolve_attempt("] = RuntimeError("no route")
        with self.assertRaises(RecordsError) as caught:
            self._resolve()
        self.assertEqual(caught.exception.error_category, "records_error")

    def test_the_processing_unit_scope_is_passed_through(self):
        """A retry row must be scoped like the row it would have claimed."""
        job_env = make_job_environment()
        resolve_ownership(self.writer, job_env, run_id="run-1",
                          logical_job_id="batch-1:0",
                          identity_extra={"exposure_id": 77, "sca": 4})
        call = self.executor.statements_matching("resolve_attempt(")[0]
        self.assertIn(77, call[1])
        self.assertIn(4, call[1])


class TestRefusedStates(unittest.TestCase):
    """States the runtime will not start work from."""

    def setUp(self):
        self.executor = RecordingExecutor()
        self.writer = AttemptWriter(self.executor)
        self.executor.logical_jobs["batch-1:0"] = ["batch-1:0"]
        self.reader = lifecycle_reader_for(self.executor)

    def _resolve(self):
        return resolve_ownership(
            self.writer, make_job_environment(), run_id="run-1",
            logical_job_id="batch-1:0", lifecycle_reader=self.reader)

    def test_a_submitted_row_is_accepted(self):
        ownership = self._resolve()
        self.assertIsNotNone(ownership.attempt_id)

    def test_a_missing_or_contradictory_row_is_a_hard_stop(self):
        """The resolver's reconciler-first branch: Batch knows about the job
        but no logical job was recorded, so there is no execution binding to
        copy and this attempt's provenance could never be completed. Doing
        science work on top of that would produce products no record can
        account for.
        """
        self.executor.logical_jobs.clear()
        self.executor.rows[500] = {
            "attempt_id": 500,
            "logical_job_id": "batch-1:0",
            "application_attempt_index": 1,
            "lifecycle_state":
                LifecycleState.MISSING_OR_CONTRADICTORY.value,
        }
        with self.assertRaises(RecordsError) as caught:
            self._resolve()
        self.assertIn("missing_or_contradictory", str(caught.exception))

    def test_an_already_closed_row_is_refused(self):
        for state in (LifecycleState.APPLICATION_CLOSED,
                      LifecycleState.TERMINAL_AFTER_START,
                      LifecycleState.TERMINAL_WITHOUT_START):
            with self.subTest(state=state.value):
                executor = RecordingExecutor()
                executor.logical_jobs["batch-1:0"] = ["batch-1:0"]
                executor.rows[600] = {
                    "attempt_id": 600,
                    "logical_job_id": "batch-1:0",
                    "application_attempt_index": 1,
                    "lifecycle_state": state.value,
                }
                with self.assertRaises(RecordsError):
                    resolve_ownership(
                        AttemptWriter(executor), make_job_environment(),
                        run_id="run-1", logical_job_id="batch-1:0",
                        lifecycle_reader=lifecycle_reader_for(executor))

    def test_a_row_that_cannot_be_read_back_is_refused(self):
        """The resolver returned an id for a row nothing can read — the
        attempt cannot be identified, so no outcome could be recorded."""
        executor = RecordingExecutor()
        executor.logical_jobs["batch-1:0"] = ["batch-1:0"]
        writer = AttemptWriter(executor)

        def empty_reader(_attempt_id):
            return None

        with self.assertRaises(RecordsError):
            resolve_ownership(writer, make_job_environment(), run_id="run-1",
                              logical_job_id="batch-1:0",
                              lifecycle_reader=empty_reader)


class TestLifecycleReader(unittest.TestCase):

    def test_reads_the_state_of_an_existing_row(self):
        executor = RecordingExecutor()
        executor.rows[1] = {"attempt_id": 1, "lifecycle_state": "started"}
        self.assertEqual(lifecycle_reader_for(executor)(1), "started")

    def test_returns_none_for_a_row_that_does_not_exist(self):
        executor = RecordingExecutor()
        self.assertIsNone(lifecycle_reader_for(executor)(999))

    def test_the_query_is_parameterized(self):
        executor = RecordingExecutor()
        executor.rows[1] = {"attempt_id": 1, "lifecycle_state": "started"}
        lifecycle_reader_for(executor)(1)
        statement, params = executor.calls[-1]
        self.assertIn("%s", statement)
        self.assertEqual(params, [1])


if __name__ == "__main__":
    unittest.main()
