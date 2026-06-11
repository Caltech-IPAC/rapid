'''
Input L2 ASDF file.
Correct gWCS.
Inject fake variable sources.
Output L2 ASDF file in a different S3 bucket.

Requires the following for correction to gWCS:

export CRDS_PATH=$HOME/crds_cache
export CRDS_SERVER_URL=https://roman-crds.stsci.edu
'''

import os
import boto3
import re
import subprocess
import numpy as np
import configparser
import asdf
import roman_datamodels as rdm
from romancal.assign_wcs import AssignWcsStep
from astropy.coordinates import SkyCoord
import astropy.units as u
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import modules.fake_src.rapid_l2_injections as fksrc
import database.modules.utils.roman_tessellation_db as sqlite


# Define code name and version.

swname = "inject_fake_sources_into_l2_asdf_files.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

python_cmd = '/usr/bin/python3.11'
generate_injection_catalog_code = '/code/modules/fake_src/generateInjectionCatalogForField.py'

debug = 1

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Define input and output S3 buckets.

bucket_name_input = "stpubdata/roman/nexus/soc_simulations/r00340/l2"
bucket_name_output = "socsims-fakesrc-asdf-20260610"


# Create S3-client and S3-resource objects.

s3_client = boto3.client('s3')
s3_resource = boto3.resource('s3')


# Determine number of vCPUs to use in parallel.

num_cores_str = os.getenv('NUMCORES')

if num_cores_str is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores_str)


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
job_info_s3_bucket = config_input['JOB_PARAMS']['job_info_s3_bucket_base']

fake_sources_dict = config_input['FAKE_SOURCES']


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(asdf_files,index_thread):


    '''
    Convert a single ASDF file into a FITS file.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    n_asdf_files = len(asdf_files)

    print("index_thread,n_asdf_files =",index_thread,n_asdf_files)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}\n")


    # Loop over input ASDF files.

    for index_asdf_file in range(n_asdf_files):

        index_core = index_asdf_file % num_cores
        if index_thread != index_core:
            continue

        input_asdf_file = asdf_files[index_asdf_file]

        fh.write(f"i,input_asdf_file = {i},{input_asdf_file}\n")

        if ".asdf" not in input_asdf_file:
            continue


        # Download file from input S3 bucket to local machine.

        s3_object_input_asdf_file = "s3://" + bucket_name_input + "/" + input_asdf_file
        download_cmd = ['aws','s3','cp',s3_object_input_asdf_file,input_asdf_file]
        exitcode_from_download_cmd = util.execute_command(download_cmd)


        # Create output FITS filename for working directory.

        output_asdf_file = input_asdf_file.replace(".asdf","_lite.asdf")


        # Correct gWCS.  Inject fake variable sources.  Output local L2 ASDF file.

        correct_gwcs_inject_fake_variable_sources_output_asdf_file(
            fh,
            input_asdf_file,
            output_asdf_file
            )


        # Gzip the output ASDF file.

        gunzip_cmd = ['gzip', output_asdf_file]
        exitcode_from_gunzip = util.execute_command(gunzip_cmd)


        # Upload gzipped file to output S3 bucket.

        gzipped_output_asdf_file = output_asdf_file + ".gz"

        s3_object_name = gzipped_output_asdf_file

        filenames = [gzipped_output_asdf_file]

        objectnames = [s3_object_name]

        util.upload_files_to_s3_bucket(s3_client,bucket_name_output,filenames,objectnames)


        # Clean up work directory.

        rm_cmd = ['rm','-f',input_asdf_file]
        exitcode_from_rm = util.execute_command(rm_cmd)

        rm_cmd = ['rm','-f',gzipped_output_asdf_file]
        exitcode_from_rm = util.execute_command(rm_cmd)


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to convert ASDF file to FITS file = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over asdf_files.

        fh.write(f"Loop end over asdf_files: index_asdf_file,input_asdf_file = {index_asdf_file},{input_asdf_file}\n")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(asdf_files_list,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,asdf_files_list,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")


#-------------------------------------------------------------------------------------------------------------
# Methods for handling ASDF-to-FITS conversion.
#-------------------------------------------------------------------------------------------------------------

def execute_command_in_shell(bash_command,fname_out=None):

    '''
    Execute a batch command (a string, not a list; can be multiple bash commands connected with &&).
    '''

    print("execute_command: bash_command =",bash_command)


    # Execute code_to_execute.  Note that STDERR and STDOUT are merged into the same data stream.
    # AWS Batch runs Python 3.9.  According to https://docs.python.org/3.9/library/subprocess.html#subprocess.run,
    # if you wish to capture and combine both streams into one, use stdout=PIPE and stderr=STDOUT instead of capture_output.
    # capture_output=False is the default.

    code_to_execute_object = subprocess.run(bash_command,shell=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    returncode = code_to_execute_object.returncode
    print("returncode =",returncode)

    code_to_execute_stdout = code_to_execute_object.stdout
    #print("code_to_execute_stdout =\n",code_to_execute_stdout)

    if fname_out is not None:

        try:
            fh = open(fname_out, 'w', encoding="utf-8")
            fh.write(code_to_execute_stdout)
            fh.close()
        except:
            print(f"*** Warning from method execute_command: Could not open output file {fname_out}; quitting...")

    code_to_execute_stderr = code_to_execute_object.stderr
    #print("code_to_execute_stderr (should be empty since STDERR is combined with STDOUT) =\n",code_to_execute_stderr)

    return returncode,code_to_execute_stdout


def correct_gwcs_inject_fake_variable_sources_output_asdf_file(fh, input_asdf_path, output_asdf_path):

    fh.write(f"Reading {input_asdf_path}...\n")
    original_dm = rdm.open(input_asdf_path)


    # Modify dm.meta.wcs to have correct WCS

    dm = AssignWcsStep.call(original_dm)


    # ------------------------------------------------------------------ #
    # Science array                                                        #
    # ------------------------------------------------------------------ #
    sci_data = np.array(dm.data)          # shape (ny, nx) or (nints, ny, nx)
    hdu_ext_label = "SCI_ORIG"
    image_data_64 = sci_data.astype(np.float64)
    shape = sci_data.shape

    # ------------------------------------------------------------------ #
    # WCS                                                                  #
    # ------------------------------------------------------------------ #
    gwcs_obj   = dm.meta.wcs              # gwcs.WCS instance


    # Compute center of ASDF image.  Image pixel coordinates must be zero-based.

    x = 2043.5
    y = 2043.5

    # Transform pixel -> sky using gwcs
    sky = gwcs_obj.pixel_to_world(x, y)
    if isinstance(sky, SkyCoord):
        ra = sky.ra.deg
        dec = sky.dec.deg
        fh.write(f"===asdf===>x,y,ra,dec = {x},{y},{ra},{dec}\n")
    else:
        # Some gwcs objects return (lon, lat) arrays directly
        ra, dec = np.asarray(sky[0]), np.asarray(sky[1])
        fh.write(f"x,y,ra,dec = {x},{y},{ra},{dec}\n")


    # Compute field.

    roman_tessellation_db.get_rtid(ra,dec)
    field = roman_tessellation_db.rtid


    # Compute all fields that overlap the science image.

    neighboring_rtids = roman_tessellation_db.get_all_neighboring_rtids(field)

    sciimg_overlapping_rtids = [field]
    for neighboring_rtid in neighboring_rtids:
        sciimg_overlapping_rtids.append(neighboring_rtid)


    # Define injection catalog files and download injection catalogs from S3 bucket.

    file_content = ""
    for overlapping_field in sciimg_overlapping_rtids:
        injection_catalog_filename = f"injection_catalog_rtid{overlapping_field}.json"
        s3_full_name_injection_catalog = f"s3://{job_info_s3_bucket}/injection_catalogs/{injection_catalog_filename}"
        injection_catalog_filename,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_injection_catalog)
        fh.write(f"s3_full_name_injection_catalog = {s3_full_name_injection_catalog}\n")
        fh.write(f"injection_catalog_filename = {injection_catalog_filename}\n")
        if downloaded_from_bucket:
            file_content += f"{injection_catalog_filename}\n"
        else:


            # Launch script to generate injection catalog for field.

            fh.write(f"*** Warning: Injection catalog is missing ({injection_catalog_filename}); generating catalog...\n")

            generate_injection_catalog_cmd = [python_cmd,
                                              generate_injection_catalog_code,
                                              str(overlapping_field)]

            exitcode_from_generate_injection_catalog_cmd = util.execute_command(generate_injection_catalog_cmd)


            # Upload fake-source injection catalog to product S3 bucket.

            s3_object_name_injection_catalog = "injection_catalogs/" + injection_catalog_filename

            util.upload_files_to_s3_bucket(s3_client,job_info_s3_bucket,[injection_catalog_filename],[s3_object_name_injection_catalog])


    # Write injection-catalog-list file.

    injection_catalog_list_filename = f"injection_catalog_list_sciimg.csv"

    with open(injection_catalog_list_filename, 'w') as f:
        f.write(file_content)


    # Run fake-source injections code.

    sci_ext = fake_sources_dict['sci_ext']
    num_injections = fake_sources_dict['num_injections']
    injection_mag_min = fake_sources_dict['mag_min']
    injection_mag_max = fake_sources_dict['mag_max']

    python_cmd = '/usr/bin/python3.11'
    fake_sources_code = rapid_sw + '/modules/fake_src/rapid_l2_injections.py'

    fake_sources_cmd = [python_cmd,
                        fake_sources_code,
                        input_asdf_path,
                        injection_catalog_list_filename,
                        output_asdf_path,
                        '--fix-wcs']

    exitcode_from_fake_sources = util.execute_command(fake_sources_cmd)

    fh.write(f"exitcode_from_fake_sources={exitcode_from_fake_sources}\n")

    return


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':

    do_not_overwrite = True

    # Parse FITS files in output S3 bucket.

    my_bucket_output = s3_resource.Bucket(bucket_name_output)

    output_asdf_files = []

    for my_bucket_output_object in my_bucket_output.objects.all():

        fname_output = str(my_bucket_output_object.key)

        #print(f"fname_output = {fname_output}")

        output_asdf_files.append(fname_output)


    # Parse desired ASDF files in input S3 bucket.

    input_asdf_files = []

    cp_cmd = f"aws s3 ls s3://{bucket_name_input}/ | grep cal.asdf"
    exitcode_from_cp,code_to_execute_stdout = execute_command_in_shell(cp_cmd)
    lines = code_to_execute_stdout.splitlines()

    i = 0
    for line in lines:

        #print(line)

        input_file_metadata = line.strip().split()

        if "cal.asdf" in input_file_metadata[3]:

            input_asdf_file = input_file_metadata[3]


            # Special logic.
            #if "r0034001001001001001_" not in input_asdf_file:
            #if "r0034001001001001001_0003_wfi06_f062_cal" not in input_asdf_file:
            #    continue


            print(f"input_asdf_file = {input_asdf_file}")

            output_asdf_file = input_asdf_file.replace(".asdf","_lite.asdf.gz")

            if do_not_overwrite and output_asdf_file in output_asdf_files:

                print(f"{output_asdf_file} exists in output S3 bucket; skipping...")
                continue

            input_asdf_files.append(input_asdf_file)

        i += 1

        #if i > 1:
        #    break

    print(f"Total number of socsims = {i}")


    #########################################################################################
    # Execute parallel tasks.  The execution is done for input ASDF files in parallel,
    # with the number of parallel threads equal to the number of cores on the job-launcher machine.
    #########################################################################################

    if num_cores > 1:
        execute_parallel_processes(input_asdf_files,num_cores)
    else:
        thread_index = 0
        run_single_core_job(input_asdf_files,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to convert ASDF files to FITS files =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    exit(0)


