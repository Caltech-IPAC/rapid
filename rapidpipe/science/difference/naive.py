"""The naive subtraction: science minus gain-matched reference.

`dev`: ``compute_naive_difference_image``
(``modules/utils/rapid_pipeline_subs.py``). A diagnostic only: its file
and catalogs are made as `dev` makes them, and it is never a registered
difference-image instance (products page, lead 2026-09-22).
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits


def compute_naive_difference_image(fits_file_sci, fits_file_ref, output_fits_file) -> None:
    with fits.open(fits_file_sci) as hdul_sci:
        hdr_sci = hdul_sci[0].header
        data_sci = np.array(hdul_sci[0].data)
    with fits.open(fits_file_ref) as hdul_ref:
        data_ref = np.array(hdul_ref[0].data)

    diff_data = data_sci - data_ref

    fits.PrimaryHDU(data=diff_data.astype(np.float32), header=hdr_sci).writeto(
        output_fits_file, overwrite=True, checksum=True)
