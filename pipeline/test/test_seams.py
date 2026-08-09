"""The VPO's three seams: pre-creation order, bounded wait, consumer call."""

import io
import unittest

from observability.attempts import ExecutionBinding
from pipeline import seams
from pipeline.reconciler.test.stubs import FakeConnection, attempt_row, utc
from submission import submit
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


#: A monotonic call counter shared by the doubles, so a test can assert that
#: one call happened before another. The submission ORDER is the contract
#: under test (review finding #2), and order is not observable from the
#: recorded arguments alone.
class CallClock:
    def __init__(self):
        self.n = 0

    def tick(self):
        self.n += 1
        return self.n


class FakeBatchClient:
    def __init__(self, clock=None):
        self.kwargs = None
        self.clock = clock or CallClock()
        self.submitted_at_call = None
        #: Set to an exception to make submit_job fail, for the case where
        #: SubmitJob fails after the rows were pre-created.
        self.fail_with = None

    def submit_job(self, **kwargs):
        self.submitted_at_call = self.clock.tick()
        self.kwargs = kwargs
        if self.fail_with is not None:
            raise self.fail_with
        return {"jobId": "job-parent", "jobName": kwargs.get("jobName")}


class PreconditionFailed(Exception):
    """What S3 raises for a failed `IfNoneMatch` (HTTP 412).

    Shaped like botocore's error so `_is_precondition_failed` recognises it the
    same way here as against the real client.
    """

    def __init__(self):
        super().__init__("At least one of the pre-conditions you specified "
                         "did not hold")
        self.response = {"Error": {"Code": "PreconditionFailed"}}


class FakeS3:
    """An object store that ENFORCES create-once, because the real one does.

    A fake that accepted every `put_object` could not tell a store that
    overwrites from one that refuses to, so the manifest's write-once contract
    was untestable here — and untested, it was not implemented.
    """

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None,  # noqa: N803
                   IfNoneMatch=None):  # noqa: N803
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise PreconditionFailed()
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class RecordingExecute:
    """Captures every statement the AttemptWriter issues, in order."""

    def __init__(self, clock=None):
        self.statements = []
        self.next_id = 100
        self.clock = clock or CallClock()
        #: The clock reading at the first `INSERT INTO attempts`, so a test can
        #: assert the rows were written before SubmitJob was called.
        self.first_attempt_insert_call = None

    def __call__(self, statement, params=None):
        call = self.clock.tick()
        self.statements.append((statement, params))
        if "INSERT INTO attempts" in statement \
                and self.first_attempt_insert_call is None:
            self.first_attempt_insert_call = call
        if "INSERT INTO logical_jobs" in statement:
            # `ON CONFLICT DO NOTHING RETURNING logical_job_id` — one row back
            # when the insert landed (FixA, #3: a conflict is verified, not
            # ignored, so the two cases must be distinguishable).
            return [(params[0],)] if params else [("lj",)]
        if "RETURNING attempt_id" in statement or "resolve_attempt" in statement:
            self.next_id += 1
            return [(self.next_id,)]
        return 1


class SubmitUnitsTests(unittest.TestCase):
    def setUp(self):
        # One clock shared by the batch client and the executor, so the
        # ordering assertions compare readings from the same sequence.
        self.clock = CallClock()
        self.batch = FakeBatchClient(clock=self.clock)
        self.s3 = FakeS3()
        self.execute = RecordingExecute(clock=self.clock)

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
        #
        # AMENDED by FixA (review finding #3): the key is RUN-SCOPED. It used
        # to be the unit's own "<exposure>/<sca>", which is a global identity
        # against a global primary key — so reprocessing one exposure/SCA
        # under a second run hit `ON CONFLICT DO NOTHING` and silently kept
        # the first run's execution binding.
        self._submit(count=1)

        inserts = [params for sql, params in self.execute.statements
                   if "INTO attempts" in sql]
        self.assertTrue(inserts)
        strings = [p for p in inserts[0] if isinstance(p, str)]
        self.assertIn("run-1:science/90000/1", strings)
        self.assertNotIn("090000/01", strings)

    def test_the_run_scoped_key_is_the_one_the_runtime_computes(self):
        # Both sides must agree on the key: the submitter writing the row and
        # the runtime claiming it through the resolver. One function owns the
        # format so a second copy cannot drift.
        #
        # KEYED BY THE DECLARED SUBJECT, NOT `.key` (co-design ruling 2): a
        # science unit's subject is `(job_type, exposure, sca)`, which is
        # "science/90000/1" rather than the zero-padded storage-path key —
        # `dedup_key`'s docstring has the full reasoning, and the crossmatch
        # collision it fixes.
        from submission.manifest import ProcessingUnit
        from submission.routes import JOB_TYPE_SCIENCE

        unit = ProcessingUnit(exposure=90000, sca=1)
        self.assertEqual("run-1:science/90000/1",
                         unit.logical_job_key("run-1", JOB_TYPE_SCIENCE))
        self.assertNotEqual(unit.logical_job_key("run-1", JOB_TYPE_SCIENCE),
                            unit.logical_job_key("run-2", JOB_TYPE_SCIENCE))

    def test_the_rows_are_created_before_submit_job(self):
        # REVIEW FINDING #2, and the reason this seam is one function rather
        # than two calls the VPO makes in sequence. The docstring stated this
        # order as the whole point; the code did the opposite, so a child
        # could start between SubmitJob and the row creation, find no logical
        # job, and be flagged missing_or_contradictory by the resolver.
        self._submit(count=2)

        self.assertTrue(self.execute.statements,
                        "no rows were written at all")
        self.assertTrue(
            self.batch.submitted_at_call is not None,
            "submit_job was never called")
        # Every attempt-row insert precedes the submit_job call.
        self.assertLess(
            self.execute.first_attempt_insert_call,
            self.batch.submitted_at_call,
            "SubmitJob ran before the attempt rows were created — the exact "
            "race the seam exists to prevent")

    def test_the_scheduler_job_ids_are_backfilled_after_submit_job(self):
        # The corollary of creating rows first: Batch has assigned no child
        # ids yet, so the rows are created without them and backfilled once
        # SubmitJob answers.
        self._submit(count=2)

        backfills = [params for sql, params in self.execute.statements
                     if "scheduler_job_id = %s" in sql and "UPDATE" in sql]
        self.assertEqual(2, len(backfills))
        assigned = {p for params in backfills for p in params
                    if isinstance(p, str) and p.startswith("job-")}
        self.assertEqual({"job-parent:0", "job-parent:1"}, assigned)

    def test_a_submit_job_failure_leaves_reconciliation_cases_not_orphans(self):
        # Review finding #2's second half. Rows already exist when SubmitJob
        # fails. They are NOT rolled back: they are correct, they simply have
        # no scheduler job to point at, and the reconciler classifies them at
        # the submission-anchored horizon — the case that horizon exists for.
        # Deleting them would destroy the only evidence work was intended,
        # and would race a child that may in fact be running.
        self.batch.fail_with = RuntimeError("Batch said no")

        with self.assertRaises(seams.SubmissionFailed) as caught:
            self._submit(count=2)

        inserts = [params for sql, params in self.execute.statements
                   if "INTO attempts" in sql]
        self.assertEqual(2, len(inserts),
                         "the pre-created rows must survive the failure")
        self.assertEqual(2, len(caught.exception.attempt_ids))
        self.assertEqual("run-1", caught.exception.run_id)

        deletes = [sql for sql, _ in self.execute.statements
                   if sql.strip().upper().startswith("DELETE")]
        self.assertEqual([], deletes, "rows must not be rolled back")

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

    def test_a_contradictory_attempt_is_finished_not_outstanding(self):
        # missing_or_contradictory is the reconciler's FINAL decision for
        # stores that disagree, not a state on its way somewhere. Treating it
        # as open made the VPO wait out the whole timeout over an attempt whose
        # reconciliation had already completed.
        conn = self._conn({"missing_or_contradictory": 1,
                           "terminal_after_start": 2})
        slept = []

        counts = seams.wait_for_completion(conn, "run-1", sleep=slept.append)

        self.assertEqual({"missing_or_contradictory": 1,
                          "terminal_after_start": 2}, counts)
        self.assertEqual([], slept)

    def test_a_contradictory_attempt_alone_does_not_time_out(self):
        conn = self._conn({"missing_or_contradictory": 1})
        seams.wait_for_completion(conn, "run-1", sleep=lambda _: None,
                                  timeout=50)


class ManifestStoreTests(unittest.TestCase):
    """The manifest is created once and never overwritten."""

    def setUp(self):
        self.s3 = FakeS3()
        self.store = submit.S3ManifestStore("bucket", "submissions",
                                            client=self.s3)
        self.key = self.store.key_for("batch-1")

    def test_the_first_write_creates_it(self):
        uri = self.store.put(self.key, b'{"units": 1}')

        self.assertEqual(f"s3://bucket/{self.key}", uri)
        self.assertEqual(b'{"units": 1}', self.s3.objects[("bucket", self.key)])

    def test_an_identical_replay_is_accepted(self):
        # Re-submitting the SAME manifest is an ordinary replay: the object
        # already there is the intended one, and there is nothing to correct.
        self.store.put(self.key, b'{"units": 1}')

        uri = self.store.put(self.key, b'{"units": 1}')

        self.assertEqual(f"s3://bucket/{self.key}", uri)

    def test_different_content_under_one_identity_is_refused(self):
        # The defect: an unconditional put replaced the manifest while
        # already-submitted children held the old checksum. Those children then
        # fail startup — or, without the checksum guard, resolve the wrong
        # array-index mapping and process another unit's inputs.
        self.store.put(self.key, b'{"units": 1}')

        with self.assertRaises(submit.ManifestConflict) as caught:
            self.store.put(self.key, b'{"units": 2}')

        self.assertIn("different content", str(caught.exception))
        # The original survives: a refused write changes nothing.
        self.assertEqual(b'{"units": 1}', self.s3.objects[("bucket", self.key)])

    def test_a_real_store_fault_is_not_swallowed_as_a_conflict(self):
        def explode(**_kwargs):
            raise RuntimeError("AccessDenied")

        self.s3.put_object = explode

        with self.assertRaises(RuntimeError) as caught:
            self.store.put(self.key, b'{"units": 1}')

        self.assertIn("AccessDenied", str(caught.exception))


class RunRegistrationTests(unittest.TestCase):
    def test_it_calls_the_consumer_and_returns_its_counts(self):
        conn = FakeConnection(rows=[
            attempt_row(1, lifecycle_state="terminal_after_start",
                        started_at=utc(2026, 8, 6, 11, 0, 0),
                        rapid_outcome="success",
                        product_disposition="published",
                        terminal_record_sequence=1)])

        run = seams.run_registration(conn)

        # AMENDED by FixA (review finding #5). With no registrar this is a
        # DECISION pass: the attempt is approved, and approval counts into
        # `would_register`. It must NOT count as `registered` — that
        # conflation is what let the registration job report registered=N
        # while writing nothing.
        self.assertEqual(1, run.would_register)
        self.assertEqual(0, run.registered)
        self.assertEqual(0, run.exit_code)

    def test_a_decision_pass_leaves_every_attempt_a_candidate(self):
        # The watermark advances only on a real registration, so nothing is
        # marked as work that did not happen.
        conn = FakeConnection(rows=[
            attempt_row(1, lifecycle_state="terminal_after_start",
                        started_at=utc(2026, 8, 6, 11, 0, 0),
                        rapid_outcome="success",
                        product_disposition="published",
                        terminal_record_sequence=1)])

        seams.run_registration(conn)

        marks = [text for text, _ in conn.statements
                 if "registered_record_sequence = %s" in text]
        self.assertEqual([], marks,
                         "a decision pass must not advance the watermark")


if __name__ == "__main__":
    unittest.main()
