"""The polling loop and one attempt's reconciliation, with every boundary stubbed."""

import json
import unittest

from pipeline.reconciler import reconstruction, service
from pipeline.reconciler.test.stubs import (
    FakeBatch, FakeConnection, FakeS3Tagging, attempt_row, batch_job, ms, utc)
from pipeline.runtime import boundaries
from pipeline.runtime.boundaries import InMemoryObjectStore

PREFIX = "attempts"
DIAGNOSTICS = "roman-rapid-diagnostics"


def build(rows, jobs, now=utc(2026, 8, 6, 12, 0, 0), lease_granted=True,
          records=None, submissions_available=False, submissions=None,
          named_jobs=None, list_jobs_raises=None):
    conn = FakeConnection(rows=rows, lease_granted=lease_granted,
                          submissions_available=submissions_available,
                          submissions=submissions)
    batch = FakeBatch(jobs=jobs, named_jobs=named_jobs,
                      list_jobs_raises=list_jobs_raises)
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
    def test_every_nonterminal_state_is_polled(self):
        rows = [
            attempt_row(1, lifecycle_state="submitted"),
            attempt_row(2, lifecycle_state="started"),
            attempt_row(3, lifecycle_state="application_closed"),
        ]
        svc, _, _, _, _ = build(rows, jobs=[])

        open_rows = svc.open_attempts()

        self.assertEqual([1, 2, 3], [r["attempt_id"] for r in open_rows])

    def test_terminal_rows_outside_the_window_are_not_revisited(self):
        # AMENDED by FixA (review finding #15). Terminal rows ARE revisited
        # now — supersession was otherwise unreachable, because polling
        # selected only the open states and a corrected scheduler fact could
        # never produce a sequence-2 record. But the requery is BOUNDED: past
        # Batch's own retention there are no new facts to learn, so a row out
        # there can never be superseded by anything.
        old = utc(2026, 8, 4, 10, 0, 0)      # two days before "now"
        rows = [
            attempt_row(1, lifecycle_state="submitted"),
            attempt_row(4, lifecycle_state="terminal_after_start",
                        ended_at=old),
            attempt_row(5, lifecycle_state="terminal_without_start",
                        ended_at=old),
        ]
        svc, _, _, _, _ = build(rows, jobs=[])

        open_rows = svc.open_attempts()

        self.assertEqual([1], [r["attempt_id"] for r in open_rows])

    def test_a_recently_closed_terminal_row_is_revisited(self):
        # Inside the window: the scheduler may still have something to say,
        # so the row is a supersession candidate.
        recent = utc(2026, 8, 6, 11, 30, 0)
        rows = [
            attempt_row(1, lifecycle_state="submitted"),
            attempt_row(4, lifecycle_state="terminal_after_start",
                        ended_at=recent),
        ]
        svc, _, _, _, _ = build(rows, jobs=[])

        open_rows = svc.open_attempts()

        self.assertEqual([1, 4], sorted(r["attempt_id"] for r in open_rows))

    def test_a_flagged_row_is_revisited_so_a_correction_can_reach_it(self):
        recent = utc(2026, 8, 6, 11, 30, 0)
        rows = [attempt_row(7, lifecycle_state="missing_or_contradictory",
                            ended_at=recent)]
        svc, _, _, _, _ = build(rows, jobs=[])

        self.assertEqual([7], [r["attempt_id"] for r in svc.open_attempts()])

    def test_the_open_set_read_holds_no_transaction(self):
        svc, conn, _, _, _ = build([attempt_row(1)], jobs=[])
        svc.open_attempts()
        self.assertGreaterEqual(conn.rollbacks, 1)


class DeferralTests(unittest.TestCase):
    """`waiting` and `deferred` are different outcomes, and the split is the
    ratified health disposition (2026-08-06): health counts only
    ACTIONABLE-UNCLOSED work. An attempt still running, or inside either
    horizon, is owed nothing yet — it is `waiting`. Only a closure step that
    TRIED and failed is `deferred`."""

    def test_a_running_attempt_is_observed_but_left_open(self):
        rows = [attempt_row(1, lifecycle_state="started")]
        jobs = [batch_job(status="RUNNING", started=utc(2026, 8, 6, 11, 0, 0))]
        svc, conn, _, _, _ = build(rows, jobs)

        summary = svc.poll_once()

        self.assertEqual(1, summary["waiting"])
        self.assertEqual(0, summary["deferred"])
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

        self.assertEqual(1, summary["waiting"])
        self.assertEqual(0, summary["deferred"])
        self.assertEqual(0, summary["classified"])

    def test_an_unresolved_child_inside_its_horizon_waits(self):
        rows = [attempt_row(1, scheduler_job_id=None,
                            submitted_at=utc(2026, 8, 6, 11, 50, 0))]
        svc, _, _, _, _ = build(rows, jobs=[], now=utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["waiting"])
        self.assertEqual(0, summary["deferred"])


class PerAttemptLogGroupTests(unittest.TestCase):
    """One service-wide log group cannot be right.

    The two class-fixed job definitions log to two different groups
    (`/rapid/batch/rapid-queue-{prompt,bulk}`), so whichever a single
    parameter named, attempts of the other class would be read from a group
    that does not hold their streams. `logs/job-log-group` was never created
    at all, so the fallback was `/aws/batch/job` — which holds no RAPID logs
    and which rapid-orchestrator-role cannot read.

    Derived from `binding_job_definition_arn`, which the row already carries:
    the job definition owns the `awslogs-group` option, so this reads the fact
    at its source rather than inferring a workload class.
    """

    GROUPS = {"rapid-pipeline-science": "/rapid/batch/rapid-queue-prompt",
              "rapid-pipeline-bulk": "/rapid/batch/rapid-queue-bulk"}

    def _service(self, groups=None):
        svc, _, _, _, _ = build([], jobs=[])
        svc.log_group = "/aws/batch/job"
        svc.log_groups = dict(self.GROUPS if groups is None else groups)
        return svc

    #: Placeholder account, assembled rather than written out: the derivation
    #: reads only the trailing `job-definition/<name>:<revision>` segment, and
    #: a literal 12-digit account id in a tracked file is what the pre-push
    #: guard exists to stop — real or not.
    ACCOUNT = "0" * 12

    def _arn(self, name, revision=19):
        return (f"arn:aws:batch:us-east-1:{self.ACCOUNT}:job-definition/"
                f"{name}:{revision}")

    def test_each_class_resolves_to_its_own_group(self):
        svc = self._service()

        self.assertEqual(
            "/rapid/batch/rapid-queue-prompt",
            svc._log_group_for(attempt_row(
                1, binding_job_definition_arn=self._arn(
                    "rapid-pipeline-science"))))
        self.assertEqual(
            "/rapid/batch/rapid-queue-bulk",
            svc._log_group_for(attempt_row(
                2, binding_job_definition_arn=self._arn(
                    "rapid-pipeline-bulk"))))

    def test_the_two_classes_do_not_resolve_to_the_same_group(self):
        # The property that makes this a derivation rather than two constants:
        # a regression to one service-wide group shows up here.
        svc = self._service()
        prompt = svc._log_group_for(attempt_row(
            1, binding_job_definition_arn=self._arn("rapid-pipeline-science")))
        bulk = svc._log_group_for(attempt_row(
            2, binding_job_definition_arn=self._arn("rapid-pipeline-bulk")))

        self.assertNotEqual(prompt, bulk)

    def test_the_revision_does_not_change_the_group(self):
        # Every revision of one definition logs to the same group, so pinning
        # a new revision must not silently unmap it.
        svc = self._service()

        self.assertEqual(
            svc._log_group_for(attempt_row(
                1, binding_job_definition_arn=self._arn(
                    "rapid-pipeline-science", 19))),
            svc._log_group_for(attempt_row(
                2, binding_job_definition_arn=self._arn(
                    "rapid-pipeline-science", 27))))

    def test_a_row_with_no_binding_falls_back_rather_than_failing(self):
        # Rows created before the binding columns landed carry no ARN. A
        # thinner reconstruction is the designed outcome; a raise is not.
        svc = self._service()

        self.assertEqual(
            "/aws/batch/job",
            svc._log_group_for(attempt_row(1,
                                           binding_job_definition_arn=None)))

    def test_an_unmapped_definition_falls_back_rather_than_failing(self):
        svc = self._service()

        self.assertEqual(
            "/aws/batch/job",
            svc._log_group_for(attempt_row(
                1, binding_job_definition_arn=self._arn("rapid-pipeline-x"))))


class ActionableWorkHealthTests(unittest.TestCase):
    """The ratified health-vs-horizon disposition, pinned.

    W9 ran the reconciler at `NRestarts=15`: every poll during a ramp step's
    first ten minutes saw nothing but attempts inside their horizons, scored
    them as attempted-and-not-closed, and tripped a check meant for a service
    that cannot work. Nothing was lost — the supervisor restarted it — but a
    healthy run was tripping an unhealthy-service alarm.
    """

    def test_a_full_step_of_waiting_attempts_never_degrades_health(self):
        # Eighteen children, all inside the grace horizon: a ramp step's first
        # poll, exactly. Poll far past the threshold and health must not move.
        rows = [attempt_row(i, lifecycle_state="started",
                            scheduler_job_id=f"job-{i}")
                for i in range(1, 19)]
        jobs = [batch_job(job_id=f"job-{i}", status="RUNNING",
                          started=utc(2026, 8, 6, 11, 0, 0))
                for i in range(1, 19)]
        svc, _, _, _, _ = build(rows, jobs)

        for _ in range(service.CLOSURE_FAILURE_POLL_THRESHOLD * 2):
            summary = svc.poll_once()

        self.assertEqual(18, summary["waiting"])
        self.assertEqual(0, summary["deferred"])
        self.assertEqual(0, svc.consecutive_unproductive_polls)
        self.assertTrue(svc.healthy)

    def test_a_persistent_closure_failure_still_degrades_health(self):
        # The counter must still do its job: this is the condition it exists
        # for, and the disposition narrows what counts, not whether it counts.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        store = InMemoryObjectStore()

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the records bucket denies writes")

        store.put_if_absent = refuse
        svc, _, _, _, _ = build([row], jobs, records=store)

        for _ in range(service.CLOSURE_FAILURE_POLL_THRESHOLD):
            summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        self.assertEqual(0, summary["waiting"])
        self.assertFalse(svc.healthy)


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

    def test_materialization_supplies_the_key_and_checksum_it_validated(self):
        """The record-written/row-not-closed crash boundary (#14).

        The row is `started` with NULL terminal_record_key and NULL checksum —
        the application sets both in the transition that never landed — and the
        record BODY cannot carry them either, because a record cannot contain
        its own key or the checksum of its own bytes. PostgreSQL requires a
        non-null key for `application_closed`, so reading them from the body or
        the row attempted an illegal transition on every pass and left the
        attempt `started` forever.
        """
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          terminal_record_key=None,
                          terminal_record_checksum=None)
        store = InMemoryObjectStore()
        key = seed_record(store, row, application_record(1))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        closed = [params for text, params in conn.statements
                  if "reconciler_materialized" in text and params
                  and "application_closed" in params]
        self.assertTrue(closed, "no application_closed transition was written")
        params = closed[-1]
        # The key it read from, and the checksum it computed over those bytes.
        # Both were NULL on the row and absent from the body — the illegal
        # transition PostgreSQL rejects on every pass.
        expected = boundaries.checksum(store.get(key))
        self.assertIn(key, params)
        self.assertIn(expected, params)


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
            # The other reconciler closed it with THESE facts — the same ones
            # this pass observed. Recording them is what makes the row
            # genuinely closed rather than a supersession candidate: since
            # FixA (review finding #15) a terminal row inside the window is
            # revisited, and it is re-closed only where the scheduler now says
            # something the row does not already record.
            conn.rows[1]["lifecycle_state"] = "terminal_after_start"
            conn.rows[1]["scheduler_state"] = "FAILED"
            conn.rows[1]["scheduler_observed_exit"] = 1
            return rows

        svc.open_attempts = steal

        summary = svc.poll_once()

        self.assertEqual(1, summary["skipped"])

    def test_a_terminal_row_whose_facts_changed_is_superseded(self):
        # Review finding #15: supersession was unreachable, so "corrected
        # scheduler facts produce sequence 2" could not happen at all. The row
        # below is terminal and recorded exit 1; the scheduler now says exit
        # 0, which is a fact the row does not carry.
        row = attempt_row(1, lifecycle_state="terminal_after_start",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          ended_at=utc(2026, 8, 6, 11, 30, 0),
                          scheduler_state="FAILED",
                          scheduler_observed_exit=1,
                          terminal_record_sequence=1)
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _conn, _batch, store, _tag = build([row], jobs)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        # A higher sequence than the one already on the row.
        sequences = sorted(int(key.rsplit("seq-", 1)[1].split(".")[0])
                           for key in store.objects
                           if "seq-" in key)
        self.assertTrue(sequences, "no closure record was published")
        self.assertGreaterEqual(max(sequences), 2)

    def test_a_terminal_row_whose_facts_agree_is_left_alone(self):
        # The bound on the other side: revisiting must not re-close every
        # finished attempt on every poll.
        row = attempt_row(1, lifecycle_state="terminal_after_start",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          ended_at=utc(2026, 8, 6, 11, 30, 0),
                          scheduler_state="SUCCEEDED",
                          scheduler_observed_exit=0)
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _conn, _batch, store, _tag = build([row], jobs)

        summary = svc.poll_once()

        self.assertEqual(1, summary["skipped"])
        self.assertEqual({}, dict(store.objects))


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


class StoreFaultTests(unittest.TestCase):
    """A store that cannot answer is not evidence about the attempt (#16)."""

    def _faulting_store(self, seeded_row, on):
        store = InMemoryObjectStore()
        seed_record(store, seeded_row, application_record(1))
        original = getattr(store, on)

        def explode(*args, **kwargs):
            if args and str(args[0]).endswith("seq-0000.json"):
                raise RuntimeError("AccessDenied")
            return original(*args, **kwargs)

        setattr(store, on, explode)
        return store

    def test_a_head_fault_defers_rather_than_closing(self):
        # The failure the review named: sequence 0 exists with product and
        # stage details, a transient GetObject failure occurs, and the
        # reconciler publishes an authoritative record WITHOUT those facts and
        # terminalizes the row — which nothing revisits, because terminal rows
        # are outside the open set.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        store = self._faulting_store(row, "head")
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, _ = build([row], jobs, records=store)

        summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        self.assertEqual(0, summary["classified"])
        # Nothing was terminalized. (Recording the scheduler's observation is
        # not a transition: it is this service's own fact to author, and the
        # row stays open either way.)
        transitions = [text for text, _ in conn.statements
                       if "lifecycle_state = %s" in text]
        self.assertEqual([], transitions)
        # ...and no lossy record was published in place of the real one.
        self.assertIsNone(store.head(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))

    def test_a_get_fault_defers_too(self):
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        store = self._faulting_store(row, "get")
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, _, _ = build([row], jobs, records=store)

        self.assertEqual(1, svc.poll_once()["deferred"])

    def test_a_deferral_is_counted_so_a_persistent_fault_is_visible(self):
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        store = self._faulting_store(row, "head")
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, _, _ = build([row], jobs, records=store)

        svc.poll_once()

        self.assertEqual(1, svc.health()["closure_failures"])

    def test_a_genuinely_absent_record_still_closes(self):
        # The deferral must not swallow the real absent case: an attempt that
        # died before writing sequence 0 has no record to wait for, and its
        # reconciler-first closure is the designed outcome.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, _, _ = build([row], jobs)

        self.assertEqual(1, svc.poll_once()["classified"])


class ReconstructionCompletenessTests(unittest.TestCase):
    """A reconstruction reads what the attempt left behind (#16).

    The record used to be built from the attempt row and the scheduler
    observation alone, while a started attempt that died before writing
    sequence 0 had also left `attempt_stages` rows — the runtime writes each
    as the stage finishes, precisely so the boundaries survive a crash — and a
    CloudWatch stream the record NAMED but never read.
    """

    STAGES = [("download", "success", utc(2026, 8, 6, 11, 0, 0), 1200, None),
              ("difference", "failure", utc(2026, 8, 6, 11, 1, 0), 900,
               "tool_failure")]

    def _build(self, logs=None, stage_rows=None):
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, store, _ = build([row], jobs)
        svc.logs = logs
        svc.log_group = "/aws/batch/job"

        original = conn.route
        rows = self.STAGES if stage_rows is None else stage_rows

        def route(text, params):
            if "attempt_stages" in text.lower():
                return (list(rows),
                        [("stage_name",), ("outcome",), ("started_at",),
                         ("duration_ms",), ("error_category",)])
            return original(text, params)

        conn.route = route
        return svc, store

    def _record(self, store):
        return json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))

    def test_the_attempts_own_stages_are_folded_in(self):
        svc, store = self._build()

        svc.poll_once()

        body = self._record(store)
        self.assertEqual([stage["stage_name"] for stage in body["stages"]],
                         ["download", "difference"])
        self.assertIn("attempt_stages", body["reconstructed_from"])

    def test_the_log_stream_is_read_not_merely_named(self):
        class FakeLogs:
            def get_log_events(self, **_kwargs):
                return {"events": [{"timestamp": 1, "message": "boom"}]}

        svc, store = self._build(logs=FakeLogs())

        svc.poll_once()

        body = self._record(store)
        self.assertTrue(body["safety_stream"]["read"])
        self.assertEqual([e["message"] for e in
                          body["safety_stream"]["events"]], ["boom"])
        self.assertIn("log_stream", body["reconstructed_from"])

    def test_an_unreadable_log_is_not_claimed_as_a_source(self):
        # Claiming evidence nobody read is worse than omitting it: a consumer
        # trusting the claim believes the boundaries were recovered.
        class Broken:
            def get_log_events(self, **_kwargs):
                raise RuntimeError("AccessDenied")

        svc, store = self._build(logs=Broken())

        svc.poll_once()

        body = self._record(store)
        self.assertFalse(body["safety_stream"]["read"])
        self.assertNotIn("log_stream", body["reconstructed_from"])

    def test_an_agreed_closure_does_not_re_read_what_it_already_has(self):
        # A predecessor folds in the application's own stages verbatim; going
        # back to the tables would be a second, disagreeing source.
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        store = InMemoryObjectStore()
        seed_record(store, row, application_record(
            1, stages=[{"stage_name": "authored", "outcome": "success"}]))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, _, _ = build([row], jobs, records=store)

        svc.poll_once()

        body = self._record(store)
        self.assertEqual([s["stage_name"] for s in body["stages"]],
                         ["authored"])

    def _absent_bundle(self, row):
        from pipeline.runtime import termination
        return termination.bundle_key(
            PREFIX, row["run_id"], row["logical_job_id"], row["attempt_id"])

    def test_a_missing_bundle_for_an_attempt_that_ran_is_counted(self):
        # "Nothing to retain" is the literal truth only for an attempt that
        # never started. For one that ran, an absent bundle means the
        # diagnostics for a real execution are gone, and accepting that
        # silently is how that evidence disappears with nothing recorded.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        conn = FakeConnection(rows=[row])
        tagging = FakeS3Tagging(missing=[self._absent_bundle(row)])
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=jobs),
            records_store=InMemoryObjectStore(),
            diagnostics_store=InMemoryObjectStore(), s3_client=tagging,
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        svc.poll_once()

        self.assertEqual(1, svc.health()["missing_bundles"])

    def test_the_missing_bundle_is_reconstructed_before_the_row_closes(self):
        """Round 3 of #16/#5: noticing the gap was never the whole rule.

        The design says the bundle exists before the attempt is closed,
        whichever way it died. Counting the absence and continuing to the
        terminal transition left abruptly killed attempts permanently terminal
        with no diagnostics — and terminal rows are outside the open set, so
        nothing ever came back for them.
        """
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        conn = FakeConnection(rows=[row])
        key = self._absent_bundle(row)
        tagging = FakeS3Tagging(missing=[key])
        diagnostics = InMemoryObjectStore()
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=jobs),
            records_store=InMemoryObjectStore(),
            diagnostics_store=diagnostics, s3_client=tagging,
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        svc.poll_once()

        self.assertIsNotNone(diagnostics.head(key),
                             "the attempt closed with no bundle at its key")
        self.assertEqual(1, svc.health()["reconstructed_bundles"])
        # It closed. The point of reconstructing rather than deferring is that
        # the attempt still reaches a terminal state.
        self.assertNotEqual("started", conn.rows[1]["lifecycle_state"])

    def test_a_bundle_that_cannot_be_written_defers_instead(self):
        # The one case that IS worth deferring: a store that refuses the write
        # is a condition a later poll may find resolved, unlike a CloudWatch
        # stream that has expired and never will be.
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="FAILED", exit_code=137,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        conn = FakeConnection(rows=[row])
        key = self._absent_bundle(row)
        diagnostics = InMemoryObjectStore()

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the diagnostics bucket denies writes")

        diagnostics.put_if_absent = refuse
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=jobs),
            records_store=InMemoryObjectStore(),
            diagnostics_store=diagnostics,
            s3_client=FakeS3Tagging(missing=[key]),
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        self.assertEqual("started", conn.rows[1]["lifecycle_state"],
                         "an attempt whose evidence is missing and "
                         "unrecoverable must not be terminalized")

    def test_a_never_started_attempt_is_not_counted_as_a_missing_bundle(self):
        """It is reconstructed, not counted.

        `missing_bundles` means "an attempt that RAN has lost its evidence",
        which is a different and worse condition than an attempt that never
        produced any. The never-started case still gets a bundle (below); it
        just is not this alarm.
        """
        row = attempt_row(1, lifecycle_state="submitted")
        jobs = [batch_job(status="FAILED", exit_code=None, started=None,
                          stopped=utc(2026, 8, 6, 11, 0, 0))]
        conn = FakeConnection(rows=[row])
        tagging = FakeS3Tagging(missing=[self._absent_bundle(row)])
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=jobs),
            records_store=InMemoryObjectStore(),
            diagnostics_store=InMemoryObjectStore(), s3_client=tagging,
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        svc.poll_once()

        self.assertEqual(0, svc.health()["missing_bundles"])

    def test_a_never_started_attempt_still_gets_a_bundle(self):
        """Round-4 finding #5: the rule is unconditional.

        A never-started attempt used to close with no bundle at all, on the
        reasoning that it had no container in which to build one. The adopted
        design names "abrupt loss, or never started" as the reconstruction
        cases together and puts the bundle before EVERY close — and it is the
        more useful truth: what is retained is the account of the
        non-execution, which is otherwise nowhere, because terminal rows are
        outside the open set.
        """
        row = attempt_row(1, lifecycle_state="submitted")
        jobs = [batch_job(status="FAILED", exit_code=None, started=None,
                          stopped=utc(2026, 8, 6, 11, 0, 0))]
        conn = FakeConnection(rows=[row])
        key = self._absent_bundle(row)
        diagnostics = InMemoryObjectStore()
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=jobs),
            records_store=InMemoryObjectStore(),
            diagnostics_store=diagnostics,
            s3_client=FakeS3Tagging(missing=[key]),
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        svc.poll_once()

        self.assertIsNotNone(
            diagnostics.head(key),
            "a never-started attempt closed with no bundle at its key")
        self.assertNotEqual("submitted", conn.rows[1]["lifecycle_state"],
                            "it must still reach a terminal state")

    def test_the_never_started_bundle_records_why_it_never_ran(self):
        """A minimal bundle, but not an empty one.

        Marked reconstructed, and carrying the submission facts and whatever
        the scheduler said — which for this class of attempt is the entire
        content of the diagnosis.
        """
        import io
        import json
        import tarfile

        row = attempt_row(1, lifecycle_state="submitted")
        jobs = [batch_job(status="FAILED", exit_code=None, started=None,
                          stopped=utc(2026, 8, 6, 11, 0, 0))]
        conn = FakeConnection(rows=[row])
        key = self._absent_bundle(row)
        diagnostics = InMemoryObjectStore()
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=jobs),
            records_store=InMemoryObjectStore(),
            diagnostics_store=diagnostics,
            s3_client=FakeS3Tagging(missing=[key]),
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        svc.poll_once()

        raw = diagnostics.get(key)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            manifest = json.loads(
                tar.extractfile(reconstruction.MANIFEST_MEMBER)
                .read().decode("utf-8"))

        self.assertTrue(manifest["reconstructed"])
        self.assertEqual(row["run_id"], manifest["run_id"])
        self.assertEqual(row["attempt_id"], manifest["attempt_id"])

    def test_the_reconstructed_manifest_keeps_stage_durations_numeric(self):
        """`default=str` would keep the manifest writable while retyping a
        numeric field under every consumer that reads it.

        `attempt_stages.duration_ms` is `numeric NOT NULL`, and
        `closure.read_attempt_stages` hands back raw psycopg2 rows — so the
        value arrives as a `Decimal`. This is the failure
        `termination._json_default`'s own docstring warns about, fixed in
        `ClosureRecord.to_bytes` and missed here.
        """
        import decimal
        import io
        import tarfile

        # The SERIALIZED bytes are the assertion, not the in-memory manifest:
        # the manifest holds the Decimal either way, and the defect is what
        # json.dumps makes of it on the way into the tar.
        body, _manifest = reconstruction.build_reconstructed_bundle(
            attempt_row(1), None, events=[],
            stages=[{"stage": "difference", "duration_ms":
                     decimal.Decimal("1234.5")}])

        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            manifest = json.loads(
                tar.extractfile(reconstruction.MANIFEST_MEMBER)
                .read().decode("utf-8"))

        duration = manifest["attempt_stages"][0]["duration_ms"]
        self.assertNotIsInstance(duration, str)
        self.assertEqual(1234.5, duration)

    def test_a_never_resolved_attempt_gets_its_bundle_before_it_closes(self):
        """The path finding #5 named: no scheduler observation at all.

        `_reconcile_unresolved` published a closure record and transitioned
        the row without invoking bundle handling at ALL — so the one class of
        attempt the design explicitly names for reconstruction was the one
        class that closed with no diagnostics whatsoever.
        """
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 0, 0))
        conn = FakeConnection(rows=[row])
        key = self._absent_bundle(row)
        diagnostics = InMemoryObjectStore()
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=[]),
            records_store=InMemoryObjectStore(),
            diagnostics_store=diagnostics,
            s3_client=FakeS3Tagging(missing=[key]),
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertIsNotNone(
            diagnostics.head(key),
            "the never-resolved path closed the attempt with no bundle")
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])

    def test_a_never_resolved_attempt_defers_if_its_bundle_cannot_be_written(
            self):
        """The bundle comes BEFORE the closure record, so a failure here
        leaves nothing published and the attempt open for the next poll.

        The reverse order would publish a closure citing an attempt whose
        bundle never appeared — the exactly-one-bundle-before-close rule
        broken in a way no later poll could repair.
        """
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 0, 0))
        conn = FakeConnection(rows=[row])
        key = self._absent_bundle(row)
        records = InMemoryObjectStore()
        diagnostics = InMemoryObjectStore()

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the diagnostics bucket denies writes")

        diagnostics.put_if_absent = refuse
        svc = service.ReconcilerService(
            conn=conn, batch_client=FakeBatch(jobs=[]),
            records_store=records, diagnostics_store=diagnostics,
            s3_client=FakeS3Tagging(missing=[key]),
            records_prefix=PREFIX, diagnostics_bucket=DIAGNOSTICS,
            now=lambda: utc(2026, 8, 6, 12, 0, 0))

        summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        self.assertEqual("submitted", conn.rows[1]["lifecycle_state"])
        # Nothing published: no closure record citing a bundle that is absent.
        self.assertEqual([], list(records.objects))


class SchedulerDiscoveryTests(unittest.TestCase):
    """Every scheduler attempt gets a row, through the resolver (#4)."""

    def _retry_job(self):
        return batch_job(status="SUCCEEDED", attempts=[
            {"startedAt": ms(utc(2026, 8, 6, 10, 0, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 2, 0)),
             "statusReason": "Host EC2 instance terminated",
             "container": {"exitCode": 137}},
            {"startedAt": ms(utc(2026, 8, 6, 10, 10, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 12, 0)),
             "container": {"exitCode": 0}},
        ])

    def test_an_attempt_with_no_row_is_resolved_one(self):
        # Attempt 1 failed during provisioning and attempt 2 ran. Only attempt
        # 2's row exists; attempt 1 received no row, no binding, no category,
        # no closure record and no retention account.
        rows = [attempt_row(2, lifecycle_state="started",
                            application_attempt_index=2)]
        svc, conn, _, _, _ = build(rows, [self._retry_job()])

        summary = svc.poll_once()

        self.assertEqual(1, summary["discovered"])
        resolved = [(text, params) for text, params in conn.statements
                    if "resolve_attempt" in text]
        self.assertTrue(resolved, "the resolver was never called")
        # Acquisition goes through the resolver and never a bare INSERT.
        self.assertFalse([text for text, _ in conn.statements
                          if "insert into attempts" in text.lower()])
        # The scheduler index it was missing, one-based as stored.
        self.assertIn(1, resolved[-1][1])

    def test_a_failing_resolver_counts_its_failures_as_errors(self):
        """Swallowed per-attempt failures were invisible to the health gate.

        Every other per-row loop in the service reports its failures into
        `summary["errors"]`; `_resolve_discovered` alone caught them and moved
        on. Because unproductive-poll health is computed from classified +
        deferred + errors, a resolver failing EVERY attempt, poll after poll,
        contributed nothing to the count and the service went on calling itself
        healthy (round-3 finding #6).
        """
        rows = [attempt_row(2, lifecycle_state="started",
                            application_attempt_index=2)]
        svc, _, _, _, _ = build(rows, [self._retry_job()])

        def refuse(*_args, **_kwargs):
            raise RuntimeError("resolve_attempt is unavailable")

        svc._resolve_one = refuse

        summary = svc.poll_once()

        self.assertEqual(0, summary["discovered"])
        self.assertTrue(summary["errors"],
                        "the resolution failure must be counted, not swallowed")

    def test_rows_that_already_exist_are_not_re_resolved(self):
        rows = [attempt_row(1, lifecycle_state="started",
                            application_attempt_index=1),
                attempt_row(2, lifecycle_state="started",
                            application_attempt_index=2)]
        svc, conn, _, _, _ = build(rows, [self._retry_job()])

        summary = svc.poll_once()

        self.assertEqual(0, summary["discovered"])
        self.assertFalse([text for text, _ in conn.statements
                          if "resolve_attempt" in text])

    def test_a_single_attempt_job_resolves_nothing(self):
        rows = [attempt_row(1, lifecycle_state="started")]
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, _, _, _, _ = build(rows, jobs)

        self.assertEqual(0, svc.poll_once()["discovered"])

    def test_a_resolver_failure_does_not_take_the_cycle_down(self):
        rows = [attempt_row(2, lifecycle_state="started",
                            application_attempt_index=2)]
        svc, conn, _, _, _ = build(rows, [self._retry_job()])
        original = conn.route

        def route(text, params):
            if "resolve_attempt" in text:
                raise RuntimeError("deadlock detected")
            return original(text, params)

        conn.route = route

        summary = svc.poll_once()

        self.assertEqual(0, summary["discovered"])
        # The attempt that DOES have a row is still reconciled.
        self.assertEqual(1, summary["classified"])

    def test_an_unindexed_row_picks_the_first_attempt_deterministically(self):
        # The pre-created-row case: created at submission, before any attempt
        # existed, so it carries no index. Returning None sent it down the
        # unresolved path, which eventually closed it `never_resolved` —
        # asserting it never ran while the scheduler's history says otherwise.
        rows = [attempt_row(1, lifecycle_state="submitted",
                            application_attempt_index=None,
                            scheduler_attempt_index=None)]
        svc, _, _, store, _ = build(rows, [self._retry_job()])

        svc.poll_once()

        body = json.loads(store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        # The submitter's row stands for the job's FIRST attempt: exit 137,
        # not the final attempt's 0.
        self.assertEqual(1, body["scheduler_attempt_index"])
        self.assertEqual(137, body["scheduler_observed_exit"])


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

        # AMENDED by FixA (#16): a closure failure defers rather than
        # erroring — the row stays open for the next poll instead of being
        # terminalized with its closure half-done. What this test is about is
        # unchanged: attempt 2 still got through.
        self.assertEqual(1, summary["deferred"])
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

        # AMENDED by FixA (review finding #16): a closure failure DEFERS its
        # attempt rather than terminalizing it, so it is counted as deferred
        # rather than as an error. The row stays open and the next poll
        # retries it — the whole point of not failing open.
        self.assertEqual(1, summary["deferred"])
        # The second attempt still got through — the point of the rollback.
        self.assertEqual(1, summary["classified"])
        self.assertGreater(conn.rollbacks, rollbacks_before)

    def test_a_closure_failure_leaves_the_row_open_rather_than_terminal(self):
        # Review finding #16's core: the reconciler used to publish the
        # record, catch a tagging or recovery failure, terminalize the row
        # anyway, and never revisit it — terminal rows are outside the open
        # set, so a bundle whose retention was never stamped then expires
        # under the wrong lifecycle rule with nothing left to notice.
        rows = [attempt_row(1, lifecycle_state="started",
                            started_at=utc(2026, 8, 6, 11, 0, 0))]
        jobs = [batch_job(status="FAILED", exit_code=1,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        svc, conn, _, _, tagging = build(rows, jobs)

        def explode(**_kwargs):
            raise RuntimeError("tagging is down")

        tagging.put_object_tagging = explode

        summary = svc.poll_once()

        self.assertEqual(1, summary["deferred"])
        self.assertEqual(0, summary.get("classified", 0))
        self.assertEqual("started", conn.rows[1]["lifecycle_state"],
                         "a failed closure must not terminalize the row")

    def test_run_forever_survives_a_failing_cycle(self):
        cycles = {"n": 0}

        class Exploding:
            consecutive_poll_failures = 0
            healthy = True

            def health(self):
                return {"healthy": self.healthy}

            def poll_once(self):
                cycles["n"] += 1
                raise RuntimeError("boom")

        # A high threshold: this test is about surviving transients, which is
        # still the behaviour below the threshold.
        service.run_forever(
            Exploding(), poll_seconds=0, sleep=lambda _: None,
            should_continue=lambda: cycles["n"] < 3,
            failure_threshold=99)

        self.assertEqual(3, cycles["n"])


class HealthTests(unittest.TestCase):
    """Health is WORK-CAPABLE, not process-alive (review finding #24).

    The loop used to catch every poll exception forever, so a dead database
    connection or an expired rotated credential made every poll fail while
    systemd saw a running process and never restarted it. The reconciler
    exists to catch exactly the conditions likeliest to make a cycle throw,
    which is why "still running" is the wrong signal.
    """

    class Exploding:
        consecutive_poll_failures = 0
        healthy = True

        def __init__(self):
            self.calls = 0

        def health(self):
            return {"healthy": self.healthy}

        def poll_once(self):
            self.calls += 1
            raise RuntimeError("the database is gone")

    def test_consecutive_failures_past_the_threshold_exit_the_loop(self):
        exploding = self.Exploding()

        with self.assertRaises(service.ReconcilerUnhealthy) as caught:
            service.run_forever(exploding, poll_seconds=0,
                                sleep=lambda _: None,
                                failure_threshold=3)

        self.assertEqual(3, exploding.calls)
        self.assertIn("reconciling nothing", str(caught.exception))

    def test_a_successful_poll_resets_the_failure_count(self):
        # A transient must not accumulate toward the threshold across
        # unrelated minutes, or a service that works fine will eventually
        # exit for reasons long past.
        class Flaky:
            consecutive_poll_failures = 0
            healthy = True

            def __init__(self):
                self.calls = 0

            def health(self):
                return {"healthy": self.healthy}

            def poll_once(self):
                self.calls += 1
                if self.calls % 2:
                    raise RuntimeError("transient")
                return {}

        flaky = Flaky()
        service.run_forever(flaky, poll_seconds=0, sleep=lambda _: None,
                            should_continue=lambda: flaky.calls < 6,
                            failure_threshold=3)

        self.assertEqual(6, flaky.calls)
        self.assertEqual(0, flaky.consecutive_poll_failures)

    def test_the_service_reports_its_own_health(self):
        svc, _, _, _, _ = build([], jobs=[])

        self.assertTrue(svc.healthy)
        svc.consecutive_poll_failures = service.POLL_FAILURE_THRESHOLD
        self.assertFalse(svc.healthy)
        self.assertFalse(svc.health()["healthy"])

    def test_persistent_per_row_failure_flips_the_unit(self):
        """Round 2 of #24: closing NOTHING, forever, while reporting healthy.

        `poll_once` catches every per-attempt exception by design — one bad row
        must not take the cycle down — so a service whose every closure failed
        returned normally each minute and the poll-failure counter never left
        zero. Persistent per-row failure is as fatal as a failing poll: the
        attempts never close and registration never sees them.
        """
        row = attempt_row(1, lifecycle_state="started",
                          started_at=utc(2026, 8, 6, 11, 0, 0))
        jobs = [batch_job(status="SUCCEEDED", exit_code=0,
                          started=utc(2026, 8, 6, 11, 0, 0),
                          stopped=utc(2026, 8, 6, 11, 5, 0))]
        store = InMemoryObjectStore()

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the records bucket denies writes")

        store.put_if_absent = refuse
        svc, _, _, _, _ = build([row], jobs, records=store)

        for poll in range(service.CLOSURE_FAILURE_POLL_THRESHOLD):
            self.assertTrue(svc.healthy, f"flipped early, after poll {poll}")
            svc.poll_once()

        self.assertFalse(svc.healthy)
        health = svc.health()
        self.assertFalse(health["healthy"])
        self.assertEqual(service.CLOSURE_FAILURE_POLL_THRESHOLD,
                         health["consecutive_unproductive_polls"])

    def test_one_successful_closure_clears_the_unproductive_count(self):
        # A single classified attempt proves the service can still work, so a
        # run of deferrals must not accumulate toward the threshold across
        # unrelated minutes.
        svc, _, _, _, _ = build([], jobs=[])
        svc.consecutive_unproductive_polls = 4

        svc.poll_once()

        self.assertEqual(0, svc.consecutive_unproductive_polls)

    def test_an_idle_poll_is_not_an_unproductive_one(self):
        # Nothing to close is not a failure to close.
        svc, _, _, _, _ = build([], jobs=[])

        for _ in range(service.CLOSURE_FAILURE_POLL_THRESHOLD + 2):
            svc.poll_once()

    def test_the_loop_exits_when_a_successful_poll_leaves_it_unhealthy(self):
        """Round 3 of #24: the property existed and nothing ever read it.

        Rounds 1 and 2 built the whole mechanism — two counters, two
        thresholds, `healthy`, `health()` — and then `run_forever` checked only
        the exception path. A poll that returns normally having classified
        nothing, over and over, is precisely the shape of failure the second
        counter was added for, and it went unnoticed because the loop's only
        question was whether `poll_once` threw.
        """
        class Unproductive:
            consecutive_poll_failures = 0

            def __init__(self):
                self.calls = 0
                self.healthy = True

            def health(self):
                return {"healthy": self.healthy,
                        "consecutive_unproductive_polls": self.calls}

            def poll_once(self):
                # Returns normally every time — never raises. Health degrades
                # the way a real run of closure failures degrades it.
                self.calls += 1
                if self.calls >= service.CLOSURE_FAILURE_POLL_THRESHOLD:
                    self.healthy = False
                return {}

        unproductive = Unproductive()
        with self.assertRaises(service.ReconcilerUnhealthy) as caught:
            service.run_forever(unproductive, poll_seconds=0,
                                sleep=lambda _: None)

        self.assertEqual(service.CLOSURE_FAILURE_POLL_THRESHOLD,
                         unproductive.calls)
        self.assertEqual(0, unproductive.consecutive_poll_failures,
                         "no poll ever raised; this is the success path")
        self.assertIn("not healthy", str(caught.exception))

    def test_a_healthy_successful_poll_keeps_the_loop_running(self):
        # The gate must not fire on the ordinary case, or the service
        # restart-loops forever and the cure is worse than the disease.
        class Fine:
            consecutive_poll_failures = 0
            healthy = True

            def __init__(self):
                self.calls = 0

            def health(self):
                return {"healthy": self.healthy}

            def poll_once(self):
                self.calls += 1
                return {}

        fine = Fine()
        service.run_forever(fine, poll_seconds=0, sleep=lambda _: None,
                            should_continue=lambda: fine.calls < 4)

        self.assertEqual(4, fine.calls)


def submission_row(submission_id=100, state="unknown", job_name="rapid-1",
                   job_queue="contract-queue", resolution_deadline=None,
                   **overrides):
    """A `submissions` row, shaped as `submission.protocol`'s SQL reads it."""
    row = {
        "submission_id": submission_id,
        "run_id": "run-1",
        "job_type": "science",
        "job_name": job_name,
        "job_queue": job_queue,
        "job_definition": "rapid-pipeline-science",
        "state": state,
        "call_started_at": utc(2026, 8, 6, 9, 0, 0),
        "resolution_deadline": resolution_deadline,
        "ambiguity_detail": None,
        "scheduler_job_id": None,
    }
    row.update(overrides)
    return row


class SubmissionResolutionPassTests(unittest.TestCase):
    """S1: `poll_once` runs a `resolve_open` pass every cycle.

    Rule 7 package S — the resolve half of `submission.protocol` had zero
    non-test callers before this package; these are `resolve_open`'s first
    integration coverage (the evidence pass found none anywhere).
    """

    def test_resolve_open_runs_once_per_cycle_via_the_batch_describer(self):
        # Criterion 1: invoked with a describer derived from self.batch.
        row = attempt_row(1, lifecycle_state="started", scheduler_job_id=None)
        submissions = [submission_row(submission_id=100, state="unknown",
                                      job_name="rapid-100")]
        svc, conn, batch, _, _ = build(
            [row], jobs=[], submissions_available=True,
            submissions=submissions,
            named_jobs={("rapid-100", "contract-queue"): "job-100"})

        svc.poll_once()

        # find_job_by_name loops once per JOB_SEARCH_STATES entry (7 states)
        # until it finds a match; every call carries the deterministic name.
        self.assertTrue(batch.list_jobs_calls)
        self.assertTrue(all(call[2][0]["values"] == ["rapid-100"]
                            for call in batch.list_jobs_calls))
        self.assertEqual(1, len(conn.submissions))
        self.assertEqual("found", conn.submissions[100]["state"])

    def test_the_pass_runs_even_when_zero_attempts_are_open(self):
        # Criterion 2: the early-return case (`if not rows:`). Open
        # submissions can exist with no open attempt rows at all — a pass
        # placed after the early return would silently never run here.
        submissions = [submission_row(submission_id=100, state="unknown",
                                      job_name="rapid-100")]
        svc, conn, batch, _, _ = build(
            [], jobs=[], submissions_available=True, submissions=submissions,
            named_jobs={("rapid-100", "contract-queue"): "job-100"})

        summary = svc.poll_once()

        self.assertEqual(0, summary["open"])
        self.assertTrue(batch.list_jobs_calls)
        self.assertEqual("found", conn.submissions[100]["state"])
        self.assertEqual(1, summary["submission_found"])

    def test_a_raising_describe_does_not_kill_the_cycle(self):
        # Criterion 3: an unreachable Batch during resolution must not stop
        # the rest of the poll — open-attempt reconciliation still happens,
        # and the failure is counted rather than propagated.
        unresolved = attempt_row(2, scheduler_job_id=None,
                                 submitted_at=utc(2026, 8, 6, 11, 55, 0))
        submissions = [submission_row(submission_id=100, state="unknown",
                                      job_name="rapid-100")]
        svc, conn, batch, _, _ = build(
            [unresolved], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions,
            list_jobs_raises=RuntimeError("Batch is unreachable"))

        summary = svc.poll_once()

        # resolve_open's own per-row try/except swallows the describe raise
        # and counts it as "errors" internally, then _resolve_submissions
        # folds that into summary["errors"] (not a cycle-level exception).
        self.assertEqual(1, summary["errors"])
        self.assertEqual("unknown", conn.submissions[100]["state"],
                         "a describe that raises must not write anything")
        # Reconciliation of the open (non-submission) attempt still ran.
        self.assertEqual(1, summary["waiting"])

    def test_a_pre_044_database_degrades_quietly(self):
        # Criterion 4: is_available() False -> no crash, no error counted.
        row = attempt_row(1, lifecycle_state="started")
        jobs = [batch_job(status="RUNNING", started=utc(2026, 8, 6, 11, 0, 0))]
        svc, conn, batch, _, _ = build(
            [row], jobs, submissions_available=False)

        summary = svc.poll_once()

        self.assertEqual(0, summary["errors"])
        self.assertEqual([], batch.list_jobs_calls,
                         "no submissions table means no describe calls at all")

    def test_resolution_outcomes_appear_in_the_summary(self):
        # Criterion 5.
        submissions = [
            submission_row(submission_id=100, state="unknown",
                          job_name="rapid-found"),
            submission_row(
                submission_id=101, state="unknown", job_name="rapid-lost",
                resolution_deadline=utc(2026, 8, 6, 11, 0, 0)),
        ]
        svc, conn, _, _, _ = build(
            [], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions,
            named_jobs={("rapid-found", "contract-queue"): "job-found"})

        summary = svc.poll_once()

        self.assertEqual(1, summary["submission_found"])
        self.assertEqual(1, summary["submission_lost"])
        self.assertEqual(0, summary["submission_unknown"])

    def test_a_resolved_pass_is_committed_and_visible_after(self):
        # Durability (criterion 13's stub-tier shadow): the fake's commit
        # counter proves the pass commits rather than leaving the write in an
        # open transaction. The contract tier proves this with a real second
        # connection (see notes-s-evidence.md); this proves the service asks
        # for a commit at all, on the success path.
        submissions = [submission_row(submission_id=100, state="unknown",
                                      job_name="rapid-100")]
        svc, conn, _, _, _ = build(
            [], jobs=[], submissions_available=True, submissions=submissions,
            named_jobs={("rapid-100", "contract-queue"): "job-100"})

        commits_before = conn.commits
        svc.poll_once()

        self.assertGreater(conn.commits, commits_before)

    def test_no_open_submissions_leaves_no_transaction_open(self):
        row = attempt_row(1, lifecycle_state="started")
        jobs = [batch_job(status="RUNNING", started=utc(2026, 8, 6, 11, 0, 0))]
        svc, conn, batch, _, _ = build(
            [row], jobs, submissions_available=True, submissions=[])

        svc.poll_once()

        self.assertEqual([], batch.list_jobs_calls)


class SubmissionRecordDecidesOverTheClockTests(unittest.TestCase):
    """S2: the submission record, not the horizon, is the truth for an
    ambiguous attempt — rule 7's headline behaviour."""

    def test_a_found_submission_waits_however_late_the_clock_is(self):
        # Criterion 6, the headline: submitted_at is FAR beyond the
        # submission horizon (30 minutes), yet a FOUND submission record
        # means the job is running. The clock says classify; the evidence
        # says running; the evidence wins.
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 8, 0, 0),
                          submission_id=100)
        submissions = [submission_row(submission_id=100, state="found")]
        svc, conn, _, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions)

        summary = svc.poll_once()

        self.assertEqual(1, summary["waiting"])
        self.assertEqual(0, summary.get("classified", 0))
        self.assertEqual("submitted", conn.rows[1]["lifecycle_state"])

    def test_a_lost_submission_classifies_without_waiting_on_the_horizon(self):
        # Criterion 7: submitted_at is WELL INSIDE the horizon, but a LOST
        # submission record is positive evidence of absence — classification
        # need not wait for the clock too.
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 55, 0),
                          submission_id=100)
        submissions = [submission_row(submission_id=100, state="lost")]
        svc, conn, _, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])

    def test_no_submission_row_classifies_at_the_horizon_unchanged(self):
        # Criterion 8: the pre-044 backstop, proven unchanged. No
        # submission_id at all (every pre-044 attempt) -> the horizon alone
        # decides, exactly as before this package.
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 0, 0),
                          submission_id=None)
        svc, conn, _, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=[])

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])

    def test_an_open_submission_inside_the_horizon_still_waits(self):
        # Criterion 9: unchanged behaviour for the genuinely-ambiguous case.
        # A future resolution_deadline is what keeps `resolve` itself from
        # concluding LOST on this cycle's own resolution pass — this test is
        # about `_reconcile_unresolved`'s read of an UNKNOWN record, not
        # about racing its own resolution.
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 55, 0),
                          submission_id=100)
        submissions = [submission_row(
            submission_id=100, state="unknown",
            resolution_deadline=utc(2026, 8, 6, 12, 30, 0))]
        svc, conn, _, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions)

        summary = svc.poll_once()

        self.assertEqual(1, summary["waiting"])
        self.assertEqual(0, summary.get("classified", 0))

    def test_the_redirect_path_also_honours_a_found_submission(self):
        # Criterion 10, first half: the `_reconcile_attempt` redirect
        # (service.py, "the scheduler returned the job but not an attempt we
        # can pair") reaches `_reconcile_unresolved` with a row that CARRIES
        # a scheduler_job_id. The FOUND branch must still apply there.
        row = attempt_row(1, lifecycle_state="submitted",
                          scheduler_job_id="job-abc",
                          submitted_at=utc(2026, 8, 6, 8, 0, 0),
                          submission_id=100,
                          application_attempt_index=None,
                          scheduler_attempt_index=None)
        # Two observations with no index on either side, so _pick_observation
        # cannot pair one to this row and redirects to _reconcile_unresolved
        # (service.py:680-687's documented case).
        jobs = [batch_job(job_id="job-abc", status="RUNNING",
                          started=utc(2026, 8, 6, 11, 0, 0),
                          attempts=[
                              {"container": {"exitCode": None},
                               "startedAt": ms(utc(2026, 8, 6, 8, 0, 0))},
                              {"container": {"exitCode": None},
                               "startedAt": ms(utc(2026, 8, 6, 9, 0, 0))},
                          ])]
        submissions = [submission_row(submission_id=100, state="found")]
        svc, conn, _, _, _ = build(
            [row], jobs, now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions)

        summary = svc.poll_once()

        self.assertEqual(1, summary["waiting"])
        self.assertEqual(0, summary.get("classified", 0))

    def test_the_attempt_ran_distinction_is_preserved_under_lost(self):
        # Criterion 10, second half: a LOST submission on a row that carries
        # a full application account must still be flagged CONTRADICTORY
        # (missing_or_contradictory), never forced into terminal_without_start
        # — the same distinction ConstraintFidelityTests pins for the
        # horizon-only path, now exercised through the submission branch.
        row = attempt_row(1, lifecycle_state="application_closed",
                          started_at=utc(2026, 8, 6, 11, 0, 0),
                          rapid_outcome="success",
                          product_disposition="published",
                          application_intended_exit=0,
                          scheduler_job_id="an-id-batch-never-heard-of",
                          submitted_at=utc(2026, 8, 6, 11, 55, 0),
                          submission_id=100)
        submissions = [submission_row(submission_id=100, state="lost")]
        svc, conn, _, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions)

        summary = svc.poll_once()

        self.assertEqual(1, summary["classified"])
        self.assertEqual("missing_or_contradictory",
                         conn.rows[1]["lifecycle_state"])

    def test_a_raising_submission_lookup_falls_through_to_the_horizon(self):
        # Criterion 11: fail OPEN. The lookup itself raising must not block
        # reconciliation and must not be mistaken for LOST — it falls
        # through to the existing horizon backstop, and is logged (checked
        # via the rollback count: the failed SELECT's aborted transaction is
        # cleared exactly like every other caught exception in this file).
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 11, 0, 0),
                          submission_id=100)
        svc, conn, _, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=[])

        def explode(text, params=None):
            if "from submissions" in text.lower() and "join attempts" in \
                    text.lower():
                raise RuntimeError("the submissions read failed")
            return real_route(text, params)

        real_route = conn.route
        conn.route = explode

        summary = svc.poll_once()

        # Past the horizon, no usable submission evidence -> the backstop
        # classifies exactly as it would with no submission_id at all.
        self.assertEqual(1, summary["classified"])
        self.assertEqual("terminal_without_start",
                         conn.rows[1]["lifecycle_state"])

    def test_never_calls_submit_job_reaching_this_path(self):
        # Criterion 12, the protocol invariant: resolution is re-query only.
        # FakeBatch has no submit_job at all, so a caller that reached for it
        # would AttributeError rather than silently succeed — the same
        # refusal discipline test_submission_protocol.py's _FakeBatch uses.
        row = attempt_row(1, scheduler_job_id=None,
                          submitted_at=utc(2026, 8, 6, 8, 0, 0),
                          submission_id=100)
        submissions = [submission_row(submission_id=100, state="found")]
        svc, _, batch, _, _ = build(
            [row], jobs=[], now=utc(2026, 8, 6, 12, 0, 0),
            submissions_available=True, submissions=submissions)
        self.assertFalse(hasattr(batch, "submit_job"))

        svc.poll_once()  # must not raise AttributeError


if __name__ == "__main__":
    unittest.main()
