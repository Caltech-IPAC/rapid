from astropy.io import fits
import numpy as np

import modules.utils.rapid_pipeline_subs as util


# Subs used by the RAPID pipeline related to difference-image processing.


#-------------------------------------------------------------------
# Reformat a Troxel OpenUniverse simulated image FITS file
# so that the image data are contained in the PRIMARY header.
# Assumes the 2D image data are in the second HDU of the FITS file.
# Compute uncertainty image via simple model (photon noise only).
# We do this because the uncertainty image is not available as
# we removed the uncertainy FITS extension earlier from the
# Troxel OpenUniverse simulated images to save on disk-space costs.
# Resize images to 4089x4089 (odd number of pixels on each side).
# Normalize by exposure time.
#
# Inputs are:
# 1. A single gunzipped Troxel OpenUniverse simulated image (as
#    read directly from the S3 bucket where it is stored), and
# 2. SCA gain.
# 3. EXPTIME

def reformat_troxel_fits_file_and_compute_uncertainty_image_via_simple_model(input_filename,sca_gain,clipped_image_mean):


    # Reformat the FITS file so that the image data are contained in the PRIMARY header.
    # Also, compute via a simple model the uncertainty image from the science image,
    # assuming some value for the SCA gain (electrons/ADU), which is unavailable for Roman WFI.

    fname_output = input_filename.replace(".fits","_reformatted.fits")
    fname_output_unc = input_filename.replace(".fits","_reformatted_unc.fits")

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

    hdu = fits.PrimaryHDU(header=hdr,data=new_np_data_norm.astype(np.float32))
    hdu_list = []
    hdu_list.append(hdu)
    hdu = fits.HDUList(hdu_list)
    hdu.writeto(fname_output,overwrite=True,checksum=True)


    # Ensure data are positive for uncertainty calculations.

    pos_np_data_norm = np.abs(new_np_data_norm)
    data_unc = np.sqrt(pos_np_data_norm / sca_gain)

    hdu_unc = fits.PrimaryHDU(header=hdr,data=data_unc.astype(np.float32))
    hdu_list_unc = []
    hdu_list_unc.append(hdu_unc)
    hdu_unc = fits.HDUList(hdu_list_unc)
    hdu_unc.writeto(fname_output_unc,overwrite=True,checksum=True)

    return fname_output,fname_output_unc


#-------------------------------------------------------------------
# Mask input image with input mask.  Values in input image are reset
# to NaN if mask is less than the given threshold at a pixel location,
# which is nominally 0.5 in cov map (converted to float after swarping).
# This method is applied to ZOGY outputs because swarping the
# reference image resets NaNs to zero, which would otherwise
# give bogus positive image values in the difference image.

def mask_difference_image_with_resampled_reference_cov_map(input_filename,mask_filename,output_filename,thresh):

    hdul_input = fits.open(input_filename)
    hdr_input = hdul_input[0].header
    data_input = hdul_input[0].data

    hdul_mask = fits.open(mask_filename)
    data_mask = hdul_mask[0].data

    np_data_input = np.array(data_input)
    np_data_mask = np.array(data_mask)

    np_data_output = np.where(np_data_mask >= thresh,np_data_input,np.nan)

    hdu_list = []
    hdu = fits.PrimaryHDU(header=hdr_input,data=np_data_output)
    hdu_list.append(hdu)
    hdu = fits.HDUList(hdu_list)
    hdu.writeto(output_filename,overwrite=True,checksum=True)

    return


#-------------------------------------------------------------------------------------------------------------
# Compute diffimage_uncertainty for use as weight.fits in computing SExtractor catalog for differenced image.
# Assume science-image photon noise, reference-image photon noise, and difference-image variance contribute.
#-------------------------------------------------------------------------------------------------------------

def compute_diffimage_uncertainty(sca_gain,
                                  science_image_filename,
                                  reference_image_filename,
                                  refiamge_cov_map_filename,
                                  diffimage_filename,
                                  diffimage_unc_filename):

    n_sigma = 3.0
    hdu_num = 0
    avg_dif_img,std_dif_img,cnt_dif_img = util.avg_data_with_clipping(diffimage_filename,n_sigma,hdu_num)

    hdul_sci = fits.open(science_image_filename)
    hdr_sci = hdul_sci[0].header
    data_sci = hdul_sci[0].data
    np_data_sci = np.array(data_sci)
    pos_np_data_sci = np.abs(np_data_sci)

    hdul_ref = fits.open(reference_image_filename)
    hdr_ref = hdul_ref[0].header
    data_ref = hdul_ref[0].data
    np_data_ref = np.array(data_ref)
    pos_np_data_ref = np.abs(np_data_ref)

    hdul_cov = fits.open(refiamge_cov_map_filename)
    hdr_cov = hdul_cov[0].header
    data_cov = hdul_cov[0].data
    np_data_cov = np.array(data_cov)
    pos_np_data_cov = np.where(np_data_cov >= 0.5,np_data_cov,np.nan)

    hdu_list_unc = []
    data_unc = np.sqrt(pos_np_data_sci / sca_gain + pos_np_data_ref / (sca_gain * pos_np_data_cov) + std_dif_img * std_dif_img)
    hdu_unc = fits.PrimaryHDU(header=hdr_sci,data=data_unc.astype(np.float32))
    hdu_list_unc.append(hdu_unc)
    hdu_unc = fits.HDUList(hdu_list_unc)
    hdu_unc.writeto(diffimage_unc_filename,overwrite=True,checksum=True)


######################################################################################
# Gain-match science and reference images by generating SExtractor catalogs for each.
# Assumptions:
# 1. Input reference image is resampled into distortion grid science image (or vice versa).
# 2. Distortion given byPV representation.
# 3. Both input images have been locally background-subtracted.
######################################################################################

def gainMatchScienceAndReferenceImages(filename_sci_image,
                                       filename_sci_uncert,
                                       filename_ref_image,
                                       filename_ref_uncert,
                                       sextractor_gainmatch_dict):


    # Compute SExtractor catalog for science image.

    filename_scigainmatchsexcat_catalog = sci_image_filename.replace(".fits","_scigainmatchsexcat.txt")

    sextractor_gainmatch_dict["sextractor_detection_image".lower()] = "None"
    sextractor_gainmatch_dict["sextractor_input_image".lower()] = filename_sci_image
    sextractor_gainmatch_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_sci_uncert
    sextractor_gainmatch_dict["sextractor_PARAMETERS_NAME".lower()] = "/code/cdf/rapidSexParamsGainMatch.inp"
    sextractor_gainmatch_dict["sextractor_FILTER_NAME".lower()] = "/code/cdf/rapidSexGainMatchFilter.conv"
    sextractor_gainmatch_dict["sextractor_STARNNW_NAME".lower()] = "/code/cdf/rapidSexGainMatchStarGalaxyClassifier.nnw"
    sextractor_gainmatch_dict["sextractor_CATALOG_NAME".lower()] = filename_scigainmatchsexcat_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_gainmatch_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)


    # Compute SExtractor catalog for reference image.

    filename_refgainmatchsexcat_catalog = ref_image_filename.replace(".fits","_refgainmatchsexcat.txt")

    sextractor_gainmatch_dict["sextractor_detection_image".lower()] = "None"
    sextractor_gainmatch_dict["sextractor_input_image".lower()] = filename_ref_image
    sextractor_gainmatch_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_ref_uncert
    sextractor_gainmatch_dict["sextractor_PARAMETERS_NAME".lower()] = "/code/cdf/rapidSexParamsGainMatch.inp"
    sextractor_gainmatch_dict["sextractor_FILTER_NAME".lower()] = "/code/cdf/rapidSexGainMatchFilter.conv"
    sextractor_gainmatch_dict["sextractor_STARNNW_NAME".lower()] = "/code/cdf/rapidSexGainMatchStarGalaxyClassifier.nnw"
    sextractor_gainmatch_dict["sextractor_CATALOG_NAME".lower()] = filename_refgainmatchsexcat_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_gainmatch_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)



#   This is a work in progress.

    return
