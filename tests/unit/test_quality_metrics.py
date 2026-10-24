"""The quality metrics' contracts, on synthetic stars.

The difference job's: a difference with no residual has core and ring 0
and a mismatched PSF leaves a positive core inside a negative ring; a
known shift is the registration offset; a defect pixel is not a residual
star; the usable fraction counts finite covered pixels; a source within 3
px of a catalog position is on a reference star. The reference job's: the
stars' width is theirs, the wider galaxies set aside; the coverage histogram is over the
covered pixels; nothing is zero that could not be measured.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from rapidpipe.science.difference import metrics
from rapidpipe.science.reference import measure

N = 400


def gaussians(positions, sigma, flux=1000.0, shape=(N, N)):
    rows, cols = np.mgrid[:shape[0], :shape[1]]
    image = np.zeros(shape)
    for x, y in positions:
        image += flux / (2 * np.pi * sigma ** 2) * np.exp(
            -((cols - x) ** 2 + (rows - y) ** 2) / (2 * sigma ** 2))
    return image


STARS = [(60.3 + 70 * i, 80.6 + 60 * j) for i in range(4) for j in range(4)]


def found(positions):
    return np.array([(x, y, 1000.0 - k) for k, (x, y) in enumerate(positions)])


def test_no_residual_gives_zero_core_and_ring():
    reference = gaussians(STARS, 1.1)
    stars = metrics.residual_stars(found(STARS), reference.shape, reference)
    out = metrics.residuals(reference, np.zeros_like(reference), stars)
    assert out["n_residual_stars"] == len(STARS)
    assert out["core_fraction"] == pytest.approx(0, abs=1e-9)
    assert out["ring_fraction"] == pytest.approx(0, abs=1e-9)


def test_a_narrower_science_psf_leaves_a_positive_core_in_a_negative_ring():
    reference = gaussians(STARS, 1.1)
    out = metrics.residuals(reference, gaussians(STARS, 0.95) - reference,
                            metrics.residual_stars(found(STARS), reference.shape, reference))
    assert out["core_fraction"] > 0.01 and out["ring_fraction"] < -0.01


def test_a_defect_pixel_is_not_a_residual_star():
    reference = gaussians(STARS, 1.1)
    reference[200, 300] = 5000.0
    stars = metrics.residual_stars(found([(300.0, 200.0), *STARS]), reference.shape, reference)
    assert (300.0, 200.0) not in stars and len(stars) == len(STARS)


def test_a_known_shift_is_the_registration_offset():
    reference = gaussians(STARS, 1.0)
    science = gaussians([(x + 0.2, y - 0.1) for x, y in STARS], 1.0)
    out = metrics.registration(science, reference, np.array(STARS))
    assert out["registration_dx_px"] == pytest.approx(0.2, abs=0.01)
    assert out["registration_dy_px"] == pytest.approx(-0.1, abs=0.01)
    assert out["n_registration_stars"] == len(STARS)


def test_without_stars_the_measurements_are_absent():
    image = np.zeros((N, N))
    assert metrics.residuals(image, image, []) == {}
    assert metrics.registration(image, image, np.empty((0, 2))) == {}
    assert "on_reference_star_fraction" not in metrics.source_counts(
        np.array([1.0]), np.array([1.0]), np.array([10.0]), np.array([1.0]), None)


def test_usable_fraction_counts_finite_covered_pixels():
    diff = np.zeros((10, 10))
    diff[0, :] = np.nan
    cov = np.ones((10, 10))
    cov[1, :] = 0.2
    assert metrics.usable_fraction(diff, cov, 0.5) == pytest.approx(0.8)


def test_source_counts_and_the_share_on_reference_stars():
    x, y = np.array([10.0, 50.0, 90.0]), np.array([10.0, 50.0, 90.0])
    out = metrics.source_counts(x, y, np.array([100.0, 10.0, 30.0]), np.array([1.0, 1.0, 1.0]),
                                np.array([[11.0, 12.0], [200.0, 200.0]]))
    assert out == {"n_sources": 3, "n_sources_snr_gt20": 2,
                   "on_reference_star_fraction": pytest.approx(1 / 3)}


def test_the_reference_metrics(tmp_path):
    image = gaussians(STARS, 1.2) + gaussians([(330.0, 330.0), (350.0, 60.0)], 2.0)
    cov = np.full((N, N), 3.0)
    cov[:100, :] = 1.5
    cov[:50, :] = 0.0
    fits.PrimaryHDU(image).writeto(tmp_path / "image.fits")
    fits.PrimaryHDU(cov).writeto(tmp_path / "cov.fits")
    rows = [[str(x + 1), str(y + 1), "0", "1000", "1"] for x, y in STARS]
    rows += [["331", "331", "0", "1000", "1"], ["351", "61", "0", "1000", "1"]]  # galaxies, wider
    m = measure.MosaicMeasurements(*([0.0] * 10), gmin=-1.0, gmax=99.0)
    out = measure.quality_metrics(tmp_path / "image.fits", tmp_path / "cov.fits", rows, m)
    assert out["star_sigma_px"] == pytest.approx(1.2, abs=0.05)
    assert out["n_sigma_stars"] == len(STARS)
    assert (out["coverage_lt1"], out["coverage_lt2"], out["coverage_lt3"]) == \
        pytest.approx((0.0, 50 / 350, 50 / 350))
    assert (out["gmin"], out["gmax"]) == (-1.0, 99.0)
    assert "star_sigma_px" not in measure.quality_metrics(
        tmp_path / "image.fits", tmp_path / "cov.fits", [], m)


def test_the_bright_footprint_grows_as_the_square_root_of_the_flux_above_its_floor():
    zp = 25.0
    flux_at_15 = 10 ** (0.4 * (zp - 15.0))
    radii = metrics.footprint_radii(np.array([flux_at_15, 4 * flux_at_15, 1e-3, 0.0]), zp)
    assert radii[0] == pytest.approx(metrics.FOOTPRINT_R15_PX)
    assert radii[1] == pytest.approx(2 * metrics.FOOTPRINT_R15_PX)
    assert radii[2] == radii[3] == metrics.FOOTPRINT_MIN_PX


def test_a_source_inside_a_footprint_is_flagged_and_one_outside_is_not():
    stars = np.array([[100.0, 100.0], [300.0, 300.0]])
    radii = np.array([20.0, 4.0])
    x = np.array([115.0, 125.0, 303.0, 306.0])
    y = np.array([100.0, 100.0, 300.0, 300.0])
    assert metrics.bright_footprint(x, y, stars, radii).tolist() == [True, False, True, False]


def test_the_footprint_fraction_is_of_the_sources_above_snr_20():
    inside = np.array([True, True, False, False])
    out = metrics.footprint_counts(inside, np.array([300.0, 10.0, 300.0, 10.0]), np.ones(4))
    assert out == {"n_in_bright_footprint": 2, "bright_footprint_fraction": 0.5}
    assert "bright_footprint_fraction" not in metrics.footprint_counts(inside, np.ones(4), np.ones(4))


def test_near_bright_counts_sources_within_6_px_of_a_pixel_above_1000():
    reference = np.zeros((100, 100))
    reference[50, 50] = 2000.0
    reference[10, 10] = 999.0
    x, y = np.array([55.0, 57.0, 10.0]), np.array([50.0, 50.0, 10.0])
    assert metrics.near_bright_count(x, y, reference) == 1


def test_the_ring_count_takes_bright_sources_6_to_20_px_from_a_top_star():
    found = np.array([[100.0, 100.0, 5000.0], [400.0, 400.0, 1.0]])
    x = np.array([110.0, 103.0, 125.0, 110.0])
    y = np.array([100.0, 100.0, 100.0, 100.0])
    flux = np.array([300.0, 300.0, 300.0, 10.0])
    assert metrics.ring_count(x, y, flux, np.ones(4), found) == 1
    assert metrics.ring_count(x, y, flux, np.ones(4), np.empty((0, 3))) is None


def test_the_star_sigma_is_the_stars_own_width():
    image = gaussians(STARS, 1.3)
    assert metrics.star_sigma(image, np.array(STARS)) == pytest.approx(
        metrics.star_sigma(gaussians([(200.3, 200.6)], 1.3), np.array([(200.3, 200.6)])), abs=1e-6)
    assert metrics.star_sigma(image, np.array(STARS)) > metrics.star_sigma(
        gaussians(STARS, 1.0), np.array(STARS))
    assert metrics.star_sigma(image, np.empty((0, 2))) is None
