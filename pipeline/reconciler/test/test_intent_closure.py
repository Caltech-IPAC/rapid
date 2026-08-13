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
        # here was the tombstone the policy forbids. The shared fixture's
        # category is `config_invalid` — one of the eleven application
        # categories — so the park names its own cause in `blocked_reason`
        # rather than parking anonymously.
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
        self.assertIn("application_failure:config_invalid", params)
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
        # intermediate physical failure). `Observation.reconciler_category`
        # calls this `scheduler_provisioning` — "the attempt did not get as
        # far as running, which is precisely what that category means" — which
        # is scheduler-visible, so policy v1 returns the unit to `ready` for a
        # NEW attempt (rule 5) instead of tombstoning it.
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
        self.assertIn("ready", params)

    def test_abrupt_loss_does_not_tombstone_the_work_unit(self):
        # WAS `test_abrupt_loss_closes_the_work_unit_failed` — the single most
        # damaging instance of the rule 4 violation: exit 137 on a container
        # that HAD started closed the logical work `failed` on one physical
        # death.
        #
        # WHICH WAY IT NOW GOES DEPENDS ON THE CATEGORY, and for a started
        # container with no recognizable scheduler reason the category is
        # `internal_error`, not a scheduler one: `Observation.
        # reconciler_category` returns None once `never_ran` is false, on the
        # stated principle that it "never invents a category for an attempt
        # that had the chance to author one", and `_classify` falls back to
        # `internal_error`. So this parks rather than retries — which is the
        # conservative half of policy v1 and still not a tombstone. A REAL
        # Spot reclaim (a "Host EC2"/"Spot instance termination" status
        # reason on a container that never ran) is classified
        # `scheduler_reclaimed` and retries; see
        # `test_never_started_does_not_tombstone_the_work_unit` for the
        # scheduler-visible path.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          work_unit_id=7)
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
        self.assertIn("blocked", params)
        self.assertIn("application_failure:internal_error", params)
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
        # Only the LOST attempt is in the open set this poll reconciles (the
        # accepted sibling is already terminal AND registered, so it is not a
        # candidate) — that is exactly the late-reconciled-failure ordering
        # rule 4 cares about. The sibling exists in the table for the series
        # census to find.
        lost = attempt_row(1, lifecycle_state="started",
                           started_at=utc(2026, 8, 6, 11, 0, 0),
                           work_unit_id=55)
        accepted = attempt_row(2, lifecycle_state="terminal_after_start",
                               started_at=utc(2026, 8, 6, 12, 0, 0),
                               ended_at=utc(2026, 8, 6, 12, 5, 0),
                               work_unit_id=55,
                               registered_at=utc(2026, 8, 6, 12, 6, 0),
                               registered_record_sequence=1)
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([lost], jobs)
        # The sibling lives in the table for the series census to find. It is
        # injected directly rather than passed to `build` so that this test
        # asserts ONE attempt's closure decision: a poll that also reconciled
        # the sibling would issue that row's own transition too, and the
        # question here is narrower — what does attempt 1's failure do to a
        # unit whose work is already accepted?
        conn.rows[accepted["attempt_id"]] = dict(accepted)

        # THE SERIES CENSUS IS THE MECHANISM, asserted directly: it reports an
        # accepted sibling, which is what makes the closure a no-op.
        sibling_accepted, losses, sibling_open = svc._work_unit_series(55, 1)
        self.assertTrue(sibling_accepted,
                        "the census did not see the accepted sibling")
        self.assertEqual(0, losses)
        # The accepted sibling is `terminal_after_start`, not open — finding
        # 6's guard must not fire here, or this test's whole point (a
        # NO-OP from the accepted-sibling rule, not from the open-sibling
        # one) would be untested.
        self.assertFalse(sibling_open)

        before = len([1 for text, params in conn.statements
                      if "UPDATE work_units SET state" in text
                      and 55 in params])
        svc._close_work_unit(dict(lost), outcome="failed")
        after = [(text, params) for text, params in conn.statements
                 if "UPDATE work_units SET state" in text and 55 in params]

        self.assertEqual(
            before, len(after),
            "the unit was transitioned despite already having an accepted "
            "attempt; an intermediate physical failure cast the unit's verdict")

    def test_a_failed_attempt_leaves_the_unit_alone_while_a_sibling_runs(self):
        """FINDING 6: an earlier superseded attempt must not reopen a work
        unit whose later attempt is still in flight.

        Batch starts a retry only after the previous attempt stops, but the
        RECONCILER'S grace horizon is anchored per attempt — so attempt 1
        (which stopped first) can clear its own horizon and reach
        `_close_work_unit` while attempt 2 (which Batch already started) is
        still `started` at the scheduler. Before this repair, attempt 1's
        `RETRY_READY` disposition (a scheduler loss) transitioned the unit
        straight back to `ready` with no read of the sibling's
        `lifecycle_state` anywhere in the series census — a gatherer could
        then submit a THIRD attempt while the second was still running, and
        the second's own eventual success could no longer complete the
        unit: its `submitted -> complete` CAS expects `submitted`, and the
        unit was `ready` (or `submitted` again, under attempt 3) by then.
        """
        lost = attempt_row(1, lifecycle_state="started",
                           started_at=utc(2026, 8, 6, 11, 0, 0),
                           work_unit_id=55)
        running = attempt_row(2, lifecycle_state="started",
                              started_at=utc(2026, 8, 6, 11, 10, 0),
                              work_unit_id=55)
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([lost], jobs)
        # The running sibling lives in the table for the series census to
        # find, injected directly for the same reason
        # `test_a_failure_beside_an_accepted_sibling_leaves_the_unit_alone`
        # does: a poll that also reconciled it would issue ITS transition
        # too, and the question here is narrower — what does attempt 1's
        # failure do to a unit whose OTHER attempt is still running?
        conn.rows[running["attempt_id"]] = dict(running)

        sibling_accepted, losses, sibling_open = svc._work_unit_series(55, 1)
        self.assertTrue(sibling_open,
                        "the census did not see the running sibling")
        self.assertFalse(sibling_accepted)

        before = len([1 for text, params in conn.statements
                      if "UPDATE work_units SET state" in text
                      and 55 in params])
        svc._close_work_unit(dict(lost), outcome="failed")
        after = [(text, params) for text, params in conn.statements
                 if "UPDATE work_units SET state" in text and 55 in params]

        self.assertEqual(
            before, len(after),
            "the unit was transitioned while a sibling attempt was still "
            "open; an earlier superseded attempt reopened work a later "
            "attempt is still doing")
        # The running sibling's own row is untouched by attempt 1's closure.
        self.assertEqual("started", conn.rows[2]["lifecycle_state"])

    def test_a_full_poll_leaves_the_unit_alone_while_a_sibling_runs(self):
        """The same finding-6 scenario, end to end through `poll_once`
        rather than a direct `_close_work_unit` call: attempt 1's row is
        already past its grace horizon and reconciles to a scheduler-loss
        disposition in this cycle; attempt 2's row is still `started` and
        the scheduler still reports it RUNNING, so it stays `waiting`. The
        unit must come out of this poll untouched either way.
        """
        lost = attempt_row(1, lifecycle_state="started",
                           application_attempt_index=1,
                           started_at=utc(2026, 8, 6, 11, 0, 0),
                           work_unit_id=55)
        running = attempt_row(2, lifecycle_state="started",
                              application_attempt_index=2,
                              started_at=utc(2026, 8, 6, 11, 10, 0),
                              work_unit_id=55)
        # One job, two attempts in its history: the first stopped (a
        # scheduler-visible loss, exit 137) and the second is still running
        # — exactly the shape Batch produces for an in-flight retry.
        jobs = [batch_job(status="RUNNING", started=utc(2026, 8, 6, 11, 10, 0),
                          attempts=[
                              {"container": {"exitCode": 137},
                               "startedAt": utc(2026, 8, 6, 11, 0, 0)
                                   .timestamp() * 1000,
                               "stoppedAt": utc(2026, 8, 6, 11, 5, 0)
                                   .timestamp() * 1000},
                              {"container": {"exitCode": None},
                               "startedAt": utc(2026, 8, 6, 11, 10, 0)
                                   .timestamp() * 1000},
                          ])]
        svc, conn, _, _, _ = build(
            [lost, running], jobs, now=utc(2026, 8, 6, 11, 30, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual(1, summary["waiting"])
        transitions = [text for text, params in conn.statements
                       if "UPDATE work_units SET state" in text
                       and 55 in params]
        self.assertEqual(
            [], transitions,
            "the unit was transitioned while attempt 2 was still running")
        self.assertEqual("terminal_after_start", conn.rows[1]["lifecycle_state"])
        self.assertEqual("started", conn.rows[2]["lifecycle_state"])

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
