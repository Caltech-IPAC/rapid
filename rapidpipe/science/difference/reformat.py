"""Reformat the delivered science image and model its uncertainty.

`dev`: ``reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model``
(``pipeline/differenceImageSubs.py``). Moves the image from HDU 1 to the
primary HDU, pads it from 4088x4088 to 4089x4089 with one row and one
column of the clipped mean, divides by EXPTIME (units DN/s), and writes a
simple-model uncertainty image (photon noise and read noise only), both
as float32 with FITS checksums.
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits


def reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model(
    input_filename,
    sca_gain: float,
    sca_readout_noise: float,
    clipped_image_mean: float,
    fname_output,
    fname_output_unc,
):
    hdul = fits.open(input_filename)
    hdr = hdul[1].header
    data = hdul[1].data

    exptime = hdr["EXPTIME"]
    hdr["BUNIT"] = "DN/s"

    np_data = np.array(data)
    new_row = np.full(np_data.shape[1], clipped_image_mean)
    new_arr = np.append(np_data, [new_row], axis=0)                 # Append extra row of trimmed-average background.
    new_col = np.full((new_arr.shape[0], 1), clipped_image_mean)
    new_np_data = np.append(new_arr, new_col, axis=1)               # Append extra column of trimmed-average background.

    new_np_data_norm = new_np_data / exptime

    hdu = fits.PrimaryHDU(header=hdr, data=new_np_data_norm.astype(np.float32))
    fits.HDUList([hdu]).writeto(fname_output, overwrite=True, checksum=True)

    # Ensure data are positive for uncertainty calculations. Read noise is
    # in electrons; /sca_gain**2 converts it to DN variance (inert at gain 1).

    pos_np_data = np.abs(new_np_data_norm)
    data_unc = np.sqrt(pos_np_data * exptime / sca_gain + sca_readout_noise ** 2 / sca_gain ** 2) / exptime

    hdu_unc = fits.PrimaryHDU(header=hdr, data=data_unc.astype(np.float32))
    fits.HDUList([hdu_unc]).writeto(fname_output_unc, overwrite=True, checksum=True)

    hdul.close()

    return fname_output, fname_output_unc
