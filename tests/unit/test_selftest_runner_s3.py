"""``rapidpipe.selftest.runner.run_fixture``'s S3 ``--output-location`` path.

Defect (measured on AWS Batch, job 7e285d37-1e45-4540-9c16-e439880a567e):
with an ``s3://`` ``output_location``, ``run_fixture`` used to pass it
straight through to the stage subprocess as ``--outputs`` and never read
the products back, so the stage-specific checks
(:attr:`~rapidpipe.selftest.runner.StageFixture.check`) never ran -- only
the exit code was checked, and ``rapidpipe selftest --stage load
--output-location s3://...`` reported "PASS (1 checks passed)" instead of
the 102 ``make stage-load`` runs locally. The fix always runs the stage
against a local ``work_dir / "outputs"`` directory, checks against that,
and only then uploads it to the S3 location with
:func:`rapidpipe.products.storage.publish_dir`.

These tests exercise :func:`run_fixture` directly, standing in for the
stage subprocess by monkeypatching
:func:`rapidpipe.selftest.runner.run_stage_subprocess` to write a minimal
manifest (and nothing else product-specific -- the ``check`` hook here is
a spy, not real per-stage checks, which :mod:`test_difference_fixture`
and ``make stage-load`` already cover against fake tools end to end) so
the test needs no external tools and no network access.
"""

from __future__ import annotations

import json
from pathlib import Path

import rapidpipe.products.storage as storage_module
import rapidpipe.selftest.runner as runner_module
from rapidpipe.products.manifest import Inputs, Manifest, Unit
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, run_fixture

from .fakes3 import FakeS3

STAGE = "widget"
UNIT_ID = "u-0001"
RUN_MODULE = "rapidpipe.stages.widget"


def _fake_expected(tmp_path: Path) -> None:
    """Write a minimal packaged expected.json/settings.toml under a fake
    fixture dir, and point runner.FIXTURES_ROOT there for the test."""
    fixture_dir = tmp_path / "fixtures" / STAGE
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "expected.json").write_text(json.dumps({"fake": {"exit_code": 0}}))
    (fixture_dir / "settings.toml").write_text("")


def _prepare(work: Path, expected: dict, fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    inputs.mkdir()
    overlay = work / "settings.toml"
    overlay.write_text("")
    return inputs, overlay, {}


def _make_fixture() -> tuple[StageFixture, list[CheckContext]]:
    seen_contexts: list[CheckContext] = []

    def check(checks: Checks, manifest: Manifest, expected: dict, context: CheckContext) -> None:
        seen_contexts.append(context)
        # A stand-in product check -- what matters for these tests is
        # only that it ran (and against which directory), not its content.
        checks.check(manifest.stage == STAGE, "manifest stage is widget")
        checks.check(len(list(context.outputs.iterdir())) >= 1, "outputs directory not empty")

    fx = StageFixture(
        stage=STAGE, module=RUN_MODULE, unit_kind="exposure", unit_id=UNIT_ID,
        fake_toolkit_env={}, prepare=_prepare, check=check)
    return fx, seen_contexts


def _stub_subprocess_writes_manifest(monkeypatch, extra_file: str = "product.dat"):
    """Replace run_stage_subprocess with one that writes a valid manifest
    (and one product file) into the outputs directory it's given, as a
    real stage would, and returns exit code 0."""

    def fake_run_stage_subprocess(python, module, inputs, outputs_location, overlay,
                                   run_id, attempt_id, unit_id, extra_env, repo_root):
        outputs_dir = Path(outputs_location)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / extra_file).write_text("data")
        record_dir = outputs_dir / "exec"
        record_dir.mkdir(exist_ok=True)
        (record_dir / f"{attempt_id}.json").write_text(json.dumps(
            {"settings_hash": "a" * 64, "source_revision": "deadbeef", "image_digest": "sha256:x"}))
        manifest = Manifest(
            run=run_id, unit=Unit(kind="exposure", id=unit_id), stage=STAGE,
            attempt=attempt_id, execution_record=f"exec/{attempt_id}.json",
            inputs=Inputs(manifest=str(inputs / "manifest.json")))
        manifest.write(outputs_dir / "manifest.json")
        return 0

    monkeypatch.setattr(runner_module, "run_stage_subprocess", fake_run_stage_subprocess)


def test_run_fixture_local_output_location_checks_in_place(monkeypatch, tmp_path):
    _fake_expected(tmp_path)
    monkeypatch.setattr(runner_module, "FIXTURES_ROOT", tmp_path / "fixtures")
    monkeypatch.setattr(runner_module, "STAGE_NAMES", (STAGE, "difference", "load"))
    _stub_subprocess_writes_manifest(monkeypatch)
    fx, seen_contexts = _make_fixture()

    result = run_fixture(
        fx, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=tmp_path / "work", output_location=str(tmp_path / "explicit-out"))

    assert result.exit_code == 0
    assert not result.checks.failures
    assert result.checks.passed >= 2
    assert result.uploaded_to is None
    assert result.output_location == str(tmp_path / "explicit-out")
    assert seen_contexts[0].outputs == tmp_path / "explicit-out"


def test_run_fixture_s3_output_location_runs_checks_against_local_outputs(monkeypatch, tmp_path):
    _fake_expected(tmp_path)
    monkeypatch.setattr(runner_module, "FIXTURES_ROOT", tmp_path / "fixtures")
    monkeypatch.setattr(runner_module, "STAGE_NAMES", (STAGE, "difference", "load"))
    _stub_subprocess_writes_manifest(monkeypatch)
    fake_s3 = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake_s3)
    fx, seen_contexts = _make_fixture()

    work_dir = tmp_path / "work"
    result = run_fixture(
        fx, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=work_dir, output_location="s3://a-bucket/a-prefix")

    # The stage-specific checks ran (the defect: they used to be skipped
    # entirely for an s3:// output location) and ran against a local
    # directory the runner itself controls, under work_dir.
    assert result.exit_code == 0
    assert not result.checks.failures
    assert result.checks.passed >= 2
    assert len(seen_contexts) == 1
    assert seen_contexts[0].outputs == work_dir / "outputs"
    assert result.output_is_local is True
    assert result.output_location == str(work_dir / "outputs")


def test_run_fixture_s3_output_location_uploads_after_checks_pass(monkeypatch, tmp_path):
    _fake_expected(tmp_path)
    monkeypatch.setattr(runner_module, "FIXTURES_ROOT", tmp_path / "fixtures")
    monkeypatch.setattr(runner_module, "STAGE_NAMES", (STAGE, "difference", "load"))
    _stub_subprocess_writes_manifest(monkeypatch)
    fake_s3 = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake_s3)
    fx, seen_contexts = _make_fixture()

    result = run_fixture(
        fx, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=tmp_path / "work", output_location="s3://a-bucket/a-prefix")

    assert result.uploaded_to == "s3://a-bucket/a-prefix"
    uploaded_keys = {key for name, key in fake_s3.calls if name == "upload_file"}
    assert "a-prefix/product.dat" in uploaded_keys
    assert "a-prefix/manifest.json" in uploaded_keys
    assert "a-prefix/exec/" in "".join(uploaded_keys)  # execution record uploaded too
    # publish_dir's own contract: manifest.json uploaded last.
    upload_order = [key for name, key in fake_s3.calls if name == "upload_file"]
    assert upload_order[-1] == "a-prefix/manifest.json"


def test_run_fixture_s3_output_location_uploads_even_when_a_check_fails(monkeypatch, tmp_path):
    """A failed product check is still reported, and the local outputs
    are still uploaded afterwards -- for inspection, not silently
    dropped -- rather than the upload being skipped on any failure."""
    _fake_expected(tmp_path)
    monkeypatch.setattr(runner_module, "FIXTURES_ROOT", tmp_path / "fixtures")
    monkeypatch.setattr(runner_module, "STAGE_NAMES", (STAGE, "difference", "load"))
    _stub_subprocess_writes_manifest(monkeypatch)
    fake_s3 = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake_s3)

    def failing_check(checks: Checks, manifest: Manifest, expected: dict,
                       context: CheckContext) -> None:
        checks.check(False, "a deliberately failing product check")

    fx = StageFixture(
        stage=STAGE, module=RUN_MODULE, unit_kind="exposure", unit_id=UNIT_ID,
        fake_toolkit_env={}, prepare=_prepare, check=failing_check)

    result = run_fixture(
        fx, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=tmp_path / "work", output_location="s3://a-bucket/a-prefix")

    assert result.checks.failures == ["a deliberately failing product check"]
    assert result.uploaded_to == "s3://a-bucket/a-prefix"
    uploaded_keys = {key for name, key in fake_s3.calls if name == "upload_file"}
    assert "a-prefix/manifest.json" in uploaded_keys


def test_run_fixture_s3_check_count_matches_local_check_count(monkeypatch, tmp_path):
    """The same fixture, run once local and once against an s3:// output
    location, must produce the same number of checks -- the concrete
    "102 vs 1" regression this defect was measured as."""
    _fake_expected(tmp_path)
    monkeypatch.setattr(runner_module, "FIXTURES_ROOT", tmp_path / "fixtures")
    monkeypatch.setattr(runner_module, "STAGE_NAMES", (STAGE, "difference", "load"))
    fake_s3 = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake_s3)

    _stub_subprocess_writes_manifest(monkeypatch)
    fx_local, _ = _make_fixture()
    local_result = run_fixture(
        fx_local, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=tmp_path / "work-local", output_location=None)

    _stub_subprocess_writes_manifest(monkeypatch)
    fx_s3, _ = _make_fixture()
    s3_result = run_fixture(
        fx_s3, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=tmp_path / "work-s3", output_location="s3://a-bucket/a-prefix")

    assert local_result.checks.passed == s3_result.checks.passed
    assert local_result.checks.failures == s3_result.checks.failures


def test_run_fixture_local_output_location_never_uploads(monkeypatch, tmp_path):
    _fake_expected(tmp_path)
    monkeypatch.setattr(runner_module, "FIXTURES_ROOT", tmp_path / "fixtures")
    monkeypatch.setattr(runner_module, "STAGE_NAMES", (STAGE, "difference", "load"))
    _stub_subprocess_writes_manifest(monkeypatch)
    fake_s3 = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake_s3)
    fx, _ = _make_fixture()

    result = run_fixture(
        fx, tools="fake", python="python3", repo_root=tmp_path,
        work_dir=tmp_path / "work", output_location=str(tmp_path / "local-out"))

    assert result.uploaded_to is None
    assert fake_s3.calls == []
