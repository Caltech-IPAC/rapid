'''
Count number of AstroObject in RAPID PostgreSQL database,
and obtain other aggregate statistical quantities.

The AstroObjects_<field> tables are not connect by inheritance,
so explicitly looping over them is necessary to aggregrate
statistics.
'''


import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "computeAggregateStatisticsForAstroObjects.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Read environment variables.

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


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)
# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

verbose = int(config_input['JOB_PARAMS']['verbose'])
debug = int(config_input['JOB_PARAMS']['debug'])



#################
# Main program.
#################

if __name__ == '__main__':


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    sql_queries = []
    sql_queries.append(f"select tablename from pg_tables where schemaname='public' and tablename like 'astroobjects_%';")
    records = dbh.execute_sql_queries(sql_queries,debug)

    fields_list = []
    for record in records:
        field = record[0].replace("astroobjects_","")
        fields_list.append(field)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to ascertain available fields =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Count number of astroobjects database tables for all fields.
    # Obtain a grand total.

    print("Counting astroobjects database tables for all fields...")

    sum = 0.0

    for field in fields_list:

        tablename = f"astroobjects_{field}"

        sql_queries = []
        sql_queries.append(f"select count(*) from pg_tables where schemaname='public' and tablename like 'astroobjects_%';")
        records = dbh.execute_sql_queries(sql_queries,debug)

        count_astroobjects_in_table = records[0][0]

        print(f"There are {count_astroobjects_in_table} astoobjects in {tablename} database table.")

        sum += count_astroobjects_in_table

    print(f"There is a grand total of {sum} astoobjects in all AstroObjects database tables.")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to count all astroobjects database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to aggregate astroobjects statistics =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
