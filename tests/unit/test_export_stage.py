"""Tests for rapidpipe.stages.export: a declared stub, exit code 69.

export is not ported in this build (supervisor step 8, 2026-09-24, ruling
R9): every check here is about the contract around the stub --
argument/settings/input-manifest validation still runs and still maps to
64/65 as a real stage's would, and only a fully valid invocation reaches
the stub's fixed exit 69, writing no manifest and opening no database
connection. The fixture itself (``make stage-export``, ``rapidpipe
selftest --stage export``) runs at the end.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import rapidpipe.stages.export as export
from rapidpipe.runs.local import disposition_for
from rapidpipe.selftest.support.fakeexport import (
    ASSOCIATION_SET_INSTANCE,
    SOURCE_SET_INSTANCE,
    STATISTICS_SET_INSTANCE,
    UNIT_ID,
    build_export_input_set,
)
from rapidpipe.stages.contract import ExitCode

RUN = "01J8Y6QZ3M0000000000000RUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"


def _argv(inputs: Path, outputs: Path, *extra: str) -> list[str]:
    return ["--run", RUN, "--unit", UNIT_ID, "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(outputs), *extra]


def _run(tmp_path: Path, *extra: str, **build) -> tuple[int, Path]:
    inputs = tmp_path / "inputs"
    if not (inputs / "manifest.json").exists():
        build_export_input_set(inputs, **build)
    outputs = tmp_path / "outputs"
    return export.main(_argv(inputs, outputs, *extra)), outputs


def test_declaration():
    export.DECLARATION.validate()
    d = export.DECLARATION
    assert (d.name, d.unit, d.database_access) == ("export", "field", "read")
    assert d.consumes == ("association-set", "statistics-set", "source-set")
    assert d.produces == ("catalog-export",)
    assert ExitCode.NOT_IMPLEMENTED in d.supported_exit_codes


def test_declaration_import_performs_no_io(monkeypatch):
    def _raise(*args, **kwargs):
        raise AssertionError("export module import must not read any file")
    monkeypatch.setattr(Path, "open", _raise)
    monkeypatch.setattr(Path, "read_text", _raise)
    importlib.reload(export)
    assert export.DECLARATION.name == "export"


def test_dry_run_exits_0_and_writes_nothing(tmp_path):
    rc, outputs = _run(tmp_path, "--dry-run")
    assert rc == int(ExitCode.SUCCESS)
    assert not outputs.exists() or not any(outputs.iterdir())


def test_dry_run_missing_result_sets_exits_65(tmp_path):
    # --dry-run runs the same named-result-set check as a real invocation
    # (run_stage's validate_inputs hook), so an input manifest naming no
    # result sets is rejected before the "dry-run validated" success path.
    rc, outputs = _run(tmp_path, "--dry-run", result_sets=())
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not outputs.exists() or not any(outputs.iterdir())


def test_valid_invocation_exits_69_and_writes_no_manifest(tmp_path):
    rc, outputs = _run(tmp_path)
    assert rc == int(ExitCode.NOT_IMPLEMENTED) == 69
    assert not (outputs / "manifest.json").exists()
    assert disposition_for(rc, manifest_ok=False) == "failed"


@pytest.mark.parametrize("result_sets", [
    (SOURCE_SET_INSTANCE,),
    (ASSOCIATION_SET_INSTANCE, STATISTICS_SET_INSTANCE),
    (SOURCE_SET_INSTANCE, ASSOCIATION_SET_INSTANCE, STATISTICS_SET_INSTANCE),
])
def test_any_nonempty_result_set_list_exits_69(tmp_path, result_sets):
    rc, outputs = _run(tmp_path, result_sets=result_sets)
    assert rc == int(ExitCode.NOT_IMPLEMENTED)
    assert not (outputs / "manifest.json").exists()


@pytest.mark.parametrize("overlay", [
    "[export]\nunknown_key = 1\n",
    "[export]\ncatalog_type = \"objects\"\n",
    "[hats]\nlowest_healpix_order = -1\n",
    "[hats]\nhighest_healpix_order = 20\n",
    "[hats]\nlowest_healpix_order = 9\nhighest_healpix_order = 3\n",
    "[hats]\nn_workers = 0\n",
    "[hats]\nra_col = \"\"\n",
])
def test_bad_settings_exit_64(tmp_path, overlay):
    inputs = tmp_path / "inputs"
    build_export_input_set(inputs)
    overlay_path = tmp_path / "overlay.toml"
    overlay_path.write_text(overlay)
    rc, outputs = _run(tmp_path, "--settings", str(overlay_path))
    assert rc == int(ExitCode.USAGE)
    assert not (outputs / "manifest.json").exists()


def test_missing_inputs_manifest_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True)
    outputs = tmp_path / "outputs"
    rc = export.main(_argv(inputs, outputs))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_wrong_unit_kind_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_export_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["unit"]["kind"] = "detector-image"
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_not_an_input_set_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_export_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["stage"] = "crossmatch"
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_no_named_result_sets_exits_65(tmp_path):
    rc, _ = _run(tmp_path, result_sets=())
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_repeated_result_set_exits_65(tmp_path):
    rc, _ = _run(tmp_path, result_sets=(SOURCE_SET_INSTANCE, SOURCE_SET_INSTANCE))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_empty_result_set_id_exits_65(tmp_path):
    rc, _ = _run(tmp_path, result_sets=(SOURCE_SET_INSTANCE, ""))
    assert rc == int(ExitCode.INPUT_REJECTED)
