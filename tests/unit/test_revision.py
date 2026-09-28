"""Tests for rapidpipe.revision.git_revision: the git-only lookup shared
by rapidpipe.cli.main, rapidpipe.runs.local and rapidpipe.stages.contract,
each of which applies its own fallback when this returns None."""

from __future__ import annotations

import rapidpipe.revision as revision_module


def test_git_revision_uses_git_when_available(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 0
        stdout = "abc123\n"

    monkeypatch.setattr(
        revision_module.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess())

    assert revision_module.git_revision() == "abc123"


def test_git_revision_none_when_git_exits_nonzero(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(
        revision_module.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess())

    assert revision_module.git_revision() is None


def test_git_revision_none_when_git_not_installed(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(revision_module.subprocess, "run", _raise)

    assert revision_module.git_revision() is None


def test_git_revision_none_when_output_empty(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 0
        stdout = "\n"

    monkeypatch.setattr(
        revision_module.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess())

    assert revision_module.git_revision() is None
