"""PSF normalisation and transposition.

`dev`: ``normalize_image`` and ``transpose_image_data``
(``modules/utils/rapid_pipeline_subs.py``). The science PSF is normalised
to unit sum before ZOGY (the reference PSF is already normalised); for
rimtimsim data, recognised by ``"rimtimsim"`` in the science file name,
the normalised science PSF is also transposed.
"""

from __future__ import annotations

import logging

import numpy as np
from astropy.io import fits

logger = logging.getLogger(__name__)


def normalize_image(fits_file, hdu_index: int, output_fits_file=None) -> None:
    """`dev` ``normalize_image``: divide by the sum; record NRMLZSUM."""
    logger.info("Normalizing image in input FITS file = %s", fits_file)

    with fits.open(fits_file) as hdul:
        hdr = hdul[hdu_index].header
        data_output = np.array(hdul[hdu_index].data)

    global_sum = np.sum(data_output)
    data_output /= global_sum

    final_sum = np.sum(data_output)
    logger.info("Method normalize_image: final_sum = %s", final_sum)

    hdr["NRMLZSUM"] = (final_sum, "Image sum after normalizing")

    if output_fits_file is None:
        output_fits_file = fits_file
    fits.PrimaryHDU(header=hdr, data=np.array(data_output)).writeto(
        output_fits_file, overwrite=True, checksum=True)


def transpose_image_data(fits_file, output_fits_file=None) -> None:
    """`dev` ``transpose_image_data``: primary-HDU data transposed."""
    logger.info("Transposing image in FITS file = %s", fits_file)

    hdul = fits.open(fits_file)
    hdr = hdul[0].header
    transpose_data = np.transpose(hdul[0].data)
    hdul[0] = fits.PrimaryHDU(header=hdr, data=np.array(transpose_data))

    if output_fits_file is None:
        output_fits_file = fits_file
    hdul.writeto(output_fits_file, overwrite=True, checksum=True)
    hdul.close()
