"""``rapidpipe.launch``: a job's stage submitted to Batch, or run locally."""

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
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "definition")
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", raising=False)


def test_submit_runs_the_stage_command_under_a_name_carrying_the_job(batch_env):
    batch = _Batch()
    assert launch.submit(7, "difference", ["--inputs", "x"], client=batch) == "batch-1"
    assert batch.submitted == [{
        "jobName": "rapid-difference-7", "jobQueue": "queue", "jobDefinition": "definition",
        "containerOverrides": {"command": ["rapidpipe", "stage", "difference", "--inputs", "x"]}}]


def test_submit_refuses_without_a_queue(batch_env, monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_QUEUE")
    batch = _Batch()
    with pytest.raises(launch.LaunchError, match="RAPIDPIPE_BATCH_JOB_QUEUE"):
        launch.submit(7, "difference", [], client=batch)
    assert batch.submitted == []


def test_submit_refuses_an_unknown_stage(batch_env):
    with pytest.raises(launch.LaunchError, match="unknown stage"):
        launch.submit(7, "crossmatch", [], client=_Batch())


def test_run_locally_returns_the_stages_exit_code():
    assert launch.run_locally("admit", ["--no-such-flag"], python=sys.executable) == 64
