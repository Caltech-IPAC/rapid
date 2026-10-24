"""The reference PSF measured from an image's stars (``rapidpipe.science.difference.refpsf``).

Contracts: on a field of pixel-integrated Gaussian stars mixed with
single-pixel defects and wider galaxies, the stamp is ``SIZE x SIZE``, unit
sum, centred within the tolerance, with the stars' second-moment sigma to
0.02 px, built from the stars alone (the defects are narrower than the
science PSF, the galaxies wider than the width cut); pixels outside
``usable`` are never read; fewer than ``MIN_STARS`` stars raise
``TooFewStars`` with the named reason.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.special import erf

from rapidpipe.science.difference import refpsf

SIGMA = 1.1


def _blob(n: int, x0: float, y0: float, sigma: float) -> np.ndarray:
    """A unit-flux Gaussian integrated over each pixel of an ``n x n`` image."""
    edges = np.arange(n + 1) - 0.5
    s2 = np.sqrt(2) * sigma
    px = np.diff(0.5 * erf((edges - x0) / s2))
    py = np.diff(0.5 * erf((edges - y0) / s2))
    return np.outer(py, px)


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
    measured = refpsf.measure(image, np.ones((n, n), dtype=bool), science_sigma_px=0.95)
    truth = refpsf.moments(_blob(refpsf.SIZE, (refpsf.SIZE - 1) / 2, (refpsf.SIZE - 1) / 2, SIGMA))
    assert measured.stamp.shape == (refpsf.SIZE, refpsf.SIZE)
    assert measured.stamp.sum() == pytest.approx(1.0, abs=1e-9)
    assert measured.sigma_px == pytest.approx(truth[2], abs=0.02)
    assert max(map(abs, measured.centroid_px)) < refpsf.CENTRING_TOLERANCE_PX
    assert measured.stars == 40 and measured.detections >= 40 + 6


def test_pixels_outside_usable_are_never_read():
    image, n = field(40)
    usable = np.ones((n, n), dtype=bool)
    usable[:, n // 2:] = False
    image[:, n // 2:] = np.nan
    measured = refpsf.measure(image, usable, science_sigma_px=0.95)
    assert 10 <= measured.stars < 40


def test_too_few_stars_raise_the_named_reason():
    image, n = field(7)
    with pytest.raises(refpsf.TooFewStars, match=r"^refpsf: 7 stars, 10 needed$"):
        refpsf.measure(image, np.ones((n, n), dtype=bool), science_sigma_px=0.95)
