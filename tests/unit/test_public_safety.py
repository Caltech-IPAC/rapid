"""scripts/check-public-safety.sh reads ``git grep``'s status, not only its output.

Contracts: a tree with no match (``git grep`` 1) is clean, exit 0; a match
(``git grep`` 0) is reported, exit 1; a scan ``git grep`` cannot run (a bad
ref, ``git grep`` 128) fails, exit 2, never reading as clean.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-public-safety.sh"


@pytest.fixture()
def repo(tmp_path):
    """A one-commit repository holding the script and ``notes.txt``; give a writer that commits it."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path,
                       check=True, capture_output=True)

    def commit(text: str) -> Path:
        (tmp_path / "notes.txt").write_text(text)
        git("add", "-A")
        git("commit", "-q", "-m", "notes", "--allow-empty")
        return tmp_path

    git("init", "-q")
    return commit


def scan(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/check-public-safety.sh", *args], cwd=root,
                          capture_output=True, text=True)


def test_no_match_is_clean(repo):
    root = repo("nothing to see\n")
    assert scan(root).returncode == 0 and scan(root, "HEAD").returncode == 0


def test_a_match_is_reported(repo):
    root = repo("written under " + "/Us" "ers/someone/work\n")     # in fragments: this file is scanned too
    for result in (scan(root), scan(root, "HEAD")):
        assert result.returncode == 1 and "BLOCKED [users-path] notes.txt:1" in result.stderr


def test_a_scan_git_grep_cannot_run_fails(repo):
    result = scan(repo("nothing to see\n"), "no-such-ref")
    assert result.returncode == 2 and "git grep failed (exit 128)" in result.stderr
