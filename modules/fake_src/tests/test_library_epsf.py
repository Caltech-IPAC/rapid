"""Tests for the empirical ePSF library model used in L2 injection.

Run with:  python -m pytest modules/fake_src/tests/test_library_epsf.py
"""

import os
import sys

import galsim
import numpy as np
from scipy.ndimage import map_coordinates

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir))

from modules.fake_src.library_epsf import LibraryEPSF, resolve_epsf_library_path


def test_template_tokens_are_substituted():
    assert (resolve_epsf_library_path("s3://b/lib/epsf_os3_FILTER_scaSCAID.fits", "F146", 4)
            == "s3://b/lib/epsf_os3_f146_sca04.fits")


def test_drawn_injection_reproduces_the_epsf():
    # Elliptical oversampled ePSF, drawn as romanisim's add_objects_to_image draws a
    # source (default method, so drawImage adds the pixel), through a local WCS with
    # a non-nominal scale, rotation, parity flip and skew.  Each pixel must match the
    # ePSF sampled directly at that pixel centre.
    oversample = 3
    yy, xx = np.mgrid[-37:38, -37:38] / oversample
    epsf = np.exp(-0.5 * ((xx / 0.75) ** 2 + (yy / 0.62) ** 2))
    epsf /= epsf.sum()

    th = np.radians(37.0)
    jac = 0.108 * np.array([[-np.cos(th), np.sin(th)], [np.sin(th), np.cos(th)]]) @ [[1.0, 0.01], [0.0, 1.0]]
    wcs = galsim.JacobianWCS(*jac.ravel())

    x0, y0 = 0.3, -0.2
    image = galsim.ImageD(11, 11, wcs=wcs, xmin=-5, ymin=-5)
    psf = LibraryEPSF(epsf, oversample, wcs).at_position(x0, y0)
    galsim.Convolve(galsim.DeltaFunction(flux=1.0), psf).drawImage(
        image=image, center=galsim.PositionD(x0, y0), add_to_image=True)

    py, px = np.mgrid[-5:6, -5:6]
    c = (epsf.shape[0] - 1) / 2
    expected = oversample ** 2 * map_coordinates(
        epsf, [c + oversample * (py - y0), c + oversample * (px - x0)], order=3)

    assert np.abs(image.array - expected).max() < 0.01 * expected.max()
