import modules.utils.rapid_pipeline_subs as util

input_filename_psf = "/Users/laher/Folks/rapid/psfs/PSFs/WFI_SCA04_F158_PSF_DET_DIST.fits"
output_filename_psf = "/Users/laher/Folks/rapid/normalized_science_psf.fits"

print("input_filename_psf = ",input_filename_psf)
print("output_filename_psf = ",output_filename_psf)

hdu_index = 0
util.normalize_image(input_filename_psf,hdu_index,output_filename_psf)
