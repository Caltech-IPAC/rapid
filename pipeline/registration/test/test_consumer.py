"""The registration consumer: reconciled-only, refusal by taxonomy, real exit."""

import json
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

    def test_the_query_excludes_attempts_already_consumed_at_this_sequence(self):
        # REVIEW FINDING #5's other half, on the CONSUMED watermark since
        # ruling R1 split it from the registered one (module docstring's
        # "THE WATERMARK SPLIT"). Without it the query selected every
        # reconciled attempt on every pass, so registration re-decided the
        # same work forever — including permanently-SKIPped attempts, which
        # is the eternal-candidates defect the split specifically closes.
        self.assertIn("consumed_record_sequence", consumer._CANDIDATE_SQL)

    def test_the_watermark_is_a_sequence_so_supersession_re_registers(self):
        # A boolean could not express "reprocesses on a later supersession":
        # an attempt consumed at sequence 1 whose reconciler later publishes
        # sequence 2 must become a candidate again, which is what comparing
        # the watermark against the record sequence does.
        self.assertIn(
            "consumed_record_sequence < terminal_record_sequence",
            " ".join(consumer._CANDIDATE_SQL.split()))

    def test_the_query_left_joins_work_units_for_the_campaign_run(self):
        # THROUGHPUT-SITTING RULING, 2026-09-11: the candidate query must
        # join `work_units` under an unambiguous alias — `attempts.run_id`
        # (the submission batch) and `work_units.run_id` (the campaign
        # scope) are different facts, and `products.register` now reads
        # the latter under this exact alias. A LEFT JOIN, not an INNER
        # JOIN: `attempts.work_unit_id` can be NULL and such a row must
        # still be a candidate (see the next test).
        sql = " ".join(consumer._CANDIDATE_SQL.split())
        self.assertIn("LEFT JOIN work_units", sql)
        self.assertIn("work_units.run_id AS work_unit_run_id", sql)

    def test_a_null_work_unit_id_row_is_still_a_candidate(self):
        # A unit-less attempt (pre-intent-layer, or the definition-FK guard
        # held it back at submission — see `_COLUMNS`'s own comment on
        # `work_unit_id`) must not vanish from the query behind an INNER
        # JOIN: it registers with `work_unit_run_id=None`, the production
        # lane, not be silently dropped.
        conn = FakeConnection(rows=[reconciled(1, work_unit_id=None)])

        rows = consumer.candidates(conn)

        self.assertEqual([1], [r["attempt_id"] for r in rows])
        self.assertIsNone(rows[0]["work_unit_run_id"])

    def test_the_campaign_run_id_reaches_the_row_under_its_own_name(self):
        # The positive case alongside the NULL case above: a row whose work
        # unit carries a campaign run surfaces it as `work_unit_run_id`,
        # distinct from and alongside the row's own `run_id` (the
        # submission-batch identity).
        conn = FakeConnection(rows=[
            reconciled(1, run_id="accept-20260911-13", work_unit_id=42,
                      work_unit_run_id="accept-20260911"),
        ])

        rows = consumer.candidates(conn)

        self.assertEqual("accept-20260911-13", rows[0]["run_id"])
        self.assertEqual("accept-20260911", rows[0]["work_unit_run_id"])


class CandidateScopingTests(unittest.TestCase):
    """`candidates()`'s optional `run_id_prefix`/`attempt_ids` scoping
    (`pipeline.registration.scoped`, the standalone bounded-registration
    entrypoint)."""

    def test_unscoped_call_issues_byte_for_byte_the_same_sql_and_params(self):
        # THE REGRESSION GUARD ON THE PRODUCTION PATH. `candidates()` is
        # called unscoped by `pipeline.entrypoints.job.dispatch_registration`
        # and `pipeline.operator.registration.run_pass` — neither passes
        # `run_id_prefix` or `attempt_ids` — so an unscoped call must issue
        # the EXACT SQL text and params it always has, not merely "the same
        # rows". A scoping bug that appended an always-true predicate (or
        # reordered params) could still pass a rows-only assertion.
        conn = FakeConnection(rows=[reconciled(1), reconciled(2)])

        consumer.candidates(conn)

        self.assertEqual(1, len(conn.statements))
        text, params = conn.statements[0]
        self.assertEqual(consumer._CANDIDATE_SQL, text)
        self.assertEqual((list(consumer.RECONCILED_STATES),), params)

    def test_run_id_prefix_narrows_the_result_set(self):
        # THE FIXTURE HAS MULTIPLE RUN_IDS, on purpose: a fixture containing
        # only the target run_id cannot detect a scoping bug that returns
        # everything regardless of the predicate — this one contains a
        # decoy row under a different run_id that a broken scope would leak.
        # Both matching rows carry a work unit: a run-scoped pass requires
        # one by construction (the incident guard above), and this test's
        # subject is prefix narrowing, not that requirement.
        conn = FakeConnection(rows=[
            reconciled(1, run_id="w9-ramp-science-18-abc", work_unit_id=1,
                       work_unit_run_id="w9-ramp-science-18-abc"),
            reconciled(2, run_id="w9-ramp-science-18-abc", work_unit_id=2,
                       work_unit_run_id="w9-ramp-science-18-abc"),
            reconciled(3, run_id="some-other-run-entirely", work_unit_id=3,
                       work_unit_run_id="some-other-run-entirely"),
        ])

        rows = consumer.candidates(conn, run_id_prefix="w9-ramp-science-18-abc")

        self.assertEqual([1, 2], sorted(r["attempt_id"] for r in rows))

    def test_run_id_prefix_matches_the_split_batch_suffix_convention(self):
        # `pipeline.seams.submit_gathered` splits a batch too large for one
        # manifest into `-0`/`-1`/... suffixed child run_ids
        # (`f"{run_id}-{index}"`). A caller scoping to the run they submitted
        # must match every suffixed child, not just an exact, unsuffixed
        # run_id that may never appear alone.
        # Both matching rows carry a work unit: a run-scoped pass requires
        # one by construction (the incident guard above), and this test's
        # subject is the split-batch suffix match, not that requirement.
        conn = FakeConnection(rows=[
            reconciled(1, run_id="w9-ramp-science-270-xyz-0", work_unit_id=1,
                       work_unit_run_id="w9-ramp-science-270-xyz"),
            reconciled(2, run_id="w9-ramp-science-270-xyz-1", work_unit_id=2,
                       work_unit_run_id="w9-ramp-science-270-xyz"),
            reconciled(3, run_id="w9-ramp-science-18-different", work_unit_id=3,
                       work_unit_run_id="w9-ramp-science-18-different"),
        ])

        rows = consumer.candidates(conn, run_id_prefix="w9-ramp-science-270-xyz")

        self.assertEqual([1, 2], sorted(r["attempt_id"] for r in rows))

    def test_attempt_ids_narrows_the_result_set(self):
        conn = FakeConnection(rows=[
            reconciled(1, run_id="run-a"),
            reconciled(2, run_id="run-a"),
            reconciled(3, run_id="run-a"),
        ])

        rows = consumer.candidates(conn, attempt_ids=[1, 3])

        self.assertEqual([1, 3], sorted(r["attempt_id"] for r in rows))

    def test_a_scoped_call_still_respects_the_reconciled_state_gate(self):
        # Scoping narrows an already-reconciled candidate set; it must not
        # widen it. A non-reconciled row inside the named run_id stays
        # excluded.
        # Row 1 carries a work unit: a run-scoped pass requires one by
        # construction (the incident guard above), and this test's subject
        # is the reconciled-state gate, not that requirement, so the
        # expected survivor must clear both.
        conn = FakeConnection(rows=[
            reconciled(1, run_id="run-a", work_unit_id=42,
                       work_unit_run_id="run-a"),
            reconciled(2, run_id="run-a", lifecycle_state="started",
                       rapid_outcome=None, product_disposition=None,
                       started_at=None, application_intended_exit=None),
        ])

        rows = consumer.candidates(conn, run_id_prefix="run-a")

        self.assertEqual([1], [r["attempt_id"] for r in rows])

    def test_a_run_scoped_query_requires_a_work_unit(self):
        # THE 2026-09-11 INCIDENT GUARD. A run-scoped registration is by
        # definition registering a campaign run's products, and the
        # registrar (`pipeline.registration.products.registrar`) reads the
        # campaign run from `work_unit_run_id` — the LEFT JOINed `work_units.
        # run_id` — never from `attempts.run_id` (see that module's own
        # comment on why). An attempt with no work unit has no campaign
        # scope: its `work_unit_run_id` is NULL, so registering it under a
        # run scope would write it to the production (`run_id IS NULL`) lane
        # — precisely NOT what the operator scoped the registration to. Such
        # an attempt is out of scope for a run-scoped pass by construction,
        # so the run-prefix branch must also require
        # `attempts.work_unit_id IS NOT NULL` in the SQL text it issues.
        conn = FakeConnection(rows=[reconciled(1, run_id="run-a")])

        consumer.candidates(conn, run_id_prefix="run-a")

        text, _params = conn.statements[0]
        self.assertIn(consumer._WORK_UNIT_ID_NOT_NULL_SQL, text)

    def test_a_run_scoped_query_does_not_return_a_unit_less_attempt(self):
        # The behavioural half of the incident guard above: a run-prefix-
        # scoped call must not hand back an attempt whose `work_unit_id` is
        # NULL, even though it matches the run prefix and the reconciled-
        # state gate — that attempt has no campaign scope to register under,
        # and returning it is exactly how the 2026-09-11 incident wrote
        # 2,427 rows onto the production lane.
        conn = FakeConnection(rows=[
            reconciled(1, run_id="accept-20260911-13", work_unit_id=42,
                      work_unit_run_id="accept-20260911"),
            reconciled(2, run_id="accept-20260911-14", work_unit_id=None),
        ])

        rows = consumer.candidates(conn, run_id_prefix="accept-20260911")

        self.assertEqual([1], [r["attempt_id"] for r in rows])

    def test_an_attempt_ids_only_scope_does_not_require_a_work_unit(self):
        # ORTHOGONAL SCOPE, DELIBERATELY UNFILTERED. `attempt_ids` is the
        # operator naming exact rows by id — a caller who already knows
        # precisely which attempts it means, unlike `run_id_prefix`'s
        # campaign framing. That scope must not gain the work-unit
        # requirement: only the run-prefix branch does.
        conn = FakeConnection(rows=[
            reconciled(1, run_id="run-a", work_unit_id=None),
        ])

        rows = consumer.candidates(conn, attempt_ids=[1])

        text, _params = conn.statements[0]
        self.assertNotIn(consumer._WORK_UNIT_ID_NOT_NULL_SQL, text)
        self.assertEqual([1], [r["attempt_id"] for r in rows])


class WatermarkTests(unittest.TestCase):
    """Registration marks what it registered, and never moves backwards."""

    def test_a_successful_registration_writes_the_watermark(self):
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=lambda row, verdict: None)

        # THE WATERMARK WRITE IS FOUND BY CONTENT, NOT BY COUNT. This used to
        # assert `len(conn.statements) == 3` and then read the LAST one —
        # encoding how many statements one registration issues, which is not
        # what the test is named for or about. Brief C's rule-8 repair added
        # one (the `l2_available` milestone, which now co-commits here rather
        # than being written by the science stage), so the count assertion
        # failed while the watermark write it exists to check was untouched.
        # What matters is that the watermark statement is issued with the
        # right sequence, inside the one transaction — all three of which are
        # asserted directly below.
        watermark = [
            (sql, params) for sql, params in conn.statements
            if "registered_record_sequence" in sql and "UPDATE attempts" in sql
        ]
        self.assertEqual(1, len(watermark),
                         f"expected exactly one watermark write, got "
                         f"{[s for s, _ in conn.statements]}")
        self.assertIn(1, watermark[0][1])
        # BOTH watermarks advance in that one statement (ruling R1's "one
        # statement" design for REGISTER — see `_MARK_REGISTERED_SQL`'s own
        # docstring) — asserted by content rather than a second statement
        # count, matching this test's own stated preference just above.
        self.assertIn("consumed_record_sequence", watermark[0][0])
        self.assertEqual(1, conn.commits)
        self.assertEqual([(consumer.ATTEMPT_LEASE_NAMESPACE, 1)],
                         conn.lease_acquisitions)

    def test_a_failed_registration_writes_no_watermark(self):
        # The attempt must stay a candidate: marking work that did not happen
        # is exactly what the watermark exists to prevent.
        conn = FakeConn()

        def explode(row, verdict):
            raise RuntimeError("no")

        consumer.register_batch(conn, [reconciled(1)], register=explode)

        # The lease and the post-lock re-read still happened — they must, to
        # even reach the point of calling `explode` — but nothing committed,
        # which is the property this test is actually about.
        self.assertEqual([], conn.committed)
        self.assertEqual(0, conn.commits)
        self.assertGreaterEqual(conn.rollbacks, 1)

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
        # What the real bodies return, and what the outcome writer reads.
        # A double that returned None could not tell a writer that records
        # the promotion from one that records nothing.
        return {"pid": 900 + row["attempt_id"], "version": 1,
                "product": "sfft_diffimage", "role_resolved_from": "record"}

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
        # Params are (moment, sequence, sequence, attempt_id, sequence) —
        # ruling R1 added the consumed-watermark sequence as a second SET
        # value, ahead of the WHERE clause's attempt_id (see
        # `_MARK_REGISTERED_SQL`).
        self.assertEqual((7, 3), (params[3], params[1]))


class RegistrationCompletesTheWorkUnitTests(unittest.TestCase):
    """FINDING 7: the work unit completes here, not on a bare success read.

    The reconciler used to transition a work unit `submitted -> complete`
    the moment `rapid_outcome == success` — before registration had run at
    all. A registration that later failed or was rejected then had a
    `complete` unit with an unaccepted result and nothing able to rerun it.
    The unit now stays `submitted` until THIS module accepts the result,
    and does so atomically with the product rows and the watermark — the
    same "one transaction per attempt" property `OneTransactionPerAttemptTests`
    pins for the watermark, extended to the work unit.
    """

    def test_a_successful_registration_completes_the_work_unit(self):
        conn = FakeConn()
        consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)],
            register=product_writer(conn))

        self.assertEqual("complete", conn.work_units[42])
        # C1 (migration 077): the completion is now one call into
        # `derived.transition_work_unit`, not a raw `UPDATE work_units`.
        completions = [(statement, params) for statement, params
                       in conn.committed
                       if "derived.transition_work_unit" in statement.lower()]
        self.assertEqual(1, len(completions))
        _, params = completions[0]
        # work_unit_id, from_state, to_state, writer, ... (C1's positional
        # order — see `WorkUnitWriter.transition_unit`'s call).
        self.assertEqual(42, params[0])
        self.assertEqual("submitted", params[1])
        self.assertEqual("complete", params[2])

    def test_the_completion_commits_with_the_product_rows_and_watermark(self):
        # The atomicity property itself: one commit carries the product
        # write, the watermark AND the work-unit completion, or none of
        # them survive. Mirrors
        # `OneTransactionPerAttemptTests.test_the_watermark_and_the_
        # product_rows_commit_together` with the completion folded in.
        conn = FakeConn()
        consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)],
            register=product_writer(conn))

        self.assertEqual(1, conn.commits,
                         "one attempt is one transaction, so one commit")
        committed = " ".join(statement for statement, _ in conn.committed)
        self.assertIn("addDiffImage", committed)
        self.assertIn("registered_record_sequence", committed)
        self.assertIn("derived.transition_work_unit", committed,
                      "the work-unit completion is not in the committed "
                      "transaction")

    def test_a_registration_failure_leaves_the_work_unit_submitted(self):
        # THE WHOLE FINDING, restated as a negative: nothing about this
        # attempt's registration commits, so the unit it would have
        # completed must not move either.
        conn = FakeConn()

        def explode(row, verdict):
            raise RuntimeError("no")

        consumer.register_batch(conn, [reconciled(1, work_unit_id=42)],
                                register=explode)

        self.assertNotIn(42, conn.work_units,
                         "the work unit was touched despite the "
                         "registration never committing")
        self.assertEqual(0, conn.commits)

    def test_a_validation_rejection_leaves_the_work_unit_submitted(self):
        # A REJECTION (integration ruling 4) commits its OWN outcome event
        # but must not complete the work unit — the record was refused, not
        # accepted, and rule 4 admits `complete` only from an accepted
        # result.
        from pipeline.registration.products import MissingRecordFact

        conn = FakeConn()

        def reject(row, verdict):
            raise MissingRecordFact("no bundle_key on this record")

        run = consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)], register=reject)

        self.assertEqual(1, run.rejected)
        self.assertEqual(1, conn.commits,
                         "the rejection's own outcome event still commits")
        self.assertNotIn(42, conn.work_units,
                         "a rejected record must not complete the unit it "
                         "was rejected for")

    def test_a_null_work_unit_id_completes_nothing(self):
        # Every pre-intent-layer row, and every row whose job type has no
        # loaded workflow_definitions row — mirrors the reconciler's
        # identical `_close_work_unit` guard. The registration itself must
        # still succeed; there is simply no work unit to complete.
        conn = FakeConn()
        run = consumer.register_batch(
            conn, [reconciled(1, work_unit_id=None)],
            register=product_writer(conn))

        self.assertEqual(1, run.registered)
        self.assertEqual({}, conn.work_units)
        completions = [statement for statement, _ in conn.statements
                      if statement.lower().startswith(
                          "update work_units set state")]
        self.assertEqual([], completions)

    def test_a_unit_no_longer_submitted_does_not_fail_the_registration(self):
        # Mirrors `_close_work_unit`'s own posture in the reconciler: a CAS
        # miss (another writer already resolved the unit — an operator
        # override, or finding 6's own guard having left a sibling to
        # settle it differently) is logged, not raised. The product rows
        # and the watermark this registration already wrote must still
        # commit; refusing to force a unit someone else has already
        # dispositioned is not a reason to roll all of that back.
        conn = FakeConn(work_units={42: "blocked"})
        run = consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)],
            register=product_writer(conn))

        self.assertEqual(1, run.registered)
        self.assertEqual(0, run.failed)
        self.assertEqual("blocked", conn.work_units[42],
                         "a unit already off 'submitted' must not be forced")
        self.assertEqual(1, conn.commits,
                         "the registration's own writes must still commit")


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

        # Filtered on the ASSIGNMENT form, not just the column name: the
        # post-lock re-read (integration ruling 4) selects `consumed_record_
        # sequence` (ruling R1), not `registered_record_sequence`, so it is
        # no longer ambiguous with this filter — kept as an assignment-form
        # filter anyway, matching this test's own stated preference for
        # being explicit about which SQL shape it means.
        watermarks = [params for statement, params in conn.committed
                      if "registered_record_sequence =" in statement]
        self.assertEqual([1, 2], [params[1] for params in watermarks],
                         "a supersession must advance the watermark to its "
                         "own sequence, or it stays a candidate forever")
        # Params are (moment, sequence, sequence, attempt_id, sequence) —
        # ruling R1 added the consumed-watermark sequence as a second SET
        # value (index 2). The CAS bound is still the same sequence, now at
        # index 4, so the guard is `< that`.
        self.assertEqual([1, 2], [params[4] for params in watermarks])


class RegistrationOutcomeTests(unittest.TestCase):
    """Migration 024's owed writer: the account of what registration did.

    The column has existed since 024 with no writer, so every registered
    attempt carried NULL — including all 1088 the role-binding replay
    registered. These fix the writer's contract.
    """

    def _outcome_writes(self, conn):
        return [(statement, params) for statement, params in conn.committed
                if "registration_outcome" in statement]

    def test_the_outcome_lands_in_the_same_transaction_as_the_watermark(self):
        # An account of a commit that did not happen is worse than none, so
        # it must be durable exactly when the products and watermark are.
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))
        self.assertEqual(1, len(self._outcome_writes(conn)))

    def test_the_event_carries_what_registration_resolved(self):
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))
        _statement, params = self._outcome_writes(conn)[0]
        event = json.loads(params["event"])
        self.assertEqual("promotion", event["type"])
        self.assertEqual(1, event["sequence"])

    def test_a_body_returning_nothing_structured_writes_no_event(self):
        # The reference path returns a dict today, but a body that returns
        # None must not fabricate a promotion event.
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=lambda row, verdict: None)
        self.assertEqual([], self._outcome_writes(conn))

    def test_the_append_is_keyed_so_a_replay_cannot_double_it(self):
        # The statement itself must carry the containment guard; a writer
        # that appended unconditionally would grow the document on every
        # replay pass, which is exactly what the role-binding replay does.
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))
        statement, _params = self._outcome_writes(conn)[0]
        self.assertIn("@>", statement)
        self.assertIn("GREATEST", statement)

    def test_the_document_is_an_object_as_the_check_constraint_requires(self):
        statement, _ = self._outcome_writes(
            self._registered_conn())[0]
        self.assertIn("jsonb_build_object", statement)

    def _registered_conn(self):
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))
        return conn


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

    AMENDED for integration ruling 4. `register_batch` now issues two more
    statement shapes as the FIRST things inside each attempt's transaction:
    `pg_advisory_xact_lock` (the lease — a no-op here, since exercising the
    lock itself needs postgres; what is under test is the CALL SHAPE, not
    postgres's locking) and the post-lock watermark re-read (a `SELECT
    registered_record_sequence, terminal_record_sequence FROM attempts ...`,
    answered from `watermarks`, a dict of attempt_id -> (registered, terminal)
    the test configures). The default — no entry — answers `(None, None)`,
    which every existing test relies on meaning "not registered yet, and no
    stale-supersession signal", i.e. registration proceeds exactly as it did
    before this lease existed.

    AMENDED for finding 7. `_complete_work_unit`'s transition needs a real
    CAS over SOME state, or a caller cannot tell "matched" from "did not" —
    `work_units`, a dict of work_unit_id -> state, is that state, defaulting
    every id to `submitted` (the state a candidate row is always in per the
    module's own invariant: the reconciler no longer completes a unit, so
    every registration candidate's unit is still exactly where it was left).

    AMENDED for C1 (campaign ruling R5, migration 077).
    `WorkUnitWriter.transition_unit` no longer issues a raw `UPDATE
    work_units` / `INSERT INTO unit_events` pair — both now live behind one
    `SELECT derived.transition_work_unit(...)` call. This fake models that
    ONE statement: it applies the same CAS against `work_units` the raw
    UPDATE used to, and on a miss raises the RA001-shaped error the real
    function raises (`pipeline.intent.errors.FakePgError`) rather than
    returning zero rows — `WorkUnitWriter` now reclassifies that into
    `WorkUnitNotFound` itself, so this fake no longer needs to emulate the
    rowcount-based path at all. `unit_events` is no longer written
    separately by this fake, matching the function owning that append.

    AMENDED for ruling R1 (migration 075, effect-lifecycle completion
    boundary). The post-lock re-read now reads the CONSUMED watermark, not
    the registered one — `watermarks` keeps its shape, (consumed,
    terminal), because REGISTER still advances both together and the
    tests that configure a raced/stale value are exercising the SAME race
    regardless of which watermark answers it. SKIP now also opens a
    transaction (it did not before this ruling — see the module docstring's
    "SKIP NOW RUNS UNDER THE SAME LEASE"), so a test exercising a SKIP
    verdict needs a real `FakeConn`, not `None`. `effect_attempt_counts` (a
    dict of work_unit_id -> count) answers `_effect_attempt_count`'s series
    read for an `effect_unconfirmed` verdict's retry-policy branch; unset
    ids read as 0.
    """

    def __init__(self, watermarks=None, work_units=None,
                effect_attempt_counts=None):
        self.statements = []
        self.committed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0
        self.lease_acquisitions = []
        #: attempt_id -> (consumed_record_sequence, terminal_record_sequence)
        #: answered by the post-lock re-read. Configure a stale/raced value
        #: here to exercise the skip paths; unset attempt ids re-read as
        #: (None, None) — "proceed", the pre-lease default.
        self.watermarks = dict(watermarks or {})
        #: work_unit_id -> state, mutated by `_complete_work_unit`'s CAS.
        #: Unset ids default to 'submitted' on first reference (see
        #: `execute`) — the only state a registration candidate's work unit
        #: is ever found in, absent a test deliberately pre-empting it.
        self.work_units = dict(work_units or {})
        #: work_unit_id -> effect-attempt series count (ruling R1), answered
        #: by `_effect_attempt_count`. Unset ids read as 0.
        self.effect_attempt_counts = dict(effect_attempt_counts or {})
        self._pending = []
        self._last_result = None
        self._last_description = None

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
        self._last_result = None
        self._last_description = None
        lowered = statement.lower()
        if "pg_advisory_xact_lock" in lowered:
            self.lease_acquisitions.append(params)
        elif ("consumed_record_sequence" in lowered
              and "terminal_record_sequence" in lowered
              and lowered.strip().startswith("select")):
            # THE POST-LOCK RE-READ (ruling R1: consumed, not registered —
            # see `consumer._REREAD_WATERMARK_SQL`'s own docstring for why).
            attempt_id = params[0] if params else None
            self._last_result = self.watermarks.get(attempt_id, (None, None))
        elif (lowered.strip().startswith("select count(*)")
              and "product_disposition" in lowered):
            # THE EFFECT-ATTEMPT SERIES COUNT (ruling R1,
            # `consumer._effect_attempt_count`), answered from
            # `effect_attempt_counts`, a dict of work_unit_id -> count the
            # test configures. Unset ids read as 0 — no prior effect
            # attempts, the ordinary case for a first unconfirmed effect.
            work_unit_id = params[0] if params else None
            self._last_result = (
                self.effect_attempt_counts.get(work_unit_id, 0),)
        elif "derived.transition_work_unit" in lowered:
            # The eight positional args `WorkUnitWriter.transition_unit`
            # passes (C1): work_unit_id, from_state, to_state, writer,
            # blocked_reason, reason, detail, lock.
            (work_unit_id, from_state, to_state, _writer, _blocked_reason,
             _reason, _detail, _lock) = params
            current = self.work_units.setdefault(work_unit_id, "submitted")
            if current == from_state:
                self.work_units[work_unit_id] = to_state
                self._last_result = [(None,)]  # the function returns void
            else:
                from pipeline.intent.errors import FakePgError
                raise FakePgError(
                    "RA001",
                    f"no work unit {work_unit_id} in state {from_state!r}")
            self._last_description = [("transition_work_unit",)]

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return list(self._last_result or [])

    @property
    def description(self):
        return self._last_description

    @property
    def rowcount(self):
        # Only reached by `_cursor_executor` for statements with no result
        # set (`description is None`) — the WU lock's bare `SELECT
        # pg_advisory_xact_lock(...)`, which this fake otherwise no-ops.
        # 1 mirrors a real driver reporting one row affected/returned for a
        # SELECT with no meaningful count of its own.
        return 1

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
        #
        # A real `FakeConn`, not `None` (ruling R1): a SKIP verdict now opens
        # a transaction under the per-attempt lease, same as REGISTER — see
        # the module docstring's "SKIP NOW RUNS UNDER THE SAME LEASE".
        row = reconciled(1, rapid_outcome="failure",
                         product_disposition="none",
                         scheduler_state="SUCCEEDED",
                         scheduler_observed_exit=0,
                         error_category="tool_failure")

        run = consumer.register_batch(FakeConn(), [row], dry_run=True)

        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)
        self.assertEqual(1, run.refused_application_failed)

    def test_a_never_started_attempt_registers_nothing(self):
        row = reconciled(1, lifecycle_state="terminal_without_start",
                         started_at=None, rapid_outcome=None,
                         product_disposition=None,
                         application_intended_exit=None,
                         error_category="scheduler_provisioning")

        run = consumer.register_batch(FakeConn(), [row], dry_run=True)

        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)

    def test_superseded_products_are_not_registered(self):
        row = reconciled(1, product_disposition="superseded")
        run = consumer.register_batch(FakeConn(), [row], dry_run=True)
        self.assertEqual(0, run.registered)

    def test_partial_success_is_not_registered_silently(self):
        row = reconciled(1, rapid_outcome="partial")
        run = consumer.register_batch(FakeConn(), [row], dry_run=True)
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


class SingleRegistrarLeaseTests(unittest.TestCase):
    """INTEGRATION RULING 4: the per-attempt lease's call shape.

    The lock itself can't be exercised without postgres (`pg_advisory_xact_lock`
    is a no-op in `FakeConn`, same as `FakeConnection` treats it for the
    reconciler's own lease tests) — what is under test is the SHAPE: the lease
    is acquired first, inside the attempt's own transaction, and the post-lock
    re-read happens before any registration work.
    """

    def test_the_lease_is_the_first_statement_in_the_attempts_transaction(self):
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=lambda row, verdict: None)

        self.assertIn("pg_advisory_xact_lock", conn.statements[0][0])
        self.assertEqual((consumer.ATTEMPT_LEASE_NAMESPACE, 1),
                         conn.statements[0][1])

    def test_the_post_lock_reread_happens_before_register_is_called(self):
        conn = FakeConn()
        order = []

        def register(row, verdict):
            order.append("register")

        consumer.register_batch(conn, [reconciled(1)], register=register)

        statement_kinds = [
            "lease" if "pg_advisory_xact_lock" in s else
            "reread" if "FROM attempts" in s else "other"
            for s, _ in conn.statements[:2]]
        self.assertEqual(["lease", "reread"], statement_kinds)
        self.assertEqual(["register"], order,
                         "register() must run only after the lease and the "
                         "re-read, not before")

    def test_the_lease_and_the_registration_share_one_transaction(self):
        # Acquired via the SAME cursor `_transaction(conn)` yields, so the
        # lease is inside the product/outcome/watermark envelope and releases
        # at that one commit — not a separate transaction of its own the way
        # the reconciler's standalone `attempt_lease` context manager is.
        conn = FakeConn()
        consumer.register_batch(conn, [reconciled(1)],
                                register=product_writer(conn))

        self.assertEqual(1, conn.commits)
        committed_statements = [s for s, _ in conn.committed]
        self.assertTrue(any("pg_advisory_xact_lock" in s
                            for s in committed_statements),
                        "the lease acquisition must be part of the same "
                        "committed unit of work as the registration")

    def test_a_stale_watermark_at_the_lease_is_a_clean_skip_not_a_failure(self):
        # Models the race the lease exists to close: another writer — the
        # operator pass or the registration job route — registered this exact
        # attempt at this exact sequence between this pass's unlocked
        # candidate read and this attempt's turn under the lease.
        conn = FakeConn(watermarks={1: (1, 1)})  # already registered at seq 1
        called = []

        def register(row, verdict):
            called.append(row["attempt_id"])
            return {"pid": 1, "version": 1, "product": "x",
                    "role_resolved_from": "record"}

        run = consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual([], called,
                         "a candidate already registered under the lease "
                         "must not reach the registration body at all")
        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)
        self.assertEqual(0, run.failed)
        # The lease and its re-read commit (the lease is transaction-scoped
        # and must release one way or the other), but nothing else does: no
        # product write, no watermark advance, no outcome event.
        committed_sql = [statement for statement, _ in conn.committed]
        self.assertTrue(all("pg_advisory_xact_lock" in s or "FROM attempts" in s
                            for s in committed_sql),
                        f"unexpected writes on a clean skip: {committed_sql}")

    def test_a_supersession_discovered_under_the_lease_is_a_clean_skip(self):
        # The candidate read saw terminal_record_sequence=1; by the time the
        # lease is held, the reconciler has published sequence 2. Registering
        # against the stale target would record the wrong sequence.
        conn = FakeConn(watermarks={1: (None, 2)})
        called = []

        def register(row, verdict):
            called.append(row["attempt_id"])

        run = consumer.register_batch(
            conn, [reconciled(1, terminal_record_sequence=1)],
            register=register)

        self.assertEqual([], called)
        self.assertEqual(0, run.registered)
        self.assertEqual(1, run.skipped)
        committed_sql = [statement for statement, _ in conn.committed]
        self.assertTrue(all("pg_advisory_xact_lock" in s or "FROM attempts" in s
                            for s in committed_sql),
                        f"unexpected writes on a clean skip: {committed_sql}")

    def test_a_fresh_candidate_under_the_lease_proceeds_normally(self):
        # The ordinary, non-racing case: watermark re-read agrees with the
        # candidate read, and registration proceeds exactly as before the
        # lease existed.
        conn = FakeConn(watermarks={1: (None, 1)})
        run = consumer.register_batch(conn, [reconciled(1)],
                                      register=product_writer(conn))

        self.assertEqual(1, run.registered)
        self.assertEqual(0, run.skipped)


class ValidationRejectionTests(unittest.TestCase):
    """INTEGRATION RULING 4: validation rejections are durable, not failures.

    `MissingRecordFact` and `RecordValidationRejected` (`pipeline.
    registration.products`) are the registrar's two validation classes — the
    record is missing a fact it needs, or a fact it has fails verification.
    Both commit a rejection outcome event and leave the watermark untouched.
    The plainer `RegistrationFailed` `RecordValidationRejected` subclasses is
    deliberately NOT one of them (see
    `test_a_bare_registration_failed_is_a_failure_not_a_rejection`): it also
    covers a retryable database conflict, so treating it as a rejection would
    misfile a retry as a permanent verdict. Every other exception keeps the
    prior failure/retry behaviour exactly.
    """

    def _rejection_writes(self, conn):
        return [(statement, params) for statement, params in conn.committed
                if "validation_rejections" in statement]

    def _watermark_writes(self, conn):
        return [(statement, params) for statement, params in conn.committed
                if "registered_record_sequence =" in statement]

    def test_a_missing_record_fact_is_rejected_not_failed(self):
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.MissingRecordFact("products", attempt_id=1)

        run = consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual(0, run.failed)
        self.assertEqual(1, run.rejected)
        self.assertEqual(0, run.registered)

    def test_a_checksum_mismatch_is_rejected(self):
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.RecordValidationRejected("checksum mismatch")

        run = consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual(0, run.failed)
        self.assertEqual(1, run.rejected)

    def test_a_rejection_commits_its_outcome_event(self):
        # Durable — the rejection is not lost when the transaction rolls
        # back, because it is not a rollback: the rejection's own transaction
        # commits the outcome event.
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.MissingRecordFact("products", attempt_id=1)

        consumer.register_batch(conn, [reconciled(1)], register=register)

        writes = self._rejection_writes(conn)
        self.assertEqual(1, len(writes))
        statement, params = writes[0]
        event = json.loads(params["event"])
        self.assertEqual("rejection", event["type"])
        self.assertEqual("MissingRecordFact", event["error_class"])
        self.assertEqual(1, conn.commits)
        self.assertEqual(0, conn.rollbacks)

    def test_a_rejection_does_not_advance_the_watermark(self):
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.RecordValidationRejected("checksum mismatch")

        consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual([], self._watermark_writes(conn),
                         "a validation rejection must leave the attempt "
                         "retryable, which the watermark write would undo")

    def test_a_rejection_writes_no_product_or_promotion_outcome(self):
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.MissingRecordFact("products", attempt_id=1)

        consumer.register_batch(conn, [reconciled(1)], register=register)

        promotions = [s for s, _ in conn.committed
                     if "'promotions'" in s]
        self.assertEqual([], promotions)

    def test_repeated_identical_rejections_do_not_grow_the_document(self):
        # THE CONVERGENCE PROPERTY. A record that fails validation the same
        # way keeps failing it the same way — the record is immutable — so a
        # rejection at the same record key/sequence/checksum on a later pass
        # must not accumulate a second, identical entry. The append-once
        # event-identity guard (mirroring `_RECORD_OUTCOME_SQL`'s) is what a
        # real database enforces via the `@>` containment check; this test
        # pins the SQL shape that guard depends on, since `FakeConn` does not
        # evaluate JSONB itself.
        self.assertIn("@>", consumer._RECORD_REJECTION_SQL)
        self.assertIn("GREATEST", consumer._RECORD_REJECTION_SQL)
        # And the event is fully determined by (attempt, record key,
        # sequence, checksum, error class) — nothing time-varying — so the
        # same rejection on the same record serializes identically every
        # pass, which is what makes the `@>` containment check actually
        # contain it.
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.MissingRecordFact("products", attempt_id=1)

        consumer.register_batch(conn, [reconciled(1)], register=register)
        first_event = json.loads(self._rejection_writes(conn)[0][1]["event"])

        conn2 = FakeConn()
        consumer.register_batch(conn2, [reconciled(1)], register=register)
        second_event = json.loads(self._rejection_writes(conn2)[0][1]["event"])

        self.assertEqual(first_event, second_event)

    def test_a_non_validation_exception_is_still_a_failure_not_a_rejection(self):
        # Infrastructure failures keep the prior behaviour exactly: counted
        # as failed, no outcome event, retried next pass — not folded into
        # the new rejection path.
        conn = FakeConn()

        def register(row, verdict):
            raise RuntimeError("the database connection dropped")

        run = consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual(1, run.failed)
        self.assertEqual(0, run.rejected)
        self.assertEqual([], self._rejection_writes(conn))

    def test_a_bare_registration_failed_is_a_failure_not_a_rejection(self):
        # THE TAXONOMY BOUNDARY. `products._check` raises plain
        # `RegistrationFailed` when a stored-procedure call reports
        # `dbh.exit_code >= 64` — a code `rapid_db.py` also sets for a
        # genuine natural-unique or partial `vbest`-index conflict
        # (catalog.md § Promotion, "Conflicts": explicitly RETRYABLE, not a
        # permanent verdict on the record) as well as for an actual database
        # fault. Catching the broad `RegistrationFailed` here would misfile
        # a retryable conflict as a durable rejection — only the narrower
        # `RecordValidationRejected` (raised solely by `read_record`'s own
        # checksum/identity checks) is caught as a rejection.
        conn = FakeConn()

        def register(row, verdict):
            raise consumer.RegistrationFailed(
                "update_refimage failed for attempt 1: rapid_db exit_code 67")

        run = consumer.register_batch(conn, [reconciled(1)], register=register)

        self.assertEqual(1, run.failed)
        self.assertEqual(0, run.rejected)
        self.assertEqual([], self._rejection_writes(conn))

    def test_record_validation_rejected_is_a_registration_failed_subclass(self):
        # So any caller that already catches the broader class still catches
        # this one — the narrowing is additive, not a breaking split.
        self.assertTrue(issubclass(consumer.RecordValidationRejected,
                                   consumer.RegistrationFailed))

    def test_a_rejection_does_not_stop_the_pass(self):
        # Per-attempt, like a failure: one rejected record must not block
        # attempts after it in the same pass.
        conn = FakeConn()

        def register(row, verdict):
            if row["attempt_id"] == 1:
                raise consumer.MissingRecordFact("products", attempt_id=1)
            return {"pid": 2, "version": 1, "product": "x",
                    "role_resolved_from": "record"}

        run = consumer.register_batch(
            conn, [reconciled(1), reconciled(2)], register=register)

        self.assertEqual(1, run.rejected)
        self.assertEqual(1, run.registered)


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


class BindFencedMessageTests(unittest.TestCase):
    """`BindFenced`'s message names the actual possible holders.

    `acquire_fence`'s `False` return carries no holder identity (see
    `pipeline.gc.fence.acquire_fence`'s docstring: the conflicting
    `ON CONFLICT ... WHERE expires_at < now()` returns nothing from
    `RETURNING`), so `_bind_fence` cannot know at the raise site whether
    GC or another registration attempt holds the key. The message used to
    hardcode "GC holds it live" regardless — wrong for the commoner case,
    a registration self-collision (two passes racing the same key; see
    `BindFenceKeysTests` above), which involves no GC fence at all. This
    pins the fixed wording: it must name registration-on-registration
    collision as the likelier cause, still mention GC as the other
    possibility, and must NOT assert GC unconditionally.
    """

    def test_the_message_names_both_possible_holders(self):
        error = consumer.BindFenced(
            "rapid-science", "science/r/u/attempt-9/a.fits", attempt_id=9)
        message = str(error)

        self.assertIn("registration", message)
        self.assertIn("gc", message.lower())
        self.assertNotIn(
            "GC holds it live", message,
            "the message still asserts a specific holder acquire_fence's "
            "False return cannot actually tell it")


class BindFenceKeysTests(unittest.TestCase):
    """`_bind_fence_keys` returns each `(bucket, object_key)` at most once."""

    def test_one_key_named_by_two_products_is_fenced_once(self):
        """Two entries over ONE object fence it once, not twice.

        `published()` indexes entries by NAME, so a record may carry two
        differently-named entries whose `uri` is the same object. Before this
        was deduplicated, `_bind_fence` acquired that key, then acquired it
        again for the second entry — colliding with the live, unexpired lease
        it was itself still holding. `acquire_fence`'s `ON CONFLICT ... WHERE
        expires_at < now()` found its own row, returned nothing, and
        `BindFenced` was raised against the bind's own fence: the attempt
        failed closed on every retry, reporting "GC holds it live" while
        `gc_fences` held nothing but that row.
        """
        shared = "s3://b/science/r/u/attempt-1/shared.fits"
        record = {
            "attempt_id": 1,
            "products": [
                {"name": "zogy_uncert", "uri": shared, "checksum": "c1"},
                {"name": "zogy_weight", "uri": shared, "checksum": "c1"},
                {"name": "other",
                 "uri": "s3://b/science/r/u/attempt-1/other.fits",
                 "checksum": "c2"},
            ],
        }

        keys = consumer._bind_fence_keys(record, attempt_id=1)

        self.assertEqual(len(keys), len(set(keys)),
                         "a key was fenced more than once")
        self.assertEqual(len(keys), 2)
        self.assertIn(("b", "science/r/u/attempt-1/shared.fits"), keys)


class AbortingConn(FakeConn):
    """A `FakeConn` with PostgreSQL's aborted-transaction semantics.

    `FakeConn` models a connection that can never be poisoned: after a
    failed statement the next one succeeds, and `commit()` always makes
    `_pending` durable. A real psycopg2 connection does neither. Once any
    statement in a transaction raises, PostgreSQL refuses every subsequent
    statement on that connection with `InFailedSqlTransaction` until the
    transaction ends, and `commit()` on an aborted transaction **does not
    raise** -- the server treats it as `ROLLBACK` and reports success. That
    asymmetry is the whole defect this class exists to expose: the consumer
    reads "commit() returned without raising" as "the attempt registered",
    and an aborted transaction cannot contradict it.

    `fail_on` is the substring of the statement that should raise, and
    `fail_times` how many times (default: every time it matches). After a
    raise the connection is `aborted` until `commit()` or `rollback()`
    ends the transaction -- and a `commit()` that ends an ABORTED
    transaction discards `_pending` exactly as a rollback would, while
    still counting as a commit, because that is what the driver reports.
    """

    def __init__(self, fail_on=None, fail_times=None, **kwargs):
        super().__init__(**kwargs)
        self.fail_on = fail_on
        self.fail_times = fail_times
        self.failures_raised = 0
        self.aborted = False
        #: Statements the server refused because the transaction was
        #: already aborted -- the fingerprint of a poisoned transaction.
        self.refused_while_aborted = []
        #: One entry per commit: True if it made rows durable, False if it
        #: silently discarded them because the transaction was aborted.
        self.commit_was_durable = []

    def execute(self, statement, params=None):
        if self.aborted:
            self.refused_while_aborted.append((statement, params))
            raise RuntimeError(
                "current transaction is aborted, commands ignored until "
                "end of transaction block")
        if (self.fail_on is not None and self.fail_on in statement
                and (self.fail_times is None
                     or self.failures_raised < self.fail_times)):
            self.failures_raised += 1
            self.aborted = True
            raise RuntimeError("permission denied for table diffimages")
        return super().execute(statement, params)

    def commit(self):
        # A commit on an ABORTED transaction does NOT raise and does NOT
        # make anything durable: the server treats it as ROLLBACK and
        # reports success. Measured against the live database 2026-09-12
        # inside the pinned image. `commit_was_durable` records which of
        # the two happened, which is the fact the driver never volunteers
        # and the consumer's counter needs.
        durable = not self.aborted
        self.commit_was_durable.append(durable)
        self.commits += 1
        if durable:
            self.committed.extend(self._pending)
        self._pending = []
        self.aborted = False

    def rollback(self):
        # Ending the transaction clears the abort, which is why a rollback
        # between attempts is what makes the next one able to run at all.
        self.rollbacks += 1
        self._pending = []
        self.aborted = False


def swallowing_product_writer(conn, rows=None):
    """A `register` callback that aborts the transaction WITHOUT raising.

    This is `rapid_db.py`'s real error contract, and it is what
    `product_writer` above cannot model. Every one of that module's
    thirty-two product methods handles a database error like this
    (`update_diffimage`, `database/modules/utils/rapid_db.py`):

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating DiffImages record ({}); skipping...')
            self.exit_code = 67
            return

    -- it catches, prints, sets `exit_code`, and RETURNS NORMALLY. The
    statement still failed, so the connection's transaction is aborted;
    but no exception reaches `register_batch`, so its per-attempt `except`
    never runs, the `with _transaction(conn)` block exits cleanly, and
    `_transaction`'s `else:` branch calls `conn.commit()` on an aborted
    transaction. Measured against the live database (2026-09-12, inside
    the pinned image): that commit returns WITHOUT raising and makes zero
    rows durable. `register_batch` then counts the attempt registered.

    `product_writer` raises, so it exercises the path that already works.
    This one exercises the path that produced `registered: 19` over zero
    product rows.
    """
    rows = rows if rows is not None else []

    def register(row, verdict):
        try:
            with conn.cursor() as cur:
                cur.execute("select * from addDiffImage(...)",
                            (row["attempt_id"],
                             row.get("terminal_record_sequence")))
        except Exception:                          # noqa: BLE001
            # `rapid_db`'s own contract: swallowed, reported by a flag the
            # caller may or may not read, and NOT re-raised.
            rows.append((row["attempt_id"], "swallowed"))
            return {"products": []}
        rows.append((row["attempt_id"],
                     row.get("terminal_record_sequence")))
        return {"products": [{"name": "difference_image", "pid": 1,
                              "version": 1}]}

    return register

class DiscardingConn(AbortingConn):
    """An `AbortingConn` whose post-failure statements SUCCEED.

    This is the live shape, and it is the one `AbortingConn` alone does not
    reproduce. Once a statement has failed, this connection lets the
    statements that follow run (they touch nothing that the aborted
    transaction refuses -- an advisory lock, a watermark UPDATE matching no
    row) while the transaction remains aborted, so the eventual `commit()`
    silently discards everything.

    WHY THIS MATTERS FOR REPRODUCING `registered: 19`. In the live run the
    registrar swallowed its database error (`rapid_db.py` catches, sets
    `exit_code`, returns) and the attempts that followed printed successful
    `updateDiffImage` / `registerDiffImMeta` results -- `('',)` -- right up
    to the summary line. Statements succeeding is what distinguishes this
    from a connection that refuses everything: a refusing connection raises,
    and `register_batch`'s per-attempt `except` already counts that as
    failed. A connection that answers normally and discards at commit is
    the one the counter cannot see, and the only evidence is the summary it
    prints.
    """

    def execute(self, statement, params=None):
        if self.aborted and (self.fail_on or "") not in statement:
            # Answered normally, as the live run's post-failure statements
            # were. The transaction stays aborted regardless, which is what
            # the commit below then discards.
            return FakeConn.execute(self, statement, params)
        return super().execute(statement, params)


class RegistrationCountsOnlyWhatItCommittedTests(unittest.TestCase):
    """The acceptance run's `registered: 19` over zero product rows.

    Found live on 2026-09-11 registering `accept-20260911`: a 20-attempt
    scoped registration printed `{'registered': 19, ..., 'failed': 1}` and
    `rows affected: 19` while writing nothing at all. Re-verified against
    the live database on 2026-09-12: of 13,204 `accept-20260911%` attempts
    with `registered_at IS NULL`, exactly 20 are genuine registration
    candidates, and every one of them still has a NULL
    `consumed_record_sequence` -- so not one of the nineteen "registered"
    attempts advanced its watermark either.

    THE MECHANISM, measured rather than reasoned. Inside the pinned image,
    against the live database:

        commit() on an ABORTED transaction: returned WITHOUT raising
        rows durable after that commit: 0

    `register_batch` increments `run.registered` in the `else:` clause of
    the `try` around its per-attempt `with _transaction(conn)` block -- so
    it counts an attempt registered whenever nothing raised. An aborted
    transaction's commit does not raise and writes nothing, so the two
    differ exactly when something below swallowed a database error instead
    of raising it. That is `rapid_db.py`'s documented contract for all
    thirty-two of its product methods (`update_diffimage`: catch, print
    'skipping...', set `exit_code = 67`, return).

    The counter is therefore derived from committed state instead, and an
    attempt that reported success while committing nothing gets its own
    count (`uncommitted`) rather than being folded into either
    `registered` or `failed` -- the two are different findings, and folding
    is how this stayed invisible.
    """

    def test_an_attempt_whose_commit_discarded_its_rows_is_not_registered(self):
        # THE DEFECT, minimal and exact: one attempt, its product write
        # swallowed, every later statement answering normally, and the
        # commit discarding the lot.
        conn = DiscardingConn(fail_on="addDiffImage")

        run = consumer.register_batch(
            conn, [reconciled(1)],
            register=swallowing_product_writer(conn))

        self.assertEqual([False], conn.commit_was_durable,
                         "the fixture must actually have discarded the "
                         "commit, or this test proves nothing")
        self.assertEqual([], conn.committed,
                         "nothing can be durable: the transaction was "
                         "aborted when it committed")
        self.assertEqual(
            0, run.registered,
            "an attempt whose commit discarded every row it wrote was "
            "counted as registered -- this is `registered: 19` over zero "
            "product rows")

    def test_the_discarded_attempt_is_counted_as_uncommitted(self):
        # It must not vanish either. An attempt that reported success and
        # committed nothing is a third outcome, and needs to appear as one:
        # `failed` would say an error path ran, and none did.
        conn = DiscardingConn(fail_on="addDiffImage")

        run = consumer.register_batch(
            conn, [reconciled(1)],
            register=swallowing_product_writer(conn))

        self.assertEqual(
            1, run.uncommitted,
            "the attempt that committed nothing is unaccounted for")
        self.assertEqual(0, run.failed,
                         "nothing raised, so no error path ran")

    def test_a_run_containing_a_discarded_attempt_cannot_exit_zero(self):
        # The operator-facing half. A pass that wrote nothing while saying
        # it registered is the most dangerous outcome here, so it must not
        # look like a clean run. Had the live run's FIRST attempt swallowed
        # its error like the nineteen after it, the pass would have exited 0.
        conn = DiscardingConn(fail_on="addDiffImage")

        run = consumer.register_batch(
            conn, [reconciled(1)],
            register=swallowing_product_writer(conn))

        self.assertNotEqual(
            0, run.exit_code,
            "a pass that registered nothing while reporting success exited "
            "zero")

    def test_the_registered_count_equals_the_durable_commits(self):
        # The invariant stated directly, over a mixed batch: attempt 1's
        # write is swallowed and discarded, attempts 2 and 3 commit for
        # real. Whatever the counters say, `registered` must equal the
        # number of commits that actually made rows durable.
        rows = [reconciled(n) for n in (1, 2, 3)]
        conn = DiscardingConn(fail_on="addDiffImage", fail_times=1)

        run = consumer.register_batch(
            conn, rows, register=swallowing_product_writer(conn))

        durable = [d for d in conn.commit_was_durable if d]
        self.assertEqual(
            run.registered, len(durable),
            "the registered count does not equal the number of commits "
            "that actually made rows durable")
        self.assertEqual(
            len(rows),
            (run.registered + run.failed + run.uncommitted + run.skipped
             + run.deferred + run.rejected),
            "every attempt in the batch must be accounted for exactly once")


class SavepointConn(FakeConn):
    """A `FakeConn` that models SAVEPOINT / ROLLBACK TO / RELEASE, and the
    fact that an unguarded failed statement aborts the whole transaction.

    `FakeConn` cannot show this defect: its `execute` never fails, so the
    CAS miss that `_complete_work_unit` swallows arrives as a bare Python
    exception with no transaction consequences. On a real connection the
    miss is SQLSTATE RA001 raised by `derived.transition_work_unit`, which
    aborts the transaction -- and a savepoint is the only thing that keeps
    the abort from reaching the writes made before it.

    `work_units` starting a unit in any state but `submitted` is what makes
    the CAS miss, exactly as the live database had them (`blocked`, from
    earlier failed siblings).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aborted = False
        #: Savepoint names currently held, innermost last.
        self.savepoints = []
        #: True for each commit that made rows durable.
        self.commit_was_durable = []

    def execute(self, statement, params=None):
        lowered = statement.strip().lower()
        if lowered.startswith("savepoint "):
            self.savepoints.append(lowered.split()[1])
            self.statements.append((statement, params))
            return None
        if lowered.startswith("rollback to savepoint "):
            # The abort is undone, which is the whole point of the guard.
            self.aborted = False
            name = lowered.split()[-1]
            if name in self.savepoints:
                del self.savepoints[self.savepoints.index(name):]
            self.statements.append((statement, params))
            return None
        if lowered.startswith("release savepoint "):
            name = lowered.split()[-1]
            if name in self.savepoints:
                self.savepoints.remove(name)
            self.statements.append((statement, params))
            return None
        if self.aborted:
            raise RuntimeError(
                "current transaction is aborted, commands ignored until "
                "end of transaction block")
        try:
            return super().execute(statement, params)
        except Exception:
            # A failed statement aborts the transaction. `FakeConn` raises
            # the RA001-shaped `FakePgError` from its
            # `derived.transition_work_unit` branch on a CAS miss; on a real
            # connection that abort is the server's, not the driver's.
            self.aborted = True
            raise

    def commit(self):
        durable = not self.aborted
        self.commit_was_durable.append(durable)
        self.commits += 1
        if durable:
            self.committed.extend(self._pending)
        self._pending = []
        self._pending = []
        self.aborted = False
        self.savepoints = []

    def rollback(self):
        self.rollbacks += 1
        self._pending = []
        self.aborted = False
        self.savepoints = []


class ACASMissDoesNotDiscardTheRegistrationTests(unittest.TestCase):
    """`_complete_work_unit`'s CAS miss must cost only the transition.

    Its docstring promises exactly this -- "a CAS miss is logged, not
    raised ... refusing to complete a unit someone else has already
    dispositioned is not a reason to roll all of that back" -- and before
    the savepoint the code delivered the opposite. `derived.
    transition_work_unit` signals a miss by RAISING SQLSTATE RA001, which
    ABORTS the transaction; PostgreSQL then discards everything it wrote
    and its COMMIT silently behaves as ROLLBACK. Catching the Python
    exception does not un-abort anything.

    Measured live on 2026-09-12 registering `accept-20260911`: 18 of 19
    candidates wrote their product rows, outcome, watermark and milestone
    successfully, and every one was discarded here. Stepping the
    post-register calls one at a time showed the transaction status going
    from 2 (INTRANS) to 3 (INERROR) at `_complete_work_unit` and at no
    other call. The units were `blocked`, not `submitted` -- earlier failed
    siblings had blocked them -- which is the very case the posture exists
    to let through.
    """

    def test_a_cas_miss_still_commits_the_product_rows_and_watermark(self):
        # The unit is `blocked`, so the SUBMITTED -> COMPLETE CAS misses,
        # exactly as the live units did.
        conn = SavepointConn(work_units={42: "blocked"})

        run = consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)],
            register=product_writer(conn))

        self.assertEqual(
            [True], conn.commit_was_durable,
            "the commit discarded everything: a CAS miss aborted the "
            "transaction instead of costing only its own statement")
        committed = " ".join(statement for statement, _ in conn.committed)
        self.assertIn("addDiffImage", committed,
                      "the product write was discarded by the CAS miss")
        self.assertIn("registered_record_sequence", committed,
                      "the watermark was discarded by the CAS miss")
        self.assertEqual(
            1, run.registered,
            "the attempt registered -- its rows and watermark are durable "
            "-- so it must be counted as registered")
        self.assertEqual(0, run.uncommitted)

    def test_the_cas_runs_inside_a_savepoint(self):
        # The mechanism, named. Without the savepoint there is nothing to
        # roll back to and the abort reaches the whole transaction.
        conn = SavepointConn(work_units={42: "blocked"})

        consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)],
            register=product_writer(conn))

        issued = [statement.strip().lower()
                  for statement, _ in conn.statements]
        self.assertTrue(
            any(s.startswith("savepoint ") for s in issued),
            "no SAVEPOINT was taken around the work-unit CAS")
        self.assertTrue(
            any(s.startswith("rollback to savepoint ") for s in issued),
            "the CAS miss did not roll back to its savepoint, so the "
            "transaction stayed aborted")

    def test_a_successful_transition_releases_its_savepoint(self):
        # The other half: a unit in `submitted` transitions, and the
        # savepoint is released rather than left open. A savepoint left
        # open per attempt would accumulate across a batch.
        conn = SavepointConn(work_units={42: "submitted"})

        run = consumer.register_batch(
            conn, [reconciled(1, work_unit_id=42)],
            register=product_writer(conn))

        issued = [statement.strip().lower()
                  for statement, _ in conn.statements]
        self.assertTrue(
            any(s.startswith("release savepoint ") for s in issued),
            "a successful transition left its savepoint open")
        self.assertEqual([], conn.savepoints,
                         "a savepoint was left held at the end of the pass")
        self.assertEqual("complete", conn.work_units[42])
        self.assertEqual(1, run.registered)

    def test_a_cas_miss_mid_batch_does_not_harm_the_attempts_after_it(self):
        # The batch-level property. Attempt 1's unit is blocked and misses;
        # attempts 2 and 3 have submitted units and must register normally.
        conn = SavepointConn(work_units={41: "blocked", 42: "submitted",
                                         43: "submitted"})
        rows = [reconciled(1, work_unit_id=41),
                reconciled(2, work_unit_id=42),
                reconciled(3, work_unit_id=43)]

        run = consumer.register_batch(conn, rows,
                                      register=product_writer(conn))

        self.assertEqual([True, True, True], conn.commit_was_durable,
                         "an attempt's commit discarded its rows")
        self.assertEqual(3, run.registered)
        self.assertEqual(0, run.uncommitted)
        self.assertEqual(0, run.failed)


if __name__ == "__main__":
    unittest.main()
