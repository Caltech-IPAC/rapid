"""``rapidpipe --help`` and every subcommand's ``--help``: no database.

Walks ``rapidpipe.cli.main._build_parser()`` recursively over every
``argparse._SubParsersAction`` choice (``run promote``, ``release cut``,
and so on), and separately checks the module runs the same way as
``python -m rapidpipe.cli.main`` and, when installed, as the ``rapidpipe``
console script.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

import pytest

from rapidpipe.cli.main import _build_parser, main


def _iter_subparsers(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """Yield ``(path, subparser)`` for every subparser reachable from
    ``parser``, including nested ones (``run`` -> ``run promote``)."""
    for action in parser._subparsers._group_actions if parser._subparsers else []:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            child_path = path + (name,)
            yield child_path, subparser
            yield from _iter_subparsers(subparser, child_path)


def _all_subparser_paths():
    parser = _build_parser()
    return list(_iter_subparsers(parser))


@pytest.mark.parametrize(
    "path", [p for p, _ in _all_subparser_paths()], ids=lambda p: " ".join(p))
def test_every_subcommand_help_exits_0(path, capsys):
    # rapidpipe stage's own subparser has add_help=False (today): --help
    # is not a recognised option there, so argparse refuses it with exit
    # 2 rather than printing help and exiting 0. Tracked as an expected
    # failure, not silently skipped, so the fix (branch step4-cli) turns
    # this green without anyone having to notice a skip went away.
    # Resolve the actual subparser object for this path directly, since
    # _iter_subparsers's own parser instances are throwaway per call.
    target = _build_parser()
    for name in path:
        found = None
        for action in target._subparsers._group_actions:
            if isinstance(action, argparse._SubParsersAction) and name in action.choices:
                found = action.choices[name]
                break
        assert found is not None, f"{name!r} not found on the way to {path}"
        target = found

    if target.add_help is False:
        with pytest.raises(SystemExit) as exc_info:
            main(list(path) + ["--help"])
        pytest.xfail(
            f"rapidpipe {' '.join(path)} --help: add_help=False on this "
            f"subparser (exit {exc_info.value.code}); fixed on branch "
            "step4-cli (supervisor step 4)")

    with pytest.raises(SystemExit) as exc_info:
        main(list(path) + ["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip()


def test_every_subparser_has_a_non_empty_help_string():
    missing = [
        " ".join(path) for path, subparser in _all_subparser_paths()
        if not (subparser.description or "").strip()
        and not _parent_help_text(path)
    ]
    # A subparser's own descriptive text lives on the *parent* parser's
    # add_parser(..., help=...), not on the child parser object itself
    # (argparse stores it as one of the parent's _SubParsersAction
    # choices' pseudo-action help strings) -- checked directly below,
    # this helper exists only so the assertion message names every
    # offender at once rather than stopping at the first.
    assert missing == []


def _parent_help_text(path: tuple[str, ...]) -> str:
    """The ``help=`` text ``add_parser`` recorded for the subcommand at
    ``path``, by walking parent -> child through each level's
    ``_SubParsersAction``."""
    parser = _build_parser()
    text = ""
    for name in path:
        subparsers_action = None
        for action in parser._subparsers._group_actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
                break
        assert subparsers_action is not None
        text = ""
        for choice_action in subparsers_action._choices_actions:
            if choice_action.dest == name:
                text = choice_action.help or ""
                break
        parser = subparsers_action.choices[name]
    return text


def test_top_level_help_lists_stage_selftest_and_run(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "stage" in out
    assert "selftest" in out
    assert "run" in out


def test_bare_invocation_prints_help_and_exits_0(capsys):
    rc = main([])
    assert rc == 0
    assert "rapidpipe" in capsys.readouterr().out


# ======================================================================
# Subprocess pass: the module and (if installed) the console script.
# ======================================================================

def test_module_invocation_help_exits_0():
    result = subprocess.run(
        [sys.executable, "-m", "rapidpipe.cli.main", "--help"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stdout.strip()


def test_module_invocation_run_help_exits_0():
    result = subprocess.run(
        [sys.executable, "-m", "rapidpipe.cli.main", "run", "--help"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stdout.strip()


def test_installed_entrypoint_help_exits_0():
    rapidpipe = shutil.which("rapidpipe")
    if rapidpipe is None:
        pytest.skip("rapidpipe console script is not on PATH (package not installed)")
    result = subprocess.run(
        [rapidpipe, "--help"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stdout.strip()
