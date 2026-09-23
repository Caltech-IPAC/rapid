"""Clipped image statistics, as `dev` computes them.

`dev`: ``compute_clip_corr`` and ``fits_data_statistics_with_clipping``
(``modules/utils/rapid_pipeline_subs.py``). The clipping-correction factor
is a Monte Carlo estimate drawn from ``numpy.random`` with no seed, so in
`dev` every call gives a slightly different factor. The port keeps that as
the default (``seed=None``) and designs in a seed, off by default: the
stage's ``[statistics] clip_correction_seed`` setting, where a
non-negative value seeds a legacy ``RandomState`` with the same draw
algorithm as ``numpy.random``'s global state.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.ma as ma
from astropy.io import fits


def _random_source(seed: int | None) -> Any:
    if seed is None or seed < 0:
        return np.random
    return np.random.RandomState(seed)


def compute_clip_corr(n_sigma: float, seed: int | None = None) -> float:
    """`dev` ``compute_clip_corr``: variance re-inflation for n-sigma clipping."""
    random = _random_source(seed)
    var_trials = []
    for _ in range(0, 10):
        a = random.normal(0.0, 1.0, 1000000)
        med = np.median(a, axis=0)
        p16 = np.percentile(a, 16, axis=0)
        p84 = np.percentile(a, 84, axis=0)
        sigma = 0.5 * (p84 - p16)
        mdmsg = med - n_sigma * sigma
        b = np.less(a, mdmsg)
        mdpsg = med + n_sigma * sigma
        c = np.greater(a, mdpsg)
        mask = np.any([b, c], axis=0)
        mx = ma.masked_array(a, mask)
        var = ma.getdata(mx.var(axis=0))
        var_trials.append(var)

    np_var_trials = np.array(var_trials)
    avg_var_trials = np.mean(np_var_trials)
    corr_fact = 1.0 / avg_var_trials

    return corr_fact


def fits_data_statistics_with_clipping(
    input_filename,
    n_sigma: float = 3.0,
    hdu_index: int = 0,
    satlev: float = 50000.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """`dev` ``fits_data_statistics_with_clipping``.

    Statistics with n-sigma outlier rejection, ignoring NaNs, over the
    whole array of ``hdu_index``. Returns `dev`'s dictionary unchanged.
    """
    with fits.open(input_filename) as hdul:
        data_array = np.array(hdul[hdu_index].data)

    cf = compute_clip_corr(n_sigma, seed)
    sqrtcf = np.sqrt(cf)

    a = data_array

    image_size = np.shape(a)
    pixcount = image_size[0] * image_size[1]

    datamin = np.nanmin(a)
    datamax = np.nanmax(a)
    nancount = np.isnan(a).sum()
    satcount = np.greater_equal(a, satlev).sum()
    med = np.nanmedian(a)
    p16 = np.nanpercentile(a, 16)
    p84 = np.nanpercentile(a, 84)
    sigma = 0.5 * (p84 - p16)
    mdmsg = med - n_sigma * sigma
    b = np.less(a, mdmsg)
    mdpsg = med + n_sigma * sigma
    c = np.greater(a, mdpsg)
    d = np.where(np.isnan(a), True, False)
    mask = b | c | d
    mx = ma.masked_array(a, mask)
    avg = ma.getdata(mx.mean())
    std = ma.getdata(mx.std()) * sqrtcf
    nkept = ma.getdata(mx.count())
    noutliers = pixcount - nancount - nkept

    return {
        "clippedavg": avg,
        "clippedstd": std,
        "nkept": nkept,
        "noutliers": noutliers,
        "gmed": med,
        "gsigma": sigma,
        "gdatamin": datamin,
        "gdatamax": datamax,
        "satcount": satcount,
        "nancount": nancount,
        "pixcount": pixcount,
    }
