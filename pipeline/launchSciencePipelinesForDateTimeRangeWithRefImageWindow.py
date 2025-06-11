import os
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py"
swvers = "1.0"

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


# Inputs are observaton start and end datetimes of exposures to be processed.
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
# representative raw image for the field and filter is processed
# to initially make the needed reference image for the other raw images
# with the same field and filter;  when it is set to False then
# all other raw images, except for the representative raw images,
# are processed.  The representative raw image is the first in
# a time-ordered, SCA-ordered list for a given field and filter.

start_refimage_mjdobs = os.getenv('STARTREFIMMJDOBS')

if start_refimage_mjdobs is None:

    print("*** Error: Env. var. STARTREFIMMJDOBS not set; quitting...")
    exit(64)

end_refimage_mjdobs = os.getenv('ENDREFIMMJDOBS')

if end_refimage_mjdobs is None:

    print("*** Error: Env. var. ENDREFIMMJDOBS not set; quitting...")
    exit(64)

min_refimage_nframes = os.getenv('MINREFIMNFRAMES')

if min_refimage_nframes is None:

    print("*** Error: Env. var. MINREFIMNFRAMES not set; quitting...")
    exit(64)

make_refimages_flag_str = os.getenv('MAKEREFIMAGESFLAG')

if make_refimages_flag_str is None:

    print("*** Error: Env. var. MAKEREFIMAGESFLAG not set; quitting...")
    exit(64)

make_refimages_flag = eval(make_refimages_flag_str)

run_fid = os.getenv('RUNFID')

if run_fid is None:
    print("*** Message: Will process all filters; quitting...")
else:
    print(f"*** Message: Will process only fid={run_fid}; quitting...")

dry_run_str = os.getenv('DRYRUN')

if dry_run_str is None:

    print("*** Error: Env. var. DRYRUN not set; quitting...")
    exit(64)

dry_run = eval(dry_run_str)


# Print parameters.

print("startdatetime =",startdatetime)
print("enddatetime =",enddatetime)
print("start_refimage_mjdobs =",start_refimage_mjdobs)
print("end_refimage_mjdobs =",end_refimage_mjdobs)
print("min_refimage_nframes =",min_refimage_nframes)
print("make_refimages_flag =",make_refimages_flag)
print("dry_run =",dry_run)


# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.

def run_script(rid):

    """
    Load unique value of rid into the environment variable RID.
    Launch single instance of script with given environment-variable setting for RID.
    """


    # Load RID into the environment.

    os.environ['RID'] = str(rid)


    # Launch single pipeline from within Docker container.

    python_cmd = 'python3.11'
    launch_single_pipeline_instance_code = '/code/pipeline/awsBatchSubmitJobs_launchSingleSciencePipeline.py'

    launch_cmd = [python_cmd,
                  launch_single_pipeline_instance_code]

    exitcode_from_launch_cmd = util.execute_command(launch_cmd)


def launch_parallel_processes(rids, num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_script,rid) for rid in rids]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")


#################
# Main program.
#################

if __name__ == '__main__':


    #
    # Launch a science pipelines for exposures/scas in input observation datetime range.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    # Query database for all field/filter/nframes combinations in reference-image window with
    # minimum number of frames in coadd stack.

    recs = dbh.get_field_fid_nframes_records_for_mjdobs_range(start_refimage_mjdobs,end_refimage_mjdobs,min_refimage_nframes)

    if dbh.exit_code >= 64:
        print("*** Error from query for field/filter/nframes combinations {}; quitting ".format(swname))
        exit(dbh.exit_code)

    num = 1

    field_list = []
    fid_list = []

    for rec in recs:

        field = rec[0]
        fid = rec[1]
        nframes = rec[2]

        field_list.append(field)
        fid_list.append(fid)

        print("num,field,fid,nframes =",field,fid,nframes)


    # Loop over field/filter combinations.

    rid_list = []

    for field,fid in zip(field_list,fid_list):

        if run_fid is not None:
            if run_fid != fid:
                print(f"*** Message: Skipping fid={fid}; continuing...")
                continue

        print("field,fid =",field,fid)


        # Query database for all L2Files records associated with input observation datetime range,
        # for a given field and filter.  Return a time-ordered,SCA-ordered list.

        recs = dbh.get_l2files_records_for_datetime_range_field_fid(startdatetime,enddatetime,field,fid)

        if dbh.exit_code >= 64:
             print("*** Error from query for L2Files records {}; quitting ".format(swname))
             exit(dbh.exit_code)


        # Aggregate pipeline instances to be run under AWS Batch.
        # When the flag to make reference images is set to True, then only one
        # representative raw image for the field and filter is processed
        # to initially make the needed reference image for the other raw images
        # with the same field and filter;  when it is set to False then
        # all other raw images, except for the representative raw images,
        # are processed.  The representative raw image is the first in
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


    # The job launching is done in parallel, taking advantage of multiple cores on the job-launcher machine.

    number_pipeline_instances = len(rid_list)
    print(f"number_pipeline_instances = {number_pipeline_instances}")

    if dry_run:
        print("*** Message: Skip launching pipelines...")
    else:
        print("*** Message: Launching pipelines...")
        launch_parallel_processes(rid_list)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch all pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
