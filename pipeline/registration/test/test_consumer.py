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

    def test_the_query_excludes_attempts_already_registered_at_this_sequence(self):
        # REVIEW FINDING #5's other half. Without a watermark the query
        # selected every reconciled attempt on every pass, so registration
        # re-registered the same work forever.
        self.assertIn("registered_record_sequence", consumer._CANDIDATE_SQL)

    def test_the_watermark_is_a_sequence_so_supersession_re_registers(self):
        # A boolean could not express "reprocesses on a later supersession":
        # an attempt registered at sequence 1 whose reconciler later publishes
        # sequence 2 must become a candidate again, which is what comparing
        # the watermark against the record sequence does.
        self.assertIn(
            "registered_record_sequence < terminal_record_sequence",
            " ".join(consumer._CANDIDATE_SQL.split()))


class WatermarkTests(unittest.TestCase):
    """Registration marks what it registered, and never moves backwards."""

    def test_a_successful_registration_writes_the_watermark(self):
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=lambda row, verdict: None)

        self.assertEqual(1, len(conn.statements))
        statement, params = conn.statements[0]
        self.assertIn("registered_record_sequence", statement)
        self.assertIn(1, params)
        self.assertEqual(1, conn.commits)

    def test_a_failed_registration_writes_no_watermark(self):
        # The attempt must stay a candidate: marking work that did not happen
        # is exactly what the watermark exists to prevent.
        conn = FakeConn()

        def explode(row, verdict):
            raise RuntimeError("no")

        consumer.register_batch(conn, [reconciled(1)], register=explode)

        self.assertEqual([], conn.statements)

    def test_the_watermark_never_moves_backwards(self):
        # Guarded in SQL, so a replay or a concurrent pass cannot lower it.
        self.assertIn("registered_record_sequence < %s",
                      " ".join(consumer._MARK_REGISTERED_SQL.split()))


class FakeConn:
    """The connection the watermark write needs (review finding #5).

    `register_batch` marks each successful registration with the record
    sequence it registered at, so a later pass does not re-register the same
    attempt — and re-registers it only when a superseding record raises the
    sequence. That write needs a cursor; these tests do not care what it
    contains, only that it happens.
    """

    def __init__(self):
        self.statements = []
        self.commits = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))

    def commit(self):
        self.commits += 1


class DecisionTests(unittest.TestCase):
    def test_a_successful_published_attempt_is_a_registration_candidate(self):
        # AMENDED by FixA (review finding #5). A dry run counts into
        # `would_register`, NOT `registered` — the two were one counter, which
        # is how the production path (which passed no callback at all)
        # reported registered=N while writing nothing.
        run = consumer.register_batch(None, [reconciled(1)], dry_run=True)

        self.assertEqual(1, run.would_register)
        self.assertEqual(0, run.registered,
                         "a dry run registers nothing, and must not say it did")
        self.assertEqual(0, run.skipped)

    def test_a_missing_callback_is_refused_unless_a_dry_run_is_asked_for(self):
        # The core of #5: omitting the callback used to silently become a dry
        # run that reported success. The production dispatch path omitted it.
        with self.assertRaises(ValueError) as caught:
            consumer.register_batch(None, [reconciled(1)])
        self.assertIn("dry_run=True", str(caught.exception))

    def test_an_application_failure_is_refused_by_taxonomy(self):
        # The case the log-grep chain got wrong by construction: Batch says
        # SUCCEEDED and exit 0, the application says it failed.
        row = reconciled(1, rapid_outcome="failure",
                         product_disposition="none",
                         scheduler_state="SUCCEEDED",
                         scheduler_observed_exit=0,
                         error_category="tool_failure")

        run = consumer.register_batch(None, [row], dry_run=True)

        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)
        self.assertEqual(1, run.refused_application_failed)

    def test_a_never_started_attempt_registers_nothing(self):
        row = reconciled(1, lifecycle_state="terminal_without_start",
                         started_at=None, rapid_outcome=None,
                         product_disposition=None,
                         application_intended_exit=None,
                         error_category="scheduler_provisioning")

        run = consumer.register_batch(None, [row], dry_run=True)

        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)

    def test_superseded_products_are_not_registered(self):
        row = reconciled(1, product_disposition="superseded")
        run = consumer.register_batch(None, [row], dry_run=True)
        self.assertEqual(0, run.registered)

    def test_partial_success_is_not_registered_silently(self):
        row = reconciled(1, rapid_outcome="partial")
        run = consumer.register_batch(None, [row], dry_run=True)
        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)


class ExitCodeTests(unittest.TestCase):
    def test_a_clean_pass_exits_zero(self):
        run = consumer.register_batch(None, [reconciled(1)], dry_run=True)
        self.assertEqual(consumer.EXIT_OK, run.exit_code)

    def test_a_failing_registration_is_counted_and_exits_nonzero(self):
        # The four scripts this replaces hardcoded exit 0, so a run where
        # every registration raised looked identical to a clean one.
        def explode(row, verdict):
            raise RuntimeError("the product store said no")

        run = consumer.register_batch(
            FakeConn(), [reconciled(1), reconciled(2)], register=explode)

        self.assertEqual(2, run.failed)
        self.assertEqual(0, run.registered)
        self.assertEqual(consumer.EXIT_FAILURES, run.exit_code)

    def test_one_failure_among_several_still_registers_the_rest(self):
        def flaky(row, verdict):
            if row["attempt_id"] == 1:
                raise RuntimeError("transient")

        run = consumer.register_batch(
            FakeConn(), [reconciled(1), reconciled(2)], register=flaky)

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
