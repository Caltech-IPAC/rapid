"""``rapidpipe check`` and the step-6 flags on ``run create``/``run
promote``/``run start``, with no database (supervisor step 6,
2026-09-24, R4-R6)."""

from __future__ import annotations

import contextlib

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.cli import runctl
from rapidpipe.runs import repository


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_k):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def cursor(self):
        return _Cursor()


@pytest.fixture()
def fake_conn(monkeypatch):
    conn = _Conn()

    @contextlib.contextmanager
    def _connect(**_kwargs):
        yield conn

    monkeypatch.setattr(cli, "connect", _connect)
    return conn


def test_check_list_names_checks_and_policies(capsys):
    assert cli.main(["check", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out] == [
        "check=catalog-counts-vs-reference@1", "check=difference-image-statistics@1",
        "policy=rebuild-strict@1", "policy=rebuild-trial@1"]
    assert ("policy=rebuild-trial@1 approval=trial approved_by=supervisor-step-6 "
            "auto_promote=false checks=difference-image-statistics@1(required),"
            "catalog-counts-vs-reference@1(advisory)") in out


def test_check_without_a_subcommand_exits_64(capsys):
    assert cli.main(["check"]) == 64
    assert "a subcommand is required: list, run, show" in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    (["--param", "n_max=5"], "--param needs --check"),
    (["--check", "difference-image-statistics@1", "--param", "n_max"], "expects KEY=VALUE"),
])
def test_check_run_param_usage_errors_exit_64_before_connecting(monkeypatch, capsys, argv,
                                                                message):
    monkeypatch.setattr(cli, "connect", lambda **_k: pytest.fail("connected"))
    assert cli.main(["check", "run", "R", *argv]) == 64
    assert message in capsys.readouterr().err


def test_check_run_and_show_refuse_an_unknown_run(fake_conn, capsys):
    assert cli.main(["check", "run", "R"]) == 64
    assert "rapidpipe check run: run R does not exist" in capsys.readouterr().err
    assert cli.main(["check", "show", "R"]) == 64
    assert "rapidpipe check show: run R does not exist" in capsys.readouterr().err


def test_run_create_auto_promote_refused_exit_64(fake_conn, capsys):
    argv = ["run", "create", "--kind", "production", "--purpose", "p", "--stages", "admit",
            "--auto-promote"]
    assert cli.main(argv) == 64
    assert capsys.readouterr().err == (
        "rapidpipe run create: policy rebuild-trial@1 does not permit automatic "
        "promotion; lead approval pending\n")
    assert cli.main(argv + ["--check-policy", "rebuild-strict@1"]) == 64
    assert "policy rebuild-strict@1 does not permit" in capsys.readouterr().err
    assert cli.main(argv[:-1] + ["--check-policy", "nosuch@1"]) == 64
    assert "check policy 'nosuch@1' does not exist" in capsys.readouterr().err


def test_run_promote_passes_check_policy_through(monkeypatch, fake_conn, capsys):
    seen = {}

    def _promote_run(conn, run_id, who, reason, **kwargs):
        seen.update(kwargs)
        return "P1"

    monkeypatch.setattr(repository, "promote_run", _promote_run)
    assert cli.main(["run", "promote", "R", "--reason", "r",
                     "--check-policy", "rebuild-strict@1"]) == 0
    assert seen["check_policy"] == "rebuild-strict@1"
    seen.clear()
    assert cli.main(["run", "promote", "R", "--reason", "r"]) == 0
    assert "check_policy" not in seen   # promote_run resolves run's ref > default


def test_run_start_end_prints_the_auto_promote_line(monkeypatch, fake_conn, capsys):
    monkeypatch.setattr(runctl, "_run_row",
                        lambda conn, run_id: runctl.RunRow("production", ["admit"], "open", None))
    monkeypatch.setattr(runctl, "_unit_row",
                        lambda conn, run_id, stage, unit_id: runctl.UnitRow(
                            "complete", "A1", "A1", "succeeded", "j", "s3://o"))
    assert cli.main(["run", "start", "R", "--unit", "U"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-2:] == ["auto-promote off (policy rebuild-trial@1)", "run=R state=complete"]
