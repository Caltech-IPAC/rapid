"""``rapidpipe selftest``'s argument parsing and exit-code mapping.

Not a fixture run: :mod:`tests.unit.test_difference_fixture` and
``make stage-load`` already cover the fixtures themselves end to end
(fake tools, in-process and as a subprocess). This module covers the
CLI layer alone -- ``_build_parser``'s ``selftest`` subparser, and
``rapidpipe.cli.main._run_selftest_command``'s translation of argparse
into :func:`rapidpipe.selftest.run`'s keyword arguments -- plus
:mod:`rapidpipe.selftest`'s own exit-code mapping (0 pass / 1 fixture
mismatch / the stage's own code on a stage failure), by constructing a
:class:`rapidpipe.selftest.runner.FixtureResult` directly rather than
running a stage subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import rapidpipe.cli.main as cli_main
from rapidpipe.selftest import _report, run as run_selftest
from rapidpipe.selftest.runner import Checks, FixtureResult


def _parse(argv: list[str]):
    return cli_main._build_parser().parse_args(argv)


def test_selftest_requires_a_stage():
    with pytest.raises(SystemExit):
        _parse(["selftest"])


def test_selftest_rejects_an_unknown_stage():
    with pytest.raises(SystemExit):
        _parse(["selftest", "--stage", "bogus"])


def test_selftest_accepts_every_known_stage():
    for stage in ("difference", "load", "maintain", "crossmatch", "alerts"):
        args = _parse(["selftest", "--stage", stage])
        assert args.stage == stage


def test_selftest_defaults():
    args = _parse(["selftest", "--stage", "difference"])
    assert args.real_tools is False
    assert args.work_dir is None
    assert args.output_location is None
    assert args.python == sys.executable


def test_selftest_flags_parse():
    args = _parse([
        "selftest", "--stage", "load", "--real-tools",
        "--work-dir", "/tmp/wd", "--output-location", "s3://bucket/prefix",
        "--python", "/usr/bin/python3.11"])
    assert args.stage == "load"
    assert args.real_tools is True
    assert args.work_dir == "/tmp/wd"
    assert args.output_location == "s3://bucket/prefix"
    assert args.python == "/usr/bin/python3.11"


def test_run_selftest_command_forwards_argparse_to_run(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_main, "run_selftest", fake_run)
    args = _parse([
        "selftest", "--stage", "difference", "--real-tools",
        "--work-dir", "/tmp/wd", "--output-location", "/tmp/out",
        "--python", "/usr/bin/python3"])
    assert cli_main._run_selftest_command(args) == 0
    assert captured == {
        "stage": "difference", "real_tools": True, "work_dir": "/tmp/wd",
        "output_location": "/tmp/out", "python": "/usr/bin/python3"}


def test_main_dispatches_selftest(monkeypatch):
    monkeypatch.setattr(cli_main, "run_selftest", lambda **kwargs: 0)
    assert cli_main.main(["selftest", "--stage", "load"]) == 0


def _result(exit_code: int, checks: Checks, tools: str = "fake") -> FixtureResult:
    return FixtureResult(
        stage="difference", tools=tools, exit_code=exit_code, checks=checks,
        work_dir=Path("/tmp/selftest-wd"), output_location="/tmp/selftest-wd/outputs",
        output_is_local=True)


def test_report_exits_zero_on_a_full_pass():
    checks = Checks()
    checks.check(True, "exit code: expected 0, got 0")
    checks.check(True, "manifest.json published")
    assert _report("difference", _result(0, checks)) == 0


def test_report_exits_one_on_a_fixture_mismatch():
    """The stage exited as expected, but a product check disagrees with
    expected.json: a fixture mismatch, not a stage failure."""
    checks = Checks()
    checks.check(True, "exit code: expected 0, got 0")
    checks.check(False, "difference-image entries: expected 1, got 0")
    assert _report("difference", _result(0, checks)) == 1


def test_report_propagates_the_stages_own_exit_code_on_a_stage_failure():
    """The stage itself did not exit as the fixture expects -- exit 70
    (STAGE_ERROR), say -- propagated as-is, not folded into the fixture's
    generic 0/1."""
    checks = Checks()
    checks.check(False, "exit code: expected 0, got 70")
    assert _report("difference", _result(70, checks)) == 70


def test_report_propagates_a_usage_exit_code_on_a_stage_failure():
    checks = Checks()
    checks.check(False, "exit code: expected 0, got 64")
    assert _report("load", _result(64, checks)) == 64


def test_run_selftest_reports_the_missing_stage_module_by_raising(monkeypatch):
    with pytest.raises(ValueError, match="no such stage fixture"):
        run_selftest(stage="bogus", real_tools=False, work_dir=None, output_location=None)
