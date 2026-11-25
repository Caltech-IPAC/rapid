"""rapidpipe.science.psfmodel and the fits that take an oversampled stamp.

Contracts: an oversampled ePSF recovers a source's flux at its own sub-pixel
phase in forced photometry and in the xsources fit, whatever the stamp's own
normalisation; "oversampled" is refused with SFFT's cross-convolution on; a
difference job under it whose stamp is missing is rejected, exit 65.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits
from scipy.special import erf

from rapidpipe.exitcodes import CommandExit
from rapidpipe.science import psfmodel
from rapidpipe.science.alerts import forced
from rapidpipe.science.difference import xsources

SIGMA, FLUX, X0, Y0 = 0.7, 5000.0, 20.37, 19.71


def box(edges, centre):
    """The Gaussian's fraction between consecutive ``edges`` along one axis."""
    return np.diff(0.5 * (1 + erf((edges - centre) / (SIGMA * np.sqrt(2)))))


def star_and_epsf(tmp_path):
    """An undersampled star of total FLUX at (X0, Y0) and its ePSF (the pixel-integrated
    Gaussian sampled every third of a pixel), stored at unit sum, not 9."""
    edges = np.arange(41) - 0.5
    image = FLUX * np.outer(box(edges, Y0), box(edges, X0))
    centres = (np.arange(45) - 22) / 3.0
    one = np.array([box(np.array([c - 0.5, c + 0.5]), 0.0)[0] for c in centres])
    epsf = np.outer(one, one)
    path = tmp_path / "os3.fits"
    fits.PrimaryHDU(epsf / epsf.sum(), fits.Header({"OVERSAMP": 3})).writeto(path)
    return image, epsf, path


def test_an_oversampled_stamp_recovers_the_flux_in_forced_photometry_and_the_xsources_fit(tmp_path):
    image, epsf, path = star_and_epsf(tmp_path)
    error = np.ones_like(image)
    flux, _, _ = forced.psfphot(image, error, np.zeros(image.shape, bool), forced.load_psf(path),
                                np.array([X0 + 1]), np.array([Y0 + 1]), fit_shape=(7, 7))  # 1-based
    assert flux[0] == pytest.approx(FLUX, rel=2e-3)
    fitted = xsources.photutils_columns(image, error, epsf, [X0], [Y0], fwhm=1.6, fit_shape=(7, 7),
                                        aperture_radius=3.0, oversampling=3)
    assert fitted["flux_fit"][0] == pytest.approx(FLUX, rel=2e-3)


def test_oversampled_is_refused_with_cross_convolution_and_a_missing_stamp_rejects_65(monkeypatch):
    settings = {"psf": {"fit_model": "oversampled"}, "difference": {"sfft": {"crossconv_flag": True}}}
    with pytest.raises(ValueError, match="crossconv_flag"):
        psfmodel.check(settings)
    assert psfmodel.fit_model({}) == "native"
    assert psfmodel.companion_name("s3://b/p/sciimage_psf_phavg_fid2_sca01.fits") == \
        "sciimage_psf_phavg_fid2_sca01_oversampled.fits"
    from rapidpipe.stages import difference

    def absent(url, name):
        raise FileNotFoundError(name)
    monkeypatch.setattr(difference, "_beside", absent)
    job = type("Job", (), {"job": 7, "settings": {"psf": {"fit_model": "oversampled"}}})()
    with pytest.raises(CommandExit, match="oversampled") as stopped:
        difference._fit_psf(job, "s3://b/p/psf.fits")
    assert stopped.value.code == 65
