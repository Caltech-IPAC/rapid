"""A failed job's exit code and reason: the process and the row agree, and the row says what failed.

Contracts: an exception inside ``job run`` exits with ``cli.exit_code``'s
code and the job row records that same code, so a transient failure (a
throttled AWS call, S3's ``SlowDown``, ``psycopg2.OperationalError``) is 75
on both and Batch repeats it, while a plain ``ValueError`` is 70 on both;
the row's ``refusal`` is ``<Class>: <message>``, a ``CommandExit``'s
included, its code unchanged; when that recording write itself fails, the
stage's own exception is the one that propagates.
"""

from __future__ import annotations

import contextlib

import psycopg2
import pytest

from rapidpipe import foundation
from rapidpipe.cli import main as cli
from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation import local


class ClientError(Exception):
    """A stand-in shaped as botocore's: ``response["Error"]["Code"]`` and the HTTP status."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class Cursor:
    def __init__(self, recorded: list):
        self.recorded, self.rows = recorded, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=()):
        self.recorded.append((statement, params))
        self.rows = [{"state": "launched", "stage": "trivial", "scope": {}, "run": 3,
                      "published_attempt": None, "refusal": None, "settings": {},
                      "survey": "HLWAS", "dataset": "d"}] if "FROM jobs j JOIN runs r" in statement else []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, recorded: list):
        self.recorded = recorded

    def cursor(self, **_):
        return Cursor(self.recorded)

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture()
def job_run(monkeypatch, tmp_path):
    """``job run --job 5`` with a stage raising ``exc``; give the exit code and the failed row's (exit_code, refusal)."""
    recorded: list = []
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", tmp_path.as_uri())
    monkeypatch.setattr(cli, "connect", lambda **_: contextlib.nullcontext(Connection(recorded)))
    monkeypatch.setattr(foundation, "job_show", lambda conn, job: {"stage": "trivial"})
    monkeypatch.setattr(local, "instance_type", lambda: None)

    def invoke(exc: BaseException):
        def stage(job):
            raise exc

        monkeypatch.setattr(cli, "_stage_run", lambda name: stage)
        code = cli.main(["job", "run", "--job", "5"])
        (row,) = [params for statement, params in recorded if "state = 'failed'" in statement]
        return code, row[:2]

    return invoke


@pytest.mark.parametrize("exc", [ClientError("ThrottlingException"), ClientError("SlowDown", 503),
                                 psycopg2.OperationalError("server closed the connection unexpectedly")],
                         ids=["throttling", "slowdown", "operational"])
def test_a_transient_failure_exits_75_and_records_75(job_run, exc):
    code, (row_code, refusal) = job_run(exc)
    assert code == row_code == ExitCode.TRANSIENT_FAILURE
    assert refusal.startswith(f"{type(exc).__name__}: ")


def test_a_plain_error_exits_70_and_records_70_with_its_class_and_message(job_run):
    code, (row_code, refusal) = job_run(ValueError("the reference has no rows"))
    assert code == row_code == ExitCode.STAGE_ERROR
    assert refusal == "ValueError: the reference has no rows"


def test_a_named_stop_keeps_its_code_and_records_its_class_and_message(job_run):
    code, (row_code, refusal) = job_run(CommandExit(int(ExitCode.INPUT_REJECTED), "no frames"))
    assert code == row_code == ExitCode.INPUT_REJECTED
    assert refusal == "CommandExit: no frames"


def test_the_reason_is_at_most_500_characters(job_run):
    _, (_, refusal) = job_run(ValueError("x" * 2000))
    assert len(refusal) == local.REFUSAL_CHARS == 500


def test_a_failed_recording_write_lets_the_stages_own_exception_propagate(monkeypatch, tmp_path, caplog):
    calls = []

    def connect():
        calls.append(None)
        if len(calls) > 1:                       # the read works; the failure's write does not
            raise psycopg2.OperationalError("the database went away")
        return contextlib.nullcontext(Connection([]))

    monkeypatch.setattr(local, "instance_type", lambda: None)

    def stage(job):
        raise ValueError("the stage's own failure")

    with pytest.raises(ValueError, match="the stage's own failure"):
        local.run_job(connect, 5, tmp_path.as_uri(), stage)
    assert "job 5" in caplog.text and "ValueError: the stage's own failure" in caplog.text
