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


# PROCREQ of the processing request (optional).  If set, only the fields of the Jobs records
# of this processing request (Jobs.reqid) are counted; otherwise, the fields of all processing
# requests that ended on the processing date are counted.

proc_req = os.getenv('PROCREQ')

print("proc_req =",proc_req)


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

naxis1_sciimage = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2_sciimage = int(config_input['INSTRUMENT']['naxis2_sciimage'])


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




def execute_single_value_query(dbh,query):

    '''
    Execute a database query that returns one value, quitting on a database error.

    Parameters
    ----------
    dbh : RAPIDDB
        Open database connection.
    query : str
        SQL query returning one row with one column.

    Returns
    -------
    value : object
        The returned value.
    '''

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

    return records[0][0]


def count_records_in_field_tables(dbh,label,fields):

    '''
    Count the records in the <label>_<field> database tables (lower-case name) of the given fields,
    skipping fields whose table does not exist.

    Parameters
    ----------
    dbh : RAPIDDB
        Open database connection.
    label : str
        Table name base, e.g. "Merges" or "XAstroObjects", used in the printed lines.
    fields : list of int
        Fields whose tables are counted.

    Returns
    -------
    nfields_table : int
        Number of fields whose table exists.
    n_table : int
        Summed record count over those tables.
    '''

    print(f"Counting {label.lower()}_<field> database tables for all fields associated with processing date...")

    nfields_table = 0
    n_table = 0

    for field in fields:

        tablename = f"{label.lower()}_{field}"

        table_exists_flag = execute_single_value_query(dbh,f"SELECT to_regclass('public.{tablename}') IS NOT NULL;")

        if not table_exists_flag:
            print(f"{label}_<field> database table does not exist for field={field}; continuing...")
            continue

        nfields_table += 1

        count = execute_single_value_query(dbh,f"SELECT count(*) FROM {tablename};")
        print(f"{label}: field,count = {field},{count}")

        n_table += count

    return nfields_table,n_table


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Count Merges, AstroObjects and AstroObjectsMeta records, and their
    XMerges, XAstroObjects and XAstroObjectsMeta counterparts, for fields
    associated with a given processing date (and processing request, if
    PROCREQ is set).  The counts may include contributions from processing
    on other dates.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the Sources and XSources child table names
    # to cross-match and distinct lists of the fields covered by the sources and xsources.

    _,fields_list,_,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid,
                                                                   reqid=proc_req,
                                                                   naxis1=naxis1_sciimage,naxis2=naxis2_sciimage)

    _,xfields_list,_,_ = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid,
                                                                   table_prefix="xsources",
                                                                   reqid=proc_req,
                                                                   naxis1=naxis1_sciimage,naxis2=naxis2_sciimage)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Count records in the <table>_<field> database tables for all fields associated with processing date.

    totals = []

    for label,fields in (("Merges",fields_list),
                         ("AstroObjects",fields_list),
                         ("AstroObjectsMeta",fields_list),
                         ("XMerges",xfields_list),
                         ("XAstroObjects",xfields_list),
                         ("XAstroObjectsMeta",xfields_list)):

        nfields_table,n_table = count_records_in_field_tables(dbh,label,fields)

        totals.append((label,nfields_table,n_table))


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print(f"Elapsed time in seconds to count records in {label.lower()} database tables =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


    # Print out the totals.

    for label,nfields_table,n_table in totals:
        name = label.lower()
        print(f"Totals for {label}: nfields_{name},n_{name} = {nfields_table},{n_table}")


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
