"""``rapidpipe.exitcodes``: the one exit-code vocabulary, the stage subset,
the parser class every entrypoint uses, and the unexpected-error boundary
of both command-line entrypoints (supervisor step 1, 2026-09-26)."""

from __future__ import annotations

import argparse
import ast
import contextlib
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import rapidpipe.exitcodes as exitcodes
from rapidpipe.cli import checkctl
from rapidpipe.cli import main as cli
from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.exitcodes import ArgumentParser, ExitCode
from rapidpipe.release import __main__ as release_main
from rapidpipe.selftest import _report
from rapidpipe.selftest.runner import Checks, FixtureResult
from rapidpipe.stages import contract
from rapidpipe.stages.contract import STAGE_EXIT_CODES, StageDeclaration


# ======================================================================
# The vocabulary and the stage subset
# ======================================================================

def test_the_eight_values():
    assert {m.name: int(m) for m in ExitCode} == {
        "SUCCESS": 0,
        "FAILURE": 1,
        "INCOMPLETE": 2,
        "USAGE": 64,
        "INPUT_REJECTED": 65,
        "NOT_IMPLEMENTED": 69,
        "STAGE_ERROR": 70,
        "TRANSIENT_FAILURE": 75,
    }


def test_contract_reexports_the_one_enum():
    assert contract.ExitCode is ExitCode


def test_stage_exit_codes_are_exactly_the_six():
    assert STAGE_EXIT_CODES == (
        ExitCode.SUCCESS, ExitCode.USAGE, ExitCode.INPUT_REJECTED,
        ExitCode.NOT_IMPLEMENTED, ExitCode.STAGE_ERROR, ExitCode.TRANSIENT_FAILURE)


def _declaration(codes):
    return StageDeclaration(
        name="admit", unit="exposure", argument_schema={}, settings_schema_path=None,
        consumes=(), produces=("exposure",), database_access="none",
        supported_exit_codes=codes)


@pytest.mark.parametrize("code", [ExitCode.INCOMPLETE, ExitCode.FAILURE])
def test_a_declaration_with_a_command_line_only_code_is_rejected(code):
    with pytest.raises(ValueError, match="unsupported exit codes"):
        _declaration((ExitCode.SUCCESS, code)).validate()


def test_the_default_declaration_and_the_full_subset_validate():
    StageDeclaration(
        name="admit", unit="exposure", argument_schema={}, settings_schema_path=None,
        consumes=(), produces=("exposure",), database_access="none").validate()
    _declaration(STAGE_EXIT_CODES).validate()


def test_the_module_imports_only_the_standard_library():
    source = Path(exitcodes.__file__).read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.level == 0
            imported.add(node.module.split(".")[0])
    assert "rapidpipe" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_a_fresh_import_loads_no_other_rapidpipe_module():
    code = ("import sys, rapidpipe.exitcodes; "
            "print(sorted(m for m in sys.modules if m.startswith('rapidpipe')))")
    out = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[2]).stdout.strip()
    # The package __init__ is imported on the way; nothing else may be.
    assert out == "['rapidpipe', 'rapidpipe.exitcodes']"


# ======================================================================
# The parser class
# ======================================================================

def _all_parsers(parser: argparse.ArgumentParser):
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from _all_parsers(child)


@pytest.mark.parametrize("build", [cli._build_parser, release_main.build_parser],
                         ids=["rapidpipe", "python-m-rapidpipe.release"])
def test_every_parser_in_the_tree_is_ours(build):
    parsers = list(_all_parsers(build()))
    assert len(parsers) > 1
    strays = [p.prog for p in parsers if not isinstance(p, ArgumentParser)]
    assert strays == []


def test_the_stage_parser_is_ours():
    declaration = _declaration(STAGE_EXIT_CODES)
    assert isinstance(contract._build_parser(declaration), ArgumentParser)


def test_a_parse_failure_exits_64_in_argparse_shape(capsys):
    parser = ArgumentParser(prog="demo")
    parser.add_argument("--n", type=int)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--n", "x"])
    assert exc.value.code == 64
    err = capsys.readouterr().err
    assert err.startswith("usage: demo")
    assert "demo: error: argument --n: invalid int value: 'x'" in err


@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"], ["check", "list", "--help"]])
def test_help_still_exits_0(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0


@pytest.mark.parametrize("argv", [["no-such-command"], ["run", "show"], ["check", "bogus"]])
def test_parse_failures_exit_64_through_the_cli(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == int(ExitCode.USAGE)


def test_a_release_parse_failure_exits_64(capsys):
    with pytest.raises(SystemExit) as exc:
        release_main.main(["cut", "--no-such-flag"])
    assert exc.value.code == int(ExitCode.USAGE)


# ======================================================================
# The unexpected-error boundary
# ======================================================================

@pytest.fixture()
def fresh_logging(monkeypatch):
    """Undo any earlier in-process stage run's ``rapidpipe.log.configure``
    (a handler bound to an earlier test's stderr, propagate off), so the
    boundary's log line reaches this test's captured stderr."""
    logger = logging.getLogger("rapidpipe")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)


def _boom(*_a, **_k):
    raise RuntimeError("boom from a handler")


def test_cli_maps_an_unexpected_exception_to_70(monkeypatch, capsys, fresh_logging):
    monkeypatch.setattr(checkctl, "dispatch", _boom)
    assert cli.main(["check", "list"]) == int(ExitCode.STAGE_ERROR)
    err = capsys.readouterr().err
    assert "rapidpipe check: unexpected error" in err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: boom from a handler" in err


@pytest.mark.parametrize("entry", ["release_main", "cli"])
def test_release_maps_an_unexpected_exception_to_70(entry, monkeypatch, capsys,
                                                    fresh_logging):
    monkeypatch.setattr(release_main, "connect",
                        lambda **_kw: contextlib.nullcontext(None))
    monkeypatch.setattr(release_main, "_run", _boom)
    if entry == "release_main":
        rc = release_main.main(["list"])
    else:
        rc = cli.main(["release", "list"])
    assert rc == int(ExitCode.STAGE_ERROR)
    err = capsys.readouterr().err
    assert "unexpected error" in err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: boom from a handler" in err


def test_the_boundary_never_swallows_keyboard_interrupt(monkeypatch):
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(checkctl, "dispatch", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["check", "list"])


# ======================================================================
# Database connection errors reach 64/75, not 70 (supervisor step 1,
# 2026-09-26): ``connect()`` is a @contextmanager generator, so
# ConnectionConfigError/ConnectionUnavailable raise at ``__enter__``
# (the ``with cm as conn:`` line), not at the ``connect(...)`` call --
# every per-command ``try: cm = connect(...) except ...`` block is dead.
# ======================================================================

class _RaisingConnect:
    """A ``connect()`` replacement whose returned context manager raises
    on ``__enter__``, matching where the real generator-based context
    manager raises."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, **_kw):
        return self

    def __enter__(self):
        raise self._exc

    def __exit__(self, *_exc_info):
        return False


@pytest.mark.parametrize("exc_type, code, word", [
    (ConnectionConfigError, ExitCode.USAGE, "configuration error"),
    (ConnectionUnavailable, ExitCode.TRANSIENT_FAILURE, "unavailable"),
])
@pytest.mark.parametrize("argv, command", [
    (["run", "create", "--kind", "scratch", "--purpose", "p", "--stages", "admit"], "run"),
    (["check", "show", "run-1"], "check"),
    (["loop", "show", "schedule-1"], "loop"),
])
def test_cli_classifies_a_connection_error_at_the_boundary(
        monkeypatch, capsys, argv, command, exc_type, code, word):
    monkeypatch.setattr(cli, "connect", _RaisingConnect(exc_type("boom")))
    rc = cli.main(argv)
    assert rc == int(code)
    err = capsys.readouterr().err
    assert f"rapidpipe {command}: database {word}: boom" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("exc_type, code, word", [
    (ConnectionConfigError, ExitCode.USAGE, "configuration error"),
    (ConnectionUnavailable, ExitCode.TRANSIENT_FAILURE, "unavailable"),
])
@pytest.mark.parametrize("entry", ["release_main", "cli"])
def test_release_classifies_a_connection_error_at_the_with_block(
        entry, monkeypatch, capsys, exc_type, code, word):
    monkeypatch.setattr(release_main, "connect", _RaisingConnect(exc_type("boom")))
    if entry == "release_main":
        rc = release_main.main(["list"])
    else:
        rc = cli.main(["release", "list"])
    assert rc == int(code)
    err = capsys.readouterr().err
    assert f"rapidpipe release list: database {word}: boom" in err
    assert "Traceback" not in err


# ======================================================================
# selftest's own exit sites
# ======================================================================

def _fixture_result(tmp_path, *, exit_code, failures):
    checks = Checks()
    for label in failures:
        checks.check(False, label)
    return FixtureResult(
        stage="photometry", tools="fake", exit_code=exit_code, checks=checks,
        work_dir=tmp_path, output_location=str(tmp_path / "outputs"),
        output_is_local=True)


def test_selftest_a_stage_exiting_0_when_69_was_expected_is_a_failure(tmp_path, capsys):
    result = _fixture_result(
        tmp_path, exit_code=0, failures=["exit code: expected 69, got 0"])
    assert _report("photometry", result) == int(ExitCode.FAILURE)


def test_selftest_an_unexpected_nonzero_stage_exit_propagates(tmp_path, capsys):
    result = _fixture_result(
        tmp_path, exit_code=70, failures=["exit code: expected 0, got 70"])
    assert _report("photometry", result) == int(ExitCode.STAGE_ERROR)


@pytest.mark.parametrize("failures, expected", [
    ([], ExitCode.SUCCESS),
    (["manifest: outputs differ"], ExitCode.FAILURE),
])
def test_selftest_pass_and_fixture_mismatch(tmp_path, capsys, failures, expected):
    result = _fixture_result(tmp_path, exit_code=0, failures=failures)
    assert _report("photometry", result) == int(expected)


def test_selftest_an_existing_work_dir_is_a_usage_error(monkeypatch, tmp_path, capsys):
    import rapidpipe.selftest as selftest

    def exists(*_a, **_k):
        raise FileExistsError(f"{tmp_path} already exists")

    monkeypatch.setattr(selftest, "run_fixture", exists)
    rc = selftest.run(stage="photometry", real_tools=False, work_dir=str(tmp_path),
                      output_location=None)
    assert rc == int(ExitCode.USAGE)
