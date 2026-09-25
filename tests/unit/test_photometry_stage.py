"""Tests for rapidpipe.stages.photometry: a declared stub, exit code 69.

photometry is not ported in this build (supervisor step 8, 2026-09-24,
ruling R9): every check here is about the contract around the stub --
argument/settings/input-manifest validation still runs and still maps to
64/65 as a real stage's would, and only a fully valid invocation reaches
the stub's fixed exit 69, writing no manifest. The fixture itself (``make
stage-photometry``, ``rapidpipe selftest --stage photometry``) runs at
the end.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import rapidpipe.stages.photometry as photometry
from rapidpipe.runs.local import disposition_for
from rapidpipe.selftest.support.fakephotometry import (
    OBJECT_SET_INSTANCE,
    UNIT_ID,
    build_photometry_input_set,
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
        build_photometry_input_set(inputs, **build)
    outputs = tmp_path / "outputs"
    return photometry.main(_argv(inputs, outputs, *extra)), outputs


def test_declaration():
    photometry.DECLARATION.validate()
    d = photometry.DECLARATION
    assert (d.name, d.unit, d.database_access) == ("photometry", "field", "none")
    assert d.consumes == ("difference-image", "psf", "statistics-set", "association-set")
    assert d.produces == ("light-curve",)
    assert ExitCode.NOT_IMPLEMENTED in d.supported_exit_codes


def test_declaration_import_performs_no_io(monkeypatch):
    def _raise(*args, **kwargs):
        raise AssertionError("photometry module import must not read any file")
    monkeypatch.setattr(Path, "open", _raise)
    monkeypatch.setattr(Path, "read_text", _raise)
    importlib.reload(photometry)
    assert photometry.DECLARATION.name == "photometry"


def test_dry_run_exits_0_and_writes_nothing(tmp_path):
    rc, outputs = _run(tmp_path, "--dry-run")
    assert rc == int(ExitCode.SUCCESS)
    assert not outputs.exists() or not any(outputs.iterdir())


def test_dry_run_missing_kind_exits_65(tmp_path):
    # --dry-run runs the same input-set shape check as a real invocation
    # (run_stage's validate_inputs hook), so a missing declared kind is
    # rejected before the "dry-run validated" success path, not after.
    inputs = tmp_path / "inputs"
    path = build_photometry_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["outputs"] = [e for e in manifest["outputs"] if e["kind"] != "psf"]
    path.write_text(json.dumps(manifest))
    rc, outputs = _run(tmp_path, "--dry-run")
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not outputs.exists() or not any(outputs.iterdir())


def test_valid_invocation_exits_69_and_writes_no_manifest(tmp_path):
    rc, outputs = _run(tmp_path)
    assert rc == int(ExitCode.NOT_IMPLEMENTED) == 69
    assert not (outputs / "manifest.json").exists()
    assert disposition_for(rc, manifest_ok=False) == "failed"


def test_multiple_epochs_and_psfs_still_exit_69(tmp_path):
    rc, outputs = _run(tmp_path, extra_differences=2, extra_psfs=1)
    assert rc == int(ExitCode.NOT_IMPLEMENTED)
    assert not (outputs / "manifest.json").exists()


@pytest.mark.parametrize("overlay", [
    "[photometry]\nunknown_key = 1\n",
    "[photometry]\npsf_source = \"nowhere\"\n",
    "[photometry]\nstampsz = 0\n",
    "[photometry]\napdiam = -1.0\n",
    "[photometry]\nmaxbadpixfrac = 1.5\n",
    "[photometry]\napplyflxcorr = 1\n",
])
def test_bad_settings_exit_64(tmp_path, overlay):
    inputs = tmp_path / "inputs"
    build_photometry_input_set(inputs)
    overlay_path = tmp_path / "overlay.toml"
    overlay_path.write_text(overlay)
    rc, outputs = _run(tmp_path, "--settings", str(overlay_path))
    assert rc == int(ExitCode.USAGE)
    assert not (outputs / "manifest.json").exists()


def test_missing_inputs_manifest_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True)
    outputs = tmp_path / "outputs"
    rc = photometry.main(_argv(inputs, outputs))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_wrong_unit_kind_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_photometry_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["unit"]["kind"] = "detector-image"
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_not_an_input_set_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_photometry_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["stage"] = "difference"
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_no_difference_image_entries_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_photometry_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["outputs"] = [e for e in manifest["outputs"] if e["kind"] != "difference-image"]
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_no_psf_entries_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_photometry_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["outputs"] = [e for e in manifest["outputs"] if e["kind"] != "psf"]
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_no_named_object_set_exits_65(tmp_path):
    rc, _ = _run(tmp_path, named_result_sets=())
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_two_named_object_sets_exits_65(tmp_path):
    rc, _ = _run(tmp_path, named_result_sets=(OBJECT_SET_INSTANCE, OBJECT_SET_INSTANCE + "X"))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_member_checksum_mismatch_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    path = build_photometry_input_set(inputs)
    manifest = json.loads(path.read_text())
    manifest["outputs"][0]["members"][0]["sha256"] = "sha256:" + "1" * 64
    path.write_text(json.dumps(manifest))
    # The declared stub validates the manifest's own shape (products page's
    # difference-image registration block) but does not read member bytes
    # back off disk -- it never gets to the point of needing them. A
    # mismatched checksum in the manifest itself is therefore not rejected
    # by this build; recorded here so a future port that adds byte
    # verification changes this test deliberately, not by surprise.
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.NOT_IMPLEMENTED)
