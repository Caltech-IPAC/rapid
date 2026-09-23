"""Coverage-map masking of difference images.

`dev`: ``mask_difference_image_with_resampled_reference_cov_map``
(``pipeline/differenceImageSubs.py``). Pixels where the resampled
reference coverage map is below the threshold (nominally 0.5) become NaN;
SWarp resets the reference's NaNs to zero, which would otherwise leave
bogus positive values in the difference.
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits


def mask_difference_image_with_resampled_reference_cov_map(
    input_filename, mask_filename, output_filename, thresh,
) -> None:
    hdul_input = fits.open(input_filename)
    hdr_input = hdul_input[0].header
    np_data_input = np.array(hdul_input[0].data)

    hdul_mask = fits.open(mask_filename)
    np_data_mask = np.array(hdul_mask[0].data)

    np_data_output = np.where(np_data_mask >= thresh, np_data_input, np.nan)

    fits.HDUList([fits.PrimaryHDU(header=hdr_input, data=np_data_output)]).writeto(
        output_filename, overwrite=True, checksum=True)

    hdul_input.close()
    hdul_mask.close()
