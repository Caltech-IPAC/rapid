"""Black-box exit-code coverage for every ``rapidpipe`` command family:
every argparse parse failure exits
:data:`ExitCode.USAGE` (64), ``--help`` exits 0 everywhere, and the CLI's
own runtime refusals map onto the vocabulary in ``rapidpipe.exitcodes``
(SUCCESS/FAILURE/INCOMPLETE/USAGE/INPUT_REJECTED/NOT_IMPLEMENTED/
STAGE_ERROR/TRANSIENT_FAILURE). Assertions are against the names, never
bare numbers, except the two below whose whole point is the number.

Section 1 needs no database and must pass locally (no ``PGHOST``, no
``cli``/``db`` fixtures -- those skip without one); section 2 is
database-backed and skips the same way the rest of this suite does (see
``tests/cli/README.md``).
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rapidpipe.cli import main as cli_main
from rapidpipe.db.ids import new_ulid
from rapidpipe.exitcodes import ExitCode
from rapidpipe.release import __main__ as release_main
from tests.unit.fakereleasedb import FakeReleaseDB
from tests.unit.releaserepo import isolate_git, make_release_repo, write_hooks

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    """Call ``rapidpipe.cli.main.main`` in-process, catching ``SystemExit``
    (an argparse parse failure or ``--help``) the way ``conftest.py``'s
    ``cli`` fixture does, so both the ``SystemExit`` and plain-``return``
    dispatch paths (``release`` with no subcommand; ``stage run`` forwarding
    to a stage's own argparse) are asserted on the same shape."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main.main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return rc, out.getvalue(), err.getvalue()


# ======================================================================
# 1a. top-level malformed arguments (SystemExit, no database)
# ======================================================================

@pytest.mark.parametrize("argv", [["--bogus"], ["frobnicate"]],
                         ids=["unknown-flag", "unknown-subcommand"])
def test_top_level_malformed_argv_exits_usage(argv):
    """Vocabulary: every argparse parse failure at the top level exits
    ExitCode.USAGE (64), with 'usage:' and 'error:' on stderr."""
    rc, _out, err = _run_main(argv)
    assert rc == ExitCode.USAGE
    assert "usage:" in err
    assert "error:" in err


# ======================================================================
# 1b. nested malformed arguments, one case each
# ======================================================================

@pytest.mark.parametrize("argv", [
    ["run", "--bogus"],
    ["run", "create", "--kind", "bogus", "--purpose", "p", "--stages", "admit"],
    ["release", "cut", "--bogus"],
    ["loop", "--bogus"],
    ["check", "--bogus"],
    ["stage", "describe", "bogus"],
], ids=["run", "run-create-bad-choice", "release-cut", "loop", "check", "stage-describe"])
def test_nested_malformed_argv_exits_usage(argv):
    """Vocabulary: a parse failure nested inside any command group -- an
    unrecognized flag at the group level, or an invalid --kind/stage-name
    choice -- exits ExitCode.USAGE (64) the same as a top-level one.

    'run create' has no case here for a missing required positional/flag:
    none of --kind/--purpose/--stages is argparse ``required=True`` in
    main.py's create_parser (the requiredness is a runtime check --
    ExitCode.USAGE via a hand-written message -- exercised already by
    test_run_lifecycle.py), so there is no argparse-level failure to add.
    """
    rc, _out, err = _run_main(argv)
    assert rc == ExitCode.USAGE
    assert "error:" in err


def test_release_with_no_subcommand_exits_usage_from_the_dispatcher():
    """Vocabulary: 'release' alone exits ExitCode.USAGE (64) too, but from
    release.__main__.dispatch's own check (a plain return), not argparse
    -- no SystemExit is raised."""
    rc, _out, err = _run_main(["release"])
    assert rc == ExitCode.USAGE
    assert "subcommand is required" in err


def test_stage_run_bogus_flag_is_already_translated_to_usage():
    """Vocabulary: 'stage run admit --bogus' is forwarded to admit's own
    argparse; rapidpipe.stages.contract's run_stage catches admit's
    SystemExit itself and returns ExitCode.USAGE (64) as a plain int --
    it never leaks out as a SystemExit here."""
    rc, _out, err = _run_main(["stage", "run", "admit", "--bogus"])
    assert rc == ExitCode.USAGE
    assert "error:" in err


# ======================================================================
# 1c. --help exits 0 with non-empty stdout (the explicit handful; the
# full subparser walk is tests/cli/test_help.py's job, not this one's)
# ======================================================================

@pytest.mark.parametrize("argv", [
    [], ["run"], ["run", "create"], ["release"], ["release", "cut"],
    ["loop"], ["check"], ["stage"],
], ids=["top", "run", "run-create", "release", "release-cut", "loop", "check", "stage"])
def test_help_exits_success_with_output(argv):
    """Vocabulary: --help exits ExitCode.SUCCESS (0) at every level, with
    something printed to stdout."""
    rc, out, _err = _run_main(argv + ["--help"])
    assert rc == ExitCode.SUCCESS
    assert out.strip()


# ======================================================================
# 1d. both release entrypoints, by subprocess
# ======================================================================

def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("PGHOST", None)
    return env


def _run_subprocess(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=str(REPO_ROOT), env=_subprocess_env(),
        capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("args, expect_rc", [
    (("-m", "rapidpipe.release", "--bogus"), ExitCode.USAGE),
    (("-m", "rapidpipe.release"), ExitCode.USAGE),
    (("-m", "rapidpipe.release", "--help"), ExitCode.SUCCESS),
    (("-m", "rapidpipe.cli.main", "release", "--bogus"), ExitCode.USAGE),
    (("-m", "rapidpipe.cli.main", "release"), ExitCode.USAGE),
    (("-m", "rapidpipe.cli.main", "--bogus"), ExitCode.USAGE),
    (("-m", "rapidpipe.cli.main", "--help"), ExitCode.SUCCESS),
], ids=[
    "release-module-bogus", "release-module-no-subcommand", "release-module-help",
    "cli-main-release-bogus", "cli-main-release-no-subcommand",
    "cli-main-bogus", "cli-main-help",
])
def test_subprocess_entrypoints_exit_code(args, expect_rc):
    """Vocabulary: 'python -m rapidpipe.release' and 'python -m
    rapidpipe.cli.main release' are one code path (the module docstring),
    checked as real subprocesses (sys.executable, PGHOST removed so
    nothing tries to connect) rather than in-process."""
    result = _run_subprocess(*args)
    assert result.returncode == expect_rc, result.stderr


# ======================================================================
# 1e. hook mapping, CLI-visible form (no database: FakeReleaseDB)
# ======================================================================

def test_release_cut_hook_exit_is_mapped_to_failure(tmp_path, monkeypatch):
    """Vocabulary: a release hook exiting non-zero -- even a code that is
    itself meaningful elsewhere in this vocabulary, like 64 -- makes
    'release cut' exit ExitCode.FAILURE (1); rapidpipe.release.hooks.
    run_hook always raises HookFailed (exit_code 1) on a non-zero exit,
    regardless of what that literal code was.

    The underlying mapping (a hook exiting 1/2/64/75, and a bad result
    line, all raising HookFailed) is tests/unit/test_release_cut.py's and
    test_release_parts.py's job; this is the
    one CLI-dispatch form of it, built the way
    tests/unit/test_release_parts.py's test_run_submit_exits_1_on_a_
    release_refusal monkeypatches connect and calls main() directly.
    """
    isolate_git(monkeypatch, tmp_path)
    repo = make_release_repo(tmp_path)
    hooks = write_hooks(tmp_path / "hooks", tmp_path / "order.log")
    migrate = hooks / "migrate"
    migrate.write_text("#!/bin/sh\nexit 64\n")
    migrate.chmod(migrate.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    db = FakeReleaseDB()
    monkeypatch.setattr(release_main, "connect", lambda **_kw: contextlib.nullcontext(db))

    rc, _out, err = _run_main(
        ["release", "cut", "--repo", str(repo), "--hooks-dir", str(hooks), "--by", "tester"])
    assert rc == ExitCode.FAILURE
    assert "exited 64" in err


# ======================================================================
# the two bare-number assertions the vocabulary's own numbers are about
# ======================================================================

def test_the_two_numbers_the_vocabulary_is_named_for():
    """The one place a bare number, not a name, is the point."""
    assert int(ExitCode.USAGE) == 64
    assert int(ExitCode.INCOMPLETE) == 2


# ======================================================================
# 2. database-backed (skip cleanly without PGHOST, as elsewhere in this
# suite)
# ======================================================================

def test_run_create_release_not_complete_exits_usage_and_creates_no_run(cli, db):
    """Vocabulary: 'run create --release TAG' whose releases row is not
    'complete' exits ExitCode.USAGE (64) with 'not complete' on stderr,
    whether the row is in an earlier state ('built') or does not exist at
    all -- and creates no run row either way."""
    built_tag = f"cli-exitcode-built-{new_ulid()}"
    absent_tag = f"cli-exitcode-absent-{new_ulid()}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, state, cut_by) "
            "VALUES (%s, %s, '1', 'built', 'cli-test')", (built_tag, "a" * 40))
    db.commit()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM runs")
            before = cur.fetchone()[0]

        built = cli("run", "create", "--kind", "scratch", "--purpose", "p",
                    "--stages", "admit", "--release", built_tag)
        assert built.rc == ExitCode.USAGE
        assert "not complete" in built.err

        absent = cli("run", "create", "--kind", "scratch", "--purpose", "p",
                     "--stages", "admit", "--release", absent_tag)
        assert absent.rc == ExitCode.USAGE
        assert "not complete" in absent.err

        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM runs")
            after = cur.fetchone()[0]
        assert after == before
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM releases WHERE tag = %s", (built_tag,))
        db.commit()


def test_run_show_of_a_missing_run_exits_failure(cli):
    """Vocabulary: a negative outcome ('run show' of a run id that names
    no run) exits ExitCode.FAILURE (1), not USAGE -- the run id is
    well-formed, just wrong."""
    result = cli("run", "show", f"no-such-run-{new_ulid()}")
    assert result.rc == ExitCode.FAILURE


# A failed check exiting ExitCode.FAILURE (1) is proved by
# tests/cli/test_checks.py's test_check_run_trial_passes_strict_fails_
# and_show_lists_newest_first (its 'strict.rc == 1' assertion): its
# fixture chain (_make_run/_diff_candidate/_catalog_source_set building a
# full difference-image + source-set candidate pair) is not cheap enough
# to justify rebuilding here just to rename one assertion's numbers.


# ======================================================================
# 'run status' without --watch on a non-terminal unit: INCOMPLETE (2),
# not a bare number -- test_run_status_compare_expire.py's own
# still_running.rc == 2 case, its assertion re-spelled against the name
# rather than duplicated as a new test (simpler than building the same
# submit/reconcile fixture chain twice).
# ======================================================================
