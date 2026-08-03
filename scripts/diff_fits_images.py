####################################################################################################################
# Read in two FITS images with GREEN_CCD and RED_CCD extnames, difference the image extensions separately,
# and then output result as FITS file.
####################################################################################################################

import argparse
import numpy as np
from astropy.io import fits

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("input_file_1", help="First input FITS file (subtracted from input_file_2).")
parser.add_argument("input_file_2", help="Second input FITS file.")
parser.add_argument("-o", "--output", dest="output_diff_file", default="diff_image.fits",
                     help="Output difference FITS file (default: diff_image.fits).")
args = parser.parse_args()

input_file_1 = args.input_file_1
input_file_2 = args.input_file_2

output_diff_file = args.output_diff_file

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

