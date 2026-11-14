"""scripts/socsims_truth.py: the socsims fake-source dataset's truth from dev's injection catalogs.

Contracts: a gzipped file's first bytes give exactly its complete headers;
the light curves are dev's (a sinusoid in magnitude at its phase, a
gaussian burst of its peak amplitude at its peak time, the static
magnitude far from it); one row per source of the image's fields that lies
within dev's 50 px band, at its evaluated magnitude through the file's zero
point and the stem's keys (the exposure's name, the SCA and the fid); a field whose catalog is absent fails the file.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

_SPEC = importlib.util.spec_from_file_location(
    "socsims_truth", Path(__file__).resolve().parents[2] / "scripts" / "socsims_truth.py")
socsims_truth = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(socsims_truth)

STEM = "r0034001005001003061_0001_wfi08_f146"
RA0, DEC0, PX = 267.2556, -29.7599, 0.11 / 3600.0


def lite_bytes(n=64) -> bytes:
    """A gzipped lite file: a bare primary HDU, then SCI_ORIG with a TAN WCS centred on (RA0, DEC0)."""
    image = fits.ImageHDU(np.zeros((n, n), dtype=np.float32), name="SCI_ORIG")
    h = image.header
    h.update({"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": RA0, "CRVAL2": DEC0,
              "CRPIX1": (n + 1) / 2, "CRPIX2": (n + 1) / 2, "CD1_1": -PX, "CD1_2": 0.0, "CD2_1": 0.0,
              "CD2_2": PX, "MJD-OBS": 61685.5, "ZPTMAG": 26.86})
    out = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), image]).writeto(out)
    return gzip.compress(out.getvalue())


def source(kind: str, ra: float, dec: float, **parameters) -> dict:
    return {"type": kind, "ra": ra, "dec": dec, "parameters": parameters}


def test_headers_from_gz_gives_only_complete_headers():
    raw = lite_bytes()
    both = socsims_truth.headers_from_gz(raw)
    assert [h.get("EXTNAME") for h in both] == [None, "SCI_ORIG"]
    assert both[1]["NAXIS1"] == 64 and both[1]["MJD-OBS"] == 61685.5
    assert len(socsims_truth.headers_from_gz(raw[:400])) < 2


def test_light_curves_are_devs():
    sinus = source("sinusoidal", 0, 0, magnitude=22.0, amplitude=0.5, period=10.0, phase=0.25)  # sin = 1 at t = 0
    burst = source("gaussian", 0, 0, magnitude=24.0, peak_amplitude=1.0, peak_time=100.0, sigma=0.1)
    at_zero = socsims_truth.evaluate([sinus, burst], 0.0)
    assert -2.5 * np.log10(at_zero[0]) == pytest.approx(22.5)
    assert -2.5 * np.log10(at_zero[1]) == pytest.approx(24.0)
    at_peak = socsims_truth.evaluate([burst], 100.0)
    assert -2.5 * np.log10(at_peak[0]) == pytest.approx(23.0)
    with pytest.raises(ValueError):
        socsims_truth.evaluate([source("constant", 0, 0)], 0.0)


def test_one_row_per_source_within_devs_band_at_its_magnitude():
    header = socsims_truth.headers_from_gz(lite_bytes())[1]
    image = socsims_truth.image(header, STEM)
    assert image["centre"] == pytest.approx((RA0, DEC0), abs=1e-9)
    assert (image["exposure"], image["sca"], image["fid"]) == ("r0034001005001003061_0001", 8, 8)
    centre, *neighbours = socsims_truth.fields(RA0, DEC0)
    cosd = np.cos(np.radians(DEC0))
    inside = source("sinusoidal", RA0 + 10 * PX / cosd, DEC0, magnitude=22.0, amplitude=0.0, period=1.0, phase=0.0)
    in_band = source("sinusoidal", RA0, DEC0 + 70 * PX, magnitude=23.0, amplitude=0.0, period=1.0, phase=0.0)
    outside = source("sinusoidal", RA0, DEC0 + 90 * PX, magnitude=23.0, amplitude=0.0, period=1.0, phase=0.0)
    catalogs = {f: {} for f in neighbours}
    catalogs[centre] = {"1": inside, "2": in_band, "3": outside}
    rows = socsims_truth.truth_rows(catalogs, image, "ds")
    assert [r["src_id"] for r in rows] == [1, 2]
    assert rows[0]["x"] == pytest.approx(31.5 - 10, abs=1e-3) and rows[0]["y"] == pytest.approx(31.5, abs=1e-3)
    assert rows[0]["mag_ab"] == pytest.approx(22.0)
    assert rows[0]["dnps"] == pytest.approx(10 ** (-0.4 * (22.0 - 26.86)))
    assert {r["dataset"] for r in rows} == {"ds"} and {r["mjd"] for r in rows} == {61685.5}
    with pytest.raises(KeyError):
        socsims_truth.truth_rows({centre: catalogs[centre]}, image, "ds")
