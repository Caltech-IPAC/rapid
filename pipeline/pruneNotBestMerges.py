import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "pruneNotBestMerges.py"
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


# Print out basic information for log file.

print("proc_date =",proc_date)


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


# Open database connections for parallel access.

num_cores = os.cpu_count()

print("num_cores =",num_cores)


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(fields,sources_child_tables,index_thread):

    '''
    Remove records from Merges_<field> database tables associated with sources that are no longer best
    (vbest=0 in associated Diffimages table).
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    nfields = len(fields)

    print("index_thread,nfields =",index_thread,nfields)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except Exception as e:
        print(f"*** Error: Could not open output file {thread_work_file} ({e}); quitting...")
        exit(64)


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Loop over all fields associated with this thread and prune not-best merges:
    # 1. Query for all records in each Merges_<field> table.
    # 2. Determine unique pids (primary key of DiffImages table).
    # 3. Check associated DiffImages records are not best (vbest=0).
    # 4. Populate vbest dictionary keyed by unique pid.
    # 5. Delete all Merges_<field> records having not-best sources.

    for index_field in range(nfields):

        index_core = index_field % num_cores
        if index_thread != index_core:
            continue

        field = fields[index_field]

        fh.write(f"Loop start: index_field,field = {index_field},{field}\n")

        merges_tablename = f"merges_{field}"
        union_parts = []
        for sources_tablename in sources_child_tables:
            union_parts.append(
                f"SELECT a.sid FROM {sources_tablename} AS a "
                f"JOIN diffimages AS b ON a.pid = b.pid "
                f"WHERE b.vbest = 0"
            )
        query_prefix = f"DELETE FROM {merges_tablename} WHERE sid IN ("
        query = query_prefix + " UNION ALL ".join(union_parts) + ");"

        fh.write(f"Querying {len(sources_child_tables)} source child tables for {merges_tablename} via UNION ALL\n")
        fh.flush()

        sql_queries = [query]
        dbh.execute_sql_queries(sql_queries,thread_debug)


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to delete not-best record(s) from {merges_tablename} database table = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over fields.

        fh.write(f"Loop end: index_field,field = {index_field},{field}\n")


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


def execute_parallel_processes(fields_list,sources_child_tables,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,fields_list,sources_child_tables,thread_index) for thread_index in range(num_cores)]

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


def run_single_core_vacuum_job(fields,index_thread):

    '''
    Vacuum/analyze or drop empty merges_<field> database tables.
    '''

    thread_debug = 0
    nfields = len(fields)

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    for index_field in range(nfields):

        index_core = index_field % num_cores
        if index_thread != index_core:
            continue

        field = fields[index_field]
        tablename = f"merges_{field}"

        query = f"SELECT EXISTS (SELECT 1 FROM {tablename} LIMIT 1);"
        sql_queries = [query]
        records = dbh.execute_sql_queries(sql_queries,thread_debug)
        merges_child_table_has_rows = records[0][0]

        if not merges_child_table_has_rows:
            print(f"Dropping {tablename} database table...")
            sql_queries = [f"DROP TABLE {tablename};"]
            dbh.execute_sql_queries(sql_queries,thread_debug)
        else:
            print(f"Vacuuming and analyzing {tablename} database table...")
            dbh.vacuum_analyze_table(tablename)

    dbh.close()

    if dbh.exit_code >= 64:
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    message = f"Vacuum finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_vacuum_processes(fields_list,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        futures = [executor.submit(run_single_core_vacuum_job,fields_list,thread_index) for thread_index in range(num_cores)]

        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)
            print(f"Vacuum completed: {i+1} processes, lastly for index={index}")

    failures = []
    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            failures.append(e)
            print(f"*** Error in vacuum thread index {index} = {e}")

    if failures:
        print(f"*** Error(s) from {len(failures)} vacuum worker(s); quitting...")
        exit(64)


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Launch parallel tasks to delete not-best Merges database records for all fields
    (for which merges tables exist).
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the Sources child table names
    # that were cross-matched and a distinct list of the fields covered by the sources.

    source_tables_to_crossmatch_tuples_list,fields_list,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date)

    sources_child_tables = []
    for table_to_crossmatch_tuple in source_tables_to_crossmatch_tuples_list:

        obs_date = table_to_crossmatch_tuple[0]
        sca = table_to_crossmatch_tuple[1]

        sources_tablename = f"sources_{obs_date}_{sca}"

        sources_child_tables.append(sources_tablename)

    if len(sources_child_tables) == 0:
        print(f"*** Error: No Sources child tables found;  quitting...")
        dbh.close()
        exit(7)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute tasks for fields in parallel, with the number of parallel threads
    # equal to the number of cores on the job-launcher machine.
    ################################################################################

    if num_cores > 1:
        execute_parallel_processes(fields_list,sources_child_tables,num_cores)
    else:
        thread_index = 0
        run_single_core_job(fields_list,sources_child_tables,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Close main database connection before parallel vacuum phase.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Vacuum and analyze merges_<field> database tables for all fields.  Drop table if empty.

    print("Vacuuming and analyzing merges_<field> database tables for all fields...")

    if num_cores > 1:
        execute_parallel_vacuum_processes(fields_list,num_cores)
    else:
        thread_index = 0
        run_single_core_vacuum_job(fields_list,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to vacuum and analyze all merges database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to delete all not-best merges =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
