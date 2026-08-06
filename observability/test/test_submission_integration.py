"""
File:    test_submission_integration.py

The seam between Q4's submission package and attempt-record creation.

submit.py stops at the submit boundary on purpose ("Deliberately absent:
attempt-record creation ... `submit_batch` returns everything an attempt-record
writer needs"). These tests exercise that composition against the REAL
`Submission`, `Manifest` and `ProcessingUnit` types — a fake Batch client and a
fake manifest store stand in for AWS, and the recording executor stands in for
the database, so no account and no database are touched.

Testing against the real types is the point: a change to `Submission`'s shape or
to `child_job_id`'s derivation would break attempt-record creation, and this is
where that breakage surfaces.
"""

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from observability.attempts import (
    AttemptWriter,
    ExecutionBinding,
    LifecycleState,
)

# The submission-time execution binding every attempt row carries from
# creation (schema_version 2, migration 013). create_submitted_for_submission
# records the logical job and copies this onto each child's row, so these
# integration tests supply it exactly as the real submitter does.
BINDING = ExecutionBinding(
    job_definition_arn=(
        "arn:aws:batch:us-east-1:ACCOUNT:job-definition/"
        "rapid-science:7"),
    job_definition_rev=7,
    image_digest="sha256:" + "a" * 64,
    release_identity="rapid-test",
    manifest_checksum="sha256:" + "b" * 64,
)
from submission.batching import Batch
from submission.manifest import Manifest, ProcessingUnit
from submission.submit import submit_batch

UTC = datetime.timezone.utc


def at(second: int) -> datetime.datetime:
    return datetime.datetime(2026, 8, 4, 12, 0, second, tzinfo=UTC)


class RecordingExecutor:
    def __init__(self):
        self.calls = []
        self._next_id = 1

    def __call__(self, sql, params):
        self.calls.append((" ".join(sql.split()), list(params)))
        if "RETURNING" in sql:
            value = self._next_id
            self._next_id += 1
            return [(value,)]
        return None


class FakeStore:
    def __init__(self):
        self.objects = {}

    def key_for(self, batch_id):
        return f"submissions/{batch_id}/manifest.json"

    def put(self, key, body):
        self.objects[key] = body
        return f"s3://fake-bucket/{key}"

    def get(self, uri):
        return self.objects[uri.removeprefix("s3://fake-bucket/")]


class FakeBatchClient:
    def __init__(self, job_id="job-real"):
        self.job_id = job_id
        self.calls = []

    def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"jobId": self.job_id, "jobName": kwargs["jobName"]}


def a_batch(units, batch_id="batch-1"):
    manifest = Manifest([ProcessingUnit(exposure=e, sca=s) for e, s in units],
                        batch_id=batch_id)
    return Batch(manifest=manifest, reason="full")


class SubmissionToAttemptRowsTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)
        self.client = FakeBatchClient()
        self.store = FakeStore()


    def attempt_calls(self):
        """Only the `attempts` INSERTs, in order.

        create_submitted_for_submission interleaves a `logical_jobs` INSERT
        per unit (the execution binding has to exist before the row that
        copies it), so a bare walk of every recorded call would index the
        wrong statement's params.
        """
        return [(sql, params) for sql, params in self.execute.calls
                if "INSERT INTO attempts" in sql]

    def submit(self, units):
        return submit_batch(a_batch(units), job_queue="rapid-prompt",
                            job_definition="rapid-science",
                            store=self.store, client=self.client)

    def test_one_attempt_row_per_array_child_of_a_real_submission(self):
        submission = self.submit([(100, 1), (100, 2), (101, 3)])
        ids = self.writer.create_submitted_for_submission(
            submission, run_id="run-1", created_at=at(0), submitted_at=at(1),
            binding=BINDING)

        self.assertEqual(len(ids), 3)
        self.assertEqual(len(ids), submission.array_size)

    def test_rows_are_created_in_submitted_state(self):
        submission = self.submit([(100, 1), (100, 2)])
        self.writer.create_submitted_for_submission(
            submission, run_id="run-1", created_at=at(0), submitted_at=at(1),
            binding=BINDING)
        for _, params in self.attempt_calls():
            self.assertIn(LifecycleState.SUBMITTED.value, params)

    def test_child_job_ids_match_what_submission_derives(self):
        submission = self.submit([(100, 1), (100, 2), (101, 3)])
        self.writer.create_submitted_for_submission(
            submission, run_id="run-1", created_at=at(0), submitted_at=at(1),
            binding=BINDING)

        recorded = [params[3] for _, params in self.attempt_calls()]
        expected = [submission.child_job_id(i) for i in range(3)]
        self.assertEqual(recorded, expected)

    def test_single_unit_batch_is_not_an_array_and_gets_one_row(self):
        # Batch rejects arraySize 1, so a one-unit batch is a plain job whose
        # child job id is the job id itself. One attempt row either way.
        submission = self.submit([(100, 1)])
        ids = self.writer.create_submitted_for_submission(
            submission, run_id="run-1", created_at=at(0), submitted_at=at(1),
            binding=BINDING)

        self.assertEqual(len(ids), 1)
        self.assertFalse(submission.is_array)
        self.assertEqual(self.attempt_calls()[0][1][3], submission.job_id)

    def test_unit_identity_is_carried_onto_each_row(self):
        submission = self.submit([(100, 1), (101, 17)])
        self.writer.create_submitted_for_submission(
            submission, run_id="run-1", created_at=at(0), submitted_at=at(1),
            binding=BINDING)

        exposures = [params[4] for _, params in self.attempt_calls()]
        scas = [params[5] for _, params in self.attempt_calls()]
        self.assertEqual(exposures, [100, 101])
        self.assertEqual(scas, [1, 17])

    def test_rows_exist_even_though_the_scheduler_has_not_confirmed_children(self):
        # The ordering the design requires: the Batch call returns one parent
        # job id, and rows for every child exist immediately — so a child that
        # never resolves is a detectable reconciliation case, not a silent gap.
        submission = self.submit([(100, 1), (100, 2), (100, 3)])
        self.assertEqual(len(self.client.calls), 1)

        ids = self.writer.create_submitted_for_submission(
            submission, run_id="run-1", created_at=at(0), submitted_at=at(1),
            binding=BINDING)
        self.assertEqual(len(ids), 3)


if __name__ == "__main__":
    unittest.main()
