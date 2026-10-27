'''
Delete records from the XSources_<obs_date>_<sca> database tables that are associated with
difference images which are no longer best (vbest=0 in the associated DiffImages record), for
the jobs of the given processing request, and drop the child tables left empty.
pruneNotBestXMerges.py must be executed first, since it finds the not-best XMerges_<field>
records through the XSources records.  This is the SExtractor-catalog counterpart of
pruneNotBestSources.py, which does the same thing for the Sources tables (photutils PSF-fit
catalogs).
'''

import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "pruneNotBestXSources.py"
swvers = "1.1"
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

ppid = int(config_input['SCI_IMAGE']['ppid'])
naxis1_sciimage = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2_sciimage = int(config_input['INSTRUMENT']['naxis2_sciimage'])


# A child table that loses at least this fraction of its records is reclustered on its sky-position
# index (CLUSTER rewrites it compactly, in order); otherwise it is only vacuumed and analyzed, which
# leaves the space of the deleted records in the table file.

recluster_deleted_fraction = config_input.getfloat('DATABASE_PRUNING','recluster_deleted_fraction',fallback=0.2)

print("recluster_deleted_fraction =",recluster_deleted_fraction)


# Number of parallel worker processes: environment variable NUM_CORES if set, otherwise all
# the cores (os.cpu_count() returns None when the number of cores cannot be determined).
# Database connections are opened by each worker process itself, since psycopg2
# connections cannot be shared across processes.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count() or 1
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# PRUNEFULLSCAN=True finds the not-best XSources by scanning the processing request's XSources child
# tables in full, which also finds those of difference images set to vbest = 0 by hand or by
# another processing request.  By default (unset or False), only the difference images of the
# request's own rids are considered (see find_notbest_pids_by_table), which is much faster.

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
    dbh.execute_sql_queries does not raise, and resets dbh.exit_code on every call, so each
    call must be checked right away.

    Parameters
    ----------
    dbh : database.modules.utils.rapid_db.RAPIDDB
        Open database connection.
    sql_queries : list of str
        SQL queries to execute.
    thread_debug : int
        Debug level passed to dbh.execute_sql_queries.
    fh : file object or None
        Log file, closed after the error message is written to it; None to skip.
    context : str
        What the queries do, for the error message.

    Returns
    -------
    records : list of tuple
        Records returned by the last query.
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


def find_notbest_pids_by_table(dbh,xsources_child_tables,rids):

    '''
    Find the not-best difference images (vbest = 0) of the processing request's rids, grouped by
    the XSources child table that holds their records (xsources_<obs_date>_<sca>).

    Parameters
    ----------
    dbh : database.modules.utils.rapid_db.RAPIDDB
        Open database connection.
    xsources_child_tables : list of str
        Names of the existing xsources_<obs_date>_<sca> child tables of the processing request.
    rids : list of int
        L2Files rids of the processing request's science-pipeline jobs.

    Returns
    -------
    pids_by_table : dict
        Lists of not-best pids, keyed by child-table name; only existing child tables are
        included.  A DiffImages record is set to vbest = 0 when a newer version for the same rid
        is registered, which happens when this processing request reprocesses the rid.
    '''

    pids_by_table = {}

    if len(rids) == 0:
        return pids_by_table

    rids_str = ",".join(str(int(rid)) for rid in sorted(set(rids)))

    query = "SELECT d.pid,cast(l.dateobs as date),l.sca " +\
            "FROM diffimages AS d " +\
            "JOIN l2files AS l ON d.rid = l.rid " +\
            f"WHERE d.ppid = {ppid} " +\
            "AND d.vbest = 0 " +\
            f"AND d.rid IN ({rids_str});"

    records = execute_or_raise(dbh,[query],0,None,"lookup of not-best difference images")

    print(f"Number of not-best difference images of the processing request's rids = {len(records)}")

    existing_tables = set(xsources_child_tables)

    for record in records:
        pid = int(record[0])
        obs_date = str(record[1]).replace("-","")
        xsources_tablename = f"xsources_{obs_date}_{record[2]}"

        if xsources_tablename not in existing_tables:
            print(f"{xsources_tablename} database table is not a child table of this request; skipping pid={pid}...")
            continue

        pids_by_table.setdefault(xsources_tablename,[]).append(pid)

    return pids_by_table


def run_single_core_job(work_items,index_thread):

    '''
    Delete the not-best records of the XSources_<obs_date>_<sca> database tables assigned to this
    worker, those whose difference image is no longer best (vbest=0 in the DiffImages table).

    Parameters
    ----------
    work_items : list of tuple
        (xsources_tablename, pids) for every child table to prune; worker index_thread takes every
        num_cores-th one.  pids is the list of not-best pids whose records are deleted, or None to
        delete the records of every DiffImages record with vbest = 0 (full scan).
    index_thread : int
        Index of this worker process.

    Returns
    -------
    ndeleted_by_table : dict
        (number of records deleted, number of records before the delete), keyed by child-table
        name.  The number before is the planner's estimate (pg_class.reltuples, kept current by
        the ANALYZE of the loading stage), or an exact count if the table was never analyzed.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    n_work_items = len(work_items)

    print("index_thread,n_work_items =",index_thread,n_work_items)

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


    # Loop over the child tables associated with this thread and delete their not-best records.

    ndeleted_by_table = {}

    for index_work_item in range(index_thread, n_work_items, num_cores):

        xsources_tablename,pids = work_items[index_work_item]

        fh.write(f"Loop start: index_work_item,xsources_tablename = {index_work_item},{xsources_tablename}\n")

        if pids is None:
            query = f"WITH deleted AS (DELETE FROM {xsources_tablename} AS a USING diffimages AS b " +\
                    "WHERE a.pid = b.pid AND b.vbest = 0 RETURNING 1) SELECT count(*) FROM deleted;"
        else:
            pids_str = ",".join(str(int(pid)) for pid in sorted(set(pids)))
            query = f"WITH deleted AS (DELETE FROM {xsources_tablename} " +\
                    f"WHERE pid IN ({pids_str}) RETURNING 1) SELECT count(*) FROM deleted;"

        query_nrows = f"SELECT reltuples FROM pg_class WHERE oid = 'public.{xsources_tablename}'::regclass;"
        records = execute_or_raise(dbh,[query_nrows],thread_debug,fh,f"row-count estimate of {xsources_tablename}")
        nrows_before = int(records[0][0])

        if nrows_before < 0:
            records = execute_or_raise(dbh,[f"SELECT count(*) FROM {xsources_tablename};"],thread_debug,fh,
                                       f"row count of {xsources_tablename}")
            nrows_before = int(records[0][0])

        fh.write(f"Deleting not-best records in {xsources_tablename} database table ({nrows_before} records)...\n")
        fh.flush()

        records = execute_or_raise(dbh,[query],thread_debug,fh,
                                   f"delete of not-best records from {xsources_tablename}")

        ndeleted = int(records[0][0])
        ndeleted_by_table[xsources_tablename] = (ndeleted,nrows_before)


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Deleted {ndeleted} not-best record(s) from {xsources_tablename} database table " +
                 f"in {diff_time_benchmark} seconds\n")
        thread_start_time_benchmark = thread_end_time_benchmark


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

    return ndeleted_by_table


def execute_parallel_processes(work_items,num_cores=None):

    '''
    Run run_single_core_job in num_cores worker processes, and exit with code 64 if any fails.

    Parameters
    ----------
    work_items : list of tuple
        (xsources_tablename, pids) for every child table to prune (see run_single_core_job).
    num_cores : int, optional
        Number of worker processes; all available cores if None.

    Returns
    -------
    ndeleted_by_table : dict
        (number of records deleted, number of records before the delete), keyed by child-table
        name (see run_single_core_job).
    '''

    if num_cores is None:
        num_cores = os.cpu_count() or 1   # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,work_items,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    ndeleted_by_table = {}
    failures = []
    for future in futures:
        index = futures.index(future)
        try:
            ndeleted_by_table.update(future.result())
            print(f"Finish normally for index_thread = {index}")
        except Exception as e:
            failures.append(e)
            print(f"*** Error in thread index {index} = {e}")

    if failures:
        print(f"*** Error(s) from {len(failures)} worker(s); quitting...")
        exit(64)

    return ndeleted_by_table


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Launch parallel tasks to delete the not-best XSources_<obs_date>_<sca> database records of
    the jobs of the given processing request.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing request the XSources child table names that were
    # cross-matched, and its jobs.

    xsource_tables_to_crossmatch_tuples_list,_,_,meta_list = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid,table_prefix="xsources",
                                                                    reqid=proc_req,
                                                                    naxis1=naxis1_sciimage,naxis2=naxis2_sciimage)

    xsources_child_tables = []
    for table_to_crossmatch_tuple in xsource_tables_to_crossmatch_tuples_list:

        obs_date = table_to_crossmatch_tuple[0]
        sca = table_to_crossmatch_tuple[1]

        xsources_tablename = f"xsources_{obs_date}_{sca}"


        # An empty XSources child table is dropped by this script, so it may be absent
        # when this script is rerun for the same processing request.

        sql_queries = [f"SELECT to_regclass('public.{xsources_tablename}') IS NOT NULL;"]
        records = execute_or_raise(dbh,sql_queries,0,None,f"existence check for {xsources_tablename}")

        if records[0][0]:
            xsources_child_tables.append(xsources_tablename)
        else:
            print(f"{xsources_tablename} database table does not exist; skipping...")

    if len(xsources_child_tables) == 0:
        print("*** Error: No XSources child tables found;  quitting...")
        dbh.close()
        exit(7)


    # The child tables to prune, with the not-best pids to delete from each (None for a full scan).

    if full_scan:
        work_items = [(xsources_tablename,None) for xsources_tablename in xsources_child_tables]
    else:
        rids = [meta_dict["rid"] for meta_dict in meta_list]
        pids_by_table = find_notbest_pids_by_table(dbh,xsources_child_tables,rids)
        work_items = sorted(pids_by_table.items())


    # Close the main database connection before the parallel phase, rather than leaving
    # it idle for its duration.  The worker processes open their own connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    if len(work_items) == 0:
        print("No not-best difference images of this processing request; nothing to delete.")
        print("terminating_exitcode =",0)
        exit(0)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute tasks for the child tables in parallel, with the number of parallel
    # processes equal to num_cores (at most the number of child tables).
    ################################################################################

    if min(num_cores,len(work_items)) > 1:
        ndeleted_by_table = execute_parallel_processes(work_items,min(num_cores,len(work_items)))
    else:
        num_cores = 1
        ndeleted_by_table = run_single_core_job(work_items,0)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Reopen the main database connection for the serial vacuum phase.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Vacuum and analyze the XSources child tables that records were deleted from, or recluster
    # those that lost at least recluster_deleted_fraction of their records.  Drop a table left empty.

    for tablename,(ndeleted,nrows_before) in sorted(ndeleted_by_table.items()):

        deleted_fraction = ndeleted / nrows_before if nrows_before > 0 else 1.0

        print(f"Deleted {ndeleted} not-best record(s) from {tablename} database table " +
              f"(about {nrows_before} before, fraction {deleted_fraction:.3f})")

        if ndeleted == 0:
            continue

        query = f"SELECT EXISTS (SELECT 1 FROM {tablename} LIMIT 1);"
        records = execute_or_raise(dbh,[query],debug,None,
                                   f"row-existence check for {tablename}")
        xsources_child_table_has_rows = records[0][0]

        if not xsources_child_table_has_rows:
            print(f"Dropping {tablename} database table...")
            execute_or_raise(dbh,[f"DROP TABLE {tablename};"],debug,
                             None,f"drop of {tablename}")
        elif deleted_fraction >= recluster_deleted_fraction:

            indexname = f"{tablename}_radec_idx"

            records = execute_or_raise(dbh,[f"SELECT to_regclass('public.{indexname}') IS NOT NULL;"],debug,
                                       None,f"existence check for {indexname}")

            if records[0][0]:
                print(f"Reclustering and analyzing {tablename} database table...")
                execute_or_raise(dbh,[f"CLUSTER {tablename} USING {indexname};",f"ANALYZE {tablename};"],debug,
                                 None,f"recluster of {tablename}")
            else:
                print(f"*** Warning: {indexname} does not exist, so {tablename} cannot be reclustered; " +
                      "vacuuming and analyzing it instead...")
                dbh.vacuum_analyze_table(tablename)

        else:
            print(f"Vacuuming and analyzing {tablename} database table...")
            dbh.vacuum_analyze_table(tablename)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to vacuum, analyze or recluster the XSources child tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to delete all not-best xsources in all XSources child tables =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
