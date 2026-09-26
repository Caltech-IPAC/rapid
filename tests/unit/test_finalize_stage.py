"""Tests for rapidpipe.stages.finalize: the manifest transform, the stamp, the exit codes.

The inputs are rapidpipe.selftest.support.fakefinalize's synthetic
difference attempt. The fixture itself (``make stage-finalize``,
``rapidpipe selftest --stage finalize``) runs at the end.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from astropy.io import fits

import rapidpipe.stages.finalize as finalize
from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.diffimage import validate_difference_entry, validate_source_catalog_entry
from rapidpipe.products.manifest import Manifest, OutputEntry
from rapidpipe.selftest import run as run_selftest
from rapidpipe.selftest.runner import STAGE_NAMES
from rapidpipe.selftest.support.fakefinalize import (
    DIFFERENCE_INSTANCE,
    L2_INSTANCE,
    REFERENCE_INSTANCE,
    UNIT_ID,
    build_difference_output,
    merge_manifests,
)
from rapidpipe.stages.contract import ExitCode

RUN = "01J8Y6QZ3M0000000000000RUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "finalize" / "run_fixture.py"


def _argv(inputs: Path, outputs: Path, *extra: str) -> list[str]:
    return ["--run", RUN, "--unit", UNIT_ID, "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(outputs), *extra]


def _run(tmp_path: Path, *extra: str, **build) -> tuple[int, Path]:
    inputs = tmp_path / "inputs"
    if not (inputs / "manifest.json").exists():
        build_difference_output(inputs, **build)
    outputs = tmp_path / "outputs"
    return finalize.main(_argv(inputs, outputs, *extra)), outputs


def _edit_manifest(inputs: Path, edit) -> None:
    path = inputs / "manifest.json"
    manifest = json.loads(path.read_text())
    edit(manifest)
    path.write_text(json.dumps(manifest))


def _rehash(inputs: Path, manifest: dict, relative: str) -> None:
    data = (inputs / relative).read_bytes()
    for entry in manifest["outputs"]:
        for member in entry["members"]:
            if member["path"] == relative:
                member["bytes"] = len(data)
                member["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()


def test_declaration():
    finalize.DECLARATION.validate()
    d = finalize.DECLARATION
    assert (d.name, d.unit, d.database_access) == ("finalize", "detector-image", "none")
    assert d.consumes == ("difference-image", "source-catalog") == d.produces
    assert d.resource_defaults == {"vcpus": 1, "memory_mib": 4096}


def test_republishes_the_difference_attempt_under_new_instances(tmp_path):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    source = Manifest.read(tmp_path / "inputs" / "manifest.json")
    assert manifest.stage == "finalize"
    diff, *catalogs = manifest.outputs
    assert diff.kind == "difference-image" and len(catalogs) == 4
    assert is_valid_ulid(diff.instance) and diff.instance != DIFFERENCE_INSTANCE
    assert diff.key == source.outputs[0].key
    registration = validate_difference_entry(diff.to_dict())
    assert registration.md5 == hashlib.md5((outputs / diff.primary).read_bytes()).hexdigest()
    assert diff.registration["finalized_from"] == DIFFERENCE_INSTANCE
    assert diff.registration["revision"] == 2
    for catalog in catalogs:
        validate_source_catalog_entry(catalog.to_dict())
        assert catalog.key["difference"] == diff.instance
        assert catalog.registration["copied_from"] in {e.instance for e in source.outputs}
    # Ruling (option b): the difference's own registered upstream only.
    assert manifest.inputs.products == {"l2-image": L2_INSTANCE,
                                        "reference-image": REFERENCE_INSTANCE}
    assert manifest.inputs.manifest == str(tmp_path / "inputs" / "manifest.json")

    header = fits.getheader(outputs / diff.primary)
    assert (header["RPRUN"], header["RPATTMPT"], header["RPINST"]) == (RUN, ATTEMPT, diff.instance)
    assert (header["RPL2INST"], header["RPREFINS"]) == (L2_INSTANCE, REFERENCE_INSTANCE)
    assert header["RPOUTLOC"] == str(outputs)
    assert header["PPID"] == 15 and header["FIELD"] == 4662268


def test_dry_run_validates_and_writes_nothing(tmp_path):
    rc, outputs = _run(tmp_path, "--dry-run")
    assert rc == ExitCode.SUCCESS
    assert not outputs.exists() or not any(outputs.iterdir())


def test_missing_execution_record_stamps_unknown(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    for record in (inputs / "exec").iterdir():
        record.unlink()
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    diff = Manifest.read(outputs / "manifest.json").outputs[0]
    header = fits.getheader(outputs / diff.primary)
    assert header["RPSRCREV"] == "unknown" and header["RPIMGDIG"] == "unknown"


def test_null_provenance_in_the_execution_record_stamps_unknown(tmp_path):
    rc, outputs = _run(tmp_path, execution_record={"source_revision": None, "image_digest": None})
    assert rc == ExitCode.SUCCESS
    diff = Manifest.read(outputs / "manifest.json").outputs[0]
    assert fits.getheader(outputs / diff.primary)["RPIMGDIG"] == "unknown"


def test_reference_absent_from_inputs_products_falls_back_to_the_key(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    _edit_manifest(inputs, lambda m: m["inputs"]["products"].pop("reference-image"))
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    diff = manifest.outputs[0]
    assert fits.getheader(outputs / diff.primary)["RPREFINS"] == REFERENCE_INSTANCE
    # A dev-registered reference has no instance row: not a dependency.
    assert manifest.inputs.products == {"l2-image": L2_INSTANCE}


def test_a_photutils_sign_the_mask_says_was_not_produced_is_absent(tmp_path):
    rc, outputs = _run(tmp_path, catalog_outcome_bits=0b10)   # ZOGY negative
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    keys = [(e.key["catalog_type"], e.key["sign"]) for e in manifest.outputs[1:]]
    assert keys == [("sextractor", "positive"), ("sextractor", "negative"),
                    ("photutils", "positive")]
    assert "source-catalog/photutils/negative" not in manifest.inputs.products


@pytest.mark.parametrize("keep", [0, 2])
def test_source_catalogs_pass_through_however_many(tmp_path, keep):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)

    def edit(manifest):
        diff, *catalogs = manifest["outputs"]
        manifest["outputs"] = [diff, *catalogs[:keep]]
    _edit_manifest(inputs, edit)
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    assert sum(1 for e in manifest.outputs if e.kind == "source-catalog") == keep
    assert set(manifest.inputs.products) == {"l2-image", "reference-image"}


def test_every_catalog_member_is_copied(tmp_path):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    source = Manifest.read(tmp_path / "inputs" / "manifest.json")
    manifest = Manifest.read(outputs / "manifest.json")
    photutils = [e for e in manifest.outputs if e.key.get("catalog_type") == "photutils"]
    assert {m.role for e in photutils for m in e.members} == {
        "catalog", "finder", "residual", "parquet"}
    by_instance = {e.instance: e for e in source.outputs}
    for entry in manifest.outputs[1:]:
        assert entry.members == by_instance[entry.registration["copied_from"]].members


def test_an_sfft_bundle_keeps_its_kernel_and_stamps_ppid_16(tmp_path):
    (tmp_path / "overlay.toml").write_text('[finalize]\ndifferencer = "sfft"\n')
    rc, outputs = _run(tmp_path, "--settings", str(tmp_path / "overlay.toml"),
                       differencer="sfft")
    assert rc == ExitCode.SUCCESS
    source = Manifest.read(tmp_path / "inputs" / "manifest.json").outputs[0]
    diff = Manifest.read(outputs / "manifest.json").outputs[0]
    assert {m.role for m in diff.members} == {"difference", "uncertainty", "psf", "kernel"}
    kernel = next(m for m in diff.members if m.role == "kernel")
    assert kernel == next(m for m in source.members if m.role == "kernel")
    header = fits.getheader(outputs / diff.primary)
    assert (header["PPID"], header["RPDIFFER"]) == (16, "sfft")


def test_the_key_keeps_the_difference_settings_hash_and_rpfseths_is_finalize_s(tmp_path):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    diff = manifest.outputs[0]
    record = json.loads((outputs / manifest.execution_record).read_text())
    header = fits.getheader(outputs / diff.primary)
    assert diff.key["settings_hash"] == header["RPSETHSH"] == (
        Manifest.read(tmp_path / "inputs" / "manifest.json").outputs[0].key["settings_hash"])
    assert header["RPFSETHS"] == "sha256:" + record["settings_hash"]
    assert header["RPFSETHS"] != header["RPSETHSH"]


def test_rpoutloc_is_the_s3_location_as_given_not_the_staging_directory(tmp_path, monkeypatch):
    import rapidpipe.products.storage as storage_module
    from tests.unit.fakes3 import FakeS3

    fake = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    location = "s3://out-bucket/runs/r1/finalize/u1/a1"
    assert finalize.main(_argv(inputs, location)) == ExitCode.SUCCESS
    manifest = Manifest.from_dict(json.loads(
        fake._objects[("out-bucket", "runs/r1/finalize/u1/a1/manifest.json")]))
    diff = manifest.outputs[0]
    stamped = tmp_path / "stamped.fits"
    stamped.write_bytes(fake._objects[("out-bucket", f"runs/r1/finalize/u1/a1/{diff.primary}")])
    assert fits.getheader(stamped)["RPOUTLOC"] == location


SFFT_INSTANCE = "01J8Y6QZ3MF1NA1E5FF0000D1F"


def _two_differencers(tmp_path: Path) -> Path:
    """A difference manifest with a ZOGY and an SFFT instance ([sfft] register_sfft on)."""
    inputs = tmp_path / "inputs"
    build_difference_output(inputs, differencer="sfft", difference_instance=SFFT_INSTANCE)
    sfft = json.loads((inputs / "manifest.json").read_text())
    build_difference_output(inputs)     # ZOGY, rewrites manifest.json
    merge_manifests(inputs, sfft)
    return inputs


def test_the_configured_differencer_is_republished_and_the_other_dropped(tmp_path):
    _two_differencers(tmp_path)
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    diffs = [e for e in manifest.outputs if e.kind == "difference-image"]
    assert [d.key["differencer"] for d in diffs] == ["zogy"]
    assert diffs[0].registration["finalized_from"] == DIFFERENCE_INSTANCE
    assert all(e.key["difference"] == diffs[0].instance
               for e in manifest.outputs if e.kind == "source-catalog")
    assert len(manifest.outputs) == 5
    notes = json.loads((outputs / manifest.execution_record).read_text())["notes"]
    assert [d["kind"] for d in notes["dropped"]] == ["difference-image"] + ["source-catalog"] * 4
    assert {d["differencer"] for d in notes["dropped"]} == {"sfft"}
    assert notes["dropped"][0]["instance"] == SFFT_INSTANCE


def test_sfft_selected_from_a_two_differencer_manifest(tmp_path):
    _two_differencers(tmp_path)
    (tmp_path / "overlay.toml").write_text('[finalize]\ndifferencer = "sfft"\n')
    rc, outputs = _run(tmp_path, "--settings", str(tmp_path / "overlay.toml"))
    assert rc == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    assert manifest.outputs[0].key["differencer"] == "sfft"
    assert fits.getheader(outputs / manifest.outputs[0].primary)["PPID"] == 16


def test_no_entry_for_the_configured_differencer_exits_65(tmp_path):
    (tmp_path / "overlay.toml").write_text('[finalize]\ndifferencer = "sfft"\n')
    rc, _ = _run(tmp_path, "--settings", str(tmp_path / "overlay.toml"))
    assert rc == ExitCode.INPUT_REJECTED


def _drop(kind_type_sign):
    def edit(manifest):
        manifest["outputs"] = [
            e for e in manifest["outputs"]
            if (e["kind"], e["key"].get("catalog_type"), e["key"].get("sign")) != kind_type_sign]
    return edit


@pytest.mark.parametrize("edit", [
    pytest.param(lambda m: m.update(stage="admit"), id="not-a-difference-manifest"),
    pytest.param(lambda m: m["unit"].update(kind="field"), id="wrong-unit-kind"),
    pytest.param(_drop(("difference-image", None, None)), id="no-difference-entry"),
    pytest.param(lambda m: m["outputs"].append(dict(m["outputs"][0], instance="01ARZ3NDEKTSV4RRFFQ69G5FAV")),
                 id="two-difference-entries"),
    pytest.param(lambda m: m["outputs"].append(dict(
        m["outputs"][1], instance="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        key={**m["outputs"][1]["key"], "difference": "01ARZ3NDEKTSV4RRFFQ69G5FAX"})),
                 id="an-entry-finalize-does-not-republish"),
    pytest.param(lambda m: m["outputs"][0]["registration"].update(md5="nope"),
                 id="invalid-registration-block"),
    pytest.param(lambda m: m["outputs"][2]["members"][0].update(bytes=1), id="member-size-mismatch"),
])
def test_bad_inputs_exit_65(tmp_path, edit):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    _edit_manifest(inputs, edit)
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()


def test_a_tampered_member_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    catalog = inputs / "work" / "zogy_diffimage_masked.txt"
    catalog.write_text(catalog.read_text().replace("1 10.0", "1 11.0"))
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED


def test_an_unreadable_difference_member_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    (inputs / "work" / "diffimage_masked.fits").write_bytes(b"not FITS" * 10)
    _edit_manifest(inputs, lambda m: _rehash(inputs, m, "work/diffimage_masked.fits"))
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED


def test_an_unreadable_execution_record_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    for record in (inputs / "exec").iterdir():
        record.write_text("{not json")
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED


@pytest.mark.parametrize("overlay", [
    '[finalize]\ndifferencer = "naive"\n',
    '[pipelines]\nzogy = "fifteen"\n',
    '[pipelines]\nzogy = 0\n',
    '[pipelines]\nzogy = 15\n[stamp]\nx = 1\n',
])
def test_bad_settings_exit_64(tmp_path, overlay):
    (tmp_path / "overlay.toml").write_text(overlay)
    rc, _ = _run(tmp_path, "--settings", str(tmp_path / "overlay.toml"))
    assert rc == ExitCode.USAGE


def test_a_differencer_without_a_pipelines_row_exits_65(tmp_path, monkeypatch):
    monkeypatch.setattr(finalize, "_check_settings", lambda settings: None)
    original = finalize.headers.ppid_for
    monkeypatch.setattr(finalize.headers, "ppid_for",
                        lambda differencer, pipelines: original(differencer, {"sfft": 16}))
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED


# The manifest transform on its own.


def test_finalized_catalog_entry_copies_members_and_rekeys(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    source = Manifest.read(inputs / "manifest.json")
    entry = source.outputs[3]           # Photutils positive: catalog + finder
    out = finalize.finalized_catalog_entry(
        inputs_dir=inputs, outputs_dir=tmp_path / "out", entry=entry,
        difference_instance="01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert isinstance(out, OutputEntry)
    assert out.instance != entry.instance and is_valid_ulid(out.instance)
    assert out.key == {**entry.key, "difference": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    assert out.members == entry.members
    assert out.primary == entry.primary
    assert out.registration == {**entry.registration, "copied_from": entry.instance}


def test_finalized_difference_entry_stamps_only_the_primary(tmp_path):
    inputs = tmp_path / "inputs"
    build_difference_output(inputs)
    source = Manifest.read(inputs / "manifest.json")
    entry = source.outputs[0]
    cards = [("RPSTAGE", "finalize", "RAPID stage that wrote this file")]
    out = finalize.finalized_difference_entry(
        inputs_dir=inputs, outputs_dir=tmp_path / "out", entry=entry,
        instance="01ARZ3NDEKTSV4RRFFQ69G5FAV", cards=cards)
    by_role = {m.role: m for m in out.members}
    for member in entry.members:
        if member.role == "difference":
            assert by_role["difference"].sha256 != member.sha256
        else:
            assert by_role[member.role] == member
    assert out.registration["md5"] == hashlib.md5(
        (tmp_path / "out" / out.primary).read_bytes()).hexdigest()
    assert out.registration["finalized_from"] == entry.instance
    assert {k: v for k, v in out.registration.items()
            if k not in ("md5", "finalized_from", "revision")} == {
        k: v for k, v in entry.registration.items() if k != "md5"}


# The fixture.


def test_selftest_knows_the_finalize_fixture():
    assert "finalize" in STAGE_NAMES


def test_make_stage_finalize_passes(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("run_fixture_finalize", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--workdir", str(tmp_path / "fx")]) == 0
    assert "stage-finalize: PASS" in capsys.readouterr().out


@pytest.mark.parametrize("real_tools", [False, True])
def test_rapidpipe_selftest_stage_finalize_passes(tmp_path, real_tools):
    assert run_selftest(stage="finalize", real_tools=real_tools,
                        work_dir=str(tmp_path / "wd"), output_location=None) == 0


# On the difference stage's own manifest (fake tools), not the synthetic one.


def test_finalizes_a_real_difference_stage_manifest(tmp_path, monkeypatch):
    import rapidpipe.stages.difference as difference
    from tests.unit.fakedifftools import (
        CDF_DIR,
        FakePsfCatalog,
        FakeToolRunner,
        build_input_set,
        fake_sip_to_pv,
    )

    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=FakeToolRunner(), sip_to_pv=fake_sip_to_pv, psf_catalog=FakePsfCatalog()))
    diff_inputs, diff_outputs = tmp_path / "diff-inputs", tmp_path / "diff-outputs"
    build_input_set(diff_inputs)
    overlay = tmp_path / "overlay.toml"
    # SFFT registration off: this test is about finalize passing a single
    # differencer's manifest through unchanged, not the two-differencer
    # drop, which test_the_configured_differencer_is_republished_and_the_
    # other_dropped and test_sfft_selected_from_a_two_differencer_manifest
    # already cover.
    overlay.write_text(f'[paths]\ncfg_path = "{CDF_DIR}"\n[sfft]\nregister_sfft = false\n')
    assert difference.main(_argv(diff_inputs, diff_outputs, "--settings", str(overlay))) == 0
    source = Manifest.read(diff_outputs / "manifest.json")

    outputs = tmp_path / "fin-outputs"
    assert finalize.main(_argv(diff_outputs, outputs)) == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    assert [e.kind for e in manifest.outputs] == [e.kind for e in source.outputs]
    diff = manifest.outputs[0]
    validate_difference_entry(diff.to_dict())
    header = fits.getheader(outputs / diff.primary)
    assert header["RPFINFRM"] == source.outputs[0].instance
    assert header["RPREFINS"] == source.outputs[0].key["reference"]
    assert header["RPSRCREV"] != ""
    for entry in manifest.outputs[1:]:
        validate_source_catalog_entry(entry.to_dict())
        assert entry.key["difference"] == diff.instance
