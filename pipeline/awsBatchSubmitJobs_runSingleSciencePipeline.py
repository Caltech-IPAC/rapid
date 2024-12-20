import os
import configparser
import boto3
from botocore.exceptions import ClientError
from astropy.io import fits
import numpy as np
from datetime import datetime
import time

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import pipeline.referenceImageSubs as rfs

start_time_benchmark = time.time()


swname = "awsBatchSubmitJobs_runSingleSciencePipeline.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)

aws_batch_job_id = os.getenv('AWS_BATCH_JOB_ID')
print("aws_batch_job_id =", aws_batch_job_id)


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_now = datetime.utcnow()
proc_datetime = datetime_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_datetime =",proc_datetime)


# JOBPROCDATE of pipeline job.

job_proc_date = os.getenv('JOBPROCDATE')

if job_proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# RAPID_JOB_ID of pipeline job.

jid = os.getenv('RAPID_JOB_ID')

if jid is None:

    print("*** Error: Env. var. RAPID_JOB_ID not set; quitting...")
    exit(64)


# JOBS3BUCKET of pipeline job.

job_info_s3_bucket = os.getenv('JOBS3BUCKET')

if job_info_s3_bucket is None:

    print("*** Error: Env. var. JOBS3BUCKET not set; quitting...")
    exit(64)


# JOBCONFIGFILENAME of pipeline job.

job_config_ini_file_filename = os.getenv('JOBCONFIGFILENAME')

if job_config_ini_file_filename is None:

    print("*** Error: Env. var. JOBCONFIGFILENAME not set; quitting...")
    exit(64)


# JOBCONFIGOBJNAME of pipeline job.

job_config_ini_file_s3_bucket_object_name = os.getenv('JOBCONFIGOBJNAME')

if job_config_ini_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. JOBCONFIGOBJNAME not set; quitting...")
    exit(64)


# REFIMAGEINPUTSFILENAME of pipeline job.

input_images_csv_filename = os.getenv('REFIMAGEINPUTSFILENAME')

if input_images_csv_filename is None:

    print("*** Error: Env. var. REFIMAGEINPUTSFILENAME not set; quitting...")
    exit(64)


# REFIMAGEINPUTSOBJNAME of pipeline job.

input_images_csv_file_s3_bucket_object_name = os.getenv('REFIMAGEINPUTSOBJNAME')

if input_images_csv_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. REFIMAGEINPUTSOBJNAME not set; quitting...")
    exit(64)


# Print out basic information for log file.

print("job_proc_date =",job_proc_date)
print("jid =",jid)
print("job_info_s3_bucket =",job_info_s3_bucket)
print("job_config_ini_file_filename =",job_config_ini_file_filename)
print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
print("input_images_csv_file_s3_bucket_object_name =",input_images_csv_file_s3_bucket_object_name)


#-------------------------------------------------------------------
# Reformat a Troxel OpenUniverse simulated image FITS file
# so that the image data are contained in the PRIMARY header.
# Assumes the 2D image data are in the second HDU of the FITS file.
# Compute uncertainty image via simple model (photon noise only).
# We do this because the uncertainty image is not available as
# we removed the uncertainy FITS extension earlier from the
# Troxel OpenUniverse simulated images to save on disk-space costs.
# Resize images to 4089x4089 (odd number of pixels on each side).
#
# Inputs are:
# 1. A single gunzipped Troxel OpenUniverse simulated image (as
#    read directly from the S3 bucket where it is stored), and
# 2. SCA gain.

def reformat_troxel_fits_file_and_compute_uncertainty_image_via_simple_model(input_filename,sca_gain,clipped_image_mean):


    # Reformat the FITS file so that the image data are contained in the PRIMARY header.
    # Also, compute via a simple model the uncertainty image from the science image,
    # assuming some value for the SCA gain (electrons/ADU), which is unavailable for Roman WFI.

    fname_output = input_filename.replace(".fits","_reformatted.fits")
    fname_output_unc = input_filename.replace(".fits","_reformatted_unc.fits")

    hdul = fits.open(input_filename)
    hdr = hdul[1].header
    data = hdul[1].data

    np_data = np.array(data)
    new_row = np.full(np_data.shape[1], clipped_image_mean)
    new_arr = np.append(np_data, [new_row], axis=0)                 # Append extra row of trimmed-average background.
    new_col = np.full((new_arr.shape[0], 1), clipped_image_mean)
    new_np_data = np.append(new_arr, new_col, axis=1)               # Append extra column of trimmed-average background.

    hdu_list = []
    hdu = fits.PrimaryHDU(header=hdr,data=new_np_data.astype(np.float32))
    hdu_list.append(hdu)
    hdu = fits.HDUList(hdu_list)
    hdu.writeto(fname_output,overwrite=True,checksum=True)

    hdu_list_unc = []


    # Ensure data are positive for uncertainty calculations.

    pos_np_data = np.where(new_np_data >= 0.0,new_np_data,0.0)

    data_unc = np.sqrt(pos_np_data / sca_gain)
    hdu_unc = fits.PrimaryHDU(header=hdr,data=data_unc.astype(np.float32))
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
    pos_np_data_sci = np.where(np_data_sci >= 0.0,np_data_sci,0.0)

    hdul_ref = fits.open(reference_image_filename)
    hdr_ref = hdul_ref[0].header
    data_ref = hdul_ref[0].data
    np_data_ref = np.array(data_ref)
    pos_np_data_ref = np.where(np_data_ref >= 0.0,np_data_ref,0.0)

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


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Download job configuration data file from S3 bucket.

    s3_client = boto3.client('s3')

    print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name,job_config_ini_file_filename))

    response = s3_client.download_file(job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name,job_config_ini_file_filename)

    print("response =",response)


    # Read in job configuration parameters from .ini file.

    config_input = configparser.ConfigParser()
    config_input.read(job_config_ini_file_filename)

    verbose = int(config_input['JOB_PARAMS']['verbose'])
    debug = int(config_input['JOB_PARAMS']['debug'])
    job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
    product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
    refimage_psf_s3_bucket_dir = config_input['JOB_PARAMS']['refimage_psf_s3_bucket_dir']
    refimage_psf_filename = config_input['JOB_PARAMS']['refimage_psf_filename']

    product_s3_bucket = product_s3_bucket_base

    product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

    sca_gain = float(config_input['INSTRUMENT']['sca_gain'])

    ppid_sciimage = int(config_input['SCI_IMAGE']['ppid'])

    rid_sciimage = int(config_input['SCI_IMAGE']['rid'])
    sca_sciimage = int(config_input['SCI_IMAGE']['sca'])
    fid_sciimage = int(config_input['SCI_IMAGE']['fid'])
    s3_full_name_science_image = config_input['SCI_IMAGE']['s3_full_name_science_image']
    expid_sciimage = int(config_input['SCI_IMAGE']['expid'])
    field_sciimage = int(config_input['SCI_IMAGE']['field'])
    mjdobs_sciimage = float(config_input['SCI_IMAGE']['mjdobs'])
    exptime_sciimage = float(config_input['SCI_IMAGE']['exptime'])
    infobits_sciimage = int(config_input['SCI_IMAGE']['infobits'])
    status_sciimage = int(config_input['SCI_IMAGE']['status'])

    ra0_sciimage = float(config_input['SCI_IMAGE']['ra0'])
    dec0_sciimage = float(config_input['SCI_IMAGE']['dec0'])
    ra1_sciimage = float(config_input['SCI_IMAGE']['ra1'])
    dec1_sciimage = float(config_input['SCI_IMAGE']['dec1'])
    ra2_sciimage = float(config_input['SCI_IMAGE']['ra2'])
    dec2_sciimage = float(config_input['SCI_IMAGE']['dec2'])
    ra3_sciimage = float(config_input['SCI_IMAGE']['ra3'])
    dec3_sciimage = float(config_input['SCI_IMAGE']['dec3'])
    ra4_sciimage = float(config_input['SCI_IMAGE']['ra4'])
    dec4_sciimage = float(config_input['SCI_IMAGE']['dec4'])

    sky_tile_rtid = int(config_input['SKY_TILE']['rtid'])
    sky_tile_ra0 = float(config_input['SKY_TILE']['ra0'])
    sky_tile_dec0 = float(config_input['SKY_TILE']['dec0'])
    sky_tile_ra1 = float(config_input['SKY_TILE']['ra1'])
    sky_tile_dec1 = float(config_input['SKY_TILE']['dec1'])
    sky_tile_ra2 = float(config_input['SKY_TILE']['ra2'])
    sky_tile_dec2 = float(config_input['SKY_TILE']['dec2'])
    sky_tile_ra3 = float(config_input['SKY_TILE']['ra3'])
    sky_tile_dec3 = float(config_input['SKY_TILE']['dec3'])
    sky_tile_ra4 = float(config_input['SKY_TILE']['ra4'])
    sky_tile_dec4 = float(config_input['SKY_TILE']['dec4'])

    rfid_str = config_input['REF_IMAGE']['rfid']

    if rfid_str == 'None':
        rfid = None
    else:
        rfid = int(rfid_str)

    ppid_refimage = int(config_input['REF_IMAGE']['ppid'])
    max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])
    naxis1_refimage = int(config_input['REF_IMAGE']['naxis1'])
    naxis2_refimage = int(config_input['REF_IMAGE']['naxis2'])
    cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1'])
    cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2'])
    crota2_refimage = float(config_input['REF_IMAGE']['crota2'])
    ra0_refimage = float(config_input['REF_IMAGE']['ra0'])
    dec0_refimage = float(config_input['REF_IMAGE']['dec0'])
    ra1_refimage = float(config_input['REF_IMAGE']['ra1'])
    dec1_refimage = float(config_input['REF_IMAGE']['dec1'])
    ra2_refimage = float(config_input['REF_IMAGE']['ra2'])
    dec2_refimage = float(config_input['REF_IMAGE']['dec2'])
    ra3_refimage = float(config_input['REF_IMAGE']['ra3'])
    dec3_refimage = float(config_input['REF_IMAGE']['dec3'])
    ra4_refimage = float(config_input['REF_IMAGE']['ra4'])
    dec4_refimage = float(config_input['REF_IMAGE']['dec4'])

    astrometric_uncert_x = float(config_input['ZOGY']['astrometric_uncert_x'])
    astrometric_uncert_y = float(config_input['ZOGY']['astrometric_uncert_y'])
    zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']
    post_zogy_keep_diffimg_lower_cov_map_thresh = float(config_input['ZOGY']['post_zogy_keep_diffimg_lower_cov_map_thresh'])
    s3_full_name_psf = config_input['ZOGY']['s3_full_name_psf']

    awaicgen_dict = config_input['AWAICGEN']

    swarp_dict = config_input['SWARP']

    sextractor_diffimage_dict = config_input['SEXTRACTOR_DIFFIMAGE']
    sextractor_sciimage_dict = config_input['SEXTRACTOR_SCIIMAGE']
    sextractor_refimage_dict = config_input['SEXTRACTOR_REFIMAGE']
    bkgest_dict = config_input['BKGEST']

    print("max_n_images_to_coadd =", max_n_images_to_coadd)


    # Download gzipped science image from S3 bucket.

    science_image_filename_gz,subdirs_science_image,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_science_image)


    # Upload science image to product S3 bucket (in order to test upload method).

    s3_object_name_science_image = job_proc_date + "/jid" + str(jid) + "/" + science_image_filename_gz

    util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,[science_image_filename_gz],[s3_object_name_science_image])


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after downloading science image =",end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Optionally read in CVS file containing inputs for generating reference image.

    if rfid is not None:


        # Download reference image and associated coverage map and uncertainty image from S3 bucket.

        infobits_refimage = config_input['REF_IMAGE']['infobits']
        s3_full_name_reference_image = config_input['REF_IMAGE']['filename']
        awaicgen_output_mosaic_image_file,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_reference_image)

        # For now, require the filename derived from the database record is same as in job configuration file under AWAICGEN block.

        if awaicgen_output_mosaic_image_file != awaicgen_dict["awaicgen_output_mosaic_image_file"]:

            print("*** Error: Filename derived from database record {} is not same as filename in job configuration file {}; quitting...".\
                format(awaicgen_output_mosaic_image_file,awaicgen_dict["awaicgen_output_mosaic_image_file"]))
            exit(64)

        awaicgen_output_mosaic_cov_map_file = awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
        awaicgen_output_mosaic_uncert_image_file = awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]
        awaicgen_output_mosaic_cov_map_s3_bucket_object_name = subdirs + "/" +\
            awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
        awaicgen_output_mosaic_uncert_image_s3_bucket_object_name = subdirs + "/" +\
            awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]

        print("Downloading s3://{}/{} into {}...".\
            format(product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name,awaicgen_output_mosaic_cov_map_file))

        response = s3_client.download_file(product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name,awaicgen_output_mosaic_cov_map_file)

        print("response =",response)

        print("Downloading s3://{}/{} into {}...".\
            format(product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name,awaicgen_output_mosaic_uncert_image_file))

        response = s3_client.download_file(product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name,awaicgen_output_mosaic_uncert_image_file)

        print("response =",response)

    else:


        # Generate reference image.

        generateReferenceImage_return_list = rfs.generateReferenceImage(s3_client,
                                                                        job_info_s3_bucket,
                                                                        input_images_csv_file_s3_bucket_object_name,
                                                                        input_images_csv_filename,
                                                                        jid,
                                                                        job_proc_date,
                                                                        awaicgen_dict,
                                                                        max_n_images_to_coadd,
                                                                        sca_gain,
                                                                        product_s3_bucket)

        infobits_refimage = generateReferenceImage_return_list[0]
        checksum_refimage = generateReferenceImage_return_list[1]
        awaicgen_output_mosaic_image_file = generateReferenceImage_return_list[2]
        awaicgen_output_mosaic_cov_map_file = generateReferenceImage_return_list[3]
        awaicgen_output_mosaic_uncert_image_file = generateReferenceImage_return_list[4]
        awaicgen_output_mosaic_image_s3_bucket_object_name = generateReferenceImage_return_list[5]
        awaicgen_output_mosaic_cov_map_s3_bucket_object_name = generateReferenceImage_return_list[6]
        awaicgen_output_mosaic_uncert_image_s3_bucket_object_name = generateReferenceImage_return_list[7]

















    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after downloading or generating reference image =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Populate config-file dictionary for products.

    product_config_ini_filename = product_config_filename_base + str(jid) + ".ini"
    product_config_ini_file_s3_bucket_object_name = job_proc_date + "/" + product_config_ini_filename

    product_config = configparser.ConfigParser()

    product_config['JOB_PARAMS'] = {'debug': str(debug),
                                 'swname': swname,
                                 'swvers': swvers}

    product_config['JOB_PARAMS']['product_s3_bucket_base'] = product_s3_bucket_base
    product_config['JOB_PARAMS']['jid'] = str(jid)
    product_config['JOB_PARAMS']['job_proc_date'] = job_proc_date
    product_config['JOB_PARAMS']['verbose'] = str(verbose)

    if rfid is None:

        product_config['REF_IMAGE'] = {}

        product_config['REF_IMAGE']['rfid'] = str(rfid)
        product_config['REF_IMAGE']['ppid'] = str(ppid_sciimage)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file_checksum'] = checksum_refimage

        mosaic_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name)
        mosaic_cov_map_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name)
        mosaic_uncert_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)

        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file'] = mosaic_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_cov_map_file'] = mosaic_cov_map_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_uncert_image_file'] = mosaic_uncert_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_status'] = str(1)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_infobits'] = str(infobits_refimage)


    # Unzip the science image gzipped file.

    gunzip_cmd = ['gunzip', science_image_filename_gz]
    exitcode_from_gunzip = util.execute_command(gunzip_cmd)

    science_image_filename = science_image_filename_gz.replace(".fits.gz",".fits")


    # Compute image statistics for image resizing.

    n_sigma = 3.0
    avg_sci_img,std_sci_img,cnt_sci_img = util.avg_data_with_clipping(science_image_filename,n_sigma,1)


    # Reformat the Troxel OpenUniverse simulated image FITS file
    # so that the image data are contained in the PRIMARY header.
    # Compute uncertainty image via simple model (photon noise only).
    # Resize images to 4089x4089 (odd number of pixels on each side).

    reformatted_science_image_filename,\
        reformatted_science_uncert_image_filename =\
        reformat_troxel_fits_file_and_compute_uncertainty_image_via_simple_model(science_image_filename,sca_gain,avg_sci_img)


    # Swarp the reference image and associated uncertainty image into the distortion frame of the science image.
    # Since the reference image was made by awaicgen, there is no geometric image distortion,
    # and, hence, no need to convert from sip to pv distortion, so the following flag is set to False.
    # Set the following flag to True only for the case where the reference image is a single Roman SCA image.

    hdu_index_for_science_image_data = 0
    hdu_index_for_reference_image_data = 0
    pv_convert_flag_for_reference_image_data = False                   # TODO

    sci_fits_file_with_pv,\
        ref_fits_file_with_pv,\
        ref_cov_fits_file_with_pv,\
        ref_uncert_fits_file_with_pv,\
        output_resampled_reference_image,\
        output_resampled_reference_cov_map,\
        output_resampled_reference_uncert_image =\
        util.resample_reference_image_to_science_image_with_pv_distortion(reformatted_science_image_filename,\
                                                                          hdu_index_for_science_image_data,\
                                                                          awaicgen_output_mosaic_image_file,\
                                                                          awaicgen_output_mosaic_cov_map_file,\
                                                                          awaicgen_output_mosaic_uncert_image_file,\
                                                                          hdu_index_for_reference_image_data,\
                                                                          pv_convert_flag_for_reference_image_data,\
                                                                          swarp_dict)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after swarping images =",end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Upload intermediate FITS files to product S3 bucket for diagnostic purposes.

    product_s3_bucket = product_s3_bucket_base
    s3_object_name_reformatted_science_image_filename = job_proc_date + "/jid" + str(jid) + "/" + reformatted_science_image_filename
    s3_object_name_reformatted_science_uncert_image_filename = job_proc_date + "/jid" + str(jid) + "/" + reformatted_science_uncert_image_filename
    s3_object_name_sci_fits_file_with_pv = job_proc_date + "/jid" + str(jid) + "/" + sci_fits_file_with_pv
    s3_object_name_ref_fits_file_with_pv = job_proc_date + "/jid" + str(jid) + "/" + ref_fits_file_with_pv
    s3_object_name_output_resampled_reference_image = job_proc_date + "/jid" + str(jid) + "/" + output_resampled_reference_image
    s3_object_name_output_resampled_reference_cov_map = job_proc_date + "/jid" + str(jid) + "/" + output_resampled_reference_cov_map
    s3_object_name_output_resampled_reference_uncert_image = job_proc_date + "/jid" + str(jid) + "/" + output_resampled_reference_uncert_image

    filenames = [reformatted_science_image_filename,
                 reformatted_science_uncert_image_filename,
                 sci_fits_file_with_pv,
                 output_resampled_reference_image,
                 output_resampled_reference_cov_map,
                 output_resampled_reference_uncert_image]

    objectnames = [s3_object_name_reformatted_science_image_filename,
                   s3_object_name_reformatted_science_uncert_image_filename,
                   s3_object_name_sci_fits_file_with_pv,
                   s3_object_name_output_resampled_reference_image,
                   s3_object_name_output_resampled_reference_cov_map,
                   s3_object_name_output_resampled_reference_uncert_image]

    if pv_convert_flag_for_reference_image_data:
        filenames.append(ref_fits_file_with_pv)
        objectnames.append(s3_object_name_ref_fits_file_with_pv)

    util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


    # Compute image statistics for ZOGY.

    n_sigma = 3.0
    avg_sci_img,std_sci_img,cnt_sci_img = util.avg_data_with_clipping(reformatted_science_image_filename,n_sigma)
    avg_ref_img,std_ref_img,cnt_ref_img = util.avg_data_with_clipping(output_resampled_reference_image,n_sigma)

    print("avg_sci_img,std_sci_img,cnt_sci_img =",avg_sci_img,std_sci_img,cnt_sci_img)
    print("avg_ref_img,std_ref_img,cnt_ref_img =",avg_ref_img,std_ref_img,cnt_ref_img)


    # Download PSFs from S3 bucket.

    filename_psf,subdirs_psf,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_psf)

    print("s3_full_name_psf = ",s3_full_name_psf)
    print("filename_psf = ",filename_psf)

    refimage_psf_filename = refimage_psf_filename.replace("FID",str(fid_sciimage))
    s3_full_name_refimage_psf = "s3://" + job_info_s3_bucket + "/" + refimage_psf_s3_bucket_dir + "/" + refimage_psf_filename
    filename_refimage_psf,subdirs_refimage_psf,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_refimage_psf)

    print("s3_full_name_refimage_psf = ",s3_full_name_refimage_psf)
    print("filename_refimage_psf = ",filename_refimage_psf)


    # Subtract background from science image.  Since the reference image has been swarped,
    # it already has the background subtracted.

    bkgest_code = '/code/c/bin/bkgest'
    bkgest_include_dir = '/code/c/include'
    filename_bkg_subbed_science_image = 'bkg_subbed_science_image.fits'
    filename_global_clippedmean_sciimage_tbl = 'global_clippedmean_science_image.tbl'

    bkgest_cmd = [bkgest_code,
                  '-i',
                  reformatted_science_image_filename,
                  '-f',
                  bkgest_dict["output_image_type"],
                  '-c',
                  bkgest_dict["clippedmean_calc_type"],
                  '-g',
                  bkgest_dict["local_clippedmean_grid_spacing"],
                  '-w',
                  bkgest_dict["local_clippedmean_input_window"],
                  '-a',
                  bkgest_include_dir,
                  '-ot',
                  filename_global_clippedmean_sciimage_tbl,
                  '-o2',
                  filename_bkg_subbed_science_image]

    exitcode_from_bkgest = util.execute_command(bkgest_cmd)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after running bkgest on science image =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # The image data in science_image_filename and sci_fits_file_with_pv FITS files are the same, only the
    # represensation of geometric distortion in the FITS headers are different (sip versus pv).
    #
    # ZOGY only cares about the image data, not what is in the FITS headers.
    # Usage: python py_zogy.py <NewImage> <RefImage> <NewPSF> <RefPSF> <NewSigmaImage> <RefSigmaImage>
    #                    <NewSigmaMode> <RefSigmaMode> <AstUncertX> <AstUncertY> <DiffImage> <DiffPSF> <ScorrImage>
    #
    # Assume top-level directory of rapid git repo is mapped to /code inside Docker container.


    python_cmd = '/usr/bin/python3'
    zogy_code = '/code/modules/zogy/v21Aug2018/py_zogy.py'
    filename_diffimage = 'diffimage.fits'
    filename_diffpsf = 'diffpsf.fits'
    filename_scorrimage = 'scorrimage.fits'

    zogy_cmd = [python_cmd,
                zogy_code,
                filename_bkg_subbed_science_image,
                output_resampled_reference_image,
                filename_psf,
                filename_refimage_psf,
                reformatted_science_uncert_image_filename,
                output_resampled_reference_uncert_image,
                str(std_sci_img),
                str(std_ref_img),
                str(astrometric_uncert_x),
                str(astrometric_uncert_y),
                filename_diffimage,
                filename_diffpsf,
                filename_scorrimage]

    exitcode_from_zogy = util.execute_command(zogy_cmd)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after running ZOGY =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Mask difference image with output_resampled_reference_cov_map.

    filename_diffimage_masked = zogy_output_diffimage_file                     # Nominally diffimage_masked.fits
    filename_scorrimage_masked = 'scorrimage_masked.fits'

    mask_difference_image_with_resampled_reference_cov_map(filename_diffimage,
                                                           output_resampled_reference_cov_map,
                                                           filename_diffimage_masked,
                                                           post_zogy_keep_diffimg_lower_cov_map_thresh)

    mask_difference_image_with_resampled_reference_cov_map(filename_scorrimage,
                                                           output_resampled_reference_cov_map,
                                                           filename_scorrimage_masked,
                                                           post_zogy_keep_diffimg_lower_cov_map_thresh)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after masking difference image =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Generate diffimage uncertainty image, which will be the weight image for sextractor_WEIGHT_IMAGE.

    filename_diffimage_unc_masked = 'diffimage_uncert_masked.fits'
    compute_diffimage_uncertainty(sca_gain,
                                 reformatted_science_image_filename,
                                 output_resampled_reference_image,
                                 output_resampled_reference_cov_map,
                                 filename_diffimage_masked,
                                 filename_diffimage_unc_masked)
    filename_weight_image = filename_diffimage_unc_masked
    filename_diffimage_sextractor_catalog = filename_diffimage_masked.replace(".fits",".txt")


    # Compute SExtractor catalog for masked difference image.
    # Execute SExtractor to first detect candidates on Scorr (S/N) match-filter
    # image, then use to perform aperture phot on difference image to generate
    # raw ascii catalog file.

    sextractor_diffimage_dict["sextractor_detection_image".lower()] = filename_scorrimage_masked
    sextractor_diffimage_dict["sextractor_input_image".lower()] = filename_diffimage_masked
    sextractor_diffimage_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_weight_image
    sextractor_diffimage_dict["sextractor_PARAMETERS_NAME".lower()] = "/code/cdf/rapidSexParamsDiffImage.inp"
    sextractor_diffimage_dict["sextractor_FILTER_NAME".lower()] = "/code/cdf/rapidSexDiffImageFilter.conv"
    sextractor_diffimage_dict["sextractor_STARNNW_NAME".lower()] = "/code/cdf/rapidSexDiffImageStarGalaxyClassifier.nnw"
    sextractor_diffimage_dict["sextractor_CATALOG_NAME".lower()] = filename_diffimage_sextractor_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_diffimage_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after running SExtractor on difference image =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Compute MD5 checksum of masked difference image.

    print("Computing checksum of ",filename_diffimage_masked)
    checksum_diffimage = db.compute_checksum(filename_diffimage_masked)

    if checksum_diffimage == 65 or checksum_diffimage == 68 or checksum_diffimage == 66:
        print("*** Error: Unexpected value for checksum =",checksum_diffimage)


    # Upload intermediate and final FITS files to product S3 bucket.

    product_s3_bucket = product_s3_bucket_base
    s3_object_name_diffimage = job_proc_date + "/jid" + str(jid) + "/" + filename_diffimage_masked
    s3_object_name_diffimage_unc = job_proc_date + "/jid" + str(jid) + "/" + filename_diffimage_unc_masked
    s3_object_name_diffimage_catalog = job_proc_date + "/jid" + str(jid) + "/" + filename_diffimage_sextractor_catalog
    s3_object_name_diffpsf = job_proc_date + "/jid" + str(jid) + "/" + filename_diffpsf
    s3_object_name_scorrimage = job_proc_date + "/jid" + str(jid) + "/" + filename_scorrimage_masked
    s3_object_name_bkg_subbed_science_image = job_proc_date + "/jid" + str(jid) + "/" + filename_bkg_subbed_science_image

    filenames = [filename_diffimage_masked,
                 filename_diffimage_unc_masked,
                 filename_diffimage_sextractor_catalog,
                 filename_diffpsf,
                 filename_scorrimage_masked,
                 filename_bkg_subbed_science_image]

    objectnames = [s3_object_name_diffimage,
                   s3_object_name_diffimage_unc,
                   s3_object_name_diffimage_catalog,
                   s3_object_name_diffpsf,
                   s3_object_name_scorrimage,
                   s3_object_name_bkg_subbed_science_image]

    util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


    # Get listing of working directory as a diagnostic.

    ls_cmd = ['ls','-ltr']
    exitcode_from_ls = util.execute_command(ls_cmd)


    # Define ZOGY dictionary in config-file dictionary for products.

    product_config['ZOGY'] = {}

    zogy_diffimage_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,s3_object_name_diffimage)
    zogy_diffpsf_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,s3_object_name_diffpsf)
    zogy_scorrimage_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,s3_object_name_scorrimage)

    product_config['ZOGY']['rid'] = str(rid_sciimage)
    product_config['ZOGY']['ppid'] = str(ppid_sciimage)
    product_config['ZOGY']['rfid'] = str(rfid)


    # By design, the following is redundant.  It is also written to REF_IMAGE block above
    # only if it was necessary for this pipeline instance to generate a reference image.

    product_config['ZOGY']['awaicgen_output_mosaic_image_infobits'] = str(infobits_refimage)

    product_config['ZOGY']['zogy_output_diffimage_file_checksum'] = checksum_diffimage
    product_config['ZOGY']['zogy_output_diffimage_file'] = zogy_diffimage_name_for_db_record
    product_config['ZOGY']['zogy_output_diffpsf_file'] = zogy_diffpsf_name_for_db_record
    product_config['ZOGY']['zogy_output_scorrimage_file'] = zogy_scorrimage_name_for_db_record
    product_config['ZOGY']['zogy_output_diffimage_file_status'] = str(1)
    product_config['ZOGY']['zogy_output_diffimage_file_infobits'] = str(0)                                        # TODO


    # The following sky positions are correct for the difference image
    # only because the current code reprojects the reference image
    # into the distorted grid of the science image.

    product_config['ZOGY']['ra0'] = str(ra0_sciimage)
    product_config['ZOGY']['dec0'] = str(dec0_sciimage)
    product_config['ZOGY']['ra1'] = str(ra1_sciimage)
    product_config['ZOGY']['dec1'] = str(dec1_sciimage)
    product_config['ZOGY']['ra2'] = str(ra2_sciimage)
    product_config['ZOGY']['dec2'] = str(dec2_sciimage)
    product_config['ZOGY']['ra3'] = str(ra3_sciimage)
    product_config['ZOGY']['dec3'] = str(dec3_sciimage)
    product_config['ZOGY']['ra4'] = str(ra4_sciimage)
    product_config['ZOGY']['dec4'] = str(dec4_sciimage)


    # Write product config file for job.

    with open(product_config_ini_filename, 'w') as product_configfile:

        product_configfile.write("#" + "\n")
        product_configfile.write("# s3://" + product_s3_bucket + "/" + product_config_ini_file_s3_bucket_object_name + "\n")
        product_configfile.write("#" + "\n")
        product_configfile.write("# " + proc_utc_datetime + "\n")
        product_configfile.write("#" + "\n")
        product_configfile.write("# Machine-generated by " + swname + "\n")
        product_configfile.write("#" + "\n")
        product_configfile.write("\n")

        product_config.write(product_configfile)


    # Upload producy config file for job, along with associated file(s) if any, to S3 bucket.

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(product_config_ini_filename,
                                         product_s3_bucket,
                                         product_config_ini_file_s3_bucket_object_name)
    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(product_config_ini_filename,product_s3_bucket,product_config_ini_file_s3_bucket_object_name))
        uploaded_to_bucket = False

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(product_config_ini_filename,product_s3_bucket,product_config_ini_file_s3_bucket_object_name))


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after uploading products at pipeline end =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
