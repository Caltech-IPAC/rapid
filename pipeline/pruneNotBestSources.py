'''
Requires that pruneNotBestMerges.py is executed before pruneNotBestSources.py as there are dependencies.
'''

import boto3
import os
import numpy as np
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

from psycopg2 import sql

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

# `job_info_s3_bucket_base` is NOT read here: it named the legacy IMSS-era
# bucket (`rapid-pipeline-files`) that this account does not carry, and
# binding it at import cost a KeyError for a value the file never used.
# Products and job records go to the buckets the parameter tree names.
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_finder_filename'])

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])

ppid = int(config_input['SCI_IMAGE']['ppid'])


# Open database connections for parallel access.

num_cores = os.cpu_count()

print("num_cores =",num_cores)

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


# Get S3 client.

s3_client = boto3.client('s3')


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(sources_table_names,index_thread):

    '''
    Remove records from Sources_<proc_date>_<sca> database tables associated with sources
    that are no longer best (vbest=0 in associated Diffimages table).  Returns
    (n_tables_ok,n_tables_failed) counts so that a table which raises partway through
    cannot be logged and then forgotten.
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
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    dbh = dbh_list[index_thread]

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Per-thread outcome counters.  These are returned to the parent process so
    # that a run which prunes nothing due to per-table failures cannot report success.

    n_tables_ok = 0
    n_tables_failed = 0


    # Loop over all sources_<proc_date>_* database tables associated with this thread and prune not-best sources:
    # 1. Query for all records in each sources_{proc_date}_* database table.
    # 2. Determine unique pids (primary key of DiffImages table).
    # 3. Check associated DiffImages records are not best (vbest=0).
    # 4. Populate vbest dictionary keyed by unique pid.
    # 5. Delete all Sources_<proc_date> records having not-best sources.

    for index_sources_table_names in range(n_sources_table_names):

        index_core = index_sources_table_names % num_cores
        if index_thread != index_core:
            continue

        sources_tablename = sources_table_names[index_sources_table_names]


        fh.write(f"Loop start: index_sources_table_names,sources_tablename = {index_sources_table_names},{sources_tablename}\n")


        # A per-table failure is caught and counted here rather than allowed to
        # abort the thread's remaining tables.

        try:

            query = sql.SQL("SELECT sid,pid FROM {tbl}").format(tbl=sql.Identifier(sources_tablename))

            sql_queries = [query]
            params_list = [None]
            records = dbh.execute_sql_queries(sql_queries,params_list,thread_debug)

            sids_list = []
            pids_list = []
            pids_dict = {}

            for record in records:

                sid = record[0]
                pid = record[1]

                sids_list.append(sid)
                pids_list.append(pid)
                pids_dict[pid] = 1


            # Code-timing benchmark.

            thread_end_time_benchmark = time.time()
            diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
            fh.write(f"Elapsed time in seconds to select all records from {sources_tablename} database tables = {diff_time_benchmark}\n")
            thread_start_time_benchmark = thread_end_time_benchmark


            # Query for all DiffImages records associated with unique list of pids.

            unique_pids_list = list(pids_dict.keys())

            vbest_dict = {}

            for pid in unique_pids_list:

                query = "SELECT vbest FROM diffimages WHERE pid = %s"

                sql_queries = [query]
                params_list = [(pid,)]
                records = dbh.execute_sql_queries(sql_queries,params_list,thread_debug)

                vbest = records[0][0]

                vbest_dict[pid] = vbest


            # Check each source is associated with a not-best DiffImages record.

            for sid,pid in zip(sids_list,pids_list):

                vbest = vbest_dict[pid]

                if vbest == 0:


                    # Source is not best, so delete sources_<proc_date>_<sca> record.

                    dbh.delete_source(sources_tablename,sid,thread_debug)


            # Code-timing benchmark.

            thread_end_time_benchmark = time.time()
            diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
            fh.write(f"Elapsed time in seconds to delete not-best record(s) from {sources_tablename} database table\n")
            thread_start_time_benchmark = thread_end_time_benchmark

            n_tables_ok += 1

        except Exception as e:
            n_tables_failed += 1
            fh.write(f"*** Error: Pruning not-best sources failed for {sources_tablename}: {e}\n")
            fh.flush()
            print(f"*** Error: Pruning not-best sources failed for {sources_tablename}: {e}")


        # End of loop over sources_table_names.

        fh.write(f"Loop end: index_sources_table_names,sources_tablename = {index_sources_table_names},{sources_tablename}\n")


        # Flush write buffer.

        fh.flush()


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")
    fh.write(f"n_tables_ok,n_tables_failed = {n_tables_ok},{n_tables_failed}\n")

    fh.close()

    return n_tables_ok,n_tables_failed


def execute_parallel_processes(sources_table_names,num_cores=None):

    '''
    Run the pruning threads and return the (n_tables_ok,n_tables_failed) totals summed
    over all threads.  A thread that dies outright counts as a failure, so an unhandled
    worker exception cannot be logged and then forgotten.
    '''

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,sources_table_names,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    n_tables_ok_total = 0
    n_tables_failed_total = 0

    for future in futures:
        index = futures.index(future)
        try:
            n_tables_ok,n_tables_failed = future.result()
            n_tables_ok_total += n_tables_ok
            n_tables_failed_total += n_tables_failed
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")
            n_tables_failed_total += 1

    return n_tables_ok_total,n_tables_failed_total


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Launch parallel tasks to delete all not-best Sources_{proc_date}_* database records
    (for all scas for which Sources_{proc_date}_* tables exist).
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    query = "select tablename from pg_tables where schemaname='public' and tablename like %s"
    sql_queries = [query]
    params_list = [(f"sources_{proc_date}%",)]
    records = dbh.execute_sql_queries(sql_queries,params_list,debug)

    sources_table_names = []
    for record in records:
        tablename = record[0]
        sources_table_names.append(tablename)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to ascertain available sources_{proc_date}_* =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute tasks for sources_table_names in parallel, with the number of parallel threads
    # equal to the number of cores on the job-launcher machine.
    ################################################################################

    n_tables_to_prune = len(sources_table_names)

    if num_cores > 1:
        n_tables_ok,n_tables_failed = execute_parallel_processes(sources_table_names,num_cores)
    else:
        thread_index = 0
        n_tables_ok,n_tables_failed = run_single_core_job(sources_table_names,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Vacuum and analyze sources database tables for given proc_date.  Drop table if empty.

    for tablename in sources_table_names:

        query = sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=sql.Identifier(tablename))

        print("query =", query)

        sql_queries = [query]
        params_list = [None]
        records = dbh.execute_sql_queries(sql_queries,params_list,debug)

        print(f"records = {records}")

        sources_child_table_count = records[0][0]

        if sources_child_table_count == 0:

            print("Dropping {tablename} database table...")

            query = sql.SQL("DROP TABLE {tbl}").format(tbl=sql.Identifier(tablename))

            sql_queries = [query]
            params_list = [None]
            records = dbh.execute_sql_queries(sql_queries,params_list,debug)

        else:

            print("Vacuuming and analyzing {tablename} database table...")

            dbh.vacuum_analyze_table(tablename)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to vacuum and analyze all sources_{proc_date}_* database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to delete all not-best sources in sources_{proc_date}_* database tables =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    for tdbh in dbh_list:
        tdbh.close()

        if tdbh.exit_code >= 64:
            exit(tdbh.exit_code)


    # Termination.  A run that had tables to prune and failed on all of them, or that
    # had any per-table failure, must not report success.

    print(f"n_tables_to_prune,n_tables_ok,n_tables_failed = {n_tables_to_prune},{n_tables_ok},{n_tables_failed}")

    if n_tables_failed > 0:
        print(f"*** Error: {n_tables_failed} of {n_tables_to_prune} table(s) failed while pruning not-best sources; quitting...")
        exit(65)

    if n_tables_to_prune > 0 and n_tables_ok == 0:
        print(f"*** Error: {n_tables_to_prune} table(s) were listed but none were pruned; quitting...")
        exit(65)

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
