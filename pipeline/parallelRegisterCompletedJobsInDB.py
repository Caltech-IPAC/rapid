"""
This is a separate version of registerCompletedJobsInDB.py that
registers database records in parallel according to number of
available cores on the job-launcher machine.
"""


import sys
import os
import signal
import configparser
import boto3
from botocore.exceptions import ClientError
import re
import healpy as hp
import numpy as np
import csv
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


swname = "parallelRegisterCompletedJobsInDB.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Initialize handler.

istop = 0

def signal_handler(signum, frame):
    print('Caught signal', signum)
    global istop
    istop = 1


# Get processing date of interest from command-line argument.

datearg = (sys.argv)[1]

print("datearg =",datearg)


# Read environment variables.

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


# Other required environment variables.

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

verbose = int(config_input['JOB_PARAMS']['verbose'])
debug = int(config_input['JOB_PARAMS']['debug'])
job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
job_logs_s3_bucket_base = config_input['JOB_PARAMS']['job_logs_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']
awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']
zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


# Set signal hander.

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGQUIT, signal_handler)


# Open database connections for parallel access.

num_cores = os.cpu_count()

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(jids,log_fnames,index_job):

    jid = jids[index_job]
    log_fname = log_fnames[index_job]

    index_core = index_job % num_cores
    dbh = dbh_list[index_core]

    print("\nStart of run_single_core_job: index_job,jid,log_fname =",index_job,jid,log_fname)

    job_exitcode = 64
    aws_batch_job_id = 'not_found'


    # Check whether done file exists in S3 bucket for job, and skip if it exists.
    # This is done by attempting to download the done file.  Regardless the sub
    # always returns the filename and subdirs by parsing the s3_full_name.

    s3_full_name_done_file = "s3://" + product_s3_bucket_base + "/" + datearg + '/jid' + str(jid) + "/" + job_config_filename_base +  str(jid)  + ".done"
    done_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

    if downloaded_from_bucket:
        print("*** Warning: Done file exists ({}); skipping...".format(done_filename))
        return


    # Download log file from S3 bucket.

    s3_bucket_object_name = datearg + '/' + log_fname

    print("Downloading s3://{}/{} into {}...".format(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname))

    response = s3_client.download_file(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname)

    print("response =",response)


    # Download job config file, in order to harvest some of its metadata.

    job_config_ini_filename = job_config_filename_base + str(jid) + ".ini"

    s3_bucket_object_name = datearg + '/' + job_config_ini_filename

    print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename))

    response = s3_client.download_file(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename)

    print("response =",response)


    # Harvest job metadata from job config file

    job_config_input = configparser.ConfigParser()
    job_config_input.read(job_config_ini_filename)

    fid = int(job_config_input['SCI_IMAGE']['fid'])

    rtid = int(job_config_input['SKY_TILE']['rtid'])
    field = rtid
    ra0 = float(job_config_input['SKY_TILE']['ra0'])
    dec0 = float(job_config_input['SKY_TILE']['dec0'])

    print("rtid,ra0,dec0 =",rtid,ra0,dec0)


    # If rfid_str == "None" and not enough inputs to make a
    # reference image, then set job_exitcode = 33 and skip to
    # next job after updating Jobs database record.

    rfid_str = job_config_input['REF_IMAGE']['rfid']
    n_images_to_coadd = int(job_config_input['REF_IMAGE']['n_images_to_coadd'])

    print("rfid_str =",rfid_str)
    print("n_images_to_coadd =",n_images_to_coadd)

    if rfid_str == "None":
        if n_images_to_coadd == 0:
            job_exitcode = 33


    # Grep log file for aws_batch_job_id and terminating_exitcode.

    file = open(log_fname, "r")
    search_string1 = "aws_batch_job_id"
    search_string2 = "terminating_exitcode"

    for line in file:
        if re.search(search_string1, line):
            line = line.rstrip("\n")
            print(line)
            tokens = re.split(r'\s*=\s*',line)
            aws_batch_job_id = tokens[1]
        elif re.search(search_string2, line):
            line = line.rstrip("\n")
            print(line)
            tokens = re.split(r'\s*=\s*',line)
            job_exitcode = tokens[1]


    # Try to download product config file, in order to harvest some of its metadata.
    # This may be unsuccessful if the pipeline failed.

    product_config_ini_filename = product_config_filename_base + str(jid) + ".ini"

    s3_bucket_object_name = datearg + '/' + product_config_ini_filename

    print("Try downloading s3://{}/{} into {}...".format(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename))

    try:
        response = s3_client.download_file(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename)

        print("response =",response)
        downloaded_from_bucket = True


        # Read input parameters from product config *.ini file.

        product_config_input_filename = product_config_ini_filename
        product_config_input = configparser.ConfigParser()
        product_config_input.read(product_config_input_filename)


        # Get the timestamps of when the job started and ended on the AWS Batch machine,
        # which have already been converted to Pacific Time.
        # In the Jobs record of the RAPID pipeline operations database, we will use for
        # job started the time the pipeline instance was launched (which was when the
        # Jobs record was initially inserted).

        job_started = product_config_input['JOB_PARAMS']['job_started']
        job_ended = product_config_input['JOB_PARAMS']['job_ended']

        print("job_started =",job_started)
        print("job_ended =",job_ended)

        string_match = re.match(r"(.+?)T(.+?) PT", job_ended)

        try:
            ended_date = string_match.group(1)
            ended_time = string_match.group(2)
            print("ended = {} {}".format(ended_date,ended_time))

        except:
            print("*** Error: Could not parse proc_pt_datetime_ended; quitting...")
            exit(64)

        ended = ended_date + " " + ended_time

    except ClientError as e:
        print("*** Warning: Failed to download {} from s3://{}/{}"\
            .format(product_config_ini_filename,product_s3_bucket_base,s3_bucket_object_name))
        downloaded_from_bucket = False


    # Get datetime of when last file was written to product bucket.
    # This will be very close to ended in the Jobs database record.
    # It is retrieved here as a sanity check and in case the
    # product config file was not generated by the pipeline instance.

    product_bucket_path = "s3://" + product_s3_bucket_base + "/" + datearg + '/jid' + str(jid) + "/"
    ended_dt,last_file_written_to_bucket = util.get_datetime_of_last_file_written_to_bucket(product_bucket_path)

    ended_str = str(ended_dt)

    print("last_file_written_to_bucket =",last_file_written_to_bucket)
    print("From S3 bucket listing: ended_str =",ended_str)

    if not downloaded_from_bucket:
        ended = ended_str

    print("For Jobs records: ended =",ended)


    # Update Jobs record.

    dbh.end_job(jid,job_exitcode,aws_batch_job_id,ended)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # If job failed, or not enough inputs to generate a reference image when required,
    # then touch done file and skip to next job.

    if int(job_exitcode) == 33:
        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,datearg,jid,s3_client)
        return

    if int(job_exitcode) >= 64:
        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,datearg,jid,s3_client)
        return


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)

    print("hp6,hp9 =",hp6,hp9)


    # Inventory products associated with job.

    product_bucket = s3_resource.Bucket(product_s3_bucket_base)

    job_prefix = datearg + '/jid' + str(jid) + '/'

    print("job_prefix =",job_prefix)

    for product_bucket_object in product_bucket.objects.filter(Prefix=job_prefix):

        print("------==------->",product_bucket_object)


        # Reference image.

        if awaicgen_output_mosaic_image_file in product_bucket_object.key:

            print("Found in reference image in S3 product bucket: {}".format(awaicgen_output_mosaic_image_file))


            # Harvest select product metadata from product config file

            try:
                rfid_str = product_config_input['REF_IMAGE']['rfid']
            except:
                rfid_str = 'Not found'

            if rfid_str == 'None':
                rfid = None

                ppid_refimage = int(product_config_input['REF_IMAGE']['ppid'])

                checksum_refimage = product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_file_checksum']
                filename_refimage = product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_file']
                infobits_refimage = int(product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_infobits'])
                status_refimage = int(product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_status'])


                # Insert record in RefImages database table.

                dbh.add_refimage(ppid_refimage,field,fid,hp6,hp9,infobits_refimage,status_refimage,filename_refimage,checksum_refimage)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)

                rfid = dbh.rfid
                version_refimage = dbh.version

                print("rfid =",rfid)
                print("version_refimage =",version_refimage)


                # Finalize record in RefImages database table, (in order to set vbest = 1 for current record).
                # Filename, checksum, infobits, and status are unchanged.

                dbh.update_refimage(rfid,filename_refimage,checksum_refimage,status_refimage,version_refimage)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)


                # Parse metadata for reference-image catalog.

                sextractor_refimage_catalog_filename_for_db = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_filename_for_db']
                sextractor_refimage_catalog_checksum = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_checksum']
                sextractor_refimage_catalog_cattype = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_cattype']
                sextractor_refimage_catalog_status = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_status']


                # Insert record in RefImCatalogs database table.

                dbh.register_refimcatalog(rfid,
                                          ppid_refimage,
                                          sextractor_refimage_catalog_cattype,
                                          field,
                                          hp6,
                                          hp9,
                                          fid,
                                          sextractor_refimage_catalog_status,
                                          sextractor_refimage_catalog_filename_for_db,
                                          sextractor_refimage_catalog_checksum)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)

                rfcatid = dbh.rfcatid
                svid = dbh.svid

                print("rfcatid =",rfcatid)
                print("svid =",svid)

                nframes = product_config_input['REF_IMAGE']['nframes']
                npixsat = product_config_input['REF_IMAGE']['npixsat']
                npixnan = product_config_input['REF_IMAGE']['npixnan']
                clmean = product_config_input['REF_IMAGE']['clmean']
                clstddev = product_config_input['REF_IMAGE']['clstddev']
                clnoutliers = product_config_input['REF_IMAGE']['clnoutliers']
                gmedian = product_config_input['REF_IMAGE']['gmedian']
                datascale = product_config_input['REF_IMAGE']['datascale']
                gmin = product_config_input['REF_IMAGE']['gmin']
                gmax = product_config_input['REF_IMAGE']['gmax']
                cov5percent = product_config_input['REF_IMAGE']['cov5percent']
                medncov = product_config_input['REF_IMAGE']['medncov']
                medpixunc = product_config_input['REF_IMAGE']['medpixunc']
                fwhmmedpix = product_config_input['REF_IMAGE']['fwhmmedpix']
                fwhmminpix = product_config_input['REF_IMAGE']['fwhmminpix']
                fwhmmaxpix = product_config_input['REF_IMAGE']['fwhmmaxpix']
                nsexcatsources = product_config_input['REF_IMAGE']['nsexcatsources']
                input_images_csv_name_for_download = product_config_input['REF_IMAGE']['input_images_csv_name_for_download']


                # Parse CSV file containing list of reference-image input files and associated data.
                # Compute mininmum and maximum MJDOBS for the RefImMeta database table.
                # Example: input_images_csv_name_for_download = s3://rapid-pipeline-files/20250214/input_images_for_refimage_jid1.csv

                filename_match = re.match(r"s3://(.+?)/(.+)", input_images_csv_name_for_download)

                try:
                    input_images_csv_file_s3_bucket_name = filename_match.group(1)
                    input_images_csv_file_s3_bucket_object_name = filename_match.group(2)
                    print("input_images_csv_file_s3_bucket_name = {}, input_images_csv_file_s3_bucket_object_name = {}".\
                        format(input_images_csv_file_s3_bucket_name,input_images_csv_file_s3_bucket_object_name))

                except:
                    print("*** Error: Could not parse input_images_csv_name_for_download; quitting...")
                    exit(64)

                filename_match2 = re.match(r".+?/(.+)", input_images_csv_file_s3_bucket_object_name)

                try:
                    input_images_csv_filename = filename_match2.group(1)
                    print("input_images_csv_filename = {}".format(input_images_csv_filename))

                except:
                    print("*** Error: Could not parse input_images_csv_file_s3_bucket_object_name; quitting...")
                    exit(64)

                print("Downloading s3://{}/{} into {}...".\
                    format(input_images_csv_file_s3_bucket_name,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename))

                response = s3_client.download_file(input_images_csv_file_s3_bucket_name,
                                                   input_images_csv_file_s3_bucket_object_name,
                                                   input_images_csv_filename)

                print("response =",response)

                refimage_input_mjdobs_list = []

                with open(input_images_csv_filename, newline='') as csvfile:

                    refimage_inputs_reader = csv.reader(csvfile, delimiter=',')

                    for row in refimage_inputs_reader:
                        refim_input_rid = row[0]
                        refimage_input_mjdobs = row[15]                                                   # TODO
                        refimage_input_mjdobs_list.append(refimage_input_mjdobs)


                        # Insert record in RefImMeta database table for each input image.

                        dbh.register_refimimage(rfid,refim_input_rid)



                mjdobs_np = np.array(refimage_input_mjdobs_list)
                mjdobs_min = min(mjdobs_np)
                mjdobs_max = max(mjdobs_np)

                print("mjdobs_min =",mjdobs_min)
                print("mjdobs_max =,",mjdobs_max)


                # Insert record in RefImMeta database table.

                dbh.register_refimmeta(rfid,
                                       fid,
                                       field,
                                       hp6,
                                       hp9,
                                       nframes,
                                       mjdobs_min,
                                       mjdobs_max,
                                       npixsat,
                                       npixnan,
                                       clmean,
                                       clstddev,
                                       clnoutliers,
                                       gmedian,
                                       datascale,
                                       gmin,
                                       gmax,
                                       cov5percent,
                                       medncov,
                                       medpixunc,
                                       fwhmmedpix,
                                       fwhmminpix,
                                       fwhmmaxpix,
                                       nsexcatsources)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)



        # Difference image.

        print("===> zogy_output_diffimage_file =",zogy_output_diffimage_file)
        print("===> product_bucket_object.key =",product_bucket_object.key)

        if zogy_output_diffimage_file in product_bucket_object.key:

            print("Found in difference image in S3 product bucket: {}".format(zogy_output_diffimage_file))


            # Harvest select product metadata from product config file

            try:
                zogy_output_diffimage_file_checksum = product_config_input['ZOGY']['zogy_output_diffimage_file_checksum']
            except:
                zogy_output_diffimage_file_checksum == 'Not found'

            if zogy_output_diffimage_file_checksum != 'Not found':

                rid_diffimage = product_config_input['ZOGY']['rid']
                ppid_diffimage = product_config_input['ZOGY']['ppid']


                rfid_diffimage = product_config_input['ZOGY']['rfid']

                if rfid_diffimage == "None":
                    rfid_diffimage = rfid

                ra0_diffimage = product_config_input['ZOGY']['ra0']
                dec0_diffimage = product_config_input['ZOGY']['dec0']
                ra1_diffimage = product_config_input['ZOGY']['ra1']
                dec1_diffimage = product_config_input['ZOGY']['dec1']
                ra2_diffimage = product_config_input['ZOGY']['ra2']
                dec2_diffimage = product_config_input['ZOGY']['dec2']
                ra3_diffimage = product_config_input['ZOGY']['ra3']
                dec3_diffimage = product_config_input['ZOGY']['dec3']
                ra4_diffimage = product_config_input['ZOGY']['ra4']
                dec4_diffimage = product_config_input['ZOGY']['dec4']

                fid_diffimage = product_config_input['ZOGY']['fid']
                sca_diffimage = product_config_input['ZOGY']['sca']
                nsexcatsources_diffimage = product_config_input['ZOGY']['nsexcatsources']
                scalefacref_diffimage = product_config_input['ZOGY']['scalefacref']

                checksum_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file_checksum']
                filename_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file']
                status_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file_status']
                infobits_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file_infobits']

                infobits_refimage = product_config_input['ZOGY']['awaicgen_output_mosaic_image_infobits']


                # Insert record in DiffImages database table.

                dbh.add_diffimage(rid_diffimage,
                                  ppid_diffimage,
                                  rfid_diffimage,
                                  infobits_diffimage,
                                  infobits_refimage,
                                  ra0_diffimage,
                                  dec0_diffimage,
                                  ra1_diffimage,
                                  dec1_diffimage,
                                  ra2_diffimage,
                                  dec2_diffimage,
                                  ra3_diffimage,
                                  dec3_diffimage,
                                  ra4_diffimage,
                                  dec4_diffimage,
                                  status_diffimage,
                                  filename_diffimage,
                                  checksum_diffimage)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)

                pid = dbh.pid
                version_diffimage = dbh.version

                print("pid =",pid)
                print("version_diffimage =",version_diffimage)


                # Finalize record in DiffImages database table, (in order to set vbest = 1 for current record).
                # Filename, checksum, infobits, and status are unchanged.

                dbh.update_diffimage(pid,filename_diffimage,checksum_diffimage,status_diffimage,version_diffimage)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)


                # Insert record in DiffImMeta database table.

                dbh.register_diffimmeta(pid,fid_diffimage,sca_diffimage,field,hp6,hp9,nsexcatsources_diffimage,scalefacref_diffimage)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)


    # Touch done file.  Upload done file to S3 bucket.

    util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,datearg,jid,s3_client)

    print("\End of run_single_core_job: jid, log_fname =",jid,log_fname)


def execute_parallel_processes(jids,log_filenames,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,jids,log_fnames,job_index) for job_index in range(len(jids))]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Open loop.

    s3_resource = boto3.resource('s3')
    s3_client = boto3.client('s3')

    exitcode = 0

    i = 0

    while True:


        # Open database connection.

        dbh = db.RAPIDDB()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Query database for Jobs records that ended on the given processing date and ran normally.

        ppid = 15
        jobs_records = dbh.get_unclosedout_jobs_for_processing_date(ppid,datearg)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)

        njobs = 0
        log_filenames = []
        jids = []

        for jobs_record in jobs_records:

            db_jid = jobs_record[0]
            awsbatchjobid = jobs_record[1]

            print("db_jid =",db_jid)

            log_filename_only = "rapid_pipeline_job_" + datearg + "_jid" + str(db_jid) + "_log.txt"

            print("log_filename_only =",log_filename_only)

            log_filenames.append(log_filename_only)
            jids.append(db_jid)

            njobs += 1


        print("njobs = ",njobs)


        ############################################################################
        # Execute job-closeout tasks for all science-pipeline jobs with jids on a
        # given processing date.  The execution for each job is done in parallel,
        # taking advantage of multiple cores on the job-launcher machine.
        ############################################################################

        execute_parallel_processes(jids,log_filenames)


        # Close database connection.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)







        # Do not iterate, after all.

        break





        # Test code.

        a = 1
        for n in range(10):
            a = a + 1
            print("n,a =",n,a)

        print("Sleeping 30 seconds...")
        time.sleep(30)
        print("Waking up...")

        if i == 3:
            #os.kill(os.getpid(), signal.SIGQUIT)          # Quits gracefully via signal handler upon receiving control-/
            #os.kill(os.getpid(), signal.SIGINT)           # Quits gracefully via signal handler upon receiving control-c
            os.kill(os.getpid(), signal.SIGSTOP)           # Quits unconditionally and immediately.

        if istop == 1:
            print("Terminating gracefully now...")
            exitcode = 7
            exit(exitcode)

        i += 1
        print("i = ",i)


        #
        # End of open loop (but we are not iterating because of break above).
        #


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)

