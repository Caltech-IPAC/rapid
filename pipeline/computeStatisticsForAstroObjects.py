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
import database.modules.utils.roman_tessellation_db as sqlite
import modules.utils.rapid_pipeline_subs as util

swname = "computeStatisticsForAstroObjects.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages for creating and setting up AstroObjectsMeta tables.

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


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# To process OpenUniverse simulation images, environment variables STARTDATETIME and ENDDATETIME
# specify observation datetimes.  Later, this will be augmented with code to query the
# SOCProcs database table for controlling the processing the Roman Space Telescope WFI data.
#
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


# Get number of cores for parallel processing.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# Define columns to be populated in AstroObjectsMeta tables.

astroobjectsmeta_cols = []
astroobjectsmeta_cols.append("aid")
astroobjectsmeta_cols.append("meanra")
astroobjectsmeta_cols.append("stdevra")
astroobjectsmeta_cols.append("meandec")
astroobjectsmeta_cols.append("stdevdec")
astroobjectsmeta_cols.append("meanflux")
astroobjectsmeta_cols.append("stdevflux")
astroobjectsmeta_cols.append("nsources")

astroobjectsmeta_cols_comma_separated_string = ", ".join(astroobjectsmeta_cols)
astroobjectsmeta_columns = tuple(astroobjectsmeta_cols)

print(f"AstroObjectsMeta columns: {astroobjectsmeta_cols_comma_separated_string}")


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(fields,index_thread):

    '''
    Update lightcurve statistics in AstroObjectsMeta_<field> database tables, omitting sources that
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


    # Open database connections.

    roman_tessellation_db = sqlite.RomanTessellationNSIDE512()

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        fh.close()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Loop over all fields associated with this thread and compute statistics for astroobjects:
    # 1. Remove AstroObjects_<field> database records with redundant aids (keep latest).
    #    This is an artifact of bulk-copying records into the PostgreSQL database for the case
    #    that there is more than one source near by in the same difference image that is
    #    assigned the same aid because of close proximity (this would not happen if
    #    row-by-row inserts were used, which, of course, would be too slow).  This may be
    #    worked around for PhotUtils catalogs computed with min_separation = 1.0 pixels.
    # 2. Delete AstroObjects_<field>  database records that do not have corresponding
    #    Merges_<field> record(s).
    # 3. Query for records in each Merges_<field> database table joined with sources table.
    # 4. Determine unique pids (primary key of DiffImages table).
    # 5. Determine unique aids (primary key of AstroObjects_<field> table).
    # 6. Check associated DiffImages records for those that are best (vbest>0).
    # 7. Populate vbest dictionary keyed by unique pid.
    # 8. Compute statistics for all Merges_<field> records with best sources.
    # 9. Populate AstroObjectsMeta_<field> database records

    my_fields = list(range(index_thread, nfields, num_cores))
    for index_field in my_fields:

        field = fields[index_field]

        fh.write(f"Loop start: index_field,field = {index_field},{field}\n")
        fh.flush()

        merges_tablename = f"merges_{field}"
        astroobjects_tablename = f"astroobjects_{field}"
        astroobjectsmeta_tablename = f"astroobjectsmeta_{field}"


        # Remove redundant-aid AstroObjects_<field> database records (keeping latest).
        # This deletes every row where a row with the same aid but higher ctid exists.
        # PostgreSQL can execute this as a merge/hash join, which is much faster than the
        # anti-join pattern of NOT IN.

        fh.write(f"Removing redundant-aid AstroObjects_<field> database records (keeping latest)...\n")

        query = f"DELETE FROM {astroobjects_tablename} a " +\
                f"USING {astroobjects_tablename} b " +\
                f"WHERE a.aid = b.aid AND a.ctid < b.ctid;"

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
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        for record in records:
            fh.write(f"record = {record}\n")


        # Delete astroobjects records that do not have corresponding
        # record(s) in the merges_<field> database table.

        #query = f"SELECT aid FROM {astroobjects_tablename} WHERE aid NOT IN " +\
        #        f"(SELECT aid FROM {merges_tablename});"

        # This query is much more efficient than the above.
        query = f"SELECT a.aid " +\
                f"FROM {astroobjects_tablename} a " +\
                f"LEFT JOIN {merges_tablename} b ON a.aid = b.aid " +\
                f"WHERE b.aid IS NULL;"

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
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        aids_list = []
        for record in records:

            aid = record[0]
            aids_list.append(aid)

        n_aids_list = len(aids_list)

        if n_aids_list > 0:

            aids_comma_separated_string = ",".join(str(a) for a in aids_list)

            fh.write(f"Deleting records for aids = {aids_comma_separated_string} in " +
                     f"{astroobjects_tablename} database table...\n")
            fh.flush()

            query = f"DELETE FROM {astroobjects_tablename} " +\
                    f"WHERE aid IN ({aids_comma_separated_string});"

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
                roman_tessellation_db.close()
                raise

            if dbh.exit_code >= 64:
                fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                roman_tessellation_db.close()
                raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

            for record in records:
                fh.write(f"record = {record}\n")


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to delete astroobjects records " +
                 f"that do not have merges records = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # For the current field, query for adjacent fields, and then query
        # the L2Files table for all records that overlap these fields to get
        # <obs_date> and <sca>, in order to generate a finite list of Sources child
        # database table to join (and avoid joining with the Sources parent table).


        # This method does not get all overlapping fields for corners that stick out.
        '''
        neighboring_rtids = roman_tessellation_db.get_all_neighboring_rtids(field)

        sciimg_overlapping_rtids = [str(field)]
        for neighboring_rtid in neighboring_rtids:
            sciimg_overlapping_rtids.append(neighboring_rtid)
        '''

        # This method may be slower, but it does a better job of finding all overlapping fields.
        # Distortion is ignored as a simplification.

        neighboring_rtids = roman_tessellation_db.get_all_neighboring_rtids(field)    # For debug purposes only.  TODO remove later.
        fh.write(f"neighboring_rtids = {neighboring_rtids}\n")                        # For debug purposes only.  TODO remove later.

        query = f"SELECT crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22 " +\
                f"FROM l2files " +\
                f"WHERE vbest > 0 " +\
                f"AND status > 0 " +\
                f"AND dateobs >= '{startdatetime}' " +\
                f"AND dateobs < '{enddatetime}' " +\
                f"AND field = {field};"

        sql_queries = [query]

        try:
            records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        rtid_dict = {}
        x_list = [*range(0,naxis1,500)]
        y_list = [*range(0,naxis2,500)]
        x_list.append(naxis1 - 1)
        y_list.append(naxis2 - 1)

        for record in records:

            crval1 = record[0]
            crval2 = record[1]
            crpix1 = record[2]
            crpix2 = record[3]
            cd11 = record[4]
            cd12 = record[5]
            cd21 = record[6]
            cd22 = record[7]

            for y in y_list:
                for x in x_list:

                    # x,y,crpix1,crpix2 must be zero-based.
                    ra,dec = util.tan_proj2(x,y,crpix1-1,crpix2-1,crval1,crval2,cd11,cd12,cd21,cd22)

                    roman_tessellation_db.get_rtid(ra,dec)
                    rtid = roman_tessellation_db.rtid

                    rtid_dict[rtid] = 1

        sciimg_overlapping_rtids = list(rtid_dict.keys())

        if not sciimg_overlapping_rtids:
            sciimg_overlapping_rtids = [field]

        fh.write(f"sciimg_overlapping_rtids = {sciimg_overlapping_rtids}\n")       # For debug purposes only.  TODO remove later.


        # Now find the relevant Sources child tables.

        sciimg_overlapping_rtids_comma_separated_string = ", ".join(str(r) for r in sciimg_overlapping_rtids)

        # We want all time history here.
        query = f"SELECT DISTINCT cast(dateobs as date),sca " +\
                f"FROM l2files " +\
                f"WHERE vbest > 0 " +\
                f"AND status > 0 " +\
                f"AND field IN ({sciimg_overlapping_rtids_comma_separated_string});"

        sql_queries = [query]

        try:
            records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        sources_child_tables_to_check_existence = []

        for record in records:

            obs_date = str(record[0]).replace("-","")
            sca = str(record[1])
            sources_tablename = f"sources_{obs_date}_{sca}"
            sources_child_tables_to_check_existence.append(sources_tablename)

        sources_child_tables_comma_separated_string = "', '".join(sources_child_tables_to_check_existence)

        # This query returns a list of sources_<date_obs>_<sca> database table names
        # that actually exist.
        query = f"SELECT c.relname FROM pg_class c " +\
                f"JOIN pg_namespace n ON n.oid = c.relnamespace " +\
                f"WHERE n.nspname = 'public' " +\
                f"AND c.relkind IN ('r','p') " +\
                f"AND c.relname IN ('{sources_child_tables_comma_separated_string}');"

        sql_queries = [query]

        try:
            table_exists_records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        sources_child_tables = []
        for table_exists_record in table_exists_records:
            sources_tablename = table_exists_record[0]
            sources_child_tables.append(sources_tablename)


        # Skip the current field if no Sources child tables were found, since an empty
        # list would otherwise yield the degenerate UNION ALL query ";", which the
        # database rejects with a syntax error.  Leave the astroobjects_<field> and
        # astroobjectsmeta_<field> database records alone in this case, rather than
        # treating every aid as having no best source and deleting it.

        if len(sources_child_tables) == 0:
            fh.write(f"*** Warning: No Sources child tables found for field {field}; " +
                     f"skipping to next field...\n")
            fh.flush()
            continue


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to determine relevant sources child tables = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # Process astroobjects/astroobjectsmeta records that do indeed have corresponding
        # record(s) in the merges_<field> database table and Sources database table.
        # Query all source child tables in a single UNION ALL query instead of one
        # round trip per child table.
        # The vbest > 0 filter is folded into the JOIN to avoid N+1 pid lookups.

        union_parts = []
        for sources_tablename in sources_child_tables:
            union_parts.append(
                f"SELECT a.aid,b.ra,b.dec,b.fluxfit FROM {merges_tablename} AS a "
                f"JOIN {sources_tablename} AS b ON a.sid = b.sid "
                f"JOIN diffimages AS d ON b.pid = d.pid "
                f"WHERE d.vbest > 0"
            )
        query = " UNION ALL ".join(union_parts) + ";"

        fh.write(f"Querying {len(sources_child_tables)} source child tables for {merges_tablename} via UNION ALL\n")
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
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
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
        fh.write(f"Elapsed time in seconds to select best records from {merges_tablename}, " +
                 f"source child tables, and diffimages = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # Delete astroobjects/astroobjectsmeta records for aids that have merges
        # but no best sources (all associated diffimages have vbest=0).
        # Uses a single batched DELETE instead of one DELETE per aid.

        best_aids = set(ras_for_aid_dict.keys())

        query = f"SELECT DISTINCT aid FROM {merges_tablename};"

        sql_queries = [query]

        try:
            all_aids_records = dbh.execute_sql_queries(sql_queries,thread_debug)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                     f"(query={query},e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error from dbh.execute_sql_queries (query={query}); quitting...")

        not_best_aids = [str(record[0]) for record in all_aids_records if record[0] not in best_aids]

        if not_best_aids:
            not_best_aids_str = ",".join(not_best_aids)
            fh.write(f"Deleting {len(not_best_aids)} not-best-source aids from " +
                     f"{astroobjects_tablename} database tables...\n")
            fh.flush()

            sql_queries = [
                f"DELETE FROM {astroobjects_tablename} WHERE aid IN ({not_best_aids_str});"
            ]

            try:
                dbh.execute_sql_queries(sql_queries,thread_debug)
            except Exception as e:
                fh.write(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                         f"(e={e});  quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                roman_tessellation_db.close()
                raise

            if dbh.exit_code >= 64:
                fh.write(f"*** Error from dbh.execute_sql_queries; quitting...\n")
                fh.flush()
                fh.close()
                dbh.close()
                roman_tessellation_db.close()
                raise RuntimeError(f"*** Error from dbh.execute_sql_queries (sql_queries={sql_queries}); quitting...")


        # Loop over astroobjects for current field:
        # 1. Compute statistics using full sources history (no cumulative statistics).
        # 2. Prepare AstroObjectsMeta_<field> records for bulk copy.

        aids_list = list(best_aids)

        astroobjectsmeta_table_file = f"astroobjectsmeta_{field}.csv"

        with open(astroobjectsmeta_table_file, "w") as csv_fh:

            i = 0
            for aid in aids_list:

                ras_list = ras_for_aid_dict[aid]
                decs_list = decs_for_aid_dict[aid]
                fluxes_list = fluxes_for_aid_dict[aid]
                nsources = len(ras_list)

                meanra,meandec,stdra,stddec,sky_position_spread = \
                    util.compute_radec_statistics(ras_list, decs_list)
                meanflux = np.mean(fluxes_list)
                stdflux = np.std(fluxes_list)

                if thread_debug == 1 and i < 5:
                    fh.write(f"sky_position_spread = {sky_position_spread} degrees\n")
                    fh.write(f"Inserting AstroObjectsMeta record: astroobjectsmeta_tablename,aid," +
                             f"meanra,meandec,nsources={astroobjectsmeta_tablename},{aid},{meanra},{meandec},{nsources}\n")
                    fh.flush()

                csv_fh.write(",".join(str(v) for v in (aid, meanra, stdra, meandec, stddec, meanflux, stdflux, nsources)) + "\n")

                i += 1

        # Load records into AstroObjectsMeta_<field> database tables.

        try:
            dbh.copy_data_from_file_into_database(astroobjectsmeta_table_file,astroobjectsmeta_tablename,astroobjectsmeta_columns)
        except Exception as e:
            fh.write(f"*** Error: Exception raised in dbh.copy_data_from_file_into_database " +
                     f"(astroobjectsmeta_table_file={astroobjectsmeta_table_file}, " +
                     f"astroobjectsmeta_tablename={astroobjectsmeta_tablename}, e={e});  quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise

        if dbh.exit_code >= 64:
            fh.write(f"*** Error bulk-loading data from file ({astroobjectsmeta_table_file}) " +
                     f"into specified database table ({astroobjectsmeta_tablename}); quitting...\n")
            fh.flush()
            fh.close()
            dbh.close()
            roman_tessellation_db.close()
            raise RuntimeError(f"*** Error bulk-loading data from file ({astroobjectsmeta_table_file}) " +
                               f"into specified database table ({astroobjectsmeta_tablename}); quitting...")


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to bulk copy records into " +
                 f"{astroobjectsmeta_tablename} database table = {diff_time_benchmark}\n")
        fh.flush()
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over fields.

        fh.write(f"Loop end: index_field,field = {index_field},{field}\n")
        fh.flush()


        # Remove no-longer-needed intermediate files.

        file_paths = [astroobjectsmeta_table_file]
        for file_path in file_paths:

            if os.path.exists(file_path):
                os.remove(file_path)
                fh.write(f"File deleted successfully ({file_path})...\n")
                fh.flush()
            else:
                fh.write(f"File does not exist({file_path})...\n")
                fh.flush()


    # Close database connections.

    roman_tessellation_db.close()

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
    Launch parallel tasks to compute lightcurve statistics in AstroObjectsMeta_<field> database tables.
    These tables must be dropped before running this script, as the tables are recreated, indexed,
    and then records populated with bulk copy for each field.  No record inserts or updates are done for speed.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the Sources child table names
    # that were cross-matched and a distinct list of the fields covered by the sources.

    source_tables_to_crossmatch_tuples_list,fields_list,_,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid)

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
    print("Elapsed time in seconds to ascertain available fields and Sources child tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Check whether astroobjectsmeta_<field> database tables exist.
    # Drop all astroobjectsmeta_<field> database tables that exist for fields
    # that are associated with the processing date.

    already_made_dict = {}

    for field in fields_list:

        tablename = f"astroobjectsmeta_{field}"

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


    # Create astroobjectsmeta database tables for all fields.
    # Defer creating indexes on all astroobjectsmeta_<field> database tables until
    # after the tables have been populated.

    print("Creating tables and grants for all astroobjectsmeta_<field> database tables...")

    sql_queries = []

    sql_queries.append("SET default_tablespace = pipeline_data_01;")

    fillfactor = 70

    for field in fields_list:

        print(f"field = {field}")

        tablename = f"astroobjectsmeta_{field}"

        sql_queries.append(f"CREATE TABLE {tablename} (LIKE astroobjectsmeta INCLUDING " +
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
    print("Elapsed time in seconds to create astroobjectsmeta database tables for all fields =",
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


    # Create indexes on all astroobjectsmeta_<field> database tables.

    print("Creating indexes for all astroobjectsmeta_<field> database tables...")

    sql_queries = []

    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for field in fields_list:

        print(f"field = {field}")

        tablename = f"astroobjectsmeta_{field}"

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
    print("Elapsed time in seconds to create indexes for all astroobjectsmeta database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Vacuum and analyze astroobjectsmeta_<field> database tables for all fields.  Drop table if empty.

    print("Vacuuming and analyzing astroobjectsmeta_<field> database tables for all fields...")

    for field in fields_list:

        tablename = f"astroobjectsmeta_{field}"

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

        astroobjectsmeta_child_table_has_rows = records[0][0]

        if not astroobjectsmeta_child_table_has_rows:

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
    print("Elapsed time in seconds to vacuum and analyze all astroobjectsmeta database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to update all astroobjectsmeta statistics =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
