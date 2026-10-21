# ported-from: modules/utils/rapid_data_analysis.py @ d56078f0
"""The 5-sigma point-source limiting magnitude of an L2 image, as `dev` computes it.

Ported from `dev`'s ``modules/utils/rapid_data_analysis.py``
(``compute_limiting_magnitude_for_l2_image`` and the helpers it calls),
which ``database/sims/db_backfill_l2files_limmag.py`` runs per ``l2files``
row. The foundation computes the value before publication instead of
backfilling it (foundation plan, Part 1: a published row never changes).

Only the path the backfill takes is ported: the background sigma from
iterative sigma clipping of the image pixels (``noise_method = "image"``,
no mask), converted to DN/s with ``BUNIT`` and ``EXPTIME``, and the PSF's
noise pixels. `dev`'s sky-limited (Poisson) comparison only prints a
warning and does not change the value, so it is not ported.

    f_lim = n_sigma * sqrt(N_p) * sigma_bkg
    m_lim = ZPTMAG - 2.5 * log10(f_lim)
"""

from __future__ import annotations

import math

import numpy as np
import numpy.ma as ma
from astropy.io import fits
from astropy.stats import sigma_clip

#: `dev`'s BUNIT spellings for data already in counts per second.
DN_PER_SEC_BUNITS = ("DN/S", "DN / S", "DN S-1", "DN/SEC", "DN / SEC", "ELECTRONS/S", "E-/S")


def image_hdu_index(hdul) -> int:
    """The index of the first HDU holding a two-dimensional image (`dev`: ``get_image_hdu_index``)."""
    for index, hdu in enumerate(hdul):
        if hdu.header.get("NAXIS") == 2 or hdu.header.get("ZNAXIS") == 2:
            return index
    raise ValueError("no HDU with two-dimensional image data")


def dn_per_sec_factor(bunit, exptime) -> float:
    """1 / EXPTIME for data in DN, 1.0 for data already in DN/s (`dev`: ``get_dn_per_sec_factor``)."""
    if bunit is not None and str(bunit).strip().upper() in DN_PER_SEC_BUNITS:
        return 1.0
    if exptime is None or exptime <= 0.0:
        raise ValueError(f"data in DN with unusable EXPTIME = {exptime}")
    return 1.0 / float(exptime)


def psf_noise_pixels(psf) -> float:
    """``(sum p)^2 / sum p^2`` over the PSF model (`dev`: ``compute_psf_noise_pixels``)."""
    p = np.array(psf, dtype=np.float64)
    p = np.where(np.isfinite(p), p, 0.0)
    total, total_sq = np.sum(p), np.sum(p * p)
    if total <= 0.0 or total_sq <= 0.0:
        raise ValueError(f"PSF model has non-positive sum ({total}, {total_sq})")
    return float(total * total / total_sq)


def clip_sigma_correction(n_clip_sigma: float) -> float:
    """The factor that reinflates an iteratively clipped Gaussian sigma (`dev`: ``compute_iterative_clip_sigma_corr``)."""
    c = 1.0
    for _ in range(100):
        t = n_clip_sigma * c
        phi = math.exp(-0.5 * t * t) / math.sqrt(2.0 * math.pi)
        cdf = 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))
        c_new = math.sqrt(1.0 - 2.0 * t * phi / (2.0 * cdf - 1.0))
        if abs(c_new - c) < 1.0e-12:
            c = c_new
            break
        c = c_new
    return 1.0 / c


def background_sigma(data, n_clip_sigma: float = 3.0, maxiters: int = 10) -> float:
    """The clipped, reinflated standard deviation of the finite pixels (`dev`: ``estimate_background_sigma``)."""
    a = np.array(data, dtype=np.float64)
    clipped = sigma_clip(ma.masked_array(a, ~np.isfinite(a)), sigma=n_clip_sigma,
                         maxiters=maxiters, masked=True)
    sigma = float(ma.getdata(clipped.std())) * clip_sigma_correction(n_clip_sigma)
    if not np.isfinite(sigma) or sigma <= 0.0:
        raise ValueError(f"invalid background sigma = {sigma}")
    return sigma


def limiting_magnitude(image_path, psf_path, n_sigma_limit: float = 5.0,
                       n_clip_sigma: float = 3.0, maxiters: int = 10) -> float:
    """The ``n_sigma_limit`` point-source limiting magnitude of the image at ``image_path`` [AB mag].

    ``ZPTMAG`` (for flux in DN/s), ``BUNIT`` and ``EXPTIME`` are read from
    the image HDU's header, then the primary header, as `dev` does.
    """
    with fits.open(image_path) as hdul:
        index = image_hdu_index(hdul)
        header, primary = hdul[index].header, hdul[0].header
        data = np.array(hdul[index].data)

    def keyword(name):
        return header[name] if name in header else primary.get(name)

    zptmag = keyword("ZPTMAG")
    if zptmag is None:
        raise KeyError(f"ZPTMAG not found in {image_path}")
    with fits.open(psf_path) as hdul:
        noise_pixels = psf_noise_pixels(hdul[0].data)
    sigma = background_sigma(data, n_clip_sigma, maxiters) * dn_per_sec_factor(
        keyword("BUNIT"), keyword("EXPTIME"))
    flux_limit = n_sigma_limit * math.sqrt(noise_pixels) * sigma
    return float(zptmag) - 2.5 * math.log10(flux_limit)
