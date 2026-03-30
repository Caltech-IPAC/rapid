import os
import numpy as np
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util


swname = "countMerges.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

debug = 1

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


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



#################
# Main program.
#################

if __name__ == '__main__':

    '''
    Count total number of records in all partitioned merges_<field> database tables.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Select all merges_<field> database tables.

    sql_queries = []
    sql_queries.append(f"select tablename from pg_tables where schemaname='public' and tablename like 'merges_%';")
    records = dbh.execute_sql_queries(sql_queries,debug)

    fields_list = []
    for record in records:
        field = record[0].replace("merges_","")
        fields_list.append(field)


    # Loop over all fields covered by merges_<field> database tables.
    # The total number of merges records include sources that are
    # inside and outside of the field of interest.

    num_merges_total = 0
    num_merges_total_outside_field_of_interest = 0

    for field in fields_list:

        merges_tablename = f"merges_{field}"


        # Count all merges_<field> database records.

        query = f"SELECT count(*) FROM {merges_tablename};"

        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        for record in records:

            num_merges = record[0]
            num_merges_total += num_merges


        # Count all merges_<field> database records outside of the field of interest.

        query = f"SELECT count(*) FROM {merges_tablename} a, sources b WHERE a.sid=b.sid AND b.field != {field};"

        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        for record in records:

            num_merges_outside_field_of_interest = record[0]
            num_merges_total_outside_field_of_interest += num_merges_outside_field_of_interest


    # Report results.

    num_tables = len(fields_list)
    print(f"Total number of merges_<field> database tables = {num_tables}")

    print(f"Total number of merges_<field> database records = {num_merges_total}")
    print(f"Total number  of merges_<field> database records outside field of interest = {num_merges_total_outside_field_of_interest}")


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to count all merges records =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
