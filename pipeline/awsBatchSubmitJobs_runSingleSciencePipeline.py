import os
import configparser
import boto3
from botocore.exceptions import ClientError
from astropy.io import fits
from astropy.io import ascii
from astropy.table import Table
import numpy as np
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import pipeline.referenceImageSubs as rfis
import pipeline.differenceImageSubs as dfis

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


swname = "awsBatchSubmitJobs_runSingleSciencePipeline.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)

aws_batch_job_id = os.getenv('AWS_BATCH_JOB_ID')
print("aws_batch_job_id =", aws_batch_job_id)


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
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
    saturation_level_sciimage = float(config_input['SCI_IMAGE']['saturation_level'])
    rid_sciimage = int(config_input['SCI_IMAGE']['rid'])
    sca_sciimage = int(config_input['SCI_IMAGE']['sca'])
    fid_sciimage = int(config_input['SCI_IMAGE']['fid'])
    filter_sciimage = config_input['SCI_IMAGE']['filter']
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
    gainmatch_dict = config_input['GAINMATCH']
    psfcat_diffimage_dict = config_input['PSFCAT_DIFFIMAGE']
    sextractor_gainmatch_dict = config_input['SEXTRACTOR_GAINMATCH']

    print("max_n_images_to_coadd =", max_n_images_to_coadd)

    saturation_level_refimage = float(sextractor_refimage_dict["sextractor_SATUR_LEVEL".lower()])


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

        generateReferenceImage_return_list = rfis.generateReferenceImage(s3_client,
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
        nframes = generateReferenceImage_return_list[8]
        refimage_input_filenames = generateReferenceImage_return_list[9]

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


        # Generate reference-image catalog.

        generateReferenceImageCatalog_return_list = rfis.generateReferenceImageCatalog(s3_client,
                                                                                       product_s3_bucket,
                                                                                       jid,
                                                                                       job_proc_date,
                                                                                       awaicgen_output_mosaic_image_file,
                                                                                       awaicgen_output_mosaic_uncert_image_file,
                                                                                       sextractor_refimage_dict)

        checksum_refimage_catalog = generateReferenceImageCatalog_return_list[0]
        filename_refimage_catalog = generateReferenceImageCatalog_return_list[1]
        refimage_catalog_s3_bucket_object_name = generateReferenceImageCatalog_return_list[2]


        # Compute additional quantities needed for later populating refimmeta table in RAPID operations database.

        sextractor_refimage_paramsfile = "/code/cdf/rapidSexParamsRefImage.inp";
        params_to_get_refimage = ["FWHM_IMAGE"]

        vals_refimage = util.parse_ascii_text_sextrator_catalog(filename_refimage_catalog,
                                                                sextractor_refimage_paramsfile,
                                                                params_to_get_refimage)

        nsexcatsources_refimage = len(vals_refimage)

        vals_fwhm = []
        for val in vals_refimage:
            vals_fwhm.append(float(val[0]))

        np_vals_fwhm = np.array(vals_fwhm)

        fwhmminpix = np.nanmin(np_vals_fwhm)
        fwhmmaxpix = np.nanmax(np_vals_fwhm)
        fwhmmedpix = np.nanmedian(np_vals_fwhm)








        # TODO:  The following is incorrect, but used as a stopgap for now:
        # Need to standardize the exposure time of a reference image,
        # and make sure it is properly scaled to the science image
        # prior to difference imaging, and also use it to compute saturation_level_refimage_rate.

        saturation_level_refimage_rate = saturation_level_refimage / exptime_sciimage








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
    product_config['JOB_PARAMS']['job_started'] = str(proc_pt_datetime_started)


    if rfid is None:

        product_config['REF_IMAGE'] = {}

        product_config['REF_IMAGE']['rfid'] = str(rfid)
        product_config['REF_IMAGE']['ppid'] = str(ppid_sciimage)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file_checksum'] = checksum_refimage

        mosaic_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name)
        mosaic_cov_map_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name)
        mosaic_uncert_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)
        refimage_catalog_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,refimage_catalog_s3_bucket_object_name)
        input_images_csv_name_for_download = "s3://{}/{}".format(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name)

        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file'] = mosaic_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_cov_map_file'] = mosaic_cov_map_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_uncert_image_file'] = mosaic_uncert_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_status'] = str(1)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_infobits'] = str(infobits_refimage)

        product_config['REF_IMAGE']['sextractor_refimage_catalog_filename_for_db'] = refimage_catalog_name_for_db_record
        product_config['REF_IMAGE']['sextractor_refimage_catalog_checksum'] = checksum_refimage_catalog
        product_config['REF_IMAGE']['sextractor_refimage_catalog_cattype'] = str(1)
        product_config['REF_IMAGE']['sextractor_refimage_catalog_status'] = str(1)

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
        product_config['REF_IMAGE']['fwhmmedpix'] = str(fwhmmedpix)
        product_config['REF_IMAGE']['fwhmminpix'] = str(fwhmminpix)
        product_config['REF_IMAGE']['fwhmmaxpix'] = str(fwhmmaxpix)
        product_config['REF_IMAGE']['nsexcatsources'] = str(nsexcatsources_refimage)
        product_config['REF_IMAGE']['input_images_csv_name_for_download'] = input_images_csv_name_for_download


        # Add informational FITS keywords to header of reference image.

        rfis.addKeywordsToReferenceImageHeader(awaicgen_output_mosaic_image_file,
                                               field_sciimage,
                                               fid_sciimage,
                                               filter_sciimage,
                                               cov5percent,
                                               nframes,
                                               refimage_input_filenames)


        # Also, add same informational FITS keywords to header of reference-image uncertainty image.

        rfis.addKeywordsToReferenceImageHeader(awaicgen_output_mosaic_uncert_image_file,
                                               field_sciimage,
                                               fid_sciimage,
                                               filter_sciimage,
                                               cov5percent,
                                               nframes,
                                               refimage_input_filenames)


        # Upload reference-image file to S3 bucket.

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


    # Unzip the science image gzipped file.

    gunzip_cmd = ['gunzip', science_image_filename_gz]
    exitcode_from_gunzip = util.execute_command(gunzip_cmd)

    science_image_filename = science_image_filename_gz.replace(".fits.gz",".fits")


    # Compute image statistics for image resizing.

    n_sigma = 3.0
    hdu_index = 1

    stats_sci_img = util.fits_data_statistics_with_clipping(science_image_filename,\
                                                            n_sigma,\
                                                            hdu_index,\
                                                            saturation_level_sciimage)

    avg_sci_img = stats_sci_img["clippedavg"]


    # Reformat the Troxel OpenUniverse simulated image FITS file
    # so that the image data are contained in the PRIMARY header.
    # Compute uncertainty image via simple model (photon noise only).
    # Resize images to 4089x4089 (odd number of pixels on each side).

    reformatted_science_image_filename,\
        reformatted_science_uncert_image_filename =\
        dfis.reformat_troxel_fits_file_and_compute_uncertainty_image_via_simple_model(science_image_filename,sca_gain,avg_sci_img)


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
    hdu_index = 0

    stats_sci_img = util.fits_data_statistics_with_clipping(reformatted_science_image_filename,\
                                                            n_sigma,\
                                                            hdu_index,\
                                                            saturation_level_sciimage)

    avg_sci_img = stats_sci_img["clippedavg"]
    std_sci_img = stats_sci_img["clippedstd"]
    cnt_sci_img = stats_sci_img["nkept"]

    stats_ref_img = util.fits_data_statistics_with_clipping(output_resampled_reference_image,\
                                                            n_sigma,\
                                                            hdu_index,\
                                                            saturation_level_refimage)

    avg_ref_img = stats_ref_img["clippedavg"]
    std_ref_img = stats_ref_img["clippedstd"]
    cnt_ref_img = stats_ref_img["nkept"]

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
                  sci_fits_file_with_pv,
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


    ######################################################################################
    # Gain-match science and reference images by generating SExtractor catalogs for each
    # and then computing scale factor.  To apply, multiply reference iamge by scalefacref.
    ######################################################################################

    scalefac,dxrmsfin,dyrmsfin,dxmedianfin,dymedianfin = dfis.gainMatchScienceAndReferenceImages(s3_client,
                                                                                                 product_s3_bucket,
                                                                                                 jid,
                                                                                                 job_proc_date,
                                                                                                 filename_bkg_subbed_science_image,
                                                                                                 reformatted_science_uncert_image_filename,
                                                                                                 output_resampled_reference_image,
                                                                                                 output_resampled_reference_uncert_image,
                                                                                                 gainmatch_dict,
                                                                                                 sextractor_gainmatch_dict,
                                                                                                 astrometric_uncert_x,
                                                                                                 astrometric_uncert_y)

    print("scalefac,dxrmsfin,dyrmsfin,dxmedianfin,dymedianfin =",scalefac,dxrmsfin,dyrmsfin,dxmedianfin,dymedianfin)

    scalefacref = 1. / scalefac


    # Compute resampled gain-matched reference image.

    output_resampled_gainmatched_reference_image = output_resampled_reference_image.replace(".fits","_gainmatched.fits")
    util.scale_image_data(output_resampled_reference_image,scalefacref,output_resampled_gainmatched_reference_image)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after running gainMatchScienceAndReferenceImages =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Replace NaNs, if any, in ZOGY input images.  Use the same saturation level rate since they are gain-matched.

    saturation_value_rate_sciimage = saturation_level_sciimage / exptime_sciimage
    nan_indices_sciimage = util.replace_nans_with_sat_val_rate(filename_bkg_subbed_science_image,saturation_value_rate_sciimage)
    nan_indices_refimage = util.replace_nans_with_sat_val_rate(output_resampled_gainmatched_reference_image,saturation_value_rate_sciimage)


    #################################################################################################################
    # The image data in science_image_filename and sci_fits_file_with_pv FITS files are the same, only the
    # representation of geometric distortion in the FITS headers are different (sip versus pv).
    #
    # ZOGY only cares about the image data, not what is in the FITS headers.
    # Usage: python py_zogy.py <NewImage> <RefImage> <NewPSF> <RefPSF> <NewSigmaImage> <RefSigmaImage>
    #                    <NewSigmaMode> <RefSigmaMode> <AstUncertX> <AstUncertY> <DiffImage> <DiffPSF> <ScorrImage>
    #
    # Assume top-level directory of rapid git repo is mapped to /code inside Docker container.
    #################################################################################################################


    python_cmd = '/usr/bin/python3.11'
    zogy_code = '/code/modules/zogy/v21Aug2018/py_zogy.py'
    filename_diffimage = 'diffimage.fits'
    filename_diffpsf = 'diffpsf.fits'
    filename_scorrimage = 'scorrimage.fits'

    zogy_cmd = [python_cmd,
                zogy_code,
                filename_bkg_subbed_science_image,
                output_resampled_gainmatched_reference_image,
                filename_psf,
                filename_refimage_psf,
                reformatted_science_uncert_image_filename,
                output_resampled_reference_uncert_image,
                str(std_sci_img),
                str(std_ref_img),
                str(dxrmsfin),
                str(dyrmsfin),
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

    dfis.mask_difference_image_with_resampled_reference_cov_map(filename_diffimage,
                                                                output_resampled_reference_cov_map,
                                                                filename_diffimage_masked,
                                                                post_zogy_keep_diffimg_lower_cov_map_thresh)

    dfis.mask_difference_image_with_resampled_reference_cov_map(filename_scorrimage,
                                                                output_resampled_reference_cov_map,
                                                                filename_scorrimage_masked,
                                                                post_zogy_keep_diffimg_lower_cov_map_thresh)


    # Restore NaNs that were masked prior to executing ZOGY.

    if nan_indices_sciimage:
        util.restore_nans(filename_diffimage_masked,nan_indices_sciimage)

    if nan_indices_refimage:
        util.restore_nans(filename_diffimage_masked,nan_indices_refimage)

    if nan_indices_sciimage:
        util.restore_nans(filename_scorrimage_masked,nan_indices_sciimage)

    if nan_indices_refimage:
        util.restore_nans(filename_scorrimage_masked,nan_indices_refimage)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after masking ZOGY difference image =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Generate diffimage uncertainty image, which will be the weight image for sextractor_WEIGHT_IMAGE.

    filename_diffimage_unc_masked = 'diffimage_uncert_masked.fits'
    dfis.compute_diffimage_uncertainty(sca_gain,
                                       reformatted_science_image_filename,
                                       output_resampled_gainmatched_reference_image,
                                       output_resampled_reference_cov_map,
                                       filename_diffimage_masked,
                                       filename_diffimage_unc_masked)
    filename_weight_image = filename_diffimage_unc_masked
    filename_diffimage_sextractor_catalog = filename_diffimage_masked.replace(".fits",".txt")


    # Compute SExtractor catalog for masked difference image.
    # Execute SExtractor to first detect candidates on Scorr (S/N) match-filter
    # image, then use to perform aperture phot on difference image to generate
    # raw ascii catalog file.

    sextractor_diffimage_paramsfile = "/code/cdf/rapidSexParamsDiffImage.inp";

    sextractor_diffimage_dict["sextractor_detection_image".lower()] = filename_scorrimage_masked
    sextractor_diffimage_dict["sextractor_input_image".lower()] = filename_diffimage_masked
    sextractor_diffimage_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_weight_image
    sextractor_diffimage_dict["sextractor_PARAMETERS_NAME".lower()] = sextractor_diffimage_paramsfile
    sextractor_diffimage_dict["sextractor_FILTER_NAME".lower()] = "/code/cdf/rapidSexDiffImageFilter.conv"
    sextractor_diffimage_dict["sextractor_STARNNW_NAME".lower()] = "/code/cdf/rapidSexDiffImageStarGalaxyClassifier.nnw"
    sextractor_diffimage_dict["sextractor_CATALOG_NAME".lower()] = filename_diffimage_sextractor_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_diffimage_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)

    params_to_get_diffimage = ["XWIN_IMAGE","YWIN_IMAGE","FLUX_APER_6"]

    vals_diffimage = util.parse_ascii_text_sextrator_catalog(filename_diffimage_sextractor_catalog,
                                                             sextractor_diffimage_paramsfile,
                                                             params_to_get_diffimage)

    nsexcatsources_diffimage = len(vals_diffimage)

    print("nsexcatsources_diffimage =",nsexcatsources_diffimage)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after running SExtractor on ZOGY difference image =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Generate PSF-fit catalog for difference image using photutils.  No background subtraction is done.

    n_clip_sigma = float(psfcat_diffimage_dict["n_clip_sigma"])
    n_thresh_sigma = float(psfcat_diffimage_dict["n_thresh_sigma"])

    fwhm = float(psfcat_diffimage_dict["fwhm"])
    fit_shape_str = psfcat_diffimage_dict["fit_shape"]
    fit_shape = tuple(int(x) for x in fit_shape_str.replace("(","").replace(")","").replace(" ", "").split(','))
    aperture_radius = float(psfcat_diffimage_dict["aperture_radius"])


    input_img_filename = psfcat_diffimage_dict["input_img_filename"]
    input_unc_filename = psfcat_diffimage_dict["input_unc_filename"]
    input_psf_filename = psfcat_diffimage_dict["input_psf_filename"]
    output_psfcat_filename = psfcat_diffimage_dict["output_psfcat_filename"]
    output_psfcat_residual_filename = psfcat_diffimage_dict["output_psfcat_residual_filename"]

    psfcat_flag,phot = util.compute_diffimage_psf_catalog(n_clip_sigma,
                                                          n_thresh_sigma,
                                                          fwhm,
                                                          fit_shape,
                                                          aperture_radius,
                                                          input_img_filename,
                                                          input_unc_filename,
                                                          input_psf_filename,
                                                          output_psfcat_residual_filename)

    print("psfcat_flag =",psfcat_flag)

    if psfcat_flag:


        # Output psf-fit catalog is an astropy table with the PSF-fitting results.
        # Output columns are documentated at
        # https://photutils.readthedocs.io/en/latest/api/photutils.psf.PSFPhotometry.html

        try:
            phot['x_init'].info.format = '.4f'
            phot['y_init'].info.format = '.4f'
            phot['flux_init'].info.format = '.6f'
            phot['x_fit'].info.format = '.4f'
            phot['y_fit'].info.format = '.4f'
            phot['flux_fit'].info.format = '.6f'
            phot['x_err'].info.format = '.4f'
            phot['y_err'].info.format = '.4f'
            phot['flux_err'].info.format = '.5f'
            phot['qfit'].info.format = '.4f'
            phot['cfit'].info.format = '.4f'

            print(phot[('id', 'x_fit', 'y_fit', 'flux_fit','x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags')])


            # Write PSF-fit catalog in astropy table to text file.

            print("output_psfcat_filename = ", output_psfcat_filename)

            ascii.write(phot, output_psfcat_filename, overwrite=True)

        except:
            pass


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after generating PSF-fit catalog on ZOGY difference image =",
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
    s3_object_name_output_resampled_gainmatched_reference_image = job_proc_date + "/jid" + str(jid) + "/" + \
                                                                  output_resampled_gainmatched_reference_image
    s3_object_name_output_psfcat_filename = job_proc_date + "/jid" + str(jid) + "/" + output_psfcat_filename
    s3_object_name_output_psfcat_residual_filename = job_proc_date + "/jid" + str(jid) + "/" + output_psfcat_residual_filename

    filenames = [filename_diffimage_masked,
                 filename_diffimage_unc_masked,
                 filename_diffimage_sextractor_catalog,
                 filename_diffpsf,
                 filename_scorrimage_masked,
                 filename_bkg_subbed_science_image,
                 output_resampled_gainmatched_reference_image,
                 output_psfcat_filename,
                 output_psfcat_residual_filename]

    objectnames = [s3_object_name_diffimage,
                   s3_object_name_diffimage_unc,
                   s3_object_name_diffimage_catalog,
                   s3_object_name_diffpsf,
                   s3_object_name_scorrimage,
                   s3_object_name_bkg_subbed_science_image,
                   s3_object_name_output_resampled_gainmatched_reference_image,
                   s3_object_name_output_psfcat_filename,
                   s3_object_name_output_psfcat_residual_filename]

    util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


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

    product_config['ZOGY']['fid'] = str(fid_sciimage)
    product_config['ZOGY']['sca'] = str(sca_sciimage)
    product_config['ZOGY']['nsexcatsources'] = str(nsexcatsources_diffimage)
    product_config['ZOGY']['scalefacref'] = str(scalefacref)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after uploading main products to S3 bucket =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    #################################################################################################################
    # Optionally run SFFT to generate an alternate difference image and catalog.
    # Filenames for the SExtractor segmented maps are provided, and, if they do not exist, they will be generated.
    # Output files (constructed by the script, but not provided as input:
    #    sfftdiffimage_masked.fits
    #    sfftsoln.fits
    #################################################################################################################

    run_sfft = False

    # Always leave as True, and can only be reset to False if and only if SFFT runs and fails.
    run_sfft_was_successful = True

    if run_sfft:

        # Cannot run under python3.11 because scikit-learn fails to install.
        python_cmd = '/usr/bin/python3'
        sfft_code = '/code/modules/sfft/sfft_rapid.py'
        filename_scifile = filename_bkg_subbed_science_image
        filename_reffile = output_resampled_gainmatched_reference_image
        filename_scisegm = 'sfftscisegm.fits'
        filename_refsegm = 'sfftrefsegm.fits'

        crossconv_flag = True

        if crossconv_flag:
            filename_sfftdiffimage = 'sfftdiffimage_cconv_masked.fits'
            filename_sfftsoln = 'sfftsoln_cconv.fits'
        else:
            filename_sfftdiffimage = 'sfftdiffimage_masked.fits'
            filename_sfftsoln = 'sfftsoln.fits'

        filename_dcdiff = 'sfftdiffimage_dconv_masked.fits'

        # A quirk in the software requires prepended "./" to input filenames.
        sfft_cmd = [python_cmd,
                    sfft_code,
                    "./" + filename_scifile,
                    "./" + filename_reffile,
                    filename_scisegm,
                    filename_refsegm]

        if crossconv_flag:
            sfft_cmd.append("--crossconv")
            sfft_cmd.append("--scipsf")
            sfft_cmd.append(filename_psf)
            sfft_cmd.append("--refpsf")
            sfft_cmd.append(filename_refimage_psf)

        exitcode_from_sfft = util.execute_command(sfft_cmd)

        if int(exitcode_from_sfft) != 0:
            run_sfft_was_successful = False


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after running SFFT =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark

        if run_sfft_was_successful:

            # Generate SFFT diffimage uncertainty image, which will be the weight image for sextractor_WEIGHT_IMAGE.

            filename_sfftdiffimage_unc = 'sfftdiffimage_uncert_masked.fits'
            dfis.compute_diffimage_uncertainty(sca_gain,
                                               reformatted_science_image_filename,
                                               output_resampled_gainmatched_reference_image,
                                               output_resampled_reference_cov_map,
                                               filename_sfftdiffimage,
                                               filename_sfftdiffimage_unc)
            filename_weight_image = filename_sfftdiffimage_unc
            filename_sfftdiffimage_sextractor_catalog = filename_sfftdiffimage.replace(".fits",".txt")


            # Compute SExtractor catalog for masked difference image.
            # Execute SExtractor to first detect candidates on Scorr (S/N) match-filter
            # image, then use to perform aperture phot on difference image to generate
            # raw ascii catalog file.

            sextractor_diffimage_paramsfile = "/code/cdf/rapidSexParamsDiffImage.inp";

            sextractor_diffimage_dict["sextractor_detection_image".lower()] = filename_sfftdiffimage
            sextractor_diffimage_dict["sextractor_input_image".lower()] = filename_sfftdiffimage
            sextractor_diffimage_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_weight_image
            sextractor_diffimage_dict["sextractor_PARAMETERS_NAME".lower()] = sextractor_diffimage_paramsfile
            sextractor_diffimage_dict["sextractor_FILTER_NAME".lower()] = "/code/cdf/rapidSexDiffImageFilter.conv"
            sextractor_diffimage_dict["sextractor_STARNNW_NAME".lower()] = "/code/cdf/rapidSexDiffImageStarGalaxyClassifier.nnw"
            sextractor_diffimage_dict["sextractor_CATALOG_NAME".lower()] = filename_sfftdiffimage_sextractor_catalog
            sextractor_cmd = util.build_sextractor_command_line_args(sextractor_diffimage_dict)
            exitcode_from_sextractor = util.execute_command(sextractor_cmd)

            params_to_get_diffimage = ["XWIN_IMAGE","YWIN_IMAGE","FLUX_APER_6"]

            vals_sfftdiffimage = util.parse_ascii_text_sextrator_catalog(filename_sfftdiffimage_sextractor_catalog,
                                                                         sextractor_diffimage_paramsfile,
                                                                         params_to_get_diffimage)

            nsexcatsources_sfftdiffimage = len(vals_sfftdiffimage)

            print("nsexcatsources_sfftdiffimage =",nsexcatsources_sfftdiffimage)


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("Elapsed time in seconds after running SExtractor on SFFT difference image =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


            # Upload SFFT-product FITS files to product S3 bucket.

            product_s3_bucket = product_s3_bucket_base
            s3_object_name_sfftdiffimage = job_proc_date + "/jid" + str(jid) + "/" + filename_sfftdiffimage
            s3_object_name_sfftsoln = job_proc_date + "/jid" + str(jid) + "/" + filename_sfftsoln
            s3_object_name_dcdiff = job_proc_date + "/jid" + str(jid) + "/" + filename_dcdiff
            s3_object_name_sfftdiffimage_unc = job_proc_date + "/jid" + str(jid) + "/" + filename_sfftdiffimage_unc
            s3_object_name_sfftdiffimage_catalog = job_proc_date + "/jid" + str(jid) + "/" + filename_sfftdiffimage_sextractor_catalog

            filenames = [filename_sfftdiffimage,
                         filename_sfftsoln,
                         filename_dcdiff,
                         filename_sfftdiffimage_unc,
                         filename_sfftdiffimage_sextractor_catalog]

            objectnames = [s3_object_name_sfftdiffimage,
                           s3_object_name_sfftsoln,
                           s3_object_name_dcdiff,
                           s3_object_name_sfftdiffimage_unc,
                           s3_object_name_sfftdiffimage_catalog]

            util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("Elapsed time in seconds after uploading SFFT products to S3 bucket =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


    # Get listing of working directory as a diagnostic.

    ls_cmd = ['ls','-ltr']
    exitcode_from_ls = util.execute_command(ls_cmd)


    # Get timestamp job ended in Pacific Time for Jobs database record later.

    datetime_utc_now = datetime.utcnow()
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


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to run one instance of science pipeline =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0
    if not run_sfft_was_successful:
        terminating_exitcode = 4

    print("terminating_exitcode =",terminating_exitcode)


    # AWS Batch job should be successful whenever terminating_exitcode < 64.

    aws_batch_job_exitcode = 0

    if (terminating_exitcode >= 64):
        aws_batch_job_exitcode = terminating_exitcode

    print("aws_batch_job_exitcode =",aws_batch_job_exitcode)

    exit(aws_batch_job_exitcode)
