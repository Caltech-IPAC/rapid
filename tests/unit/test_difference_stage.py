"""Tests for rapidpipe.stages.difference, run end to end with fake tools.

Every external tool is replaced by tests/unit/fakedifftools.py's fakes
(``Toolkit`` is monkeypatched), so these tests exercise the stage's own
logic -- input validation, `dev`'s step order and command lines, the
catalog-outcome mask, the SFFT and naive branches, and the manifest it
publishes -- not the tools' science.
"""

from __future__ import annotations

import json
import shlex

import numpy as np
import pytest
from astropy.io import fits

import rapidpipe.stages.difference as difference
from rapidpipe.products.diffimage import validate_difference_entry, validate_source_catalog_entry
from rapidpipe.products.manifest import Manifest
from rapidpipe.stages.contract import ExitCode

from .fakedifftools import (
    CDF_DIR,
    L2_INSTANCE,
    REF_INSTANCE,
    FakePsfCatalog,
    FakeToolRunner,
    _l2_registration,
    build_input_set,
    fake_sip_to_pv,
)


@pytest.fixture()
def fakes(monkeypatch):
    runner = FakeToolRunner()
    catalog = FakePsfCatalog()
    monkeypatch.setattr(
        difference, "toolkit",
        lambda: difference.Toolkit(runner=runner, sip_to_pv=fake_sip_to_pv, psf_catalog=catalog))
    return runner, catalog


def _run(tmp_path, *, overlay: str = "", build_kwargs=None, argv_extra=()):
    inputs = tmp_path / "inputs"
    if not (inputs / "manifest.json").exists():
        build_input_set(inputs, **(build_kwargs or {}))
    settings = tmp_path / "overlay.toml"
    settings.write_text(
        f'[paths]\ncfg_path = "{CDF_DIR}"\n[statistics]\nclip_correction_seed = 1\n' + overlay)
    outputs = tmp_path / "outputs"
    code = difference.main([
        "--run", "r1", "--unit", "e20260821001234/SCA07", "--attempt", "a1",
        "--inputs", str(inputs), "--outputs", str(outputs), "--settings", str(settings),
        *argv_extra])
    return code, outputs


def _manifest(outputs) -> Manifest:
    return Manifest.read(outputs / "manifest.json")


def _entries(manifest, kind):
    return [e for e in manifest.outputs if e.kind == kind]


def _exec_record(outputs) -> dict:
    return json.loads((outputs / "exec" / "a1.json").read_text())


# ----------------------------------------------------------------------
# The whole stage
# ----------------------------------------------------------------------


def test_full_run_publishes_a_valid_zogy_instance(tmp_path, fakes):
    code, outputs = _run(tmp_path)
    assert code == ExitCode.SUCCESS
    manifest = _manifest(outputs)
    assert manifest.stage == "difference"

    diffs = _entries(manifest, "difference-image")
    assert len(diffs) == 1
    entry = diffs[0]
    registration = validate_difference_entry(entry.to_dict())
    assert entry.key["differencer"] == "zogy"
    assert entry.key["l2"] == L2_INSTANCE
    assert entry.key["reference"] == REF_INSTANCE
    assert entry.key["settings_hash"].startswith("sha256:")
    assert {m.role for m in entry.members} == {"difference", "uncertainty", "significance", "psf"}
    assert entry.primary == "diff/zogy_diffimage_masked.fits"
    assert registration.detection_role == "significance"
    assert registration.catalog_outcome_bits == 0
    assert registration.centre == _l2_registration()["centre"]
    assert registration.corners == _l2_registration()["corners"]
    assert registration.reference_rfid is None
    assert registration.source_counts["sextractor"]["positive"] >= 1  # the transient

    catalogs = _entries(manifest, "source-catalog")
    assert {(c.key["catalog_type"], c.key["sign"]) for c in catalogs} == {
        ("sextractor", "positive"), ("sextractor", "negative"),
        ("photutils", "positive"), ("photutils", "negative")}
    for catalog in catalogs:
        validate_source_catalog_entry(catalog.to_dict())
        assert catalog.key["difference"] == entry.instance

    assert manifest.inputs.products == {"l2-image": L2_INSTANCE, "reference-image": REF_INSTANCE}
    assert _exec_record(outputs)["notes"] == {"sfft": {"ran": True, "succeeded": True, "exit_code": 0}}


def test_tools_run_in_dev_order(tmp_path, fakes):
    runner, _ = fakes
    _run(tmp_path)
    assert runner.tools_called() == [
        "sextractor",                   # science-image catalog
        "swarp", "swarp", "swarp",      # reference image, coverage map, uncertainty
        "bkgest",
        "sextractor", "sextractor",     # gain-match catalogs, science then reference
        "zogy",
        "sextractor", "sextractor",     # ZOGY positive, negative
        "sextractor", "sextractor",     # SFFT positive, negative
        "sextractor", "sextractor",     # naive positive, negative
        "sfft",
    ]
    # (The fake records the SFFT shell call separately; it runs after the
    # ZOGY catalogs and before the SFFT catalogs.)


def test_zogy_command_is_devs(tmp_path, fakes):
    runner, _ = fakes
    _run(tmp_path)
    zogy = next(a for a in runner.calls if a[1].endswith("py_zogy.py"))
    assert zogy[1] == "/code/modules/zogy/v21Aug2018/py_zogy.py"
    assert zogy[2:8] == [
        "bkg_subbed_science_image.fits",
        "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
        "sciimage_psf_f184_sca07_normalized.fits",
        "refimage_psf_f146_sca07.fits",
        "Roman_WAS_simple_model_F184_1234_7_reformatted_unc.fits",
        "awaicgen_output_mosaic_uncert_image_resampled_gainmatched.fits",
    ]
    assert zogy[10:12] == ["0.0", "0.0"]   # astrometric sigma, dev's override
    assert zogy[12:] == ["zogy_diffimage.fits", "diffpsf.fits", "scorrimage.fits"]


def test_astrometric_sigma_setting_reaches_zogy_and_registration(tmp_path, fakes):
    runner, _ = fakes
    code, outputs = _run(tmp_path, overlay="[zogy]\nastrometric_sigma = 0.3\n")
    assert code == ExitCode.SUCCESS
    zogy = next(a for a in runner.calls if a[1].endswith("py_zogy.py"))
    assert zogy[10:12] == ["0.3", "0.3"]
    residual = _entries(_manifest(outputs), "difference-image")[0].registration["registration_residual"]
    assert residual["x_rms"] == 0.3 and residual["y_rms"] == 0.3


def test_zogy_catalogs_detect_on_scorr_with_devs_overrides(tmp_path, fakes):
    runner, _ = fakes
    _run(tmp_path)
    sex = [a for a in runner.calls if a[0] == "sex"]
    zogy_pos, zogy_neg = sex[3], sex[4]
    assert zogy_pos[1] == "scorrimage_masked.fits,zogy_diffimage_masked.fits"
    assert zogy_neg[1] == "scorrimage_masked_negative.fits,zogy_diffimage_masked_negative.fits"
    for call in (zogy_pos, zogy_neg):
        assert call[call.index("-WEIGHT_TYPE") + 1] == "NONE,MAP_RMS"
        assert call[call.index("-FILTER") + 1] == "N"
        assert call[call.index("-WEIGHT_IMAGE") + 1] == "zogy_diffimage_uncert_masked.fits"
    naive_pos = sex[7]
    assert naive_pos[1] == "naive_diffimage_masked.fits,naive_diffimage_masked.fits"
    # dev reverts the overrides before the naive catalogs.
    assert naive_pos[naive_pos.index("-WEIGHT_TYPE") + 1] == "MAP_RMS,MAP_RMS"
    assert naive_pos[naive_pos.index("-FILTER") + 1] == "Y"


def test_sfft_command_is_devs(tmp_path, fakes):
    runner, _ = fakes
    _run(tmp_path)
    (command,) = runner.shell_calls
    assert command == (
        "source /sfft_env/bin/activate && python3.11 /code/modules/sfft/sfft_rapid_rimtimsim.py "
        "./bkg_subbed_science_image.fits ./awaicgen_output_mosaic_image_resampled_gainmatched.fits "
        "--bsmaskvalue 20000.0 --bsmaskradius 30.0 --scipsf sciimage_psf_f184_sca07_normalized.fits "
        "&& deactivate")


def test_uncovered_reference_pixels_are_nan_in_the_difference(tmp_path, fakes):
    _, outputs = _run(tmp_path)
    data = fits.getdata(outputs / "diff" / "zogy_diffimage_masked.fits")
    assert np.isnan(data[:, :3]).all()
    assert np.isfinite(data[:, 3:]).all()


# ----------------------------------------------------------------------
# SFFT: runs as dev runs it; registration off by default
# ----------------------------------------------------------------------


def test_sfft_failure_is_not_fatal_and_is_noted(tmp_path, fakes):
    runner, _ = fakes
    runner.sfft_exit_code = 1
    code, outputs = _run(tmp_path, overlay="[sfft]\nregister_sfft = true\n")
    assert code == ExitCode.SUCCESS
    manifest = _manifest(outputs)
    diffs = _entries(manifest, "difference-image")
    assert [d.key["differencer"] for d in diffs] == ["zogy"]   # never a partial SFFT bundle
    assert _exec_record(outputs)["notes"]["sfft"] == {"ran": True, "succeeded": False, "exit_code": 1}
    assert not (outputs / "diff" / "sfftdiffimage_masked_negative.fits").exists()


def test_sfft_is_not_registered_by_default(tmp_path, fakes):
    _, outputs = _run(tmp_path)
    assert [d.key["differencer"] for d in _entries(_manifest(outputs), "difference-image")] == ["zogy"]
    assert (outputs / "diff" / "sfftdiffimage_masked.fits").exists()   # a diagnostic


def test_register_sfft_adds_its_own_instance(tmp_path, fakes):
    code, outputs = _run(tmp_path, overlay="[sfft]\nregister_sfft = true\n")
    assert code == ExitCode.SUCCESS
    manifest = _manifest(outputs)
    sfft = next(d for d in _entries(manifest, "difference-image") if d.key["differencer"] == "sfft")
    registration = validate_difference_entry(sfft.to_dict())
    assert {m.role for m in sfft.members} == {"difference", "uncertainty", "psf", "kernel"}
    assert registration.detection_role == "difference"
    sfft_catalogs = [c for c in _entries(manifest, "source-catalog")
                     if c.key["difference"] == sfft.instance]
    assert len(sfft_catalogs) == 4


def test_run_sfft_off_runs_no_sfft(tmp_path, fakes):
    runner, _ = fakes
    code, outputs = _run(tmp_path, overlay="[sfft]\nrun_sfft = false\n")
    assert code == ExitCode.SUCCESS
    assert runner.shell_calls == []
    assert "notes" not in _exec_record(outputs)


# ----------------------------------------------------------------------
# The catalog-outcome mask and the naive diagnostic
# ----------------------------------------------------------------------


def test_missing_photutils_catalog_sets_its_bit_and_a_null_count(tmp_path, fakes):
    _, catalog = fakes
    catalog.fail = ("positive zogy difference image", "negative naive difference image")
    code, outputs = _run(tmp_path)
    assert code == ExitCode.SUCCESS
    manifest = _manifest(outputs)
    registration = _entries(manifest, "difference-image")[0].registration
    assert registration["catalog_outcome_bits"] == (1 << 0) | (1 << 5)
    assert registration["source_counts"]["photutils"]["positive"] is None
    assert {(c.key["catalog_type"], c.key["sign"]) for c in _entries(manifest, "source-catalog")} == {
        ("sextractor", "positive"), ("sextractor", "negative"), ("photutils", "negative")}


def test_sfft_catalog_bits_count_only_when_sfft_ran(tmp_path, fakes):
    runner, catalog = fakes
    catalog.fail = ("positive sfft difference image",)
    _, outputs = _run(tmp_path)
    assert _entries(_manifest(outputs), "difference-image")[0].registration[
        "catalog_outcome_bits"] == 1 << 2


def test_naive_is_never_registered_and_can_be_turned_off(tmp_path, fakes):
    runner, _ = fakes
    code, outputs = _run(tmp_path)
    assert (outputs / "diff" / "naive_diffimage_masked.fits").exists()
    assert all(d.key["differencer"] != "naive" for d in _entries(_manifest(outputs), "difference-image"))

    other = tmp_path / "off"
    other.mkdir()
    code, outputs = _run(other, overlay="[naive_diffimage]\nnaive_diffimage_flag = false\n")
    assert code == ExitCode.SUCCESS
    assert not (outputs / "diff" / "naive_diffimage_masked.fits").exists()


# ----------------------------------------------------------------------
# References registered by dev
# ----------------------------------------------------------------------


def test_legacy_reference_carries_its_rfid_and_is_not_a_dependency(tmp_path, fakes):
    code, outputs = _run(tmp_path, build_kwargs={"rfid": 4321, "ref_infobits": 8})
    assert code == ExitCode.SUCCESS
    manifest = _manifest(outputs)
    registration = _entries(manifest, "difference-image")[0].registration
    assert registration["reference_rfid"] == 4321
    assert registration["infobits_reference"] == 8
    assert manifest.inputs.products == {"l2-image": L2_INSTANCE}


def test_uncompressed_science_image_is_accepted(tmp_path, fakes):
    code, _ = _run(tmp_path, build_kwargs={"gzip_science": False})
    assert code == ExitCode.SUCCESS


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def _edit_manifest(tmp_path, edit):
    inputs = tmp_path / "inputs"
    build_input_set(inputs)
    path = inputs / "manifest.json"
    manifest = json.loads(path.read_text())
    edit(manifest)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("edit", [
    lambda m: m["outputs"].pop(1),                                        # no reference
    lambda m: m["outputs"].pop(4),                                        # one psf only
    lambda m: m["outputs"][3]["key"].update(applies_to="reference"),      # two reference psfs
    lambda m: m["outputs"][0]["members"][0].update(sha256="sha256:" + "0" * 64),
    lambda m: m["outputs"][1]["members"].pop(1),                          # no coverage map
    lambda m: m["outputs"][1]["registration"].update(infobits=None),
    lambda m: m["outputs"][0]["registration"].pop("exptime"),
])
def test_bad_input_set_exits_65_and_publishes_nothing(tmp_path, fakes, edit):
    _edit_manifest(tmp_path, edit)
    code, outputs = _run(tmp_path)
    assert code == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()


@pytest.mark.parametrize("overlay", [
    "[fake_sources]\ninject_fake_sources_flag = true\n",
    '[zogy]\ndetection_role = "kernel"\n',
    '[sfft]\ndetection_role = "significance"\n',
    "[zogy]\nno_such_setting = 1\n",
])
def test_bad_settings_exit_64(tmp_path, fakes, overlay):
    code, outputs = _run(tmp_path, overlay=overlay)
    assert code == ExitCode.USAGE
    assert not (outputs / "manifest.json").exists()


def test_dry_run_runs_no_tools_and_writes_nothing(tmp_path, fakes):
    runner, _ = fakes
    code, outputs = _run(tmp_path, argv_extra=("--dry-run",))
    assert code == ExitCode.SUCCESS
    assert runner.calls == []
    assert not outputs.exists()


def test_cli_dispatches_to_difference_stage(tmp_path, fakes):
    from rapidpipe.cli.main import main as cli_main

    build_input_set(tmp_path / "inputs")
    code = cli_main(shlex.split(
        f"stage difference --run r1 --unit u1 --attempt a1 --inputs {tmp_path / 'inputs'} "
        f"--outputs {tmp_path / 'outputs'} --dry-run"))
    assert code == ExitCode.SUCCESS


def test_declaration():
    d = difference.DECLARATION
    d.validate()
    assert d.database_access == "none"
    assert d.produces == ("difference-image", "source-catalog")
