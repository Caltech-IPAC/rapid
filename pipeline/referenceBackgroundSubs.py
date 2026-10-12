"""
Background subtraction of the resampled reference image with the science image's estimator.

The science image has its background removed by bkgest (a local clipped mean on a coarse grid).
The resampled reference used to have it removed by SWarp (SUBTRACT_BACK, a finer mesh), and the
two estimators treat the extended wings of bright stars and crowded regions differently, which
leaves a variable background in the difference image.  Running the same bkgest on both images
makes any estimator bias common-mode, so it cancels in the difference: on a galactic-bulge
field the difference-image median went from -5.58 to -0.06 DN/s.

SWarp writes 0 where the reference has no coverage, and bkgest skips NaN but not 0, so those
pixels are blanked to NaN for the bkgest run and given back their SWarp values afterwards.
"""

import numpy as np
from astropy.io import fits


def uncovered_pixels(cov_map_file,hdu_index=0):

    '''
    Return a boolean array, True where the resampled reference coverage map is not positive.
    '''

    cov = fits.getdata(cov_map_file,hdu_index)

    return ~(np.isfinite(cov) & (cov > 0))


def write_bkgest_input(reference_image_file,uncovered,bkgest_input_file,hdu_index=0):

    '''
    Write a copy of the reference image with the uncovered pixels set to NaN, for bkgest.
    '''

    with fits.open(reference_image_file) as hdul:
        data = np.array(hdul[hdu_index].data,dtype=np.float32)
        hdr = hdul[hdu_index].header.copy()

    if data.shape != uncovered.shape:
        raise ValueError(f"Reference image shape {data.shape} does not match coverage map shape {uncovered.shape}")

    data[uncovered] = np.nan

    fits.writeto(bkgest_input_file,data,hdr,overwrite=True)


def restore_uncovered_pixels(bkgest_output_file,reference_image_file,uncovered,output_file,hdu_index=0):

    '''
    Write the background-subtracted reference image to output_file, with the reference image's
    header and with the uncovered pixels given back their original values.
    '''

    with fits.open(reference_image_file) as hdul:
        original = np.array(hdul[hdu_index].data,dtype=np.float32)
        hdr = hdul[hdu_index].header.copy()

    data = np.array(fits.getdata(bkgest_output_file,0),dtype=np.float32)

    if data.shape != original.shape:
        raise ValueError(f"bkgest output shape {data.shape} does not match reference shape {original.shape}")

    data[uncovered] = original[uncovered]

    hdr["REFBKGSB"] = (True,"background subtracted with the science bkgest")

    fits.writeto(output_file,data,hdr,overwrite=True)
