"""``job run``'s exit code after publishing: a refusal because the run is not live is not a failure.

Contracts: a job published unbound because its run is not live exits 0
(Batch's status means "the job ran"; the refusal is on the job's row and
on stderr); a job published but refused for any other reason exits 1.
"""

from __future__ import annotations

import contextlib

import pytest

from rapidpipe import foundation
from rapidpipe.cli import main as cli
from rapidpipe.exitcodes import ExitCode


@pytest.fixture()
def job_run(monkeypatch, capsys):
    """Run ``job run --job 5`` with ``run_job`` returning ``result``; give the exit code and stderr."""
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://outputs/foundation")
    monkeypatch.setattr(cli, "connect", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr(foundation, "job_show", lambda conn, job: {"stage": "trivial"})
    monkeypatch.setattr(cli, "_stage_run", lambda stage: None)

    def invoke(result):
        monkeypatch.setattr(foundation, "run_job", lambda *args, **kwargs: {"job": 5, "attempt": 1, **result})
        code = cli.main(["job", "run", "--job", "5"])
        return code, capsys.readouterr().err

    return invoke


def test_a_job_published_under_a_run_that_is_not_live_exits_zero(job_run):
    refusal = "run 3 is not live for its survey and dataset"
    code, err = job_run({"published": True, "group": None, "refusal": refusal, "live": False})
    assert code == ExitCode.SUCCESS
    assert refusal in err


def test_a_job_published_but_refused_under_a_live_run_exits_one(job_run):
    refusal = "binding 12 is no longer open"
    code, err = job_run({"published": True, "group": None, "refusal": refusal, "live": True})
    assert code == ExitCode.FAILURE
    assert refusal in err
