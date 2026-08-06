"""The scheduler reader: batching, and the flagged attempt-index derivation."""

import unittest

from pipeline.reconciler import scheduler
from pipeline.reconciler.test.stubs import FakeBatch, batch_job, ms, utc


class DescribeBatchingTests(unittest.TestCase):
    def test_chunks_at_one_hundred(self):
        ids = [f"job-{n}" for n in range(250)]
        client = FakeBatch(jobs=[batch_job(job_id=i) for i in ids])

        batches = list(scheduler.describe_in_batches(client, ids))

        self.assertEqual([100, 100, 50], [len(c) for c in client.calls])
        self.assertEqual(250, sum(len(b.jobs) for b in batches))

    def test_reports_ids_the_scheduler_did_not_return(self):
        # Batch drops unknown ids silently rather than erroring. A reconciler
        # that assumed every id came back would never notice a lost job.
        client = FakeBatch(jobs=[batch_job(job_id="job-1")])

        batches = list(scheduler.describe_in_batches(
            client, ["job-1", "job-gone"]))

        self.assertEqual(("job-gone",), batches[0].missing)

    def test_skips_empty_identifiers(self):
        client = FakeBatch(jobs=[])
        list(scheduler.describe_in_batches(client, [None, "", "job-1"]))
        self.assertEqual([["job-1"]], client.calls)


class AttemptIndexDerivationTests(unittest.TestCase):
    """THE FLAGGED API DEPENDENCY: Batch exposes no attempt ordinal."""

    def test_numbers_attempts_one_based_in_the_order_batch_lists_them(self):
        # AMENDED by FixA (review finding #4). The derivation is LIST
        # POSITION, not start-time order: Batch appends each attempt to the
        # history as it is made, so the list is already in scheduler order —
        # which is the order the index has to agree with.
        #
        # The two derivations coincide on any history where every attempt
        # started, which is why sorting looked correct. They differ exactly in
        # the never-started case, which is the case retries exist for; see
        # test_a_never_started_attempt_keeps_its_own_position.
        attempts = [
            {"startedAt": ms(utc(2026, 8, 6, 10, 0, 0))},
            {"startedAt": ms(utc(2026, 8, 6, 10, 30, 0))},
        ]

        derived = scheduler.derive_attempt_indices(attempts)

        self.assertEqual([1, 2], [index for index, _ in derived])
        self.assertEqual(ms(utc(2026, 8, 6, 10, 0, 0)),
                         derived[0][1]["startedAt"])

    def test_a_never_started_attempt_keeps_its_own_position(self):
        # REVIEW FINDING #4, and the exact scenario the reviewer named. A pull
        # failure never starts, so it has no startedAt — and the derivation
        # used to sort those AFTER the started ones, which numbered the
        # SUCCESSFUL SECOND attempt 1 and the failed first attempt 2. Every
        # existing row then paired with the wrong observation.
        #
        # The old docstring argued a never-started attempt "still consumed an
        # ordinal", which is true and is exactly why it must keep its
        # POSITION. Batch appends attempts as they are made, so list order IS
        # scheduler order.
        attempts = [
            {"startedAt": None, "statusReason": "CannotPullContainerError"},
            {"startedAt": ms(utc(2026, 8, 6, 10, 0, 0))},
        ]

        derived = scheduler.derive_attempt_indices(attempts)

        self.assertEqual(2, len(derived))
        self.assertEqual(1, derived[0][0])
        self.assertIsNone(derived[0][1]["startedAt"],
                          "the never-started FIRST attempt is attempt 1")
        self.assertEqual(2, derived[1][0])
        self.assertIsNotNone(derived[1][1]["startedAt"])

    def test_ties_keep_the_schedulers_own_order(self):
        same = ms(utc(2026, 8, 6, 10, 0, 0))
        attempts = [{"startedAt": same, "tag": "first"},
                    {"startedAt": same, "tag": "second"}]

        derived = scheduler.derive_attempt_indices(attempts)

        self.assertEqual(["first", "second"],
                         [attempt["tag"] for _, attempt in derived])

    def test_empty_history(self):
        self.assertEqual([], scheduler.derive_attempt_indices([]))


class ObservationTests(unittest.TestCase):
    def test_terminal_and_exit_code_from_the_job(self):
        job = batch_job(status="SUCCEEDED", exit_code=0,
                        started=utc(2026, 8, 6, 10, 0, 0),
                        stopped=utc(2026, 8, 6, 10, 5, 0))

        observation = scheduler.observation_from_job(job)

        self.assertTrue(observation.is_terminal)
        self.assertEqual(0, observation.exit_code)
        self.assertEqual(utc(2026, 8, 6, 10, 5, 0), observation.stopped_at)
        self.assertFalse(observation.never_ran)

    def test_never_ran_is_decided_by_the_absent_start_not_the_exit_code(self):
        job = batch_job(status="FAILED", exit_code=None, started=None,
                        stopped=utc(2026, 8, 6, 10, 1, 0),
                        status_reason="CannotPullContainerError: manifest unknown")

        observation = scheduler.observation_from_job(job)

        self.assertTrue(observation.never_ran)
        self.assertEqual("scheduler_provisioning",
                         observation.reconciler_category())

    def test_host_termination_is_a_reclaim(self):
        job = batch_job(status="FAILED", exit_code=None, started=None,
                        status_reason="Host EC2 instance terminated")

        self.assertEqual("scheduler_reclaimed",
                         scheduler.observation_from_job(job).reconciler_category())

    def test_an_attempt_that_ran_gets_no_reconciler_category(self):
        # The application had its chance to classify itself; the reconciler
        # never invents a category over the top of that.
        job = batch_job(status="FAILED", exit_code=1,
                        started=utc(2026, 8, 6, 10, 0, 0),
                        stopped=utc(2026, 8, 6, 10, 1, 0))

        self.assertIsNone(
            scheduler.observation_from_job(job).reconciler_category())

    def test_unrecognised_failure_reason_defaults_to_provisioning(self):
        job = batch_job(status="FAILED", exit_code=None, started=None,
                        status_reason="something nobody has seen before")

        self.assertEqual("scheduler_provisioning",
                         scheduler.observation_from_job(job).reconciler_category())

    def test_one_observation_per_attempt_when_there_is_a_retry_history(self):
        job = batch_job(job_id="job-retry", status="SUCCEEDED", attempts=[
            {"startedAt": None, "statusReason": "Host EC2 instance terminated",
             "container": {"exitCode": None}},
            {"startedAt": ms(utc(2026, 8, 6, 10, 10, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 12, 0)),
             "container": {"exitCode": 0}},
        ])

        observations = scheduler.observations_for_job(job)

        self.assertEqual(2, len(observations))
        self.assertEqual([1, 2], [o.attempt_index for o in observations])
        # AMENDED by FixA (#4): attempt 1 is the one Batch listed first — the
        # reclaimed attempt that never ran. It used to be the SECOND one,
        # because never-started attempts were sorted to the end.
        self.assertTrue(observations[0].never_ran)
        self.assertEqual(0, observations[1].exit_code)

    def test_each_attempt_carries_its_own_state_not_the_jobs(self):
        # REVIEW FINDING #4's second half. `status` is the JOB's status, and
        # it was handed to every attempt observation — so a job that failed
        # once and then succeeded reported SUCCEEDED for BOTH attempts. The
        # failed attempt then looked like a success, and a started-then-
        # reclaimed attempt was classified `internal_error` rather than
        # `scheduler_reclaimed`, because the reconciler-authored categories
        # are only returned for observations that look like they never ran.
        job = batch_job(job_id="job-retry", status="SUCCEEDED", attempts=[
            {"startedAt": ms(utc(2026, 8, 6, 10, 0, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 5, 0)),
             "statusReason": "Host EC2 instance terminated",
             "container": {"exitCode": 137,
                           "reason": "Host EC2 instance terminated"}},
            {"startedAt": ms(utc(2026, 8, 6, 10, 10, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 12, 0)),
             "container": {"exitCode": 0}},
        ])

        first, final = scheduler.observations_for_job(job)

        self.assertEqual("FAILED", first.state,
                         "a superseded attempt did not succeed just because "
                         "the job eventually did")
        self.assertEqual("SUCCEEDED", final.state,
                         "the job's status describes its LAST attempt")

    def test_a_started_then_reclaimed_attempt_is_categorised_as_reclaimed(self):
        # The consequence #4 names: with the job's SUCCEEDED state on it, a
        # reclaimed attempt did not look like one that never ran, so
        # `reconciler_category` returned None and the service fell back to
        # `internal_error` — blaming the application for a host that died.
        job = batch_job(job_id="job-retry", status="SUCCEEDED", attempts=[
            {"startedAt": None,
             "statusReason": "Host EC2 instance terminated",
             "container": {"reason": "Host EC2 instance terminated"}},
            {"startedAt": ms(utc(2026, 8, 6, 10, 10, 0)),
             "stoppedAt": ms(utc(2026, 8, 6, 10, 12, 0)),
             "container": {"exitCode": 0}},
        ])

        first, _final = scheduler.observations_for_job(job)

        self.assertEqual("scheduler_reclaimed", first.reconciler_category())

    def test_a_job_with_no_history_yields_one_observation(self):
        observations = scheduler.observations_for_job(batch_job())
        self.assertEqual(1, len(observations))
        self.assertIsNone(observations[0].attempt_index)


if __name__ == "__main__":
    unittest.main()
