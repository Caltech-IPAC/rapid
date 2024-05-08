####################################################################################################################
# Read in two FITS images with GREEN_CCD and RED_CCD extnames, difference the image extensions separately,
# and then output result as FITS file.
####################################################################################################################

import numpy as np
from astropy.io import fits

input_file_1 = "/Users/laher/git/rapid/c/common/awaicgen/awaicgen-5.2/output.fits"
input_file_2 = "/Users/laher/git/rapid/c/common/awaicgen/awaicgen-5.2/output2.fits"

output_diff_file = "diff_image.fits"

hdul_1 = fits.open(input_file_1)
hdul_2 = fits.open(input_file_2)

#ffis = ["GREEN_CCD","RED_CCD"]
ffis = [0]

hdu_list = []
x_window = 200         # Approximately along dispersion dimension.
y_window = 1           # Approximately along cross-dispersion dimension.
n_sigma = 3            # 3-sigma, double-sided outlier rejection

empty_data = None
hdu_list.append(fits.PrimaryHDU(empty_data))

for ffi in ffis:

    data_1 = hdul_1[ffi].data
    data_2 = hdul_2[ffi].data

    diff_data = data_2 - data_1

    hdu = fits.ImageHDU(diff_data.astype(np.float32))

    if type(ffi) == "str":
        hdu.header["EXTNAME"] = ffi
    else:
        hdu.header["EXTNAME"] = "PRIMARY"
    
    hdu_list.append(hdu)

hdu = fits.HDUList(hdu_list)
hdu.writeto(output_diff_file,overwrite=True,checksum=True)

