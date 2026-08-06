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

Amended for schema version 2 (migration 013). The version-2 surface the
suite now pins: the execution binding copied onto every attempt row, the
claim-or-create resolver as the only acquisition path, the
application_closed state and its defining absence of the scheduler-observed
facts, the reconciler-owned mark_terminal_after_start with its COALESCE
guard, and the error-category allowlist.
"""

import datetime
import unittest

from observability.attempts import (
    APPLICATION_ERROR_CATEGORIES,
    ERROR_CATEGORIES,
    RECONCILER_ERROR_CATEGORIES,
    SCHEMA_VERSION,
    AttemptIdentity,
    AttemptWriter,
    ExecutionBinding,
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


def binding(**overrides) -> ExecutionBinding:
    """The submission-time binding every version-2 attempt row carries."""
    fields = dict(
        job_definition_arn="arn:aws:batch:us-east-1:ACCOUNT:job-definition/rapid:7",
        image_digest="sha256:image",
        manifest_checksum="sha256:manifest",
        job_definition_rev=7,
        release_identity="rapid-2026.08.1",
    )
    fields.update(overrides)
    return ExecutionBinding(**fields)


class RecordingExecutor:
    """Stands in for the database, recording every statement."""

    def __init__(self, returning: int = 1):
        self.calls: list[tuple[str, list]] = []
        self._next_id = returning

    def __call__(self, sql, params):
        self.calls.append((" ".join(sql.split()), list(params)))
        if "RETURNING" in sql or "SELECT resolve_attempt(" in sql:
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

    def last(self):
        assert self.calls, "no statement was issued"
        return self.calls[-1]


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
        self.binding = binding()

    def test_returns_attempt_id_from_returning_clause(self):
        attempt_id = self.writer.create_submitted(
            self.identity, created_at=at(0), submitted_at=at(1),
            binding=self.binding)
        self.assertEqual(attempt_id, 1)

    def test_row_is_created_in_submitted_state_with_both_timestamps(self):
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1), binding=self.binding)
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
                                     submitted_at=at(1), binding=self.binding)
        sql, _ = self.execute.only()
        for column in ("started_at", "ended_at", "scheduler_observed_exit",
                       "application_intended_exit", "rapid_outcome",
                       "product_disposition", "error_category", "source_sha",
                       "container_digest", "config_digest",
                       "terminal_record_key", "reconciliation_class"):
            self.assertNotIn(column, sql,
                             f"submitted INSERT must not write {column}")

    def test_scheduler_job_id_is_null_when_not_yet_assigned(self):
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1), binding=self.binding)
        _, params = self.execute.only()
        self.assertIn(None, params)

    def test_uses_parameters_never_interpolation(self):
        hostile = AttemptIdentity(run_id="'; DROP TABLE attempts; --",
                                  logical_job_id="job-1")
        self.writer.create_submitted(hostile, created_at=at(0),
                                     submitted_at=at(1), binding=self.binding)
        sql, params = self.execute.only()
        self.assertNotIn("DROP TABLE", sql)
        self.assertIn("'; DROP TABLE attempts; --", params)


class ExecutionBindingTests(unittest.TestCase):
    """The binding is submission-authored and copied onto every attempt row,
    so a retry row and a reconciler-authored record both carry it."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)
        self.identity = AttemptIdentity(run_id="run-1", logical_job_id="job-1")

    def test_binding_columns_are_written_on_the_submitted_row(self):
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1), binding=binding())
        sql, _ = self.execute.only()
        for column in ("binding_job_definition_arn", "binding_job_definition_rev",
                       "binding_image_digest", "binding_release_identity",
                       "binding_manifest_checksum"):
            self.assertIn(column, sql,
                          f"submitted INSERT must carry {column}")

    def test_binding_values_are_carried_into_the_parameters(self):
        self.writer.create_submitted(self.identity, created_at=at(0),
                                     submitted_at=at(1), binding=binding())
        _, params = self.execute.only()
        for value in ("arn:aws:batch:us-east-1:ACCOUNT:job-definition/rapid:7",
                      "sha256:image", "sha256:manifest", "rapid-2026.08.1", 7):
            self.assertIn(value, params)

    def test_absent_release_identity_is_null_not_fabricated(self):
        # The DDL requires only ARN, image digest and manifest checksum at
        # submitted; a job predating release identification carries NULL.
        self.writer.create_submitted(
            self.identity, created_at=at(0), submitted_at=at(1),
            binding=binding(release_identity=None, job_definition_rev=None))
        _, params = self.execute.only()
        self.assertNotIn("rapid-2026.08.1", params)
        self.assertIn("sha256:image", params)

    def test_binding_is_required_at_schema_version_2(self):
        # Checked locally so the failure names the missing thing rather than
        # arriving as a constraint violation after a round trip.
        with self.assertRaises(ValueError):
            self.writer.create_submitted(self.identity, created_at=at(0),
                                         submitted_at=at(1))
        self.assertEqual(self.execute.calls, [])

    def test_binding_stays_optional_at_schema_version_1(self):
        # Migration 013 gates the requirement on schema_version >= 2, so a
        # writer that declares 1 is still writing pre-amendment rows.
        v1 = AttemptWriter(self.execute, schema_version=1)
        attempt_id = v1.create_submitted(self.identity, created_at=at(0),
                                         submitted_at=at(1))
        self.assertEqual(attempt_id, 1)
        _, params = self.execute.only()
        self.assertIn(1, params)


class LogicalJobTests(unittest.TestCase):
    """The binding is authored once at logical-job scope, before any attempt
    row can need to copy it."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_logical_job_records_the_binding(self):
        self.writer.create_logical_job("job-1", "run-1", binding(),
                                       scheduler_job_id="job-abc")
        sql, params = self.execute.only()
        self.assertIn("INSERT INTO logical_jobs", sql)
        for value in ("job-1", "run-1", "sha256:image", "sha256:manifest",
                      "rapid-2026.08.1", "job-abc", 7):
            self.assertIn(value, params)

    def test_replayed_submission_cannot_rewrite_a_recorded_binding(self):
        # Idempotent by identity: the guard is ON CONFLICT DO NOTHING, so a
        # running attempt's belief about what it is executing is never
        # silently rewritten.
        self.writer.create_logical_job("job-1", "run-1", binding())
        sql, _ = self.execute.only()
        self.assertIn("ON CONFLICT", sql)
        self.assertIn("DO NOTHING", sql)
        self.assertNotIn("DO UPDATE", sql)

    def test_scheduler_job_id_is_optional_at_logical_job_creation(self):
        self.writer.create_logical_job("job-1", "run-1", binding())
        _, params = self.execute.only()
        self.assertIn(None, params)


class ResolveAttemptTests(unittest.TestCase):
    """The claim-or-create resolver — the only sanctioned acquisition path.
    Neither the runtime nor the reconciler bare-INSERTs."""

    def setUp(self):
        self.execute = RecordingExecutor(returning=77)
        self.writer = AttemptWriter(self.execute)
        self.identity = AttemptIdentity(run_id="run-1", logical_job_id="job-1",
                                        exposure_id=4242, sca=7,
                                        sky_tile="tile-3")

    def test_calls_the_database_resolver_function(self):
        self.writer.resolve_attempt(self.identity, created_at=at(0),
                                    submitted_at=at(1),
                                    application_attempt_index=1)
        sql, _ = self.execute.only()
        self.assertIn("SELECT resolve_attempt(", sql)
        self.assertNotIn("INSERT INTO attempts", sql)

    def test_returns_the_resolved_attempt_id(self):
        attempt_id = self.writer.resolve_attempt(
            self.identity, created_at=at(0), submitted_at=at(1),
            application_attempt_index=1)
        self.assertEqual(attempt_id, 77)

    def test_passes_eleven_parameters_in_the_resolver_order(self):
        self.writer.resolve_attempt(
            self.identity, created_at=at(0), submitted_at=at(1),
            scheduler_job_id="job-abc", application_attempt_index=2,
            scheduler_attempt_index=3)
        _, params = self.execute.only()
        self.assertEqual(len(params), 11)
        self.assertEqual(params, [
            "run-1", "job-1", "job-abc", 2, 3, at(0), at(1),
            4242, 7, "tile-3", SCHEMA_VERSION,
        ])

    def test_either_index_alone_identifies_the_attempt(self):
        self.writer.resolve_attempt(self.identity, created_at=at(0),
                                    submitted_at=at(1),
                                    scheduler_attempt_index=1)
        _, params = self.execute.only()
        self.assertIsNone(params[3])
        self.assertEqual(params[4], 1)

    def test_neither_index_is_not_identifying_an_attempt(self):
        with self.assertRaises(ValueError):
            self.writer.resolve_attempt(self.identity, created_at=at(0),
                                        submitted_at=at(1))
        self.assertEqual(self.execute.calls, [])

    def test_attempt_indexes_are_one_based_so_zero_is_rejected(self):
        for kwargs in ({"application_attempt_index": 0},
                       {"scheduler_attempt_index": 0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.writer.resolve_attempt(self.identity, created_at=at(0),
                                                submitted_at=at(1), **kwargs)

    def test_negative_attempt_index_is_rejected_on_either_column(self):
        for kwargs in ({"application_attempt_index": -1},
                       {"scheduler_attempt_index": -2}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.writer.resolve_attempt(self.identity, created_at=at(0),
                                                submitted_at=at(1), **kwargs)
        self.assertEqual(self.execute.calls, [])


class ArrayChildCreationTests(unittest.TestCase):
    """Array children are rows at submission time — the ordering the design
    requires, so an unresolved child is detectable rather than a silent gap."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)
        self.binding = binding()
        self.submission = FakeSubmission(
            "batch-9", "job-abc",
            [FakeUnit(100, 1), FakeUnit(100, 2), FakeUnit(101, 3)])

    def attempt_inserts(self):
        return [(sql, params) for sql, params in self.execute.calls
                if "INSERT INTO attempts" in sql]

    def test_one_row_per_array_child(self):
        ids = self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1), binding=self.binding)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(self.attempt_inserts()), 3)

    def test_each_child_row_carries_its_own_unit_identity(self):
        self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1), binding=self.binding)
        scas = [params[5] for _, params in self.attempt_inserts()]
        exposures = [params[4] for _, params in self.attempt_inserts()]
        self.assertEqual(scas, [1, 2, 3])
        self.assertEqual(exposures, [100, 100, 101])

    def test_children_are_independent_rows_not_one_shared_row(self):
        ids = self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1), binding=self.binding)
        self.assertEqual(len(set(ids)), 3)

    def test_child_job_ids_follow_batch_parent_colon_index(self):
        self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1), binding=self.binding)
        job_ids = [params[3] for _, params in self.attempt_inserts()]
        self.assertEqual(job_ids, ["job-abc:0", "job-abc:1", "job-abc:2"])

    def test_logical_job_is_recorded_before_its_attempt_row(self):
        # The binding must exist before any attempt row can need to copy it,
        # so a runtime that resolves its own row finds one rather than being
        # flagged as an orphan.
        self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1), binding=self.binding)
        kinds = ["logical_jobs" if "logical_jobs" in sql else "attempts"
                 for sql in self.execute.statements]
        self.assertEqual(kinds, ["logical_jobs", "attempts"] * 3)

    def test_each_child_gets_its_own_logical_job_row(self):
        self.writer.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1), binding=self.binding)
        logical_ids = [params[0] for sql, params in self.execute.calls
                       if "logical_jobs" in sql]
        self.assertEqual(logical_ids, ["batch-9:0", "batch-9:1", "batch-9:2"])

    def test_no_logical_job_rows_without_a_binding(self):
        v1 = AttemptWriter(self.execute, schema_version=1)
        v1.create_submitted_for_submission(
            self.submission, run_id="run-1", created_at=at(0),
            submitted_at=at(1))
        self.assertTrue(all("INSERT INTO attempts" in sql
                            for sql in self.execute.statements))


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
        for column in ("ended_at", "scheduler_observed_exit",
                       "application_intended_exit", "rapid_outcome",
                       "product_disposition"):
            self.assertNotIn(column, sql)

    def test_started_records_the_application_observed_attempt_index(self):
        self.writer.mark_started(1, started_at=at(5), provenance=self.provenance,
                                 application_attempt_index=2)
        sql, params = self.execute.only()
        self.assertIn("application_attempt_index", sql)
        self.assertIn(2, params)

    def test_a_claimed_rows_attempt_index_is_never_re_indexed(self):
        # COALESCE(application_attempt_index, %s): the resolver's value wins,
        # so re-supplying the same index at start is harmless.
        self.writer.mark_started(1, started_at=at(5), provenance=self.provenance,
                                 application_attempt_index=2)
        sql, _ = self.execute.only()
        self.assertIn("COALESCE(application_attempt_index,", sql)

    def test_attempt_index_is_null_when_the_runtime_did_not_read_one(self):
        self.writer.mark_started(1, started_at=at(5), provenance=self.provenance)
        _, params = self.execute.only()
        self.assertIn(None, params)


class ApplicationClosedTests(unittest.TestCase):
    """The application's own closing transition: it cites the S3 terminal
    record it has already written, and knows nothing of the scheduler."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def close(self, **overrides):
        kwargs = dict(
            attempt_id=1, ended_at=at(9), application_intended_exit=0,
            rapid_outcome=RapidOutcome.SUCCESS,
            product_disposition=ProductDisposition.PUBLISHED,
            terminal_record_key="run-1/job-1/attempt-1.json",
        )
        kwargs.update(overrides)
        return self.writer.mark_application_closed(**kwargs)

    def test_row_reaches_application_closed(self):
        self.close()
        _, params = self.execute.only()
        self.assertIn(LifecycleState.APPLICATION_CLOSED.value, params)
        self.assertEqual(LifecycleState.APPLICATION_CLOSED.value,
                         "application_closed")

    def test_cites_the_terminal_record_it_already_wrote(self):
        self.close(terminal_record_sequence=3,
                   terminal_record_checksum="sha256:record")
        sql, params = self.execute.only()
        self.assertIn("terminal_record_key", sql)
        self.assertIn("terminal_record_sequence", sql)
        self.assertIn("terminal_record_checksum", sql)
        self.assertIn("run-1/job-1/attempt-1.json", params)
        self.assertIn(3, params)
        self.assertIn("sha256:record", params)

    def test_writes_the_intended_exit_as_an_intent(self):
        # A classified application failure still intends exit 0 under the
        # fail-loud posture; a nonzero exit is reserved for the unrecordable.
        self.close(application_intended_exit=0,
                   rapid_outcome=RapidOutcome.FAILURE,
                   product_disposition=ProductDisposition.NONE,
                   error_category="tool_failure")
        sql, params = self.execute.only()
        self.assertIn("application_intended_exit", sql)
        self.assertIn(RapidOutcome.FAILURE.value, params)
        self.assertIn("tool_failure", params)
        self.assertIn(0, params)

    def test_says_nothing_about_the_scheduler_observed_facts(self):
        # The defining absence of this state: those facts are not yet known,
        # which is what application_closed MEANS. The reconciler supplies them.
        self.close()
        sql, _ = self.execute.only()
        self.assertNotIn("scheduler_observed_exit", sql)
        self.assertNotIn("scheduler_state", sql)

    def test_terminal_record_sequence_is_monotonic_from_zero(self):
        with self.assertRaises(ValueError):
            self.close(terminal_record_sequence=-1)
        self.assertEqual(self.execute.calls, [])

    def test_sequence_defaults_to_the_first_record(self):
        self.close()
        _, params = self.execute.only()
        self.assertIn(0, params)

    def test_reconciler_materialized_flows_through(self):
        # Set only when the reconciler projects this transition from a
        # validated S3 record — the one sanctioned projection of application
        # facts by another writer.
        self.close(reconciler_materialized=True)
        sql, params = self.execute.only()
        self.assertIn("reconciler_materialized = %s", sql)
        self.assertIs(params[9], True)

    def test_application_authored_close_is_not_reconciler_materialized(self):
        # Asserted positionally: `True == 1` in Python, so a membership test
        # would collide with the attempt_id in the same parameter list.
        self.close()
        _, params = self.execute.only()
        self.assertIs(params[9], False)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_scheduler_succeeded_with_application_failure_is_representable(self):
        # The 2026-07-22 failure mode, made first-class: both fields are written
        # as given and neither is inferred from the other.
        self.writer.mark_terminal_after_start(
            1, ended_at=at(9), scheduler_observed_exit=0,
            scheduler_state="SUCCEEDED",
            rapid_outcome=RapidOutcome.FAILURE,
            product_disposition=ProductDisposition.NONE,
            error_category="internal_error")
        _, params = self.execute.only()
        self.assertIn("SUCCEEDED", params)
        self.assertIn(RapidOutcome.FAILURE.value, params)
        self.assertIn(0, params)

    def test_writes_the_scheduler_observed_facts_it_owns(self):
        # The reconciler's contribution: the scheduler end state and the exit
        # code the container actually produced.
        self.writer.mark_terminal_after_start(
            1, ended_at=at(9), scheduler_observed_exit=137,
            scheduler_state="FAILED")
        sql, params = self.execute.only()
        self.assertIn("scheduler_observed_exit = %s", sql)
        self.assertIn("scheduler_state = %s", sql)
        self.assertIn(LifecycleState.TERMINAL_AFTER_START.value, params)
        self.assertIn(137, params)
        self.assertIn("FAILED", params)

    def test_never_overwrites_what_the_application_authored(self):
        # COALESCE on every application-authored field, so a reconciler pass
        # over an already-closed row adds facts rather than replacing them.
        self.writer.mark_terminal_after_start(
            1, ended_at=at(9), scheduler_observed_exit=0,
            scheduler_state="SUCCEEDED",
            rapid_outcome=RapidOutcome.SUCCESS,
            product_disposition=ProductDisposition.PUBLISHED,
            application_intended_exit=0)
        sql, _ = self.execute.only()
        self.assertIn("COALESCE", sql)
        for column in ("rapid_outcome", "product_disposition",
                       "application_intended_exit", "error_category"):
            self.assertIn(f"{column} = COALESCE({column}, %s)", sql,
                          f"{column} must be applied with COALESCE")

    def test_application_authored_fields_are_optional_for_the_reconciler(self):
        # The normal path already wrote them at application-close.
        self.writer.mark_terminal_after_start(
            1, ended_at=at(9), scheduler_observed_exit=0,
            scheduler_state="SUCCEEDED")
        _, params = self.execute.only()
        self.assertIn(None, params)

    def test_terminal_record_reference_is_coalesced_the_other_way(self):
        # A reconciler-supplied key fills a NULL; it does not blank one the
        # application already cited.
        self.writer.mark_terminal_after_start(
            1, ended_at=at(9), scheduler_observed_exit=0,
            scheduler_state="SUCCEEDED",
            terminal_record_key="run-1/job-1/attempt-1.json",
            terminal_record_sequence=1)
        sql, params = self.execute.only()
        self.assertIn("terminal_record_key = COALESCE(%s, terminal_record_key)",
                      sql)
        self.assertIn("run-1/job-1/attempt-1.json", params)

    def test_rejects_a_scheduler_state_the_scheduler_does_not_define(self):
        with self.assertRaises(ValueError):
            self.writer.mark_terminal_after_start(
                1, ended_at=at(9), scheduler_observed_exit=0,
                scheduler_state="EXPLODED")
        self.assertEqual(self.execute.calls, [])

    def test_terminal_without_start_omits_started_only_fields(self):
        self.writer.mark_terminal_without_start(
            1, ended_at=at(9), scheduler_state="FAILED",
            error_category="scheduler_provisioning")
        sql, params = self.execute.only()
        self.assertIn(LifecycleState.TERMINAL_WITHOUT_START.value, params)
        for column in ("started_at", "scheduler_observed_exit",
                       "application_intended_exit", "rapid_outcome",
                       "product_disposition", "source_sha"):
            self.assertNotIn(column, sql,
                             f"never-started row must not write {column}")

    def test_rejects_a_state_the_scheduler_does_not_define(self):
        with self.assertRaises(ValueError):
            self.writer.mark_terminal_without_start(
                1, ended_at=at(9), scheduler_state="EXPLODED")


class ErrorCategoryTests(unittest.TestCase):
    """The allowlist mirrors migration 013's foreign key as an early local
    failure — the database stays the authority, this is a copy for speed."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def close(self, category):
        self.writer.mark_application_closed(
            1, ended_at=at(9), application_intended_exit=0,
            rapid_outcome=RapidOutcome.FAILURE,
            product_disposition=ProductDisposition.NONE,
            terminal_record_key="run-1/job-1/attempt-1.json",
            error_category=category)

    def test_the_v1_allowlist_has_thirteen_categories(self):
        self.assertEqual(len(ERROR_CATEGORIES), 13)
        self.assertEqual(
            ERROR_CATEGORIES,
            APPLICATION_ERROR_CATEGORIES | RECONCILER_ERROR_CATEGORIES)
        self.assertEqual(
            APPLICATION_ERROR_CATEGORIES & RECONCILER_ERROR_CATEGORIES,
            frozenset())

    def test_every_allowlisted_category_is_accepted(self):
        for category in sorted(ERROR_CATEGORIES):
            with self.subTest(category=category):
                self.execute.calls.clear()
                self.close(category)
                _, params = self.execute.only()
                self.assertIn(category, params)

    def test_no_category_is_accepted_for_a_successful_attempt(self):
        self.close(None)
        _, params = self.execute.only()
        self.assertIn(None, params)

    def test_a_category_outside_the_allowlist_is_rejected(self):
        # A typo names itself here rather than arriving as a 23503 after a
        # round trip. Extending the vocabulary is a schema-versioned change.
        for category in ("science_failure", "oom", "spot_reclaim",
                         "container_pull_error", "TOOL_FAILURE"):
            with self.subTest(category=category):
                self.execute.calls.clear()
                with self.assertRaises(ValueError):
                    self.close(category)
                self.assertEqual(self.execute.calls, [])

    def test_the_reconciler_transition_validates_too(self):
        with self.assertRaises(ValueError):
            self.writer.mark_terminal_after_start(
                1, ended_at=at(9), scheduler_observed_exit=1,
                scheduler_state="FAILED", error_category="not_a_category")
        self.assertEqual(self.execute.calls, [])


class AbruptLossTests(unittest.TestCase):
    """OOM kill, Spot reclaim, host death: the job never wrote its own record."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = AttemptWriter(self.execute)

    def test_abrupt_loss_stays_terminal_after_start(self):
        # It did start, and its provenance is already on the row.
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="resource_exhausted")
        _, params = self.execute.only()
        self.assertIn(LifecycleState.TERMINAL_AFTER_START.value, params)

    def test_abrupt_loss_records_failure_not_success(self):
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="scheduler_reclaimed")
        _, params = self.execute.only()
        self.assertIn(RapidOutcome.FAILURE.value, params)
        self.assertIn("scheduler_reclaimed", params)

    def test_unobserved_exit_code_says_killed_never_zero(self):
        # A fabricated 0 would assert the process succeeded. 137 states what
        # actually happened: killed.
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="resource_exhausted")
        _, params = self.execute.only()
        self.assertIn(137, params)
        self.assertNotIn(0, params)

    def test_observed_exit_code_is_preserved(self):
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="resource_exhausted",
                                     scheduler_observed_exit=139)
        _, params = self.execute.only()
        self.assertIn(139, params)

    def test_the_exit_written_is_the_schedulers_not_the_applications(self):
        # The reconciler is the writer and the scheduler is where the
        # observation came from, so it lands in the scheduler column.
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="resource_exhausted",
                                     scheduler_observed_exit=139)
        sql, params = self.execute.only()
        self.assertIn("scheduler_observed_exit = %s", sql)
        # The scheduler exit is positional; the application's intent is the
        # NULL that follows it.
        self.assertEqual(params[2], 139)
        self.assertIsNone(params[4])

    def test_application_intended_exit_stays_absent(self):
        # The honest absence: the application never got to state an intent,
        # and NULL says exactly that where a fabricated value would not.
        self.writer.mark_abrupt_loss(1, ended_at=at(9), scheduler_state="FAILED",
                                     error_category="resource_exhausted")
        sql, params = self.execute.only()
        self.assertIn("application_intended_exit = COALESCE("
                      "application_intended_exit, %s)", sql)
        self.assertIsNone(params[4])

    def test_reconciler_first_record_reference_is_carried(self):
        # A terminal_after_start row must cite the record that accounts for it,
        # so the reconciler supplies its own record's key and sequence.
        self.writer.mark_abrupt_loss(
            1, ended_at=at(9), scheduler_state="FAILED",
            error_category="scheduler_reclaimed",
            terminal_record_key="run-1/job-1/reconciler-1.json",
            terminal_record_sequence=0)
        _, params = self.execute.only()
        self.assertIn("run-1/job-1/reconciler-1.json", params)

    def test_abrupt_loss_rejects_a_category_outside_the_allowlist(self):
        with self.assertRaises(ValueError):
            self.writer.mark_abrupt_loss(1, ended_at=at(9),
                                         scheduler_state="FAILED",
                                         error_category="oom")
        self.assertEqual(self.execute.calls, [])


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


class SchemaVersionTests(unittest.TestCase):
    def test_writers_declare_migration_013s_version(self):
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_version_is_stamped_on_every_new_row(self):
        execute = RecordingExecutor()
        writer = AttemptWriter(execute)
        writer.create_submitted(
            AttemptIdentity(run_id="run-1", logical_job_id="job-1"),
            created_at=at(0), submitted_at=at(1), binding=binding())
        _, params = execute.only()
        self.assertEqual(params[0], SCHEMA_VERSION)

    def test_a_declared_version_is_what_the_resolver_is_told(self):
        execute = RecordingExecutor()
        writer = AttemptWriter(execute, schema_version=1)
        writer.resolve_attempt(
            AttemptIdentity(run_id="run-1", logical_job_id="job-1"),
            created_at=at(0), submitted_at=at(1),
            application_attempt_index=1)
        _, params = execute.only()
        self.assertEqual(params[-1], 1)

    def test_the_six_lifecycle_states_are_the_ddls_vocabulary(self):
        self.assertEqual(
            {state.value for state in LifecycleState},
            {"submitted", "started", "application_closed",
             "terminal_after_start", "terminal_without_start",
             "missing_or_contradictory"})


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
