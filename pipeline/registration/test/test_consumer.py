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


def product_writer(conn, rows=None, fail_after_write=False):
    """A `register` callback that writes its product rows on `conn`.

    This is the fixture the whole finding turns on, so it is worth saying what
    it models and what it deliberately does not. The real registrar calls
    `dbh.add_diffimage` and friends, and after the fix the handle those calls go
    through is `RAPIDDB.borrowing(conn)` — the consumer's own connection, with
    its per-call commits suppressed. So from this module's point of view a
    registration is exactly this: some statements executed on the connection it
    was handed, and nothing committed.

    A callback that wrote nowhere, or wrote to a second connection of its own,
    could not tell the fixed code from the broken code — which is what the old
    suite's `lambda row, verdict: None` could not do. Writing HERE is what lets
    `conn.committed` answer the only question that matters: when the watermark
    fails, are the product rows still there?
    """
    rows = rows if rows is not None else []

    def register(row, verdict):
        with conn.cursor() as cur:
            cur.execute("select * from addDiffImage(...)",
                        (row["attempt_id"],
                         row.get("terminal_record_sequence")))
        rows.append((row["attempt_id"], row.get("terminal_record_sequence")))
        if fail_after_write:
            # The crash window. Under the old two-connection design the
            # product rows were already committed by the time control reached
            # here, and nothing downstream could take them back.
            raise RuntimeError("the registrar died after writing its products")

    return register


class OneTransactionPerAttemptTests(unittest.TestCase):
    """ROUND-3 FINDING #8: the product rows and the watermark are one unit.

    The defect these pin: `registrar_for` handed the product bodies
    `rapid_db.RAPIDDB` as a factory, and that class opens its own psycopg2
    connection and commits after every call. The watermark was written on the
    consumer's connection and committed separately. Two connections cannot be
    one transaction by construction, so between the product write and the
    watermark there was a durable window — rows written, attempt still a
    candidate — and every crash in it produced a duplicate registration on the
    next pass.

    The module docstring used to promise that "a failure leaves the attempt a
    candidate rather than marking work that did not happen". Half of that was
    true and the important half was not: the work HAD happened.
    """

    def test_a_failure_after_the_product_write_commits_no_product_rows(self):
        # THE WHOLE FINDING. The registrar writes its rows and then dies —
        # which is the crash window — and nothing may survive it. Under the old
        # code the rows were committed by the registrar's own connection before
        # this callback even returned, so they survived, the watermark did not,
        # and the next pass registered them again.
        conn = FakeConn()
        written = []

        consumer.register_batch(
            conn, [reconciled(1)],
            register=product_writer(conn, written, fail_after_write=True))

        self.assertEqual([(1, 1)], written,
                         "the registrar must actually have written, or this "
                         "test proves nothing about rolling writes back")
        self.assertEqual([], conn.committed,
                         "product rows were committed despite the failure: "
                         "the registration is not all-or-nothing")
        self.assertEqual(0, conn.commits)
        self.assertGreaterEqual(conn.rollbacks, 1)

    def test_the_watermark_and_the_product_rows_commit_together(self):
        # One commit for the pair, and both statements inside it. Two commits,
        # or a commit containing only one of them, is the split boundary.
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))

        self.assertEqual(1, conn.commits,
                         "one attempt is one transaction, so one commit")
        committed = " ".join(statement for statement, _ in conn.committed)
        self.assertIn("addDiffImage", committed,
                      "the product write is not in the committed transaction")
        self.assertIn("registered_record_sequence", committed,
                      "the watermark is not in the committed transaction")
        self.assertEqual(0, conn.rollbacks)

    def test_the_watermark_is_not_committed_before_the_registration_returns(self):
        # The ORDERING half of the boundary, which a count of commits at the
        # end of the pass cannot see. `mark_registered` used to commit the
        # moment it was called; here the registrar checks, from inside its own
        # callback, that nothing has been committed yet. Under the old code the
        # registrar's own connection had already committed its product rows by
        # this point too — the observation this makes is that on ONE connection
        # there is nothing durable until the whole unit of work ends.
        conn = FakeConn()
        seen = []

        def register(row, verdict):
            with conn.cursor() as cur:
                cur.execute("select * from addDiffImage(...)", (1,))
            seen.append((conn.commits, list(conn.committed)))

        consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual([(0, [])], seen,
                         "something was already durable while the "
                         "registration was still in progress")
        self.assertEqual(1, conn.commits)

    def test_a_failed_attempt_does_not_roll_back_the_ones_before_it(self):
        # The `except` is per-attempt on purpose: an attempt whose record is
        # incomplete must not discard registrations that already committed.
        conn = FakeConn()

        def flaky(row, verdict):
            with conn.cursor() as cur:
                cur.execute("select * from addDiffImage(...)",
                            (row["attempt_id"],))
            if row["attempt_id"] == 2:
                raise RuntimeError("this one's record is incomplete")

        run = consumer.register_batch(
            conn, [reconciled(1), reconciled(2), reconciled(3)],
            register=flaky)

        self.assertEqual(2, run.registered)
        self.assertEqual(1, run.failed)
        committed = [params for _, params in conn.committed]
        self.assertNotIn((2,), committed,
                         "the failed attempt's product write survived")
        self.assertEqual(2, conn.commits)

    def test_the_watermark_write_no_longer_commits_on_its_own(self):
        # `mark_registered` used to end with `conn.commit()`, which is what
        # made the watermark a transaction of its own no matter what the caller
        # wrapped it in. A caller owning the boundary cannot own it if the
        # callee keeps ending it.
        conn = FakeConn()
        consumer.mark_registered(conn, 1, 1)

        self.assertEqual(0, conn.commits,
                         "mark_registered committed by itself; the caller no "
                         "longer owns the transaction boundary")
        self.assertEqual(1, len(conn.statements))

    def test_the_watermark_reuses_the_transaction_cursor_when_given_one(self):
        # Passed the cursor its `transaction(conn)` block yielded, the write
        # goes there rather than onto a second cursor — same transaction
        # either way, but it keeps the unit of work visibly on one cursor.
        conn = FakeConn()
        cur = conn.cursor()
        consumer.mark_registered(conn, 7, 3, cursor=cur)

        statement, params = conn.statements[-1]
        self.assertIn("registered_record_sequence", statement)
        self.assertEqual((7, 3), (params[2], params[1]))


class WatermarkSequenceTests(unittest.TestCase):
    """The watermark records the sequence it registered at, not a boolean.

    The product-row half of replay and supersession — that the same
    (attempt_id, sequence) pair reaches the stored function on a replay, and a
    higher one on a supersession — is asserted in `test_products.py`, against
    the real registrar and the real `add_diffimage` argument list. What belongs
    here is the watermark that has to agree with it.
    """

    def test_the_watermark_carries_the_record_sequence_it_registered_at(self):
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))
        consumer.register_batch(conn,
                                [reconciled(1, terminal_record_sequence=2)],
                                register=product_writer(conn))

        watermarks = [params for statement, params in conn.committed
                      if "registered_record_sequence" in statement]
        self.assertEqual([1, 2], [params[1] for params in watermarks],
                         "a supersession must advance the watermark to its "
                         "own sequence, or it stays a candidate forever")
        # The CAS bound is the same sequence, so the guard is `< that`.
        self.assertEqual([1, 2], [params[3] for params in watermarks])


class FakeConn:
    """The connection the watermark write needs (review finding #5).

    `register_batch` marks each successful registration with the record
    sequence it registered at, so a later pass does not re-register the same
    attempt — and re-registers it only when a superseding record raises the
    sequence.

    AMENDED for round-3 finding #8. The registration and its watermark are now
    one transaction, so this fake has to model a transaction boundary rather
    than just count `commit()` calls: `statements` is everything the connection
    was ever handed, and `committed` is only what a commit made durable. The
    difference between those two lists is the whole property under test — under
    the old code the product rows were durable the moment the registrar wrote
    them, and a failure before the watermark could not take them back.
    """

    def __init__(self):
        self.statements = []
        self.committed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0
        self._pending = []

    def cursor(self):
        return self

    def close(self):
        self.closed_cursors += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        self._pending.append((statement, params))

    def commit(self):
        self.commits += 1
        self.committed.extend(self._pending)
        self._pending = []

    def rollback(self):
        self.rollbacks += 1
        self._pending = []


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
