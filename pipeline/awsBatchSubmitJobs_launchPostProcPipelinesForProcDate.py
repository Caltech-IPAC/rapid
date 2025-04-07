"""
This script launches many instances of the RAPID post-processing pipeline.
It is assumed that many instances of the RAPID science pipeline for a
given processing date have already been run, AND the metadata from these jobs
have already been registered in the Jobs table of the operations database.

Input the processing date for this script, and it will query the
Jobs database table for all of those jobs that ran normally,
using a database query like the following:

select * from jobs
where ppid = 15
and ended >= '2025-03-26'
and ended <= started + '1 day'
and status > 0
and exitcode <= 32;
"""

import os
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py"
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


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Print out basic information for log file.

print("proc_date =",proc_date)


if __name__ == '__main__':


    #
    # Launch RAPID post-processing pipelines
    # for all RAPID science pipelines that already
    # ran on a given processing date.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all normal RAPID science-pipeline Jobs records
    # that are associated with the given processing date.

    recs = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(proc_date)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Launch pipeline instances via AWS Batch.

    for rec in recs:
        jid = rec[0]
        print("jid =",jid)


        # Load RAPID_JOB_ID into the environment.

        os.environ['RAPID_JOB_ID'] = str(jid)


        # Launch single pipeline from within Docker container.

        python_cmd = 'python3.11'
        launch_single_pipeline_instance_code = '/code/pipeline/awsBatchSubmitJobs_launchSinglePostProcPipeline.py'

        launch_cmd = [python_cmd,
                      launch_single_pipeline_instance_code]

        exitcode_from_launch_cmd = util.execute_command(launch_cmd)


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
