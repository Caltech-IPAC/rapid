"""The polling loop and one attempt's reconciliation, with every boundary stubbed."""

import json
import unittest

from pipeline.reconciler import service
from pipeline.reconciler.test.stubs import (
    FakeBatch, FakeConnection, FakeS3Tagging, attempt_row, batch_job, ms, utc)
from pipeline.runtime.boundaries import InMemoryObjectStore

PREFIX = "attempts"
DIAGNOSTICS = "roman-rapid-diagnostics"


def build(rows, jobs, now=utc(2026, 8, 6, 12, 0, 0), lease_granted=True,
          records=None):
    conn = FakeConnection(rows=rows, lease_granted=lease_granted)
    batch = FakeBatch(jobs=jobs)
    store = records or InMemoryObjectStore()
    tagging = FakeS3Tagging()
    svc = service.ReconcilerService(
        conn=conn, batch_client=batch, records_store=store,
        diagnostics_store=store, s3_client=tagging,
        records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
        now=lambda: now)
    return svc, conn, batch, store, tagging


def application_record(attempt_id, **overrides):
    body = {
        "schema_version": 2, "record_sequence": 0, "record_author": "application",
        "attempt_id": attempt_id, "run_id": "run-1",
        "logical_job_id": "90000/1", "scheduler_job_id": "job-abc",
        "rapid_outcome": "failure", "product_disposition": "none",
        "application_intended_exit": 0, "error_category": "config_invalid",
        "bundle_key": "attempts/bundles/run-1/90000_1/attempt-1.tar.gz",
        "bundle_checksum": "bundle-sha",
    }
    body.update(overrides)
    return body


def seed_record(store, row, body, sequence=0):
    from pipeline.runtime import termination
    key = termination.terminal_record_key(
        PREFIX, row["run_id"], row["logical_job_id"], row["attempt_id"],
        sequence)
    store.put_if_absent(key, json.dumps(body).encode("utf-8"),
                        content_type="application/json")
    return key


class OpenSetTests(unittest.TestCase):
    def test_only_nonterminal_states_are_polled(self):
        rows = [
            attempt_row(1, lifecycle_state="submitted"),
            attempt_row(2, lifecycle_state="started"),
            attempt_row(3, lifecycle_state="application_closed"),
            attempt_row(4, lifecycle_state="terminal_after_start"),
            attempt_row(5, lifecycle_state="terminal_without_start"),
        ]
        svc, _, _, _, _ = build(rows, jobs=[])

        open_rows = svc.open_attempts()

        self.assertEqual([1, 2, 3], [r["attempt_id"] for r in open_rows])

    def test_the_open_set_read_holds_no_transaction(self):
        svc, conn, _, _, _ = build([attempt_row(1)], jobs=[])
        svc.open_attempts()
        self.assertGreaterEqual(conn.rollbacks, 1)


class DeferralTests(unittest.TestCase):
    def test_a_running_attempt_is_observed_but_left_open(self):
        rows = [attempt_row(1, lifecycle_state="started")]
        jobs = [batch_job(status="RUNNING", started=utc(2026, 8, 6, 11, 0, 0))]
        svc, conn, _, _, _ = build(rows, jobs)

        summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        # The observation is still recorded — an operator can see queue and
        # start times long before anything terminal happens.
        self.assertTrue(any("scheduler_state" in text.lower() or
                            "scheduler_started_at" in text.lower()
                            for text, _ in conn.statements))

    def test_terminal_but_inside_the_grace_horizon_waits(self):
        rows = [attempt_row(1, lifecycle_state="started")]
        jobs = [batch_job(status="SUCCEEDED",
                          started=utc(2026, 8, 6, 11, 55, 0),
                          stopped=utc(2026, 8, 6, 11, 57, 0))]
        svc, _, _, _, _ = build(rows, jobs, now=utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        self.assertEqual(0, summary["classified"])

    def test_an_unresolved_child_inside_its_horizon_waits(self):
        rows = [attempt_row(1, scheduler_job_id=None,
                            submitted_at=utc(2026, 8, 6, 11, 50, 0))]
        svc, _, _, _, _ = build(rows, jobs=[], now=utc(2026, 8, 6, 12, 0, 0))

        self.assertEqual(1, svc.poll_once()["deferred"])


class AgreedClosureTests(unittest.TestCase):
    def test_an_application_closed_attempt_gets_an_agreed_closure_record(self):
        # An application_closed row always carries its own started_at: the
        # runtime writes it in the started-CAS long before it can close.
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="failure", product_disposition="none",
                          terminal_record_sequence=0)
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(1))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, tagging = build([row], jobs, records=store)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        # A sequence-1 closure record exists, and it is a complete snapshot.
        key = "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"
        body = json.loads(store.get(key))
        self.assertEqual("reconciler", body["record_author"])
        self.assertEqual("failure", body["rapid_outcome"])
        self.assertEqual("SUCCEEDED", body["scheduler_state"])
        self.assertFalse(body["reconstructed"])
        # The row reached terminal_after_start.
        self.assertEqual("terminal_after_start",
                         conn.rows[1]["lifecycle_state"])
        # The bundle was tagged failure-class: the application failed even
        # though the scheduler succeeded.
        self.assertEqual("failure",
                         list(tagging.tags.values())[0]["retention-class"])

    def test_a_started_row_beside_a_valid_record_is_materialized(self):
        # The W5 canary's attempt 32 state exactly: the record is written and
        # valid, the application-closed transition never happened.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(1))
        jobs = [batch_job(status="FAILED", exit_code=70,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        body = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        # The application's own account is carried verbatim, not re-derived.
        self.assertEqual("failure", body["rapid_outcome"])
        self.assertEqual(0, body["application_intended_exit"])
        # ...beside the scheduler's contradicting exit code, preserved.
        self.assertEqual(70, body["scheduler_observed_exit"])
        self.assertFalse(body["reconstructed"])


class ReconcilerFirstTests(unittest.TestCase):
    def test_a_never_started_attempt_is_closed_without_start(self):
        row = attempt_row(1, lifecycle_state="submitted")
        jobs = [batch_job(status="FAILED", exit_code=None, started=None,
                          stopped=utc(2026, 8, 6, 11, 0, 0),
                          status_reason="CannotPullContainerError: nope")]
        svc, conn, _, store, _ = build([row], jobs)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])
        body = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        self.assertTrue(body["reconstructed"])
        self.assertEqual("scheduler_provisioning", body["error_category"])

    def test_a_started_attempt_with_no_record_is_an_abrupt_loss(self):
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0),
                          status_reason="Host EC2 instance terminated")]
        svc, conn, _, store, _ = build([row], jobs)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        body = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        self.assertTrue(body["reconstructed"])
        self.assertEqual(closure_reason(body), "absent")

    def test_a_checksum_invalid_record_is_replaced_and_the_key_cited(self):
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        store = InMemoryObjectStore()
        key = seed_record(store, row, application_record(1))
        store.objects[key]["body"] = b'{"attempt_id": 1, "tampered": true}'
        jobs = [batch_job(status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, _, _ = build([row], jobs, records=store)

        svc.poll_once()

        body = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        self.assertTrue(body["reconstructed"])
        self.assertEqual("checksum_invalid",
                         body["rejected_predecessor"]["reason"])
        self.assertIn("seq-0000.json", body["rejected_predecessor"]["key"])

    def test_an_unresolved_child_past_its_horizon_is_classified(self):
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 0, 0))
        svc, conn, _, store, _ = build([row], jobs=[],
                                       now=utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])


def closure_reason(body):
    return body.get("rejected_predecessor", {}).get("reason")


class ConstraintFidelityTests(unittest.TestCase):
    """States the DDL forbids must never be attempted. All found live."""

    def test_a_row_with_no_start_is_never_closed_as_terminal_after_start(self):
        # terminal_after_start requires started_at IS NOT NULL. The scheduler
        # reporting a start for the JOB does not mean this attempt started.
        row = attempt_row(1, lifecycle_state="submitted", started_at=None)
        jobs = [batch_job(status="FAILED", exit_code=126,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs)

        svc.poll_once()

        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])

    def test_application_facts_with_no_start_are_flagged_contradictory(self):
        # terminal_without_start forbids rapid_outcome/product_disposition;
        # terminal_after_start requires started_at. A row with an outcome and
        # no start satisfies neither and must not be forced into either.
        row = attempt_row(1, lifecycle_state="submitted", started_at=None,
                          rapid_outcome="success",
                          product_disposition="published")
        jobs = [batch_job(status="FAILED", exit_code=None, started=None,
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs)

        svc.poll_once()

        self.assertEqual("missing_or_contradictory",
                         conn.rows[1]["lifecycle_state"])

    def test_an_unresolvable_id_on_a_row_that_ran_is_contradictory(self):
        # The scheduler knows nothing about the job (a wrong id, or one aged
        # out of Batch's retention) but the row carries a full application
        # account. terminal_without_start forbids those fields, so asserting
        # "never started" would contradict the row's own evidence.
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="success",
                          product_disposition="published",
                          application_intended_exit=0,
                          scheduler_job_id="an-id-batch-never-heard-of",
                          submitted_at=utc(2026, 8, 6, 10, 0, 0))
        svc, conn, _, _, _ = build([row], jobs=[],
                                   now=utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("missing_or_contradictory",
                         conn.rows[1]["lifecycle_state"])

    def test_an_unresolvable_id_on_a_row_that_never_ran_is_never_started(self):
        row = attempt_row(1, lifecycle_state="submitted", started_at=None,
                          scheduler_job_id="an-id-batch-never-heard-of",
                          submitted_at=utc(2026, 8, 6, 10, 0, 0))
        svc, conn, _, _, _ = build([row], jobs=[],
                                   now=utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])

    def test_a_started_row_is_closed_as_terminal_after_start(self):
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0),
                          status_reason="Host EC2 instance terminated")]
        svc, conn, _, _, _ = build([row], jobs)

        svc.poll_once()

        self.assertEqual("terminal_after_start",
                         conn.rows[1]["lifecycle_state"])


class LeaseTests(unittest.TestCase):
    def test_an_attempt_leased_elsewhere_is_skipped_not_queued_behind(self):
        row = attempt_row(1, lifecycle_state="started")
        jobs = [batch_job(status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, lease_granted=False)

        summary = svc.poll_once()

        self.assertEqual(1, summary["skipped"])
        self.assertEqual(0, summary["classified"])
        # The scheduler observation IS still recorded — it is unconditionally
        # the reconciler's to author and does not need the lease. What the
        # lease guards is classification, so the row must not have been
        # transitioned by the reconciler that lost the race.
        self.assertNotIn(conn.rows[1]["lifecycle_state"],
                         ("terminal_after_start", "terminal_without_start"))
        transitions = [text for text, _ in conn.statements
                       if "lifecycle_state = %s" in text]
        self.assertEqual([], transitions)

    def test_a_row_closed_between_poll_and_lease_is_skipped(self):
        row = attempt_row(1, lifecycle_state="started")
        jobs = [batch_job(status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs)
        # Another reconciler wins the race after the open-set read.
        original = svc.open_attempts

        def steal():
            rows = original()
            conn.rows[1]["lifecycle_state"] = "terminal_after_start"
            return rows

        svc.open_attempts = steal

        summary = svc.poll_once()

        self.assertEqual(1, summary["skipped"])


class SequenceTests(unittest.TestCase):
    def test_the_reconciler_never_writes_sequence_zero(self):
        svc, _, _, _, _ = build([], jobs=[])
        self.assertEqual(1, svc._next_sequence(attempt_row(1)))

    def test_a_correction_climbs_from_the_recorded_sequence(self):
        svc, _, _, _, _ = build([], jobs=[])
        row = attempt_row(1, terminal_record_sequence=2)
        self.assertEqual(3, svc._next_sequence(row))


class BatchingTests(unittest.TestCase):
    def test_the_whole_open_set_is_described_in_hundreds(self):
        rows = [attempt_row(n, scheduler_job_id=f"job-{n}") for n in range(150)]
        jobs = [batch_job(job_id=f"job-{n}", status="RUNNING",
                          started=utc(2026, 8, 6, 11, 0, 0))
                for n in range(150)]
        svc, _, batch, _, _ = build(rows, jobs)

        svc.poll_once()

        self.assertEqual([100, 50], [len(call) for call in batch.calls])


class RetryHistoryTests(unittest.TestCase):
    def test_each_attempt_row_pairs_with_its_own_observation(self):
        # Two rows for one job: attempt 1 was reclaimed, attempt 2 ran.
        rows = [
            attempt_row(1, lifecycle_state="started",
                        application_attempt_index=1),
            attempt_row(2, lifecycle_state="started",
                        application_attempt_index=2),
        ]
        jobs = [batch_job(status="SUCCEEDED", attempts=[
            {"startedAt": ms(utc(2026, 8, 6, 10, 0, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 2, 0)),
             "statusReason": "Host EC2 instance terminated",
             "container": {"exitCode": 137}},
            {"startedAt": ms(utc(2026, 8, 6, 10, 10, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 12, 0)),
             "container": {"exitCode": 0}},
        ])]
        svc, _, _, store, _ = build(rows, jobs)

        svc.poll_once()

        first = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        second = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-2/seq-0001.json"))
        # The exit codes did not get crossed between the two attempts.
        self.assertEqual(137, first["scheduler_observed_exit"])
        self.assertEqual(0, second["scheduler_observed_exit"])


class ResilienceTests(unittest.TestCase):
    def test_one_bad_attempt_does_not_take_the_cycle_down(self):
        rows = [attempt_row(1, lifecycle_state="started"),
                attempt_row(2, lifecycle_state="started",
                            scheduler_job_id="job-two")]
        jobs = [batch_job(status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0)),
                batch_job(job_id="job-two", status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, store, _ = build(rows, jobs)

        # Make the first attempt's closure publication explode.
        real_publish = service.closure_mod.publish_closure_record
        calls = {"n": 0}

        def flaky(store_, prefix, row, record):
            calls["n"] += 1
            if row["attempt_id"] == 1:
                raise RuntimeError("simulated store outage")
            return real_publish(store_, prefix, row, record)

        service.closure_mod.publish_closure_record = flaky
        try:
            summary = svc.poll_once()
        finally:
            service.closure_mod.publish_closure_record = real_publish

        self.assertEqual(1, summary["errors"])
        self.assertEqual(1, summary["classified"])

    def test_a_submitted_row_is_not_given_a_scheduler_state(self):
        # The DDL forbids scheduler_state on a `submitted` row, and it is
        # right to: a row still claiming nothing has started must not carry a
        # scheduler verdict beside that claim. Found live — the first real
        # cycle raised CheckViolation on attempts_state_submitted_check.
        row = attempt_row(1, lifecycle_state="submitted")
        jobs = [batch_job(status="RUNNING", started=utc(2026, 8, 6, 11, 0, 0))]
        svc, conn, _, _, _ = build([row], jobs)

        svc.poll_once()

        observations = [(text, params) for text, params in conn.statements
                        if "scheduler_state" in text and text.startswith("UPDATE")]
        self.assertTrue(observations, "the observation was not recorded at all")
        for _, params in observations:
            # The state parameter is first in record_scheduler_observation.
            self.assertIsNone(params[0])

    def test_a_failing_attempt_rolls_back_so_the_next_one_still_runs(self):
        # PostgreSQL aborts the whole transaction on a failed statement:
        # without a rollback, every later attempt dies with
        # InFailedSqlTransaction and one bad row kills the cycle. Found live.
        rows = [attempt_row(1, lifecycle_state="started"),
                attempt_row(2, lifecycle_state="started",
                            scheduler_job_id="job-two")]
        jobs = [batch_job(status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0)),
                batch_job(job_id="job-two", status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, store, _ = build(rows, jobs)

        real_publish = service.closure_mod.publish_closure_record

        def flaky(store_, prefix, row, record):
            if row["attempt_id"] == 1:
                raise RuntimeError("simulated statement failure")
            return real_publish(store_, prefix, row, record)

        rollbacks_before = conn.rollbacks
        service.closure_mod.publish_closure_record = flaky
        try:
            summary = svc.poll_once()
        finally:
            service.closure_mod.publish_closure_record = real_publish

        self.assertEqual(1, summary["errors"])
        # The second attempt still got through — the point of the rollback.
        self.assertEqual(1, summary["classified"])
        self.assertGreater(conn.rollbacks, rollbacks_before)

    def test_run_forever_survives_a_failing_cycle(self):
        cycles = {"n": 0}

        class Exploding:
            def poll_once(self):
                cycles["n"] += 1
                raise RuntimeError("boom")

        service.run_forever(
            Exploding(), poll_seconds=0, sleep=lambda _: None,
            should_continue=lambda: cycles["n"] < 3)

        self.assertEqual(3, cycles["n"])


if __name__ == "__main__":
    unittest.main()
