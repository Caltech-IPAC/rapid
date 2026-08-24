'''
Requires that pruneNotBestMerges.py is executed before pruneNotBestSources.py as there are dependencies.
'''

import boto3
import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "pruneNotBestSources.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages for querying database tables.

debug = 1


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Other required environment variables.

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

job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_finder_filename'])

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])

ppid = int(config_input['SCI_IMAGE']['ppid'])


# Number of parallel worker processes.
# os.cpu_count() returns None when the number of cores cannot be determined.
# Database connections are opened by each worker process itself, since psycopg2
# connections cannot be shared across processes.

num_cores = os.cpu_count() or 1

print("num_cores =",num_cores)


# Get S3 client.

s3_client = boto3.client('s3')


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def execute_or_raise(dbh,sql_queries,thread_debug,fh,context):

    '''
    Execute a list of SQL queries and raise RuntimeError if the database reports an error.

    dbh.execute_sql_queries does not raise on failure: it prints, rolls back, sets
    dbh.exit_code and returns None.  It also resets dbh.exit_code to zero at the start of
    every call, so a failure that is not checked immediately is erased by the next
    successful query.  Every call must therefore be checked right away.
    '''

    records = dbh.execute_sql_queries(sql_queries,thread_debug)

    if dbh.exit_code >= 64 or records is None:

        message = f"*** Error executing SQL for {context} (dbh.exit_code={dbh.exit_code}); quitting..."

        if fh is not None:
            fh.write(message + "\n")
            fh.flush()
            fh.close()

        raise RuntimeError(message)

    return records


def run_single_core_job(sources_table_names,index_thread):

    '''
    Remove records from Sources_<obs_date>_<sca> database tables associated with sources
    that are no longer best (vbest=0 in associated Diffimages table).
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    n_sources_table_names = len(sources_table_names)

    print("index_thread,n_sources_table_names =",index_thread,n_sources_table_names)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except Exception as e:
        print(f"*** Error: Could not open output file {thread_work_file} ({e}); quitting...")
        exit(64)


    # Open database connection.  Each worker process must open its own connection,
    # since psycopg2 connections cannot be shared across processes.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Loop over all sources_<obs_date>_* database tables associated with this thread
    # and prune not-best sources: delete every record whose pid refers to a DiffImages
    # record that is no longer best (vbest=0).

    my_sources_table_names = list(range(index_thread, n_sources_table_names, num_cores))
    for index_sources_table_names in my_sources_table_names:

        sources_tablename = sources_table_names[index_sources_table_names]

        fh.write("Loop start: index_sources_table_names,sources_tablename = " +
                 f"{index_sources_table_names},{sources_tablename}\n")


        # An empty Sources child table is dropped by the vacuum phase of this script,
        # so it may be absent when this script is rerun for the same processing date.

        sql_queries = [f"SELECT to_regclass('public.{sources_tablename}') IS NOT NULL;"]
        records = execute_or_raise(dbh,sql_queries,thread_debug,fh,
                                   f"existence check for {sources_tablename}")

        if not records or not records[0][0]:

            fh.write(f"{sources_tablename} database table does not exist; skipping...\n")
            fh.flush()

            continue

        query = (f"DELETE FROM {sources_tablename} AS a "
                 f"USING diffimages AS b "
                 f"WHERE a.pid = b.pid AND b.vbest = 0;")

        fh.write(f"Deleting not-best records in {sources_tablename} database table...\n")
        fh.flush()

        execute_or_raise(dbh,[query],thread_debug,fh,
                         f"delete of not-best records from {sources_tablename}")


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write("Elapsed time in seconds to delete not-best record(s) from " +
                 f"{sources_tablename} database table = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over sources_table_names.

        fh.write("Loop end: index_sources_table_names,sources_tablename = " +
                 f"{index_sources_table_names},{sources_tablename}\n")


        # Flush write buffer.

        fh.flush()


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(sources_table_names,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count() or 1   # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,sources_table_names,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    failures = []
    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            failures.append(e)
            print(f"*** Error in thread index {index} = {e}")

    if failures:
        print(f"*** Error(s) from {len(failures)} worker(s); quitting...")
        exit(64)


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Launch parallel tasks to delete all not-best Sources_<obs_date>_<sca> database records
    for observation dates that are associated with the given processing date.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the Sources child table names
    # that were cross-matched.

    source_tables_to_crossmatch_tuples_list,_,_,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid)

    sources_child_tables = []
    for table_to_crossmatch_tuple in source_tables_to_crossmatch_tuples_list:

        obs_date = table_to_crossmatch_tuple[0]
        sca = table_to_crossmatch_tuple[1]

        sources_tablename = f"sources_{obs_date}_{sca}"

        sources_child_tables.append(sources_tablename)

    if len(sources_child_tables) == 0:
        print("*** Error: No Sources child tables found;  quitting...")
        dbh.close()
        exit(7)


    # Close the main database connection before the parallel phase, rather than leaving
    # it idle for its duration.  The worker processes open their own connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute tasks for sources_child_tables in parallel, with the number of parallel
    # threads equal to the number of cores on the job-launcher machine.
    ################################################################################

    if num_cores > 1:
        execute_parallel_processes(sources_child_tables,num_cores)
    else:
        thread_index = 0
        run_single_core_job(sources_child_tables,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Reopen the main database connection for the serial vacuum phase.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Vacuum and analyze sources database tables for given proc_date.  Drop table if empty.

    for tablename in sources_child_tables:


        # The table may already have been dropped by an earlier run of this script.

        sql_queries = [f"SELECT to_regclass('public.{tablename}') IS NOT NULL;"]
        records = execute_or_raise(dbh,sql_queries,debug,None,
                                   f"existence check for {tablename}")

        if not records or not records[0][0]:
            print(f"{tablename} database table does not exist; skipping...")
            continue

        query = f"SELECT EXISTS (SELECT 1 FROM {tablename} LIMIT 1);"
        records = execute_or_raise(dbh,[query],debug,None,
                                   f"row-existence check for {tablename}")
        sources_child_table_has_rows = records[0][0]

        if not sources_child_table_has_rows:
            print(f"Dropping {tablename} database table...")
            execute_or_raise(dbh,[f"DROP TABLE {tablename};"],debug,
                             None,f"drop of {tablename}")
        else:
            print(f"Vacuuming and analyzing {tablename} database table...")
            dbh.vacuum_analyze_table(tablename)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to vacuum and analyze all Sources child tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to delete all not-best sources in all Sources child tables =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
