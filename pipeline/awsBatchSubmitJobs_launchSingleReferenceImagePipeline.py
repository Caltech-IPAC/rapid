'''
Terminating exit codes:

=============       =========================================
exit_code            Definition
=============       =========================================
     0               Normal termination
     7               Reference image already exists, so it will not be remade
    32               Warning
    33               n_images_to_coadd < min_n_images_to_coadd: pipeline not launched
    64               Error
=============       =========================================
'''

import boto3
import os
import ast
import math
import configparser
from botocore.exceptions import ClientError
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite

swname = "awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of RAPID science-pipeline jobs.  Processing date is always in Pacific time zone.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Print out basic information for log file.

print("proc_date =",proc_date)


# Read environment variable FIELD.
# This is the sky tile upon which the reference image is centered.

field_str = os.getenv('FIELD')

if field_str is None:

    print("*** Error: Env. var. FIELD not set; quitting...")
    exit(64)

field = int(field_str)


# Read environment variable FID.
# This is the filter ID of the reference image.

fid_str = os.getenv('FID')

if fid_str is None:

    print("*** Error: Env. var. FID not set; quitting...")
    exit(64)

fid = int(fid_str)


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

dry_run_str = os.getenv('DRYRUN')

if dry_run_str is None:
    dry_run_str = "False"

dry_run = ast.literal_eval(dry_run_str)

print(f"dry_run = {dry_run}")


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

verbose = int(config_input['JOB_PARAMS']['verbose'])
debug = int(config_input['JOB_PARAMS']['debug'])
upload_to_s3_bucket = ast.literal_eval(config_input['JOB_PARAMS']['upload_to_s3_bucket'])
job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']
refimage_psf_s3_bucket_dir = config_input['JOB_PARAMS']['refimage_psf_s3_bucket_dir']
refimage_psf_filename = config_input['JOB_PARAMS']['refimage_psf_filename']

sca_gain = float(config_input['INSTRUMENT']['sca_gain'])
sca_readout_noise = float(config_input['INSTRUMENT']['sca_readout_noise'])

ppid = int(config_input['SCI_IMAGE']['ppid'])

ppid_refimage = int(config_input['REF_IMAGE']['ppid'])
min_n_images_to_coadd = int(config_input['REF_IMAGE']['min_n_images_to_coadd'])
max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])
naxis1_refimage = int(config_input['REF_IMAGE']['naxis1_refimage'])
naxis2_refimage = int(config_input['REF_IMAGE']['naxis2_refimage'])
cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1_refimage'])
cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2_refimage'])
crota2_refimage = float(config_input['REF_IMAGE']['crota2_refimage'])

print("min_n_images_to_coadd =",min_n_images_to_coadd)
print("max_n_images_to_coadd =",max_n_images_to_coadd)


# Set up AWS Batch.

client = boto3.client('batch')


# Define job definition.  Use AWS Batch Console to set this up once.

job_definition = config_input['AWS_BATCH']['refimage_job_definition']


# Define job queue.  Use AWS Batch Console to set this up once.

job_queue = config_input['AWS_BATCH']['job_queue']


# Define job name.

job_name_base = config_input['AWS_BATCH']['refimage_job_name_base']


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
awaicgen_dict["zprefimg"] = config_input['AWAICGEN']['zprefimg']


# Update the awaicgen dictionary for quantities that do not vary with sky location.

pixel_scale = math.fabs(cdelt1_refimage)
awaicgen_mosaic_size_x = pixel_scale * float(naxis1_refimage)
awaicgen_mosaic_size_y = pixel_scale * float(naxis2_refimage)

awaicgen_dict["awaicgen_mosaic_size_x"] = str(awaicgen_mosaic_size_x)
awaicgen_dict["awaicgen_mosaic_size_y"] = str(awaicgen_mosaic_size_y)
awaicgen_dict["awaicgen_mosaic_rotation"] = str(crota2_refimage)


# Get the sextractor parameters.  Some of these parameters will be overwritten by this script.
# Do not convert to numerical types, since these will just be passed through.

sextractor_refimage_dict = {}
for key in config_input['SEXTRACTOR_REFIMAGE'].keys():
    #print('Input SEXTRACTOR_REFIMAGE: key, value =',key,config_input['SEXTRACTOR_REFIMAGE'][key])
    sextractor_refimage_dict[key] = config_input['SEXTRACTOR_REFIMAGE'][key]

psfcat_refimage_dict = config_input['PSFCAT_REFIMAGE']
fake_sources_dict = config_input['FAKE_SOURCES']


#-------------------------------------------------------------------------------------------------------------
# Method to submit a job to AWS Batch.
#-------------------------------------------------------------------------------------------------------------

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

    aws_batch_job_id = response['jobId']


    return aws_batch_job_id


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    #
    # Launch a reference-image pipeline for input FIELD and FID (obtained from environment variables), which
    # entails machine-generating a config file, and, if applicable, a CSV file with reference-image inputs.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for filter name.

    exposure_filter = dbh.get_exposure_filter(fid)

    if dbh.exit_code >= 64:
        print(f"*** Error from dbh.get_exposure_filter (dbh.exit_code = {dbh.exit_code}); quitting...")
        dbh.close()             # Close database connection.
        exit(64)


    # Get sky positions of center and four corners of sky tile.

    rtid = field
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


    # Compute all fields that overlap the reference image.

    overlapping_rtid_records = roman_tessellation_db.get_overlapping_rtids(ra0_refimage,dec0_refimage,
                                                                           ra1_refimage,dec1_refimage,
                                                                           ra2_refimage,dec2_refimage,
                                                                           ra3_refimage,dec3_refimage,
                                                                           ra4_refimage,dec4_refimage)

    refimg_overlapping_rtids = []
    for overlapping_rtid_record in overlapping_rtid_records:
        overlapping_rtid = overlapping_rtid_record[0]
        refimg_overlapping_rtids.append(overlapping_rtid)


    # Query RefImages database table for the best version of reference image
    # (which is usually the latest unless a prior version is locked).
    # A reference image depends only on pipeline number, field, filter, and version.
    # If a reference image does not exist, then aggregate all the inputs required to make one.
    # First, check for reference images made by the dedicated reference-image pipeline (ppid=12).
    # If no reference imag is found, check whether there is one made by the science pipeline (ppid=15).

    rfid = None

    db_refimages_rec_dict = dbh.get_best_reference_image(ppid_refimage,field,fid)
    ppid_existing_refimg = ppid_refimage

    if dbh.exit_code == 7:
        print("No database record from dbh.get_best_reference_image for ppid={} called by {}; continuing with rfid = None...".format(ppid_refimage,swname))

        db_refimages_rec_dict = dbh.get_best_reference_image(ppid,field,fid)
        ppid_existing_refimg = ppid

    if dbh.exit_code == 7:
        print("No database record from dbh.get_best_reference_image for ppid={} called by {}; continuing with rfid = None...".format(ppid,swname))
        ppid_existing_refimg = ppid_refimage
    elif dbh.exit_code >= 64:
        print(f"*** Error from dbh.get_best_reference_image (dbh.exit_code = {dbh.exit_code}); quitting...")
        dbh.close()             # Close database connection.
        exit(64)
    else:
        print(f"ppid_existing_refimg = {ppid_existing_refimg}")
        rfid = db_refimages_rec_dict["rfid"]
        filename_refimage = db_refimages_rec_dict["filename"]
        infobits_refimage = db_refimages_rec_dict["infobits"]


    if rfid is not None:

        print(f"*** Message: Reference image found in database for rfid={rfid}; quitting")
        terminating_exitcode = 7
        dbh.close()                    # Close database connection.
        exit(terminating_exitcode)


    # Insert or update record in Jobs database table and return job ID.

    rid = 'null'
    sca = 'null'
    expid = 'null'

    jid = dbh.start_job(ppid_refimage,fid,expid,field,sca,rid)

    if dbh.exit_code >= 64:
        print(f"*** Error from dbh.start_job (dbh.exit_code = {dbh.exit_code}); quitting...")
        dbh.close()             # Close database connection.
        exit(64)


    # Query L2FileMeta database table for RID,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,field
    # and distance from tile center (degrees) for all best science images in the
    # L2Files database table that overlap the sky tile associated with the input science image
    # and its filter.  Use radius_of_initial_cone_search = 0.18 degrees.
    # Returned list is ordered by distance from tile center.
    #
    # If environment variables STARTREFIMMJDOBS and ENDREFIMMJDOBS are set, these
    # will be included as qualifiers in the following database query (read in by
    # method dbh.get_overlapping_l2files).

    filename_refimage = "None"
    infobits_refimage = "None"
    input_images_csv_filename = "input_images_for_refimage_jid"+ str(jid) + ".csv"
    input_images_csv_file = rapid_work + "/" + input_images_csv_filename
    input_images_csv_file_s3_bucket_object_name = proc_date + "/" + input_images_csv_filename

    radius_of_initial_cone_search = 0.18
    # mjdobs defines the end MJD covered by the database query.
    # MJD of 999999.9 converts to May 11, 4692, at approximately 21:36:00 UTC.
    mjdobs = 999999.9
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
        print(f"*** Error from dbh.get_overlapping_l2files (dbh.exit_code = {dbh.exit_code}); quitting...")
        dbh.close()             # Close database connection.
        exit(64)


    # For each overlapping image, query L2Files database table for
    # filename, sca, mjdobs, exptime, infobits, and status.
    #
    # NOTE: max_n_images_to_coadd is not enforced here, but instead
    # when the RAPID pipeline instance is executed.  TODO?

    n_images_to_coadd = 0

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
        field_from_get_overlapping_l2files = image_meta[11]
        cone_search_dist_refimage_input = image_meta[12]

        image_info = dbh.get_info_for_l2file(rid_refimage_input)

        if dbh.exit_code >= 64:
            print(f"*** Error from dbh.get_info_for_l2file (dbh.exit_code = {dbh.exit_code}); quitting...")
            dbh.close()             # Close database connection.
            f.close()
            exit(64)

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

        if status_refimage_input == 0: continue             # Omit if status = 0
        if vbest_refimage_input == 0: continue              # Omit if not the best version


        # Sanity check:

        if field_refimage_input != field_from_get_overlapping_l2files:
            print(f"*** Error: field_refimage_input ({field_refimage_input}) not equal to " +
                  f"field_from_get_overlapping_l2files ({field_from_get_overlapping_l2files}); quitting...")
            dbh.close()             # Close database connection.
            f.close()
            exit(64)


        # Format CSV record.

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

        n_images_to_coadd += 1

    f.close()

    if n_images_to_coadd < min_n_images_to_coadd:
        print(f"*** Warning: n_images_to_coadd ({n_images_to_coadd}) < min_n_images_to_coadd " +\
              f"({min_n_images_to_coadd}) for rid,field,fid = {rid},{field},{fid}; quitting...")
        terminating_exitcode = 33
        dbh.close()             # Close database connection.
        exit(terminating_exitcode)


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

    job_config['JOB_PARAMS']['upload_to_s3_bucket'] = str(upload_to_s3_bucket)
    job_config['JOB_PARAMS']['job_info_s3_bucket_base'] = job_info_s3_bucket_base
    job_config['JOB_PARAMS']['product_s3_bucket_base'] = product_s3_bucket_base
    job_config['JOB_PARAMS']['product_config_filename_base'] = product_config_filename_base
    job_config['JOB_PARAMS']['verbose'] = str(verbose)
    job_config['JOB_PARAMS']['refimage_psf_s3_bucket_dir'] = refimage_psf_s3_bucket_dir
    job_config['JOB_PARAMS']['refimage_psf_filename'] = refimage_psf_filename

    job_config['INSTRUMENT'] = {}

    job_config['INSTRUMENT']['sca_gain'] = str(sca_gain)
    job_config['INSTRUMENT']['sca_readout_noise'] = str(sca_readout_noise)

    job_config['REF_IMAGE'] = {}

    job_config['REF_IMAGE']['field'] = str(field)
    job_config['REF_IMAGE']['fid'] = str(fid)
    job_config['REF_IMAGE']['filter'] = exposure_filter
    job_config['REF_IMAGE']['ppid'] = str(ppid_refimage)
    job_config['REF_IMAGE']['min_n_images_to_coadd'] = str(min_n_images_to_coadd)
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

    job_config['REF_IMAGE']['overlapping_fields'] = str(refimg_overlapping_rtids)

    job_config['FAKE_SOURCES'] = fake_sources_dict

    job_config['AWAICGEN'] = awaicgen_dict

    job_config['SEXTRACTOR_REFIMAGE'] = {}
    for key in sextractor_refimage_dict.keys():
        #print('Output SEXTRACTOR_REFIMAGE: key, value =',key,sextractor_refimage_dict[key])
        job_config['SEXTRACTOR_REFIMAGE'][key] = sextractor_refimage_dict[key]

    job_config['PSFCAT_REFIMAGE'] = psfcat_refimage_dict


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


    # Upload output config file for job, along with associated file(s) if any, to S3 bucket,
    # regardless of dry-run mode, so that the files can be examined.

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
        dbh.close()             # Close database connection.
        exit(64)

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(job_config_ini_file,job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name))

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(input_images_csv_file,
                                        job_info_s3_bucket,
                                        input_images_csv_file_s3_bucket_object_name)
    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(input_images_csv_file,job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name))
        uploaded_to_bucket = False
        dbh.close()             # Close database connection.
        exit(64)

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(input_images_csv_file,job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name))

    if not dry_run:

        aws_batch_job_id = submit_job_to_aws_batch(proc_date,
                                                   jid,
                                                   job_info_s3_bucket,
                                                   job_config_ini_file_filename,
                                                   job_config_ini_file_s3_bucket_object_name,
                                                   input_images_csv_filename,
                                                   input_images_csv_file_s3_bucket_object_name)


        # Update record in Jobs database table with aws_batch_job_id.

        dbh.update_job_with_aws_batch_job_id(jid,aws_batch_job_id)

        if dbh.exit_code >= 64:
            print(f"*** Error: dbh.update_job_with_aws_batch_job_id returned abnormal exit code (dbh.exit_code = {dbh.exit_code}); continuing...")

    else:

        print(f"*** Message: Skipping launch of single reference-image pipeline (dry_run={dry_run})...")


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        print(f"*** Error: dbh.close returned abnormal exit code (dbh.exit_code = {dbh.exit_code}); continuing...")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch single reference-image pipeline =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
