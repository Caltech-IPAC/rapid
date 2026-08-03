import argparse
import modules.utils.rapid_pipeline_subs as util

parser = argparse.ArgumentParser(description="Normalize a PSF FITS image.")
parser.add_argument("input_filename_psf", help="Input PSF FITS file.")
parser.add_argument("-o", "--output", dest="output_filename_psf", default="normalized_science_psf.fits",
                     help="Output normalized PSF FITS file (default: normalized_science_psf.fits).")
args = parser.parse_args()

input_filename_psf = args.input_filename_psf
output_filename_psf = args.output_filename_psf

print("input_filename_psf = ",input_filename_psf)
print("output_filename_psf = ",output_filename_psf)

hdu_index = 0
util.normalize_image(input_filename_psf,hdu_index,output_filename_psf)
