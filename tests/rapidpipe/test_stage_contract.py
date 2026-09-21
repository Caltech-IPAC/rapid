"""Tests for rapidpipe.stages.contract: run_stage exercised end to end.

The test-only stage below is defined here rather than under
rapidpipe.stages, since StageDeclaration.validate() restricts stage names
to the contract's fixed list and this test does not want a fictitious name
added to it.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.products.manifest import CompletionManifest, OutputEntry
from rapidpipe.stages.contract import (
    ExitCode,
    InputRejected,
    StageDeclaration,
    StageError,
    TransientFailure,
    UsageError,
    run_stage,
)

DECLARATION = StageDeclaration(
    name="admit",
    unit="exposure",
    argument_schema={},
    settings_schema_path=None,
    consumes=(),
    produces=("exposure",),
    database_access="none",
)


def _success_body(context):
    return CompletionManifest(
        run_id=context.run_id,
        unit_id=context.unit_id,
        stage=context.declaration.name,
        attempt_id=context.attempt_id,
        execution_record_ref="exec:1",
        input_manifest_ref="in:1",
        outputs=(OutputEntry("out1", "exposure", "1", "run-scoped:out1"),),
    )


@pytest.fixture
def inputs_dir(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(json.dumps({"inputs": []}))
    return inputs


def _argv(inputs_dir, outputs_dir, *, run="r1", unit="u1", attempt="a1", extra=()):
    return [
        "--run", run, "--unit", unit, "--attempt", attempt,
        "--inputs", str(inputs_dir), "--outputs", str(outputs_dir),
        *extra,
    ]


def test_success_publishes_manifest(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)
    manifest_path = outputs_dir / "manifest.json"
    assert manifest_path.exists()
    restored = CompletionManifest.read(manifest_path)
    assert restored.run_id == "r1"
    assert restored.unit_id == "u1"
    assert restored.attempt_id == "a1"


def test_dry_run_publishes_nothing_and_exits_zero(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        DECLARATION, _success_body,
        _argv(inputs_dir, outputs_dir, extra=["--dry-run"]))
    assert rc == int(ExitCode.SUCCESS)
    assert not (outputs_dir / "manifest.json").exists()


@pytest.mark.parametrize("exception_cls, expected_code", [
    (UsageError, ExitCode.USAGE),
    (InputRejected, ExitCode.INPUT_REJECTED),
    (StageError, ExitCode.STAGE_ERROR),
    (TransientFailure, ExitCode.TRANSIENT_FAILURE),
])
def test_each_exception_class_yields_its_code(
        inputs_dir, tmp_path, exception_cls, expected_code):
    def body(context):
        raise exception_cls("boom")

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, body, _argv(inputs_dir, outputs_dir))
    assert rc == int(expected_code)


def test_unhandled_exception_yields_stage_error_code(inputs_dir, tmp_path):
    def body(context):
        raise RuntimeError("not a contract exception")

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.STAGE_ERROR)


def test_missing_input_manifest_yields_input_rejected(tmp_path):
    inputs_dir = tmp_path / "no-such-inputs"
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_unknown_settings_key_yields_usage_error(inputs_dir, tmp_path):
    settings_path = tmp_path / "overlay.toml"
    settings_path.write_text("unknown_key = 1\n")
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        DECLARATION, _success_body,
        _argv(inputs_dir, outputs_dir, extra=["--settings", str(settings_path)]))
    assert rc == int(ExitCode.USAGE)


def test_missing_required_argument_yields_usage_error(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    argv = ["--unit", "u1", "--attempt", "a1",
            "--inputs", str(inputs_dir), "--outputs", str(outputs_dir)]
    rc = run_stage(DECLARATION, _success_body, argv)
    assert rc == int(ExitCode.USAGE)


def test_s3_inputs_are_rejected_with_usage_error(tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        DECLARATION, _success_body,
        _argv("s3://bucket/prefix", outputs_dir))
    assert rc == int(ExitCode.USAGE)


def test_manifest_is_absent_when_body_raises(inputs_dir, tmp_path):
    def body(context):
        raise StageError("boom")

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.STAGE_ERROR)
    assert not (outputs_dir / "manifest.json").exists()


def test_stage_declaration_rejects_unknown_name():
    with pytest.raises(ValueError):
        StageDeclaration(
            name="not-a-real-stage",
            unit="exposure",
            argument_schema={},
            settings_schema_path=None,
            consumes=(),
            produces=(),
            database_access="none",
        ).validate()


def test_stage_declaration_rejects_unknown_unit():
    with pytest.raises(ValueError):
        StageDeclaration(
            name="admit",
            unit="not-a-real-unit",
            argument_schema={},
            settings_schema_path=None,
            consumes=(),
            produces=(),
            database_access="none",
        ).validate()


def test_stage_declaration_rejects_unknown_database_access():
    with pytest.raises(ValueError):
        StageDeclaration(
            name="admit",
            unit="exposure",
            argument_schema={},
            settings_schema_path=None,
            consumes=(),
            produces=(),
            database_access="sideways",
        ).validate()
