"""
Repair extreme artifact pixels in the science image before difference imaging.

Kept free of module-level side effects so that the logic can be imported and tested directly
(see pipeline/test/test_artifact_repair.py).
"""

import numpy as np
from astropy.io import fits


def find_extreme_artifact_pixels(fits_file, threshold, hdu_index=0):

    """
    Locate pixels whose absolute value exceeds threshold.

    Measured on five socsims frames, a symmetric absolute threshold above the stellar peak
    identifies cosmic rays, hot pixels and dead pixels with complete reliability: every pixel
    it selected carried a nonzero romancal data-quality flag, and none carried SATURATED, so no
    real star was ever selected.  Only three to twelve pixels per frame were selected.

    The threshold must therefore sit above the brightest real stellar pixel, which makes it a
    per-data-set quantity.  socsims stars peak near 5 to 6e3 DN/s, so 10000 is a safe choice
    there; the rimtimsims reach their 20000 DN/s rate saturation and would need more.

    A symmetric test on the absolute value is used because these artifacts occur with either
    sign.  On one frame the most extreme pixel was -2.6e6 DN/s, and a one-sided test would
    have missed it entirely.

    Returns a (row_indices, col_indices) tuple, matching the convention used by
    modules/utils/rapid_pipeline_subs.py restore_nans().
    """

    data = fits.getdata(fits_file, ext=hdu_index)

    extreme = np.isfinite(data) & (np.abs(data) >= float(threshold))

    return np.where(extreme)


def repair_extreme_artifact_pixels(fits_file,
                                   threshold,
                                   box_half_width=3,
                                   max_box_half_width=8,
                                   min_clean_neighbours=5,
                                   hdu_index=0):

    """
    Replace extreme artifact pixels with the median of their unaffected neighbours, in place.

    Repairing the input is strongly preferable to masking the output.  A single bad pixel does
    not stay a single bad pixel in the difference image: for ZOGY the impulse response of one
    science pixel is ifft(P_R_hat/den), which retains 2 to 5e-5 of its peak amplitude out to a
    radius of about 43 pixels, set by the PSF stamp support.  Suppressing a 3e5-sigma spike
    below the detection threshold by masking therefore needs a radius near 44 pixels, roughly
    6000 pixels per artifact, whereas repairing the input touches one pixel and leaves nothing
    to mask.  (For SFFT with ForceConv='REF' the difference is pointwise in the science image,
    so there the spike does stay a single pixel.)

    Other flagged pixels are excluded from each median so that adjacent artifacts, such as a
    two-pixel cosmic ray or a small dead-pixel cluster, do not repair each other.  The box is
    grown until enough clean neighbours are available.

    Returns the number of pixels repaired.
    """

    with fits.open(fits_file) as hdul:
        header = hdul[hdu_index].header
        data = np.array(hdul[hdu_index].data, dtype=np.float64)

    extreme = np.isfinite(data) & (np.abs(data) >= float(threshold))
    n_extreme = int(extreme.sum())

    if n_extreme == 0:
        return 0

    usable = np.isfinite(data) & ~extreme
    n0, n1 = data.shape
    rows, cols = np.where(extreme)

    repaired = data.copy()

    for row, col in zip(rows, cols):

        value = None

        half_width = int(box_half_width)

        while half_width <= int(max_box_half_width):

            r0 = max(row - half_width, 0)
            r1 = min(row + half_width + 1, n0)
            c0 = max(col - half_width, 0)
            c1 = min(col + half_width + 1, n1)

            neighbourhood = data[r0:r1, c0:c1]
            clean = usable[r0:r1, c0:c1]

            if int(clean.sum()) >= int(min_clean_neighbours):
                value = float(np.median(neighbourhood[clean]))
                break

            half_width *= 2

        if value is None:

            # Nothing clean nearby; fall back to the median of the whole usable image.

            value = float(np.median(data[usable])) if usable.any() else 0.0

        repaired[row, col] = value

    header["NARTRPR"] = (n_extreme, "Number of extreme artifact pixels repaired")
    header["ARTRPRTH"] = (float(threshold), "Absolute threshold for artifact repair")

    fits.PrimaryHDU(repaired.astype(np.float32), header=header).writeto(fits_file, overwrite=True)

    return n_extreme
