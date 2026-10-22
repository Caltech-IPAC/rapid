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
swvers = "1.3"
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


# PROCREQ of the processing request.  This is the reqid of the ProcReqs database
# record that the VPO creates.  Only the Jobs records of this processing request
# (Jobs.reqid) are processed, so that other processing requests for the same
# processing date are left alone.

proc_req = os.getenv('PROCREQ')

if proc_req is None:

    print("*** Error: Env. var. PROCREQ not set; quitting...")
    exit(64)

print("proc_req =",proc_req)


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
naxis1_sciimage = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2_sciimage = int(config_input['INSTRUMENT']['naxis2_sciimage'])


# Open database connections for parallel access.
# os.cpu_count() returns None when the number of cores cannot be determined.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count() or 1
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# PRUNEFULLSCAN=True finds the not-best Sources by scanning the processing request's Sources child
# tables in full, which also finds those of difference images set to vbest = 0 by hand or by
# another processing request.  By default (unset or False), only the difference images of the
# request's own rids are considered (see load_notbest_sids_table), which is much faster.

full_scan_str = os.getenv('PRUNEFULLSCAN')

if full_scan_str is None or full_scan_str == "False":
    full_scan = False
elif full_scan_str == "True":
    full_scan = True
else:
    print(f"*** Error: Env. var. PRUNEFULLSCAN must be True or False (PRUNEFULLSCAN={full_scan_str}); quitting...")
    exit(64)

print("full_scan =",full_scan)


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


def drop_notbest_sids_table(dbh,notbest_tablename):

    '''
    Drop the given not-best-sids database table if it exists (it is left behind if an earlier
    run of this script for the same processing request failed).  Raises RuntimeError on a
    database error.
    '''

    execute_or_raise(dbh,[f"DROP TABLE IF EXISTS {notbest_tablename};"],0,None,
                     f"drop of {notbest_tablename}")


def load_notbest_sids_table(dbh,notbest_tablename,sources_child_tables,rids,full_scan):

    '''
    Create the given database table and load into it, on the database server, the sids of the
    Sources records that are no longer best (vbest = 0 in the associated DiffImages record),
    from the given Sources child tables.

    The sids are selected and inserted by INSERT ... SELECT on the database server, rather than
    being read into Python and sent back as a literal array: a processing request can make
    ~100 million sids not best, and a query string that size exceeds the 1-GB limit of a
    PostgreSQL memory allocation.  The table is a regular (unlogged) table rather than a
    temporary table, so that all the parallel worker processes can read the one copy.

    Parameters
    ----------
    dbh : database.modules.utils.rapid_db.RAPIDDB
        Open database connection.
    notbest_tablename : str
        Name of the database table to create; it must not exist.
    sources_child_tables : list of str
        Names of the existing sources_<obs_date>_<sca> child tables of the processing request.
    rids : list of int
        L2Files rids of the processing request's science-pipeline jobs.
    full_scan : bool
        False (the default mode): only the DiffImages records of the request's rids are
        considered.  A DiffImages record is set to vbest = 0 by the updateDiffImage stored
        function when a newer version for the same rid is registered, which happens when this
        processing request reprocesses the rid, so these are the records this request has made
        not best.  Their sids are read through the pid index of the child tables that hold them.
        True: every Sources record in the child tables whose DiffImages record has vbest = 0 is
        found, whichever processing request (or manual change) set vbest = 0.  This scans the
        child tables in full.

    Returns
    -------
    nsids : int
        Number of not-best sids loaded into the table (0 if none).  Raises RuntimeError on a
        database error.
    '''

    thread_debug = 0


    # Create the table without its primary key, which is added after the bulk insert (faster).

    sql_queries = [
        "SET default_tablespace = pipeline_data_01;",
        f"CREATE UNLOGGED TABLE {notbest_tablename} (sid bigint NOT NULL);",
    ]

    execute_or_raise(dbh,sql_queries,thread_debug,None,f"creation of {notbest_tablename}")

    if full_scan:

        for sources_tablename in sources_child_tables:

            query = f"INSERT INTO {notbest_tablename} (sid) " +\
                    f"SELECT a.sid FROM {sources_tablename} AS a " +\
                    f"JOIN diffimages AS b ON a.pid = b.pid " +\
                    f"WHERE b.vbest = 0;"

            execute_or_raise(dbh,[query],thread_debug,None,
                             f"full scan of {sources_tablename} for not-best sids")

        return index_and_count_notbest_sids_table(dbh,notbest_tablename)

    if len(rids) == 0:
        return index_and_count_notbest_sids_table(dbh,notbest_tablename)


    # Not-best difference images of the request's rids, and the child table that holds the
    # Sources of each (sources_<obs_date>_<sca>, from the L2Files record).

    rids_str = ",".join(str(int(rid)) for rid in sorted(set(rids)))

    query = f"SELECT d.pid,cast(l.dateobs as date),l.sca " +\
            f"FROM diffimages AS d " +\
            f"JOIN l2files AS l ON d.rid = l.rid " +\
            f"WHERE d.ppid = {ppid} " +\
            f"AND d.vbest = 0 " +\
            f"AND d.rid IN ({rids_str});"

    records = execute_or_raise(dbh,[query],thread_debug,None,"lookup of not-best difference images")

    print(f"Number of not-best difference images of the processing request's rids = {len(records)}")

    pids_by_table = {}

    for record in records:
        pid = int(record[0])
        obs_date = str(record[1]).replace("-","")
        sources_tablename = f"sources_{obs_date}_{record[2]}"
        pids_by_table.setdefault(sources_tablename,[]).append(pid)

    existing_tables = set(sources_child_tables)

    for sources_tablename,pids in pids_by_table.items():

        if sources_tablename not in existing_tables:
            print(f"{sources_tablename} database table is not a child table of this request; skipping...")
            continue

        pids_str = ",".join(str(pid) for pid in sorted(set(pids)))

        query = f"INSERT INTO {notbest_tablename} (sid) " +\
                f"SELECT sid FROM {sources_tablename} WHERE pid IN ({pids_str});"

        execute_or_raise(dbh,[query],thread_debug,None,
                         f"lookup of not-best sids in {sources_tablename}")

    return index_and_count_notbest_sids_table(dbh,notbest_tablename)


def index_and_count_notbest_sids_table(dbh,notbest_tablename):

    '''
    Add the primary key (sid) to the given not-best-sids database table, analyze it, and return
    its number of records.  Raises RuntimeError on a database error (including a duplicate sid,
    which the sid sequence of the Sources tables rules out).
    '''

    sql_queries = [
        f"ALTER TABLE {notbest_tablename} ADD PRIMARY KEY (sid) USING INDEX TABLESPACE pipeline_indx_01;",
        f"ANALYZE {notbest_tablename};",
        f"SELECT count(*) FROM {notbest_tablename};",
    ]

    records = execute_or_raise(dbh,sql_queries,0,None,f"indexing and counting of {notbest_tablename}")

    return int(records[0][0])


def run_single_core_job(fields,notbest_tablename,index_thread):

    '''
    Remove records from Merges_<field> database tables associated with Sources that are no longer best
    (vbest=0 in associated Diffimages table).  Returns a dictionary of the number of records deleted
    from each Merges_<field> table (absent tables are left out).
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

    ndeleted_by_field = {}

    my_fields = list(range(index_thread, nfields, num_cores))

    if len(my_fields) == 0:

        fh.write(f"No fields assigned to index_thread={index_thread}; nothing to do.\n")
        fh.close()

        return ndeleted_by_field


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Prune not-best merges for all fields associated with this thread: delete from each
    # Merges_<field> table the records having the not-best sids, which the main process loaded
    # once into the shared notbest_tablename database table, through its sid index.

    fh.write(f"Using not-best sids in database table {notbest_tablename}.\n")
    fh.flush()

    for index_field in my_fields:

        field = fields[index_field]

        fh.write(f"Loop start: index_field,field = {index_field},{field}\n")

        merges_tablename = f"merges_{field}"


        # An empty Merges_<field> table is dropped by the vacuum phase of this script,
        # so it may be absent.

        sql_queries = [f"SELECT to_regclass('public.{merges_tablename}') IS NOT NULL;"]
        records = execute_or_raise(dbh,sql_queries,thread_debug,fh,
                                   f"existence check for {merges_tablename}")

        if not records or not records[0][0]:

            fh.write(f"{merges_tablename} database table does not exist; skipping...\n")
            fh.flush()

            continue

        query = f"WITH deleted AS (DELETE FROM {merges_tablename} AS m USING {notbest_tablename} AS n " +\
                f"WHERE m.sid = n.sid RETURNING 1) SELECT count(*) FROM deleted;"

        records = execute_or_raise(dbh,[query],thread_debug,fh,
                                   f"delete of not-best records from {merges_tablename}")

        ndeleted = int(records[0][0])
        ndeleted_by_field[field] = ndeleted


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Deleted {ndeleted} not-best record(s) from {merges_tablename} database table " +
                 f"in {diff_time_benchmark} seconds\n")
        thread_start_time_benchmark = thread_end_time_benchmark

        fh.write(f"Loop end: index_field,field = {index_field},{field}\n")
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

    return ndeleted_by_field


def execute_parallel_processes(fields_list,notbest_tablename,num_cores):

    '''
    Run run_single_core_job in num_cores processes, and return the combined dictionary of
    the number of records deleted from each Merges_<field> table.
    '''

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,fields_list,notbest_tablename,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    ndeleted_by_field = {}
    failures = []
    for future in futures:
        index = futures.index(future)
        try:
            ndeleted_by_field.update(future.result())
            print(f"Finish normally for index_thread = {index}")
        except Exception as e:
            failures.append(e)
            print(f"*** Error in thread index {index} = {e}")

    if failures:
        print(f"*** Error(s) from {len(failures)} worker(s); quitting...")
        exit(64)

    return ndeleted_by_field


def run_single_core_vacuum_job(fields,ndeleted_by_field,index_thread):

    '''
    Drop empty Merges_<field> database tables, and vacuum/analyze those from which records were
    deleted.
    '''

    thread_debug = 0
    nfields = len(fields)

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    for index_field in range(index_thread, nfields, num_cores):

        field = fields[index_field]
        tablename = f"merges_{field}"


        # The table may already have been dropped by an earlier run of this script.

        sql_queries = [f"SELECT to_regclass('public.{tablename}') IS NOT NULL;"]
        records = execute_or_raise(dbh,sql_queries,thread_debug,None,
                                   f"existence check for {tablename}")

        if not records or not records[0][0]:
            print(f"{tablename} database table does not exist; skipping...")
            continue

        query = f"SELECT EXISTS (SELECT 1 FROM {tablename} LIMIT 1);"
        records = execute_or_raise(dbh,[query],thread_debug,None,
                                   f"row-existence check for {tablename}")
        merges_child_table_has_rows = records[0][0]

        if not merges_child_table_has_rows:
            print(f"Dropping {tablename} database table...")
            execute_or_raise(dbh,[f"DROP TABLE {tablename};"],thread_debug,None,
                             f"drop of {tablename}")
        elif ndeleted_by_field.get(field,0) > 0:
            print(f"Vacuuming and analyzing {tablename} database table...")
            dbh.vacuum_analyze_table(tablename)
        else:
            print(f"No records deleted from {tablename} database table; not vacuuming...")

    dbh.close()

    if dbh.exit_code >= 64:
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    message = f"Vacuum finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_vacuum_processes(fields_list,ndeleted_by_field,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        futures = [executor.submit(run_single_core_vacuum_job,fields_list,ndeleted_by_field,thread_index) for thread_index in range(num_cores)]

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
    Delete all not-best Merges_<field> database records for the fields of the given
    processing request.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing request the Sources child table names
    # that were cross-matched, the fields covered by its Sources, and its jobs.

    source_tables_to_crossmatch_tuples_list,fields_list,_,meta_list = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid,reqid=proc_req,
                                                                    naxis1=naxis1_sciimage,naxis2=naxis2_sciimage)

    sources_child_tables = []
    for table_to_crossmatch_tuple in source_tables_to_crossmatch_tuples_list:

        obs_date = table_to_crossmatch_tuple[0]
        sca = table_to_crossmatch_tuple[1]

        sources_tablename = f"sources_{obs_date}_{sca}"

        sql_queries = [f"SELECT to_regclass('public.{sources_tablename}') IS NOT NULL;"]
        records = execute_or_raise(dbh,sql_queries,0,None,f"existence check for {sources_tablename}")

        if records[0][0]:
            sources_child_tables.append(sources_tablename)

    if len(sources_child_tables) == 0:
        print("*** Error: No Sources child tables found;  quitting...")
        dbh.close()
        exit(7)


    # Load, once, the sids of the Sources that are no longer best into a database table that
    # all the worker processes share.  The table is named for the processing request, and one
    # left behind by an earlier failed run for the same request is dropped first.

    rids = [meta_dict["rid"] for meta_dict in meta_list]

    notbest_tablename = f"notbest_sids_{proc_date}_req{int(proc_req)}"

    drop_notbest_sids_table(dbh,notbest_tablename)

    nnotbest_sids = load_notbest_sids_table(dbh,notbest_tablename,sources_child_tables,rids,full_scan)

    print(f"Number of not-best sids to prune (full_scan={full_scan}) = {nnotbest_sids}")


    # Close the main database connection before the parallel phases, rather than leaving
    # it idle for their duration.  The worker processes open their own connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs and not-best sids =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Delete the not-best records for fields in parallel, with the number of parallel
    # threads equal to the number of cores on the job-launcher machine.  Skipped if
    # there is nothing to prune.  The not-best-sids table is dropped afterwards, even
    # if a worker fails.
    ################################################################################

    try:
        if nnotbest_sids == 0:
            print("No not-best sids; nothing to prune.")
            ndeleted_by_field = {}
        elif num_cores > 1:
            ndeleted_by_field = execute_parallel_processes(fields_list,notbest_tablename,num_cores)
        else:
            thread_index = 0
            ndeleted_by_field = run_single_core_job(fields_list,notbest_tablename,thread_index)
    finally:
        dbh = db.RAPIDDB()

        if dbh.exit_code < 64:
            drop_notbest_sids_table(dbh,notbest_tablename)
            dbh.close()
        else:
            print(f"*** Error: Could not open database connection to drop {notbest_tablename}; " +
                  f"it will be dropped by the next run for this processing request...")

    print("Total number of not-best Merges records deleted =",sum(ndeleted_by_field.values()))


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Drop empty Merges_<field> database tables, and vacuum and analyze those from which
    # records were deleted.

    print("Dropping empty Merges_<field> database tables and vacuuming pruned ones...")

    if num_cores > 1:
        execute_parallel_vacuum_processes(fields_list,ndeleted_by_field,num_cores)
    else:
        thread_index = 0
        run_single_core_vacuum_job(fields_list,ndeleted_by_field,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to drop and vacuum Merges database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to delete all not-best merges =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
