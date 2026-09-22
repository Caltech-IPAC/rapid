"""Tests for rapidpipe.stages.contract: run_stage exercised end to end.

The test-only stage below is defined here rather than under
rapidpipe.stages, since StageDeclaration.validate() restricts stage names
to the contract's fixed list and this test does not want a fictitious name
added to it.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.products.manifest import Inputs, Manifest, Member, OutputEntry, Unit
from rapidpipe.stages.contract import (
    ExitCode,
    InputRejected,
    StageDeclaration,
    StageError,
    StageResult,
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
    return StageResult(
        outputs=(
            OutputEntry(
                kind="exposure",
                format_version="1",
                instance="pi-exp-1",
                key={"exposure": context.unit_id},
                primary="out1.fits",
                members=(Member("image", "out1.fits", 3, "sha256:" + "a" * 64),),
            ),
        ),
    )


def _upstream_manifest_json() -> str:
    upstream = Manifest(
        run="r0",
        unit=Unit(kind="exposure", id="u0"),
        stage="admit",
        attempt="a0",
        execution_record="exec/a0.json",
        inputs=Inputs(manifest="s3://bucket/root/manifest.json"),
    )
    return upstream.to_json()


@pytest.fixture
def inputs_dir(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(_upstream_manifest_json())
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
    restored = Manifest.read(manifest_path)
    assert restored.run == "r1"
    assert restored.unit == Unit(kind=DECLARATION.unit, id="u1")
    assert restored.attempt == "a1"
    assert restored.stage == DECLARATION.name
    assert restored.inputs.manifest == str(inputs_dir / "manifest.json")


def test_success_writes_execution_record(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)
    restored = Manifest.read(outputs_dir / "manifest.json")
    execution_record_path = outputs_dir / restored.execution_record
    assert execution_record_path.exists()
    record = json.loads(execution_record_path.read_text())
    assert "settings_hash" in record
    assert "source_revision" in record
    assert "image_digest" in record


def test_products_read_and_result_sets_read_reach_the_manifest(inputs_dir, tmp_path):
    def body(context):
        return StageResult(
            outputs=(
                OutputEntry(
                    kind="association-set", format_version="1",
                    instance="pi-assoc-1", key={"field": "f1"}),
            ),
            products_read={"l2-image": "pi-l2-9"},
            result_sets_read=("pi-source-1", "pi-source-2"),
        )

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)
    restored = Manifest.read(outputs_dir / "manifest.json")
    assert restored.inputs.products == {"l2-image": "pi-l2-9"}
    assert restored.inputs.result_sets == ("pi-source-1", "pi-source-2")
    assert restored.outputs[0].is_result_set()


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
