"""The registration consumer: reconciled-only, refusal by taxonomy, real exit."""

import unittest

from pipeline.registration import consumer
from pipeline.reconciler.test.stubs import FakeConnection, attempt_row, utc


def reconciled(attempt_id=1, **overrides):
    """A row as the reconciler leaves it: terminal, with a closure record."""
    row = attempt_row(attempt_id,
                      lifecycle_state="terminal_after_start",
                      started_at=utc(2026, 8, 6, 11, 0, 0),
                      rapid_outcome="success",
                      product_disposition="published",
                      application_intended_exit=0,
                      scheduler_observed_exit=0,
                      scheduler_state="SUCCEEDED",
                      terminal_record_sequence=1,
                      terminal_record_key="attempts/records/x/seq-0001.json")
    row.update(overrides)
    return row


class CandidateQueryTests(unittest.TestCase):
    def test_only_reconciled_states_are_candidates(self):
        conn = FakeConnection(rows=[
            reconciled(1),
            reconciled(2, lifecycle_state="terminal_without_start",
                       rapid_outcome=None, product_disposition=None,
                       started_at=None, application_intended_exit=None),
            reconciled(3, lifecycle_state="application_closed"),
            reconciled(4, lifecycle_state="started"),
            reconciled(5, lifecycle_state="missing_or_contradictory"),
        ])

        rows = consumer.candidates(conn)

        self.assertEqual([1, 2], [r["attempt_id"] for r in rows])

    def test_the_query_requires_a_reconciler_written_record(self):
        # Sequence 0 is the application's own record and proves nothing about
        # scheduler truth. The SQL must gate on >= 1.
        self.assertIn("terminal_record_sequence >= 1", consumer._CANDIDATE_SQL)

    def test_the_candidate_read_holds_no_transaction(self):
        conn = FakeConnection(rows=[reconciled(1)])
        consumer.candidates(conn)
        self.assertGreaterEqual(conn.rollbacks, 1)


class DecisionTests(unittest.TestCase):
    def test_a_successful_published_attempt_registers(self):
        run = consumer.register_batch(None, [reconciled(1)])

        self.assertEqual(1, run.registered)
        self.assertEqual(0, run.skipped)

    def test_an_application_failure_is_refused_by_taxonomy(self):
        # The case the log-grep chain got wrong by construction: Batch says
        # SUCCEEDED and exit 0, the application says it failed.
        row = reconciled(1, rapid_outcome="failure",
                         product_disposition="none",
                         scheduler_state="SUCCEEDED",
                         scheduler_observed_exit=0,
                         error_category="tool_failure")

        run = consumer.register_batch(None, [row])

        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)
        self.assertEqual(1, run.refused_application_failed)

    def test_a_never_started_attempt_registers_nothing(self):
        row = reconciled(1, lifecycle_state="terminal_without_start",
                         started_at=None, rapid_outcome=None,
                         product_disposition=None,
                         application_intended_exit=None,
                         error_category="scheduler_provisioning")

        run = consumer.register_batch(None, [row])

        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)

    def test_superseded_products_are_not_registered(self):
        row = reconciled(1, product_disposition="superseded")
        run = consumer.register_batch(None, [row])
        self.assertEqual(0, run.registered)

    def test_partial_success_is_not_registered_silently(self):
        row = reconciled(1, rapid_outcome="partial")
        run = consumer.register_batch(None, [row])
        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)


class ExitCodeTests(unittest.TestCase):
    def test_a_clean_pass_exits_zero(self):
        run = consumer.register_batch(None, [reconciled(1)])
        self.assertEqual(consumer.EXIT_OK, run.exit_code)

    def test_a_failing_registration_is_counted_and_exits_nonzero(self):
        # The four scripts this replaces hardcoded exit 0, so a run where
        # every registration raised looked identical to a clean one.
        def explode(row, verdict):
            raise RuntimeError("the product store said no")

        run = consumer.register_batch(
            None, [reconciled(1), reconciled(2)], register=explode)

        self.assertEqual(2, run.failed)
        self.assertEqual(0, run.registered)
        self.assertEqual(consumer.EXIT_FAILURES, run.exit_code)

    def test_one_failure_among_several_still_registers_the_rest(self):
        def flaky(row, verdict):
            if row["attempt_id"] == 1:
                raise RuntimeError("transient")

        run = consumer.register_batch(
            None, [reconciled(1), reconciled(2)], register=flaky)

        self.assertEqual(1, run.failed)
        self.assertEqual(1, run.registered)
        self.assertEqual(consumer.EXIT_FAILURES, run.exit_code)


class NoLegacyMechanismTests(unittest.TestCase):
    def test_the_consumer_never_reads_a_log_or_a_sentinel(self):
        # A structural assertion, not a behavioural one: the module must not
        # mention the mechanisms the fence deleted.
        import inspect

        source = inspect.getsource(consumer)
        for banned in ("terminating_exitcode", ".done", "get_log_events",
                       "download_file_from_s3_bucket", "write_done_file"):
            self.assertNotIn(banned, source,
                             f"the consumer references {banned!r}")


if __name__ == "__main__":
    unittest.main()
