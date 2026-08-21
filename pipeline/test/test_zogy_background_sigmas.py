"""Tests for the ZOGY SN/SR background-sigma selection.

The flag-off path must reproduce the historical values exactly, so that enabling the new
[ZOGY] parameter is the only thing that can change a difference image.

Run with:  python -m pytest pipeline/test/test_zogy_background_sigmas.py
"""

import os
import sys

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.zogyNoiseSubs import (background_sigma_from_uncertainty_map,
                                    zogy_background_sigmas)


def test_flag_off_reproduces_the_historical_values():

    """With the flag off the returned pair must be exactly the previous expression."""

    std_sci, std_ref, scalefacref = 4.4321, 0.31287, 17968.4

    sn, sr = zogy_background_sigmas(False, 'unused.fits', 'unused.fits',
                                    std_sci, std_ref, scalefacref)

    assert sn == float(std_sci)
    assert sr == float(std_ref * scalefacref)


def test_flag_off_does_not_touch_the_uncertainty_maps():

    """The historical path must not require the uncertainty maps to exist."""

    sn, sr = zogy_background_sigmas(False, '/nonexistent/sci.fits', '/nonexistent/ref.fits',
                                    1.0, 2.0, 3.0)
    assert (sn, sr) == (1.0, 6.0)


def _write_map(path, value, shape=(256, 256), border_value=None):

    data = np.full(shape, value, dtype=np.float32)
    if border_value is not None:
        data[:20, :] = border_value
        data[-20:, :] = border_value
        data[:, :20] = border_value
        data[:, -20:] = border_value
    fits.PrimaryHDU(data).writeto(path, overwrite=True)


def test_uniform_map_returns_that_value(tmp_path):

    f = str(tmp_path / 'unc.fits')
    _write_map(f, 0.25)
    assert background_sigma_from_uncertainty_map(f) == pytest.approx(0.25)


def test_low_percentile_rejects_source_inflated_pixels(tmp_path):

    """Pixels with source-inflated uncertainty must not raise the estimate."""

    f = str(tmp_path / 'unc.fits')
    data = np.full((256, 256), 0.4, dtype=np.float32)
    rng = np.random.default_rng(0)
    ys = rng.integers(40, 216, 4000)
    xs = rng.integers(40, 216, 4000)
    data[ys, xs] = 40.0                      # ~6% of pixels, 100x the background
    fits.PrimaryHDU(data).writeto(f, overwrite=True)

    assert background_sigma_from_uncertainty_map(f) == pytest.approx(0.4)
    # the mean would be badly biased by the same pixels
    assert float(np.mean(data)) > 2.0


def test_border_is_excluded(tmp_path):

    """Atypical edge uncertainties must not drive the estimate."""

    f = str(tmp_path / 'unc.fits')
    _write_map(f, 0.5, border_value=1e-4)
    assert background_sigma_from_uncertainty_map(f, border=32) == pytest.approx(0.5)


def test_border_capped_for_small_images(tmp_path):

    """A border wider than the image must not empty the selection."""

    f = str(tmp_path / 'unc.fits')
    _write_map(f, 0.7, shape=(64, 64))
    assert background_sigma_from_uncertainty_map(f, border=500) == pytest.approx(0.7)


def test_nonfinite_and_nonpositive_are_ignored(tmp_path):

    f = str(tmp_path / 'unc.fits')
    data = np.full((256, 256), 0.3, dtype=np.float32)
    data[100:110, 100:110] = np.nan
    data[120:130, 120:130] = 0.0
    data[140:150, 140:150] = -1.0
    fits.PrimaryHDU(data).writeto(f, overwrite=True)
    assert background_sigma_from_uncertainty_map(f) == pytest.approx(0.3)


def test_all_bad_raises(tmp_path):

    f = str(tmp_path / 'unc.fits')
    fits.PrimaryHDU(np.zeros((64, 64), dtype=np.float32)).writeto(f, overwrite=True)
    with pytest.raises(ValueError):
        background_sigma_from_uncertainty_map(f)
