"""The VPO's three seams: pre-creation order, bounded wait, consumer call."""

import io
import unittest

from observability.attempts import ExecutionBinding
from pipeline import seams
from pipeline.intent.errors import (FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION,
                                    FakePgError)
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
    """Captures every statement the AttemptWriter issues, in order.

    Extended (integration review ruling 13) to also stand in for
    work_units/unit_events: `work_units_by_scope` seeds the SELECT a
    find-or-create issues (keyed by `(job_type, input_scope)`), and
    `fk_missing_job_types` simulates the definition-FK guard `_precreate`
    is written to tolerate — a job type in that set makes an INSERT INTO
    work_units raise, exactly as the real FK does today for every job type
    Both simulated database failures are raised as `FakePgError` carrying a
    real SQLSTATE, because production classifies on SQLSTATE rather than
    message text (`pipeline.intent.errors`) — a double that can only raise a
    hand-crafted message cannot exercise the branch that runs.
    """

    def __init__(self, clock=None):
        self.statements = []
        self.next_id = 100
        self.next_work_unit_id = 1
        self.clock = clock or CallClock()
        #: The clock reading at the first `INSERT INTO attempts`, so a test can
        #: assert the rows were written before SubmitJob was called.
        self.first_attempt_insert_call = None
        #: (job_type, input_scope) -> {"work_unit_id": int, "state": str}.
        #: Empty by default: every unit is a fresh work unit.
        self.work_units_by_scope: dict[tuple[str, str], dict] = {}
        #: Job types whose INSERT INTO work_units raises SQLSTATE 23503,
        #: simulating a missing workflow_definitions row. No longer swallowed
        #: by production (rule 12): a missing definition is a hard error now
        #: that a deployment step loads them.
        self.fk_missing_job_types: set[str] = set()
        #: (job_type, input_scope) pairs whose next INSERT raises SQLSTATE
        #: 23505 and materializes `race_winner_id` as the winning row — the
        #: claim-race loser's view (rule 6).
        self.unique_violation_scopes: set[tuple[str, str]] = set()
        #: The work_unit_id a simulated race winner holds.
        self.race_winner_id = 4242

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
        if "SELECT work_unit_id" in statement and "FROM work_units" in statement:
            job_type, input_scope = params[0], params[1]
            found = self.work_units_by_scope.get((job_type, input_scope))
            if found is None:
                return []
            return [(found["work_unit_id"], job_type, input_scope,
                     "prompt-processing", 1, found["state"], None, None)]
        if "INSERT INTO work_units" in statement:
            job_type, input_scope = params[0], params[1]
            if job_type in self.fk_missing_job_types:
                # A DRIVER-SHAPED ERROR, not a message string. Production
                # classifies by SQLSTATE (`pipeline.intent.errors`), so a
                # double that raises only hand-crafted text cannot exercise
                # the branch that actually runs — the stub-blindness this
                # suite's previous FK test had. `FakePgError` carries a real
                # `pgcode`.
                raise FakePgError(FOREIGN_KEY_VIOLATION,
                                  'insert or update on table "work_units" '
                                  'violates foreign key constraint '
                                  '"work_units_definition_fk"')
            if (job_type, input_scope) in self.unique_violation_scopes:
                # The claim-race loser's view: another transaction created
                # this exact identity first and migration 036's partial
                # unique index refuses ours.
                self.unique_violation_scopes.discard((job_type, input_scope))
                self.work_units_by_scope[(job_type, input_scope)] = {
                    "work_unit_id": self.race_winner_id, "state": "ready"}
                raise FakePgError(UNIQUE_VIOLATION,
                                  'duplicate key value violates unique '
                                  'constraint '
                                  '"work_units_current_identity_uq"')
            work_unit_id = self.next_work_unit_id
            self.next_work_unit_id += 1
            self.work_units_by_scope[(job_type, input_scope)] = {
                "work_unit_id": work_unit_id, "state": "ready"}
            return [(work_unit_id,)]
        if "UPDATE work_units SET state" in statement:
            job_type = None
            for (jt, scope), row in self.work_units_by_scope.items():
                if row["work_unit_id"] == params[-2]:
                    row["state"] = params[0]
                    job_type = jt
            return 1 if job_type is not None else 0
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


class AttachWorkUnitTests(unittest.TestCase):
    """`_precreate` attaches a work_unit_id to every new attempt (ruling 13).

    Extends this file's own `RecordingExecute`/`SubmitUnitsTests` fixtures
    per the task brief's instruction, rather than a parallel test file —
    this class needs exactly the same `submit_units` call shape those tests
    already build.
    """

    def setUp(self):
        self.clock = CallClock()
        self.batch = FakeBatchClient(clock=self.clock)
        self.s3 = FakeS3()
        self.execute = RecordingExecute(clock=self.clock)

    def _submit(self, count=2, job_type="science"):
        return seams.submit_units(
            units(count), job_type=job_type, queue="rapid-queue-prompt",
            job_definition="rapid-pipeline-science", binding=BINDING,
            manifest_bucket="bucket", manifest_prefix="submissions",
            s3_client=self.s3, batch_client=self.batch,
            execute=self.execute, run_id="run-1",
            now=utc(2026, 8, 6, 12, 0, 0))

    def test_every_new_attempt_gets_a_work_unit_id(self):
        self._submit(count=3)

        updates = [params for sql, params in self.execute.statements
                   if "UPDATE attempts SET work_unit_id" in sql]
        self.assertEqual(3, len(updates))

    def test_a_fresh_scope_creates_a_work_unit_in_ready_then_submitted(self):
        self._submit(count=1)

        creates = [(sql, params) for sql, params in self.execute.statements
                   if "INSERT INTO work_units" in sql]
        self.assertEqual(1, len(creates))
        _, create_params = creates[0]
        self.assertIn("ready", create_params)

        transitions = [(sql, params) for sql, params in self.execute.statements
                       if "UPDATE work_units SET state" in sql]
        self.assertEqual(1, len(transitions))
        _, transition_params = transitions[0]
        self.assertIn("submitted", transition_params)
        self.assertIn("ready", transition_params)

    def test_two_units_with_different_subjects_get_different_work_units(self):
        self._submit(count=2)

        creates = [params for sql, params in self.execute.statements
                   if "INSERT INTO work_units" in sql]
        self.assertEqual(2, len(creates))
        scopes = {(p[0], p[1]) for p in creates}
        self.assertEqual(2, len(scopes),
                         "two different (exposure, sca) units must not "
                         "collide on one work-unit scope")

    def test_an_existing_ready_unit_is_reused_not_recreated(self):
        # Simulates the campaign-staging case: a work unit was pre-created
        # (by the mock transformer, part 5) in state 'ready' before
        # submission ever runs. _precreate's find-or-create must find it
        # rather than creating a duplicate under the same scope, which the
        # partial unique index would refuse anyway.
        unit = units(count=1)[0]
        from submission.subjects import subject_for
        subject = subject_for("science").subject_for(unit)
        scope = "/".join(str(c) for c in subject[1:])
        self.execute.work_units_by_scope[("science", scope)] = {
            "work_unit_id": 777, "state": "ready"}

        self._submit(count=1)

        creates = [s for s, _ in self.execute.statements
                   if "INSERT INTO work_units" in s]
        self.assertEqual([], creates,
                         "a pre-existing ready unit must be reused, not recreated")
        updates = [params for sql, params in self.execute.statements
                   if "UPDATE attempts SET work_unit_id" in sql]
        self.assertIn(777, updates[0])

    def test_missing_workflow_definition_is_now_a_hard_error(self):
        # THE INVERSE OF WHAT THIS TEST USED TO ASSERT (rule 12 repair). It
        # previously asserted that a definition-FK violation was SWALLOWED and
        # submission carried on, which is what made the intent layer silently
        # optional — the FK was standing in for a deployment step, and a
        # message-substring match decided what it meant.
        #
        # Definitions are now loaded by an explicit deployment step
        # (`pipeline.intent.definitions.load_definitions`) and verified by the
        # startup completeness check, so reaching work-unit creation with no
        # definition means the deploy was incomplete. That is a fault, and it
        # propagates.
        self.execute.fk_missing_job_types.add("science")

        with self.assertRaises(FakePgError) as caught:
            self._submit(count=2)
        self.assertEqual(FOREIGN_KEY_VIOLATION, caught.exception.pgcode)

    def test_claim_race_loser_resolves_to_the_winning_unit(self):
        # RULE 6, at the seam: the INSERT loses to a concurrent creator, and
        # this caller re-SELECTs the winner and attaches to it rather than
        # letting the unique violation abort the pass. The fake raises a real
        # SQLSTATE 23505 (not a message string), which is what production
        # classifies on.
        from pipeline.seams import _attach_work_unit

        unit = units(count=1)[0]
        scope = seams._input_scope_for("science", unit)
        self.execute.unique_violation_scopes.add(("science", scope))

        _attach_work_unit(self.execute, "science", unit, attempt_id=601,
                          moment=utc(2027, 10, 1))

        updates = [params for sql, params in self.execute.statements
                   if "UPDATE attempts SET work_unit_id" in sql]
        self.assertTrue(updates,
                        "the race loser attached to no work unit at all")
        self.assertIn(self.execute.race_winner_id, updates[0])

    def test_a_unique_violation_whose_winner_vanishes_still_raises(self):
        # The one case that must NOT be papered over: a 23505 whose winning
        # row cannot then be found is a contradiction, not a race, and a
        # blind retry loop there is how a claim path spins forever.
        from pipeline.seams import _attach_work_unit

        unit = units(count=1)[0]
        scope = seams._input_scope_for("science", unit)

        # Raise 23505 but leave the winner unmaterialized.
        original = self.execute.__call__

        def conflicting(statement, params=None):
            if "INSERT INTO work_units" in statement:
                raise FakePgError(UNIQUE_VIOLATION, "duplicate key")
            return original(statement, params)

        with self.assertRaises(FakePgError):
            _attach_work_unit(conflicting, "science", unit, attempt_id=602,
                              moment=utc(2027, 10, 1))
        self.assertIsNotNone(scope)

    def test_find_or_create_race_both_callers_resolve_to_one_work_unit(self):
        # The race-shape test the task brief asks for: two "concurrent"
        # calls over the same (job_type, input_scope) both resolve to the
        # same work_unit_id, and only one actually creates. Simulated here
        # by calling _attach_work_unit twice directly against one shared
        # fake executor — the second call's SELECT now finds what the
        # first call's INSERT created, exactly the re-SELECT-on-conflict
        # shape `_attach_work_unit`'s docstring describes, without needing
        # real thread concurrency to prove the resolution is idempotent.
        from pipeline.seams import _attach_work_unit
        unit = units(count=1)[0]

        _attach_work_unit(self.execute, "science", unit, attempt_id=501,
                          moment=utc(2026, 8, 6, 12, 0, 0))
        _attach_work_unit(self.execute, "science", unit, attempt_id=502,
                          moment=utc(2026, 8, 6, 12, 0, 1))

        creates = [s for s, _ in self.execute.statements
                   if "INSERT INTO work_units" in s]
        self.assertEqual(1, len(creates),
                         "only the first caller creates the work unit")
        updates = [params for sql, params in self.execute.statements
                   if "UPDATE attempts SET work_unit_id" in sql]
        self.assertEqual(2, len(updates))
        self.assertEqual(updates[0][0], updates[1][0],
                         "both attempts attach to the SAME work unit")


class CampaignUnitTransitionIntegrityTests(unittest.TestCase):
    """IR-13-a build spec item 6: the campaign-created unit and the unit
    `_attach_work_unit` later finds at submission time must be the SAME
    row — one shared `input_scope` grammar, tested end to end through the
    ONE `RecordingExecute` instance both `create_mock_campaign_from_staged`
    and `_precreate`/`_attach_work_unit` write through, exactly as a live
    campaign run and its later submission share one database.
    """

    def setUp(self):
        self.execute = RecordingExecute()

    def test_a_gathered_campaign_unit_finds_its_pre_created_row(self):
        from pipeline.intent.writer import CampaignWriter, WorkUnitWriter
        from pipeline.mock.transformer import create_mock_campaign_from_staged
        from pipeline.seams import _attach_work_unit
        from submission.subjects import parse_exposure_sca_scope

        campaign_writer = CampaignWriter(self.execute)
        work_writer = WorkUnitWriter(self.execute)

        class Source:
            exit_code = 0

            def get_l2files_records_for_datetime_range(self, start, end):
                return [(101, 7, 8)]  # (rid, sca, fid)

            def get_info_for_l2file(self, rid):
                # (filename, expid, sca, field) — truncated to what the
                # creator reads.
                return ("s3://in/exp5001_sca7.fits", 5001, 7, 4678622)

        create_mock_campaign_from_staged(
            self.execute, campaign_writer, work_writer, Source(),
            "mock-day-1", start=utc(2027, 10, 1), end=utc(2027, 10, 2),
            max_units=10)

        creates_before = [s for s, _ in self.execute.statements
                          if "INSERT INTO work_units" in s]
        self.assertEqual(1, len(creates_before),
                         "the campaign creates exactly one work unit")

        # The campaign gatherer's own downstream consequence: a science
        # ProcessingUnit at the SAME (exposure, sca) the campaign unit's
        # input_scope names — the exact identity gather_campaign_units
        # would have parsed back via parse_exposure_sca_scope. Built
        # directly (not via this file's `units()` helper, whose sca is a
        # positional `(i % 18) + 1` unrelated to the campaign's own 5001/7).
        gathered_unit = ProcessingUnit(
            exposure=5001, sca=7,
            facts=UnitFacts(rid=101, fid=8, field=4678622, expid=5001))

        _attach_work_unit(self.execute, "science", gathered_unit,
                          attempt_id=9001, moment=utc(2027, 10, 1, 1))

        # THE ASSERTION THAT MATTERS: no SECOND work unit was minted — the
        # find-or-create SELECT found the campaign's own row.
        creates_after = [s for s, _ in self.execute.statements
                         if "INSERT INTO work_units" in s]
        self.assertEqual(1, len(creates_after),
                         "the gathered unit must FIND the campaign's work "
                         "unit, not create a second one")

        # And the campaign's own work unit is the one now transitioned
        # ready->submitted and attached to the new attempt.
        [(job_type, campaign_scope)] = self.execute.work_units_by_scope.keys()
        self.assertEqual(parse_exposure_sca_scope(campaign_scope), (5001, 7))
        campaign_work_unit_id = self.execute.work_units_by_scope[
            (job_type, campaign_scope)]["work_unit_id"]

        updates = [params for sql, params in self.execute.statements
                   if "UPDATE attempts SET work_unit_id" in sql]
        self.assertEqual(1, len(updates))
        self.assertIn(campaign_work_unit_id, updates[0])

        transitions = [params for sql, params in self.execute.statements
                       if "UPDATE work_units SET state" in sql]
        self.assertEqual(1, len(transitions))
        self.assertIn("submitted", transitions[0])


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
