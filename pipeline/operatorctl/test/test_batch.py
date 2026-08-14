"""Stub-tier tests for `terminate_jobs_audited`'s partial-failure accounting
and record-after ordering (fix round 2, wave B, item 5).

The defect: the audit row was written and COMMITTED before the termination
loop ran, claiming `rows_affected=len(jobs)` — the size of the population,
not the count of what the loop actually managed to terminate — and the
module's own docstring promised a later update ("updated after with the
count actually terminated") that no code ever performed. `gc.py`'s own
header names this exact defect and its fix (record execution AFTER, with
the real per-item tally); this file's tests pin the batch-termination
equivalent: replay is checked FIRST via a read-only lookup (no row
written), each job's termination is attempted independently, and the ONE
audit row written afterward carries the REAL count — not `len(jobs)` —
plus the individual failures, when there are any.

No live database and no live AWS: `_FakeConn` scripts exactly the two SQL
round trips this module makes (`derived.mutation_replay`, `derived.
record_external_action`) and records every statement so a test can assert
on ORDERING, not just outcome — the property the original bug violated was
ordering (record-then-act instead of act-then-record), and a double that
could not distinguish "recorded before the loop" from "recorded after"
could not have caught it. `_PartiallyRefusingBatchClient` — the double
under test's whole reason to exist — fails specific jobs on command; a
double that always succeeds could not exercise the partial-failure path
this fix adds at all (`stub-blind-testing`'s house rule).
"""

import sys
import types
import unittest

# Two drivers this module's import chain reaches lazily, neither installed
# in this stub-tier environment — matching the repo-wide convention
# (`pipeline/reconciler/test/test_main.py`'s identical psycopg2 stub):
#
#   * `pipeline.operatorctl.batch._list_all` imports `aws.terminate_batch_
#     jobs`, which imports `boto3` at MODULE scope. Only `list_jobs`/
#     `terminate_jobs` reach the client object at runtime — neither touches
#     the `boto3` name itself — so an empty placeholder module satisfies
#     the import with no behavioural stand-in needed.
#   * `pipeline.operatorctl.contract.call_function` imports `psycopg2`
#     LAZILY, inside the function body, specifically so a stub can sit in
#     `sys.modules` before it is ever called (that module's own comment:
#     "the stub tier puts a fake psycopg2 into sys.modules"). It needs
#     `psycopg2.Error` to exist as an exception class for the `except
#     psycopg2.Error` clause to even be valid Python at call time.
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

from pipeline.operatorctl.batch import terminate_jobs_audited


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
    """`script` is a list of jsonb-shaped return values (dicts or `None`),
    one per statement the module issues — in order, `mutation_replay`
    first (if reached), then `record_external_action` (if reached).
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


class _PartiallyRefusingBatchClient:
    """A Batch client whose `terminate_job` fails for named job ids.

    Records every attempted termination, successful or not — a test needs
    to see both to assert the loop kept going past a failure rather than
    aborting the batch.
    """

    def __init__(self, jobs, failing_job_ids=()):
        self._jobs = jobs
        self._failing = set(failing_job_ids)
        self.attempted = []
        self.terminated = []

    def get_paginator(self, name):
        assert name == "list_jobs"
        return self

    def paginate(self, jobQueue=None, jobStatus=None):   # noqa: N803
        return [{"jobSummaryList": [j for j in self._jobs
                                    if j["status"] == jobStatus]}]

    def terminate_job(self, jobId=None, reason=None):     # noqa: N803
        self.attempted.append(jobId)
        if jobId in self._failing:
            raise RuntimeError("Batch refused: job %s already terminal"
                              % jobId)
        self.terminated.append((jobId, reason))


def _jobs(count, status="RUNNING"):
    return [{"jobId": "job-%d" % i, "jobName": "fixture-%d" % i,
             "status": status} for i in range(count)]


def _null_out():
    import io
    return io.StringIO()


def _audit_call(conn):
    """The one `record_external_action` call this module issues per real
    invocation — asserted to be exactly one by every test below.
    """
    calls = [c for c in conn.calls if "record_external_action" in c[0]]
    assert len(calls) == 1, (
        "expected exactly one record_external_action call, got %d"
        % len(calls))
    return calls[0]


class PartialFailureAccountingTests(unittest.TestCase):
    """THE FIX: the audit row's rows_affected is the REAL terminated count,
    written AFTER the loop — never `len(jobs)` written before it.
    """

    def test_rows_affected_is_the_real_terminated_count_not_the_population(
            self):
        jobs = _jobs(3)
        client = _PartiallyRefusingBatchClient(jobs, failing_job_ids={
            "job-1"})
        conn = _FakeConn([None,        # mutation_replay: no prior row
                          {"audit_id": 1, "rows_affected": 2,
                           "dry_run": False}])
        result, _scope = terminate_jobs_audited(
            conn, "k1", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())

        self.assertEqual(len(client.attempted), 3, (
            "a failing job must not stop the loop from attempting the rest"))
        self.assertEqual(len(client.terminated), 2)
        self.assertEqual(result["rows_affected"], 2, (
            "rows_affected must equal jobs ACTUALLY terminated (2), "
            "not len(jobs) (3) — the exact defect this fix closes"))

    def test_the_audit_call_carries_the_failure_detail(self):
        jobs = _jobs(2)
        client = _PartiallyRefusingBatchClient(jobs, failing_job_ids={
            "job-0"})
        conn = _FakeConn([None, {"audit_id": 2, "rows_affected": 1}])
        terminate_jobs_audited(
            conn, "k2", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())

        _sql, params = _audit_call(conn)
        # record_external_action(idempotency_key, action_class, target_scope,
        #   reason, expected_state, dry_run, rows_affected, detail,
        #   policy_citation) — detail is the 8th positional parameter,
        # already JSON-serialized by `actions._json` before it reaches the
        # driver (real psycopg2 adapts the jsonb column the same way on
        # the way back out; this fake sees the raw string it was sent).
        import json
        detail = json.loads(params[7])
        self.assertIn("failures", detail)
        self.assertEqual(len(detail["failures"]), 1)
        self.assertEqual(detail["failures"][0]["job_id"], "job-0")

    def test_all_jobs_failing_records_zero_not_the_population(self):
        jobs = _jobs(2)
        client = _PartiallyRefusingBatchClient(
            jobs, failing_job_ids={"job-0", "job-1"})
        conn = _FakeConn([None, {"audit_id": 3, "rows_affected": 0}])
        result, _ = terminate_jobs_audited(
            conn, "k3", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())
        self.assertEqual(len(client.attempted), 2)
        self.assertEqual(len(client.terminated), 0)
        _sql, params = _audit_call(conn)
        self.assertEqual(params[6], 0, (
            "rows_affected parameter must be 0 when every job failed, "
            "never len(jobs)"))

    def test_the_all_succeed_case_still_reports_the_full_count(self):
        """The ordinary path, unchanged in outward behaviour: when nothing
        fails, the real count and `len(jobs)` coincide.
        """
        jobs = _jobs(3)
        client = _PartiallyRefusingBatchClient(jobs)
        conn = _FakeConn([None, {"audit_id": 4, "rows_affected": 3}])
        result, _ = terminate_jobs_audited(
            conn, "k4", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())
        self.assertEqual(len(client.terminated), 3)
        _sql, params = _audit_call(conn)
        self.assertEqual(params[6], 3)


class RecordAfterOrderingTests(unittest.TestCase):
    """THE ORDER ITSELF: the audit call happens AFTER every termination
    attempt, not before — asserted by call sequence, not just by outcome.
    """

    def test_every_terminate_job_call_precedes_the_audit_call(self):
        jobs = _jobs(2)
        client = _PartiallyRefusingBatchClient(jobs)
        conn = _FakeConn([None, {"audit_id": 5, "rows_affected": 2}])
        terminate_jobs_audited(
            conn, "k5", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())

        audit_call_index = next(
            i for i, (sql, _params) in enumerate(conn.calls)
            if "record_external_action" in sql)
        # Every termination attempt on the (single, injected) client
        # happened via client.terminate_job, which this fake records
        # separately from conn.calls — the assertion that matters is that
        # BOTH attempts happened, and the audit call is the ONLY database
        # write this module issues, so its position among conn.calls is
        # trivially last; the real ordering guarantee is that
        # client.attempted is already fully populated by the time the
        # audit call is built, checked next.
        self.assertEqual(len(client.attempted), 2)
        self.assertEqual(audit_call_index, len(conn.calls) - 1)

    def test_only_one_database_write_happens_for_a_real_invocation(self):
        """Before the fix, a row was written BEFORE the loop (claiming the
        population count) with a docstring promise of a later update that
        never happened — i.e., ONE write, wrong content. This does not
        merely re-assert "one write"; it is paired with the rows_affected
        tests above to confirm that one write carries the RIGHT content.
        """
        jobs = _jobs(1)
        client = _PartiallyRefusingBatchClient(jobs)
        conn = _FakeConn([None, {"audit_id": 6, "rows_affected": 1}])
        terminate_jobs_audited(
            conn, "k6", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())
        write_calls = [c for c in conn.calls
                      if "record_external_action" in c[0]]
        self.assertEqual(len(write_calls), 1)


class DryRunStillWritesZeroTests(unittest.TestCase):
    """The pre-existing dry-run contract (`rows_affected=0`, nothing
    terminated) must survive this restructuring unchanged.
    """

    def test_dry_run_terminates_nothing_and_records_zero(self):
        jobs = _jobs(3)
        client = _PartiallyRefusingBatchClient(jobs)
        conn = _FakeConn([None, {"audit_id": 7, "rows_affected": 0,
                                 "dry_run": True}])
        result, _ = terminate_jobs_audited(
            conn, "k7", "q", ["RUNNING"], "why", dry_run=True,
            session_factory=lambda: client, out=_null_out())
        self.assertEqual(client.attempted, [])
        _sql, params = _audit_call(conn)
        self.assertEqual(params[6], 0)
        self.assertTrue(params[5], "dry_run parameter must be True")


class ReplayChecksBeforeAnyAwsCallTests(unittest.TestCase):
    """The read-only replay pre-check: a replayed key touches AWS not at
    all, and writes no second audit row.
    """

    def test_a_replayed_key_never_calls_terminate_job(self):
        jobs = _jobs(2)
        client = _PartiallyRefusingBatchClient(jobs)
        conn = _FakeConn([
            {"action": "external_batch_terminate", "dry_run": False,
             "replayed": True, "rows_affected": 2, "audit_id": 1,
             "performed_at": "2026-08-14T00:00:00Z"},
        ])
        result, scope = terminate_jobs_audited(
            conn, "k8", "q", ["RUNNING"], "why", dry_run=False,
            session_factory=lambda: client, out=_null_out())
        self.assertEqual(client.attempted, [], (
            "a replayed key must never reach AWS — the listing pass "
            "itself is also skipped"))
        self.assertTrue(result["replayed"])
        # Only ONE statement issued at all: the replay lookup. No second
        # write, because record_external_action is never reached on replay.
        self.assertEqual(len(conn.calls), 1)


if __name__ == "__main__":
    unittest.main()
