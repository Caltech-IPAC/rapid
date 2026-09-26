"""Tests for rapidpipe.launch.batch that need no database.

Exercises the pure and near-pure pieces: the submit_job call shape,
missing-environment-variable refusal, describe_jobs batching, and the
Batch status -> disposition mapping -- with repository calls monkeypatched
so no database connection is ever attempted. Database-backed end-to-end
behaviour (a real allocated attempt, a real selected upstream unit,
manifest fetch from a fake S3) is in tests/db/test_launch.py.
"""

from __future__ import annotations

import re

import pytest

from rapidpipe.launch import batch as launch_batch
from tests.unit.fakebatch import FakeBatch


class _FakeConn:
    def commit(self):
        pass

    def rollback(self):
        pass


_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _patch_repository(monkeypatch, *, attempt_id="ATTEMPT01", run_kind="scratch"):
    calls = {}

    def _fake_run_kind(conn, run_id):
        calls["run_kind"] = run_id
        return run_kind

    def _fake_add_unit(conn, run_id, stage, unit_kind, unit_id):
        calls["add_unit"] = (run_id, stage, unit_kind, unit_id)

    def _fake_allocate_attempt(conn, run_id, stage, unit_id, *, outputs_root=None):
        calls["allocate_outputs_root"] = outputs_root
        return attempt_id

    def _fake_record_scheduler_job(conn, attempt_id_, scheduler_job_id, output_location=None):
        calls["scheduler_job"] = (attempt_id_, scheduler_job_id, output_location)

    monkeypatch.setattr(launch_batch, "_run_kind", _fake_run_kind)
    monkeypatch.setattr(launch_batch, "_release_job_definition", lambda conn, run_id: None)
    monkeypatch.setattr(launch_batch, "add_unit", _fake_add_unit)
    monkeypatch.setattr(launch_batch, "allocate_attempt", _fake_allocate_attempt)
    monkeypatch.setattr(launch_batch, "record_scheduler_job", _fake_record_scheduler_job)
    monkeypatch.setattr(
        launch_batch, "record_attempt_locations",
        lambda conn, attempt_id_, inputs, settings: calls.__setitem__(
            "locations", (attempt_id_, inputs, settings)))
    return calls


# ======================================================================
# submit_unit: environment variables
# ======================================================================

def test_submit_unit_missing_job_queue_names_the_variable(monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_QUEUE", raising=False)
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    _patch_repository(monkeypatch)

    with pytest.raises(launch_batch.MissingEnvironmentVariable, match="RAPIDPIPE_BATCH_JOB_QUEUE"):
        launch_batch.submit_unit(
            _FakeConn(), run_id="r1", stage="admit", unit_kind="detector-image",
            unit_id="u1", inputs_location="s3://bucket/in", client=FakeBatch())


def test_submit_unit_missing_job_definition_names_the_variable(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_DEFINITION", raising=False)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    _patch_repository(monkeypatch)

    with pytest.raises(
        launch_batch.MissingEnvironmentVariable, match="RAPIDPIPE_BATCH_JOB_DEFINITION",
    ):
        launch_batch.submit_unit(
            _FakeConn(), run_id="r1", stage="admit", unit_kind="detector-image",
            unit_id="u1", inputs_location="s3://bucket/in", client=FakeBatch())


def test_submit_unit_missing_outputs_root_names_the_variable(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.delenv("RAPIDPIPE_OUTPUTS_ROOT", raising=False)
    _patch_repository(monkeypatch)

    with pytest.raises(launch_batch.MissingEnvironmentVariable, match="RAPIDPIPE_OUTPUTS_ROOT"):
        launch_batch.submit_unit(
            _FakeConn(), run_id="r1", stage="admit", unit_kind="detector-image",
            unit_id="u1", inputs_location="s3://bucket/in", client=FakeBatch())


# ======================================================================
# submit_unit: submit_job call shape
# ======================================================================

def test_submit_unit_submit_job_call_shape(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", raising=False)
    calls = _patch_repository(monkeypatch, attempt_id="01ATTEMPT0000000000000000")

    fake = FakeBatch()
    submission = launch_batch.submit_unit(
        _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
        unit_id="e1/SCA07", inputs_location="s3://in-bucket/in-prefix",
        client=fake)

    assert len(fake.submitted) == 1
    job = fake.submitted[0]
    assert job["jobQueue"] == "queue1"
    assert job["jobDefinition"] == "def1"
    assert job["jobName"] == submission.job_name
    assert _JOB_NAME_RE.match(job["jobName"])
    assert "/" not in job["jobName"]  # unit id, which may contain "/", is not in the name

    command = job["containerOverrides"]["command"]
    assert command == [
        "stage", "admit",
        "--run", "RUN01",
        "--unit", "e1/SCA07",
        "--attempt", "01ATTEMPT0000000000000000",
        "--inputs", "s3://in-bucket/in-prefix",
        "--outputs", submission.output_location,
    ]
    assert submission.output_location == (
        "s3://bucket/prefix/runs/RUN01/admit/e1/SCA07/01ATTEMPT0000000000000000")

    env = job["containerOverrides"]["environment"]
    assert {"name": "RAPIDPIPE_RUN_ID", "value": "RUN01"} in env
    assert {"name": "RAPIDPIPE_ATTEMPT_ID", "value": "01ATTEMPT0000000000000000"} in env

    assert calls["scheduler_job"] == (
        "01ATTEMPT0000000000000000", submission.job_id, submission.output_location)


def test_submit_unit_with_settings_location(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    _patch_repository(monkeypatch)

    fake = FakeBatch()
    launch_batch.submit_unit(
        _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
        unit_id="u1", inputs_location="s3://in/pre",
        settings_location="s3://in/settings.toml", client=fake)

    command = fake.submitted[0]["containerOverrides"]["command"]
    assert command[-2:] == ["--settings", "s3://in/settings.toml"]


def test_submit_unit_custom_job_name_prefix(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", "myproj")
    _patch_repository(monkeypatch, attempt_id="ATT1")

    fake = FakeBatch()
    submission = launch_batch.submit_unit(
        _FakeConn(), run_id="r1", stage="difference", unit_kind="field",
        unit_id="u1", inputs_location="s3://in/pre", client=fake)

    assert submission.job_name == "myproj-difference-ATT1"


# ======================================================================
# outputs_root_for / job_definition_for: kind-specific, then plain fallback
# ======================================================================

_KIND_VARS = (
    "RAPIDPIPE_OUTPUTS_ROOT", "RAPIDPIPE_OUTPUTS_ROOT_SCRATCH",
    "RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", "RAPIDPIPE_BATCH_JOB_DEFINITION",
    "RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH", "RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION",
)


@pytest.fixture()
def clean_kind_env(monkeypatch):
    for name in _KIND_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("kind", ["scratch", "production"])
def test_outputs_root_for_prefers_the_kind_specific_variable(clean_kind_env, kind):
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://plain/root")
    clean_kind_env.setenv(f"RAPIDPIPE_OUTPUTS_ROOT_{kind.upper()}", f"s3://{kind}/root")
    assert launch_batch.outputs_root_for(kind) == f"s3://{kind}/root"


def test_outputs_root_for_falls_back_to_the_plain_variable(clean_kind_env):
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://plain/root")
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", "s3://project/root")
    assert launch_batch.outputs_root_for("scratch") == "s3://plain/root"
    assert launch_batch.outputs_root_for("production") == "s3://project/root"


def test_outputs_root_for_scratch_missing_names_both_variables(clean_kind_env):
    with pytest.raises(launch_batch.MissingEnvironmentVariable) as excinfo:
        launch_batch.outputs_root_for("scratch")
    assert "RAPIDPIPE_OUTPUTS_ROOT_SCRATCH" in str(excinfo.value)
    assert "RAPIDPIPE_OUTPUTS_ROOT " in str(excinfo.value)


def test_production_never_falls_back_to_the_plain_variables(clean_kind_env):
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://plain/root")
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "plain-def")
    with pytest.raises(
        launch_batch.MissingEnvironmentVariable, match="RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION",
    ):
        launch_batch.outputs_root_for("production")
    with pytest.raises(
        launch_batch.MissingEnvironmentVariable, match="RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION",
    ):
        launch_batch.job_definition_for("production")


def test_submit_unit_for_a_production_run_fails_closed(clean_kind_env):
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://plain/root")
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "plain-def")
    calls = _patch_repository(clean_kind_env, run_kind="production")
    fake = FakeBatch()
    with pytest.raises(launch_batch.MissingEnvironmentVariable):
        launch_batch.submit_unit(
            _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
            unit_id="u1", inputs_location="s3://in/pre", client=fake)
    assert fake.submitted == []
    assert "add_unit" not in calls


@pytest.mark.parametrize("kind", ["scratch", "production"])
def test_job_definition_for_prefers_the_kind_specific_variable(clean_kind_env, kind):
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "plain-def")
    clean_kind_env.setenv(f"RAPIDPIPE_BATCH_JOB_DEFINITION_{kind.upper()}", f"{kind}-def")
    assert launch_batch.job_definition_for(kind) == f"{kind}-def"


def test_job_definition_for_scratch_falls_back_to_the_plain_variable(clean_kind_env):
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "plain-def")
    assert launch_batch.job_definition_for("scratch") == "plain-def"


def test_job_definition_for_missing_raises(clean_kind_env):
    with pytest.raises(
        launch_batch.MissingEnvironmentVariable, match="RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH",
    ):
        launch_batch.job_definition_for("scratch")


def test_kind_lookup_refuses_an_unknown_kind(clean_kind_env):
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://plain/root")
    with pytest.raises(ValueError):
        launch_batch.outputs_root_for("staging")


@pytest.mark.parametrize("kind", ["scratch", "production"])
def test_submit_unit_uses_the_run_kinds_root_and_definition(clean_kind_env, kind):
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", "s3://scratch-bucket/s")
    clean_kind_env.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", "s3://project-bucket/p")
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH", "scratch-def")
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION", "production-def")
    calls = _patch_repository(clean_kind_env, attempt_id="ATT1", run_kind=kind)

    fake = FakeBatch()
    submission = launch_batch.submit_unit(
        _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
        unit_id="u1", inputs_location="s3://in/pre", client=fake)

    expected_root = {"scratch": "s3://scratch-bucket/s",
                     "production": "s3://project-bucket/p"}[kind]
    assert calls["run_kind"] == "RUN01"
    assert submission.output_location == f"{expected_root}/runs/RUN01/admit/u1/ATT1"
    # The attempt row is inserted with its final location (amendment A7).
    assert calls["allocate_outputs_root"] == expected_root
    assert fake.submitted[0]["jobDefinition"] == f"{kind}-def"


def test_submit_unit_explicit_arguments_skip_the_kind_lookup(clean_kind_env):
    clean_kind_env.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    calls = _patch_repository(clean_kind_env, attempt_id="ATT1")

    fake = FakeBatch()
    submission = launch_batch.submit_unit(
        _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
        unit_id="u1", inputs_location="s3://in/pre", outputs_root="s3://given/root",
        job_definition="given-def", client=fake)

    assert "run_kind" not in calls
    assert submission.output_location == "s3://given/root/runs/RUN01/admit/u1/ATT1"
    assert fake.submitted[0]["jobDefinition"] == "given-def"


# ======================================================================
# describe_jobs batching at 100
# ======================================================================

def test_reconcile_batches_describe_jobs_at_100(monkeypatch):
    job_ids = [f"job-{i}" for i in range(150)]
    rows = [(f"attempt-{i}", f"loc-{i}", job_id) for i, job_id in enumerate(job_ids)]

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            self._query = query

        def fetchall(self):
            # The repair pass (rapidpipe.launch.batch
            # ._repair_stranded_succeeded_attempts) queries "JOIN units";
            # nothing to repair here.
            if "JOIN units" in self._query:
                return []
            return rows

    class _Conn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    fake = FakeBatch()
    for attempt_id, _loc, job_id in rows:
        fake.set_status(job_id, "RUNNING")

    launch_batch.reconcile(_Conn(), run_id="r1", client=fake)

    describe_calls = [c for c in fake.calls if c[0] == "describe_jobs"]
    assert len(describe_calls) == 2
    assert len(describe_calls[0][1]) == 100
    assert len(describe_calls[1][1]) == 50


# ======================================================================
# status -> disposition mapping (including a missing job id)
# ======================================================================

def _reconcile_one(monkeypatch, *, status, container_exit_code=None,
                    manifest_ok=None, forget=False,
                    manifest_fetch_error=None, exec_record_fetch_error=None,
                    repair_rows=()):
    """Reconcile exactly one attempt/job and return the Reconciled result.

    manifest_ok controls what _fetch_manifest_if_valid returns for a
    SUCCEEDED job; None means "not applicable" (any other status).
    manifest_fetch_error/exec_record_fetch_error, when given, make the
    corresponding fetch function raise it instead of returning a value --
    standing in for a launcher-side S3 fetch failure (e.g. AccessDenied)
    reconcile could not resolve.
    repair_rows, when given, is what the repair pass's "JOIN units" query
    returns -- rows already stranded 'succeeded'-but-unselected before
    this reconcile call.
    """
    recorded = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            self._query = query

        def fetchall(self):
            if "JOIN units" in self._query:
                return list(repair_rows)
            return [("attempt-1", "loc-1", "job-1")]

    class _Conn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    def _fake_record_attempt_result(conn, attempt_id, exit_code, disposition,
                                     output_location, execution_record,
                                     scheduler_job_id):
        recorded["record"] = (attempt_id, exit_code, disposition, scheduler_job_id)

    def _fake_select_attempt(conn, attempt_id):
        recorded["selected"] = attempt_id

    def _fake_record_reconcile_note(conn, attempt_id, note):
        recorded["note"] = (attempt_id, note)

    monkeypatch.setattr(launch_batch, "record_attempt_result", _fake_record_attempt_result)
    monkeypatch.setattr(launch_batch, "select_attempt", _fake_select_attempt)
    monkeypatch.setattr(launch_batch, "record_reconcile_note", _fake_record_reconcile_note)
    monkeypatch.setattr(launch_batch, "_run_schema_version", lambda conn, run_id: "1")

    def _classify(exc, *, key):
        # Mirrors _fetch_manifest_if_valid/_fetch_execution_record's own
        # classification: a "not found" code means "no such object", any
        # other error is a ReconcileFetchFailed reconcile cannot resolve.
        code = launch_batch._client_error_code(exc)
        if code in launch_batch._NOT_FOUND_ERROR_CODES:
            return None
        raise launch_batch.ReconcileFetchFailed(exc, key=key) from exc

    def _fake_fetch_manifest(output_location, s3_client):
        if manifest_fetch_error is not None:
            return _classify(manifest_fetch_error, key=output_location)
        return object() if manifest_ok else None

    def _fake_fetch_execution_record(output_location, attempt_id, s3_client):
        if exec_record_fetch_error is not None:
            result = _classify(exec_record_fetch_error, key=output_location)
            return {} if result is None else result
        return {}

    if manifest_ok is not None or manifest_fetch_error is not None:
        monkeypatch.setattr(launch_batch, "_fetch_manifest_if_valid", _fake_fetch_manifest)
        monkeypatch.setattr(
            launch_batch, "_fetch_execution_record", _fake_fetch_execution_record)

    fake = FakeBatch()
    if not forget:
        fake.set_status(
            "job-1", status, container_exit_code=container_exit_code)

    results = launch_batch.reconcile(_Conn(), run_id="r1", client=fake)
    assert len(results) == 1
    return results[0], recorded


@pytest.mark.parametrize("status", ["SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"])
def test_reconcile_unresolved_statuses_untouched(monkeypatch, status):
    result, recorded = _reconcile_one(monkeypatch, status=status)
    assert result.disposition is None
    assert result.selected is False
    assert "record" not in recorded


def test_reconcile_succeeded_with_valid_manifest(monkeypatch):
    result, recorded = _reconcile_one(monkeypatch, status="SUCCEEDED", manifest_ok=True)
    assert result.disposition == "succeeded"
    assert result.selected is True
    assert recorded["record"] == ("attempt-1", 0, "succeeded", "job-1")
    assert recorded["selected"] == "attempt-1"


def test_reconcile_succeeded_without_manifest_is_failed(monkeypatch):
    result, recorded = _reconcile_one(monkeypatch, status="SUCCEEDED", manifest_ok=False)
    assert result.disposition == "failed"
    assert result.selected is False
    assert recorded["record"] == ("attempt-1", 0, "failed", "job-1")
    assert "selected" not in recorded


# ======================================================================
# SUCCEEDED + a launcher-side fetch failure: must NOT become terminal
# ======================================================================

class _FakeAccessDenied(Exception):
    """Stands in for a botocore ClientError-shaped AccessDenied: not a
    "not found" code, so _client_error_code must not classify it as
    "the object is absent"."""

    def __init__(self):
        super().__init__("AccessDenied: not authorized")
        self.response = {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}


def test_reconcile_succeeded_manifest_fetch_error_stays_unresolved(monkeypatch):
    result, recorded = _reconcile_one(
        monkeypatch, status="SUCCEEDED", manifest_fetch_error=_FakeAccessDenied())
    assert result.disposition is None
    assert result.selected is False
    assert "record" not in recorded
    assert "selected" not in recorded
    attempt_id, note = recorded["note"]
    assert attempt_id == "attempt-1"
    assert "AccessDenied" in note


def test_reconcile_succeeded_exec_record_fetch_error_stays_unresolved(monkeypatch):
    # The manifest fetch succeeds, but the execution-record fetch itself
    # hits the same class of launcher-side error -- still must not become
    # terminal for the same reason.
    result, recorded = _reconcile_one(
        monkeypatch, status="SUCCEEDED", manifest_ok=True,
        exec_record_fetch_error=_FakeAccessDenied())
    assert result.disposition is None
    assert result.selected is False
    assert "record" not in recorded
    assert "selected" not in recorded
    attempt_id, note = recorded["note"]
    assert attempt_id == "attempt-1"
    assert "AccessDenied" in note


def test_reconcile_succeeded_manifest_not_found_is_still_failed(monkeypatch):
    # A real "not found" (404/NoSuchKey) is unchanged behaviour: the job
    # genuinely wrote no manifest, so this stays terminal 'failed', not a
    # reconcile note.
    not_found = _FakeAccessDenied()
    not_found.response = {"Error": {"Code": "NoSuchKey", "Message": "not found"}}
    result, recorded = _reconcile_one(
        monkeypatch, status="SUCCEEDED", manifest_fetch_error=not_found)
    assert result.disposition == "failed"
    assert recorded["record"] == ("attempt-1", 0, "failed", "job-1")
    assert "note" not in recorded


def test_reconcile_failed_exit_75_is_transient(monkeypatch):
    result, recorded = _reconcile_one(
        monkeypatch, status="FAILED", container_exit_code=75)
    assert result.disposition == "transient"
    assert recorded["record"] == ("attempt-1", 75, "transient", "job-1")


def test_reconcile_failed_exit_70_is_failed(monkeypatch):
    result, recorded = _reconcile_one(
        monkeypatch, status="FAILED", container_exit_code=70)
    assert result.disposition == "failed"
    assert recorded["record"] == ("attempt-1", 70, "failed", "job-1")


def test_reconcile_failed_no_exit_code_is_killed(monkeypatch):
    result, recorded = _reconcile_one(
        monkeypatch, status="FAILED", container_exit_code=None)
    assert result.disposition == "killed"
    assert recorded["record"] == ("attempt-1", None, "killed", "job-1")


def test_reconcile_missing_job_id_is_lost(monkeypatch):
    result, recorded = _reconcile_one(monkeypatch, status="FAILED", forget=True)
    assert result.disposition == "lost"
    assert result.batch_status == "LOST"
    assert recorded["record"] == ("attempt-1", None, "lost", "job-1")


# ======================================================================
# reconcile: repair pass for a pre-existing stranded 'succeeded' attempt
#
# Before this fix, record_attempt_result and select_attempt committed
# separately in the SUCCEEDED+manifest branch above; a process death or a
# failed second commit between them left an attempt 'succeeded' with its
# unit neither selected nor complete, and invisible to the main query
# (disposition IS NULL) forever after. reconcile now also runs
# _repair_stranded_succeeded_attempts, whose own "JOIN units" query finds
# exactly that shape and selects it. The main branch's success case now
# shares one commit for both writes, so this only ever cleans up
# pre-existing damage.
# ======================================================================

def _repair_conn(*, main_rows=(), repair_rows=()):
    """A fake conn answering the main unresolved-attempts query and the
    repair pass's "JOIN units" query from two canned row lists, keyed off
    query text exactly as test_reconcile_batches_describe_jobs_at_100 and
    _reconcile_one do."""

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            self._query = query

        def fetchall(self):
            if "JOIN units" in self._query:
                return list(repair_rows)
            return list(main_rows)

    class _Conn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    return _Conn()


def test_reconcile_repair_pass_selects_stranded_succeeded_attempt(monkeypatch):
    # No attempt is unresolved (main query empty); the repair query finds
    # one attempt already 'succeeded' with no selected attempt on its
    # unit -- the pre-existing-damage case.
    conn = _repair_conn(repair_rows=[("attempt-9", "job-9")])
    selected = []
    monkeypatch.setattr(
        launch_batch, "select_attempt",
        lambda conn, attempt_id: selected.append(attempt_id))

    results = launch_batch.reconcile(conn, run_id="r1")

    assert selected == ["attempt-9"]
    assert len(results) == 1
    result = results[0]
    assert result.attempt_id == "attempt-9"
    assert result.job_id == "job-9"
    assert result.disposition == "succeeded"
    assert result.selected is True


def test_reconcile_repair_pass_leaves_already_complete_unit_untouched(monkeypatch):
    # The repair query's own WHERE clause excludes a unit that already
    # has a selected attempt or is otherwise terminal (exercised against
    # a real database in tests/db/test_launch.py); at this level that
    # means the query simply returns no row for it, so there is nothing
    # for the repair pass to select.
    conn = _repair_conn(repair_rows=[])
    selected = []
    monkeypatch.setattr(
        launch_batch, "select_attempt",
        lambda conn, attempt_id: selected.append(attempt_id))

    results = launch_batch.reconcile(conn, run_id="r1")

    assert selected == []
    assert results == []


class _SchemaConn:
    """Fake connection for _execution_record_with_defaults: cursor().execute()
    then fetchone() returns a one-tuple schema version, as
    _run_schema_version expects."""

    def __init__(self, schema_version="1"):
        self._schema_version = schema_version

    def cursor(self):
        conn = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, query, params=None):
                pass

            def fetchone(self):
                return (conn._schema_version,)

        return _Cursor()


def test_execution_record_with_defaults_fills_missing_keys():
    record = launch_batch._execution_record_with_defaults(_SchemaConn("2"), "r1", {})
    assert record == {
        "schema_version": "2",
        "source_revision": "unknown",
        "settings_hash": "unknown",
    }


def test_execution_record_with_defaults_fills_none_values():
    # A Batch-written exec/<attempt>.json with the keys present but null
    # (the failure this fix addresses: dict.setdefault is a no-op when the
    # key already exists, even with value None).
    record = launch_batch._execution_record_with_defaults(
        _SchemaConn("2"), "r1",
        {"image_digest": None, "settings_hash": "6725abc", "source_revision": None})
    assert record == {
        "schema_version": "2",
        "source_revision": "unknown",
        "settings_hash": "6725abc",
        "image_digest": None,
    }


def test_execution_record_with_defaults_keeps_real_values():
    record = launch_batch._execution_record_with_defaults(
        _SchemaConn("2"), "r1",
        {"schema_version": "9", "source_revision": "abc123", "settings_hash": "hash1"})
    assert record == {
        "schema_version": "9",
        "source_revision": "abc123",
        "settings_hash": "hash1",
    }


def test_execution_record_with_defaults_handles_none_record():
    record = launch_batch._execution_record_with_defaults(_SchemaConn("2"), "r1", None)
    assert record == {
        "schema_version": "2",
        "source_revision": "unknown",
        "settings_hash": "unknown",
    }


def test_submit_unit_profile_sets_the_container_environment(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    _patch_repository(monkeypatch, run_kind="scratch")

    fake = FakeBatch()
    launch_batch.submit_unit(
        _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
        unit_id="u1", inputs_location="s3://in/pre", client=fake, profile=True)

    env = fake.submitted[0]["containerOverrides"]["environment"]
    assert {"name": "RAPIDPIPE_PROFILE", "value": "1"} in env


def test_submit_unit_without_profile_omits_the_variable(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "def1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/prefix")
    _patch_repository(monkeypatch, run_kind="scratch")

    fake = FakeBatch()
    launch_batch.submit_unit(
        _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
        unit_id="u1", inputs_location="s3://in/pre", client=fake)

    env = fake.submitted[0]["containerOverrides"]["environment"]
    assert all(item["name"] != "RAPIDPIPE_PROFILE" for item in env)


def test_submit_unit_profile_refused_for_a_production_run(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", "s3://project/root")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION", "prod-def")
    calls = _patch_repository(monkeypatch, run_kind="production")

    fake = FakeBatch()
    with pytest.raises(launch_batch.ProfileNotAllowed, match="RUN01"):
        launch_batch.submit_unit(
            _FakeConn(), run_id="RUN01", stage="admit", unit_kind="detector-image",
            unit_id="u1", inputs_location="s3://in/pre", client=fake, profile=True)
    # Refused before anything is written or submitted.
    assert fake.submitted == []
    assert "add_unit" not in calls
