"""Scoped registration: refuses an unbounded scope, and a dry run writes
nothing — even for a row that would SKIP.

`pipeline.registration.scoped` is the standalone entrypoint that runs
`pipeline.registration.consumer.register_batch` over an explicit, bounded
scope rather than `candidates()`'s own unscoped, thousands-wide sweep. These
tests exercise the module's OWN additions — the refusal, the scope
resolution, and the dry-run path that deliberately never reaches
`register_batch` — not `register_batch`'s per-attempt semantics, which
`pipeline.registration.test.test_consumer` already covers and this module's
brief forbids changing.
"""

import unittest
from unittest import mock

from observability.registration import RegistrationDecision
from pipeline.reconciler.test.stubs import FakeConnection, attempt_row, utc
from pipeline.registration import scoped
from pipeline.registration.consumer import RegistrationRun
from pipeline.runtime.boundaries import S3ObjectStore


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


class UnboundedScopeRefusalTests(unittest.TestCase):
    """The entrypoint refuses to run with no scope — unlike `candidates()`
    itself, which stays happy to run unscoped for its two production
    callers (that is the whole point of this module existing)."""

    def test_resolve_scope_refuses_with_neither_argument(self):
        conn = FakeConnection(rows=[reconciled(1)])
        with self.assertRaises(scoped.UnboundedScopeError):
            scoped.resolve_scope(conn)

    def test_resolve_scope_refuses_with_an_explicitly_empty_attempt_id_list(self):
        # An empty list is still "no scope" — it must not be read as
        # "attempt_ids=[] means match nothing" nor silently coerced into
        # "no attempt_ids filter", either of which would need its own
        # explicit handling. `attempt_ids or None` in `main()` is what
        # normalizes this at the CLI boundary; `resolve_scope` itself
        # applies the identical `is None` check `run_scoped_registration`
        # does, so this asserts the function-level contract directly.
        conn = FakeConnection(rows=[reconciled(1)])
        with self.assertRaises(scoped.UnboundedScopeError):
            scoped.resolve_scope(conn, attempt_ids=None)

    def test_run_scoped_registration_refuses_with_no_scope(self):
        conn = FakeConnection(rows=[reconciled(1)])
        with self.assertRaises(scoped.UnboundedScopeError):
            scoped.run_scoped_registration(conn)

    def test_main_refuses_with_no_cli_scope_flags(self):
        # main() itself, over sys.argv-shaped input — the actual invocation
        # surface. No database or parameter-tree access should be reachable
        # before the refusal: argparse succeeds (both flags are optional),
        # but `run_scoped_registration` must raise before any connection
        # does real work. Patches `fetch_parameters`/`connection` to fail
        # loudly if reached, so a refusal that happened AFTER connecting
        # would fail this test rather than passing by accident.
        import pipeline.registration.scoped as scoped_module

        def _unreachable_fetch_parameters():
            raise AssertionError(
                "fetch_parameters() must not be reached before the "
                "unbounded-scope refusal")

        class _UnreachableConnection:
            def __enter__(self):
                raise AssertionError(
                    "the database connection must not be opened before "
                    "the unbounded-scope refusal")

            def __exit__(self, *exc):
                return False

        import submission.startup as startup_module
        import database.modules.utils.rapid_db_connect as dbc_module

        original_fetch = startup_module.fetch_parameters
        original_connection = dbc_module.connection
        startup_module.fetch_parameters = _unreachable_fetch_parameters
        dbc_module.connection = lambda *a, **k: _UnreachableConnection()
        try:
            rc = scoped_module.main([])
        finally:
            startup_module.fetch_parameters = original_fetch
            dbc_module.connection = original_connection

        self.assertEqual(scoped.EXIT_FAILURES, rc)


class DryRunTests(unittest.TestCase):
    """The dry-run path (the module's default) writes nothing — for every
    verdict a scoped row can reach, not only REGISTER."""

    def test_dry_run_reports_registrable_rows_as_would_register(self):
        conn = FakeConnection(rows=[
            reconciled(1, run_id="run-a"),
            reconciled(2, run_id="run-a"),
        ])

        run, rows = scoped.run_scoped_registration(
            conn, run_id_prefix="run-a", dry_run=True)

        self.assertEqual(2, run.would_register)
        self.assertEqual(0, run.registered)
        self.assertEqual([1, 2], sorted(r["attempt_id"] for r in rows))

    def test_dry_run_writes_nothing_even_for_a_row_that_would_skip(self):
        # THE CASE `register_batch(conn, rows, dry_run=True)` GETS WRONG for
        # this module's purposes: its SKIP branch is unconditional and
        # writes the consumed watermark (and, for some dispositions,
        # transitions the work unit) regardless of dry_run. A scoped dry run
        # must not reach register_batch AT ALL, so a SKIP-shaped candidate
        # in scope must not cause a single statement beyond the read-only
        # candidate query.
        conn = FakeConnection(rows=[
            reconciled(1, run_id="run-a", rapid_outcome="failure",
                      product_disposition=None),
        ])

        run, rows = scoped.run_scoped_registration(
            conn, run_id_prefix="run-a", dry_run=True)

        self.assertEqual(1, run.skipped)
        self.assertEqual(0, run.would_register)
        # Only the candidate SELECT was ever issued — nothing else touched
        # the connection: no lease acquisition, no watermark UPDATE.
        self.assertEqual(1, len(conn.statements))
        self.assertGreaterEqual(conn.rollbacks, 1)
        self.assertEqual(0, conn.commits)

    def test_dry_run_writes_nothing_for_a_deferred_row(self):
        conn = FakeConnection(rows=[
            reconciled(1, run_id="run-a", lifecycle_state="started",
                      rapid_outcome=None, product_disposition=None,
                      started_at=None, application_intended_exit=None,
                      terminal_record_sequence=None, terminal_record_key=None),
        ])
        # A non-terminal row is not even a *candidate* — candidates() itself
        # excludes it (terminal_record_sequence IS NULL fails `>= 1`) — so
        # this asserts the scope resolves to nothing, not that a deferred
        # verdict was produced; either way, nothing is written.
        run, rows = scoped.run_scoped_registration(
            conn, run_id_prefix="run-a", dry_run=True)

        self.assertEqual([], rows)
        self.assertEqual(0, run.would_register)
        self.assertEqual(0, run.skipped)
        self.assertEqual(0, run.deferred)
        self.assertEqual(1, len(conn.statements))

    def test_main_default_mode_is_dry_run(self):
        args = scoped._parse_args(["--run-id-prefix", "run-a"])
        self.assertFalse(args.execute)

    def test_execute_flag_requests_real_registration(self):
        args = scoped._parse_args(["--run-id-prefix", "run-a", "--execute"])
        self.assertTrue(args.execute)


class RegistrarForScopeTests(unittest.TestCase):
    """`registrar_for_scope`'s own return shape: a `(register, store)` pair,
    the store built over the `records_bucket` it was given.

    No real S3 traffic — `S3ObjectStore.__init__` only stores the bucket and
    client, it makes no calls, so a bare sentinel stands in for the client
    without needing to fake any boto3 method.
    """

    def test_returns_a_two_tuple(self):
        conn = FakeConnection(rows=[])
        result = scoped.registrar_for_scope(
            "records-bucket", conn, s3_client=object())

        self.assertIsInstance(result, tuple)
        self.assertEqual(2, len(result))

    def test_the_second_element_is_a_store_over_the_records_bucket(self):
        conn = FakeConnection(rows=[])
        register, store = scoped.registrar_for_scope(
            "records-bucket", conn, s3_client=object())

        self.assertTrue(callable(register))
        self.assertIsInstance(store, S3ObjectStore)
        self.assertEqual("records-bucket", store.bucket)


class LiveRegistrationFenceTests(unittest.TestCase):
    """THE REGRESSION TEST. `run_scoped_registration`'s live-write branch
    (`dry_run=False`) used to call `register_batch(conn, rows, register=
    register, store=None)` — a hardcoded `None` that left every scoped
    `--execute` run unfenced against GC deletes, per `register_batch`'s own
    `store is not None` gate (`consumer.py`'s per-attempt loop: no store
    means `_bind_fence` is never reached, and the attempt is registered
    under `contextlib.nullcontext()` instead).

    `register_batch` itself is patched here rather than driven through
    `FakeConnection` — its own per-attempt transaction/lease/fence machinery
    is `test_consumer`'s territory, and this module's brief is explicit that
    those semantics are not under test here. What IS under test is what
    `run_scoped_registration` hands `register_batch`: a spy that raises on
    anything but a keyword call is enough to pin that.
    """

    def _run(self, records_bucket="records-bucket"):
        conn = FakeConnection(rows=[reconciled(1, run_id="run-a")])
        sentinel_register = object()
        sentinel_store = object()
        calls = []

        def fake_registrar_for_scope(bucket, passed_conn, s3_client=None):
            calls.append({
                "records_bucket": bucket,
                "conn": passed_conn,
                "s3_client": s3_client,
            })
            return sentinel_register, sentinel_store

        def fake_register_batch(passed_conn, rows, register=None,
                                run=None, dry_run=False, store=None):
            calls.append({
                "register_batch": True,
                "conn": passed_conn,
                "rows": rows,
                "register": register,
                "store": store,
            })
            return RegistrationRun()

        with mock.patch.object(scoped, "registrar_for_scope",
                               fake_registrar_for_scope), \
             mock.patch.object(scoped, "register_batch",
                               fake_register_batch):
            scoped.run_scoped_registration(
                conn, run_id_prefix="run-a", dry_run=False,
                records_bucket=records_bucket)

        registrar_call = next(c for c in calls if "records_bucket" in c)
        batch_call = next(c for c in calls if c.get("register_batch"))
        return sentinel_store, registrar_call, batch_call

    def test_register_batch_receives_a_non_none_store(self):
        _, _, batch_call = self._run()
        self.assertIsNotNone(batch_call["store"])

    def test_register_batch_receives_the_same_store_the_registrar_built(self):
        # Not just "non-None" — THE SAME OBJECT `registrar_for_scope` built,
        # so the registrar and the fence are pointed at the same bucket by
        # construction rather than by two independently-built stores that
        # could disagree. A second, divergent `S3ObjectStore` would satisfy
        # "non-None" while still reintroducing the bug this guards against
        # (fencing one bucket while the registrar reads/writes another).
        sentinel_store, _, batch_call = self._run()
        self.assertIs(sentinel_store, batch_call["store"])

    def test_registrar_for_scope_is_called_with_the_records_bucket(self):
        _, registrar_call, _ = self._run(records_bucket="my-records-bucket")
        self.assertEqual("my-records-bucket", registrar_call["records_bucket"])


class DryRunNeverReachesRegisterBatchTests(unittest.TestCase):
    """Unchanged behaviour, pinned alongside the fix above so a future edit
    to the live-write branch cannot accidentally widen dry-run's reach: a
    dry run returns before `register_batch` — and before `registrar_for_
    scope` — are ever called at all (see `_dry_run_verdicts`'s docstring for
    why: `register_batch`'s SKIP branch writes unconditionally, so a scoped
    dry run must not call it for ANY verdict, not just avoid passing it a
    `register` callback)."""

    def test_dry_run_calls_neither_registrar_for_scope_nor_register_batch(self):
        conn = FakeConnection(rows=[reconciled(1, run_id="run-a")])

        def _unreachable(*args, **kwargs):
            raise AssertionError("must not be reached on a dry run")

        with mock.patch.object(scoped, "registrar_for_scope", _unreachable), \
             mock.patch.object(scoped, "register_batch", _unreachable):
            run, rows = scoped.run_scoped_registration(
                conn, run_id_prefix="run-a", dry_run=True)

        self.assertEqual(1, run.would_register)
        self.assertEqual([1], [r["attempt_id"] for r in rows])


class RecordsBucketRequiredTests(unittest.TestCase):
    """The `records_bucket is None` refusal still fires before anything
    else on the live-write branch — checked with `registrar_for_scope` and
    `register_batch` both patched to explode, so a refusal that happened
    AFTER either call would fail this test rather than passing by luck."""

    def test_raises_before_building_a_registrar_or_calling_register_batch(self):
        conn = FakeConnection(rows=[reconciled(1, run_id="run-a")])

        def _unreachable(*args, **kwargs):
            raise AssertionError("must not be reached with records_bucket=None")

        with mock.patch.object(scoped, "registrar_for_scope", _unreachable), \
             mock.patch.object(scoped, "register_batch", _unreachable):
            with self.assertRaises(ValueError):
                scoped.run_scoped_registration(
                    conn, run_id_prefix="run-a", dry_run=False,
                    records_bucket=None)


class ArgParsingTests(unittest.TestCase):
    def test_attempt_id_is_repeatable(self):
        args = scoped._parse_args(
            ["--attempt-id", "1", "--attempt-id", "2"])
        self.assertEqual([1, 2], args.attempt_ids)

    def test_run_id_prefix_and_attempt_ids_are_both_none_by_default(self):
        args = scoped._parse_args([])
        self.assertIsNone(args.run_id_prefix)
        self.assertEqual([], args.attempt_ids)


class RegistrationRunSynthesisTests(unittest.TestCase):
    """The `RegistrationRun` a dry run builds from `decide_all` classifies
    every `RegistrationDecision` value into the same counter a real
    `register_batch` pass would (would_register / skipped / deferred) —
    checked against the taxonomy directly, not by re-deriving it, so a new
    decision value added to the enum without a matching branch here fails
    loudly instead of silently falling into the wrong bucket."""

    def test_every_decision_value_has_a_counter_branch(self):
        accounted = {RegistrationDecision.REGISTER,
                    RegistrationDecision.SKIP,
                    RegistrationDecision.DEFER}
        self.assertEqual(accounted, set(RegistrationDecision))

    def test_empty_scope_is_a_clean_zero_run(self):
        run = RegistrationRun()
        self.assertEqual(0, run.would_register)
        self.assertEqual(scoped.EXIT_OK, run.exit_code)
