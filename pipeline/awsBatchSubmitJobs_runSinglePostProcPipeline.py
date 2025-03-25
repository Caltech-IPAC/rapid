"""
The purpose of the post-processing pipeline is to add database IDs
to products already made by the science pipeline.  This is run only
after the products have been registered in the RAPID operations database.
"""


import boto3
import os
import configparser
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


swname = "awsBatchSubmitJobs_runSinglePostProcPipeline.py"
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

job_prefix = job_proc_date + '/jid' + str(jid) + '/'


# Print out basic information for log file.

print("job_proc_date =",job_proc_date)
print("jid =",jid)
print("job_info_s3_bucket =",job_info_s3_bucket)
print("job_config_ini_file_filename =",job_config_ini_file_filename)
print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
print("job_prefix =",job_prefix)







#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Initialize termination exitcode to normal termination.
    # If detected during post-processing, an abnormal termination exitcode will be set.

    terminating_exitcode = 0


    # Get S3 resource.

    s3_resource = boto3.resource('s3')


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


    ppid = int(config_input['SCI_IMAGE']['ppid'])
    rid = int(config_input['SCI_IMAGE']['rid'])
    expid = int(config_input['SCI_IMAGE']['expid'])
    fid = int(config_input['SCI_IMAGE']['fid'])
    field = int(config_input['SCI_IMAGE']['field'])
    sca = int(config_input['SCI_IMAGE']['sca'])

    pid = int(config_input['DIFF_IMAGE']['pid'])
    s3_full_filename_diffimage = config_input['DIFF_IMAGE']['filename']
    infobitssci_diffimage = int(config_input['DIFF_IMAGE']['infobitssci'])

    rfid = int(config_input['REF_IMAGE']['rfid'])
    s3_full_filename_refimage = config_input['REF_IMAGE']['filename']
    infobits_refimage = int(config_input['REF_IMAGE']['infobits'])

    awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']

    zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


    # Inventory products associated with job.

    product_bucket = s3_resource.Bucket(product_s3_bucket_base)

    for product_bucket_object in product_bucket.objects.filter(Prefix=job_prefix):

        print("product_bucket_object.key =",product_bucket_object.key)


        # Reference image.

        if awaicgen_output_mosaic_image_file in product_bucket_object.key:

            print("Found in reference image in S3 product bucket: {}".format(awaicgen_output_mosaic_image_file))


            # Download reference image from S3 bucket.

            print("s3_full_filename_refimage = ",s3_full_filename_refimage)
            s3_full_filename_refimage,subdirs_refimage,downloaded_from_bucket = \
                util.download_file_from_s3_bucket(s3_client,s3_full_filename_refimage)


            # Update FITS header of reference image.

            keywords = ['RFID','RFFILEN','INFOBITS','PPID']
            kwdvals = [str(rfid),
                       s3_full_filename_refimage,
                       str(infobits_refimage),
                       str(ppid)]
            hdu_index = 0
            util.addKeywordsToFITSHeader(awaicgen_output_mosaic_image_file,
                                         keywords,
                                         kwdvals,
                                         hdu_index,
                                         awaicgen_output_mosaic_image_file)


            # Upload reference image to S3 bucket.

            s3_object_name_refimage = job_proc_date + "/jid" + str(jid) + "/" + awaicgen_output_mosaic_image_file
            filenames = [zogy_output_refimage_file]
            objectnames = [s3_object_name_refimage]
            util.upload_files_to_s3_bucket(s3_client,product_s3_bucket_base,filenames,objectnames)


        # Difference image.

        print("===> zogy_output_diffimage_file =",zogy_output_diffimage_file)

        if zogy_output_diffimage_file in product_bucket_object.key:

            print("Found in difference image in S3 product bucket: {}".format(zogy_output_diffimage_file))


            # Download difference image from S3 bucket.

            print("s3_full_filename_diffimage = ",s3_full_filename_diffimage)
            s3_full_filename_diffimage,subdirs_diffimage,downloaded_from_bucket = \
                util.download_file_from_s3_bucket(s3_client,s3_full_filename_diffimage)


            # Update FITS header of difference image.


            keywords = ['PID','DIFFILEN','INFOBITS','PPID','RID','EXPID','FID','FIELD']
            kwdvals = [str(pid),
                       s3_full_filename_diffimage,
                       str(infobitssci_diffimage),
                       str(ppid),
                       str(rid),
                       str(expid),
                       str(fid),
                       str(field)]
            hdu_index = 0
            util.addKeywordsToFITSHeader(zogy_output_diffimage_file,
                                         keywords,
                                         kwdvals,
                                         hdu_index,
                                         zogy_output_diffimage_file)


            # Upload difference image to S3 bucket.

            s3_object_name_diffimage = job_proc_date + "/jid" + str(jid) + "/" + zogy_output_diffimage_file
            filenames = [zogy_output_diffimage_file]
            objectnames = [s3_object_name_diffimage]
            util.upload_files_to_s3_bucket(s3_client,product_s3_bucket_base,filenames,objectnames)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)


