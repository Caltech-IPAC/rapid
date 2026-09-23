"""NaN replacement and restoration, and extreme-artifact repair.

`dev`: ``replace_nans_with_value`` and ``restore_nans``
(``modules/utils/rapid_pipeline_subs.py``), and
``repair_extreme_artifact_pixels`` (``pipeline/artifactRepairSubs.py``).
ZOGY's inputs have their NaNs replaced (images by 0.0, uncertainty images
by the clipped standard deviation); the positions are restored as NaN in
the masked outputs afterwards.
"""

from __future__ import annotations

import logging

import numpy as np
from astropy.io import fits

logger = logging.getLogger(__name__)


def replace_nans_with_value(fits_file, value):
    """`dev` ``replace_nans_with_value``: in place; returns (rows, cols) or None."""
    logger.info("Replacing NaNs, if any, with value = %s in image of FITS file = %s", value, fits_file)

    hdul = fits.open(fits_file)
    hdr = hdul[0].header
    data_array = np.array(hdul[0].data)

    nan_mask = np.isnan(data_array)
    logger.info("nan_count = %s", nan_mask.sum())

    if np.any(nan_mask):
        row_indices, col_indices = np.where(nan_mask)
        new_image_array = np.where(nan_mask, value, data_array)
        hdul[0] = fits.PrimaryHDU(header=hdr, data=new_image_array.astype(np.float32))
        hdul.writeto(fits_file, overwrite=True, checksum=True)
        hdul.close()
        return row_indices, col_indices

    hdul.close()
    return None


def restore_nans(fits_file, nan_indices) -> None:
    """`dev` ``restore_nans``: set the given pixels back to NaN, in place."""
    if nan_indices:
        logger.info("Restoring NaNs in image of FITS file = %s", fits_file)

        row_indices, col_indices = nan_indices

        hdul = fits.open(fits_file)
        hdr = hdul[0].header
        data = hdul[0].data

        for idx in range(len(row_indices)):
            data[row_indices[idx]][col_indices[idx]] = np.nan

        new_image_array = np.array(data)
        hdul[0] = fits.PrimaryHDU(header=hdr, data=new_image_array.astype(np.float32))
        hdul.writeto(fits_file, overwrite=True, checksum=True)
        hdul.close()


def repair_extreme_artifact_pixels(fits_file,
                                   threshold,
                                   box_half_width=3,
                                   max_box_half_width=8,
                                   min_clean_neighbours=5,
                                   hdu_index=0):
    """`dev` ``repair_extreme_artifact_pixels``: in place; returns the count repaired.

    Pixels with ``|value| >= threshold`` (cosmic rays, hot and dead pixels)
    are replaced by the median of their clean neighbours, growing the box
    until enough clean neighbours are found, else by the median of the
    whole usable image.
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
            value = float(np.median(data[usable])) if usable.any() else 0.0

        repaired[row, col] = value

    header["NARTRPR"] = (n_extreme, "Number of extreme artifact pixels repaired")
    header["ARTRPRTH"] = (float(threshold), "Absolute threshold for artifact repair")

    fits.PrimaryHDU(repaired.astype(np.float32), header=header).writeto(fits_file, overwrite=True)

    return n_extreme
