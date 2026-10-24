"""The reference PSF measured from an image's stars (``rapidpipe.science.difference.refpsf``).

Contracts: on a field of pixel-integrated Gaussian stars mixed with
single-pixel defects and wider galaxies, the stamp is ``SIZE x SIZE``, unit
sum, centred within the tolerance, with the stars' second-moment sigma to
0.02 px, and the stars' own median width the same, built from the stars alone (the defects are narrower than the
science PSF, the galaxies wider than the width cut); beyond the outer
wing radius the stamp is the science PSF's, renormalised, so ZOGY sees no
edge where the measured ePSF ends; pixels outside ``usable`` are never
read; a science PSF of another size is refused; fewer than ``MIN_STARS``
stars raise ``TooFewStars`` with the named reason; the peak floor is in units
of the noise, so a field of a hundredth of the count rate and noise keeps
the same stars.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.special import erf

from rapidpipe.science.difference import refpsf

SIGMA = 1.1
CENTRE = (refpsf.SIZE - 1) / 2


def _blob(n: int, x0: float, y0: float, sigma: float) -> np.ndarray:
    """A unit-flux Gaussian integrated over each pixel of an ``n x n`` image."""
    edges = np.arange(n + 1) - 0.5
    s2 = np.sqrt(2) * sigma
    px = np.diff(0.5 * erf((edges - x0) / s2))
    py = np.diff(0.5 * erf((edges - y0) / s2))
    return np.outer(py, px)


def science_psf() -> np.ndarray:
    """Narrower than the stars, with a faint halo reaching past the ePSF's 15 px."""
    return (0.97 * _blob(refpsf.SIZE, CENTRE, CENTRE, 0.95)
            + 0.03 * _blob(refpsf.SIZE, CENTRE, CENTRE, 12.0))


def field(stars: int, *, n: int = 640, defects: int = 6, galaxies: int = 6,
          seed: int = 3) -> tuple[np.ndarray, int]:
    """Stars on a grid with random sub-pixel phases, defects and galaxies between them."""
    rng = np.random.default_rng(seed)
    image = rng.normal(0.0, 0.02, (n, n))
    grid = [(x, y) for y in range(40, n - 40, 40) for x in range(40, n - 40, 40)]
    rng.shuffle(grid)
    for x, y in grid[:stars]:
        image += rng.uniform(300, 3000) * _blob(n, x + rng.uniform(-.5, .5), y + rng.uniform(-.5, .5),
                                                SIGMA)
    for x, y in grid[stars:stars + galaxies]:
        image += 3000 * _blob(n, x + 0.3, y - 0.2, 2.2)
    for x, y in grid[stars + galaxies:stars + galaxies + defects]:
        image[y, x] += rng.uniform(100, 1000)
    return image, n


def test_the_stamp_has_the_stars_width_and_rejects_defects_and_galaxies():
    image, n = field(40)
    measured = refpsf.measure(image, np.ones((n, n), dtype=bool), science_psf=science_psf())
    truth = refpsf.moments(_blob(refpsf.SIZE, (refpsf.SIZE - 1) / 2, (refpsf.SIZE - 1) / 2, SIGMA))
    assert measured.stamp.shape == (refpsf.SIZE, refpsf.SIZE)
    assert measured.stamp.sum() == pytest.approx(1.0, abs=1e-9)
    assert measured.sigma_px == pytest.approx(truth[2], abs=0.02)
    assert measured.star_sigma_px == pytest.approx(truth[2], abs=0.02)
    assert max(map(abs, measured.centroid_px)) < refpsf.CENTRING_TOLERANCE_PX
    assert measured.stars == 40 and measured.detections >= 40 + 6


def test_beyond_the_wing_radius_the_stamp_is_the_science_psf():
    image, n = field(40)
    measured = refpsf.measure(image, np.ones((n, n), dtype=bool), science_psf=science_psf())
    rows, cols = np.indices(measured.stamp.shape)
    r = np.hypot(cols - CENTRE, rows - CENTRE)
    outer = r >= refpsf.WING_RADII_PX[1]
    ratio = measured.stamp[outer] / science_psf()[outer]
    assert np.allclose(ratio, ratio[0], rtol=1e-9)        # one normalisation, no edge
    assert measured.stamp[r > 16].sum() > 0.01            # the halo the ePSF cannot hold


def test_the_stamp_takes_the_science_psfs_side_and_an_even_or_small_one_is_refused():
    image, n = field(40)
    c = 60.0
    wide = 0.97 * _blob(121, c, c, 0.95) + 0.03 * _blob(121, c, c, 12.0)
    measured = refpsf.measure(image, np.ones((n, n), dtype=bool), science_psf=wide)
    assert measured.stamp.shape == (121, 121)
    assert measured.stamp.sum() == pytest.approx(1.0, abs=1e-9)
    for shape in ((9, 9), (62, 62), (61, 59)):
        with pytest.raises(ValueError, match="not square, odd and at least"):
            refpsf.measure(image, np.ones((n, n), dtype=bool), science_psf=np.ones(shape))


def test_pixels_outside_usable_are_never_read():
    image, n = field(40)
    usable = np.ones((n, n), dtype=bool)
    usable[:, n // 2:] = False
    image[:, n // 2:] = np.nan
    measured = refpsf.measure(image, usable, science_psf=science_psf())
    assert 10 <= measured.stars < 40


def test_too_few_stars_raise_the_named_reason():
    image, n = field(7)
    with pytest.raises(refpsf.TooFewStars, match=r"^refpsf: 7 stars, 10 needed$"):
        refpsf.measure(image, np.ones((n, n), dtype=bool), science_psf=science_psf())


def test_the_peak_floor_follows_the_noise_not_the_count_rate():
    # A filter with a hundredth of the count rate and of the noise has the same stars.
    image, n = field(30)
    usable = np.ones((n, n), dtype=bool)
    bright = refpsf.measure(image, usable, science_psf=science_psf())
    faint = refpsf.measure(image / 100, usable, science_psf=science_psf())
    assert faint.stars == bright.stars == 30
    assert faint.sigma_px == pytest.approx(bright.sigma_px, abs=1e-3)


class _Overshooting:
    """An ePSF whose evaluated centroid moves 1.8 times its shift, 0.15 px off at no shift (job 20184's)."""

    def evaluate(self, x, y, flux, x0, y0):
        c = (x.shape[0] - 1) / 2
        cx, cy = c + 1.8 * (x0 - c) - 0.15, c + 1.8 * (y0 - c) - 0.12
        return flux * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * 1.1 ** 2))


def test_the_centring_converges_where_the_centroid_overshoots_the_shift():
    stamp = refpsf._evaluate_centred(_Overshooting(), 61)
    assert max(map(abs, refpsf.moments(stamp)[:2])) < refpsf.CENTRING_TOLERANCE_PX


def test_the_search_centres_what_the_stepping_does_not():
    # One shift is too few for the stepping (job 21029's ePSF stalled it over ten); the search finishes.
    stamp = refpsf._evaluate_centred(_Overshooting(), 61, iterations=1)
    assert max(map(abs, refpsf.moments(stamp)[:2])) < refpsf.CENTRING_TOLERANCE_PX


class _Uncentrable:
    """An ePSF whose centroid never comes within the tolerance of the centre."""

    def evaluate(self, x, y, flux, x0, y0):
        c = (x.shape[0] - 1) / 2
        return flux * np.exp(-((x - c - 2.0) ** 2 + (y - c) ** 2) / (2 * 1.1 ** 2))


def test_an_uncentrable_epsf_still_raises():
    with pytest.raises(ValueError, match="at best over the search"):
        refpsf._evaluate_centred(_Uncentrable(), 61)
