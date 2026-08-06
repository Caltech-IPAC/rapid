import boto3
import os
from astropy.io import fits
import re

from pipeline.runtime.process import run_tool

swname = "awsBatchSubmitJobs_CompressTroxelFitsFiles.py"
swvers = "1.0"

subdir_input = "new"
subdir_output = "new-lite"



# Input FILTERSTRING, such as 'J129' (needs to be uppercase as in the *.fits.gz filenames).

filterstring = os.getenv('FILTERSTRING')

if filterstring is None:

    print("*** Error: Env. var. FILTERSTRING not set; quitting...")
    exit(64)

filter_substring_for_dir = filterstring.lower()

bucket_name_input = 'sims-sn-' + filter_substring_for_dir
bucket_name_output = 'sims-sn-' + filter_substring_for_dir +'-lite'

# Credentials come from boto3's default chain (job role, instance role,
# or SSO) — no explicit key pair needed or read here.


# Set up AWS Batch.

client = boto3.client('batch')


# Define job definition. Bare name, not an account/region-qualified ARN
# (boto3 accepts either) — portable across environments. Override via
# env var for a non-default job definition; falls back to the smoke-test
# name until this sims tool gets its own job definition. Use AWS Batch
# Console to set this up once.

job_definition = os.getenv('COMPRESS_JOB_DEFINITION', 'Fetch_and_run')


# Define job queue. Same bare-name/env-var-override approach as above.

job_queue = os.getenv('COMPRESS_JOB_QUEUE', 'rapid-queue')


# Define job name.

job_name_base = "rapid_compress_job"



def submit_jobs():

    run_tool(['mkdir', subdir_input])

    run_tool(['mkdir', subdir_output])

    s3 = boto3.resource('s3')


    # Parse input files in input S3 bucket.

    my_bucket_input = s3.Bucket(bucket_name_input)

    files_input = {}

    for my_bucket_input_object in my_bucket_input.objects.all():

        #print(my_bucket_input_object.key)

        gzfname_input = my_bucket_input_object.key

        filename_match = re.match(r"(.+)/(.+\.fits\.gz)",gzfname_input)

        try:
            subdir_only = filename_match.group(1)
            only_gzfname_input = filename_match.group(2)
            #print("-----0-----> subdir_only =",subdir_only)
            #print("-----1-----> only_gzfname_input =",only_gzfname_input)

        except:
            #print("-----2-----> No match in",gzfname_input)
            continue

        if subdir_only in files_input.keys():
            files_input[subdir_only].append(only_gzfname_input)
        else:
            files_input[subdir_only] = [only_gzfname_input]


    # Loop over subdirs and launch one AWS Batch job per subdir.

    njobs = 0

    for subdir_only in files_input.keys():

        print("subdir_only =",subdir_only)


        # Submit single job for 18 SCA_NUM input files in input S3 bucket for a given exposure.

        job_name = job_name_base + "_" + filter_substring_for_dir + "_" + subdir_only

        response = client.submit_job(
            jobName=job_name,
            jobQueue=job_queue,
            jobDefinition=job_definition,
            containerOverrides={
                'environment': [
                    {
                        'name': 'INPUTSUBDIR',
                        'value': subdir_only
                    },
                    {
                        'name': 'FILTERSTRING',
                        'value': filterstring
                    },
                    {
                        'name': 'BATCH_FILE_S3_URL',
                        'value': 's3://sims-sn-j129-lite/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.sh'
                    },
                    {
                        'name': 'BATCH_FILE_TYPE',
                        'value': 'script'
                    }
                ]
            }
        )

        print("response =",response)


        # Increment number of jobs.

        njobs += 1

        # Comment out the two following lines for the full run.
        #if njobs > 4:
        #    exit(0)


if __name__ == '__main__':
    submit_jobs()
