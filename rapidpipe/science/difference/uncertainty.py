"""The difference-image uncertainty image.

`dev`: ``compute_diffimage_uncertainty`` (``pipeline/differenceImageSubs.py``).
Science source-excess photon noise, reference photon noise over the
coverage, and the difference image's clipped variance, in quadrature;
NaNs replaced by the clipped standard deviation. It is the SExtractor
weight image and the Photutils error image for the difference catalogs.

`dev` calls it with ``sca_gain * exptime`` as the gain argument.
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.statistics import fits_data_statistics_with_clipping


def compute_diffimage_uncertainty(sca_gain,
                                  science_image_filename,
                                  reference_image_filename,
                                  refimage_cov_map_filename,
                                  post_zogy_keep_diffimg_lower_cov_map_thresh,
                                  diffimage_filename,
                                  diffimage_unc_filename,
                                  seed: int | None = None) -> None:
    stats = fits_data_statistics_with_clipping(diffimage_filename, 3.0, 0, seed=seed)
    std_dif_img = stats["clippedstd"]

    hdul_sci = fits.open(science_image_filename)
    hdr_sci = hdul_sci[0].header
    np_data_sci = np.array(hdul_sci[0].data)
    # Non-negative source-excess flux only: on the background-subtracted
    # science image, blank-sky noise goes to 0 (its Poisson term is
    # already in std_dif_img).
    pos_np_data_sci = np.maximum(np_data_sci, 0.0)

    hdul_ref = fits.open(reference_image_filename)
    pos_np_data_ref = np.abs(np.array(hdul_ref[0].data))

    hdul_cov = fits.open(refimage_cov_map_filename)
    np_data_cov = np.array(hdul_cov[0].data)
    # Below-threshold coverage becomes 1.0, not NaN, so photutils does not fail.
    pos_np_data_cov = np.where(np_data_cov >= post_zogy_keep_diffimg_lower_cov_map_thresh,
                               np_data_cov, 1.0)

    data_unc = np.sqrt(pos_np_data_sci / sca_gain
                       + pos_np_data_ref / (sca_gain * pos_np_data_cov)
                       + std_dif_img * std_dif_img)
    pos_data_unc = np.where(np.isnan(data_unc), std_dif_img, data_unc)
    fits.HDUList([fits.PrimaryHDU(header=hdr_sci, data=pos_data_unc.astype(np.float32))]).writeto(
        diffimage_unc_filename, overwrite=True, checksum=True)

    hdul_sci.close()
    hdul_ref.close()
    hdul_cov.close()
