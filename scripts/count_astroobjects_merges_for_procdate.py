import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite


swname = "count_astroobjects_for_procdate.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages for creating and setting up Merges and AstroObjects tables.

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


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
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

ppid = int(config_input['SCI_IMAGE']['ppid'])

match_radius = float(config_input['SOURCE_MATCHING']['match_radius'])


# Open database connections for parallel access.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# Define columns to be populated in AstroObjects tables.

astroobjects_cols = []
astroobjects_cols.append("aid")
astroobjects_cols.append("ra0")
astroobjects_cols.append("dec0")
astroobjects_cols.append("flux0")

astroobjects_cols_comma_separated_string = ", ".join(astroobjects_cols)
astroobjects_columns = tuple(astroobjects_cols)

print(f"AstroObjects columns: {astroobjects_cols_comma_separated_string}")


# Define columns to be populated in Merges tables.

merges_cols = []
merges_cols.append("aid")
merges_cols.append("sid")

merges_cols_comma_separated_string = ", ".join(merges_cols)
merges_columns = tuple(merges_cols)

print(f"Merges columns: {merges_cols_comma_separated_string}")




#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Count AstroObjects and Merges for fields associated with a
    given processing date.  The counts may include contributions
    from processing on other dates.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the Sources child table names
    # to cross-match and a distinct list of the fields covered by the sources.

    source_tables_to_crossmatch_tuples_list,fields_list,_,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Determine whether merges_<field> database tables exist.

    already_made_dict = {}

    for field in fields_list:

        tablename = f"merges_{field}"

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


    # Count records in merges_<field> database tables for all fields associated with processing date.

    print("Counting merges_<field> database tables for all fields associated with processing date...")

    nfields_merges = 0
    n_merges = 0

    for field in fields_list:

        table_exists_flag = already_made_dict[field]

        if not table_exists_flag:
            print(f"Merges_<field> and Merges_<field> database tables " +
                  f"does not exist for field={field}; continuing...")
            continue

        nfields_merges += 1

        tablename = f"merges_{field}"

        query = f"SELECT count(*) FROM {tablename};"

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
            print("*** Error from {}; quitting ".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        count = records[0][0]
        print(f"Merges: field,count = {field},{count}")

        n_merges += count


    # Determine whether astroobjects_<field> database tables exist.

    already_made_dict = {}

    for field in fields_list:

        tablename = f"astroobjects_{field}"

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


    # Count records in astroobjects_<field> database tables for all fields associated with processing date.

    print("Counting astroobjects_<field> database tables for all fields associated with processing date...")

    nfields_astroobjects = 0
    n_astroobjects = 0

    for field in fields_list:

        table_exists_flag = already_made_dict[field]

        if not table_exists_flag:
            print(f"AstroObjects_<field> and Merges_<field> database tables " +
                  f"does not exist for field={field}; continuing...")
            continue

        nfields_astroobjects += 1

        tablename = f"astroobjects_{field}"

        query = f"SELECT count(*) FROM {tablename};"

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
            print("*** Error from {}; quitting ".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        count = records[0][0]
        print(f"AstroObjects: field,count = {field},{count}")

        n_astroobjects += count


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to count records in astroobjects database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Determine whether astroobjectsmeta_<field> database tables exist.

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


    # Count records in astroobjectsmeta_<field> database tables for all fields associated with processing date.

    print("Counting astroobjectsmeta_<field> database tables for all fields associated with processing date...")

    nfields_astroobjectsmeta = 0
    n_astroobjectsmeta = 0

    for field in fields_list:

        table_exists_flag = already_made_dict[field]

        if not table_exists_flag:
            print(f"AstroObjectsMeta_<field> and Merges_<field> database tables " +
                  f"does not exist for field={field}; continuing...")
            continue

        nfields_astroobjectsmeta += 1

        tablename = f"astroobjectsmeta_{field}"

        query = f"SELECT count(*) FROM {tablename};"

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
            print("*** Error from {}; quitting ".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        count = records[0][0]
        print(f"AstroObjectsMeta: field,count = {field},{count}")

        n_astroobjectsmeta += count


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to count records in astroobjectsmeta database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Print out the totals.

    print(f"Totals for Merges: nfields_merges,n_merges = {nfields_merges},{n_merges}")
    print(f"Totals for AstroObjects: nfields_astroobjects,n_astroobjects = {nfields_astroobjects},{n_astroobjects}")
    print(f"Totals for AstroObjectsMeta: nfields_astroobjectsmeta,n_astroobjectsmeta = {nfields_astroobjectsmeta},{n_astroobjectsmeta}")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to count records in merges database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to run this script =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
