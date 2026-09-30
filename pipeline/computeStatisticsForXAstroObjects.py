'''
Compute lightcurve statistics for the XAstroObjects built from the SExtractor catalogs of
the SFFT difference images, and load the XAstroObjectsMeta_<field> database tables.

This is the SExtractor-catalog counterpart of computeStatisticsForAstroObjects.py, which does
the same thing for the photutils PSF-fit catalogs.  The two scripts are deliberately parallel
in structure; the differences are the tables read (xsources_<obs_date>_<sca>, xmerges_<field>,
xastroobjects_<field>), the table written (xastroobjectsmeta_<field>), the keys (xsid/xaid),
and the flux column (fluxap instead of fluxfit).
'''

import os
import numpy as np
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "computeStatisticsForXAstroObjects.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages for creating and setting up XAstroObjectsMeta tables.

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

ppid = int(config_input['SCI_IMAGE']['ppid'])


# Get number of cores for parallel processing.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# Define columns to be populated in XAstroObjectsMeta tables.

xastroobjectsmeta_cols = []
xastroobjectsmeta_cols.append("xaid")
xastroobjectsmeta_cols.append("meanra")
xastroobjectsmeta_cols.append("stdevra")
xastroobjectsmeta_cols.append("meandec")
xastroobjectsmeta_cols.append("stdevdec")
xastroobjectsmeta_cols.append("meanflux")
xastroobjectsmeta_cols.append("stdevflux")
xastroobjectsmeta_cols.append("nsources")

xastroobjectsmeta_cols_comma_separated_string = ", ".join(xastroobjectsmeta_cols)
xastroobjectsmeta_columns = tuple(xastroobjectsmeta_cols)

print(f"XAstroObjectsMeta columns: {xastroobjectsmeta_cols_comma_separated_string}")


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(fields,index_thread):

    '''
    Update lightcurve statistics in XAstroObjectsMeta_<field> database tables, omitting xsources that
    are associated with not-best difference images.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 1

    nfields = len(fields)

    print("index_thread,nfields =",index_thread,nfields)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except Exception as e:
        print(f"*** Error: Could not open output file {thread_work_file} ({e}); quitting...")
        raise


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Loop over all fields associated with this thread and compute statistics for xastroobjects:
    # 1. Remove XAstroObjects_<field> database records with redundant xaids (keep latest).
    #    This is an artifact of bulk-copying records into the PostgreSQL database for the case
    #    that there is more than one xsource near by in the same difference image that is
    #    assigned the same xaid because of close proximity (this would not happen if
    #    row-by-row inserts were used, which, of course, would be too slow).  This may be
    #    worked around for PhotUtils catalogs computed with min_separation = 1.0 pixels.
    # 2. Delete XAstroObjects_<field>  database records that do not have corresponding
    #    XMerges_<field> record(s).
    # 3. Query for records in each XMerges_<field> database table joined with xsources table.
    # 4. Determine unique pids (primary key of DiffImages table).
    # 5. Determine unique xaids (primary key of XAstroObjects_<field> table).
    # 6. Check associated DiffImages records for those that are best (vbest>0).
    # 7. Populate vbest dictionary keyed by unique pid.
    # 8. Compute statistics for all XMerges_<field> records with best xsources.
    # 9. Populate XAstroObjectsMeta_<field> database records

    my_fields = list(range(index_thread, nfields, num_cores))
    for index_field in my_fields:

        field = fields[index_field]

        fh.write(f"Loop start: index_field,field = {index_field},{field}\n")
        fh.flush()

        xmerges_tablename = f"xmerges_{field}"
        xastroobjects_tablename = f"xastroobjects_{field}"
        xastroobjectsmeta_tablename = f"xastroobjectsmeta_{field}"


        # Remove redundant-xaid XAstroObjects_<field> database records (keeping latest).
        # This deletes every row where a row with the same xaid but higher ctid exists.
        # PostgreSQL can execute this as a merge/hash join, which is much faster than the
        # anti-join pattern of NOT IN.

        fh.write(f"Removing redundant-xaid XAstroObjects_<field> database records (keeping latest)...\n")

        query = f"DELETE FROM {xastroobjects_tablename} a " +\
                f"USING {xastroobjects_tablename} b " +\
                f"WHERE a.xaid = b.xaid AND a.ctid < b.ctid;"

        fh.write(f"query = {query}\n")
        fh.flush()

        sql_queries = []
        sql_queries.append(query)

        try:
            records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        for record in records:
            fh.write(f"record = {record}\n")


        # Delete xastroobjects records that do not have corresponding
        # record(s) in the xmerges_<field> database table.

        #query = f"SELECT xaid FROM {xastroobjects_tablename} WHERE xaid NOT IN " +\
        #        f"(SELECT xaid FROM {xmerges_tablename});"

        # This query is much more efficient than the above.
        query = f"SELECT a.xaid " +\
                f"FROM {xastroobjects_tablename} a " +\
                f"LEFT JOIN {xmerges_tablename} b ON a.xaid = b.xaid " +\
                f"WHERE b.xaid IS NULL;"

        fh.write(f"query = {query}\n")
        fh.flush()

        sql_queries = []
        sql_queries.append(query)

        try:
            records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        xaids_list = []
        for record in records:

            xaid = record[0]
            xaids_list.append(xaid)

        n_aids_list = len(xaids_list)

        if n_aids_list > 0:

            xaids_comma_separated_string = ",".join(str(a) for a in xaids_list)

            fh.write(f"Deleting records for xaids = {xaids_comma_separated_string} in " +
                     f"{xastroobjects_tablename} database table...\n")
            fh.flush()

            query = f"DELETE FROM {xastroobjects_tablename} " +\
                    f"WHERE xaid IN ({xaids_comma_separated_string});"

            fh.write(f"query = {query}\n")
            fh.flush()

            sql_queries = []
            sql_queries.append(query)

            try:
                records = dbh.execute_sql_queries(sql_queries,thread_debug)
            except Exception as e:
                fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                         f"(query={query},e={e});  quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                raise

            if dbh.exit_code >= 64:
                fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

            for record in records:
                fh.write(f"record = {record}\n")


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to delete xastroobjects records " +
                 f"that do not have xmerges records = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # For the current field, query the L2Files table for all records that contain
        # the current field in the L2Files.overlapfields column (meaning that any
        # returned science image overlaps the current field), in order to get
        # <obs_date> and <sca> for generation of a finite list of XSources child
        # database table to join (and avoid joining with the XSources parent table).

        fh.write(f"field = {field}\n")

        query = f"SELECT cast(dateobs as date),sca " +\
                f"FROM l2files " +\
                f"WHERE vbest > 0 " +\
                f"AND status > 0 " +\
                f"AND overlapfields @> ARRAY[cast({field} as integer)];"

        sql_queries = [query]

        try:
            records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        xsources_child_tables_to_check_existence_dict = {}

        for record in records:

            obs_date = str(record[0]).replace("-","")
            sca = str(record[1])

            xsources_tablename = f"xsources_{obs_date}_{sca}"
            xsources_child_tables_to_check_existence_dict[xsources_tablename] = 1

        xsources_child_tables_to_check_existence = list(xsources_child_tables_to_check_existence_dict.keys())

        fh.write(f"xsources_child_tables_to_check_existence = {xsources_child_tables_to_check_existence}\n")

        xsources_child_tables_comma_separated_string = "','".join(xsources_child_tables_to_check_existence)

        # This query returns a list of xsources_<date_obs>_<sca> database table names
        # that actually exist.
        query = f"SELECT c.relname FROM pg_class c " +\
                f"JOIN pg_namespace n ON n.oid = c.relnamespace " +\
                f"WHERE n.nspname = 'public' " +\
                f"AND c.relkind IN ('r','p') " +\
                f"AND c.relname IN ('{xsources_child_tables_comma_separated_string}');"

        sql_queries = [query]

        try:
            table_exists_records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        xsources_child_tables = []
        for table_exists_record in table_exists_records:
            xsources_tablename = table_exists_record[0]
            xsources_child_tables.append(xsources_tablename)

        fh.write(f"xsources_child_tables = {xsources_child_tables}\n")


        # Skip the current field if no XSources child tables were found, since an empty
        # list would otherwise yield the degenerate UNION ALL query ";", which the
        # database rejects with a syntax error.  Leave the xastroobjects_<field> and
        # xastroobjectsmeta_<field> database records alone in this case, rather than
        # treating every xaid as having no best xsource and deleting it.

        if len(xsources_child_tables) == 0:
            fh.write(f"*** Warning: No XSources child tables found for field {field}; " +
                     f"skipping to next field...\n")
            fh.flush()
            continue


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to determine relevant xsources child tables = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # Process xastroobjects/xastroobjectsmeta records that do indeed have corresponding
        # record(s) in the xmerges_<field> database table and XSources database table.
        # Query all xsource child tables in a single UNION ALL query instead of one
        # round trip per child table.
        # The vbest > 0 filter is folded into the JOIN to avoid N+1 pid lookups.

        union_parts = []
        for xsources_tablename in xsources_child_tables:
            union_parts.append(
                f"SELECT a.xaid,b.ra,b.dec,b.fluxap FROM {xmerges_tablename} AS a "
                f"JOIN {xsources_tablename} AS b ON a.xsid = b.xsid "
                f"JOIN diffimages AS d ON b.pid = d.pid "
                f"WHERE d.vbest > 0"
            )
        query = " UNION ALL ".join(union_parts) + ";"

        fh.write(f"Querying {len(xsources_child_tables)} xsource child tables for {xmerges_tablename} via UNION ALL\n")
        fh.flush()

        sql_queries = [query]

        try:
            all_records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        fh.write(f"Total records from UNION ALL query = {len(all_records)}\n")

        ras_for_aid_dict = defaultdict(list)
        decs_for_aid_dict = defaultdict(list)
        fluxes_for_aid_dict = defaultdict(list)

        for record in all_records:
            ras_for_aid_dict[record[0]].append(record[1])
            decs_for_aid_dict[record[0]].append(record[2])
            fluxes_for_aid_dict[record[0]].append(record[3])


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to select best records from {xmerges_tablename}, " +
                 f"xsource child tables, and diffimages = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # Delete xastroobjects/xastroobjectsmeta records for xaids that have xmerges
        # but no best xsources (all associated diffimages have vbest=0).
        # Uses a single batched DELETE instead of one DELETE per xaid.

        best_aids = set(ras_for_aid_dict.keys())

        query = f"SELECT DISTINCT xaid FROM {xmerges_tablename};"

        sql_queries = [query]

        try:
            all_aids_records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        not_best_aids = [str(record[0]) for record in all_aids_records if record[0] not in best_aids]

        if not_best_aids:
            not_best_aids_str = ",".join(not_best_aids)
            fh.write(f"Deleting {len(not_best_aids)} not-best-xsource xaids from " +
                     f"{xastroobjects_tablename} database tables...\n")
            fh.flush()

            sql_queries = [
                f"DELETE FROM {xastroobjects_tablename} WHERE xaid IN ({not_best_aids_str});"
            ]

            try:
                dbh.execute_sql_queries(sql_queries,thread_debug)
            except Exception as e:
                fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                         f"(e={e});  quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                raise

            if dbh.exit_code >= 64:
                fh.write(f"*** Error from dbh.execute_sql_queries; quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                raise RuntimeError(f"*** Error from dbh.execute_sql_queries (sql_queries={sql_queries}); quitting...")


        # Loop over xastroobjects for current field:
        # 1. Compute statistics using full xsources history (no cumulative statistics).
        # 2. Prepare XAstroObjectsMeta_<field> records for bulk copy.

        xaids_list = list(best_aids)

        xastroobjectsmeta_table_file = f"xastroobjectsmeta_{field}.csv"

        with open(xastroobjectsmeta_table_file, "w") as csv_fh:

            i = 0
            for xaid in xaids_list:

                ras_list = ras_for_aid_dict[xaid]
                decs_list = decs_for_aid_dict[xaid]
                fluxes_list = fluxes_for_aid_dict[xaid]
                nsources = len(ras_list)

                meanra,meandec,stdra,stddec,sky_position_spread = \
                    util.compute_radec_statistics(ras_list, decs_list)
                meanflux = np.mean(fluxes_list)
                stdflux = np.std(fluxes_list)

                if thread_debug == 1 and i < 5:
                    fh.write(f"sky_position_spread = {sky_position_spread} degrees\n")
                    fh.write(f"Inserting XAstroObjectsMeta record: xastroobjectsmeta_tablename,xaid," +
                             f"meanra,meandec,nsources={xastroobjectsmeta_tablename},{xaid},{meanra},{meandec},{nsources}\n")
                    fh.flush()

                csv_fh.write(",".join(str(v) for v in (xaid, meanra, stdra, meandec, stddec, meanflux, stdflux, nsources)) + "\n")

                i += 1


        # Load records into XAstroObjectsMeta_<field> database tables.

        try:
            dbh.copy_data_from_file_into_database(xastroobjectsmeta_table_file,xastroobjectsmeta_tablename,xastroobjectsmeta_columns)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.copy_data_from_file_into_database " +
                     f"(xastroobjectsmeta_table_file={xastroobjectsmeta_table_file}, " +
                     f"xastroobjectsmeta_tablename={xastroobjectsmeta_tablename}, e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error bulk-loading data from file ({xastroobjectsmeta_table_file}) " +
                     f"into specified database table ({xastroobjectsmeta_tablename}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            raise RuntimeError(f"*** Error bulk-loading data from file ({xastroobjectsmeta_table_file}) " +
                               f"into specified database table ({xastroobjectsmeta_tablename}); quitting...")


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to bulk copy records into " +
                 f"{xastroobjectsmeta_tablename} database table = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over fields.

        fh.write(f"Loop end: index_field,field = {index_field},{field}\n")
        fh.flush()


        # Remove no-longer-needed intermediate files.

        file_paths = [xastroobjectsmeta_table_file]
        for file_path in file_paths:

            if os.path.exists(file_path):
                os.remove(file_path)
                fh.write(f"File deleted successfully ({file_path})...\n")
                fh.flush()
            else:
                fh.write(f"File does not exist({file_path})...\n")
                fh.flush()


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")
    fh.flush()

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(fields_list,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,fields_list,thread_index) for thread_index in range(num_cores)]

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
    Launch parallel tasks to compute lightcurve statistics in XAstroObjectsMeta_<field> database tables.
    These tables must be dropped before running this script, as the tables are recreated, indexed,
    and then records populated with bulk copy for each field.  No record inserts or updates are done for speed.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the XSources child table names
    # that were cross-matched and a distinct list of the fields covered by the xsources.

    xsource_tables_to_crossmatch_tuples_list,fields_list,_,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid,
                                                                   table_prefix="xsources")

    xsources_child_tables = []
    for table_to_crossmatch_tuple in xsource_tables_to_crossmatch_tuples_list:

        obs_date = table_to_crossmatch_tuple[0]
        sca = table_to_crossmatch_tuple[1]

        xsources_tablename = f"xsources_{obs_date}_{sca}"

        xsources_child_tables.append(xsources_tablename)

    if len(xsources_child_tables) == 0:
        print(f"*** Error: No XSources child tables found;  quitting...")
        dbh.close()
        exit(7)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to ascertain available fields and XSources child tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Check whether xastroobjectsmeta_<field> database tables exist.
    # Drop all xastroobjectsmeta_<field> database tables that exist for fields
    # that are associated with the processing date.

    already_made_dict = {}

    for field in fields_list:

        tablename = f"xastroobjectsmeta_{field}"

        sql_queries = []
        sql_queries.append(f"SELECT to_regclass('public.{tablename}') IS NOT NULL;")

        try:
            records = dbh.execute_sql_queries(sql_queries,debug)
        except Exception as e:
            print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                  f"(e={e});  quitting...")
            dbh.close()
            exit(64)

        if dbh.exit_code >= 64:
            print("*** Error from {}; quitting ".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        table_exists_flag = records[0][0]

        already_made_dict[field] = table_exists_flag

        if table_exists_flag:

            print(f"Dropping {tablename} database table...")

            query = f"DROP TABLE {tablename};"

            sql_queries = []
            sql_queries.append(query)

            try:
                records = dbh.execute_sql_queries(sql_queries,debug)
            except Exception as e:
                print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                      f"(e={e});  quitting...")
                dbh.close()
                exit(64)

            if dbh.exit_code >= 64:
                print(f"*** Error: Exception raised in dbh.execute_sql_queries;  quitting...")
                dbh.close()
                exit(dbh.exit_code)


    # Create xastroobjectsmeta database tables for all fields.
    # Defer creating indexes on all xastroobjectsmeta_<field> database tables until
    # after the tables have been populated.

    print("Creating tables and grants for all xastroobjectsmeta_<field> database tables...")

    sql_queries = []

    sql_queries.append("SET default_tablespace = pipeline_data_01;")

    fillfactor = 70

    for field in fields_list:

        print(f"field = {field}")

        tablename = f"xastroobjectsmeta_{field}"

        sql_queries.append(f"CREATE TABLE {tablename} (LIKE xastroobjectsmeta INCLUDING " +
                           f"DEFAULTS INCLUDING CONSTRAINTS) WITH (fillfactor = {fillfactor});")
        sql_queries.append(f"ALTER TABLE {tablename} OWNER TO rapidporole;")
        sql_queries.append(f"REVOKE ALL ON TABLE {tablename} FROM rapidreadrole;")
        sql_queries.append(f"GRANT SELECT ON TABLE {tablename} TO GROUP rapidreadrole;")
        sql_queries.append(f"REVOKE ALL ON TABLE {tablename} FROM rapidadminrole;")
        sql_queries.append(f"GRANT ALL ON TABLE {tablename} TO GROUP rapidadminrole;")
        sql_queries.append(f"REVOKE ALL ON TABLE {tablename} FROM rapidporole;")
        sql_queries.append(f"GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE {tablename} TO rapidporole;")

        sql_queries.append(f"ALTER TABLE {tablename} SET UNLOGGED;")

    try:
        dbh.execute_sql_queries(sql_queries,debug)
    except Exception as e:
        print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
              f"(e={e});  quitting...")
        dbh.close()
        exit(64)

    if dbh.exit_code >= 64:
        print(f"*** Error: Exception raised in dbh.execute_sql_queries;  quitting...")
        dbh.close()
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to create xastroobjectsmeta database tables for all fields =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute tasks for fields in parallel, with the number of parallel threads
    # equal to the number of cores on the job-launcher machine.
    ################################################################################

    if num_cores > 1:
        execute_parallel_processes(fields_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job(fields_list,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Create indexes on all xastroobjectsmeta_<field> database tables.

    print("Creating indexes for all xastroobjectsmeta_<field> database tables...")

    sql_queries = []

    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for field in fields_list:

        print(f"field = {field}")

        tablename = f"xastroobjectsmeta_{field}"

        sql_queries.append(f"CREATE INDEX {tablename}_nsources_idx ON {tablename} (nsources);")
        sql_queries.append(f"CREATE INDEX {tablename}_meanradec_idx ON {tablename} (q3c_ang2ipix(meanra, meandec));")

        sql_queries.append(f"CLUSTER {tablename} USING {tablename}_meanradec_idx;")

    try:
        dbh.execute_sql_queries(sql_queries,debug)
    except Exception as e:
        print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
              f"(e={e});  quitting...")
        dbh.close()
        exit(64)

    if dbh.exit_code >= 64:
        print(f"*** Error: Exception raised in dbh.execute_sql_queries;  quitting...")
        dbh.close()
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to create indexes for all xastroobjectsmeta database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Vacuum and analyze xastroobjectsmeta_<field> database tables for all fields.  Drop table if empty.

    print("Vacuuming and analyzing xastroobjectsmeta_<field> database tables for all fields...")

    for field in fields_list:

        tablename = f"xastroobjectsmeta_{field}"

        query = f"SELECT EXISTS (SELECT 1 FROM {tablename} LIMIT 1);"

        print(f"query = {query}")

        sql_queries = []
        sql_queries.append(query)

        try:
            records = dbh.execute_sql_queries(sql_queries,debug)
        except Exception as e:
            print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                  f"(e={e});  quitting...")
            dbh.close()
            exit(64)

        if dbh.exit_code >= 64:
            print(f"*** Error: Exception raised in dbh.execute_sql_queries;  quitting...")
            dbh.close()
            exit(dbh.exit_code)

        print(f"records = {records}")

        xastroobjectsmeta_child_table_has_rows = records[0][0]

        if not xastroobjectsmeta_child_table_has_rows:

            print(f"Dropping {tablename} database table...")

            query = f"DROP TABLE {tablename};"

            sql_queries = []
            sql_queries.append(query)

            try:
                records = dbh.execute_sql_queries(sql_queries,debug)
            except Exception as e:
                print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                      f"(e={e});  quitting...")
                dbh.close()
                exit(64)

            if dbh.exit_code >= 64:
                print(f"*** Error: Exception raised in dbh.execute_sql_queries;  quitting...")
                dbh.close()
                exit(dbh.exit_code)

        else:

            print(f"Vacuuming and analyzing {tablename} database table...")

            try:
                dbh.vacuum_analyze_table(tablename)
            except Exception as e:
                print(f"*** Error: Exception raised in dbh.vacuum_analyze_table " +
                      f"(tablename={tablename},e={e});  quitting...")
                dbh.close()
                exit(64)

            if dbh.exit_code >= 64:
                print(f"*** Error: Exception raised in dbh.vacuum_analyze_table (tablename={tablename});  quitting...")
                dbh.close()
                exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to vacuum and analyze all xastroobjectsmeta database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to update all xastroobjectsmeta statistics =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
