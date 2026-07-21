import os
import configparser
import ast
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"
launch_single_pipeline_instance_code = 'awsBatchSubmitJobs_launchSingleSciencePipeline.py'

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


# Inputs are observation start and end datetimes of exposures to be processed.
# E.g., startdatetime = "2028-09-08 00:18:00", enddatetime = "2028-09-11 00:00:00"

startdatetime = os.getenv('STARTDATETIME')

if startdatetime is None:

    print("*** Error: Env. var. STARTDATETIME not set; quitting...")
    exit(64)

enddatetime = os.getenv('ENDDATETIME')

if enddatetime is None:

    print("*** Error: Env. var. ENDDATETIME not set; quitting...")
    exit(64)


# Additional inputs are observation start and end MJD of window for generating
# reference images, and minimum number of frames in coadd stack.
# When the flag to make reference images is set to True, then only one
# representative L2 science image for the field and filter is processed
# to initially make the needed reference image for the other L2 science images
# with the same field and filter;  when it is set to False then
# all other L2 science images, except for the representative L2 science images,
# are processed.  The representative L2 science image is the first in
# a time-ordered, SCA-ordered list for a given field and filter.

start_refimage_mjdobs = os.getenv('STARTREFIMMJDOBS')

if start_refimage_mjdobs is None:

    print("*** Error: Env. var. STARTREFIMMJDOBS not set; quitting...")
    exit(64)

end_refimage_mjdobs = os.getenv('ENDREFIMMJDOBS')

if end_refimage_mjdobs is None:

    print("*** Error: Env. var. ENDREFIMMJDOBS not set; quitting...")
    exit(64)


# Set flag to determine whether pipeline instances may generate reference images.

make_refimages_flag_str = os.getenv('MAKEREFIMAGESFLAG')

if make_refimages_flag_str is None:

    print("*** Error: Env. var. MAKEREFIMAGESFLAG not set; quitting...")
    exit(64)

try:
    make_refimages_flag = ast.literal_eval(make_refimages_flag_str)
except (ValueError, SyntaxError):
    print(f"*** Error: make_refimages_flag_str is neither True nor False ({make_refimages_flag_str}); quitting...")
    exit(64)


# If RUNFID is set, then process just the specified filter.

run_fid_str = os.getenv('RUNFID')

if run_fid_str is None:
    run_fid = None
    print("*** Message: Will process all filters...")
else:
    try:
        run_fid = int(run_fid_str)
    except:
        print(f"*** Error: run_fid cannot be converted to integer (run_fid={run_fid_str}); quitting...")
        exit(64)
    print(f"*** Message: Will process only fid={run_fid}...")


# Get optional DRYRUN.

dry_run_str = os.getenv('DRYRUN')

if dry_run_str is None:
    dry_run = False
else:
    try:
        dry_run = ast.literal_eval(dry_run_str)
    except (ValueError, SyntaxError):
        print(f"*** Error: dry_run_str is neither True nor False ({dry_run_str}); quitting...")
        exit(64)


# Determine number of parallel processes.

num_cores_str = os.getenv('NUM_CORES')

if num_cores_str is None:
    num_cores = os.cpu_count()
    if num_cores is None:
        num_cores = 1
else:
    try:
        num_cores = int(num_cores_str)
    except:
        print(f"*** Error: num_cores cannot be converted to integer (num_cores_str={num_cores_str}); quitting...")
        exit(64)


# Print parameters.

print("startdatetime =",startdatetime)
print("enddatetime =",enddatetime)
print("start_refimage_mjdobs =",start_refimage_mjdobs)
print("end_refimage_mjdobs =",end_refimage_mjdobs)
print("make_refimages_flag =",make_refimages_flag)
print("run_fid =",run_fid)
print("dry_run =",dry_run)
print("num_cores =",num_cores)


# Get env. var. RAPID_SW and assign cfg_path.

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

min_n_images_to_coadd = int(config_input['REF_IMAGE']['min_n_images_to_coadd'])
max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])

print("min_n_images_to_coadd =",min_n_images_to_coadd)
print("max_n_images_to_coadd =",max_n_images_to_coadd)


# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.

def run_single_core_job(rids,num_cores,index_thread):

    """
    Load unique value of rid into the environment variable RID.
    Launch single instance of script with given environment-variable setting RID.
    """

    njobs = len(rids)

    print("index_thread,njobs =",index_thread,njobs)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    with open(thread_work_file, 'w', encoding="utf-8") as fh:

        fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}\n")

        for index_job in range(njobs):

            index_core = index_job % num_cores
            if index_thread != index_core:
                continue

            rid = rids[index_job]


            # Load RID into the environment.

            os.environ['RID'] = str(rid)


            # Launch single pipeline from within Docker container.

            python_cmd = 'python3.11'
            python_script = f"{rapid_sw}/pipeline/{launch_single_pipeline_instance_code}"

            launch_cmd = [python_cmd,
                          python_script]

            if dry_run:
                fh.write(f"Skipped launching science pipeline for dry_run,rid = {dry_run},{rid}\n")
            else:
                exitcode_from_launch_cmd = util.execute_command(launch_cmd)


                if exitcode_from_launch_cmd == 0:
                    fh.write(f"Launched science pipeline for dry_run,rid = {dry_run},{rid}\n")
                elif exitcode_from_launch_cmd >= 64:
                    fh.write(f"*** Error from launch_cmd = {launch_cmd}: " +
                             f"exitcode_from_launch_cmd,dry_run,rid = " +
                             f"{exitcode_from_launch_cmd},{dry_run},{rid}\n")
                else:
                    fh.write(f"*** Warning from launch_cmd = {launch_cmd}: " +
                             f"exitcode_from_launch_cmd,dry_run,rid = " +
                             f"{exitcode_from_launch_cmd},{dry_run},{rid}\n")

            fh.flush()

            # End of loop over rids.


        fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")


    message = f"Finished normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(rids,num_cores):

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,rids,num_cores,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    exitcode_execute_parallel_processes = 0

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")
            exitcode_execute_parallel_processes = 64

    return exitcode_execute_parallel_processes


#################
# Main program.
#################

if __name__ == '__main__':


    #
    # Launch science pipelines for exposures/SCAs in input observation datetime range.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all field/filter/nframes combinations in reference-image window with
    # minimum number of frames in coadd stack.

    print("Querying database for all field/filter/nframes combinations in the " +
          "reference-image window with minimum number of frames in coadd stack.")

    n_filters = 8

    num = 0

    field_list = []
    fid_list = []

    for fid in range(1,n_filters + 1):

        if run_fid is not None:
            if run_fid != fid:
                print(f"*** Message: Skipping fid={fid}; continuing...")
                continue

        recs = dbh.get_field_fid_nframes_records_for_mjdobs_range(start_refimage_mjdobs,
                                                                  end_refimage_mjdobs,
                                                                  min_n_images_to_coadd,
                                                                  fid)

        if dbh.exit_code >= 64:
            print("*** Error from query for field/filter/nframes combinations {}; quitting ".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        for rec in recs:

            field = rec[0]
            _ = rec[1]         # Parse from query to avoid confusion; will be same as fid.
            nframes = rec[2]

            field_list.append(field)
            fid_list.append(fid)

            num += 1

            print("num,field,fid,nframes =",num,field,fid,nframes)


    # Loop over field/filter combinations.
    #
    # Note: In order to run an instance of the RAPID pipeline that both
    # 1. Generates a reference image; and
    # 2. Processes a science image

    rid_list = []

    for field,fid in zip(field_list,fid_list):

        print("field,fid =",field,fid)


        # Query database for all L2Files records associated with input observation datetime range,
        # for a given field and filter.  Return a time-ordered,SCA-ordered list.

        recs = dbh.get_l2files_records_for_datetime_range_field_fid(startdatetime,enddatetime,field,fid)

        if dbh.exit_code >= 64:
            print("*** Error from query for L2Files records {}; quitting...".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        n_records = len(recs)

        if n_records == 0:
            print("*** Message: No records returned dbh.get_l2files_records_for_datetime_range_field_fid; continuing...")
            continue


        # For the remaining records (which are not reserved for reference-image generation),
        # aggregate pipeline instances to be run under AWS Batch.
        # When the flag to make reference images is set to True, then only one
        # representative L2 science image for the field and filter is processed
        # to initially make the needed reference image for the other L2 science images
        # with the same field and filter;  when it is set to False then
        # all other L2 science images, except for the representative L2 science images,
        # are processed.  The representative L2 science image is the first in
        # a time-ordered, SCA-ordered list for a given field and filter.

        if make_refimages_flag:
            # Process only first record in ordered list of records.
            rec = recs[0]
            rid = rec[0]
            sca = rec[1]
            rid_list.append(rid)
            print("rid, sca =",rid,sca)
        else:
            # Process all records except first record in ordered list of records.
            recs.pop(0)
            for rec in recs:
                rid = rec[0]
                sca = rec[1]
                rid_list.append(rid)
                print("rid, sca =",rid,sca)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # The job launching is done in parallel, taking advantage of multiple cores
    # on the job-launcher machine.

    number_pipeline_instances = len(rid_list)
    print(f"number_pipeline_instances = {number_pipeline_instances}")

    exitcode_execute_parallel_processes = 0

    if num_cores > 1:
        print(f"*** Message: Calling method execute_parallel_processes...")
        exitcode_execute_parallel_processes = execute_parallel_processes(rid_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job(rid_list,num_cores,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch all pipelines =",
        end_time_benchmark - start_time_benchmark)


    # Termination.

    terminating_exitcode = 0

    if exitcode_execute_parallel_processes >= 64:
        terminating_exitcode = exitcode_execute_parallel_processes

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
