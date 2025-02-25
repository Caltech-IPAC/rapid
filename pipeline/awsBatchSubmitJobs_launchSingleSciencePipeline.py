import boto3
import os
from astropy.io import fits
import subprocess
import re
import math
import configparser
from datetime import datetime, timezone
from botocore.exceptions import ClientError
from dateutil import tz

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite

swname = "awsBatchSubmitJobs_launchSingleSciencePipeline.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)


# RID of input file, read from environment variable RID.

rid = os.getenv('RID')

if rid is None:

    print("*** Error: Env. var. RID not set; quitting...")
    exit(64)


# Compute processing datetime (UT) and processing date (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')


def utc_to_local(utc_dt):
    """Converts a UTC datetime object to local time."""

    return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)

local_time = utc_to_local(datetime_utc_now)
proc_date = local_time.strftime('%Y%m%d')


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


# Other required inputs.

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)

#print("aws_access_key_id =",aws_access_key_id)
#print("aws_secret_access_key =",aws_secret_access_key)

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

rapid_work = os.getenv('RAPID_WORK')

if rapid_work is None:

    print("*** Error: Env. var. RAPID_WORK not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

verbose = int(config_input['JOB_PARAMS']['verbose'])
debug = int(config_input['JOB_PARAMS']['debug'])
job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']
refimage_psf_s3_bucket_dir = config_input['JOB_PARAMS']['refimage_psf_s3_bucket_dir']
refimage_psf_filename = config_input['JOB_PARAMS']['refimage_psf_filename']

sca_gain = float(config_input['INSTRUMENT']['sca_gain'])

ppid = int(config_input['SCI_IMAGE']['ppid'])
saturation_level_sciimage = config_input['SCI_IMAGE']['saturation_level']

ppid_refimage = int(config_input['REF_IMAGE']['ppid_refimage'])
max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])
naxis1_refimage = int(config_input['REF_IMAGE']['naxis1_refimage'])
naxis2_refimage = int(config_input['REF_IMAGE']['naxis2_refimage'])
cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1_refimage'])
cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2_refimage'])
crota2_refimage = float(config_input['REF_IMAGE']['crota2_refimage'])


# Set up AWS Batch.

client = boto3.client('batch')


# Define job definition.  Use AWS Batch Console to set this up once.

job_definition = config_input['AWS_BATCH']['job_definition']


# Define job queue.  Use AWS Batch Console to set this up once.

job_queue = config_input['AWS_BATCH']['job_queue']


# Define job name.

job_name_base = config_input['AWS_BATCH']['job_name_base']


# Get the awaicgen parameters.  Some of these parameters will be overwritten by this script.
# Do not convert to numerical types, since these will just be passed through (except for those
# overwritten by this script).

awaicgen_dict = {}

awaicgen_dict["awaicgen_input_images_list_file"] = config_input['AWAICGEN']['awaicgen_input_images_list_file']
awaicgen_dict["awaicgen_input_uncert_list_file"] = config_input['AWAICGEN']['awaicgen_input_uncert_list_file']
awaicgen_dict["awaicgen_mosaic_size_x"] = config_input['AWAICGEN']['awaicgen_mosaic_size_x']
awaicgen_dict["awaicgen_mosaic_size_y"] = config_input['AWAICGEN']['awaicgen_mosaic_size_y']
awaicgen_dict["awaicgen_RA_center"] = config_input['AWAICGEN']['awaicgen_RA_center']
awaicgen_dict["awaicgen_Dec_center"] = config_input['AWAICGEN']['awaicgen_Dec_center']
awaicgen_dict["awaicgen_mosaic_rotation"] = config_input['AWAICGEN']['awaicgen_mosaic_rotation']
awaicgen_dict["awaicgen_pixelscale_factor"] = config_input['AWAICGEN']['awaicgen_pixelscale_factor']
awaicgen_dict["awaicgen_pixelscale_absolute"] = config_input['AWAICGEN']['awaicgen_pixelscale_absolute']
awaicgen_dict["awaicgen_mos_cellsize_factor"] = config_input['AWAICGEN']['awaicgen_mos_cellsize_factor']
awaicgen_dict["awaicgen_drizzle_factor"] = config_input['AWAICGEN']['awaicgen_drizzle_factor']
awaicgen_dict["awaicgen_inv_var_weight_flag"] = config_input['AWAICGEN']['awaicgen_inv_var_weight_flag']
awaicgen_dict["awaicgen_pixelflux_scale_flag"] = config_input['AWAICGEN']['awaicgen_pixelflux_scale_flag']
awaicgen_dict["awaicgen_simple_coadd_flag"] = config_input['AWAICGEN']['awaicgen_simple_coadd_flag']
awaicgen_dict["awaicgen_num_threads"] = config_input['AWAICGEN']['awaicgen_num_threads']
awaicgen_dict["awaicgen_unc_sigfigs_retained"] = config_input['AWAICGEN']['awaicgen_unc_sigfigs_retained']
awaicgen_dict["awaicgen_output_mosaic_image_file"] = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']
awaicgen_dict["awaicgen_output_mosaic_cov_map_file"] = config_input['AWAICGEN']['awaicgen_output_mosaic_cov_map_file']
awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"] = config_input['AWAICGEN']['awaicgen_output_mosaic_uncert_image_file']
awaicgen_dict["awaicgen_debug"] = config_input['AWAICGEN']['awaicgen_debug']
awaicgen_dict["awaicgen_verbose"] = config_input['AWAICGEN']['awaicgen_verbose']


# Update the awaicgen dictionary for quantities that do not vary with sky location.

pixel_scale = math.fabs(cdelt1_refimage)
awaicgen_mosaic_size_x = pixel_scale * float(naxis1_refimage)
awaicgen_mosaic_size_y = pixel_scale * float(naxis2_refimage)

awaicgen_dict["awaicgen_mosaic_size_x"] = str(awaicgen_mosaic_size_x)
awaicgen_dict["awaicgen_mosaic_size_y"] = str(awaicgen_mosaic_size_y)
awaicgen_dict["awaicgen_mosaic_rotation"] = str(crota2_refimage)


# Get the ZOGY parameters.
# Do not convert to numerical types, since these will just be passed through.

zogy_dict = {}

zogy_dict["astrometric_uncert_x"] = config_input['ZOGY']['astrometric_uncert_x']
zogy_dict["astrometric_uncert_y"] = config_input['ZOGY']['astrometric_uncert_y']
zogy_dict["zogy_output_diffimage_file"] = config_input['ZOGY']['zogy_output_diffimage_file']
zogy_dict["post_zogy_keep_diffimg_lower_cov_map_thresh"] = config_input['ZOGY']['post_zogy_keep_diffimg_lower_cov_map_thresh']


# Get the swarp parameters.  Some of these parameters will be overwritten by this script.
# Do not convert to numerical types, since these will just be passed through.

swarp_dict = {}

swarp_dict["swarp_input_image"] = config_input['SWARP']['swarp_input_image']
swarp_dict["swarp_IMAGEOUT_NAME"] = config_input['SWARP']['swarp_IMAGEOUT_NAME']
swarp_dict["swarp_WEIGHTOUT_NAME"] = config_input['SWARP']['swarp_WEIGHTOUT_NAME']
swarp_dict["swarp_HEADER_ONLY"] = config_input['SWARP']['swarp_HEADER_ONLY']
swarp_dict["swarp_HEADER_SUFFIX"] = config_input['SWARP']['swarp_HEADER_SUFFIX']
swarp_dict["swarp_WEIGHT_TYPE"] = config_input['SWARP']['swarp_WEIGHT_TYPE']
swarp_dict["swarp_RESCALE_WEIGHTS"] = config_input['SWARP']['swarp_RESCALE_WEIGHTS']
swarp_dict["swarp_WEIGHT_SUFFIX"] = config_input['SWARP']['swarp_WEIGHT_SUFFIX']
swarp_dict["swarp_WEIGHT_IMAGE"] = config_input['SWARP']['swarp_WEIGHT_IMAGE']
swarp_dict["swarp_WEIGHT_THRESH"] = config_input['SWARP']['swarp_WEIGHT_THRESH']
swarp_dict["swarp_COMBINE"] = config_input['SWARP']['swarp_COMBINE']
swarp_dict["swarp_COMBINE_TYPE"] = config_input['SWARP']['swarp_COMBINE_TYPE']
swarp_dict["swarp_CLIP_AMPFRAC"] = config_input['SWARP']['swarp_CLIP_AMPFRAC']
swarp_dict["swarp_CLIP_SIGMA"] = config_input['SWARP']['swarp_CLIP_SIGMA']
swarp_dict["swarp_CLIP_WRITELOG"] = config_input['SWARP']['swarp_CLIP_WRITELOG']
swarp_dict["swarp_CLIP_LOGNAME"] = config_input['SWARP']['swarp_CLIP_LOGNAME']
swarp_dict["swarp_BLANK_BADPIXELS"] = config_input['SWARP']['swarp_BLANK_BADPIXELS']
swarp_dict["swarp_CELESTIAL_TYPE"] = config_input['SWARP']['swarp_CELESTIAL_TYPE']
swarp_dict["swarp_PROJECTION_TYPE"] = config_input['SWARP']['swarp_PROJECTION_TYPE']
swarp_dict["swarp_PROJECTION_ERR"] = config_input['SWARP']['swarp_PROJECTION_ERR']
swarp_dict["swarp_CENTER_TYPE"] = config_input['SWARP']['swarp_CENTER_TYPE']
swarp_dict["swarp_CENTER"] = config_input['SWARP']['swarp_CENTER']
swarp_dict["swarp_PIXELSCALE_TYPE"] = config_input['SWARP']['swarp_PIXELSCALE_TYPE']
swarp_dict["swarp_PIXEL_SCALE"] = config_input['SWARP']['swarp_PIXEL_SCALE']
swarp_dict["swarp_IMAGE_SIZE"] = config_input['SWARP']['swarp_IMAGE_SIZE']
swarp_dict["swarp_RESAMPLE"] = config_input['SWARP']['swarp_RESAMPLE']
swarp_dict["swarp_RESAMPLE_DIR"] = config_input['SWARP']['swarp_RESAMPLE_DIR']
swarp_dict["swarp_RESAMPLE_SUFFIX"] = config_input['SWARP']['swarp_RESAMPLE_SUFFIX']
swarp_dict["swarp_RESAMPLING_TYPE"] = config_input['SWARP']['swarp_RESAMPLING_TYPE']
swarp_dict["swarp_OVERSAMPLING"] = config_input['SWARP']['swarp_OVERSAMPLING']
swarp_dict["swarp_INTERPOLATE"] = config_input['SWARP']['swarp_INTERPOLATE']
swarp_dict["swarp_FSCALASTRO_TYPE"] = config_input['SWARP']['swarp_FSCALASTRO_TYPE']
swarp_dict["swarp_FSCALE_KEYWORD"] = config_input['SWARP']['swarp_FSCALE_KEYWORD']
swarp_dict["swarp_FSCALE_DEFAULT"] = config_input['SWARP']['swarp_FSCALE_DEFAULT']
swarp_dict["swarp_GAIN_KEYWORD"] = config_input['SWARP']['swarp_GAIN_KEYWORD']
swarp_dict["swarp_GAIN_DEFAULT"] = config_input['SWARP']['swarp_GAIN_DEFAULT']
swarp_dict["swarp_SATLEV_KEYWORD"] = config_input['SWARP']['swarp_SATLEV_KEYWORD']
swarp_dict["swarp_SATLEV_DEFAULT"] = config_input['SWARP']['swarp_SATLEV_DEFAULT']
swarp_dict["swarp_SUBTRACT_BACK"] = config_input['SWARP']['swarp_SUBTRACT_BACK']
swarp_dict["swarp_BACK_TYPE"] = config_input['SWARP']['swarp_BACK_TYPE']
swarp_dict["swarp_BACK_DEFAULT"] = config_input['SWARP']['swarp_BACK_DEFAULT']
swarp_dict["swarp_BACK_SIZE"] = config_input['SWARP']['swarp_BACK_SIZE']
swarp_dict["swarp_BACK_FILTERSIZE"] = config_input['SWARP']['swarp_BACK_FILTERSIZE']
swarp_dict["swarp_BACK_FILTTHRESH"] = config_input['SWARP']['swarp_BACK_FILTTHRESH']
swarp_dict["swarp_VMEM_DIR"] = config_input['SWARP']['swarp_VMEM_DIR']
swarp_dict["swarp_VMEM_MAX"] = config_input['SWARP']['swarp_VMEM_MAX']
swarp_dict["swarp_MEM_MAX"] = config_input['SWARP']['swarp_MEM_MAX']
swarp_dict["swarp_COMBINE_BUFSIZE"] = config_input['SWARP']['swarp_COMBINE_BUFSIZE']
swarp_dict["swarp_DELETE_TMPFILES"] = config_input['SWARP']['swarp_DELETE_TMPFILES']
swarp_dict["swarp_COPY_KEYWORDS"] = config_input['SWARP']['swarp_COPY_KEYWORDS']
swarp_dict["swarp_WRITE_FILEINFO"] = config_input['SWARP']['swarp_WRITE_FILEINFO']
swarp_dict["swarp_WRITE_XML"] = config_input['SWARP']['swarp_WRITE_XML']
swarp_dict["swarp_VERBOSE_TYPE"] = config_input['SWARP']['swarp_VERBOSE_TYPE']
swarp_dict["swarp_NNODES"] = config_input['SWARP']['swarp_NNODES']
swarp_dict["swarp_NODE_INDEX"] = config_input['SWARP']['swarp_NODE_INDEX']
swarp_dict["swarp_NTHREADS"] = config_input['SWARP']['swarp_NTHREADS']
swarp_dict["swarp_NOPENFILES_MAX"] = config_input['SWARP']['swarp_NOPENFILES_MAX']


# Get the sextractor parameters.  Some of these parameters will be overwritten by this script.
# Do not convert to numerical types, since these will just be passed through.

sextractor_diffimage_dict = config_input['SEXTRACTOR_DIFFIMAGE']
sextractor_sciimage_dict = config_input['SEXTRACTOR_SCIIMAGE']

sextractor_refimage_dict = {}
for key in config_input['SEXTRACTOR_REFIMAGE'].keys():
    #print('Input SEXTRACTOR_REFIMAGE: key, value =',key,config_input['SEXTRACTOR_REFIMAGE'][key])
    sextractor_refimage_dict[key] = config_input['SEXTRACTOR_REFIMAGE'][key]

bkgest_dict = config_input['BKGEST']

gainmatch_dict = config_input['GAINMATCH']
psfcat_diffimage_dict = config_input['PSFCAT_DIFFIMAGE']

sextractor_gainmatch_dict = {}
for key in config_input['SEXTRACTOR_GAINMATCH'].keys():
    #print('Input SEXTRACTOR_GAINMATCH: key, value =',key,config_input['SEXTRACTOR_GAINMATCH'][key])
    sextractor_gainmatch_dict[key] = config_input['SEXTRACTOR_GAINMATCH'][key]


def submit_job_to_aws_batch(proc_date,
                            jid,
                            job_info_s3_bucket,
                            job_config_ini_file_filename,
                            job_config_ini_file_s3_bucket_object_name,
                            input_images_csv_filename,
                            input_images_csv_file_s3_bucket_object_name):

    print("proc_date =",proc_date)
    print("jid =",jid)
    print("job_info_s3_bucket =",job_info_s3_bucket)
    print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
    print("input_images_csv_file_s3_bucket_object_name =",input_images_csv_file_s3_bucket_object_name)


    # Submit single job.

    job_name = job_name_base + proc_date + "_jid" + str(jid)

    print("Submitting job to AWS Batch...")

    response = client.submit_job(
        jobName=job_name,
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={
            'environment': [
                {
                    'name': 'JOBPROCDATE',
                    'value': proc_date
                },
                {
                    'name': 'RAPID_JOB_ID',
                    'value': str(jid)
                },
                {
                    'name': 'JOBS3BUCKET',
                    'value': job_info_s3_bucket
                },
                {
                    'name': 'JOBCONFIGFILENAME',
                    'value': job_config_ini_file_filename
                },
                {
                    'name': 'JOBCONFIGOBJNAME',
                    'value': job_config_ini_file_s3_bucket_object_name
                },
                {
                    'name': 'REFIMAGEINPUTSFILENAME',
                    'value': input_images_csv_filename
                },
                {
                    'name': 'REFIMAGEINPUTSOBJNAME',
                    'value': input_images_csv_file_s3_bucket_object_name
                }
            ]
        }
    )

    print("response =",response)


if __name__ == '__main__':


    #
    # Launch a science pipeline for input science image (obtained from environment variable RID above), which
    # entails machine-generating a config file, and, if applicable, a CVS file with reference-image inputs.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    # Query database for associated L2FileMeta record.

    sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = dbh.get_l2filemeta_record(rid)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Query PSFs database table for the best version of PSF, required by ZOGY.

    psfid,s3_full_name_psf = dbh.get_best_psf(sca,fid)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Query database for select columns in L2Files record.

    image_info = dbh.get_info_for_l2file(rid)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    s3_full_name_science_image = image_info[0]
    expid = image_info[1]
    sca = image_info[2]
    field = image_info[3]
    mjdobs = image_info[4]
    exptime = image_info[5]
    infobits = image_info[6]
    status = image_info[7]
    vbest = image_info[8]
    version = image_info[9]

    if vbest == 0:
        print('*** Error: vbest is zero for rid = {}; quitting....'.format(rid))
        exit(64)


    # Get field number (rtid) of sky tile containing center of input science image.

    roman_tessellation_db.get_rtid(ra0,dec0)
    rtid = roman_tessellation_db.rtid

    if rtid != field:
        print("*** Error: rtid (= {}) does not match field (= {}); quitting....".format(rtid,field))
        exit(64)


    # Get sky positions of center and four corners of sky tile.

    roman_tessellation_db.get_center_sky_position(rtid)
    ra0_field = roman_tessellation_db.ra0
    dec0_field = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(rtid)
    ra1_field = roman_tessellation_db.ra1
    dec1_field = roman_tessellation_db.dec1
    ra2_field = roman_tessellation_db.ra2
    dec2_field = roman_tessellation_db.dec2
    ra3_field = roman_tessellation_db.ra3
    dec3_field = roman_tessellation_db.dec3
    ra4_field = roman_tessellation_db.ra4
    dec4_field = roman_tessellation_db.dec4


    # Compute the sky positions of the four corners of the reference.
    # Remember the reference image is centered on the sky tile with zero rotation.

    ra0_refimage = ra0_field
    dec0_refimage = dec0_field

    crpix1_refimage = 0.5 * float(naxis1_refimage) + 0.5
    crpix2_refimage = 0.5 * float(naxis2_refimage) + 0.5
    crval1_refimage = ra0_refimage
    crval2_refimage = dec0_refimage


    # Update the awaicgen dictionary for mosaic center.

    awaicgen_dict["awaicgen_RA_center"] = str(ra0_refimage)
    awaicgen_dict["awaicgen_Dec_center"] = str(dec0_refimage)


    # Integer pixel coordinates are zero-based and centered on pixel.

    x1_refimage = 0.5 - 1.0     # We want the extreme outer image edges.
    y1_refimage = 0.5 - 1.0

    x2_refimage = naxis1_refimage + 0.5 - 1.0
    y2_refimage = 0.5 - 1.0

    x3_refimage = naxis1_refimage + 0.5 - 1.0
    y3_refimage = naxis2_refimage + 0.5 - 1.0

    x4_refimage = 0.5 - 1.0
    y4_refimage = naxis2_refimage + 0.5 - 1.0


    ra1_refimage,dec1_refimage = util.tan_proj(x1_refimage,y1_refimage,
                                               crpix1_refimage,crpix2_refimage,
                                               crval1_refimage,crval2_refimage,
                                               cdelt1_refimage,cdelt2_refimage,
                                               crota2_refimage)

    ra2_refimage,dec2_refimage = util.tan_proj(x2_refimage,y2_refimage,
                                               crpix1_refimage,crpix2_refimage,
                                               crval1_refimage,crval2_refimage,
                                               cdelt1_refimage,cdelt2_refimage,
                                               crota2_refimage)

    ra3_refimage,dec3_refimage = util.tan_proj(x3_refimage,y3_refimage,
                                               crpix1_refimage,crpix2_refimage,
                                               crval1_refimage,crval2_refimage,
                                               cdelt1_refimage,cdelt2_refimage,
                                               crota2_refimage)

    ra4_refimage,dec4_refimage = util.tan_proj(x4_refimage,y4_refimage,
                                               crpix1_refimage,crpix2_refimage,
                                               crval1_refimage,crval2_refimage,
                                               cdelt1_refimage,cdelt2_refimage,
                                               crota2_refimage)


    # Insert or update record in Jobs database table and return job ID.

    jid = dbh.start_job(ppid,fid,expid,field,sca,rid)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query RefImages database table for the best version of reference image
    # (which is usually the latest unless a prior version is locked).
    # A reference image depends only on pipeline number, field, filter, and version.
    # If a reference image does not exist, then aggregate all the inputs required to make one.

    rfid,filename_refimage,infobits_refimage = dbh.get_best_reference_image(ppid_refimage,field,fid)

    if dbh.exit_code >= 64 and dbh.exit_code != 67:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)

    if rfid is not None:

        print("*** Message: Reference image found in database for rfid={}".format(rfid))
        input_images_csv_filename = "None"
        input_images_csv_file = "None"
        input_images_csv_file_s3_bucket_object_name = "None"
        n_images_to_coadd = -1

    else:
        filename_refimage = "None"
        input_images_csv_filename = "input_images_for_refimage_jid"+ str(jid) + ".csv"
        input_images_csv_file = rapid_work + "/" + input_images_csv_filename
        input_images_csv_file_s3_bucket_object_name = proc_date + "/" + input_images_csv_filename


        # Query L2FileMeta database table for RID,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,
        # and distance from tile center (degrees) for all science images that
        # overlap the sky tile associated with the input science image and its filter.
        # Use radius_of_initial_cone_search = 0.18 degrees.
        # Returned list is ordered by distance from tile center.
        #
        # NOTE: The returned list includes all versions, and regardless of status (the query of the
        # L2FileMeta table does NOT join with the L2Files table, in order to optimize query speed).

        radius_of_initial_cone_search = 0.18
        overlapping_images = dbh.get_overlapping_l2files(rid,
                                                         fid,
                                                         mjdobs,
                                                         ra0_field,dec0_field,
                                                         ra1_field,dec1_field,
                                                         ra2_field,dec2_field,
                                                         ra3_field,dec3_field,
                                                         ra4_field,dec4_field,
                                                         radius_of_initial_cone_search)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)

        n_images_to_coadd = len(overlapping_images)


        # For each overlapping image, query L2Files database table for
        # filename, sca, mjdobs, exptime, infobits, and status.

        f = open(input_images_csv_file, "w")

        for image_meta in overlapping_images:
            rid_refimage_input = image_meta[0]
            ra0_refimage_input = image_meta[1]
            dec0_refimage_input = image_meta[2]
            ra1_refimage_input = image_meta[3]
            dec1_refimage_input = image_meta[4]
            ra2_refimage_input = image_meta[5]
            dec2_refimage_input = image_meta[6]
            ra3_refimage_input = image_meta[7]
            dec3_refimage_input = image_meta[8]
            ra4_refimage_input = image_meta[9]
            dec4_refimage_input = image_meta[10]
            cone_search_dist_refimage_input = image_meta[11]

            image_info = dbh.get_info_for_l2file(rid_refimage_input)

            if dbh.exit_code >= 64:
                exit(dbh.exit_code)

            filename_refimage_input = image_info[0]
            expid_refimage_input= image_info[1]
            sca_refimage_input= image_info[2]
            field_refimage_input= image_info[3]
            mjdobs_refimage_input= image_info[4]
            exptime_refimage_input= image_info[5]
            infobits_refimage_input= image_info[6]
            status_refimage_input= image_info[7]
            vbest_refimage_input= image_info[8]
            version_refimage_input= image_info[9]

            if status_refimage_input== 0: continue             # Omit if status = 0
            if vbest_refimage_input== 0: continue              # Omit if not the best version

            csv_record = str(rid_refimage_input) + "," +\
                         str(ra0_refimage_input) + "," +\
                         str(dec0_refimage_input) + "," +\
                         str(ra1_refimage_input) + "," +\
                         str(dec1_refimage_input) + "," +\
                         str(ra2_refimage_input) + "," +\
                         str(dec2_refimage_input) + "," +\
                         str(ra3_refimage_input) + "," +\
                         str(dec3_refimage_input) + "," +\
                         str(ra4_refimage_input) + "," +\
                         str(dec4_refimage_input) + "," +\
                         str(filename_refimage_input) + "," +\
                         str(expid_refimage_input) + "," +\
                         str(sca_refimage_input) + "," +\
                         str(field_refimage_input) + "," +\
                         str(mjdobs_refimage_input) + "," +\
                         str(exptime_refimage_input) + "," +\
                         str(infobits_refimage_input) + "," +\
                         str(status_refimage_input) + "," +\
                         str(vbest_refimage_input) + "," +\
                         str(version_refimage_input)

            f.write(csv_record + "\n")

        f.close()


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Populate config-file dictionary for job.

    job_config_ini_file_filename = job_config_filename_base + str(jid) + ".ini"
    job_config_ini_file = rapid_work + "/" + job_config_ini_file_filename
    job_info_s3_bucket = job_info_s3_bucket_base
    job_config_ini_file_s3_bucket_object_name = proc_date + "/" + job_config_ini_file_filename

    job_config = configparser.ConfigParser()

    job_config['JOB_PARAMS'] = {'debug': str(debug),

                         'swname': swname,

                         'swvers': swvers,
                         'jid': str(jid)}

    job_config['JOB_PARAMS']['job_info_s3_bucket_base'] = job_info_s3_bucket_base
    job_config['JOB_PARAMS']['product_s3_bucket_base'] = product_s3_bucket_base
    job_config['JOB_PARAMS']['product_config_filename_base'] = product_config_filename_base
    job_config['JOB_PARAMS']['verbose'] = str(verbose)
    job_config['JOB_PARAMS']['refimage_psf_s3_bucket_dir'] = refimage_psf_s3_bucket_dir
    job_config['JOB_PARAMS']['refimage_psf_filename'] = refimage_psf_filename

    job_config['INSTRUMENT'] = {}

    job_config['INSTRUMENT']['sca_gain'] = str(sca_gain)

    job_config['SCI_IMAGE'] = {}

    job_config['SCI_IMAGE']['ppid'] = str(ppid)
    job_config['SCI_IMAGE']['saturation_level'] = str(saturation_level_sciimage)
    job_config['SCI_IMAGE']['rid'] = str(rid)
    job_config['SCI_IMAGE']['sca'] = str(sca)
    job_config['SCI_IMAGE']['fid'] = str(fid)

    job_config['SCI_IMAGE']['s3_full_name_science_image'] = s3_full_name_science_image
    job_config['SCI_IMAGE']['expid'] = str(expid)
    job_config['SCI_IMAGE']['field'] = str(field)
    job_config['SCI_IMAGE']['mjdobs'] = str(mjdobs)
    job_config['SCI_IMAGE']['exptime'] = str(exptime)
    job_config['SCI_IMAGE']['infobits'] = str(infobits)
    job_config['SCI_IMAGE']['status'] = str(status)

    job_config['SCI_IMAGE']['ra0'] = str(ra0)
    job_config['SCI_IMAGE']['dec0'] = str(dec0)
    job_config['SCI_IMAGE']['ra1'] = str(ra1)
    job_config['SCI_IMAGE']['dec1'] = str(dec1)
    job_config['SCI_IMAGE']['ra2'] = str(ra2)
    job_config['SCI_IMAGE']['dec2'] = str(dec2)
    job_config['SCI_IMAGE']['ra3'] = str(ra3)
    job_config['SCI_IMAGE']['dec3'] = str(dec3)
    job_config['SCI_IMAGE']['ra4'] = str(ra4)
    job_config['SCI_IMAGE']['dec4'] = str(dec4)

    job_config['SKY_TILE'] = {}

    job_config['SKY_TILE']['rtid'] = str(rtid)

    job_config['SKY_TILE']['ra0'] = str(ra0_field)
    job_config['SKY_TILE']['dec0'] = str(dec0_field)
    job_config['SKY_TILE']['ra1'] = str(ra1_field)
    job_config['SKY_TILE']['dec1'] = str(dec1_field)
    job_config['SKY_TILE']['ra2'] = str(ra2_field)
    job_config['SKY_TILE']['dec2'] = str(dec2_field)
    job_config['SKY_TILE']['ra3'] = str(ra3_field)
    job_config['SKY_TILE']['dec3'] = str(dec3_field)
    job_config['SKY_TILE']['ra4'] = str(ra4_field)
    job_config['SKY_TILE']['dec4'] = str(dec4_field)

    job_config['REF_IMAGE'] = {}

    job_config['REF_IMAGE']['ppid'] = str(ppid_refimage)
    job_config['REF_IMAGE']['max_n_images_to_coadd'] = str(max_n_images_to_coadd)
    job_config['REF_IMAGE']['n_images_to_coadd'] = str(n_images_to_coadd)
    job_config['REF_IMAGE']['rfid'] = str(rfid)
    job_config['REF_IMAGE']['filename'] = filename_refimage
    job_config['REF_IMAGE']['infobits'] = str(infobits_refimage)
    job_config['REF_IMAGE']['input_images_csv_file'] = input_images_csv_file
    job_config['REF_IMAGE']['naxis1'] = str(naxis1_refimage)
    job_config['REF_IMAGE']['naxis2'] = str(naxis2_refimage)
    job_config['REF_IMAGE']['cdelt1'] = str(cdelt1_refimage)
    job_config['REF_IMAGE']['cdelt2'] = str(cdelt2_refimage)
    job_config['REF_IMAGE']['crota2'] = str(crota2_refimage)
    job_config['REF_IMAGE']['ra0'] = str(ra0_refimage)
    job_config['REF_IMAGE']['dec0'] = str(dec0_refimage)
    job_config['REF_IMAGE']['ra1'] = str(ra1_refimage)
    job_config['REF_IMAGE']['dec1'] = str(dec1_refimage)
    job_config['REF_IMAGE']['ra2'] = str(ra2_refimage)
    job_config['REF_IMAGE']['dec2'] = str(dec2_refimage)
    job_config['REF_IMAGE']['ra3'] = str(ra3_refimage)
    job_config['REF_IMAGE']['dec3'] = str(dec3_refimage)
    job_config['REF_IMAGE']['ra4'] = str(ra4_refimage)
    job_config['REF_IMAGE']['dec4'] = str(dec4_refimage)

    zogy_dict["psfid"] = str(psfid)
    zogy_dict["s3_full_name_psf"] = s3_full_name_psf

    job_config['ZOGY'] = zogy_dict

    job_config['AWAICGEN'] = awaicgen_dict
    job_config['SWARP'] = swarp_dict
    job_config['SEXTRACTOR_DIFFIMAGE'] = sextractor_diffimage_dict
    job_config['SEXTRACTOR_SCIIMAGE'] = sextractor_sciimage_dict

    job_config['SEXTRACTOR_REFIMAGE'] = {}
    for key in sextractor_refimage_dict.keys():
        #print('Output SEXTRACTOR_REFIMAGE: key, value =',key,sextractor_refimage_dict[key])
        job_config['SEXTRACTOR_REFIMAGE'][key] = sextractor_refimage_dict[key]

    job_config['BKGEST'] = bkgest_dict

    job_config['GAINMATCH'] = gainmatch_dict
    job_config['PSFCAT_DIFFIMAGE'] = psfcat_diffimage_dict

    job_config['SEXTRACTOR_GAINMATCH'] = {}
    for key in sextractor_gainmatch_dict.keys():
        #print('Output SEXTRACTOR_GAINMATCH: key, value =',key,sextractor_gainmatch_dict[key])
        job_config['SEXTRACTOR_GAINMATCH'][key] = sextractor_gainmatch_dict[key]


    # Write output config file for job.

    with open(job_config_ini_file, 'w') as job_configfile:

        job_configfile.write("#" + "\n")
        job_configfile.write("# s3://" + job_info_s3_bucket + "/" + job_config_ini_file_s3_bucket_object_name + "\n")
        job_configfile.write("#" + "\n")
        job_configfile.write("# " + proc_utc_datetime + "\n")
        job_configfile.write("#" + "\n")
        job_configfile.write("# Machine-generated by " + swname + "\n")
        job_configfile.write("#" + "\n")
        job_configfile.write("\n")

        job_config.write(job_configfile)


    # Upload output config file for job, along with associated file(s) if any, to S3 bucket.

    s3_client = boto3.client('s3')

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(job_config_ini_file,
                                         job_info_s3_bucket,
                                         job_config_ini_file_s3_bucket_object_name)
    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(job_config_ini_file,job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name))
        uploaded_to_bucket = False

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(job_config_ini_file,job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name))

    if rfid is None:

        uploaded_to_bucket = True

        try:
            response = s3_client.upload_file(input_images_csv_file,
                                            job_info_s3_bucket,
                                            input_images_csv_file_s3_bucket_object_name)
        except ClientError as e:
            print("*** Error: Failed to upload {} to s3://{}/{}"\
                .format(input_images_csv_file,job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name))
            uploaded_to_bucket = False

        if uploaded_to_bucket:
            print("Successfully uploaded {} to s3://{}/{}"\
                .format(input_images_csv_file,job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name))


    submit_job_to_aws_batch(proc_date,
                            jid,
                            job_info_s3_bucket,
                            job_config_ini_file_filename,
                            job_config_ini_file_s3_bucket_object_name,
                            input_images_csv_filename,
                            input_images_csv_file_s3_bucket_object_name)
