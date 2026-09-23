"""The subpixel offset applied to the gain-matched reference before ZOGY.

`dev`: ``apply_subpixel_orthogonal_offsets``
(``modules/utils/rapid_pipeline_subs.py``). The image is upsampled by six,
shifted by the nearest whole upsampled pixel, and summed back down; CRPIX
is corrected by the offset. Applied only when 0.1 < |dx| < 5.0 or
0.1 < |dy| < 5.0 pixels. The pixel loops are `dev`'s, kept as written for
an exact port (they are slow on a full 4089x4089 image; vectorising them
is a later improvement).
"""

from __future__ import annotations

import logging

import numpy as np
from astropy.io import fits

logger = logging.getLogger(__name__)


def apply_subpixel_orthogonal_offsets(fits_file, dx, dy, output_fits_file=None) -> None:
    """`dev` ``apply_subpixel_orthogonal_offsets``; positive offsets shift left and down."""
    logger.info("Sub apply_subpixel_orthogonal_offsets: dx = %s, dy = %s fits_file = %s",
                dx, dy, fits_file)

    if (abs(dx) > 0.1 and abs(dx) < 5.0) or (abs(dy) > 0.1 and abs(dy) < 5.0):

        logger.info("Applying subpixel offsets dx = %s, dy = %s to FITS file = %s", dx, dy, fits_file)

        hdul = fits.open(fits_file)
        hdr = hdul[0].header
        data_array = np.array(hdul[0].data)

        crpix1 = hdr["CRPIX1"]
        crpix2 = hdr["CRPIX2"]
        hdr["CRPIX1"] = crpix1 - dx
        hdr["CRPIX2"] = crpix2 - dy

        naxis1 = hdr["NAXIS1"]
        naxis2 = hdr["NAXIS2"]

        scale_factor = 6

        x_offset = int(round(dx * scale_factor))
        y_offset = int(round(dy * scale_factor))
        logger.info("Upsampled image x and y offsets = %s %s", x_offset, y_offset)

        sfsq = scale_factor ** 2
        new_naxis1 = scale_factor * naxis1
        new_naxis2 = scale_factor * naxis2

        up_data = np.empty(shape=(new_naxis2, new_naxis1))
        for i in range(naxis2):
            for j in range(naxis1):
                ii_s = i * scale_factor
                jj_s = j * scale_factor
                ii_e = ii_s + scale_factor
                jj_e = jj_s + scale_factor
                up_data[ii_s:ii_e, jj_s:jj_e] = data_array[i][j] / sfsq

        dn_data = np.zeros(shape=(naxis2, naxis1))
        for i in range(naxis2):
            for j in range(naxis1):
                ii_s = max(0, i * scale_factor + y_offset)
                jj_s = max(0, j * scale_factor + x_offset)
                ii_e = min(new_naxis2, ii_s + scale_factor)
                jj_e = min(new_naxis1, jj_s + scale_factor)
                dn_data[i][j] = np.sum(up_data[ii_s:ii_e, jj_s:jj_e])

        hdul[0] = fits.PrimaryHDU(header=hdr, data=np.array(dn_data))

        if output_fits_file is None:
            output_fits_file = fits_file
        hdul.writeto(output_fits_file, overwrite=True, checksum=True)
        hdul.close()
