####################################################################################################################
# Compute the n-sigma point-source limiting magnitude of a Roman WFI L2 image.
#
# Usage:
#
#     python scripts/compute_limiting_magnitude.py <l2_image.fits> <psf.fits> [uncertainty.fits]
#
# Data units: the L2 image data are in DN (accumulated counts over EXPTIME seconds), whereas the
# ZPTMAG keyword is the AB magnitude zeropoint for flux in DN/s.  The background noise is
# therefore divided by EXPTIME before the zeropoint is applied:
#
#     flux [DN/s] = flux [DN] / EXPTIME [s]
#     mag [AB]    = -2.5 * log10(flux [DN/s]) + ZPTMAG
#
# An image the pipeline has already normalized to a rate carries BUNIT = "DN/s" and is not
# divided again.  The PSF model must be sampled at the pixel scale of the image; the RAPID PSF
# library files (WFI_SCAnn_Fnnn_PSF_DET_DIST.fits) are.
#
# Giving the uncertainty image as a third argument adds a second estimate of the limiting
# magnitude, made from the clipped median of that image instead of from the image pixels, which
# is a useful check that the uncertainty map is reasonable.  The uncertainty image must be in the
# same units as the science image.
#
# Run from the top of the RAPID software tree, or with it on PYTHONPATH.
####################################################################################################################

import os
import sys


# The RAPID software tree must be importable, which the pipeline arranges with
# PYTHONPATH=$RAPID_SW.  Fall back to RAPID_SW itself when running the script by hand.

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is not None and rapid_sw not in sys.path:
    sys.path.insert(0,rapid_sw)

try:
    import modules.utils.rapid_data_analysis as rda
except ModuleNotFoundError:
    print("*** Error: Cannot import modules.utils.rapid_data_analysis; set PYTHONPATH or "
          "RAPID_SW to the top of the RAPID software tree; quitting...")
    exit(64)


swname = "compute_limiting_magnitude.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)


# Number of sigmas that defines the limit, and the clipping used for the background estimate.
# All three are dimensionless.
# The 6-sigma single pass of the difference-image FOM metric is n_clip_sigma = 6.0, maxiters = 1,
# which is not appropriate for an L2 science image, whose sources survive a single pass.

n_sigma_limit = 5.0
n_clip_sigma = 3.0
maxiters = 10


# Read input filenames from the command line.

if len(sys.argv) < 3:
    print(f"*** Error: Usage: python scripts/{swname} <l2_image.fits> <psf.fits> [uncertainty.fits]")
    exit(64)

input_img_filename = sys.argv[1]
input_psf_filename = sys.argv[2]

input_unc_filename = None

if len(sys.argv) > 3:
    input_unc_filename = sys.argv[3]

for filename in (input_img_filename,input_psf_filename,input_unc_filename):

    if filename is not None and not os.path.exists(filename):
        print(f"*** Error: File not found: {filename}; quitting...")
        exit(64)

print("input_img_filename =",input_img_filename)
print("input_psf_filename =",input_psf_filename)
print("input_unc_filename =",input_unc_filename)


# List what the FITS header contributes to the calculation.

metadata = rda.get_l2_image_metadata(input_img_filename)

print("\nFITS-header metadata (zptmag is in AB mag for flux in DN/s; exptime in s;")
print("bunit gives the units of the image data; skymean, where present, is in DN):")

for key,val in metadata.items():
    print(f"   {key} = {val}")


# Compute the limiting magnitude from the background noise measured in the image itself.

print("\nLimiting magnitude from the image pixels:\n")

limmag = rda.compute_limiting_magnitude_for_l2_image(input_img_filename,
                                                     input_psf_filename,
                                                     n_sigma_limit = n_sigma_limit,
                                                     n_clip_sigma = n_clip_sigma,
                                                     maxiters = maxiters)


# Repeat with the uncertainty image, if one was given, as a check on the uncertainty map.

if input_unc_filename is not None:

    print("\nLimiting magnitude from the uncertainty image:\n")

    limmag_unc = rda.compute_limiting_magnitude_for_l2_image(input_img_filename,
                                                             input_psf_filename,
                                                             n_sigma_limit = n_sigma_limit,
                                                             n_clip_sigma = n_clip_sigma,
                                                             maxiters = maxiters,
                                                             input_unc_filename = input_unc_filename,
                                                             noise_method = "uncertainty")

    print("\nbkgsig ratio, image over uncertainty map (dimensionless) =",
          limmag["bkgsig"] / limmag_unc["bkgsig"])
    print("maglimit difference, image minus uncertainty map [AB mag] =",
          limmag["maglimit"] - limmag_unc["maglimit"])


print(f"\n{n_sigma_limit}-sigma limiting magnitude [AB mag] = {limmag['maglimit']}")

print("\nTerminating normally...")

exit(0)
