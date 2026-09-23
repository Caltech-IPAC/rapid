"""FITS image utilities several difference steps share.

`dev`: ``scale_image_data`` (``modules/utils/rapid_pipeline_subs.py``),
used for the gain-matched reference and its uncertainty, and for every
negative difference and significance image (a scale by -1).
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits


def scale_image_data(input_fits_file, scale_factor: float, output_fits_file) -> None:
    """`dev` ``scale_image_data``: primary-HDU data times a factor, as float32."""
    hdul = fits.open(input_fits_file)

    data = hdul[0].data

    np_data = np.array(data)
    scaled_data = np_data * scale_factor

    hdul[0].data = scaled_data

    new_hdu = fits.PrimaryHDU(data=hdul[0].data.astype(np.float32), header=hdul[0].header)

    new_hdu.writeto(output_fits_file, overwrite=True, checksum=True)

    hdul.close()
