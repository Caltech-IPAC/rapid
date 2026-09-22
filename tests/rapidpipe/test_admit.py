"""Tests for rapidpipe.stages.admit.

A fixture builds a small FITS file with a TAN-SIP WCS and every header
keyword `admit` reads, plus a delivery manifest.json pointing at it (the
shape the products page describes: stage 'delivery', one l2-image entry,
format_version 'delivered', a key naming exposure/detector/version, the
delivered file as its member).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.products.l2image import L2ImageRegistration
from rapidpipe.products.manifest import Manifest
from rapidpipe.stages.admit import DECLARATION, _body, main
from rapidpipe.stages.contract import ExitCode, run_stage

EXPOSURE_ID = "e20260821001234"
DETECTOR = "7"
DELIVERED_VERSION = "1"

CRVAL1 = 269.45
CRVAL2 = -28.77
NAXIS = 64
PIXEL_SCALE_DEG = 0.11 / 3600.0  # ~0.11 arcsec/pixel


def _header_dict(*, include_checksums_placeholder=True) -> dict:
    return {
        "SCA-NUM": DETECTOR,
        "DATE-OBS": "2026-08-21T00:12:34",
        "MJD-OBS": 61273.008726851855,
        "EXPTIME": 140.0,
        "FILTER": "F184",
        "RA_TARG": CRVAL1,
        "DEC_TARG": CRVAL2,
        "PA_OBSY": 12.5,
        "PA_FPA": 1.5,
        "ZPTMAG": 25.5,
        "SKY-MEAN": 100.0,
        "EQUINOX": 2000.0,
    }


def _build_fits(path, *, with_checksums=True, delete_keyword=None, sca_value=None):
    rng = np.random.default_rng(0)
    data = rng.normal(size=(NAXIS, NAXIS)).astype(np.float32)

    header = fits.Header()
    for keyword, value in _header_dict().items():
        header[keyword] = value
    if sca_value is not None:
        header["SCA-NUM"] = sca_value
    if delete_keyword is not None and delete_keyword in header:
        del header[delete_keyword]

    header["CRVAL1"] = CRVAL1
    header["CRVAL2"] = CRVAL2
    header["CRPIX1"] = NAXIS / 2.0
    header["CRPIX2"] = NAXIS / 2.0
    header["CD1_1"] = -PIXEL_SCALE_DEG
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = PIXEL_SCALE_DEG
    header["CTYPE1"] = "RA---TAN-SIP"
    header["CTYPE2"] = "DEC--TAN-SIP"
    header["CUNIT1"] = "deg"
    header["CUNIT2"] = "deg"
    header["A_ORDER"] = 2
    header["A_2_0"] = 1.0e-7
    header["A_0_2"] = 2.0e-7
    header["B_ORDER"] = 2
    header["B_2_0"] = 3.0e-7
    header["B_0_2"] = 4.0e-7

    hdu = fits.PrimaryHDU(data=data, header=header)
    hdul = fits.HDUList([hdu])
    hdul.writeto(path, checksum=with_checksums, overwrite=True)


def _sha256_of(path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _build_delivery(tmp_path, *, fits_name="delivered.fits", **fits_kwargs):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    fits_path = inputs_dir / fits_name
    _build_fits(fits_path, **fits_kwargs)

    byte_size = fits_path.stat().st_size
    sha256_hex = _sha256_of(fits_path)

    manifest = {
        "schema_version": "1",
        "run": "delivery",
        "unit": {"kind": "detector-image", "id": f"{EXPOSURE_ID}/SCA{DETECTOR}"},
        "stage": "delivery",
        "attempt": "delivery-0",
        "execution_record": "exec/delivery-0.json",
        "inputs": {"manifest": "s3://bucket/root/manifest.json", "products": {}, "result_sets": []},
        "outputs": [
            {
                "kind": "l2-image",
                "format_version": "delivered",
                "instance": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "key": {
                    "exposure": EXPOSURE_ID,
                    "detector": DETECTOR,
                    "version": DELIVERED_VERSION,
                },
                "primary": fits_name,
                "members": [
                    {
                        "role": "image",
                        "path": fits_name,
                        "bytes": byte_size,
                        "sha256": f"sha256:{sha256_hex}",
                    }
                ],
                "registration": {"source": "socsims-fakesrc-fits-20260807-lite"},
            }
        ],
    }
    (inputs_dir / "manifest.json").write_text(json.dumps(manifest))
    return inputs_dir, fits_path


def _argv(inputs_dir, outputs_dir, *, extra=()):
    return [
        "--run", "r1", "--unit", f"{EXPOSURE_ID}/SCA{DETECTOR}", "--attempt", "a1",
        "--inputs", str(inputs_dir), "--outputs", str(outputs_dir),
        *extra,
    ]


def test_full_run_publishes_manifest_with_valid_registration(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path)
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)

    manifest = Manifest.read(outputs_dir / "manifest.json")
    assert len(manifest.outputs) == 1
    entry = manifest.outputs[0]
    assert entry.kind == "l2-image"
    assert entry.key == {
        "exposure": EXPOSURE_ID, "detector": DETECTOR, "version": DELIVERED_VERSION,
    }

    primary_path = outputs_dir / entry.primary
    assert primary_path.exists()
    assert primary_path.parent.name == "l2"

    primary_member = next(m for m in entry.members if m.path == entry.primary)
    assert primary_member.bytes == primary_path.stat().st_size
    assert primary_member.sha256 == f"sha256:{_sha256_of(primary_path)}"

    registration = L2ImageRegistration.from_dict(entry.registration)
    registration.validate()
    assert registration.md5 == hashlib.md5(primary_path.read_bytes()).hexdigest()
    assert registration.delivery_instance == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert registration.delivery_source == "socsims-fakesrc-fits-20260807-lite"

    assert manifest.inputs.products == {}


def test_centre_and_corners_match_wcs(tmp_path):
    from astropy.coordinates import SkyCoord

    inputs_dir, fits_path = _build_delivery(tmp_path)
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)

    manifest = Manifest.read(outputs_dir / "manifest.json")
    registration = manifest.outputs[0].registration

    with fits.open(fits_path) as hdul:
        header = hdul[0].header
    wcs = WCS(header)

    cx = 0.5 * NAXIS + 0.5 - 1.0
    cy = 0.5 * NAXIS + 0.5 - 1.0
    expected_centre = wcs.pixel_to_world(cx, cy)
    actual_centre = SkyCoord(
        registration["centre"]["ra"], registration["centre"]["dec"], unit="deg")
    assert expected_centre.separation(actual_centre).arcsec < 1.0

    corner_pixels = [
        (0.5 - 1.0, 0.5 - 1.0),
        (NAXIS + 0.5 - 1.0, 0.5 - 1.0),
        (NAXIS + 0.5 - 1.0, NAXIS + 0.5 - 1.0),
        (0.5 - 1.0, NAXIS + 0.5 - 1.0),
    ]
    for (x, y), corner in zip(corner_pixels, registration["corners"]):
        expected = wcs.pixel_to_world(x, y)
        actual = SkyCoord(corner[0], corner[1], unit="deg")
        assert expected.separation(actual).arcsec < 1.0


def test_status_is_one_with_checksums_present(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path, with_checksums=True)
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs_dir / "manifest.json")
    assert manifest.outputs[0].registration["status"] == 1


def test_status_is_zero_with_checksums_absent_when_not_required(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path, with_checksums=False)
    outputs_dir = tmp_path / "outputs"
    settings_path = tmp_path / "overlay.toml"
    settings_path.write_text("[verify]\nrequire_fits_checksums = false\n")
    rc = run_stage(
        DECLARATION, _body,
        _argv(inputs_dir, outputs_dir, extra=["--settings", str(settings_path)]))
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs_dir / "manifest.json")
    assert manifest.outputs[0].registration["status"] == 0


def test_missing_checksums_exit_65_when_required(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path, with_checksums=False)
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_corrupted_stated_sha256_exits_65_and_publishes_nothing(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][0]["members"][0]["sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs_dir / "manifest.json").exists()


@pytest.mark.parametrize("mutation,expected", [
    ("stage", "difference"),
])
def test_wrong_delivery_stage_exits_65(tmp_path, mutation, expected):
    inputs_dir, _ = _build_delivery(tmp_path)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[mutation] = expected
    manifest_path.write_text(json.dumps(manifest))

    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_two_output_entries_exits_65(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"].append(dict(manifest["outputs"][0]))
    manifest_path.write_text(json.dumps(manifest))

    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_missing_key_version_exits_65(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["outputs"][0]["key"]["version"]
    manifest_path.write_text(json.dumps(manifest))

    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_detector_disagreeing_with_sca_num_exits_65(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][0]["key"]["detector"] = "99"
    manifest_path.write_text(json.dumps(manifest))

    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_missing_required_keyword_exits_65(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path, delete_keyword="MJD-OBS")
    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_optional_keywords_absent_give_none_not_zero(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path, delete_keyword="ZPTMAG")
    outputs_dir = tmp_path / "outputs"
    rc = run_stage(DECLARATION, _body, _argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs_dir / "manifest.json")
    assert manifest.outputs[0].registration["zptmag"] is None


def test_dry_run_exits_zero_and_writes_nothing(tmp_path):
    inputs_dir, _ = _build_delivery(tmp_path)
    outputs_dir = tmp_path / "outputs"
    rc = main(_argv(inputs_dir, outputs_dir, extra=["--dry-run"]))
    assert rc == int(ExitCode.SUCCESS)
    assert not outputs_dir.exists() or not any(outputs_dir.iterdir())


def test_cli_dispatches_to_admit_stage(tmp_path):
    # Invoked out-of-process, through rapidpipe.cli.main.main's own
    # __main__ entrypoint, rather than imported directly in this test
    # module: tests/rapidpipe/__init__.py and the installed rapidpipe
    # package share a name, and pytest's rootdir-based sys.path insertion
    # (no pytest.ini/pyproject pytest config sets an import mode) makes a
    # *fresh* bare "from rapidpipe import ..." resolve to
    # tests/rapidpipe/__init__.py instead of the installed package -- a
    # pre-existing property of this test tree's layout, not something
    # admit's own tests can fix. A subprocess exercises the same
    # "rapidpipe stage admit ..." entrypoint a real invocation uses,
    # without sharing this test process's sys.modules state.
    inputs_dir, _ = _build_delivery(tmp_path)
    outputs_dir = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, "-m", "rapidpipe.cli.main",
         "stage", "admit", *_argv(inputs_dir, outputs_dir)],
        capture_output=True, text=True)
    assert result.returncode == int(ExitCode.SUCCESS), result.stderr
    assert (outputs_dir / "manifest.json").exists()
