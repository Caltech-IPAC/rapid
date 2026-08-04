"""
File:    test_attempts.py

Tests for the attempt-record write path.

Every test here substitutes a recording executor for the database. Nothing in
this file opens a connection, and nothing points at the live database — the
seam exists precisely so the emission logic is assertable without one.

What is actually asserted is the part that breaks: which SQL runs, in what
order, with which parameters, and — the load-bearing one — which fields are
LEFT OUT of a statement, because "absent, never sentinel-valued" is a property
of the emitted SQL, not just of the DDL.
"""

import datetime
import unittest

from observability.attempts import (
    SCHEMA_VERSION,
    AttemptIdentity,
    AttemptWriter,
    LifecycleState,
    ProductDisposition,
    Provenance,
    RapidOutcome,
    ReconciliationClass,
    Stage,
    StageOutcome,
)

UTC = datetime.timezone.utc


def at(second: int) -> datetime.datetime:
    return datetime.datetime(2026, 8, 4, 12, 0, second, tzinfo=UTC)


class RecordingExecutor:
    """Stands in for the database, recording every statement."""

    def __init__(self, returning: int = 1):
        self.calls: list[tuple[str, list]] = []
        self._next_id = returning

    def __call__(self, sql, params):
        self.calls.append((" ".join(sql.split()), list(params)))
        if "RETURNING" in sql:
            value = self._next_id
            self._next_id += 1
            return [(value,)]
        return None

    @property
    def statements(self):
        return [sql for sql, _ in self.calls]

    def only(self):
        assert len(self.calls) == 1, f"expected 1 statement, got {len(self.calls)}"
        return self.calls[0]


class FakeUnit:
    def __init__(self, exposure, sca):
        self.exposure = exposure
        self.sca = sca


class FakeManifest:
    def __init__(self, units):
        self.units = tuple(units)


class FakeSubmission:
    """The shape submission.Submission presents to an attempt-record writer."""

    def __init__(self, batch_id, job_id, units):
        self.batch_id = batch_id
        self.job_id = job_id
        self.manifest = FakeManifest(units)

    def child_job_id(self, index):
        return f"{self.job_id}:{index}"


class CreateSubmittedTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)
        self.identity = AttemptIdentity(run_id="run-1", logical_job_id="job-1",
                                        exposure_id=4242, sca=7)

    def test_returns_attempt_id_from_returning_clause(self):
        attempt_id = self.writer.create_submitted(
            self.identity, created_at=at(0), submitted_at=at(1))
        self.assertEqual(attempt_id, 1)

    def test_row_is_created_in_submitted_state_with_both_timestamps(self):
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1))
        sql, params = self.execute.only()
        self.assertIn("INSERT INTO attempts", sql)
        self.assertIn(LifecycleState.SUBMITTED.value, params)
        self.assertIn(at(0), params)
        self.assertIn(at(1), params)
        self.assertIn(SCHEMA_VERSION, params)

    def test_submitted_row_omits_every_not_yet_reached_field(self):
        # The absence rule, asserted against the SQL: a submitted row's INSERT
        # must not mention started/ended/outcome/provenance columns at all.
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1))
        sql, _ = self.execute.only()
        for column in ("started_at", "ended_at", "process_exit_code",
                       "rapid_outcome", "product_disposition",
                       "error_category", "source_sha", "container_digest",
                       "job_definition_rev", "config_digest",
                       "reconciliation_class"):
            self.assertNotIn(column, sql,
                             f"submitted INSERT must not write {column}")

    def test_scheduler_job_id_is_null_when_not_yet_assigned(self):
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1))
        _, params = self.execute.only()
        self.assertIn(None, params)

    def test_uses_parameters_never_interpolation(self):
        hostile = AttemptIdentity(run_id="'; DROP TABLE attempts; --",
                                  logical_job_id="job-1")
        self.writer.create_submitted(hostile, created_at=at(0),
                                     submitted_at=at(1))
        sql, params = self.execute.only()
        self.assertNotIn("DROP TABLE", sql)
        self.assertIn("'; DROP TABLE attempts; --", params)


class ArrayChildCreationTests(unittest.TestCase):
    """Array children are rows at submission time — the ordering the design
    requires, so an unresolved child is detectable rather than a silent gap."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)
        self.submission = FakeSubmission(
            "batch-9", "job-abc",
            [FakeUnit(100, 1), FakeUnit(100, 2), FakeUnit(101, 3)])

    def test_one_row_per_array_child(self):
        ids = self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1))
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(self.execute.calls), 3)

    def test_each_child_row_carries_its_own_unit_identity(self):
        self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1))
        scas = [params[5] for _, params in self.execute.calls]
        exposures = [params[4] for _, params in self.execute.calls]
        self.assertEqual(scas, [1, 2, 3])
        self.assertEqual(exposures, [100, 100, 101])

    def test_children_are_independent_rows_not_one_shared_row(self):
        ids = self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1))
        self.assertEqual(len(set(ids)), 3)

    def test_child_job_ids_follow_batch_parent_colon_index(self):
        self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1))
        job_ids = [params[3] for _, params in self.execute.calls]
        self.assertEqual(job_ids, ["job-abc:0", "job-abc:1", "job-abc:2"])


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_backfill_sets_ids_for_rows_created_without_them(self):
        count = self.writer.backfill_scheduler_job_ids(
            [(1, "job-abc:0"), (2, "job-abc:1")])
        self.assertEqual(count, 2)
        self.assertEqual(len(self.execute.calls), 2)

    def test_backfill_never_overwrites_an_id_already_recorded(self):
        # The guard lives in the WHERE clause, so a re-run is a no-op rather
        # than a silent overwrite of the scheduler's own answer.
        self.writer.backfill_scheduler_job_ids([(1, "job-abc:0")])
        sql, _ = self.execute.only()
        self.assertIn("scheduler_job_id IS NULL", sql)

    def test_unresolved_child_is_left_alone(self):
        # Nothing is written for a child that never resolved: its row keeps a
        # NULL scheduler_job_id and reconciliation finds it.
        count = self.writer.backfill_scheduler_job_ids([])
        self.assertEqual(count, 0)
        self.assertEqual(self.execute.calls, [])


class StartedTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)
        self.provenance = Provenance(source_sha="abc123",
                                     container_digest="sha256:def",
                                     job_definition_rev="rapid-job:7",
                                     config_digest="sha256:cfg")

    def test_started_writes_all_four_provenance_fields(self):
        self.writer.mark_started(1, started_at=at(5), provenance=self.provenance)
        sql, params = self.execute.only()
        self.assertIn(LifecycleState.STARTED.value, params)
        for value in ("abc123", "sha256:def", "rapid-job:7", "sha256:cfg"):
            self.assertIn(value, params)

    def test_started_does_not_write_terminal_fields(self):
        self.writer.mark_started(1, started_at=at(5), provenance=self.provenance)
        sql, _ = self.execute.only()
        for column in ("ended_at", "process_exit_code", "rapid_outcome",
                       "product_disposition"):
            self.assertNotIn(column, sql)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_scheduler_succeeded_with_application_failure_is_representable(self):
        # The 2026-07-22 failure mode, made first-class: both fields are written
        # as given and neither is inferred from the other.
        self.writer.mark_terminal_after_start(
            1, ended_at=at(9), process_exit_code=0,
            rapid_outcome=RapidOutcome.FAILURE,
            product_disposition=ProductDisposition.NONE,
            error_category="science_failure", scheduler_state="SUCCEEDED")
        _, params = self.execute.only()
        self.assertIn("SUCCEEDED", params)
        self.assertIn(RapidOutcome.FAILURE.value, params)
        self.assertIn(0, params)

    def test_terminal_without_start_omits_started_only_fields(self):
        self.writer.mark_terminal_without_start(
            1, ended_at=at(9), scheduler_state="FAILED",
            error_category="container_pull_error")
        sql, params = self.execute.only()
        self.assertIn(LifecycleState.TERMINAL_WITHOUT_START.value, params)
        for column in ("started_at", "process_exit_code", "rapid_outcome",
                       "product_disposition", "source_sha"):
            self.assertNotIn(column, sql,
                             f"never-started row must not write {column}")

    def test_rejects_a_state_the_scheduler_does_not_define(self):
        with self.assertRaises(ValueError):
            self.writer.mark_terminal_without_start(
                1, ended_at=at(9), scheduler_state="EXPLODED")


class AbruptLossTests(unittest.TestCase):
    """OOM kill, Spot reclaim, host death: the job never wrote its own record."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_abrupt_loss_stays_terminal_after_start(self):
        # It did start, and its provenance is already on the row.
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="oom")
        _, params = self.execute.only()
        self.assertIn(LifecycleState.TERMINAL_AFTER_START.value, params)

    def test_abrupt_loss_records_failure_not_success(self):
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="spot_reclaim")
        _, params = self.execute.only()
        self.assertIn(RapidOutcome.FAILURE.value, params)
        self.assertIn("spot_reclaim", params)

    def test_unobserved_exit_code_says_killed_never_zero(self):
        # A fabricated 0 would assert the process succeeded. 137 states what
        # actually happened: killed.
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="oom")
        _, params = self.execute.only()
        self.assertIn(137, params)
        self.assertNotIn(0, params)

    def test_observed_exit_code_is_preserved(self):
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="oom", process_exit_code=139)
        _, params = self.execute.only()
        self.assertIn(139, params)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_flagging_records_class_sources_and_detection_time(self):
        self.writer.mark_missing_or_contradictory(
            1, reconciliation_class=ReconciliationClass.MISSING,
            reconciliation_sources=["postgres", "batch"], detected_at=at(20))
        _, params = self.execute.only()
        self.assertIn("missing", params)
        self.assertIn(["postgres", "batch"], params)
        self.assertIn(at(20), params)

    def test_sources_cannot_be_empty(self):
        with self.assertRaises(ValueError):
            self.writer.mark_missing_or_contradictory(
                1, reconciliation_class=ReconciliationClass.CONTRADICTORY,
                reconciliation_sources=[], detected_at=at(20))


class SchedulerObservationTests(unittest.TestCase):
    """The reconciler-written columns sit BESIDE the application's, never on top."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_writes_only_the_scheduler_columns(self):
        self.writer.record_scheduler_observation(
            1, scheduler_state="SUCCEEDED", created_at=at(1),
            started_at=at(4), stopped_at=at(9), attempt_index=0)
        sql, _ = self.execute.only()
        self.assertIn("scheduler_created_at", sql)
        self.assertIn("scheduler_started_at", sql)
        self.assertIn("scheduler_stopped_at", sql)
        self.assertIn("scheduler_attempt_index", sql)

    def test_never_touches_the_application_authored_timestamps(self):
        # The amendment's whole point: no column has two writers, so
        # disagreement survives for reconciliation to find.
        self.writer.record_scheduler_observation(
            1, started_at=at(4), stopped_at=at(9))
        sql, _ = self.execute.only()
        self.assertNotIn("SET started_at", sql)
        self.assertNotIn(" ended_at =", sql)
        self.assertNotIn(" submitted_at =", sql)
        self.assertNotIn(" created_at =", sql)

    def test_rejects_a_negative_attempt_index(self):
        with self.assertRaises(ValueError):
            self.writer.record_scheduler_observation(1, attempt_index=-1)


class StageAndMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_stage_is_appended_with_monotonic_duration(self):
        self.writer.record_stage(1, Stage("difference", at(5), 1500.0,
                                          StageOutcome.SUCCESS))
        sql, params = self.execute.only()
        self.assertIn("INSERT INTO attempt_stages", sql)
        self.assertIn(1500.0, params)
        self.assertIn("difference", params)

    def test_stages_are_never_updated_only_inserted(self):
        self.writer.record_stages(1, [
            Stage("ingest", at(1), 10.0, StageOutcome.SUCCESS),
            Stage("difference", at(2), 20.0, StageOutcome.FAILURE),
        ])
        self.assertTrue(all("INSERT INTO attempt_stages" in sql
                            for sql in self.execute.statements))

    def test_negative_duration_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            Stage("difference", at(5), -1.0, StageOutcome.SUCCESS)

    def test_milestone_requires_a_processing_unit_scope(self):
        with self.assertRaises(ValueError):
            self.writer.record_milestone("alert_published", reached_at=at(30))

    def test_milestone_carries_producing_attempt_for_traceability(self):
        self.writer.record_milestone("alert_published", reached_at=at(30),
                                     exposure_id=4242, sca=7,
                                     producing_attempt_id=1)
        sql, params = self.execute.only()
        self.assertIn("INSERT INTO milestones", sql)
        self.assertIn("alert_published", params)
        self.assertIn(1, params)


if __name__ == "__main__":
    unittest.main()
