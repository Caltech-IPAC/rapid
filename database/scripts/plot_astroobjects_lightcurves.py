import os
import numpy as np
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import matplotlib.pyplot as plt

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util


swname = "plot_astroobjects_lightcurves.py"
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


# Ensure the following environment variables are set:
# DBPORT,DBNAME,DBUSER,DBPASS,DBSERVER,PYTHONPATH.

dbport = os.getenv('DBPORT')
if dbport is None:
    print("*** Error: Env. var. DBPORT not set; quitting...")
    exit(64)

dbname = os.getenv('DBNAME')
if dbname is None:
    print("*** Error: Env. var. DBNAME not set; quitting...")
    exit(64)

dbuser = os.getenv('DBUSER')
if dbuser is None:
    print("*** Error: Env. var. DBUSER not set; quitting...")
    exit(64)

dbpass = os.getenv('DBPASS')
if dbpass is None:
    print("*** Error: Env. var. DBPASS not set; quitting...")
    exit(64)

dbserver = os.getenv('DBSERVER')
if dbserver is None:
    print("*** Error: Env. var. DBSERVER not set; quitting...")
    exit(64)

pythonpath = os.getenv('PYTHONPATH')
if pythonpath is None:
    print("*** Error: Env. var. PYTHONPATH not set; quitting...")
    exit(64)


def plot_lightcurve(sim_type_db,field,aid,x,y):


    # Set up the plot.

    plt.figure(figsize=(10, 6))


    # Add labels and a title
    plt.xlabel("Observation MJD (day)")
    plt.ylabel("PSF-fit flux (DN/s)")
    plt.title(f"Lightcurve from PostgreSQL Sources DB = {sim_type_db}, field={field}, astroObjectID={aid} ")

    ax = plt.subplot(1,1,1)
    ax.plot(x,y,'.', markersize=6, alpha=1.0, color='red', markerfacecolor='red')


    # Output plot to PNG file.

    fig_name = f'{sim_type_db}_field{field}_aid{aid}.png'
    plt.savefig(fig_name)


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Plot lightcurves in all partitioned astroobjects_<field> database tables
    having more than the specified number of data points.
    '''

    sim_type_db = "rimtimsims2db"
    num_pts_min = 6


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Select all astroobjects_<field> database tables.

    sql_queries = []
    sql_queries.append(f"select tablename from pg_tables where schemaname='public' and tablename like 'astroobjects_%';")
    records = dbh.execute_sql_queries(sql_queries,debug)

    fields_list = []
    for record in records:
        field = record[0].replace("astroobjects_","")
        fields_list.append(field)


    # Loop over all fields covered by astroobjects_<field> database tables.

    num_astroobjects_total = 0

    for field in fields_list:

        astroobjects_tablename = f"astroobjects_{field}"
        merges_tablename = f"merges_{field}"


        # Query all astroobjects_<field> database records with a minimum number of lightcurve data points.

        query = f"SELECT aid,meanra,meandec FROM {astroobjects_tablename} where nsources >= {num_pts_min};"

        sql_queries = []
        sql_queries.append(query)
        astroobjects_records = dbh.execute_sql_queries(sql_queries,debug)

        num_astroobjects_records = len(astroobjects_records)

        print(f"=====> num_astroobjects_records = {num_astroobjects_records}")

        if num_astroobjects_records == 0:
            continue

        for astroobjects_record in astroobjects_records:

            aid = astroobjects_record[0]
            mean_ra = astroobjects_record[1]
            mean_dec = astroobjects_record[2]

            num_astroobjects_total += num_astroobjects_records


            # Query all merges_<field> database records associated with astroobject.

            query = f"SELECT a.sid,mjdobs,ra,dec,fluxfit FROM {merges_tablename} a, Sources b where a.sid = b.sid and aid = {aid};"

            sql_queries = []
            sql_queries.append(query)
            merges_records = dbh.execute_sql_queries(sql_queries,debug)


            mjd_data = []
            flux_data = []

            for merges_record in merges_records:

                sid = merges_record[0]
                mjdobs = merges_record[1]
                ra = merges_record[2]
                dec = merges_record[3]
                flux = merges_record[4]

                mjd_data.append(mjdobs)
                flux_data.append(flux)

            # Plot lightcurve.

            plot_lightcurve(sim_type_db,field,aid,mjd_data,flux_data)


    # Report results.

    num_tables = len(fields_list)
    print(f"Total number of astroobjects_<field> database tables = {num_tables}")

    print(f"Total number of astroobjects_<field> database records where nsources >= {num_pts_min} = {num_astroobjects_total}")


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to plot astroobjects lightcurves =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
