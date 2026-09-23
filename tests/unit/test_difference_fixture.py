"""The difference stage's fixture, run as `make stage-difference` runs it (fake tools).

Runs tests/fixtures/difference/run_fixture.py in-process: it prepares an
isolated directory, runs the stage as a subprocess with
RAPIDPIPE_DIFFERENCE_TOOLKIT selecting the fakes, and checks the manifest
and products against expected.json. Also covers that hook itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import rapidpipe.stages.difference as difference
from rapidpipe.stages.contract import UsageError

from .fakedifftools import FakeToolRunner

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "difference" / "run_fixture.py"


def _run_fixture_module():
    spec = importlib.util.spec_from_file_location("run_fixture", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_stage_difference_passes_with_fake_tools(tmp_path, capsys):
    assert _run_fixture_module().main(["--tools", "fake", "--workdir", str(tmp_path / "fx")]) == 0
    assert "stage-difference: PASS" in capsys.readouterr().out


def test_fixture_refuses_a_non_empty_workdir(tmp_path):
    (tmp_path / "stale").write_text("x")
    assert _run_fixture_module().main(["--workdir", str(tmp_path)]) == 1


def test_toolkit_defaults_to_the_real_tools(monkeypatch):
    monkeypatch.delenv(difference.TOOLKIT_ENV, raising=False)
    kit = difference.toolkit()
    assert type(kit.runner).__name__ == "ToolRunner"


def test_toolkit_environment_hook_selects_the_fakes(monkeypatch):
    monkeypatch.setenv(difference.TOOLKIT_ENV, "tests.unit.fakedifftools:fake_toolkit")
    # By name: under importlib import mode the hook imports its own copy.
    assert type(difference.toolkit().runner).__name__ == FakeToolRunner.__name__


def test_toolkit_environment_hook_refuses_a_bad_name(monkeypatch):
    monkeypatch.setenv(difference.TOOLKIT_ENV, "tests.unit.fakedifftools:no_such_factory")
    with pytest.raises(UsageError):
        difference.toolkit()
