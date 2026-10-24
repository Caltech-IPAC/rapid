"""The Gaia offset on a synthetic frame whose stars sit a known translation from the WCS.

Contracts: the offset is the median of measured minus predicted star
position through the frame's WCS, x along columns; a Gaia star with
another within the isolation radius, on a NaN window or too faint is not
measured; a frame with no measurable star reports only the count, zero.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.wcs import WCS

from rapidpipe.science.astrometry import ISOLATION_PX, gaia_offset

N = 400
SHIFT = (0.3, -0.7)


def tan_wcs() -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [9.5, -45.5]
    w.wcs.crpix = [N / 2, N / 2]
    w.wcs.cd = [[-0.11 / 3600, 0.0], [0.0, 0.11 / 3600]]
    return w


def frame(positions, flux: float = 5000.0, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = 10.0 + rng.normal(0.0, 1.0, (N, N))
    yy, xx = np.mgrid[:N, :N]
    for x, y in positions:
        image += flux / (2 * np.pi) * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 2.0)
    return image


def grid(step: int = 50) -> np.ndarray:
    return np.array([(x + 0.37 * i % 1, y) for i, (x, y) in enumerate(
        (x, y) for x in range(40, N - 40, step) for y in range(40, N - 40, step))])


def test_the_offset_is_the_translation_of_the_stars_from_the_wcs():
    w, predicted = tan_wcs(), grid()
    ra, dec = w.all_pix2world(predicted[:, 0], predicted[:, 1], 0)
    measured = gaia_offset(frame(predicted + SHIFT), w, ra, dec)
    assert measured["n_gaia_stars"] == len(predicted)
    assert measured["gaia_dx_px"] == pytest.approx(SHIFT[0], abs=0.03)
    assert measured["gaia_dy_px"] == pytest.approx(SHIFT[1], abs=0.03)


def test_crowded_nan_and_faint_stars_are_not_measured():
    w, predicted = tan_wcs(), grid()
    pair = predicted[0] + (ISOLATION_PX / 2, 0)                  # crowds the first star
    stars = np.vstack([predicted, pair])
    ra, dec = w.all_pix2world(stars[:, 0], stars[:, 1], 0)
    image = frame(stars + SHIFT)
    x, y = (int(round(v)) for v in predicted[1])
    image[y, x + 2] = np.nan                                     # the second star's window
    assert gaia_offset(image, w, ra, dec)["n_gaia_stars"] == len(predicted) - 2
    faint = gaia_offset(frame(predicted + SHIFT, flux=20.0), w, *w.all_pix2world(
        predicted[:, 0], predicted[:, 1], 0))
    assert faint == {"n_gaia_stars": 0}


def test_a_copy_with_nothing_on_the_frame_reports_a_zero_count():
    assert gaia_offset(frame([]), tan_wcs(), np.array([120.0]), np.array([30.0])) == {"n_gaia_stars": 0}
