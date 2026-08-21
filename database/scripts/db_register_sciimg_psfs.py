import os
import boto3
import configparser
import re
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "db_register_sciimg_psfs.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)

debug = True


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


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


#################
# Main program.
#################

if __name__ == '__main__':

    s3_client = boto3.client('s3')

    s3_subdir = 'psfs'

    s3_url = f"s3://{job_info_s3_bucket_base}/{s3_subdir}"


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    ls_cmd = f"aws s3 ls {s3_url}/ | grep sciimage"
    exitcode_from_ls,code_to_execute_stdout = util.execute_command_in_shell(ls_cmd,print_output=False)
    lines = code_to_execute_stdout.splitlines()

    i = 0
    for line in lines:

        cols = line.split()

        filename = cols[3]
        print(filename)


        # Download PSF.

        s3_bucket_object_name = s3_subdir + '/' + filename

        print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket_base,s3_bucket_object_name,filename))

        response = s3_client.download_file(job_info_s3_bucket_base,s3_bucket_object_name,filename)

        print("response =",response)


        # Compute MD5 checksum of reference image.

        print("Computing checksum of ",filename)
        checksum = db.compute_checksum(filename)


        # Set fid and parse filename to get SCA.

        fid = 8

        string_match = re.match(r"sciimage_psf_f146_sca(.+?).fits", filename)

        try:
            sca = int(string_match.group(1))
            print(f"sca = {sca}")

        except:
            print("*** Error: Could not parse filename for SCA; quitting...")
            exit(64)


        # Insert records in PSFs database table.

        add_psf(fid,sca,status,filename,checksum)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)

        psfid = dbh.psfid
        version_psf = dbh.version

        print("psfid =",psfid)
        print("version_psf =",version_psf)


        # Finalize record in PSFs database table (in order to set vbest = 1 for current record).

        update_psf(psfid,filename,checksum,status,version)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to register PSFs database records =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
