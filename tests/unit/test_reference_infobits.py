"""The reference's InfoBits and status, as `dev` grades a new reference (dev cb53d2c7).

Contracts: each fatal bit (2, 5, 7, 8, 16) sets status 0 and its bit; a
reference inside every threshold gets infobits 0 and status 1; a non-fatal
bit leaves status 1; a metric that is absent or NaN sets no bit, so the
PSF-fit catalog's bits stay clear in a build that makes no such catalog;
the header cards are cdf/infobitsref.txt's unreserved bits with the
thresholds in place; the FWHM percentile is taken over the mode's sample.
With the PSF-fit catalog attempted, its flag, count and the two limiting
magnitudes reach the grade, so bits 5, 6, 7 and 16 go live, a catalog not
made counting 0 sources; the limiting magnitudes follow the median
``flux_err`` and the background-noise formula, NaN when either input is
missing.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from rapidpipe.science.difference.fwhm import fwhm_percentile
from rapidpipe.science.limmag import background_sigma, psf_noise_pixels
from rapidpipe.science.reference import infobits, psfcat

REPO = Path(__file__).resolve().parents[2]
PARAMS = infobits.check_params(
    tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
    ["reference"]["infobits"])
DEFINITIONS = REPO / "cdf" / infobits.DEFINITIONS_FILE
SCALE_DEG = 0.11 / 3600.0


def _reference(tmp_path: Path, *, nan_fraction: float = 0.0) -> tuple[Path, Path]:
    """A 100 x 100 mosaic at 0.11 arcsec, every pixel covered, a fraction of them NaN."""
    header = fits.Header()
    header.update(CTYPE1="RA---TAN", CTYPE2="DEC--TAN", CRVAL1=270.0, CRVAL2=-29.0,
                  CRPIX1=50.5, CRPIX2=50.5, CDELT1=-SCALE_DEG, CDELT2=SCALE_DEG)
    image = np.full((100, 100), 0.05, dtype=np.float32)
    image.flat[:int(nan_fraction * image.size)] = np.nan
    fits.PrimaryHDU(data=image, header=header).writeto(tmp_path / "image.fits")
    fits.PrimaryHDU(data=np.full((100, 100), 3.0, dtype=np.float32)).writeto(tmp_path / "cov.fits")
    return tmp_path / "image.fits", tmp_path / "cov.fits"


def _grade(tmp_path: Path, nan_fraction: float = 0.0, **overrides) -> infobits.Grade:
    image, cov = _reference(tmp_path, nan_fraction=nan_fraction)
    values = dict(gmedian=0.05, datascale=0.01, satcount=0, fwhm_mode_pix=2.2,
                  fwhm_pctl_pix=3.0, nsexcat=4000, input_zptmags=[26.0, 26.0, 26.1])
    values.update(overrides)
    return infobits.grade(image, cov, DEFINITIONS, PARAMS, **values)


#: Metrics inside every threshold, as the grade sees them.
CLEAN = {"gmedian": 0.05, "datascale": 0.01, "pctbad": 1.0, "fwhm_med_arcsec": 0.24,
         "fwhm_pctl_arcsec": 0.33, "maglimcat": 26.0, "maglimit": 26.0, "npsfcat": 3000,
         "nsexcat": 4000, "zptmag_rms": 0.05, "psfcat_flag": True}


def test_a_reference_inside_every_threshold_is_status_1_infobits_0(tmp_path):
    graded = _grade(tmp_path)
    assert (graded.infobits, graded.status) == (0, 1)
    assert infobits.compute_refimage_infobits(CLEAN, PARAMS) == (0, 1)


@pytest.mark.parametrize("bit, metric, value", [
    (2, "pctbad", 70.0),
    (5, "maglimcat", 14.0),
    (7, "npsfcat", 3),
    (8, "nsexcat", 3),
    (16, "psfcat_flag", False),
])
def test_each_fatal_bit_sets_status_0(bit, metric, value):
    assert infobits.compute_refimage_infobits({**CLEAN, metric: value}, PARAMS) == (2**bit, 0)


def test_the_fatal_bits_the_grade_measures_set_status_0_on_a_synthetic_reference(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    bad = _grade(tmp_path / "a", nan_fraction=0.7)
    assert (bad.infobits, bad.status) == (2**2, 0)
    assert bad.metrics["pctbad"] == pytest.approx(70.0)
    few = _grade(tmp_path / "b", nsexcat=3)
    assert (few.infobits, few.status) == (2**8, 0)


def test_a_non_fatal_bit_leaves_status_1(tmp_path):
    graded = _grade(tmp_path, gmedian=5.0, fwhm_mode_pix=12.0)
    assert graded.bits == [0, 3] and graded.status == 1


def test_absent_or_nan_metrics_set_no_bit(tmp_path):
    assert infobits.compute_refimage_infobits(
        {"nsexcat": 4000, "maglimcat": np.nan, "pctbad": np.nan}, PARAMS) == (0, 1)
    assert not {"maglimcat", "maglimit", "npsfcat", "psfcat_flag"} & set(_grade(tmp_path).metrics)


def test_the_header_cards_are_the_unreserved_bits_with_thresholds():
    cards = infobits.infobit_cards(DEFINITIONS, PARAMS)
    assert [bit for _, bit, _ in cards] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 16]
    assert dict((k, c) for k, _, c in cards)["BIT02"] == \
        "PERCENT OF BAD/SATURATED PIXELS is > 65.0%"
    assert "90TH PCTILE" in dict((k, c) for k, _, c in cards)["BIT04"]
    assert infobits.format_infobits_comment(0) == "infoBits string; bit(s) = none"


def test_fwhm_percentile_is_over_the_point_sources_or_all_when_too_few():
    point = [[2.0 + 0.1 * i, 0, 1000.0, 10.0, 1.0] for i in range(20)]
    galaxies = [[8.0, 0, 1000.0, 10.0, 2.0] for _ in range(20)]
    assert fwhm_percentile(point + galaxies, 50.0) == pytest.approx(2.95)
    assert fwhm_percentile(point[:3] + galaxies, 50.0) == pytest.approx(8.0)


@pytest.mark.parametrize("change", [{"t03": "1.0"}, {"fatalinfobits": "2,5"},
                                    {"fatalinfobits": [40]}])
def test_bad_params_are_refused(change):
    table = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
    with pytest.raises(ValueError):
        infobits.check_params({**table["reference"]["infobits"], **change})


def test_the_catalogs_metrics_set_bits_5_6_7_and_16_when_it_was_attempted(tmp_path):
    for name in "abcde":
        (tmp_path / name).mkdir()
    clean = _grade(tmp_path / "a", psfcat_flag=True, npsfcat=3000, maglimcat=26.0, maglimit=26.0)
    assert (clean.infobits, clean.status) == (0, 1) and clean.metrics["psfcat_flag"] is True
    shallow = _grade(tmp_path / "b", psfcat_flag=True, npsfcat=3000, maglimcat=14.0, maglimit=14.0)
    assert (shallow.bits, shallow.status) == ([5, 6], 0)
    sparse = _grade(tmp_path / "c", psfcat_flag=True, npsfcat=3, maglimcat=26.0, maglimit=26.0)
    assert (sparse.bits, sparse.status) == ([7], 0)
    none = _grade(tmp_path / "d", psfcat_flag=False, npsfcat=50)
    assert (none.bits, none.status) == ([7, 16], 0) and none.metrics["npsfcat"] == 0
    assert none.metrics["maglimcat"] is None and none.metrics["maglimit"] is None
    assert not {"psfcat_flag", "npsfcat"} & set(_grade(tmp_path / "e").metrics)


def test_the_limiting_magnitudes_follow_the_catalogs_errors_and_the_background_formula(tmp_path):
    rng = np.random.default_rng(3)
    image = tmp_path / "image.fits"
    fits.PrimaryHDU(rng.normal(0.0, 0.05, size=(200, 200)).astype("float32")).writeto(image)
    yy, xx = np.mgrid[:21, :21] - 10
    psf = tmp_path / "psf.fits"
    fits.PrimaryHDU(np.exp(-(xx ** 2 + yy ** 2) / (2 * 1.3 ** 2))).writeto(psf)
    catalog = tmp_path / "cat.txt"
    catalog.write_text("id flux_err\n1 0.01\n2 0.03\n3 0.02\n4 nan\n5 -1\n")
    maglimcat, maglimit = psfcat.limiting_magnitudes(image, psf, catalog, 26.0, snr=5.0)
    assert maglimcat == pytest.approx(26.0 - 2.5 * np.log10(5.0 * 0.02))
    expected = 26.0 - 2.5 * np.log10(5.0 * np.sqrt(psf_noise_pixels(fits.getdata(psf)))
                                     * background_sigma(fits.getdata(image)))
    assert maglimit == pytest.approx(expected)
    assert np.isnan(psfcat.limiting_magnitudes(image, None, None, 26.0)).all()


def test_the_runs_psfcat_strings_parse_as_the_catalog_reads_them():
    table = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())["reference"]["psfcat"]
    parsed = psfcat.settings_from_table(table)
    assert (parsed.fit_shape, parsed.sharphi, parsed.min_separation, parsed.aperture_radius) == (
        (17, 17), 1.0, 1.5, 8.0)
