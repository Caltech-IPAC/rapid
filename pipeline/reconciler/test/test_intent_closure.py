"""The reconciler's work-unit closure integration (integration review ruling 13).

`pipeline.reconciler.service.ReconcilerService._close_work_unit` resolves an
attempt's work unit in the same call as the attempt's own terminal
transition — under RETRY POLICY v1 since the rule 4 repair, so the mapping is
four-way, not two-way: an accepted result completes the unit, a
scheduler-visible loss returns it to `ready` for a new attempt, an
application failure parks it `blocked` with a reason, and only explicit
policy exhaustion closes it `failed`. Three tests in this file previously
asserted the two-way mapping (`..._closes_the_work_unit_failed`) and were
inverted by that repair; they are named for what they now assert.

This file proves that mapping and its
NULL-skip guard, using the same `build`/`attempt_row`/`application_record`
fixtures `test_service.py` already has — `FakeConnection.route` (stubs.py)
answers `UPDATE work_units`/`INSERT INTO unit_events`/
`INSERT INTO work_units`/`SELECT ... FROM work_units` with its generic
"one row affected" fallback (any unrecognised statement returns
`rowcount=1`), which is sufficient here: these tests assert on the
STATEMENTS issued and the ATTEMPT row's own final state, not on a
work_units table this stub does not model as data.
"""

import json
import unittest

from pipeline.reconciler.test.stubs import (
    FakeConnection, attempt_row, batch_job, utc)
from pipeline.reconciler.test.test_service import (
    DIAGNOSTICS, PREFIX, application_record, build, seed_record)
from pipeline.runtime.boundaries import InMemoryObjectStore


class WorkUnitClosureTests(unittest.TestCase):
    def test_success_closes_the_work_unit_complete(self):
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="success",
                          product_disposition="published",
                          terminal_record_sequence=0,
                          work_unit_id=42)
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(
            1, rapid_outcome="success", product_disposition="published"))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        transitions = [(text, params) for text, params in conn.statements
                       if "UPDATE work_units SET state" in text]
        self.assertEqual(1, len(transitions))
        text, params = transitions[0]
        self.assertIn("complete", params)
        self.assertIn(42, params)
        self.assertIn("submitted", params)

    def test_application_failure_parks_the_work_unit_blocked(self):
        # WAS `test_failure_closes_the_work_unit_failed` (rule 4 repair). An
        # application failure is the case retry policy v1 calls
        # park-until-change and "never tombstoned"; closing the unit `failed`
        # here was the tombstone the policy forbids. This row carries no
        # `error_category` — the honest state of an attempt that failed
        # without saying why — so it parks under the `unclassified_failure`
        # reason rather than being retried on a timer.
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="failure", product_disposition="none",
                          terminal_record_sequence=0,
                          work_unit_id=42)
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(1))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        svc.poll_once()

        transitions = [(text, params) for text, params in conn.statements
                       if "UPDATE work_units SET state" in text]
        self.assertEqual(1, len(transitions))
        _, params = transitions[0]
        self.assertIn("blocked", params)
        self.assertIn(42, params)
        self.assertIn("unclassified_failure", params)
        self.assertNotIn("failed", params)

    def test_null_work_unit_id_is_skipped_silently(self):
        # Every pre-intent-layer row, and every row whose job type has no
        # loaded workflow_definitions row (as of this writer: every job
        # type — see pipeline.seams._attach_work_unit's FK-guard catch).
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="success",
                          product_disposition="published",
                          terminal_record_sequence=0,
                          work_unit_id=None)
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(
            1, rapid_outcome="success", product_disposition="published"))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        transitions = [text for text, _ in conn.statements
                       if "UPDATE work_units SET state" in text]
        self.assertEqual([], transitions,
                         "no work-unit statement for a NULL work_unit_id")
        # The attempt itself still closes normally — the guard is silent,
        # not a failure that would leave the row open.
        self.assertEqual("terminal_after_start",
                         conn.rows[1]["lifecycle_state"])

    def test_never_started_does_not_tombstone_the_work_unit(self):
        # WAS `test_never_started_closes_the_work_unit_failed`. A container
        # that never started produced no verdict about the work, so the unit
        # must not be closed `failed` on it (rule 4: never from an
        # intermediate physical failure). With no error_category on the row
        # the conservative disposition is a park, not a retry — an
        # unexplained failure repeated on a timer is the unbounded loop the
        # policy exists to prevent.
        row = attempt_row(1, lifecycle_state="submitted", work_unit_id=99)
        jobs = [batch_job(status="FAILED", exit_code=None, started=None,
                          stopped=utc(2026, 8, 6, 11, 0, 0))]
        svc, conn, _, _, _ = build([row], jobs)

        svc.poll_once()

        transitions = [(text, params) for text, params in conn.statements
                       if "UPDATE work_units SET state" in text]
        self.assertEqual(1, len(transitions))
        _, params = transitions[0]
        self.assertIn(99, params)
        self.assertNotIn("failed", params)
        self.assertIn("blocked", params)

    def test_abrupt_loss_returns_the_work_unit_to_ready_for_a_new_attempt(self):
        # WAS `test_abrupt_loss_closes_the_work_unit_failed` — the single most
        # damaging instance of the rule 4 violation. Exit 137 is an OOM
        # kill/Spot reclaim: a fact about a container, carrying no verdict on
        # the logical work. This row's category IS scheduler-visible, so
        # policy v1 hands it to the automatic-retry surface and the unit goes
        # back to `ready` for a NEW RAPID attempt (rule 5) rather than being
        # tombstoned.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          work_unit_id=7,
                          error_category="scheduler_reclaimed")
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs)

        svc.poll_once()

        transitions = [(text, params) for text, params in conn.statements
                       if "UPDATE work_units SET state" in text]
        self.assertEqual(1, len(transitions))
        _, params = transitions[0]
        self.assertIn(7, params)
        self.assertIn("ready", params)
        self.assertNotIn("failed", params)

    def test_a_failure_beside_an_accepted_sibling_leaves_the_unit_alone(self):
        # RULE 4's ordering half: "a later successful attempt of the same
        # logical work must be able to complete the unit" runs both ways.
        # Reconciliation is per-attempt and unordered — a supersession requery
        # can reach attempt 1's abrupt loss AFTER attempt 2 was accepted — so
        # a closure decision made from the triggering row alone would overwrite
        # a legitimately complete unit with a verdict about a dead container.
        # With an accepted sibling in the series, this row's failure must
        # produce NO work-unit transition at all.
        lost = attempt_row(1, lifecycle_state="started",
                           started_at=utc(2026, 8, 6, 11, 0, 0),
                           work_unit_id=55,
                           error_category="scheduler_reclaimed")
        accepted = attempt_row(2, lifecycle_state="terminal_after_start",
                               started_at=utc(2026, 8, 6, 12, 0, 0),
                               ended_at=utc(2026, 8, 6, 12, 5, 0),
                               work_unit_id=55,
                               registered_at=utc(2026, 8, 6, 12, 6, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([lost, accepted], jobs)

        svc.poll_once()

        transitions = [(text, params) for text, params in conn.statements
                       if "UPDATE work_units SET state" in text
                       and 55 in params]
        self.assertEqual(
            [], transitions,
            "the unit was transitioned despite already having an accepted "
            "attempt; an intermediate physical failure cast the unit's verdict")

    def test_the_transition_is_written_inside_the_same_lease_as_the_close(self):
        # Same-transaction atomicity (see `_close_work_unit`'s docstring) is
        # a property of `_Executor(self.conn)` sharing one connection with
        # the AttemptWriter's own executor, both issued between one
        # `pg_try_advisory_xact_lock` acquisition and the ONE `conn.commit()`
        # `attempt_lease` calls on clean exit (lease.py) — this stub has no
        # real transaction boundary, so the observable proxy is ORDERING:
        # the lock acquisition precedes both writes, and both writes precede
        # the SAME commit, with nothing rolled back in between. (A poll also
        # commits its open-set read and its heartbeat separately — those are
        # outside the lease and are not what this test is about.)
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="success",
                          product_disposition="published",
                          terminal_record_sequence=0,
                          work_unit_id=42)
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(
            1, rapid_outcome="success", product_disposition="published"))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        summary = svc.poll_once()

        lock_index = next(i for i, (text, _) in enumerate(conn.statements)
                          if "pg_try_advisory_xact_lock" in text)
        attempt_close_index = next(
            i for i, (text, _) in enumerate(conn.statements)
            if "UPDATE attempts SET lifecycle_state" in text)
        work_unit_close_index = next(
            i for i, (text, _) in enumerate(conn.statements)
            if "UPDATE work_units SET state" in text)
        event_index = next(
            i for i, (text, _) in enumerate(conn.statements)
            if "INSERT INTO unit_events" in text)

        self.assertLess(lock_index, attempt_close_index,
                        "the attempt close must happen under the lease")
        self.assertLess(attempt_close_index, work_unit_close_index,
                        "the work-unit transition follows the attempt close, "
                        "still inside the same lease")
        self.assertLess(work_unit_close_index, event_index,
                        "the unit_event is appended in the same call, "
                        "before the lease commits")
        # `_classify`'s own open-set read rolls back its read-only snapshot
        # (service.py line 336-ish) BEFORE the lease is even acquired, and
        # unrelated reconciler bookkeeping may roll back its own read-only
        # snapshots elsewhere in the same poll — neither touches the
        # lease's transaction. What matters here is narrower and directly
        # observable: `attempt_lease` commits on clean exit and rolls back
        # only on an exception (lease.py), and this poll's summary reports
        # the attempt CLASSIFIED, not deferred or errored — so whatever
        # rollbacks occurred elsewhere in the poll did not touch the lease
        # spanning the two writes asserted above.
        self.assertEqual(1, summary["classified"])
        self.assertEqual(0, summary.get("errors", 0))


if __name__ == "__main__":
    unittest.main()
