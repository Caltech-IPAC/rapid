import sys
import os
import time
import signal
import configparser
import boto3
import re

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db

swname = "registerCompletedJobInDB.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


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

verbose = int(config_input['DEFAULT']['verbose'])
debug = int(config_input['DEFAULT']['debug'])
job_info_s3_bucket_base = config_input['DEFAULT']['job_info_s3_bucket_base']
job_logs_s3_bucket_base = config_input['DEFAULT']['job_logs_s3_bucket_base']
product_s3_bucket_base = config_input['DEFAULT']['product_s3_bucket_base']





# Set signal hander.

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGQUIT, signal_handler)


# Open loop.

s3_resource = boto3.resource('s3')
s3_client = boto3.client('s3')

exitcode = 0

i = 0

while True:


    # Examine log files for given processing date.


    my_bucket = s3_resource.Bucket(job_logs_s3_bucket_base)

    nfiles = 0
    log_filenames = []
    jids = []

    for my_bucket_object in my_bucket.objects.all():

        if debug > 0:
            print(my_bucket_object.key)

        input_file = my_bucket_object.key

        filename_match = re.match(r"(\d\d\d\d\d\d\d\d)/(.+jid(\d+)_log\.txt)",input_file)

        try:
            subdir_only = filename_match.group(1)
            filename_only = filename_match.group(2)
            jid = filename_match.group(3)

            if subdir_only == datearg:

                print("-----0-----> subdir_only =",subdir_only)
                print("-----1-----> filename_only =",filename_only)
                print("-----2-----> jid =",jid)

                log_filenames.append(filename_only)
                jids.append(jid)

                nfiles += 1

        except:
            if debug > 0:
                print("-----2-----> No match in",input_file)


    print("End of S3 bucket listing...")
    print("nfiles = ",nfiles)


    # Loop over jobs for a given processing date.

    for jid,log_fname in zip(jids,log_filenames):


        # Download log file from S3 bucket.

        s3_bucket_object_name = datearg + '/' + log_fname

        print("Downloading s3://{}/{} into {}...".format(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname))

        response = s3_client.download_file(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname)

        print("response =",response)


        # Grep log file for aws_batch_job_id and terminating_exitcode.

        job_exitcode = 64
        aws_batch_job_id = 'not_found'
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


        # Open database connection.

        dbh = db.RAPIDDB()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)

        # Update Jobs record.

        dbh.end_job(jid,job_exitcode,aws_batch_job_id)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Close database connection.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)





    # Test code.

    a = 1
    for n in range(10):
        a = a + 1
        print("n,a =",n,a)

    print("Sleeping 10 seconds...")
    time.sleep(10)
    print("Waking up...")

    if i == 3:
        #os.kill(os.getpid(), signal.SIGQUIT)          # Quits gracefully via signal handler upon receiving control-/
        #os.kill(os.getpid(), signal.SIGINT)           # Quits gracefully via signal handler upon receiving control-c
        os.kill(os.getpid(), signal.SIGSTOP)           # Quits unconditionally and immediately.

    if istop == 1:
        print("Terminating gracefully now...")
        exitcode =7
        exit(exitcode)

    i += 1
    print("i = ",i)


# Termination.

exit(exitcode)
