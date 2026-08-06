"""The VPO's three seams: pre-creation order, bounded wait, consumer call."""

import unittest

from observability.attempts import ExecutionBinding
from pipeline import seams
from pipeline.reconciler.test.stubs import FakeConnection, attempt_row, utc
from submission.manifest import ProcessingUnit, UnitFacts


def units(count=2, base=90000):
    return [ProcessingUnit(exposure=base + i, sca=(i % 18) + 1,
                           facts=UnitFacts(rid=1, fid=1, field=1,
                                           expid=base + i),
                           fields={})
            for i in range(count)]


BINDING = ExecutionBinding(
    job_definition_arn="arn:aws:batch:us-east-1:1:job-definition/x:10",
    job_definition_rev=10, image_digest="sha256:abc",
    release_identity="rel-1", manifest_checksum="placeholder")


class FakeBatchClient:
    def __init__(self):
        self.kwargs = None

    def submit_job(self, **kwargs):
        self.kwargs = kwargs
        return {"jobId": "job-parent", "jobName": kwargs.get("jobName")}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.objects[(Bucket, Key)] = Body


class RecordingExecute:
    """Captures every statement the AttemptWriter issues, in order."""

    def __init__(self):
        self.statements = []
        self.next_id = 100

    def __call__(self, statement, params=None):
        self.statements.append((statement, params))
        if "RETURNING attempt_id" in statement or "resolve_attempt" in statement:
            self.next_id += 1
            return [(self.next_id,)]
        return 1


class SubmitUnitsTests(unittest.TestCase):
    def setUp(self):
        self.batch = FakeBatchClient()
        self.s3 = FakeS3()
        self.execute = RecordingExecute()

    def _submit(self, count=2):
        return seams.submit_units(
            units(count), job_type="science", queue="rapid-queue-prompt",
            job_definition="rapid-pipeline-science", binding=BINDING,
            manifest_bucket="bucket", manifest_prefix="submissions",
            s3_client=self.s3, batch_client=self.batch,
            execute=self.execute, run_id="run-1",
            now=utc(2026, 8, 6, 12, 0, 0))

    def test_one_array_job_not_one_submit_per_unit(self):
        submission, _ = self._submit(count=3)

        self.assertEqual("job-parent", submission.job_id)
        self.assertEqual(3, submission.array_size)
        self.assertEqual(3, self.batch.kwargs["arrayProperties"]["size"])

    def test_a_row_is_created_for_every_child(self):
        _submission, attempt_ids = self._submit(count=3)
        self.assertEqual(3, len(attempt_ids))

    def test_the_logical_job_is_recorded_before_its_attempt_row(self):
        # resolve_attempt copies the execution binding from the logical job,
        # so a row created first would have nothing to copy.
        self._submit(count=1)

        kinds = [("logical" if "logical_jobs" in sql else
                  "attempt" if "INTO attempts" in sql else "other")
                 for sql, _ in self.execute.statements]
        self.assertLess(kinds.index("logical"), kinds.index("attempt"))

    def test_rows_are_keyed_by_the_id_the_runtime_will_resolve_with(self):
        # THE defect this seam exists to avoid: pre-creating rows under a
        # different logical_job_id than the runtime uses means resolve_attempt
        # can never claim them, so every child makes a second row and every
        # pre-created row is orphaned in `submitted`.
        self._submit(count=1)

        inserts = [params for sql, params in self.execute.statements
                   if "INTO attempts" in sql]
        self.assertTrue(inserts)
        # The manifest unit's key is "<exposure>/<sca>".
        self.assertIn("90000/1", [p for p in inserts[0] if isinstance(p, str)])

    def test_the_binding_carries_the_published_manifests_checksum(self):
        submission, _ = self._submit(count=2)

        logicals = [params for sql, params in self.execute.statements
                    if "logical_jobs" in sql]
        self.assertIn(submission.manifest_checksum, logicals[0])
        # ...and NOT the placeholder the caller passed in.
        self.assertNotIn("placeholder", logicals[0])


class WaitForCompletionTests(unittest.TestCase):
    def _conn(self, *snapshots):
        """A connection whose progress query returns each snapshot in turn."""
        conn = FakeConnection(rows=[])
        self._snapshots = list(snapshots)

        def route(text, params):
            if "group by lifecycle_state" in text.lower():
                counts = self._snapshots.pop(0) if self._snapshots else {}
                return ([(state, n) for state, n in counts.items()],
                        [("lifecycle_state",), ("count",)])
            return None

        conn.route = route
        return conn

    def test_returns_when_every_attempt_is_reconciler_terminal(self):
        conn = self._conn({"terminal_after_start": 3})

        counts = seams.wait_for_completion(conn, "run-1", sleep=lambda _: None)

        self.assertEqual({"terminal_after_start": 3}, counts)

    def test_waits_while_anything_is_still_open(self):
        conn = self._conn(
            {"submitted": 2, "terminal_after_start": 1},
            {"started": 1, "terminal_after_start": 2},
            {"terminal_after_start": 3})
        slept = []

        counts = seams.wait_for_completion(
            conn, "run-1", sleep=slept.append, poll_seconds=7)

        self.assertEqual({"terminal_after_start": 3}, counts)
        self.assertEqual([7, 7], slept)

    def test_application_closed_does_not_count_as_finished(self):
        # It is the application's own verdict with no scheduler truth yet —
        # exactly what registration must not act on.
        conn = self._conn({"application_closed": 1}, {"terminal_after_start": 1})
        slept = []

        seams.wait_for_completion(conn, "run-1", sleep=slept.append)

        self.assertEqual(1, len(slept))

    def test_the_wait_is_bounded_and_says_what_is_outstanding(self):
        # The old wait had no timeout at all: a stuck job blocked forever.
        conn = self._conn({"started": 2}, {"started": 2}, {"started": 2})
        clock = iter([0, 0, 100, 200, 300, 400])

        with self.assertRaises(seams.CompletionTimeout) as caught:
            seams.wait_for_completion(
                conn, "run-1", timeout=50, sleep=lambda _: None,
                monotonic=lambda: next(clock))

        self.assertEqual(2, caught.exception.outstanding)
        self.assertIn("reconciliation case", str(caught.exception))

    def test_no_rows_at_all_returns_rather_than_hanging(self):
        conn = self._conn({})
        self.assertEqual({}, seams.wait_for_completion(
            conn, "run-1", sleep=lambda _: None))


class RunRegistrationTests(unittest.TestCase):
    def test_it_calls_the_consumer_and_returns_its_counts(self):
        conn = FakeConnection(rows=[
            attempt_row(1, lifecycle_state="terminal_after_start",
                        started_at=utc(2026, 8, 6, 11, 0, 0),
                        rapid_outcome="success",
                        product_disposition="published",
                        terminal_record_sequence=1)])

        run = seams.run_registration(conn)

        self.assertEqual(1, run.registered)
        self.assertEqual(0, run.exit_code)


if __name__ == "__main__":
    unittest.main()
