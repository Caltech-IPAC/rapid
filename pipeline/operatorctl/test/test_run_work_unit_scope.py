"""Stub-tier tests for the release-scope ruling (2026-09-11): `submit_run`
and `start_run_audited` gain an optional `work_unit_run_id` parameter so a
run can RESUBMIT another run's released work units under its own,
separately-queryable `run_id` — the mechanism `pipeline.seams.
submit_gathered` already exposes (see its docstring on `work_unit_run_id`)
but that `pipeline.operatorctl.run.submit_run` could not reach, since it
hardcoded `work_unit_run_id=name` for every caller.

New file, not appended to `test_run.py`, per the task split with the
concurrent agent editing that file and `main.py`. Follows `test_run.py`'s
own patterns throughout: the `boto3`/`psycopg2` stub-injection preamble
(needed because this module's import chain reaches
`pipeline.operatorctl.contract.call_function`, which needs `psycopg2.Error`
to exist as an exception class at call time), `SubmitRunSubmissionRoleTests`'
way of patching `pipeline.seams.submit_gathered` with a spy that records
the kwargs it was called with, and `types.SimpleNamespace(commit=...)` as
the minimal `conn` double `submit_run` needs.

Four required cases:
  * default (`work_unit_run_id` not given): `submit_gathered` receives
    `run_id=name` AND `work_unit_run_id=name` — the unchanged behaviour.
  * explicit: `submit_gathered` receives `run_id=name` and
    `work_unit_run_id=<the given value>`, the two differing.
  * `start_run_audited` threads the parameter through to `submit_run`.
  * the audit detail names the work-unit scope when it differs from
    `name`, and does not invent one when it does not.
"""

import contextlib
import io
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


def _null_out():
    return io.StringIO()


class SubmitRunWorkUnitScopeTests(unittest.TestCase):
    """Direct tests of `submit_run`'s new `work_unit_run_id` parameter,
    at the same level `SubmitRunSubmissionRoleTests` in `test_run.py`
    exercises it: `submission_role` and `pipeline.seams.submit_gathered`
    are both replaced by spies, so the assertion lands on exactly the
    kwargs `submit_run` passes down, with no database or AWS involved.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

        @contextlib.contextmanager
        def fake_submission_role(conn):
            yield conn

        role_patcher = mock.patch.object(
            run_mod, "submission_role", fake_submission_role)
        role_patcher.start()
        self.addCleanup(role_patcher.stop)

        self.submit_calls = []

        def fake_submit_gathered(units, **kwargs):
            self.submit_calls.append(kwargs)
            return [("submission-1", ["attempt-1"])]

        import pipeline.seams as seams_mod
        seams_patcher = mock.patch.object(
            seams_mod, "submit_gathered", fake_submit_gathered)
        seams_patcher.start()
        self.addCleanup(seams_patcher.stop)

        self.conn = types.SimpleNamespace(commit=lambda: None)
        self.context = {
            "queue": "q", "job_definition": "jd", "binding": "b",
            "manifest_bucket": "mb", "manifest_prefix": "mp",
            "s3_client": "s3", "batch_client": "batch"}

    def test_default_scopes_work_units_to_the_run_s_own_name(self):
        # Unchanged behaviour: omitting `work_unit_run_id` must still
        # scope the submission's work units to `name` itself, exactly as
        # before this parameter existed.
        self.run_mod.submit_run(
            self.conn, "accept-20260911", "job-type-x", ["unit-a"],
            "reason", context=self.context)

        self.assertEqual(len(self.submit_calls), 1)
        kwargs = self.submit_calls[0]
        self.assertEqual(kwargs["run_id"], "accept-20260911")
        self.assertEqual(kwargs["work_unit_run_id"], "accept-20260911")

    def test_explicit_scope_claims_a_different_run_s_units(self):
        # The release case this parameter exists for: a NEW run name
        # authors the submission, but the work units it must find and
        # claim carry the PRIOR run's name.
        self.run_mod.submit_run(
            self.conn, "accept-20260911-release", "job-type-x",
            ["unit-a"], "reason", context=self.context,
            work_unit_run_id="accept-20260911")

        self.assertEqual(len(self.submit_calls), 1)
        kwargs = self.submit_calls[0]
        self.assertEqual(kwargs["run_id"], "accept-20260911-release")
        self.assertEqual(kwargs["work_unit_run_id"], "accept-20260911")
        self.assertNotEqual(kwargs["run_id"], kwargs["work_unit_run_id"])

    def test_empty_units_short_circuits_regardless_of_scope(self):
        # `submit_run` returns `[]` for empty `units` without calling
        # `seams.submit_gathered` at all -- unaffected by whether a scope
        # override was given.
        results = self.run_mod.submit_run(
            self.conn, "accept-20260911-release", "job-type-x", [],
            "reason", context=self.context,
            work_unit_run_id="accept-20260911")

        self.assertEqual(results, [])
        self.assertEqual(self.submit_calls, [])


class StartRunAuditedWorkUnitScopeTests(unittest.TestCase):
    """`start_run_audited` threads its own new `work_unit_run_id`
    parameter through to `submit_run`, and records it in the audit
    `detail` only when it differs from the run's own `name`.

    `_replay_lookup`, `gather_for_run`, `submit_run` and
    `record_external_action` are all patched on the `run_mod` module —
    the level `StartRunAuditedOrderingTests` in `test_run.py` already
    treats as the right seam for this function's collaborators — so this
    drives the real, unmodified `start_run_audited` body on the
    non-windowed `statistics` phase (no window/context resolution needed)
    without a database or AWS.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run",
            lambda *a, **k: ("job-type-x", ["unit-a"]))
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        self.submit_calls = []

        def fake_submit_run(conn, name, job_type, units, reason,
                            context=None, work_unit_run_id=None):
            self.submit_calls.append({
                "name": name, "work_unit_run_id": work_unit_run_id})
            submission = types.SimpleNamespace(job_id="job-1")
            return [(submission, ["attempt-1"])]

        submit_patcher = mock.patch.object(
            run_mod, "submit_run", fake_submit_run)
        submit_patcher.start()
        self.addCleanup(submit_patcher.stop)

        self.audit_calls = []

        def fake_record_external_action(conn, idempotency_key,
                                        action_class, target_scope, reason,
                                        dry_run=False, rows_affected=0,
                                        detail=None, policy_citation=None):
            self.audit_calls.append(dict(detail or {}))
            return {"rows_affected": rows_affected, "detail": detail}

        audit_patcher = mock.patch.object(
            run_mod, "record_external_action", fake_record_external_action)
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        # `RAPIDDB()` is only reached for its `exit_code` guard.
        db_mod_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_mod_patcher.start()
        self.addCleanup(db_mod_patcher.stop)

    def test_work_unit_run_id_is_threaded_through_to_submit_run(self):
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="key-1", name="accept-20260911-release",
            phase="statistics", reason="release stranded units",
            dry_run=False, out=_null_out(),
            work_unit_run_id="accept-20260911")

        self.assertEqual(len(self.submit_calls), 1)
        self.assertEqual(self.submit_calls[0]["name"],
                         "accept-20260911-release")
        self.assertEqual(self.submit_calls[0]["work_unit_run_id"],
                         "accept-20260911")

    def test_default_threads_none_through_to_submit_run(self):
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="key-2", name="accept-20260911",
            phase="statistics", reason="ordinary run",
            dry_run=False, out=_null_out())

        self.assertEqual(len(self.submit_calls), 1)
        self.assertIsNone(self.submit_calls[0]["work_unit_run_id"])

    def test_audit_detail_names_the_scope_when_it_differs(self):
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="key-3", name="accept-20260911-release",
            phase="statistics", reason="release stranded units",
            dry_run=False, out=_null_out(),
            work_unit_run_id="accept-20260911")

        self.assertEqual(len(self.audit_calls), 1)
        self.assertEqual(self.audit_calls[0].get("work_unit_run_id"),
                         "accept-20260911")

    def test_audit_detail_omits_the_scope_when_it_matches_the_run_name(
            self):
        # No override given at all -- must not invent a work_unit_run_id
        # entry for an ordinary run.
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="key-4", name="accept-20260911",
            phase="statistics", reason="ordinary run",
            dry_run=False, out=_null_out())

        self.assertEqual(len(self.audit_calls), 1)
        self.assertNotIn("work_unit_run_id", self.audit_calls[0])

    def test_audit_detail_omits_the_scope_when_explicitly_equal_to_name(
            self):
        # Given, but equal to `name` -- still not a "different scope"
        # fact worth recording.
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="key-5", name="accept-20260911",
            phase="statistics", reason="ordinary run, explicit but equal",
            dry_run=False, out=_null_out(),
            work_unit_run_id="accept-20260911")

        self.assertEqual(len(self.audit_calls), 1)
        self.assertNotIn("work_unit_run_id", self.audit_calls[0])


if __name__ == "__main__":
    unittest.main()
