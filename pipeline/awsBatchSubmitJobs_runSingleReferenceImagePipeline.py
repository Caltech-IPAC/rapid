'''
RAPID reference-image pipeline


Terminating exit codes:

=============       =========================================
exit_code            Definition
=============       =========================================
     0               Normal termination
    32               Warning
    64               Error (nonspecific)
=============       =========================================


RAPID pipeline output_refimage_file_infobits:

==========  ==========================================================================
infobit     Definition
==========  ==========================================================================
     0      Median background level is > x DN/s
     1      Robust pixel noise (RMS) is > x DN/s
     2      Etc.
==========  ==========================================================================
 '''

import os
import ast
import configparser
import boto3
from botocore.exceptions import ClientError
import numpy as np
from datetime import datetime, timezone
from dateutil import tz
import time
from contextlib import chdir           # Requires Python 3.11+
import shutil

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import pipeline.referenceImageSubs as rfis


swname = "awsBatchSubmitJobs_runSingleReferenceImagePipeline.py"
swvers = "1.0"

rapid_sw = "/code"
cfg_path = rapid_sw + "/cdf"


print("swname =", swname)
print("swvers =", swvers)

aws_batch_job_id = os.getenv('AWS_BATCH_JOB_ID')
print("aws_batch_job_id =", aws_batch_job_id)


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


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


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    start_time_benchmark = time.time()
    start_time_benchmark_at_start = start_time_benchmark


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
    upload_to_s3_bucket = config_input.getboolean('JOB_PARAMS','upload_to_s3_bucket')

    override_upload_to_s3_bucket = os.getenv('DONOTUPLOADPRODUCTS')

    if override_upload_to_s3_bucket is not None:
        upload_to_s3_bucket = False

    product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
    refimage_psf_s3_bucket_dir = config_input['JOB_PARAMS']['refimage_psf_s3_bucket_dir']
    refimage_psf_filename = config_input['JOB_PARAMS']['refimage_psf_filename']

    product_s3_bucket = product_s3_bucket_base

    product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

    sca_gain = float(config_input['INSTRUMENT']['sca_gain'])
    sca_readout_noise = float(config_input['INSTRUMENT']['sca_readout_noise'])


    rfid_str = config_input['REF_IMAGE']['rfid']

    if rfid_str == 'None':
        rfid = None
    else:
        rfid = int(rfid_str)

    field_refimage = int(config_input['REF_IMAGE']['field'])
    fid_refimage = int(config_input['REF_IMAGE']['fid'])
    filter_refimage = config_input['REF_IMAGE']['filter']
    # The following is passed through from the master config file (ppid=12)
    # by the reference-image-pipeline launcher script.
    ppid_refimage = int(config_input['REF_IMAGE']['ppid'])
    max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])
    naxis1_refimage = int(config_input['REF_IMAGE']['naxis1'])
    naxis2_refimage = int(config_input['REF_IMAGE']['naxis2'])
    cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1'])
    cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2'])
    crota2_refimage = float(config_input['REF_IMAGE']['crota2'])

    overlapping_fields_refimage = ast.literal_eval(config_input['REF_IMAGE']['overlapping_fields'])

    print(f"overlapping_fields_refimage = {overlapping_fields_refimage}")

    awaicgen_dict = config_input['AWAICGEN']

    sextractor_refimage_dict = config_input['SEXTRACTOR_REFIMAGE']
    fake_sources_dict = config_input['FAKE_SOURCES']
    psfcat_refimage_dict = config_input['PSFCAT_REFIMAGE']

    print("max_n_images_to_coadd =", max_n_images_to_coadd)

    inject_fake_sources_flag = ast.literal_eval(fake_sources_dict['inject_fake_sources_flag'])

    saturation_level_refimage = float(sextractor_refimage_dict["sextractor_SATUR_LEVEL".lower()])


    # Download reference-image PSF from S3 bucket.

    refimage_psf_filename = refimage_psf_filename.replace("FID",str(fid_refimage))
    s3_full_name_refimage_psf = "s3://" + job_info_s3_bucket + "/" + refimage_psf_s3_bucket_dir + "/" + refimage_psf_filename
    filename_refimage_psf,subdirs_refimage_psf,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_refimage_psf)

    if not downloaded_from_bucket:
        print(f"*** Error: Could not download reference-image PSF from S3 bucket ({s3_full_name_refimage_psf}); skipping...\n")
        exit(64)


    print("s3_full_name_refimage_psf = ",s3_full_name_refimage_psf)
    print("filename_refimage_psf = ",filename_refimage_psf)


    # Optionally read in CSV file containing inputs for generating reference image.

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

        filename_sex_refimage_catalog = awaicgen_output_mosaic_image_file.replace("image.fits","refimsexcat.txt")
        refimage_sex_catalog_s3_bucket_object_name =  subdirs + "/" + filename_sex_refimage_catalog

        print("Downloading s3://{}/{} into {}...".\
            format(product_s3_bucket,refimage_sex_catalog_s3_bucket_object_name,filename_sex_refimage_catalog))

        response = s3_client.download_file(product_s3_bucket,refimage_sex_catalog_s3_bucket_object_name,filename_sex_refimage_catalog)

        print("response =",response)


    else:


        # Generate reference image.
        # Isolate generation in subdir and move products up to normal work dir.

        refimage_generation_subdir = "refimage_generation"

        os.mkdir(refimage_generation_subdir)

        with chdir(refimage_generation_subdir):

            generateReferenceImage_return_list = rfis.generateReferenceImage(s3_client,
                                                                             job_info_s3_bucket,
                                                                             input_images_csv_file_s3_bucket_object_name,
                                                                             input_images_csv_filename,
                                                                             jid,
                                                                             job_proc_date,
                                                                             awaicgen_dict,
                                                                             max_n_images_to_coadd,
                                                                             sca_gain,
                                                                             sca_readout_noise,
                                                                             product_s3_bucket,
                                                                             upload_to_s3_bucket,
                                                                             inject_fake_sources_flag,
                                                                             fake_sources_dict,
                                                                             rapid_sw,
                                                                             overlapping_fields_refimage)

        infobits_refimage = generateReferenceImage_return_list[0]
        checksum_refimage = generateReferenceImage_return_list[1]
        awaicgen_output_mosaic_image_file = generateReferenceImage_return_list[2]
        awaicgen_output_mosaic_cov_map_file = generateReferenceImage_return_list[3]
        awaicgen_output_mosaic_uncert_image_file = generateReferenceImage_return_list[4]
        awaicgen_output_mosaic_image_s3_bucket_object_name = generateReferenceImage_return_list[5]
        awaicgen_output_mosaic_cov_map_s3_bucket_object_name = generateReferenceImage_return_list[6]
        awaicgen_output_mosaic_uncert_image_s3_bucket_object_name = generateReferenceImage_return_list[7]
        nframes = generateReferenceImage_return_list[8]
        refimage_input_filenames = generateReferenceImage_return_list[9]
        jdstart = generateReferenceImage_return_list[10]
        jdend = generateReferenceImage_return_list[11]
        zprefimg = generateReferenceImage_return_list[12]
        total_refimage_exptime = generateReferenceImage_return_list[13]

        shutil.move(f"{refimage_generation_subdir}/{awaicgen_output_mosaic_image_file}", awaicgen_output_mosaic_image_file)
        print(f"Moved {refimage_generation_subdir}/{awaicgen_output_mosaic_image_file} up to {awaicgen_output_mosaic_image_file}")

        shutil.move(f"{refimage_generation_subdir}/{awaicgen_output_mosaic_cov_map_file}", awaicgen_output_mosaic_cov_map_file)
        print(f"Moved {refimage_generation_subdir}/{awaicgen_output_mosaic_cov_map_file} up to {awaicgen_output_mosaic_cov_map_file}")

        shutil.move(f"{refimage_generation_subdir}/{awaicgen_output_mosaic_uncert_image_file}", awaicgen_output_mosaic_uncert_image_file)
        print(f"Moved {refimage_generation_subdir}/{awaicgen_output_mosaic_uncert_image_file} up to {awaicgen_output_mosaic_uncert_image_file}")


        # Compute cov5percent metric for reference image.

        cov5percent = rfis.compute_cov5percent(awaicgen_output_mosaic_cov_map_file)


        # Compute required statistics for reference-image depth-of-coverage image and uncertainty image.

        n_sigma = 3.0
        hdu_index = 0

        stats_covimage = util.fits_data_statistics_with_clipping(awaicgen_output_mosaic_cov_map_file,\
                                                                 n_sigma,\
                                                                 hdu_index)
        medncov = stats_covimage["gmed"]


        stats_uncimage = util.fits_data_statistics_with_clipping(awaicgen_output_mosaic_uncert_image_file,\
                                                                 n_sigma,\
                                                                 hdu_index)
        medpixunc = stats_uncimage["gmed"]


        # Generate SExtractor reference-image catalog.

        refimgsexcat_return_list = rfis.generateSExtractorReferenceImageCatalog(s3_client,
                                                                                product_s3_bucket,
                                                                                jid,
                                                                                job_proc_date,
                                                                                awaicgen_output_mosaic_image_file,
                                                                                awaicgen_output_mosaic_uncert_image_file,
                                                                                sextractor_refimage_dict,
                                                                                upload_to_s3_bucket)

        checksum_sex_refimage_catalog = refimgsexcat_return_list[0]
        filename_sex_refimage_catalog = refimgsexcat_return_list[1]
        refimage_sex_catalog_s3_bucket_object_name = refimgsexcat_return_list[2]


        # Generate PhotUtils reference-image catalog.

        refimgpsfcat_return_list = rfis.generatePhotUtilsReferenceImageCatalog(s3_client,
                                                                               product_s3_bucket,
                                                                               jid,
                                                                               job_proc_date,
                                                                               awaicgen_output_mosaic_image_file,
                                                                               awaicgen_output_mosaic_uncert_image_file,
                                                                               filename_refimage_psf,
                                                                               psfcat_refimage_dict,
                                                                               upload_to_s3_bucket)


        flag_psf_refimage_catalog = refimgpsfcat_return_list[0]
        checksum_psf_refimage_catalog = refimgpsfcat_return_list[1]
        checksum_psf_finder_refimage_catalog = refimgpsfcat_return_list[2]
        filename_psf_refimage_catalog = refimgpsfcat_return_list[3]
        filename_psf_finder_refimage_catalog = refimgpsfcat_return_list[4]
        refimage_psf_catalog_s3_bucket_object_name = refimgpsfcat_return_list[5]
        refimage_psf_finder_catalog_s3_bucket_object_name = refimgpsfcat_return_list[6]
        refimage_psf_catalog_uploaded_to_bucket = refimgpsfcat_return_list[7]
        refimage_psf_finder_catalog_uploaded_to_bucket = refimgpsfcat_return_list[8]







        # TODO:  The following is incorrect (60 seconds), but used as a stopgap for now:
        # Need to standardize the exposure time of a reference image,
        # and make sure it is properly scaled to the science image
        # prior to difference imaging, and also use it to compute saturation_level_refimage_rate.

        saturation_level_refimage_rate = saturation_level_refimage / 60.0








        n_sigma = 3.0
        hdu_index = 0

        stats_refimage = util.fits_data_statistics_with_clipping(awaicgen_output_mosaic_image_file,\
                                                                 n_sigma,\
                                                                 hdu_index,\
                                                                 saturation_level_refimage_rate)

        avg_refimage = stats_refimage["clippedavg"]
        std_refimage = stats_refimage["clippedstd"]
        cnt_refimage = stats_refimage["nkept"]
        noutliers_refimage = stats_refimage["noutliers"]
        gmed_refimage = stats_refimage["gmed"]
        datascale_refimage = stats_refimage["gsigma"]
        gmin_refimage = stats_refimage["gdatamin"]
        gmax_refimage = stats_refimage["gdatamax"]
        npixsat_refimage = stats_refimage["satcount"]
        npixnan_refimage = stats_refimage["nancount"]


    # Compute additional quantities needed for later.

    sextractor_refimage_paramsfile = cfg_path + "/rapidSexParamsRefImage.inp"
    params_to_get_refimage = ["FWHM_IMAGE"]

    vals_refimage = util.parse_ascii_text_sextractor_catalog(filename_sex_refimage_catalog,
                                                             sextractor_refimage_paramsfile,
                                                             params_to_get_refimage)

    nsexcatsources_refimage = len(vals_refimage)

    vals_fwhm = []
    for val in vals_refimage:
        vals_fwhm.append(float(val[0]))

    np_vals_fwhm = np.array(vals_fwhm)

    fwhm_ref_minpix = np.nanmin(np_vals_fwhm)
    fwhm_ref_maxpix = np.nanmax(np_vals_fwhm)
    fwhm_ref_medpix = np.nanmedian(np_vals_fwhm)


    print("fwhm_ref_medpix,fwhm_ref_minpix,fwhm_ref_maxpix =",fwhm_ref_medpix,fwhm_ref_minpix,fwhm_ref_maxpix)

    fwhm_ref = fwhm_ref_medpix
    if np.isnan(fwhm_ref) or fwhm_ref < 0.0:
        fwhm_ref = 2.0

    print("fwhm_ref =",fwhm_ref)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after downloading or generating reference image =",
        round(end_time_benchmark - start_time_benchmark,3))
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
    product_config['JOB_PARAMS']['job_started'] = str(proc_pt_datetime_started)

    product_config['REF_IMAGE'] = {}
    product_config['REF_IMAGE']['rfid'] = str(rfid)
    product_config['REF_IMAGE']['ppid'] = str(ppid_refimage)


    # The launcher script will not launch a reference-image pipeline instance unless rfid is None.

    if rfid is None:

        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file_checksum'] = checksum_refimage

        mosaic_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name)
        mosaic_cov_map_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name)
        mosaic_uncert_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)
        refimage_sex_catalog_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,refimage_sex_catalog_s3_bucket_object_name)
        refimage_psf_catalog_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,refimage_psf_catalog_s3_bucket_object_name)
        input_images_csv_name_for_download = "s3://{}/{}".format(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name)

        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file'] = mosaic_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_cov_map_file'] = mosaic_cov_map_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_uncert_image_file'] = mosaic_uncert_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_status'] = str(1)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_infobits'] = str(infobits_refimage)

        product_config['REF_IMAGE']['sextractor_refimage_catalog_filename_for_db'] = refimage_sex_catalog_name_for_db_record
        product_config['REF_IMAGE']['sextractor_refimage_catalog_checksum'] = checksum_sex_refimage_catalog
        product_config['REF_IMAGE']['sextractor_refimage_catalog_cattype'] = str(1)     # cattype = 1 in database.
        product_config['REF_IMAGE']['sextractor_refimage_catalog_status'] = str(1)

        product_config['REF_IMAGE']['photutils_refimage_catalog_filename_for_db'] = refimage_psf_catalog_name_for_db_record
        product_config['REF_IMAGE']['photutils_refimage_catalog_checksum'] = checksum_psf_refimage_catalog
        product_config['REF_IMAGE']['photutils_refimage_catalog_cattype'] = str(2)     # cattype = 2 in database.
        product_config['REF_IMAGE']['photutils_refimage_catalog_status'] = str(1)
        product_config['REF_IMAGE']['photutils_refimage_catalog_uploaded_to_bucket'] = str(refimage_psf_catalog_uploaded_to_bucket)

        product_config['REF_IMAGE']['nframes'] = str(nframes)
        product_config['REF_IMAGE']['npixsat'] = str(npixsat_refimage)
        product_config['REF_IMAGE']['npixnan'] = str(npixnan_refimage)
        product_config['REF_IMAGE']['clmean'] = str(avg_refimage)
        product_config['REF_IMAGE']['clstddev'] = str(std_refimage)
        product_config['REF_IMAGE']['clnoutliers'] = str(noutliers_refimage)
        product_config['REF_IMAGE']['gmedian'] = str(gmed_refimage)
        product_config['REF_IMAGE']['datascale'] = str(datascale_refimage)
        product_config['REF_IMAGE']['gmin'] = str(gmin_refimage)
        product_config['REF_IMAGE']['gmax'] = str(gmax_refimage)
        product_config['REF_IMAGE']['cov5percent'] = str(cov5percent)
        product_config['REF_IMAGE']['medncov'] = str(medncov)
        product_config['REF_IMAGE']['medpixunc'] = str(medpixunc)
        product_config['REF_IMAGE']['fwhmmedpix'] = str(fwhm_ref_medpix)
        product_config['REF_IMAGE']['fwhmminpix'] = str(fwhm_ref_minpix)
        product_config['REF_IMAGE']['fwhmmaxpix'] = str(fwhm_ref_maxpix)
        product_config['REF_IMAGE']['nsexcatsources'] = str(nsexcatsources_refimage)
        product_config['REF_IMAGE']['input_images_csv_name_for_download'] = input_images_csv_name_for_download


        # Add informational FITS keywords to header of reference image.

        rfis.addKeywordsToReferenceImageHeader(awaicgen_output_mosaic_image_file,
                                               field_refimage,
                                               fid_refimage,
                                               filter_refimage,
                                               cov5percent,
                                               nframes,
                                               refimage_input_filenames,
                                               jdstart,
                                               jdend,
                                               zprefimg,
                                               total_refimage_exptime)


        # Also, add same informational FITS keywords to header of reference-image uncertainty image.

        rfis.addKeywordsToReferenceImageHeader(awaicgen_output_mosaic_uncert_image_file,
                                               field_refimage,
                                               fid_refimage,
                                               filter_refimage,
                                               cov5percent,
                                               nframes,
                                               refimage_input_filenames,
                                               jdstart,
                                               jdend,
                                               zprefimg,
                                               total_refimage_exptime)


        # Upload reference-image file to S3 bucket.

        if upload_to_s3_bucket:

            uploaded_to_bucket = True

            try:
                response = s3_client.upload_file(awaicgen_output_mosaic_image_file,
                                                 product_s3_bucket,
                                                 awaicgen_output_mosaic_image_s3_bucket_object_name)

                print("response =",response)

            except ClientError as e:
                print("*** Error: Failed to upload {} to s3://{}/{}"\
                    .format(awaicgen_output_mosaic_image_file,product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name))
                uploaded_to_bucket = False

            if uploaded_to_bucket:
                print("Successfully uploaded {} to s3://{}/{}"\
                    .format(awaicgen_output_mosaic_image_file,product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name))


            # Upload reference-image uncertainty file to S3 bucket.

            uploaded_to_bucket = True

            try:
                response = s3_client.upload_file(awaicgen_output_mosaic_uncert_image_file,
                                                 product_s3_bucket,
                                                 awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)

                print("response =",response)

            except ClientError as e:
                print("*** Error: Failed to upload {} to s3://{}/{}"\
                    .format(awaicgen_output_mosaic_uncert_image_file,product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name))
                uploaded_to_bucket = False

            if uploaded_to_bucket:
                print("Successfully uploaded {} to s3://{}/{}"\
                    .format(awaicgen_output_mosaic_uncert_image_file,product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name))


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("Elapsed time in seconds after uploading reference image to S3 product bucket =",round(end_time_benchmark - start_time_benchmark,3))
            start_time_benchmark = end_time_benchmark


    # Get listing of working directory as a diagnostic.

    ls_cmd = ['ls','-ltr']
    exitcode_from_ls = util.execute_command(ls_cmd)


    # Get timestamp job ended in Pacific Time for Jobs database record later.

    datetime_utc_now = datetime.now(timezone.utc)
    proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
    datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
    proc_pt_datetime_ended = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

    print("proc_pt_datetime_ended =",proc_pt_datetime_ended)

    product_config['JOB_PARAMS']['job_ended'] = str(proc_pt_datetime_ended)


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


    # Upload product config file for job, along with associated file(s) if any, to S3 bucket.

    if upload_to_s3_bucket:

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
        print("Elapsed time in seconds after uploading products at pipeline end to S3 product bucket =",
            round(end_time_benchmark - start_time_benchmark,3))
        start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to run one instance of reference-image pipeline =",
        round(end_time_benchmark - start_time_benchmark_at_start,3))


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)


    # AWS Batch job should be successful whenever terminating_exitcode < 64.

    aws_batch_job_exitcode = 0

    if (terminating_exitcode >= 64):
        aws_batch_job_exitcode = terminating_exitcode

    print("aws_batch_job_exitcode =",aws_batch_job_exitcode)

    exit(aws_batch_job_exitcode)
