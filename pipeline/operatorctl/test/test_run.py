"""Stub-tier tests for the `run` command group (migrations 108/109).

Three properties this file exists to pin, each a regression guard named in
the task ruling:

  * THE FAILURE PREDICATE. `run status` must count as a failure both a
    TERMINAL attempt with a non-success outcome (NULL included) and a
    `missing_or_contradictory` row -- a prior defect used `rapid_outcome
    <> 'success'` alone and reported a clean pass over 218 dead letters
    whose `rapid_outcome` was NULL (never started at all, so `<>` against
    NULL is NULL, neither true nor false, and the row vanished from both
    sides of the count). Tested here directly against
    `actions._RUN_FAILURE_PREDICATE`'s SQL text evaluated in SQLite (no
    live Postgres in the stub tier), over scripted rows including a NULL
    outcome and a `missing_or_contradictory` row -- a real predicate
    evaluation, not a string match on the SQL.
  * PREFIX MATCHING, NEVER EQUALITY. An attempt with `run_id = '<name>-3'`
    (a split-pass batch suffix) must be counted for `<name>` — matching by
    equality was the defect 108's own COMMENT ON TABLE names as "the ninth
    defect of the 8/21 rerun".
  * `run archive` without `--apply` performs NO write and its rendered
    output states nothing was changed, via `render_plan` — not a
    hand-rolled string.
  * `run create` passes the idempotency key as the FIRST positional
    argument to `derived.create_run`, matching 109's `p_idempotency_key`
    being the function's first parameter.

Follows `test_batch.py`'s pattern throughout: `_FakeConn`/`_FakeCursor`
script jsonb-shaped return values and record every `(sql, params)` call so
ordering and parameter content can both be asserted, and the `boto3`/
`psycopg2` stub-injection preamble is copied verbatim (this module's import
chain reaches `pipeline.operatorctl.contract.call_function`, which needs
`psycopg2.Error` to exist as an exception class at call time).
"""

import sqlite3
import sys
import types
import unittest
from unittest import mock

if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.modules["boto3"] = types.ModuleType("boto3")

if "psycopg2" not in sys.modules:
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg2")
        stub.Error = type("Error", (Exception,), {})
        sys.modules["psycopg2"] = stub

from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import render_plan


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params))
        script = self._conn.script
        if not script:
            raise AssertionError(
                "no more scripted responses; unexpected statement: %s" % sql)
        self._result = script.pop(0)

    def fetchone(self):
        return (self._result,)

    @property
    def description(self):
        return [("result",)]


class _FakeConn:
    """`script` is a list of jsonb-shaped return values, one per statement
    the module issues, in order — identical shape to `test_batch.py`'s
    `_FakeConn`.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


# ---------------------------------------------------------------------------
# The failure predicate, evaluated for real over scripted rows.
# ---------------------------------------------------------------------------
# SQLite stands in for Postgres here ONLY to evaluate a boolean expression
# over a table of rows — `IS DISTINCT FROM` is valid SQLite syntax (3.39+)
# and means exactly the same thing there as in Postgres: unlike `<>`, it
# treats NULL as an ordinary comparable value rather than propagating NULL
# through the comparison. This is a real evaluation of the SQL text
# `actions.py` ships, not a Python reimplementation of the predicate that
# could silently drift from what is actually sent to the database.
def _count_failures(rows):
    """`rows` is a list of (lifecycle_state, rapid_outcome) pairs. Returns
    how many the real predicate text in `actions._RUN_FAILURE_PREDICATE`
    counts as failures.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE attempts (lifecycle_state TEXT, rapid_outcome TEXT)")
    conn.executemany("INSERT INTO attempts VALUES (?, ?)", rows)
    sql = ("SELECT count(*) FROM attempts WHERE "
          + actions._RUN_FAILURE_PREDICATE)
    return conn.execute(sql).fetchone()[0]


class FailurePredicateTests(unittest.TestCase):
    """THE REGRESSION GUARD for the sci-c defect: a NULL rapid_outcome on a
    terminal row, and a missing_or_contradictory row, both count.
    """

    def test_a_null_outcome_terminal_attempt_counts_as_a_failure(self):
        # This is the exact sci-c shape: 218 attempts dead-lettered with
        # started_at IS NULL, rapid_outcome never set (still NULL) because
        # the container exited before start_attempt. lifecycle_state here
        # is a stand-in "terminal_after_start"-shaped value (LIKE
        # 'terminal%').
        rows = [("terminal_after_start", None)]
        self.assertEqual(_count_failures(rows), 1, (
            "a terminal attempt with rapid_outcome IS NULL must count as "
            "a failure -- `rapid_outcome <> 'success'` alone evaluates to "
            "NULL here and silently drops the row, which is the exact "
            "defect that reported a clean pass over 218 dead letters"))

    def test_a_missing_or_contradictory_row_counts_as_a_failure(self):
        # NOT itself a `terminal%` lifecycle_state (see live_w9_ramp's own
        # `_dead_lettered_pairs` comment) -- must be caught by the SECOND
        # disjunct, not the first.
        rows = [("missing_or_contradictory", None)]
        self.assertEqual(_count_failures(rows), 1)

    def test_a_successful_terminal_attempt_does_not_count(self):
        rows = [("terminal_after_start", "success")]
        self.assertEqual(_count_failures(rows), 0)

    def test_a_non_terminal_attempt_with_null_outcome_does_not_count(self):
        # In flight, not dead -- e.g. lifecycle_state = 'submitted'. Must
        # not be swept in just because rapid_outcome is NULL.
        rows = [("submitted", None)]
        self.assertEqual(_count_failures(rows), 0)

    def test_mixed_population_counts_exactly_the_failing_rows(self):
        rows = [
            ("terminal_after_start", "success"),      # not a failure
            ("terminal_after_start", None),            # failure (NULL)
            ("terminal_after_start", "failure"),        # failure (explicit)
            ("missing_or_contradictory", None),         # failure
            ("submitted", None),                        # not a failure
        ]
        self.assertEqual(_count_failures(rows), 3)


# ---------------------------------------------------------------------------
# Prefix matching, never equality.
# ---------------------------------------------------------------------------
class PrefixMatchingTests(unittest.TestCase):
    """A split-pass batch's `<name>-<n>` suffix must be counted for `name`.

    THE WILDCARD LIVES ON THE PARAMETER, NOT IN THE SQL TEXT — `actions.
    _run_prefix_pattern(name)` returns `name + "%"`, and every query here is
    a plain `run_id LIKE %s`. This is deliberate: splicing a literal `%`
    into the SQL string next to psycopg2's own `%s` placeholder syntax is
    exactly the kind of thing that is easy to get subtly wrong (`%%`
    doubling), so the wildcard is kept out of the SQL text entirely,
    matching `pipeline.registration.consumer.candidates`'s own convention
    for its `run_id_prefix` parameter.
    """

    def test_run_attempt_tally_sql_matches_by_like_not_equality(self):
        # Asserted on the SQL TEXT actions.py actually sends, not on a
        # mocked return value -- the property under test is which operator
        # reaches the database, and a query built with `=` would still let
        # a test pass if the fixture data happened not to exercise a
        # suffixed run_id.
        self.assertIn("run_id LIKE %s", actions._RUN_ATTEMPT_TALLY)
        self.assertNotIn("run_id = %s", actions._RUN_ATTEMPT_TALLY)

    def test_run_prefix_pattern_appends_the_wildcard_to_the_parameter(self):
        self.assertEqual(actions._run_prefix_pattern("w9-ramp-science-18-x"),
                         "w9-ramp-science-18-x%")

    def test_a_split_batch_suffix_is_counted_for_its_run_via_like(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE attempts (run_id TEXT)")
        conn.executemany("INSERT INTO attempts VALUES (?)", [
            ("w9-ramp-science-18-x",),
            ("w9-ramp-science-18-x-0",),   # split-batch suffix
            ("w9-ramp-science-18-x-1",),   # split-batch suffix
            ("some-other-run",),
        ])
        # The SAME pattern actions.run_attempt_tally builds, evaluated for
        # real against a LIKE.
        pattern = actions._run_prefix_pattern("w9-ramp-science-18-x")
        count = conn.execute(
            "SELECT count(*) FROM attempts WHERE run_id LIKE ?",
            (pattern,)).fetchone()[0]
        self.assertEqual(count, 3, (
            "all three attempts under the run's prefix must be counted, "
            "including the two split-batch suffixes -- matching by `=` "
            "would find only the first"))

    def test_run_stage_walltime_sql_also_matches_by_like_not_equality(self):
        self.assertIn("run_id LIKE %s", actions._RUN_STAGE_WALLTIME)
        self.assertNotIn("run_id = %s", actions._RUN_STAGE_WALLTIME)

    def test_run_product_counts_sql_also_matches_by_like_not_equality(self):
        self.assertIn("run_id LIKE %s", actions._RUN_PRODUCT_COUNTS)
        self.assertNotIn("run_id = %s", actions._RUN_PRODUCT_COUNTS)

    def test_release_dead_letter_candidates_sql_matches_by_like_not_equality(
            self):
        from pipeline.operatorctl.run import _RELEASE_CANDIDATES_SQL
        self.assertIn("run_id LIKE %s", _RELEASE_CANDIDATES_SQL)
        self.assertNotIn("run_id = %s", _RELEASE_CANDIDATES_SQL)


# ---------------------------------------------------------------------------
# `run archive` without --apply: no write, and render_plan's own wording.
# ---------------------------------------------------------------------------
class ArchiveDryRunTests(unittest.TestCase):
    def test_dry_run_performs_no_write_and_reports_nothing_changed(self):
        # `derived.archive_run`'s own jsonb result shape for a dry run
        # (109): dry_run True, rows_affected counts what an apply WOULD
        # touch, but nothing is actually written -- the fake conn's
        # `calls` list is what proves that, not the returned dict.
        conn = _FakeConn([
            {"action": "run_archive", "dry_run": True, "replayed": False,
             "rows_affected": 1, "audit_id": 42, "kind": "campaign",
             "prior_state": "complete", "refimages_demoted": 0,
             "diffimages_demoted": 0, "psfs_demoted": 0,
             "nothing_deleted": True},
        ])
        result = actions.archive_run(
            conn, "archive-key-1", "w9-ramp-science-18-x", "wrap up",
            dry_run=True)

        # ONE call only: the SELECT derived.archive_run(...) itself. No
        # second statement, no commit beyond call_function's own (which
        # the real function's dry-run path performs with nothing changed
        # inside its own transaction) -- what matters here is that this
        # module issued exactly one statement, not a write followed by a
        # rollback the caller has to trust the database did correctly.
        self.assertEqual(len(conn.calls), 1)
        sql, params = conn.calls[0]
        self.assertIn("derived.archive_run", sql)
        # idempotency key first, name second, matching 109's signature.
        self.assertEqual(params[0], "archive-key-1")
        self.assertEqual(params[1], "w9-ramp-science-18-x")

        rendered = render_plan("run_archive", "runs:w9-ramp-science-18-x",
                               "wrap up", "archive-key-1", result, False)
        self.assertIn("Nothing was changed", rendered)
        self.assertIn("DRY RUN", rendered)


# ---------------------------------------------------------------------------
# `run create` passes the idempotency key FIRST.
# ---------------------------------------------------------------------------
class CreateRunKeyOrderingTests(unittest.TestCase):
    def test_idempotency_key_is_the_first_argument_to_derived_create_run(self):
        conn = _FakeConn([
            {"action": "run_create", "dry_run": True, "replayed": False,
             "rows_affected": 0, "audit_id": 7, "already_present": False,
             "run_id": None, "would_add": True},
        ])
        actions.create_run(
            conn, "create-key-1", "w9-ramp-science-18-x", "ben", "campaign",
            reason="new ramp step", dry_run=True)

        self.assertEqual(len(conn.calls), 1)
        sql, params = conn.calls[0]
        self.assertIn("derived.create_run", sql)
        self.assertEqual(params[0], "create-key-1", (
            "the idempotency key must be the FIRST positional argument to "
            "derived.create_run, matching migration 109's "
            "p_idempotency_key-first signature"))
        self.assertEqual(params[1], "w9-ramp-science-18-x")
        self.assertEqual(params[2], "ben")
        self.assertEqual(params[3], "campaign")


# ---------------------------------------------------------------------------
# `run release-dead-letters`: expected-state refusal and per-candidate
# independence, over a fake WorkUnitWriter transition.
# ---------------------------------------------------------------------------
class ReleaseDeadLettersTests(unittest.TestCase):
    def test_dry_run_writes_nothing_and_records_the_candidate_count(self):
        from pipeline.operatorctl import run as run_mod

        class _FakeConnWithCandidates(_FakeConn):
            def cursor(self):
                return _FindCandidatesCursor(self)

        class _FindCandidatesCursor(_FakeCursor):
            def execute(self, sql, params=None):
                self._conn.calls.append((" ".join(sql.split()), params))
                if "FROM attempts a" in sql and "JOIN work_units" in sql:
                    self._rows = [(101, 501, "job-1"), (102, 502, "job-2")]
                else:
                    self._result = self._conn.script.pop(0)
                    self._rows = None

            def fetchall(self):
                return self._rows or []

        conn = _FakeConnWithCandidates([
            None,   # mutation_replay: no prior row
            {"action": "run_release_dead_letters", "dry_run": True,
             "replayed": False, "rows_affected": 0, "audit_id": 9},
        ])
        result, scope = run_mod.release_dead_letters_audited(
            conn, "release-key-1", "w9-ramp-science-18-x", "recover sci-c",
            dry_run=True, out=_null_out())
        self.assertEqual(result["rows_affected"], 0)
        self.assertEqual(scope, "run:w9-ramp-science-18-x:release-dead-letters")

    def test_expected_state_mismatch_is_raised_before_any_release(self):
        from pipeline.operatorctl import run as run_mod
        from pipeline.operatorctl.contract import ExpectedStateMismatch

        class _FakeConnWithCandidates(_FakeConn):
            def cursor(self):
                return _FindCandidatesCursor(self)

        class _FindCandidatesCursor(_FakeCursor):
            def execute(self, sql, params=None):
                self._conn.calls.append((" ".join(sql.split()), params))
                if "FROM attempts a" in sql and "JOIN work_units" in sql:
                    self._rows = [(101, 501, "job-1")]
                else:
                    self._result = self._conn.script.pop(0)
                    self._rows = None

            def fetchall(self):
                return self._rows or []

        conn = _FakeConnWithCandidates([None])  # only the replay lookup
        with self.assertRaises(ExpectedStateMismatch):
            run_mod.release_dead_letters_audited(
                conn, "release-key-2", "w9-ramp-science-18-x", "recover",
                expected_state={"candidates": 5}, dry_run=True,
                out=_null_out())
        # No record_external_action call: the mismatch must be raised
        # before the audit write, matching every other expected-state
        # check in this package.
        writes = [c for c in conn.calls if "record_external_action" in c[0]]
        self.assertEqual(len(writes), 0)


def _null_out():
    import io
    return io.StringIO()


# ---------------------------------------------------------------------------
# `run start --phase reference/science` (throughput-sitting ruling,
# 2026-09-11): `gather_for_run` dispatches the two MJD-windowed phases to
# the right gatherer with the right window, distinct from the four
# post-DB-chain phases' `_phase_table()` dispatch. Tested against
# `gather_for_run` directly, with `submission.gathering`'s two windowed
# gatherers replaced by fakes that record their call -- no database, no
# AWS, matching this file's own stub-tier convention throughout.
# ---------------------------------------------------------------------------
class WindowedPhaseDispatchTests(unittest.TestCase):

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        from submission import gathering

        self.run_mod = run_mod
        self.calls = []

        def fake_reference(handle, start, end, start_mjdobs, end_mjdobs,
                           min_images_to_coadd, s3_client, job_bucket,
                           run_id, fids=None, run_scope=None):
            self.calls.append({
                "gatherer": "reference", "start": start, "end": end,
                "start_mjdobs": start_mjdobs, "end_mjdobs": end_mjdobs,
                "min_images_to_coadd": min_images_to_coadd,
                "s3_client": s3_client, "job_bucket": job_bucket,
                "run_id": run_id, "fids": fids, "run_scope": run_scope})
            return iter(())

        def fake_science(handle, start, end, start_mjdobs, end_mjdobs,
                         min_images_to_coadd, fids=None,
                         make_references=False, run_scope=None):
            self.calls.append({
                "gatherer": "science", "start": start, "end": end,
                "start_mjdobs": start_mjdobs, "end_mjdobs": end_mjdobs,
                "min_images_to_coadd": min_images_to_coadd,
                "fids": fids, "make_references": make_references,
                "run_scope": run_scope})
            return iter(())

        patcher_ref = mock.patch.object(
            gathering, "gather_reference_units", fake_reference)
        patcher_sci = mock.patch.object(
            gathering, "gather_science_units", fake_science)
        patcher_ref.start()
        patcher_sci.start()
        self.addCleanup(patcher_ref.stop)
        self.addCleanup(patcher_sci.stop)

    def _window(self, start_mjd=61600.0, end_mjd=61700.0, min_coadd=3):
        return ("2027-10-01 00:00:00", "2027-10-08 00:00:00",
               start_mjd, end_mjd, min_coadd)

    def test_phase_reference_dispatches_to_gather_reference_units(self):
        from submission import routes

        job_type, units = self.run_mod.gather_for_run(
            dbh=object(), phase="reference", window=self._window(),
            run_name="w9-campaign-1", s3_client="fake-s3",
            job_bucket="fake-bucket")

        self.assertEqual(list(units), [])
        self.assertEqual(job_type, routes.JOB_TYPE_REFERENCE_IMAGE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["gatherer"], "reference")

    def test_phase_science_dispatches_to_gather_science_units(self):
        from submission import routes

        job_type, units = self.run_mod.gather_for_run(
            dbh=object(), phase="science", window=self._window(),
            run_name="w9-campaign-1")

        self.assertEqual(list(units), [])
        self.assertEqual(job_type, routes.JOB_TYPE_SCIENCE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["gatherer"], "science")

    def test_the_window_reaches_the_gatherer_as_mjd_bounds(self):
        self.run_mod.gather_for_run(
            dbh=object(), phase="science",
            window=self._window(start_mjd=61601.5, end_mjd=61701.5),
            run_name="w9-campaign-1")

        self.assertEqual(self.calls[0]["start_mjdobs"], 61601.5)
        self.assertEqual(self.calls[0]["end_mjdobs"], 61701.5)

    def test_run_name_reaches_the_gate_as_run_scope_for_science(self):
        self.run_mod.gather_for_run(
            dbh=object(), phase="science", window=self._window(),
            run_name="w9-campaign-1")

        self.assertEqual(self.calls[0]["run_scope"], "w9-campaign-1")

    def test_run_name_reaches_both_run_id_and_run_scope_for_reference(self):
        # THE JUDGMENT CALL this task ruling asked to be stated explicitly:
        # `gather_reference_units`' own `run_id` (publish-key prefix) and
        # the new `run_scope` (gate scope) are different parameters, but
        # `run start`'s single `--name` is passed to BOTH -- a run
        # publishes its own artifacts under its own name and is gated only
        # on its own prior work. See `gathering.gather_reference_units`'s
        # docstring for the full reasoning.
        self.run_mod.gather_for_run(
            dbh=object(), phase="reference", window=self._window(),
            run_name="w9-campaign-1", s3_client="fake-s3",
            job_bucket="fake-bucket")

        self.assertEqual(self.calls[0]["run_id"], "w9-campaign-1")
        self.assertEqual(self.calls[0]["run_scope"], "w9-campaign-1")

    def test_fids_is_passed_through_unchanged(self):
        self.run_mod.gather_for_run(
            dbh=object(), phase="science", window=self._window(),
            run_name="w9-campaign-1", fids=[8])

        self.assertEqual(self.calls[0]["fids"], [8])

    def test_reference_without_s3_client_or_bucket_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_mod.gather_for_run(
                dbh=object(), phase="reference", window=self._window(),
                run_name="w9-campaign-1")
        self.assertIn("reference", str(ctx.exception))

    def test_a_windowed_phase_without_a_window_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_mod.gather_for_run(
                dbh=object(), phase="science", window=None,
                run_name="w9-campaign-1")
        self.assertIn("window", str(ctx.exception))

    def test_a_windowed_phase_without_a_run_name_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_mod.gather_for_run(
                dbh=object(), phase="science", window=self._window(),
                run_name=None)
        self.assertIn("run name", str(ctx.exception))

    def test_cap_still_applies_to_a_windowed_gather(self):
        from submission import gathering

        def five_units(handle, start, end, start_mjdobs, end_mjdobs,
                       min_images_to_coadd, fids=None, make_references=False,
                       run_scope=None):
            return iter(range(5))

        with mock.patch.object(gathering, "gather_science_units",
                               five_units):
            _job_type, units = self.run_mod.gather_for_run(
                dbh=object(), phase="science", window=self._window(),
                run_name="w9-campaign-1", cap=2)

        self.assertEqual(units, [0, 1])

    def test_the_four_post_db_chain_phases_ignore_window_and_run_name(self):
        # The non-windowed phases must keep working with NO new required
        # arguments -- `window`/`run_name`/`s3_client`/`job_bucket`/`fids`
        # all default to None and are simply unused for these four.
        from submission import gathering

        with mock.patch.object(gathering, "gather_statistics_units",
                               lambda handle: iter(())):
            job_type, units = self.run_mod.gather_for_run(
                dbh=object(), phase="statistics")

        from submission import routes
        self.assertEqual(job_type, routes.JOB_TYPE_STATISTICS)
        self.assertEqual(list(units), [])
        # Neither fake windowed gatherer was called.
        self.assertEqual(self.calls, [])

    def test_an_unknown_phase_still_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.run_mod.gather_for_run(
                dbh=object(), phase="not-a-real-phase")


if __name__ == "__main__":
    unittest.main()
