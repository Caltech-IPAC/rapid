import boto3
import os
import numpy as np
from astropy.io import fits
import subprocess
import re
from multiprocessing import Process, Queue, current_process

MULTIPROCESS = True
NUMBER_OF_CPUS = 9

subdir_input = "new"
subdir_output = "new-lite"

bucket_name_input = 'sims-sn-h158'
bucket_name_output = 'sims-sn-h158-lite'

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

print("aws_access_key_id =",aws_access_key_id)
print("aws_secret_access_key =",aws_secret_access_key)


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
    print("cmd = ",cmd)
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in p.stdout.readlines():
        print("--->",line)
        strvalue = line.decode('utf-8').strip()
        print(strvalue)
    retval = p.wait()
    print("retval =",retval)

    if not no_check:
        if (retval != 0):
            print("*** Error from execute_command; quitting...")
            exit(1)

    return retval


def process_fits_file_in_subdir(file):

    only_gzfname_input = file

    file_splitext = os.path.splitext(only_gzfname_input)
    fname_input = file_splitext[0]
    fname_output = fname_input.replace(".fits","_lite.fits")
    gzfname_output = fname_output + ".gz"

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


    # Parse output files in output S3 bucket.

    my_bucket_output = s3.Bucket(bucket_name_output)

    files_output = {}

    for my_bucket_output_object in my_bucket_output.objects.all():

        #print(my_bucket_output_object.key)

        gzfname_output = my_bucket_output_object.key

        filename_match = re.match(r"(.+)/(.+\.fits\.gz)",gzfname_output)

        try:
            subdir_only = filename_match.group(1)
            only_gzfname_output = filename_match.group(2)
            #print("-----0-----> subdir_only =",subdir_only)
            #print("-----1-----> only_gzfname_output =",only_gzfname_output)

        except:
            #print("-----2-----> No match in",gzfname_output)
            pass

        if subdir_only in files_output.keys():
            files_output[subdir_only].append(only_gzfname_output)
        else:
            files_output[subdir_only] = [only_gzfname_output]



    # Loop over subdirs and copy 18 files from S3 bucket with one copy command.

    nfiles = 0

    for subdir_only in files_input.keys():

        print("subdir_only =",subdir_only)


        # Check if output directory already exists.

        if subdir_only in files_output.keys():
            dir_to_check = "s3://" + bucket_name_output + "/" + subdir_only
            print("*** Warning: Directory exists in S3 bucket ({}); skipping...".format(dir_to_check))
            continue


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
        execute_command(cmd)

        cmd = "rm -rf " + subdir_input + "/*fits"
        execute_command(cmd)

        cmd = "rm -rf " + subdir_output + "/*fits.gz"
        execute_command(cmd)

        nfiles += 1

        #if nfiles > 30:
        #    exit(0)


if __name__ == '__main__':
    compress_files()
