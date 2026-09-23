"""ZOGY: its background-noise arguments and its command line.

`dev`: ``pipeline/zogyNoiseSubs.py`` (``background_sigma_from_uncertainty_map``,
``zogy_background_sigmas``) and the ZOGY block inline in
``awsBatchSubmitJobs_runSingleSciencePipeline.py``. ZOGY itself is
``modules/zogy/v21Aug2018/py_zogy.py``, run by subprocess as `dev` runs
it:

    python py_zogy.py <NewImage> <RefImage> <NewPSF> <RefPSF>
        <NewSigmaImage> <RefSigmaImage> <NewSigmaMode> <RefSigmaMode>
        <AstUncertX> <AstUncertY> <DiffImage> <DiffPSF> <ScorrImage>

The astrometric-uncertainty arguments are the stage's
``[zogy] astrometric_sigma`` setting, default 0.0: `dev` overrides the
measured RMS with 0.0 before calling ZOGY ("2025-08-15 Jacob's
recommendation"), and registers that 0.0 as ``dxrmsfin``/``dyrmsfin``
(ruling 1, lead 2026-09-22).
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits


def background_sigma_from_uncertainty_map(filename, percentile=10.0, border=500, hdu_index=0) -> float:
    """`dev` ``background_sigma_from_uncertainty_map``.

    A low percentile of the finite, positive interior of an uncertainty
    map, the border capped at an eighth of each axis.
    """
    data = fits.getdata(filename, ext=hdu_index).astype(np.float64)

    n0, n1 = data.shape
    b0 = min(int(border), n0 // 8)
    b1 = min(int(border), n1 // 8)

    interior = data[b0:n0 - b0, b1:n1 - b1] if (b0 > 0 and b1 > 0) else data

    finite = interior[np.isfinite(interior) & (interior > 0.0)]

    if finite.size == 0:
        raise ValueError("no finite positive uncertainties in {}".format(filename))

    return float(np.percentile(finite, percentile))


def zogy_background_sigmas(use_uncertainty_maps,
                           sci_uncert_filename,
                           ref_uncert_filename,
                           std_sci_img,
                           std_ref_img,
                           scalefacref,
                           percentile=10.0,
                           border=500):
    """`dev` ``zogy_background_sigmas``: the (SN, SR) pair for py_zogy.

    Off: the clipped standard deviation of the science image, and that of
    the un-gain-matched reference scaled by ``scalefacref``. On: a low
    percentile of each uncertainty map.
    """
    if not use_uncertainty_maps:
        return float(std_sci_img), float(std_ref_img * scalefacref)

    sn = background_sigma_from_uncertainty_map(sci_uncert_filename, percentile, border)
    sr = background_sigma_from_uncertainty_map(ref_uncert_filename, percentile, border)
    return sn, sr


def build_zogy_command(python_cmd: str,
                       zogy_code: str,
                       new_image: str,
                       ref_image: str,
                       new_psf: str,
                       ref_psf: str,
                       new_sigma_image: str,
                       ref_sigma_image: str,
                       zogy_sn: float,
                       zogy_sr: float,
                       dx: float,
                       dy: float,
                       diff_image: str,
                       diff_psf: str,
                       scorr_image: str) -> list[str]:
    """`dev`'s ZOGY argument list; numbers are passed through ``str()`` as in `dev`."""
    return [python_cmd,
            zogy_code,
            new_image,
            ref_image,
            new_psf,
            ref_psf,
            new_sigma_image,
            ref_sigma_image,
            str(zogy_sn),
            str(zogy_sr),
            str(dx),
            str(dy),
            diff_image,
            diff_psf,
            scorr_image]
