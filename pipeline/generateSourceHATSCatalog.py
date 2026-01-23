'''
Dump sources database table and generate a HATS catalog.  HATS stands for Hierarchical Adaptive Tiling Scheme.

Here is an example sources database record:

   sid    | id  |  pid   |        ra         |         dec         |   xfit    |   yfit    |  fluxfit  |  xerr  |  yerr  | fluxerr | npixfit |   qfit    |  cfit   | flags | sharpness  | roundness1  | roundness2  | npix |     peak     |  field  |  hp6  |   hp9   | expid | fid | sca |   mjdobs   | isdiffpos
----------+-----+--------+-------------------+---------------------+-----------+-----------+-----------+--------+--------+---------+---------+-----------+---------+-------+------------+-------------+-------------+------+--------------+---------+-------+---------+-------+-----+-----+------------+-----------
 69740901 | 193 | 262489 |  9.71589268001384 |  -43.84065969107064 | 2213.0532 | 1115.5831 | -5.212908 |  0.101 | 0.0928 | 0.49486 |     289 |  -11.5949 |  0.0349 |     4 | 0.52294546 | -0.06923953 |  0.43613455 |   25 | -0.049189996 | 5321340 | 35388 | 2264889 | 35509 |   4 |   5 | 62042.7656 | f
'''

import os
import csv
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

from pathlib import Path
from dask.distributed import Client
from hats_import.catalog.arguments import ImportArguments
from hats_import.pipeline import pipeline_with_client
from hats_import.catalog.file_readers import CsvReader

import database.modules.utils.rapid_db as db


swname = "generateSourceHATSCatalog.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages.

debug = 1


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


# Get parameters associated with generating a HATS catalog, including the
# Sources-database-table columns to be exported to HATS catalog (can be a subset).

sources_cols = config_input['HATS_CATALOGS']['sources_cols']
source_input_filename_glob = config_input['HATS_CATALOGS']['source_input_filename_glob']
source_catalog_name = config_input['HATS_CATALOGS']['source_catalog_name']
lowest_healpix_order = int(config_input['HATS_CATALOGS']['lowest_healpix_order'])
highest_healpix_order = int(config_input['HATS_CATALOGS']['highest_healpix_order'])
n_workers = int(config_input['HATS_CATALOGS']['n_workers'])


##############################################################
# Method to generate HATS catalog from set of input CSV files.
##############################################################

def generate_hats_catalog(catalog_csv_path):

    print(f"catalog_csv_path={catalog_csv_path}")

    # Temporary directory for the intermediate/output files
    tmp_dir = Path.cwd() / "tmp"
    print(f"tmp_path={tmp_path}")


    # Specify import arguments

    args = ImportArguments(
        ra_column="ra",
        dec_column="dec",
        lowest_healpix_order=lowest_healpix_order,
        highest_healpix_order=highest_healpix_order,
        file_reader=CsvReader(),
        input_file_list=catalog_csv_path,
        output_artifact_name=source_catalog_name,
        output_path=output_path,
        tmp_dir=tmp_dir,
        tmp_path=tmp_dir,
        resume=False
    )


    # Write HATS catalog.  HATS stands for Hierarchical Adaptive Tiling Scheme.

    with Client(n_workers=n_workers) as client:
        pipeline_with_client(args, client)


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Dump sources database table and generate a HATS catalog.  HATS stands for Hierarchical Adaptive Tiling Scheme.
    Dump operations are segmented and written to multiple files for memory management and possible parallelization.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    # Query for list of unique source IDs (sid) from parent Sources database table.

    query = f"SELECT sid FROM sources order by sid;"
    sql_queries = []
    sql_queries.append(query)
    records = dbh.execute_sql_queries(sql_queries,debug)

    sid_list = []

    for record in records:

        sid = record[0]
        sid_list.append(sid)

    nrows_per_file = 1000000
    nfiles = len(sid_list) // nrows_per_file
    r = len(sid_list) % nrows_per_file
    if r > 0:
        nfiles += 1

    start_index = sid_list[0]
    next_start_index = start_index + nrows_per_file
    catalog_csv_path = []
    for i in range(nfiles):
        file_num = i + 1
        filename_csv = source_input_filename_glob.replace("*",str(file_num))
        catalog_csv_path.append(filename_csv)

        start_index = start_index + nrows_per_file
        next_start_index = start_index + nrows_per_file

        query = f"SELECT {sources_cols} FROM sources order by sid;"
        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        with open(filename_csv, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(sources_cols.split(","))
            writer.writerows(data)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to dumping sources into files =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Call method to generate the HATS catalog.

    generate_hats_catalog(catalog_csv_path)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to generate HATS catalog =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Close database connection.

    dbh.close()


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds from start to finish =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
