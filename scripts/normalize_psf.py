import os
import modules.utils.rapid_pipeline_subs as util

# Input/output PSF paths are set via environment variables; no personal
# path is committed here.
input_filename_psf = os.environ["PSF_INPUT_FILE"]
output_filename_psf = os.environ.get("PSF_OUTPUT_FILE", "normalized_science_psf.fits")

print("input_filename_psf = ",input_filename_psf)
print("output_filename_psf = ",output_filename_psf)

hdu_index = 0
util.normalize_image(input_filename_psf,hdu_index,output_filename_psf)
