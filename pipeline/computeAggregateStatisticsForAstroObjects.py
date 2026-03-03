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


# Set debug = 1 here to get debug messages for creating and setting up Merges and AstroObjects tables.

query_debug = 1


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


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
    records = dbh.execute_sql_queries(sql_queries,query_debug)

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
        sql_queries.append(f"select count(*) from {tablename};")
        records = dbh.execute_sql_queries(sql_queries,query_debug)

        count_astroobjects_in_table = records[0][0]     # Do not catch exception; handle manually.

        print(f"There are {count_astroobjects_in_table} records in {tablename} database table.")

        sum += count_astroobjects_in_table

    sum = int(sum)

    print(f"There is a grand total of {sum} records in all astroobjects_<field> database tables.")

    n_astroobjects_tables = len(fields_list)

    print(f"There are {n_astroobjects_tables} astroobjects_<field> database tables.")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to count all astroobjects database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Count number of merges database tables for all fields.
    # Obtain a grand total.

    print("Counting merges database tables for all fields...")

    sum = 0.0

    for field in fields_list:

        tablename = f"merges_{field}"

        sql_queries = []
        sql_queries.append(f"select count(*) from {tablename};")
        records = dbh.execute_sql_queries(sql_queries,query_debug)

        try:
            count_merges_in_table = records[0][0]     # Do not catch exception; handle manually.

            print(f"There are {count_merges_in_table} records in {tablename} database table.")

            sum += count_merges_in_table

        except:

            astroobjects_tablename = f"astroobjects_{field}"

            print(f"Dropping {astroobjects_tablename} database table...")

            query = f"DROP TABLE {astroobjects_tablename};"

            sql_queries = []
            sql_queries.append(query)
            records = dbh.execute_sql_queries(sql_queries,query_debug)


    sum = int(sum)

    print(f"There is a grand total of {sum} records in all merges_<field> database tables.")

    n_merges_tables = len(fields_list)

    print(f"There are {n_merges_tables} merges_<field> database tables.")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to count all merges database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark



    '''
    Test code for the following:

    fakesourcesdb=> select * from astroobjects_5257277;
       aid    |        ra0        |        dec0        |  flux0   |      meanra       | stdevra |      meandec       | stdevdec | meanflux | stdevflux | nsources |  field  |  hp6  |   hp9
    ----------+-------------------+--------------------+----------+-------------------+---------+--------------------+----------+----------+-----------+----------+---------+-------+---------
     13806316 | 9.471188013414718 | -42.23346195294582 | 1.592923 | 9.471188013414718 |       0 | -42.23346195294582 |        0 | 1.592923 |         0 |        1 | 5257277 | 35476 | 2270521
    (1 row)

    fakesourcesdb=> select * from merges_5257277 where aid = 13806316;
     aid | sid
    -----+-----
    (0 rows)

    fakesourcesdb=> select * from merges_5257277 order by aid;
     aid | sid
    -----+-----
    (0 rows)



    print("Counting records in merges_<field> database tables for where aid = 13806316...")

    for field in fields_list:

        tablename = f"merges_{field}"

        sql_queries = []
        sql_queries.append(f"select count(*) from {tablename} where aid = 13806316;")
        records = dbh.execute_sql_queries(sql_queries,0)

        count_astroobjects_in_table = records[0][0]

        if count_astroobjects_in_table >= 1:

            print(f"There are {count_astroobjects_in_table} astoobjects in {tablename} database table.")
            exit(7)

        else:

            print(f"{tablename} database table does not contain record(s) for aid = 13806316...")

    '''





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
