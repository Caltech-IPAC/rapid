"""The VPO's three seams: pre-creation order, bounded wait, consumer call."""

import io
import unittest

from observability.attempts import ExecutionBinding
from pipeline import seams
from pipeline.intent.errors import (FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION,
                                    FakePgError)
from pipeline.reconciler.test.stubs import FakeConnection, attempt_row, utc
from submission import submit
from submission.manifest import ProcessingUnit
from submission.routes import JOB_TYPE_SCIENCE
from submission.test import payload_fixtures as fixtures


def units(count=2, base=90000):
    return [ProcessingUnit(
                payload=fixtures.science_payload(
                    exposure=base + i, sca=(i % 18) + 1,
                    rid=1, fid=1, field=1, expid=base + i))
            for i in range(count)]


BINDING = ExecutionBinding(
    job_definition_arn="arn:aws:batch:us-east-1:1:job-definition/x:10",
    job_definition_rev=10, image_digest="sha256:abc",
    release_identity="rel-1", manifest_checksum="placeholder")

#: A sentinel distinguishing "the caller passed no `protocol_commit`
#: override" from "the caller explicitly passed None" (which means "do not
#: commit", a real, distinct case `submit_units`'s docstring documents).
_MISSING = object()


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

    Extended again (fix-txn-core, findings 2-4) to stand in for the
    `submissions` table too. Before this, `_open_submission`'s
    `protocol.is_available` probe and `protocol.prepare`'s
    `RETURNING submission_id` fell through to the unconditional `return 1`
    below — a bare rowcount, not the `[(submission_id,)]` row shape
    `_single_value` needs — and every test in this class that reached
    `_open_submission` was silently surviving that via the OLD
    catch-and-degrade behaviour `_open_submission` used to have: the
    resulting `TypeError` was swallowed by its blanket `except Exception`
    and logged as "the submission proceeds without one". Finding 3 narrows
    that catch to `is_available` alone, so a double this thin now surfaces
    the `TypeError` as a real test failure instead of a silently-degraded
    pass — which is precisely the "no more silently swallowing a broken
    protocol write" behaviour the finding exists to produce, and exactly why
    this double needed to grow up rather than the fix being narrowed back
    down to keep it passing.

    `submissions_available` defaults to True (DRAFT 044 IS applied, this
    class's ordinary posture) with the full six-state machine reachable
    through `submissions_by_id`; set it False to exercise the pre-protocol
    degrade path this class predates.

    Extended again (migration 060, finding 4's FOUND-recovery close) to
    model `attempts.array_index` and `attempts.scheduler_job_id` well enough
    for `submission.protocol.backfill_found_children`'s two statements
    (`_COUNT_UNBOUND_CHILDREN_SQL`, `_BACKFILL_FOUND_CHILDREN_SQL`) to read
    and write real per-row state, via `attempts_by_id`. `INSERT INTO
    attempts` now seeds a row there keyed by the id it returns, recording
    the array_index the INSERT carries as its last parameter (matching
    `AttemptWriter.create_submitted`'s column list) — so a test that submits
    through `submit_units` and then resolves FOUND exercises the same rows
    end to end, rather than a second, independently-seeded fixture.
    """

    def __init__(self, clock=None, submissions_available=True):
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
        #: Whether DRAFT 044 (submissions) is "applied" in this double —
        #: `protocol.is_available`'s answer. False exercises the
        #: pre-protocol degrade path `_open_submission` still has.
        self.submissions_available = submissions_available
        self.next_submission_id = 900
        #: submission_id -> {"state": str, "attempt_ids": set[int]}.
        self.submissions_by_id: dict[int, dict] = {}
        #: When set, `attach_attempts`'s UPDATE reports one row short of what
        #: was asked — simulating the "some attempt was not linked" case
        #: finding 3's hard count-check exists to catch.
        self.attach_short_by: int = 0
        #: When set, `mark_bound`'s UPDATE (the WHERE state = 'calling' CAS)
        #: matches nothing — simulating a lost race or a wrong submission_id.
        self.fail_mark_bound = False
        #: How many of the NEXT scheduler-id backfill UPDATEs report a
        #: zero rowcount (the SQL guard's no-op case), simulating a short
        #: backfill for finding 4's hard-check tests.
        self.backfill_short_by_rows: int = 0
        #: attempt_id -> {"scheduler_job_id": str|None, "array_index":
        #: int|None, "submission_id": int|None}. Seeded by `INSERT INTO
        #: attempts` (migration 060: the array_index INSERT carries is
        #: recorded here) and updated by `attach_attempts`'s UPDATE and by
        #: `backfill_found_children`'s count/UPDATE pair — the state a FOUND
        #: recovery test needs to exist independent of a real database.
        self.attempts_by_id: dict[int, dict] = {}
        #: The next id `INSERT INTO campaigns ... RETURNING campaign_id`
        #: hands back, so `CampaignWriter.create_campaign`'s `_single_value`
        #: read gets a real row shape rather than a bare rowcount (see the
        #: fallthrough's docstring note below — this is the exact statement
        #: that fallthrough was silently mis-modeling).
        self.next_campaign_id = 1
        #: Every `unit_events`/`campaigns` row this double has recorded,
        #: for a test that wants to assert on them directly rather than
        #: only on `work_units_by_scope`'s derived state.
        self.unit_events: list[tuple] = []

    def __call__(self, statement, params=None):
        call = self.clock.tick()
        self.statements.append((statement, params))
        if statement.startswith("INSERT INTO attempts"):
            if self.first_attempt_insert_call is None:
                self.first_attempt_insert_call = call
            self.next_id += 1
            # `array_index` is this INSERT's last bound parameter
            # (`AttemptWriter.create_submitted`'s column list); `scheduler_
            # job_id` is its fourth (after schema_version, run_id,
            # logical_job_id) — read positionally rather than re-parsing the
            # SQL text, matching how this double already treats every other
            # INSERT here.
            self.attempts_by_id[self.next_id] = {
                "scheduler_job_id": params[3] if params else None,
                "array_index": params[-1] if params else None,
                "submission_id": None,
            }
            return [(self.next_id,)]
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
        if "derived.transition_work_unit" in statement:
            # C1 (campaign ruling R5, migration 077): `WorkUnitWriter.
            # transition_unit` now issues this ONE call in place of the old
            # `UPDATE work_units SET state ...` — the CAS, the advisory
            # lock, and the `unit_events` append all live behind it. Params
            # are the eight positional args `transition_unit` passes:
            # `work_unit_id, from_state, to_state, writer, blocked_reason,
            # reason, detail, lock`. This double models `work_units` as
            # data (unlike `pipeline.reconciler.test.stubs`'s thinner
            # double, which does not), so the CAS is applied for real
            # against `work_units_by_scope` rather than reported
            # unconditionally: `transition_unit` itself no longer checks
            # this call's return value (only whether it raised), so what
            # matters here is the state mutation the test assertions read
            # back, not the return shape.
            work_unit_id, from_state = params[0], params[1]
            job_type = None
            for (jt, scope), row in self.work_units_by_scope.items():
                if row["work_unit_id"] == work_unit_id:
                    if row["state"] == from_state:
                        row["state"] = params[2]
                        job_type = jt
                    break
            return [(None,)] if job_type is not None else 0
        # -- submission/protocol.py's statements (findings 2-4) -------------
        if "information_schema.tables" in statement:
            return [(1,)] if self.submissions_available else []
        if "INSERT INTO submissions" in statement:
            submission_id = self.next_submission_id
            self.next_submission_id += 1
            self.submissions_by_id[submission_id] = {
                "state": "prepared", "attempt_ids": set()}
            return [(submission_id,)]
        if "UPDATE attempts SET submission_id" in statement:
            submission_id, attempt_ids = params
            row = self.submissions_by_id[submission_id]
            attached = list(attempt_ids)
            if self.attach_short_by:
                attached = attached[:len(attached) - self.attach_short_by]
            row["attempt_ids"].update(attached)
            for attempt_id in attached:
                if attempt_id in self.attempts_by_id:
                    self.attempts_by_id[attempt_id]["submission_id"] = \
                        submission_id
            return len(attached)
        if "SET state = 'calling'" in statement:
            submission_id = params[-1]
            row = self.submissions_by_id.get(submission_id)
            if row is None or row["state"] != "prepared":
                return 0
            row["state"] = "calling"
            return 1
        if "SET state = 'bound'" in statement:
            submission_id = params[-1]
            row = self.submissions_by_id.get(submission_id)
            if self.fail_mark_bound or row is None or row["state"] != "calling":
                return 0
            row["state"] = "bound"
            row["scheduler_job_id"] = params[0]
            return 1
        if "SET state = 'unknown'" in statement:
            submission_id = params[-1]
            row = self.submissions_by_id.get(submission_id)
            if row is None or row["state"] != "calling":
                return 0
            row["state"] = "unknown"
            return 1
        if "SET state = 'found'" in statement:
            # `protocol.mark_found`: CALLING or UNKNOWN -> FOUND, the
            # resolution pass's positive outcome (migration 060's
            # `FoundRecoveryTests` is this handler's first exerciser — the
            # class's own docstring on `resolve`'s FOUND branch had no
            # coverage through this double before).
            submission_id = params[-1]
            row = self.submissions_by_id.get(submission_id)
            if row is None or row["state"] not in ("calling", "unknown"):
                return 0
            row["state"] = "found"
            row["scheduler_job_id"] = params[0]
            return 1
        if "RETURNING attempt_id" in statement or "resolve_attempt" in statement:
            self.next_id += 1
            return [(self.next_id,)]
        if statement.startswith("UPDATE attempts SET scheduler_job_id"):
            if "|| array_index" in statement:
                # `submission.protocol._BACKFILL_FOUND_CHILDREN_SQL` (migration
                # 060, FOUND recovery): set-based, one UPDATE for every
                # attached child still missing a scheduler id AND carrying a
                # recorded array_index. Rows with no recorded array_index
                # (simulating a pre-060 attempt) are the guard's job to skip,
                # exactly as the real `WHERE ... array_index IS NOT NULL`
                # does — this double applies the same two-part WHERE against
                # `attempts_by_id` rather than trusting a fixed return value.
                scheduler_job_id, submission_id = params
                updated = 0
                for row in self.attempts_by_id.values():
                    if (row["submission_id"] == submission_id
                            and row["scheduler_job_id"] is None
                            and row["array_index"] is not None):
                        row["scheduler_job_id"] = (
                            f"{scheduler_job_id}:{row['array_index']}")
                        updated += 1
                return updated
            # The direct-path array-child backfill (`AttemptWriter.
            # backfill_scheduler_job_ids`). `backfill_short_by_rows` lets a
            # test simulate the SQL-guarded no-op case (`WHERE
            # scheduler_job_id IS NULL` matched nothing) for the first N
            # calls, without reaching for a mock — finding 4's hard-check on
            # a short backfill is exercised through the real return-value
            # contract `_rowcount` reads, exactly as production does.
            if self.backfill_short_by_rows:
                self.backfill_short_by_rows -= 1
                return 0
            scheduler_job_id, attempt_id = params
            if attempt_id in self.attempts_by_id:
                self.attempts_by_id[attempt_id]["scheduler_job_id"] = \
                    scheduler_job_id
            return 1
        if statement.startswith("SELECT count(*), count(array_index)"):
            # `submission.protocol._COUNT_UNBOUND_CHILDREN_SQL`: how many of
            # this submission's attached attempts still have no scheduler
            # id, and how many of THOSE also carry a recorded array_index —
            # the split `backfill_found_children` validates its own UPDATE
            # count against.
            submission_id = params[0]
            unbound = [row for row in self.attempts_by_id.values()
                      if row["submission_id"] == submission_id
                      and row["scheduler_job_id"] is None]
            with_index = [row for row in unbound
                         if row["array_index"] is not None]
            return [(len(unbound), len(with_index))]
        if statement.startswith("UPDATE attempts SET work_unit_id"):
            # `pipeline.seams._attach_work_unit`'s final step: filling in
            # the FK on an attempt row that already exists, not a new
            # attempt. Recorded onto `attempts_by_id` so a test asserting
            # on that dict (rather than only on the raw statement list)
            # sees the attachment too.
            work_unit_id, attempt_id = params
            if attempt_id in self.attempts_by_id:
                self.attempts_by_id[attempt_id]["work_unit_id"] = work_unit_id
            return 1
        if "INSERT INTO campaigns" in statement:
            # `CampaignWriter.create_campaign`'s `RETURNING campaign_id`.
            # THE STATEMENT THE OLD BLANKET `return 1` FALLTHROUGH WAS
            # SILENTLY MIS-MODELING (near-miss on record: commit d96a3261,
            # caught only by count assertions on a DIFFERENT call). Reading
            # `_single_value`'s own fallback branch shows why the old
            # fallthrough passed: `_single_value(1)` is not None, not a
            # list/tuple, not a dict, so it falls to `return rows` and hands
            # back the bare int `1` as if it were a real campaign_id — every
            # test using this double's `create_campaign` was silently
            # getting `campaign_id == 1` regardless of what was "inserted",
            # rather than a row shape a real cursor would return. This
            # route returns the real `[(campaign_id,)]` row shape instead.
            campaign_id = self.next_campaign_id
            self.next_campaign_id += 1
            return [(campaign_id,)]
        if "INSERT INTO unit_events" in statement:
            # `WorkUnitWriter._record_event`: no RETURNING clause, so the
            # real contract is a bare rowcount — but recorded here (rather
            # than silently accepted by the old fallthrough) so a test can
            # assert on the event history the way `fixture.unit_events`
            # does for the contract tier.
            self.unit_events.append(tuple(params) if params else ())
            return 1
        raise AssertionError(
            f"RecordingExecute has no route for this statement — a stub "
            f"that silently returns 1 for anything unmatched is exactly "
            f"the near-miss on record (commit d96a3261): {statement!r} "
            f"params={params!r}")


class SubmitUnitsTests(unittest.TestCase):
    def setUp(self):
        # One clock shared by the batch client and the executor, so the
        # ordering assertions compare readings from the same sequence.
        self.clock = CallClock()
        self.batch = FakeBatchClient(clock=self.clock)
        self.s3 = FakeS3()
        self.execute = RecordingExecute(clock=self.clock)
        #: Calls to `protocol_commit`, recorded against the shared clock so a
        #: test can order a commit relative to `submit_job` and to the
        #: statements in `self.execute.statements` (fix-txn-core, findings
        #: 2-4: `protocol_commit` is now called TWICE per successful
        #: submission — once before SubmitJob, once after the post-Batch
        #: bookkeeping — and both instants are asserted below).
        self.commits: list[int] = []

    def _commit(self):
        self.commits.append(self.clock.tick())

    def _submit(self, count=2, protocol_commit=_MISSING):
        if protocol_commit is _MISSING:
            protocol_commit = self._commit
        return seams.submit_units(
            units(count), job_type="science", queue="rapid-queue-prompt",
            job_definition="rapid-pipeline-science", binding=BINDING,
            manifest_bucket="bucket", manifest_prefix="submissions",
            s3_client=self.s3, batch_client=self.batch,
            execute=self.execute, run_id="run-1",
            now=utc(2026, 8, 6, 12, 0, 0),
            protocol_commit=protocol_commit)

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

        unit = ProcessingUnit(
            payload=fixtures.science_payload(exposure=90000, sca=1))
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

        # NARROWED to `UPDATE attempts` specifically (fix-txn-core): with the
        # submission-protocol path now actually reachable in this double
        # (previously `_open_submission` silently failed and never got this
        # far — see `RecordingExecute`'s docstring), `protocol.mark_bound`'s
        # `UPDATE submissions SET state = 'bound', scheduler_job_id = %s...`
        # ALSO matches the old, looser "scheduler_job_id = %s" + "UPDATE"
        # filter, which is a different write on a different table and not
        # what this test means to count.
        backfills = [params for sql, params in self.execute.statements
                     if sql.startswith("UPDATE attempts")
                     and "scheduler_job_id = %s" in sql]
        self.assertEqual(2, len(backfills))
        assigned = {p for params in backfills for p in params
                    if isinstance(p, str) and p.startswith("job-")}
        self.assertEqual({"job-parent:0", "job-parent:1"}, assigned)

    def test_array_index_is_recorded_at_precreate_in_manifest_order(self):
        # Migration 060 (finding 4's FOUND-recovery gap). `_precreate` writes
        # each row's array position — the SAME `enumerate(manifest.units)`
        # position `_bind_scheduler_jobs` later re-derives Batch child ids
        # from — so a later FOUND recovery pass has it to read back, rather
        # than only the in-memory loop that created the rows ever knowing it.
        self._submit(count=3)

        inserts = [params for sql, params in self.execute.statements
                   if sql.startswith("INSERT INTO attempts")]
        self.assertEqual(3, len(inserts))
        # `array_index` is the INSERT's last bound parameter (see
        # `AttemptWriter.create_submitted`'s column list).
        self.assertEqual([0, 1, 2], [params[-1] for params in inserts])

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

    # -- fix-txn-core: protocol_commit's two boundaries (findings 2-4) ------

    def test_protocol_commit_is_called_before_and_after_submit_job(self):
        # `submit_units`'s docstring names two commit instants: between
        # marking `calling` and calling Batch (rule 7's durability
        # requirement), and after the post-Batch bookkeeping (finding 4's
        # atomic mark-bound-and-backfill). Both must fire, in that order,
        # bracketing the one `submit_job` call — never zero, never one,
        # never after a third unrelated point.
        self._submit(count=2)

        self.assertEqual(2, len(self.commits))
        first_commit, second_commit = self.commits
        self.assertLess(first_commit, self.batch.submitted_at_call,
                        "the `calling` marker must commit before SubmitJob")
        self.assertGreater(second_commit, self.batch.submitted_at_call,
                           "the bound+backfill commit must follow SubmitJob")

    def test_no_protocol_commit_supplied_means_no_commit_is_ever_called(self):
        # `protocol_commit=None` is a real, documented case (every caller
        # predating this brief): the protocol's writes simply ride in the
        # caller's own transaction, uncommitted by this function. A
        # submission still succeeds end to end; `self.commits` (fed only by
        # the `self._commit` this test deliberately did NOT pass in) stays
        # empty, proving `submit_units` never reaches for a commit callable
        # of its own when the caller supplied none.
        submission, attempt_ids = self._submit(count=1, protocol_commit=None)

        self.assertEqual(1, len(attempt_ids))
        self.assertEqual("job-parent", submission.job_id)
        self.assertEqual([], self.commits)

    def test_a_short_attach_aborts_before_submit_job_is_ever_called(self):
        # FINDING 3. `attach_attempts`'s reported count is now hard-checked:
        # a short attach means the submission row would be PREPARED but not
        # fully wired to its attempts, invisible to every resolution pass
        # (`open_submissions` never selects `prepared`), so this must abort
        # BEFORE `submit_batch` runs rather than silently degrade and
        # proceed — the narrowed contract `_open_submission`'s docstring
        # describes.
        from submission.protocol import SubmissionProtocolError

        self.execute.attach_short_by = 1

        with self.assertRaises(SubmissionProtocolError):
            self._submit(count=2)

        self.assertIsNone(
            self.batch.submitted_at_call,
            "SubmitJob must never be called once the protocol row is known "
            "to be only partially wired to its attempts")

    def test_a_short_backfill_raises_rather_than_warns(self):
        # FINDING 4. `_bind_scheduler_jobs` used to `logger.warning` and
        # return a short count; a caller that never checked the return value
        # (the live path, before this brief) sailed on with `bound` durably
        # recorded and one or more children permanently unaddressable by
        # scheduler id. It must now raise `SubmissionBookkeepingFailed`
        # instead — loud, typed, and distinct from `SubmissionFailed`
        # because Batch DID accept the job here; `submit_job` must never be
        # called again for it. Driven through the double's real return-value
        # contract (`self.execute.backfill_short_by_rows`), the same
        # SQL-guard-matched-nothing shape `AttemptWriter.
        # backfill_scheduler_job_ids` reads in production — not a mock of
        # the writer itself.
        self.execute.backfill_short_by_rows = 1

        with self.assertRaises(seams.SubmissionBookkeepingFailed) as caught:
            self._submit(count=2)

        self.assertEqual("run-1", caught.exception.run_id)
        self.assertEqual("job-parent", caught.exception.scheduler_job_id)
        # The second (post-Batch) commit must NOT have fired: a caller
        # relying on `protocol_commit` never sees `bound` become durable
        # over an incomplete backfill.
        self.assertEqual(1, len(self.commits),
                         "only the pre-Batch commit may have fired")


class FoundRecoveryTests(unittest.TestCase):
    """Migration 060: FOUND recovery backfills array children (finding 4's
    second half).

    Drives real pre-created rows through `submit_units` (so `array_index` is
    recorded exactly as the live path records it), then simulates the
    scenario the migration exists for — Batch accepted the parent but the
    process died before the direct-path backfill ran — by clearing the
    rows' `scheduler_job_id` and the submission's state back to `unknown`,
    and calls `submission.protocol.resolve` directly against the SAME
    `RecordingExecute`, exactly as a resolution pass would.
    """

    def setUp(self):
        self.clock = CallClock()
        self.batch = FakeBatchClient(clock=self.clock)
        self.s3 = FakeS3()
        self.execute = RecordingExecute(clock=self.clock)

    def _submit_and_orphan(self, count):
        """Submit `count` array children, then simulate a lost backfill.

        Returns `(submission_id, attempt_ids)`. The submission row is left
        `unknown` (as `_mark_submission_unknown` would leave a genuinely
        ambiguous call) and every attached attempt's `scheduler_job_id` is
        cleared back to NULL — the direct-path backfill DID run inside
        `submit_units` (there is no other way to get real `array_index`-
        bearing rows through this double), so this undoes only that one
        write, leaving `array_index` itself untouched, which is the fact
        this whole mechanism depends on having survived.
        """
        submission, attempt_ids = seams.submit_units(
            units(count), job_type="science", queue="rapid-queue-prompt",
            job_definition="rapid-pipeline-science", binding=BINDING,
            manifest_bucket="bucket", manifest_prefix="submissions",
            s3_client=self.s3, batch_client=self.batch,
            execute=self.execute, run_id="run-1",
            now=utc(2026, 8, 6, 12, 0, 0), protocol_commit=None)

        submission_id = next(
            sid for sid, row in self.execute.submissions_by_id.items()
            if row["state"] == "bound")
        self.execute.submissions_by_id[submission_id]["state"] = "unknown"
        for attempt_id in attempt_ids:
            self.execute.attempts_by_id[attempt_id]["scheduler_job_id"] = None
        return submission_id, submission, attempt_ids

    def _row_for(self, submission_id, submission, array_size):
        return {
            "submission_id": submission_id, "job_name": "irrelevant",
            "job_queue": "rapid-queue-prompt", "array_size": array_size,
        }

    def test_found_recovery_backfills_exactly_the_null_scheduler_id_children(
            self):
        submission_id, submission, attempt_ids = self._submit_and_orphan(3)
        row = self._row_for(submission_id, submission, array_size=3)

        from submission import protocol
        outcome = protocol.resolve(
            self.execute, row, describe=lambda name, queue: submission.job_id)

        self.assertEqual(protocol.FOUND, outcome)
        recovered = [self.execute.attempts_by_id[aid]["scheduler_job_id"]
                    for aid in attempt_ids]
        self.assertEqual(
            [f"{submission.job_id}:{i}" for i in range(3)], recovered)
        self.assertEqual(
            "found", self.execute.submissions_by_id[submission_id]["state"])

    def test_found_recovery_validates_the_updated_row_count(self):
        # `backfill_found_children` returns (updated, expected); resolve()
        # logs a warning rather than raising when they disagree (a FOUND
        # submission must still be recorded FOUND — the recovery gap is not
        # a reason to leave the submission open), but the two counts must
        # still be independently readable and correct.
        from submission import protocol

        submission_id, submission, _attempt_ids = self._submit_and_orphan(2)

        updated, expected = protocol.backfill_found_children(
            self.execute, submission_id, submission.job_id)

        self.assertEqual(2, expected)
        self.assertEqual(2, updated)

    def test_null_array_index_rows_are_skipped_not_guessed(self):
        # A row that predates migration 060 carries no array_index. FOUND
        # recovery must leave it exactly as it was — NULL scheduler_job_id —
        # rather than guessing a position for it.
        from submission import protocol

        submission_id, submission, attempt_ids = self._submit_and_orphan(3)
        # Simulate one pre-060 row among the three: its array_index was
        # never recorded.
        pre_060_id = attempt_ids[1]
        self.execute.attempts_by_id[pre_060_id]["array_index"] = None

        updated, expected = protocol.backfill_found_children(
            self.execute, submission_id, submission.job_id)

        self.assertEqual(2, expected, "only the two indexed rows count")
        self.assertEqual(2, updated)
        self.assertIsNone(
            self.execute.attempts_by_id[pre_060_id]["scheduler_job_id"],
            "a row with no recorded array_index must be left NULL, never "
            "guessed at")
        other_ids = [aid for aid in attempt_ids if aid != pre_060_id]
        for attempt_id in other_ids:
            self.assertIsNotNone(
                self.execute.attempts_by_id[attempt_id]["scheduler_job_id"])

    def test_single_job_submissions_are_never_sent_through_the_array_backfill(
            self):
        # array_size == 1: `resolve`'s dispatch must not call
        # backfill_found_children at all — a single job's own scheduler id
        # is the bare parent id, not `<parent>:0`, which the backfill
        # formula would produce if it ran.
        from submission import protocol

        submission_id, submission, attempt_ids = self._submit_and_orphan(1)
        row = self._row_for(submission_id, submission, array_size=1)

        outcome = protocol.resolve(
            self.execute, row, describe=lambda name, queue: submission.job_id)

        self.assertEqual(protocol.FOUND, outcome)
        # The one attempt's scheduler_job_id stays NULL: nothing in this
        # test path re-derives a single-job id from the parent, and the
        # direct (never-ambiguous) backfill is what normally supplies it.
        self.assertIsNone(
            self.execute.attempts_by_id[attempt_ids[0]]["scheduler_job_id"])
        count_calls = [sql for sql, _ in self.execute.statements
                      if sql.startswith("SELECT count(*), count(array_index)")]
        self.assertEqual(
            [], count_calls,
            "backfill_found_children must not even be asked for an "
            "array_size == 1 submission")


class SubmissionAuthorizationTests(unittest.TestCase):
    """Finding 1 (fix-state-gate), exercised through `submit_units` itself
    rather than at the `_attach_work_unit`/`_decide_work_unit` unit level:
    a unit whose work unit is not `ready` must never reach Batch, and never
    get an attempt row, however it arrived in the gathered list — a stale
    poll, a second operator replica, or a re-gathered blocked/completed
    subject.
    """

    def setUp(self):
        self.clock = CallClock()
        self.batch = FakeBatchClient(clock=self.clock)
        self.s3 = FakeS3()
        self.execute = RecordingExecute(clock=self.clock)

    def _submit(self, unit_list, **overrides):
        kwargs = dict(
            job_type="science", queue="rapid-queue-prompt",
            job_definition="rapid-pipeline-science", binding=BINDING,
            manifest_bucket="bucket", manifest_prefix="submissions",
            s3_client=self.s3, batch_client=self.batch,
            execute=self.execute, run_id="run-1",
            now=utc(2026, 8, 6, 12, 0, 0))
        kwargs.update(overrides)
        return seams.submit_units(unit_list, **kwargs)

    def _seed_scope(self, unit, state, work_unit_id=555):
        from submission.subjects import subject_for
        subject = subject_for("science").subject_for(unit)
        scope = "/".join(str(c) for c in subject[1:])
        self.execute.work_units_by_scope[("science", scope)] = {
            "work_unit_id": work_unit_id, "state": state}
        return scope

    def test_an_already_submitted_units_subject_is_excluded_not_resubmitted(self):
        # THE DEFECT, DIRECTLY: a re-gathered subject whose work unit is
        # already `submitted` (a stale gathered list, or a second operator
        # replica that already claimed it) must not get a second attempt
        # row or a second Batch job.
        unit = units(count=1)[0]
        self._seed_scope(unit, "submitted")

        with self.assertRaises(ValueError):
            # Manifest() itself refuses an empty unit list — the whole
            # batch had exactly one unit and it was excluded, so there is
            # nothing left to submit. This is the correct outcome, not an
            # incidental one: see the "everything excluded" test below for
            # the assertion on WHY nothing was submitted.
            self._submit([unit])

        self.assertIsNone(self.batch.submitted_at_call,
                          "SubmitJob must never be called for a unit whose "
                          "work unit is not ready")
        creates = [s for s, _ in self.execute.statements
                  if "INSERT INTO attempts" in s]
        self.assertEqual([], creates,
                         "an unauthorized unit must get no attempt row")

    def test_blocked_and_complete_subjects_are_also_excluded(self):
        from submission.subjects import subject_for

        for state in ("blocked", "complete", "failed", "quarantined"):
            with self.subTest(state=state):
                execute = RecordingExecute(clock=CallClock())
                unit = units(count=1)[0]
                subject = subject_for("science").subject_for(unit)
                scope = "/".join(str(c) for c in subject[1:])
                execute.work_units_by_scope[("science", scope)] = {
                    "work_unit_id": 555, "state": state}
                batch = FakeBatchClient()

                with self.assertRaises(ValueError):
                    seams.submit_units(
                        [unit], job_type="science", queue="rapid-queue-prompt",
                        job_definition="rapid-pipeline-science",
                        binding=BINDING, manifest_bucket="bucket",
                        manifest_prefix="submissions", s3_client=FakeS3(),
                        batch_client=batch, execute=execute, run_id="run-1",
                        now=utc(2026, 8, 6, 12, 0, 0))

                self.assertIsNone(batch.submitted_at_call)

    def test_a_mixed_batch_submits_only_the_authorized_units(self):
        # The realistic shape: gathering returns several units, one of
        # which is a stale re-offer of an already-submitted subject. The
        # manifest built and the array job submitted must contain only the
        # units this call is authorized to submit — not zero (the other
        # units ARE ready) and not all of them (the stale one is not).
        fresh_units = units(count=2, base=91000)
        stale_unit = units(count=1, base=90000)[0]
        self._seed_scope(stale_unit, "submitted", work_unit_id=555)

        submission, attempt_ids = self._submit(
            fresh_units + [stale_unit])

        self.assertEqual(2, submission.array_size,
                         "only the two fresh units are in the array job")
        self.assertEqual(2, len(attempt_ids))
        self.assertIsNotNone(self.batch.submitted_at_call)

    def test_a_fresh_unit_alongside_a_blocked_one_is_still_authorized(self):
        # The gate must not be all-or-nothing at the batch level: excluding
        # one unauthorized unit must not also exclude its authorized
        # batch-mates.
        fresh_unit = units(count=1, base=91000)[0]
        blocked_unit = units(count=1, base=90000)[0]
        self._seed_scope(blocked_unit, "blocked", work_unit_id=556)

        submission, attempt_ids = self._submit([fresh_unit, blocked_unit])

        self.assertEqual(1, submission.array_size)
        self.assertEqual(1, len(attempt_ids))

    def test_a_pre_existing_ready_unit_is_still_authorized_and_transitions(self):
        # The positive case, asserted alongside the negative ones above so
        # a regression that excludes EVERYTHING (not just non-ready units)
        # would fail here: a unit whose work unit already exists in
        # 'ready' (the campaign-staging shape) is authorized, transitions
        # ready->submitted, and reaches Batch exactly as a fresh unit does.
        unit = units(count=1)[0]
        self._seed_scope(unit, "ready", work_unit_id=777)

        submission, attempt_ids = self._submit([unit])

        self.assertEqual(1, submission.array_size)
        self.assertEqual(1, len(attempt_ids))
        # C1: the transition is now one call to `derived.transition_work_unit`
        # (migration 077), not a raw `UPDATE work_units SET state`.
        transitions = [params for sql, params in self.execute.statements
                      if "derived.transition_work_unit" in sql]
        self.assertEqual(1, len(transitions))
        self.assertIn("submitted", transitions[0])
        self.assertIn(777, transitions[0])

    def test_a_second_replicas_submit_units_call_defers_to_the_first(self):
        # THE STATED SCENARIO: "two operator replicas... submits duplicate
        # Batch jobs." Simulated as two submit_units calls sharing one
        # execute/database double — the second call's unit is the SAME
        # (job_type, input_scope) the first call's submit_units already
        # transitioned to 'submitted'.
        unit = units(count=1)[0]

        first_submission, first_attempts = self._submit([unit])
        self.assertEqual(1, first_submission.array_size)
        self.assertEqual(1, len(first_attempts))

        second_batch = FakeBatchClient(clock=self.clock)
        with self.assertRaises(ValueError):
            seams.submit_units(
                [unit], job_type="science", queue="rapid-queue-prompt",
                job_definition="rapid-pipeline-science", binding=BINDING,
                manifest_bucket="bucket", manifest_prefix="submissions",
                s3_client=self.s3, batch_client=second_batch,
                execute=self.execute, run_id="run-2",
                now=utc(2026, 8, 6, 12, 5, 0))

        self.assertIsNone(second_batch.submitted_at_call,
                          "the second replica must never call SubmitJob "
                          "for a unit the first replica already submitted")


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

        # C1: one call to `derived.transition_work_unit` (migration 077)
        # replaces the old raw `UPDATE work_units SET state`.
        transitions = [(sql, params) for sql, params in self.execute.statements
                       if "derived.transition_work_unit" in sql]
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
        # shape `_decide_work_unit`'s docstring describes, without needing
        # real thread concurrency to prove the resolution is idempotent.
        #
        # **ONLY ONE CALLER IS AUTHORIZED (finding 1, fix-state-gate).**
        # This test used to assert that BOTH calls attached their attempt's
        # FK to the shared work unit — which was finding 1's bug stated as
        # a passing test: the second call finds the unit already
        # `submitted` by the first and, before this fix, attached to it
        # anyway. Only the first caller (the `ready -> submitted` CAS
        # winner) may attach; the second must be deferred, not attached to
        # a unit it does not own.
        from pipeline.seams import _attach_work_unit
        unit = units(count=1)[0]

        first_id = _attach_work_unit(self.execute, "science", unit,
                                     attempt_id=501,
                                     moment=utc(2026, 8, 6, 12, 0, 0))
        second_id = _attach_work_unit(self.execute, "science", unit,
                                      attempt_id=502,
                                      moment=utc(2026, 8, 6, 12, 0, 1))

        creates = [s for s, _ in self.execute.statements
                   if "INSERT INTO work_units" in s]
        self.assertEqual(1, len(creates),
                         "only the first caller creates the work unit")
        # Both calls resolve to the SAME work_unit_id — the identity
        # question is answered identically either way.
        self.assertEqual(first_id, second_id)
        updates = [params for sql, params in self.execute.statements
                   if "UPDATE attempts SET work_unit_id" in sql]
        self.assertEqual(1, len(updates),
                         "only the authorized (first) caller attaches; the "
                         "second finds the unit already submitted and is "
                         "deferred, not attached")
        self.assertIn(first_id, updates[0])


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
            payload=fixtures.science_payload(
                exposure=5001, sca=7, rid=101, fid=8, field=4678622,
                expid=5001))

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

        # C1: one call to `derived.transition_work_unit` (migration 077)
        # replaces the old raw `UPDATE work_units SET state`.
        transitions = [params for sql, params in self.execute.statements
                       if "derived.transition_work_unit" in sql]
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
