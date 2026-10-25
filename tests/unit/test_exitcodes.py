"""``rapidpipe.exitcodes``: the one exit-code vocabulary, the parser class
every entrypoint uses, and the command line's unexpected-error boundary (tool.md §Exit codes)."""

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
from rapidpipe.cli import main as cli
from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.exitcodes import ArgumentParser, ExitCode
from rapidpipe import foundation


# ======================================================================
# The vocabulary
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


def test_every_parser_in_the_tree_is_ours():
    parsers = list(_all_parsers(cli._build_parser()))
    assert len(parsers) > 1
    strays = [p.prog for p in parsers if not isinstance(p, ArgumentParser)]
    assert strays == []


def test_a_parse_failure_exits_64_in_argparse_shape(capsys):
    parser = ArgumentParser(prog="demo")
    parser.add_argument("--n", type=int)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--n", "x"])
    assert exc.value.code == 64
    err = capsys.readouterr().err
    assert err.startswith("usage: demo")
    assert "demo: error: argument --n: invalid int value: 'x'" in err


@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"], ["official", "change", "--help"]])
def test_help_still_exits_0(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0


@pytest.mark.parametrize("argv", [["no-such-command"], ["run", "show"], ["official", "bogus"]])
def test_parse_failures_exit_64_through_the_cli(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
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
    monkeypatch.setattr(cli, "connect", lambda **_kw: contextlib.nullcontext(None))
    monkeypatch.setattr(foundation, "run_show", _boom)
    assert cli.main(["run", "show", "1"]) == int(ExitCode.STAGE_ERROR)
    err = capsys.readouterr().err
    assert "rapidpipe run show: unexpected error" in err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: boom from a handler" in err


def test_the_boundary_never_swallows_keyboard_interrupt(monkeypatch):
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "connect", lambda **_kw: contextlib.nullcontext(None))
    monkeypatch.setattr(foundation, "run_show", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["run", "show", "1"])


# ======================================================================
# Database connection errors reach 64/75, not 70: ``connect()`` is a
# @contextmanager generator, so
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
    (["run", "show", "1"], "run show"),
    (["job", "show", "1"], "job show"),
    (["official", "undo", "1", "--who", "w", "--why", "y"], "official undo"),
])
def test_cli_classifies_a_connection_error_at_the_boundary(
        monkeypatch, capsys, argv, command, exc_type, code, word):
    monkeypatch.setattr(cli, "connect", _RaisingConnect(exc_type("boom")))
    rc = cli.main(argv)
    assert rc == int(code)
    err = capsys.readouterr().err
    assert f"rapidpipe {command}: database {word}: boom" in err
    assert "Traceback" not in err
