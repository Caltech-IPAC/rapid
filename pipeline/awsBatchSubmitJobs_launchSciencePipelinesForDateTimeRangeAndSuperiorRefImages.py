import os
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRangeAndSuperiorRefImages.py"
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

nframes = os.getenv('NFRAMES')

if nframes is None:

    print("*** Error: Env. var. NFRAMES not set; quitting...")
    exit(64)

cov5percent = os.getenv('COV5PERCENT')

if cov5percent is None:

    print("*** Error: Env. var. COV5PERCENT not set; quitting...")
    exit(64)


# Print parameters.

print("startdatetime =",startdatetime)
print("enddatetime =",enddatetime)
print("nframes =",nframes)
print("cov5percent =",cov5percent)


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
    # Launch science pipelines for exposures-scas in input observation datetime range
    # and superior reference images as defined by the input criteria nframes and cov5percent.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all L2Files records associated with input observation datetime range
    # and superior reference images as defined by the input criteria nframes and cov5percent.

    recs = dbh.get_l2files_records_for_datetime_range_and_superior_reference_images(startdatetime,
                                                                                    enddatetime,
                                                                                    nframes,
                                                                                    cov5percent)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Launch pipeline instances to be run under AWS Batch.

    rid_list = []

    for rec in recs:

        rid = rec[0]
        sca = rec[1]

        rid_list.append(rid)

        print("rid, sca =",rid,sca)


    # The job launching is done in parallel, taking advantage of multiple cores on the job-launcher machine.

    launch_parallel_processes(rid_list)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
