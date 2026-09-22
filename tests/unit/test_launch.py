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


def _patch_repository(monkeypatch, *, attempt_id="ATTEMPT01"):
    calls = {}

    def _fake_add_unit(conn, run_id, stage, unit_kind, unit_id):
        calls["add_unit"] = (run_id, stage, unit_kind, unit_id)

    def _fake_allocate_attempt(conn, run_id, stage, unit_id):
        return attempt_id

    def _fake_record_scheduler_job(conn, attempt_id_, scheduler_job_id):
        calls["scheduler_job"] = (attempt_id_, scheduler_job_id)

    monkeypatch.setattr(launch_batch, "add_unit", _fake_add_unit)
    monkeypatch.setattr(launch_batch, "allocate_attempt", _fake_allocate_attempt)
    monkeypatch.setattr(launch_batch, "record_scheduler_job", _fake_record_scheduler_job)
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

    assert calls["scheduler_job"] == ("01ATTEMPT0000000000000000", submission.job_id)


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
            pass

        def fetchall(self):
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
                    manifest_ok=None, forget=False):
    """Reconcile exactly one attempt/job and return the Reconciled result.

    manifest_ok controls what _fetch_manifest_if_valid returns for a
    SUCCEEDED job; None means "not applicable" (any other status).
    """
    recorded = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            pass

        def fetchall(self):
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

    monkeypatch.setattr(launch_batch, "record_attempt_result", _fake_record_attempt_result)
    monkeypatch.setattr(launch_batch, "select_attempt", _fake_select_attempt)
    monkeypatch.setattr(launch_batch, "_run_schema_version", lambda conn, run_id: "1")
    if manifest_ok is not None:
        monkeypatch.setattr(
            launch_batch, "_fetch_manifest_if_valid",
            lambda output_location, s3_client: (object() if manifest_ok else None))
        monkeypatch.setattr(
            launch_batch, "_fetch_execution_record",
            lambda output_location, attempt_id, s3_client: {})

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
