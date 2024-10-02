import boto3
import os
import time
import numpy as np
from astropy.io import fits
import subprocess
from multiprocessing import Process, Queue, current_process

swname = "awsBatchJobLowLevelScript_CompressTroxelFitsFiles.py"
swvers = "1.0"

MULTIPROCESS = True
NUMBER_OF_CPUS = 4

subdir_input = "new"
subdir_output = "new-lite"


# Print out AWS Batch jobId to log file.

aws_batch_job_id = os.getenv('AWS_BATCH_JOB_ID')

print("aws_batch_job_id =",aws_batch_job_id)


# Input FILTERSTRING, such as 'J129' (needs to be uppercase as in the *.fits.gz filenames).

filterstring = os.getenv('FILTERSTRING')

if filterstring is None:

    print("*** Error: Env. var. FILTERSTRING not set; quitting...")
    exit(64)

filter_substring_for_dir = filterstring.lower()

bucket_name_input = 'sims-sn-' + filter_substring_for_dir
bucket_name_output = 'sims-sn-' + filter_substring_for_dir +'-lite'

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

print("aws_access_key_id =",aws_access_key_id)
print("aws_secret_access_key =",aws_secret_access_key)

subdir_only = os.getenv('INPUTSUBDIR')


#
# Function run by worker processes
#

def worker(input, output):
    for func, args in iter(input.get, 'STOP'):
        result = calculate(func, args)
        output.put(result)


#
# Function used to calculate result
#

def calculate(func, args):
    result = func(*args)
    return args,result


def execute_command(cmd,no_check=False):

    max_ntries = 5

    ntries = 0
    while ntries < max_ntries:

        print("ntries = ",ntries)
        print("Executing cmd = ",cmd)

        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in p.stdout.readlines():
            print("--->",line)
            strvalue = line.decode('utf-8').strip()
            print(strvalue)
        retval = p.wait()
        print("retval =",retval)

        if (retval == 0):
            break

        time.sleep(3)
        ntries += 1

    if not no_check:
        if (retval != 0):
            print("*** Error from execute_command; quitting...")
            exit(1)

    return retval


def process_fits_file_in_subdir(file):

    file_to_check = subdir_input + "/" + file

    if not os.path.isfile(file_to_check):

        print("*** Warning: File does not exist ({}); skipping...".format(file_to_check))
        return

    only_gzfname_input = file

    file_splitext = os.path.splitext(only_gzfname_input)
    fname_input = file_splitext[0]
    fname_output = fname_input.replace(".fits","_lite.fits")
    gzfname_output = fname_output + ".gz"




    # Let astropy do the gunzipping and gzipping.

    #fname_input = file
    #fname_output = fname_input.replace(".fits","_lite.fits")




    print("fname_input =",fname_input)
    print("fname_output =",fname_output)

    cmd = "gunzip " + subdir_input + "/" + only_gzfname_input
    retval = execute_command(cmd,no_check=True)

    if (retval != 0):
        print("*** Error: Input file from S3 bucket could not be unzipped ({}); skipping...".format(cmd))
        return(0)

    print("Reducing size of FITS file...")

    hdul_input = fits.open(subdir_input + "/" + fname_input)

    ffis = ["SCI"]

    hdu_list = []

    primary_header = hdul_input[0].header
    hdu_list.append(fits.PrimaryHDU(data=None,header=primary_header))

    for ffi in ffis:
        data = hdul_input[ffi].data
        header = hdul_input[ffi].header

        hdu = fits.ImageHDU(data.astype(np.float32),header)
        hdu_list.append(hdu)

    hdu = fits.HDUList(hdu_list)
    hdu.writeto(subdir_output + "/" + fname_output,overwrite=True,checksum=True)

    cmd = "gzip " + subdir_output + "/" + fname_output
    execute_command(cmd)

    return(0)


def compress_files():

    cmd = "mkdir " + subdir_input
    execute_command(cmd)

    cmd = "mkdir " + subdir_output
    execute_command(cmd)

    s3 = boto3.resource('s3')

    if subdir_only is None:

        print("*** Error: Environment variable INPUTSUBDIR not set; quitting...")
        exit(64)

    else:


        # Construct input filenames.

        files_input = {}

        for i in range(0,18):

            sca_num = i + 1

            only_gzfname_input = 'Roman_TDS_simple_model_' + filterstring + '_' + subdir_only + '_' + str(sca_num) + '.fits.gz'

            print("only_gzfname_input =",only_gzfname_input)

            if subdir_only in files_input.keys():
                files_input[subdir_only].append(only_gzfname_input)
            else:
                files_input[subdir_only] = [only_gzfname_input]


        # Copy 18 files from S3 bucket with one copy command.

        print("subdir_only =",subdir_only)


        # Copy 18 input files from input S3 bucket to local machine.

        cmd = "aws s3 cp --quiet --recursive s3://" + bucket_name_input + "/" + subdir_only + " new"
        execute_command(cmd)

        if MULTIPROCESS:

            TASKS1 = []
            for file in files_input[subdir_only]:
                TASKS1.append((process_fits_file_in_subdir, (file,)))

            # Create queues
            task_queue = Queue()
            done_queue = Queue()

            # Submit tasks
            for task in TASKS1:
                task_queue.put(task)

            # Start worker processes
            for i in range(NUMBER_OF_CPUS):
                Process(target=worker, args=(task_queue, done_queue)).start()

            # Get and print results
            print('Unordered results:')
            for i in range(len(TASKS1)):
                results = done_queue.get()
                print(results[0][0], "---->",results[1])

            # Tell child processes to stop
            for i in range(NUMBER_OF_CPUS):
                task_queue.put('STOP')

        else:

            for file in files_input[subdir_only]:

                    print("file =",file)
                    process_fits_file_in_subdir(file)


        # Copy 18 output files from local machine to output S3 bucket.

        cmd = "aws s3 cp --quiet --recursive new-lite s3://" + bucket_name_output + "/" + subdir_only
        exit_code = execute_command(cmd)

        cmd = "rm -rf " + subdir_input + "/*fits"
        execute_command(cmd)

        cmd = "rm -rf " + subdir_output + "/*fits.gz"
        execute_command(cmd)

        return exit_code

if __name__ == '__main__':
    exit_code = compress_files()
    exit(exit_code)
