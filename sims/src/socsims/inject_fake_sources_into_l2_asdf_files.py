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
from pipeline.runtime.process import run_tool, run_shell
from pipeline.runtime.errors import ToolError


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
bucket_name_output = "socsims-fakesrc-asdf-20260709"


# Create S3-client and S3-resource objects.

s3_client = boto3.client('s3')
s3_resource = boto3.resource('s3')


# Need access to distortion model for gWCS correction.

crds_path = os.getenv('CRDS_PATH')

if crds_path is None:
    home_env_var = os.getenv('HOME')
    os.environ['CRDS_PATH'] = f"{home_env_var}/crds_cache"

crds_server_url = os.getenv('CRDS_SERVER_URL')

if crds_server_url is None:
    os.environ['CRDS_SERVER_URL'] = "https://roman-crds.stsci.edu"


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


# AWS credentials come from boto3's default chain (job role, instance
# role, or SSO) — no explicit key pair needed or read here.

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
    Convert a single ASDF file into an ASDF file with injected fake variables.  Returns
    (n_ok,n_failed) counts so that a file which raises partway through cannot be logged
    and then forgotten.
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


    # Per-thread outcome counters.  These are returned to the parent process so
    # that a run which converts nothing due to per-file failures cannot report success.

    n_ok = 0
    n_failed = 0


    # Loop over input ASDF files.

    for index_asdf_file in range(n_asdf_files):

        index_core = index_asdf_file % num_cores
        if index_thread != index_core:
            continue

        input_asdf_file = asdf_files[index_asdf_file]

        fh.write(f"index_asdf_file,input_asdf_file = {index_asdf_file},{input_asdf_file}\n")

        if ".asdf" not in input_asdf_file:
            continue


        # A per-file failure is caught and counted here rather than allowed to
        # abort the thread's remaining files.

        try:


            # Download file from input S3 bucket to local machine.

            s3_object_input_asdf_file = "s3://" + bucket_name_input + "/" + input_asdf_file
            download_cmd = ['aws','s3','cp',s3_object_input_asdf_file,input_asdf_file]
            run_tool(download_cmd)


            # Create output ASDF filename for working directory.

            output_asdf_file = input_asdf_file.replace(".asdf","_lite.asdf")


            # Correct gWCS.  Inject fake variable sources.  Output local L2 ASDF file.

            correct_gwcs_inject_fake_variable_sources_output_asdf_file(
                fh,
                input_asdf_file,
                output_asdf_file
                )


            # Gzip the output ASDF file.

            gunzip_cmd = ['gzip', output_asdf_file]
            run_tool(gunzip_cmd)


            # Upload gzipped file to output S3 bucket.

            gzipped_output_asdf_file = output_asdf_file + ".gz"

            s3_object_name = gzipped_output_asdf_file

            filenames = [gzipped_output_asdf_file]

            objectnames = [s3_object_name]

            util.upload_files_to_s3_bucket(s3_client,bucket_name_output,filenames,objectnames)


            # Clean up work directory.

            # Best-effort: a leftover work file is not an injection failure.
            try:
                run_tool(['rm','-f',input_asdf_file])
            except ToolError as exc:
                print(f"*** Warning: cleanup failed for {input_asdf_file}: {exc}")

            try:
                run_tool(['rm','-f',gzipped_output_asdf_file])
            except ToolError as exc:
                print(f"*** Warning: cleanup failed for {gzipped_output_asdf_file}: {exc}")


            # Code-timing benchmark.

            thread_end_time_benchmark = time.time()
            diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
            fh.write(f"Elapsed time in seconds to convert ASDF file to ASDF file with injected fake variables = {diff_time_benchmark}\n")
            thread_start_time_benchmark = thread_end_time_benchmark


            # End of loop over asdf_files.

            fh.write(f"Loop end over asdf_files: index_asdf_file,input_asdf_file = {index_asdf_file},{input_asdf_file}\n")

            n_ok += 1

        except Exception as e:
            n_failed += 1
            fh.write(f"*** Error: Fake-source injection failed for {input_asdf_file}: {e}\n")
            fh.flush()
            print(f"*** Error: Fake-source injection failed for {input_asdf_file}: {e}")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")
    fh.write(f"n_ok,n_failed = {n_ok},{n_failed}\n")

    fh.close()

    print(f"Finish for index_thread = {index_thread}: n_ok,n_failed = {n_ok},{n_failed}")

    return n_ok,n_failed


def execute_parallel_processes(asdf_files_list,num_cores=None):

    '''
    Run the injection threads and return the (n_ok,n_failed) totals summed over all
    threads.  A thread that dies outright counts as a failure, so an unhandled worker
    exception cannot be logged and then forgotten.
    '''

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

    n_ok_total = 0
    n_failed_total = 0

    for future in futures:
        index = futures.index(future)
        try:
            n_ok,n_failed = future.result()
            n_ok_total += n_ok
            n_failed_total += n_failed
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")
            n_failed_total += 1

    return n_ok_total,n_failed_total


#-------------------------------------------------------------------------------------------------------------
# Methods for handling conversion from ASDF to ASDF with injected fake variables.
#-------------------------------------------------------------------------------------------------------------

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

            run_tool(generate_injection_catalog_cmd)


            # Add newly generated injection catalog to list.

            file_content += f"{injection_catalog_filename}\n"


            # Upload fake-source injection catalog to product S3 bucket.

            s3_object_name_injection_catalog = "injection_catalogs/" + injection_catalog_filename

            util.upload_files_to_s3_bucket(s3_client,job_info_s3_bucket,[injection_catalog_filename],[s3_object_name_injection_catalog])


    # Write injection-catalog-list file.

    injection_catalog_list_filename = input_asdf_path.replace(".asdf", "_catalog_list_sciimg.csv")

    with open(injection_catalog_list_filename, 'w') as f:
        f.write(file_content)


    # Run fake-source injections code.

    sci_ext = fake_sources_dict['sci_ext']
    num_injections = fake_sources_dict['num_injections']
    injection_mag_min = fake_sources_dict['mag_min']
    injection_mag_max = fake_sources_dict['mag_max']

    fake_sources_code = rapid_sw + '/modules/fake_src/rapid_l2_injections.py'

    fake_sources_cmd = [python_cmd,
                        fake_sources_code,
                        input_asdf_path,
                        injection_catalog_list_filename,
                        output_asdf_path,
                        '--fix-wcs']

    run_tool(fake_sources_cmd)

    return


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':

    do_not_overwrite = True

    # Parse ASDF files in output S3 bucket.

    my_bucket_output = s3_resource.Bucket(bucket_name_output)

    output_asdf_files = []

    for my_bucket_output_object in my_bucket_output.objects.all():

        fname_output = str(my_bucket_output_object.key)

        #print(f"fname_output = {fname_output}")

        output_asdf_files.append(fname_output)


    # Parse desired ASDF files in input S3 bucket.

    input_asdf_files = []

    cp_cmd = f"aws s3 ls s3://{bucket_name_input}/ | grep cal.asdf"
    try:
        code_to_execute_stdout = run_shell(cp_cmd).stdout
    except ToolError as exc:
        # grep exits 1 when nothing matches -- an empty listing, not a failure.
        if exc.details.get("returncode") == 1:
            code_to_execute_stdout = ""
        else:
            raise
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

    n_to_convert = len(input_asdf_files)

    if num_cores > 1:
        n_ok,n_failed = execute_parallel_processes(input_asdf_files,num_cores)
    else:
        thread_index = 0
        n_ok,n_failed = run_single_core_job(input_asdf_files,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to convert ASDF files to ASDF-with-fake-variables files =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.  A run that had files to convert and failed on all of them, or that
    # had any per-file failure, must not report success.

    print(f"n_to_convert,n_ok,n_failed = {n_to_convert},{n_ok},{n_failed}")

    if n_failed > 0:
        print(f"*** Error: {n_failed} of {n_to_convert} file(s) failed to convert; quitting...")
        exit(65)

    if n_to_convert > 0 and n_ok == 0:
        print(f"*** Error: {n_to_convert} file(s) were listed but none were converted; quitting...")
        exit(65)

    exit(0)


