"""``rapidpipe.launch``: a job submitted to Batch as ``job run``, or an old-contract stage run locally."""

from __future__ import annotations

import sys

import pytest

from rapidpipe import launch


class _Batch:
    def __init__(self):
        self.submitted = []

    def submit_job(self, **request):
        self.submitted.append(request)
        return {"jobId": "batch-1"}


@pytest.fixture()
def batch_env(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    monkeypatch.delenv("RAPID_DB_SECRET_ID", raising=False)
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", raising=False)


def test_submit_runs_job_run_for_the_job_with_the_outputs_root(batch_env):
    batch = _Batch()
    assert launch.submit(7, "difference", job_definition="definition:3", client=batch) == "batch-1"
    assert batch.submitted == [{
        "jobName": "rapid-difference-7", "jobQueue": "queue", "jobDefinition": "definition:3",
        "containerOverrides": {
            "command": ["job", "run", "--job", "7"],
            "environment": [{"name": "RAPIDPIPE_OUTPUTS_ROOT", "value": "s3://products/foundation"}]}}]


class _Conn:
    """Records the statements a submission runs and whether it committed."""

    def __init__(self):
        self.statements, self.committed = [], False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.statements.append((sql, params))

    def commit(self):
        self.committed = True


def test_submit_records_the_batch_job_id_on_the_job_row(batch_env):
    conn = _Conn()
    launch.submit(7, "difference", job_definition="definition", conn=conn, client=_Batch())
    assert conn.statements == [("UPDATE jobs SET batch_job_id = %s WHERE id = %s", ("batch-1", 7))]
    assert conn.committed


def test_submit_passes_the_database_secret_when_the_submitter_names_one(batch_env, monkeypatch):
    monkeypatch.setenv("RAPID_DB_SECRET_ID", "secret-name")
    batch = _Batch()
    launch.submit(7, "difference", job_definition="definition", client=batch)
    assert {"name": "RAPID_DB_SECRET_ID", "value": "secret-name"} in \
        batch.submitted[0]["containerOverrides"]["environment"]


def test_submit_refuses_without_a_queue(batch_env, monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_QUEUE")
    batch = _Batch()
    with pytest.raises(launch.LaunchError, match="RAPIDPIPE_BATCH_JOB_QUEUE"):
        launch.submit(7, "difference", job_definition="definition", client=batch)
    assert batch.submitted == []


def test_submit_refuses_an_outputs_root_a_batch_job_cannot_write(batch_env, monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "file:///home/someone/products")
    batch = _Batch()
    with pytest.raises(launch.LaunchError, match="s3://"):
        launch.submit(7, "difference", job_definition="definition", client=batch)
    assert batch.submitted == []


def test_run_locally_returns_the_stages_exit_code():
    assert launch.run_locally("admit", ["--no-such-flag"], python=sys.executable) == 64
