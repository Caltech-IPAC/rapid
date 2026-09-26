"""Tests for rapidpipe.stages.contract: run_stage exercised end to end.

The test-only stage below is defined here rather than under
rapidpipe.stages, since StageDeclaration.validate() restricts stage names
to the contract's fixed list and this test does not want a fictitious name
added to it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import rapidpipe.products.storage as storage_module
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
from rapidpipe.stages.settings import canonical_hash

from .fakes3 import FakeClientError, FakeEndpointConnectionError, FakeS3

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


def test_execution_record_carries_the_resolved_settings(inputs_dir, tmp_path):
    # DECLARATION itself declares no settings (settings_schema_path=None,
    # so resolved_settings would trivially be {}); a real schema exercises
    # the actual gap this closes (execution_records.resolved_settings was
    # documented, but never written, direction/logging-timing).
    schema_path = Path(__file__).resolve().parents[2] / "rapidpipe" / "settings" / "admit.toml"
    declaration = dataclasses.replace(DECLARATION, settings_schema_path=str(schema_path))

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(declaration, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)

    restored = Manifest.read(outputs_dir / "manifest.json")
    record = json.loads((outputs_dir / restored.execution_record).read_text())
    assert record["resolved_settings"]  # non-empty: admit.toml has real defaults
    assert record["resolved_settings"]["header"]["sca"] == "SCA-NUM"
    # Consistent with the hash already recorded alongside it.
    assert canonical_hash(record["resolved_settings"]) == record["settings_hash"]


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


def test_malformed_s3_inputs_are_rejected_with_usage_error(tmp_path):
    # No bucket name -- malformed, not merely "not yet fetched" -- is still
    # a UsageError. Full S3 input/output behaviour (successful fetch,
    # publish, error mapping) is covered in test_storage.py's end-to-end
    # run_stage tests, which supply a FakeS3 client via monkeypatch.
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        DECLARATION, _success_body,
        _argv("s3:///no-bucket", outputs_dir))
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


# --- S3 --inputs/--outputs end to end ------------------------------------
#
# run_stage resolves an S3 location's client through
# rapidpipe.products.storage.s3_client(); monkeypatching that to return a
# FakeS3 makes the whole fetch-then-run-then-publish path exercisable
# without boto3 or network access.


def _s3_upstream_manifest_json() -> str:
    upstream = Manifest(
        run="r0", unit=Unit(kind="exposure", id="u0"), stage="admit",
        attempt="a0", execution_record="exec/a0.json",
        inputs=Inputs(manifest="s3://bucket/root/manifest.json"))
    return upstream.to_json()


def _seed_s3_inputs(fake: FakeS3, bucket: str, prefix: str) -> None:
    fake.seed(bucket, f"{prefix}/manifest.json", _s3_upstream_manifest_json().encode())


def test_run_stage_s3_inputs_and_outputs_end_to_end(monkeypatch, tmp_path):
    fake = FakeS3()
    _seed_s3_inputs(fake, "in-bucket", "runs/r0/admit/u0/a0")
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    seen_work_dirs = {}

    def body(context):
        seen_work_dirs["inputs_dir"] = context.inputs_dir
        seen_work_dirs["outputs_dir"] = context.outputs_dir
        return StageResult(
            outputs=(
                OutputEntry(
                    kind="exposure", format_version="1", instance="pi-exp-1",
                    key={"exposure": context.unit_id}, primary="out1.fits",
                    members=(Member("image", "out1.fits", 3, "sha256:" + "a" * 64),),
                ),
            ),
        )

    argv = [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", "s3://in-bucket/runs/r0/admit/u0/a0",
        "--outputs", "s3://out-bucket/runs/r1/admit/u1/a1",
    ]
    rc = run_stage(DECLARATION, body, argv)
    assert rc == int(ExitCode.SUCCESS)

    # The published manifest landed in the fake bucket, with the original
    # S3 argument recorded as inputs.manifest, not the local temp path.
    manifest_bytes = fake._objects[("out-bucket", "runs/r1/admit/u1/a1/manifest.json")]
    published = Manifest.from_dict(json.loads(manifest_bytes))
    assert published.inputs.manifest == "s3://in-bucket/runs/r0/admit/u0/a0/manifest.json"
    assert published.outputs[0].instance == "pi-exp-1"

    # The execution record was uploaded too.
    assert ("out-bucket", "runs/r1/admit/u1/a1/exec/a1.json") in fake._objects

    # The manifest was the last of publish_dir's own uploads (the
    # per-stage log file is re-uploaded separately, after publish_dir,
    # since its work directory is removed right afterwards).
    upload_keys = [key for op, key in fake.calls if op == "upload_file"]
    assert upload_keys[-1] == "runs/r1/admit/u1/a1/log/admit.log"
    assert upload_keys[-2] == "runs/r1/admit/u1/a1/manifest.json"

    # The temporary work directory used for both inputs and outputs is
    # gone afterwards.
    assert not seen_work_dirs["inputs_dir"].exists()
    assert not seen_work_dirs["outputs_dir"].exists()

    # The uploaded log is not just present (asserted above): it contains
    # the final success line, with the exit code, published manifest
    # location, and all four timing fields, not only what was logged
    # before the log file was closed and uploaded.
    uploaded_log = fake._objects[("out-bucket", "runs/r1/admit/u1/a1/log/admit.log")].decode()
    (final_line,) = (line for line in uploaded_log.splitlines() if "exit=" in line)
    assert f"exit={int(ExitCode.SUCCESS)}" in final_line
    assert "manifest=s3://out-bucket/runs/r1/admit/u1/a1/manifest.json" in final_line
    assert "elapsed_s=" in final_line
    assert "fetch_s=" in final_line
    assert "body_s=" in final_line
    assert "publish_s=" in final_line


def test_run_stage_s3_outputs_body_raises_uploads_only_the_log_and_keeps_work_dir(
        monkeypatch, tmp_path):
    fake = FakeS3()
    _seed_s3_inputs(fake, "in-bucket", "runs/r0/admit/u0/a0")
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    seen_work_dirs = {}

    def body(context):
        seen_work_dirs["outputs_dir"] = context.outputs_dir
        raise StageError("boom")

    argv = [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", "s3://in-bucket/runs/r0/admit/u0/a0",
        "--outputs", "s3://out-bucket/runs/r1/admit/u1/a1",
    ]
    rc = run_stage(DECLARATION, body, argv)
    assert rc == int(ExitCode.STAGE_ERROR)

    # No manifest, no execution record: body raised before either was
    # written. The per-stage log file is still re-uploaded, best-effort,
    # so a failed S3 attempt's log is not stranded on this host alone.
    upload_keys = [key for op, key in fake.calls if op == "upload_file"]
    assert upload_keys == ["runs/r1/admit/u1/a1/log/admit.log"]
    # The attempt's own outputs work directory survives for inspection;
    # its parent (the attempt's overall temp work dir) does too.
    assert seen_work_dirs["outputs_dir"].parent.exists()


def test_run_stage_s3_dry_run_fetches_only_the_manifest_object(monkeypatch, tmp_path):
    fake = FakeS3()
    _seed_s3_inputs(fake, "in-bucket", "runs/r0/admit/u0/a0")
    # A second object present under the same prefix: --dry-run must not
    # fetch it.
    fake.seed("in-bucket", "runs/r0/admit/u0/a0/l2/delivered.fits", b"should-not-fetch")
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    argv = [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", "s3://in-bucket/runs/r0/admit/u0/a0",
        "--outputs", str(tmp_path / "outputs"),
        "--dry-run",
    ]
    rc = run_stage(DECLARATION, _success_body, argv)
    assert rc == int(ExitCode.SUCCESS)

    download_keys = [key for op, key in fake.calls if op == "download_file"]
    assert download_keys == ["runs/r0/admit/u0/a0/manifest.json"]


def test_run_stage_s3_missing_manifest_exits_input_rejected(monkeypatch, tmp_path):
    fake = FakeS3()  # nothing seeded
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    argv = [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", "s3://in-bucket/runs/r0/admit/u0/a0",
        "--outputs", str(tmp_path / "outputs"),
    ]
    rc = run_stage(DECLARATION, _success_body, argv)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_run_stage_s3_connection_error_exits_transient_failure(monkeypatch, tmp_path, capsys):
    class _BrokenS3(FakeS3):
        def list_objects_v2(self, **kwargs):
            raise FakeEndpointConnectionError("could not connect")

    fake = _BrokenS3()
    _seed_s3_inputs(fake, "in-bucket", "runs/r0/admit/u0/a0")
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    argv = [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", "s3://in-bucket/runs/r0/admit/u0/a0",
        "--outputs", str(tmp_path / "outputs"),
    ]
    rc = run_stage(DECLARATION, _success_body, argv)
    assert rc == int(ExitCode.TRANSIENT_FAILURE)

    # The S3 fetch ran (and failed) inside the fetch phase, so it gets a
    # real elapsed value, not the "-" a phase that never started would
    # get; body and publish never ran.
    err = capsys.readouterr().err
    assert "fetch_s=-" not in err
    assert "body_s=-" in err and "publish_s=-" in err


def test_run_stage_s3_publish_failure_reports_real_publish_time_not_dashed(
        monkeypatch, tmp_path, capsys):
    class _BrokenUploadS3(FakeS3):
        def upload_file(self, filename, bucket, key, **kwargs):
            raise FakeEndpointConnectionError("could not connect")

    fake = _BrokenUploadS3()
    _seed_s3_inputs(fake, "in-bucket", "runs/r0/admit/u0/a0")
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    argv = [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", "s3://in-bucket/runs/r0/admit/u0/a0",
        "--outputs", "s3://out-bucket/runs/r1/admit/u1/a1",
    ]
    rc = run_stage(DECLARATION, _success_body, argv)
    assert rc == int(ExitCode.TRANSIENT_FAILURE)

    # publish_dir's upload failed, but only after fetch and body ran to
    # completion, and after publish itself started, so all three get a
    # real elapsed value; only "-" would mean a phase never started.
    err = capsys.readouterr().err
    assert "fetch_s=-" not in err
    assert "body_s=-" not in err
    assert "publish_s=-" not in err


# --- --settings as an s3:// overlay ---------------------------------------
#
# A Batch job has no laptop path to give --settings, so the overlay may
# also be an s3://bucket/key location; contract.py fetches it (settings.py
# stays stdlib-only) before resolve_settings runs. These tests use a
# declaration with a real settings schema, since DECLARATION above (schema
# None) can only accept an empty overlay.


@pytest.fixture
def declaration_with_settings(tmp_path):
    schema_path = tmp_path / "schema.toml"
    schema_path.write_text('greeting = "hello"\ncount = 1\n')
    return StageDeclaration(
        name="admit",
        unit="exposure",
        argument_schema={},
        settings_schema_path=str(schema_path),
        consumes=(),
        produces=("exposure",),
        database_access="none",
    )


def test_s3_settings_overlay_is_applied(
        monkeypatch, declaration_with_settings, inputs_dir, tmp_path):
    fake = FakeS3()
    fake.seed("cfg-bucket", "overlays/a1.toml", b'greeting = "hi"\n')
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    seen_settings = {}

    def body(context):
        seen_settings["settings"] = context.settings
        return StageResult(
            outputs=(
                OutputEntry(
                    kind="exposure", format_version="1", instance="pi-exp-1",
                    key={"exposure": context.unit_id}, primary="out1.fits",
                    members=(Member("image", "out1.fits", 3, "sha256:" + "a" * 64),),
                ),
            ),
        )

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        declaration_with_settings, body,
        _argv(
            inputs_dir, outputs_dir,
            extra=["--settings", "s3://cfg-bucket/overlays/a1.toml"]))
    assert rc == int(ExitCode.SUCCESS)
    assert seen_settings["settings"] == {"greeting": "hi", "count": 1}

    restored = Manifest.read(outputs_dir / "manifest.json")
    record = json.loads(
        (outputs_dir / restored.execution_record).read_text())
    assert record["settings_hash"] == canonical_hash(
        {"greeting": "hi", "count": 1})


def test_s3_settings_overlay_missing_object_exits_usage_error(
        monkeypatch, declaration_with_settings, inputs_dir, tmp_path):
    fake = FakeS3()  # nothing seeded at overlays/missing.toml
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        declaration_with_settings, _success_body,
        _argv(
            inputs_dir, outputs_dir,
            extra=["--settings", "s3://cfg-bucket/overlays/missing.toml"]))
    # A missing --settings overlay is a usage error (64), the same family
    # as a missing local --settings path -- not InputRejected (65), which
    # is reserved for a declared --inputs location being absent.
    assert rc == int(ExitCode.USAGE)
    assert not (outputs_dir / "manifest.json").exists()


def test_s3_settings_overlay_storage_error_exits_usage_error(
        monkeypatch, declaration_with_settings, inputs_dir, tmp_path):
    class _BrokenS3(FakeS3):
        def download_file(self, bucket, key, dest):
            raise FakeEndpointConnectionError("could not connect")

    fake = _BrokenS3()
    fake.seed("cfg-bucket", "overlays/a1.toml", b'greeting = "hi"\n')
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        declaration_with_settings, _success_body,
        _argv(
            inputs_dir, outputs_dir,
            extra=["--settings", "s3://cfg-bucket/overlays/a1.toml"]))
    # Even a transient-shaped S3 failure maps to USAGE here (not
    # TRANSIENT_FAILURE): from the contract's point of view --settings was
    # an unusable argument, and --settings does not get its own retry path.
    assert rc == int(ExitCode.USAGE)


def test_local_settings_overlay_still_works_alongside_s3(
        declaration_with_settings, inputs_dir, tmp_path):
    settings_path = tmp_path / "overlay.toml"
    settings_path.write_text('greeting = "howdy"\n')

    seen_settings = {}

    def body(context):
        seen_settings["settings"] = context.settings
        return StageResult(
            outputs=(
                OutputEntry(
                    kind="exposure", format_version="1", instance="pi-exp-1",
                    key={"exposure": context.unit_id}, primary="out1.fits",
                    members=(Member("image", "out1.fits", 3, "sha256:" + "a" * 64),),
                ),
            ),
        )

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        declaration_with_settings, body,
        _argv(inputs_dir, outputs_dir, extra=["--settings", str(settings_path)]))
    assert rc == int(ExitCode.SUCCESS)
    assert seen_settings["settings"] == {"greeting": "howdy", "count": 1}


def test_dry_run_with_s3_settings_overlay_validates_without_publishing(
        monkeypatch, declaration_with_settings, inputs_dir, tmp_path):
    fake = FakeS3()
    fake.seed("cfg-bucket", "overlays/a1.toml", b'greeting = "hi"\n')
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        declaration_with_settings, _success_body,
        _argv(
            inputs_dir, outputs_dir,
            extra=[
                "--settings", "s3://cfg-bucket/overlays/a1.toml",
                "--dry-run",
            ]))
    assert rc == int(ExitCode.SUCCESS)
    assert not (outputs_dir / "manifest.json").exists()
    # The overlay object was actually fetched and validated, not skipped.
    download_keys = [key for op, key in fake.calls if op == "download_file"]
    assert "overlays/a1.toml" in download_keys


def test_dry_run_with_missing_s3_settings_overlay_exits_usage_error(
        monkeypatch, declaration_with_settings, inputs_dir, tmp_path):
    fake = FakeS3()  # nothing seeded
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        declaration_with_settings, _success_body,
        _argv(
            inputs_dir, outputs_dir,
            extra=[
                "--settings", "s3://cfg-bucket/overlays/missing.toml",
                "--dry-run",
            ]))
    assert rc == int(ExitCode.USAGE)


# ======================================================================
# source revision / image digest env fallbacks (a Batch container has no
# git repository, but its image bakes the built SHA into
# RAPID_SOURCE_REVISION, and the deployed job definition sets
# RAPID_IMAGE_DIGEST)
# ======================================================================

def test_source_revision_uses_git_when_available(monkeypatch):
    import rapidpipe.stages.contract as contract_module

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "abc123\n"

    monkeypatch.setattr(
        contract_module.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess())
    monkeypatch.setenv("RAPID_SOURCE_REVISION", "should-not-be-used")

    assert contract_module._source_revision() == "abc123"


def test_source_revision_falls_back_to_env_when_git_fails(monkeypatch):
    import rapidpipe.stages.contract as contract_module

    class _FakeCompletedProcess:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(
        contract_module.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess())
    monkeypatch.setenv("RAPID_SOURCE_REVISION", "baked-sha-456")

    assert contract_module._source_revision() == "baked-sha-456"


def test_source_revision_falls_back_to_env_when_git_not_installed(monkeypatch):
    import rapidpipe.stages.contract as contract_module

    def _raise(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(contract_module.subprocess, "run", _raise)
    monkeypatch.setenv("RAPID_SOURCE_REVISION", "baked-sha-789")

    assert contract_module._source_revision() == "baked-sha-789"


def test_source_revision_none_when_git_fails_and_env_unset(monkeypatch):
    import rapidpipe.stages.contract as contract_module

    class _FakeCompletedProcess:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(
        contract_module.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess())
    monkeypatch.delenv("RAPID_SOURCE_REVISION", raising=False)

    assert contract_module._source_revision() is None


def test_write_execution_record_prefers_rapidpipe_image_digest(monkeypatch, tmp_path):
    import rapidpipe.stages.contract as contract_module

    monkeypatch.setattr(contract_module, "_source_revision", lambda: "rev1")
    monkeypatch.setenv("RAPIDPIPE_IMAGE_DIGEST", "sha256:developer")
    monkeypatch.setenv("RAPID_IMAGE_DIGEST", "sha256:deployed")

    outputs_dir = tmp_path / "outputs"
    relative = contract_module._write_execution_record(outputs_dir, "a1", "hash1")
    record = json.loads((outputs_dir / relative).read_text())
    assert record["image_digest"] == "sha256:developer"


def test_write_execution_record_falls_back_to_rapid_image_digest(monkeypatch, tmp_path):
    import rapidpipe.stages.contract as contract_module

    monkeypatch.setattr(contract_module, "_source_revision", lambda: "rev1")
    monkeypatch.delenv("RAPIDPIPE_IMAGE_DIGEST", raising=False)
    monkeypatch.setenv("RAPID_IMAGE_DIGEST", "sha256:deployed")

    outputs_dir = tmp_path / "outputs"
    relative = contract_module._write_execution_record(outputs_dir, "a1", "hash1")
    record = json.loads((outputs_dir / relative).read_text())
    assert record["image_digest"] == "sha256:deployed"


def test_write_execution_record_digest_none_when_both_unset(monkeypatch, tmp_path):
    import rapidpipe.stages.contract as contract_module

    monkeypatch.setattr(contract_module, "_source_revision", lambda: "rev1")
    monkeypatch.delenv("RAPIDPIPE_IMAGE_DIGEST", raising=False)
    monkeypatch.delenv("RAPID_IMAGE_DIGEST", raising=False)

    outputs_dir = tmp_path / "outputs"
    relative = contract_module._write_execution_record(outputs_dir, "a1", "hash1")
    record = json.loads((outputs_dir / relative).read_text())
    assert record["image_digest"] is None


# ======================================================================
# Per-stage log file, timing, and opt-in profiling
# ======================================================================

def test_success_writes_a_per_stage_log_file_with_timing(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)

    log_path = outputs_dir / "log" / "admit.log"
    assert log_path.exists()
    contents = log_path.read_text()
    assert "run=r1" in contents
    assert "attempt=a1" in contents
    assert "stage=admit" in contents
    assert "unit=u1" in contents
    # The final success line, with every phase reached (all local, so
    # nothing is "-").
    last_line = contents.strip().splitlines()[-1]
    assert "exit=0" in last_line
    for field in ("elapsed_s=", "fetch_s=", "body_s=", "publish_s="):
        assert field in last_line
        value = last_line.split(field, 1)[1].split()[0]
        assert value != "-"
        float(value)  # a real number, not a placeholder


def test_error_log_line_has_elapsed_fields_with_dashes_for_unreached_phases(
        inputs_dir, tmp_path):
    def body(context):
        raise StageError("boom")

    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.STAGE_ERROR)

    log_path = outputs_dir / "log" / "admit.log"
    contents = log_path.read_text()
    last_line = contents.strip().splitlines()[-1]
    assert "error=boom" in last_line
    assert "elapsed_s=" in last_line and "fetch_s=" in last_line
    assert "body_s=" in last_line and "publish_s=-" in last_line  # never reached


def test_missing_input_manifest_error_reports_real_fetch_time_not_dashed(tmp_path, capsys):
    # A missing input manifest fails inside the fetch phase (after
    # fetch_start is set, and after the per-stage log file already
    # exists): fetch ran, however briefly, before failing, so it gets a
    # real elapsed value, not "-". body and publish never ran, so they
    # are still "-".
    rc = run_stage(DECLARATION, _success_body, [
        "--run", "r1", "--unit", "u1", "--attempt", "a1",
        "--inputs", str(tmp_path / "no-such-inputs"), "--outputs", str(tmp_path / "out"),
    ])
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert (tmp_path / "out" / "log" / "admit.log").exists()
    err = capsys.readouterr().err
    assert "fetch_s=-" not in err
    assert "body_s=-" in err and "publish_s=-" in err
    assert "elapsed_s=-" not in err


def test_error_before_fetch_starts_still_has_dashed_phases(tmp_path, capsys):
    # A malformed --inputs location fails while parsing argv, before
    # fetch_start is even set (and before the per-stage log file is
    # created): fetch/body/publish never ran, so every one of them is
    # "-" (total is not, since the attempt did run for some measurable
    # time before failing).
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        DECLARATION, _success_body,
        _argv("s3:///no-bucket", outputs_dir))
    assert rc == int(ExitCode.USAGE)
    assert not (outputs_dir / "log").exists()
    err = capsys.readouterr().err
    assert "fetch_s=-" in err and "body_s=-" in err and "publish_s=-" in err
    assert "elapsed_s=-" not in err


def test_execution_record_gets_a_timing_key(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)

    restored = Manifest.read(outputs_dir / "manifest.json")
    record = json.loads((outputs_dir / restored.execution_record).read_text())
    timing = record["timing"]
    assert set(timing) == {"started", "fetch_s", "body_s"}
    assert timing["started"].endswith("Z")
    assert isinstance(timing["fetch_s"], (int, float))
    assert isinstance(timing["body_s"], (int, float))
    assert "ended" not in timing  # written before publish; not known yet


def test_dry_run_writes_no_log_file(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(
        DECLARATION, _success_body,
        _argv(inputs_dir, outputs_dir, extra=["--dry-run"]))
    assert rc == int(ExitCode.SUCCESS)
    # --dry-run "returns 0 without calling body or writing anything": the
    # per-stage log file is no exception.
    assert not outputs_dir.exists() or not any(outputs_dir.iterdir())


def test_profile_env_var_writes_pstats_and_txt(monkeypatch, inputs_dir, tmp_path):
    monkeypatch.setenv("RAPIDPIPE_PROFILE", "1")
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)

    pstats_path = outputs_dir / "profile" / "admit.pstats"
    txt_path = outputs_dir / "profile" / "admit.txt"
    assert pstats_path.exists()
    assert txt_path.exists()
    assert pstats_path.stat().st_size > 0
    text = txt_path.read_text()
    assert "cumulative" in text

    # Not manifest members.
    restored = Manifest.read(outputs_dir / "manifest.json")
    published_paths = {entry.primary for entry in restored.outputs}
    assert "profile/admit.pstats" not in published_paths
    assert "profile/admit.txt" not in published_paths


def test_profile_files_absent_without_the_env_var(inputs_dir, tmp_path):
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _success_body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)
    assert not (outputs_dir / "profile").exists()
