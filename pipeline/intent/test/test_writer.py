"""
File:    test_writer.py

Tests for `pipeline.intent.writer`: the work-unit and campaign state
machines, against a fake executor — no live database, per the repo's
house convention (`observability/test/test_attempts.py`'s
`RecordingExecutor` is the model this file's `RecordingExecutor` follows).
"""

import unittest

from pipeline.intent.writer import (
    ABANDONED,
    ACTIVE,
    BLOCKED,
    CAMPAIGN_COMPLETE,
    COMPLETE,
    DEFINED,
    FAILED,
    PAUSED,
    QUARANTINED,
    READY,
    SUBMITTED,
    WRITER_MUTATION_API,
    WRITER_ORCHESTRATOR,
    WRITER_RECONCILER,
    WRITER_VALIDATION_INGEST,
    CampaignWriter,
    IllegalTransition,
    SupersessionConflict,
    WorkUnitIdentity,
    WorkUnitNotFound,
    WorkUnitWriter,
    WrongWriterForTransition,
)


class RecordingExecutor:
    """Captures every statement issued, in order. See observability's twin."""

    def __init__(self, returning: int = 1, affected: int = 1):
        self.calls: list[tuple[str, list]] = []
        self._next_id = returning
        self.affected = affected

    def __call__(self, sql, params):
        self.calls.append((" ".join(sql.split()), list(params)))
        if "RETURNING" in sql:
            value = self._next_id
            self._next_id += 1
            return [(value,)]
        if sql.strip().upper().startswith("SELECT"):
            return self._select_result
        return self.affected

    _select_result: list = []

    @property
    def statements(self):
        return [sql for sql, _ in self.calls]

    def last(self):
        assert self.calls, "no statement was issued"
        return self.calls[-1]


def identity(job_type="catalog-load", input_scope="2027-10-01/5",
            operational_class="prompt-processing", definition_version=1):
    return WorkUnitIdentity(job_type=job_type, input_scope=input_scope,
                            operational_class=operational_class,
                            definition_version=definition_version)


class CreateWorkUnitTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = WorkUnitWriter(self.execute)

    def test_returns_the_new_work_unit_id(self):
        work_unit_id = self.writer.create_work_unit(
            identity(), writer=WRITER_VALIDATION_INGEST)
        self.assertEqual(1, work_unit_id)

    def test_default_state_is_ready(self):
        self.writer.create_work_unit(identity(), writer=WRITER_VALIDATION_INGEST)
        sql, params = self.execute.calls[0]
        self.assertIn("INSERT INTO work_units", sql)
        self.assertIn(READY, params)

    def test_creation_writes_one_unit_event_with_null_from_state(self):
        self.writer.create_work_unit(identity(), writer=WRITER_VALIDATION_INGEST)
        event_sql, event_params = self.execute.calls[1]
        self.assertIn("INSERT INTO unit_events", event_sql)
        self.assertIn(None, event_params)  # from_state
        self.assertIn(READY, event_params)  # to_state
        self.assertIn(WRITER_VALIDATION_INGEST, event_params)

    def test_blocked_requires_a_reason(self):
        with self.assertRaises(ValueError):
            self.writer.create_work_unit(
                identity(), writer=WRITER_VALIDATION_INGEST, state=BLOCKED)

    def test_blocked_with_reason_succeeds(self):
        work_unit_id = self.writer.create_work_unit(
            identity(), writer=WRITER_VALIDATION_INGEST, state=BLOCKED,
            blocked_reason="staging incomplete")
        self.assertEqual(1, work_unit_id)
        sql, params = self.execute.calls[0]
        self.assertIn("staging incomplete", params)

    def test_reason_forbidden_outside_blocked(self):
        with self.assertRaises(ValueError):
            self.writer.create_work_unit(
                identity(), writer=WRITER_VALIDATION_INGEST, state=READY,
                blocked_reason="should not be here")

    def test_unknown_writer_is_refused(self):
        with self.assertRaises(ValueError):
            self.writer.create_work_unit(identity(), writer="not-a-writer")

    def test_campaign_id_is_written_when_supplied(self):
        self.writer.create_work_unit(
            identity(), writer=WRITER_VALIDATION_INGEST, campaign_id=42)
        _, params = self.execute.calls[0]
        self.assertIn(42, params)


class FindCurrentUnitTests(unittest.TestCase):
    def test_returns_none_when_no_row(self):
        execute = RecordingExecutor()
        execute._select_result = []
        writer = WorkUnitWriter(execute)
        self.assertIsNone(writer.find_current_unit("catalog-load", "x"))

    def test_returns_dict_when_row_found(self):
        execute = RecordingExecutor()
        execute._select_result = [
            (7, "catalog-load", "x", "prompt-processing", 1, "ready", None, None)]
        writer = WorkUnitWriter(execute)
        row = writer.find_current_unit("catalog-load", "x")
        self.assertEqual(7, row["work_unit_id"])
        self.assertEqual("ready", row["state"])

    def test_selects_only_non_superseded_rows(self):
        execute = RecordingExecutor()
        execute._select_result = []
        writer = WorkUnitWriter(execute)
        writer.find_current_unit("catalog-load", "x")
        sql, params = execute.calls[0]
        self.assertIn("superseded_by_unit_id IS NULL", sql)
        self.assertEqual(["catalog-load", "x"], params)


class TransitionLegalityTests(unittest.TestCase):
    """The state-machine graph, exercised without touching SQL at all —
    every illegal edge must be refused before `_execute` is ever called."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.writer = WorkUnitWriter(self.execute)

    def test_blocked_to_ready_is_legal(self):
        self.writer.transition_unit(1, BLOCKED, READY,
                                    writer=WRITER_ORCHESTRATOR)
        self.assertTrue(self.execute.calls)

    def test_ready_to_submitted_is_legal(self):
        self.writer.transition_unit(1, READY, SUBMITTED,
                                    writer=WRITER_ORCHESTRATOR)
        self.assertTrue(self.execute.calls)

    def test_submitted_to_complete_is_legal(self):
        self.writer.transition_unit(1, SUBMITTED, COMPLETE,
                                    writer=WRITER_RECONCILER)
        self.assertTrue(self.execute.calls)

    def test_submitted_to_failed_is_legal(self):
        self.writer.transition_unit(1, SUBMITTED, FAILED,
                                    writer=WRITER_RECONCILER)
        self.assertTrue(self.execute.calls)

    def test_any_open_state_to_quarantined_is_legal(self):
        for source in (BLOCKED, READY, SUBMITTED):
            with self.subTest(source=source):
                execute = RecordingExecutor()
                writer = WorkUnitWriter(execute)
                writer.transition_unit(1, source, QUARANTINED,
                                       writer=WRITER_MUTATION_API)
                self.assertTrue(execute.calls)

    def test_complete_to_anything_is_illegal(self):
        with self.assertRaises(IllegalTransition):
            self.writer.transition_unit(1, COMPLETE, READY,
                                        writer=WRITER_MUTATION_API)
        self.assertEqual([], self.execute.calls,
                         "no SQL must be issued for an illegal transition")

    def test_ready_to_complete_is_illegal_skips_submitted(self):
        with self.assertRaises(IllegalTransition):
            self.writer.transition_unit(1, READY, COMPLETE,
                                        writer=WRITER_RECONCILER)
        self.assertEqual([], self.execute.calls)

    def test_failed_to_ready_requires_mutation_api_writer(self):
        with self.assertRaises(WrongWriterForTransition):
            self.writer.transition_unit(1, FAILED, READY,
                                        writer=WRITER_ORCHESTRATOR)
        self.assertEqual([], self.execute.calls)

    def test_failed_to_ready_succeeds_for_mutation_api(self):
        self.writer.transition_unit(1, FAILED, READY,
                                    writer=WRITER_MUTATION_API)
        self.assertTrue(self.execute.calls)

    def test_quarantined_to_ready_requires_mutation_api_writer(self):
        with self.assertRaises(WrongWriterForTransition):
            self.writer.transition_unit(1, QUARANTINED, READY,
                                        writer=WRITER_RECONCILER)
        self.assertEqual([], self.execute.calls)

    def test_quarantined_to_ready_succeeds_for_mutation_api(self):
        self.writer.transition_unit(1, QUARANTINED, READY,
                                    writer=WRITER_MUTATION_API)
        self.assertTrue(self.execute.calls)

    def test_unknown_state_is_refused(self):
        with self.assertRaises(ValueError):
            self.writer.transition_unit(1, "bogus", READY,
                                        writer=WRITER_ORCHESTRATOR)


class TransitionCasTests(unittest.TestCase):
    """The SQL-level compare-and-set: a legal edge that matches zero rows."""

    def test_matching_zero_rows_raises_work_unit_not_found(self):
        execute = RecordingExecutor(affected=0)
        writer = WorkUnitWriter(execute)
        with self.assertRaises(WorkUnitNotFound):
            writer.transition_unit(1, READY, SUBMITTED,
                                   writer=WRITER_ORCHESTRATOR)

    def test_the_update_matches_on_work_unit_id_and_from_state(self):
        execute = RecordingExecutor()
        writer = WorkUnitWriter(execute)
        writer.transition_unit(99, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
        sql, params = execute.calls[0]
        self.assertIn("WHERE work_unit_id = %s AND state = %s", sql)
        self.assertIn(99, params)
        self.assertIn(READY, params)

    def test_transition_writes_a_unit_event(self):
        execute = RecordingExecutor()
        writer = WorkUnitWriter(execute)
        writer.transition_unit(1, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
        event_sql, event_params = execute.calls[1]
        self.assertIn("INSERT INTO unit_events", event_sql)
        self.assertIn(READY, event_params)
        self.assertIn(SUBMITTED, event_params)
        self.assertIn(WRITER_ORCHESTRATOR, event_params)

    def test_blocking_transition_requires_reason(self):
        # submitted->blocked IS in the graph now (rule 4 repair): retry
        # policy v1 parks application failures rather than tombstoning them,
        # so the reconciler needs this edge and the graph gained it. This
        # test previously asserted the graph REFUSED the edge, and noted that
        # the missing-reason ValueError branch was therefore unreachable —
        # now the edge is legal, so the assertion this test is named for is
        # the one it can finally make: entering `blocked` without a reason is
        # refused, because migration 036's
        # `work_units_blocked_reason_ck` makes a blocked unit with no reason
        # unrepresentable and a caller must not learn that from a constraint
        # violation three layers down.
        execute = RecordingExecutor()
        execute._select_result = [("submitted",)]
        writer = WorkUnitWriter(execute)
        with self.assertRaises(ValueError):
            writer.transition_unit(1, SUBMITTED, BLOCKED,
                                   writer=WRITER_ORCHESTRATOR)

    def test_blocking_transition_with_a_reason_is_legal(self):
        # The other half: with a reason, the edge fires and the reason lands
        # in the UPDATE's parameters. Guards against a future graph edit
        # silently removing the edge retry policy depends on.
        execute = RecordingExecutor()
        execute._select_result = [("submitted",)]
        writer = WorkUnitWriter(execute)
        writer.transition_unit(1, SUBMITTED, BLOCKED,
                               writer=WRITER_RECONCILER,
                               blocked_reason="application_failure:tool_failure")
        updates = [call for call in execute.calls
                   if call[0].startswith("UPDATE work_units")]
        self.assertTrue(updates, "no work_units UPDATE was issued")
        self.assertIn("application_failure:tool_failure", updates[0][1])


class SupersedeUnitTests(unittest.TestCase):
    def test_sets_the_pointer(self):
        execute = RecordingExecutor()
        execute._select_result = [("ready",)]
        writer = WorkUnitWriter(execute)
        writer.supersede_unit(1, 2, writer=WRITER_MUTATION_API)
        update_sql, update_params = execute.calls[0]
        self.assertIn("superseded_by_unit_id", update_sql)
        self.assertIn(2, update_params)
        self.assertIn(1, update_params)

    def test_cannot_supersede_self(self):
        execute = RecordingExecutor()
        writer = WorkUnitWriter(execute)
        with self.assertRaises(ValueError):
            writer.supersede_unit(1, 1, writer=WRITER_MUTATION_API)

    def test_already_superseded_raises_conflict(self):
        execute = RecordingExecutor(affected=0)
        writer = WorkUnitWriter(execute)
        with self.assertRaises(SupersessionConflict):
            writer.supersede_unit(1, 2, writer=WRITER_MUTATION_API)

    def test_records_an_event_with_the_pointer_in_detail(self):
        execute = RecordingExecutor()
        execute._select_result = [("ready",)]
        writer = WorkUnitWriter(execute)
        writer.supersede_unit(5, 6, writer=WRITER_MUTATION_API)
        event_sql, event_params = execute.calls[-1]
        self.assertIn("INSERT INTO unit_events", event_sql)
        detail = event_params[-1]
        # `_as_jsonb` serializes (the real driver refuses a bare dict —
        # found live); the recorded param is the deterministic JSON text.
        import json
        self.assertEqual({"superseded_by_unit_id": 6}, json.loads(detail))


class CampaignLifecycleTests(unittest.TestCase):
    def test_create_campaign_defaults_to_defined(self):
        execute = RecordingExecutor()
        writer = CampaignWriter(execute)
        campaign_id = writer.create_campaign("mock-day-1", "test")
        self.assertEqual(1, campaign_id)
        _, params = execute.calls[0]
        self.assertIn(DEFINED, params)

    def test_activate_sets_started_at(self):
        execute = RecordingExecutor()
        writer = CampaignWriter(execute)
        writer.activate_campaign(1)
        sql, _ = execute.calls[0]
        self.assertIn("started_at", sql)

    def test_full_lifecycle_defined_active_paused_active_complete(self):
        execute = RecordingExecutor()
        writer = CampaignWriter(execute)
        writer.activate_campaign(1)
        writer.pause_campaign(1)
        writer.resume_campaign(1)
        writer.complete_campaign(1)
        self.assertEqual(4, len(execute.calls))

    def test_complete_campaign_is_a_cas_against_active(self):
        # complete_campaign always declares from_state=ACTIVE (the design's
        # only completion edge) and relies on the SQL-level compare-and-set
        # — `WHERE campaign_id = %s AND state = %s` — to refuse when the
        # row is not actually 'active', exactly as `AttemptWriter`'s own
        # transitions trust the caller's declared from_state and let the
        # CAS be the real guard (see the module docstring's callout of the
        # two distinct failure modes: an illegal EDGE, refused in Python
        # before any SQL, versus a stale from_state, refused by the CAS).
        # A campaign genuinely still 'defined' therefore surfaces as
        # WorkUnitNotFound (zero rows matched), not IllegalTransition.
        execute = RecordingExecutor(affected=0)
        writer = CampaignWriter(execute)
        with self.assertRaises(WorkUnitNotFound):
            writer.complete_campaign(1)
        sql, params = execute.calls[0]
        self.assertIn("WHERE campaign_id = %s AND state = %s", sql)
        self.assertIn(ACTIVE, params)

    def test_abandon_from_paused_is_legal(self):
        execute = RecordingExecutor()
        writer = CampaignWriter(execute)
        writer.abandon_campaign(1, from_state=PAUSED)
        self.assertTrue(execute.calls)

    def test_abandon_from_complete_is_illegal(self):
        execute = RecordingExecutor()
        writer = CampaignWriter(execute)
        with self.assertRaises(IllegalTransition):
            writer.abandon_campaign(1, from_state=CAMPAIGN_COMPLETE)

    def test_campaign_transition_is_a_real_cas(self):
        execute = RecordingExecutor(affected=0)
        writer = CampaignWriter(execute)
        with self.assertRaises(WorkUnitNotFound):
            writer.activate_campaign(1)


if __name__ == "__main__":
    unittest.main()
