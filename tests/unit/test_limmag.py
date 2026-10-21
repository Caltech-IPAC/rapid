"""The ported limiting magnitude: ZPTMAG - 2.5 log10(n_sigma * sqrt(N_p) * sigma / EXPTIME).

Contract: on Gaussian noise of known sigma in DN, with a one-pixel PSF
(one noise pixel), the value is the formula's to within the sampling
error of the sigma; data already in DN/s are not divided by EXPTIME.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from astropy.io import fits

from rapidpipe.science.limmag import limiting_magnitude, psf_noise_pixels


def write(tmp_path, bunit):
    data = np.random.default_rng(1).normal(500.0, 20.0, (400, 400)).astype(np.float32)
    header = fits.Header({"ZPTMAG": 26.0, "EXPTIME": 50.0, "BUNIT": bunit})
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data, header)]).writeto(tmp_path / "img.fits")
    psf = np.zeros((5, 5))
    psf[2, 2] = 3.0
    fits.PrimaryHDU(psf).writeto(tmp_path / "psf.fits")
    return tmp_path / "img.fits", tmp_path / "psf.fits"


@pytest.mark.parametrize("bunit, scale", [("DN", 1 / 50.0), ("DN/s", 1.0)])
def test_limiting_magnitude_follows_the_formula(tmp_path, bunit, scale):
    image, psf = write(tmp_path, bunit)
    expected = 26.0 - 2.5 * math.log10(5.0 * 1.0 * 20.0 * scale)
    assert limiting_magnitude(image, psf) == pytest.approx(expected, abs=0.01)


def test_noise_pixels_of_a_flat_psf_is_its_area():
    assert psf_noise_pixels(np.ones((3, 4))) == pytest.approx(12.0)
