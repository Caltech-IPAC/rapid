"""
Background-noise sigma values for the ZOGY SN and SR command-line arguments.

Kept free of module-level side effects so that the logic can be imported and tested directly
(see pipeline/test/test_zogy_background_sigmas.py).
"""

import numpy as np
from astropy.io import fits


def background_sigma_from_uncertainty_map(filename,
                                          percentile=10.0,
                                          border=500,
                                          hdu_index=0):

    """
    Estimate the background noise sigma from a per-pixel uncertainty map.

    ZOGY's SN and SR arguments are meant to be the background noise sigma of the science and
    reference images.  Taking them from the clipped standard deviation of the images
    themselves is only valid where the field is background-dominated: in a crowded field the
    scatter is dominated by sources, not by noise.  Measured on the socsims galactic-bulge
    frames the image-scatter ratio is about 0.8 where the ratio implied by the uncertainty
    maps is about 6.5, and using the latter is worth roughly 28 to 38 per cent in
    matched-filter depth.

    A low percentile of the uncertainty map is used rather than its mean or median so that
    pixels whose uncertainty is inflated by source photons are excluded.  Note that only the
    ratio SN/SR affects the difference image, so any error common to both cancels.

    The border is excluded because coadd edges and reference-coverage boundaries carry
    atypical uncertainties.  It is capped at an eighth of the array so that the calculation
    remains well defined for small images.
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

    """
    Return the (SN, SR) pair for the py_zogy command line.

    When use_uncertainty_maps is False the historical values are returned unchanged: the
    clipped standard deviation of the science image, and that of the un-gain-matched reference
    image scaled onto the gain-matched flux scale by scalefacref.
    """

    if not use_uncertainty_maps:

        return float(std_sci_img), float(std_ref_img * scalefacref)

    sn = background_sigma_from_uncertainty_map(sci_uncert_filename, percentile, border)
    sr = background_sigma_from_uncertainty_map(ref_uncert_filename, percentile, border)

    return sn, sr
