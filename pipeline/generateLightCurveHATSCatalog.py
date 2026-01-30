'''
Generate RAPID light-curve HATS catalog from database dump of
select columns in the AstroObjects, Merges, and Sources database table.

HATS stands for Hierarchical Adaptive Tiling Scheme.

Here are the AstroObjects columns to be included:

aid (unique AstroObjects ID)
meanra as ra
meandec as dec
nsources
field

For each aid, there is a lightcurve consisting of the following columns.

sid (unique source ID)
pid (unique ID of difference image from which the source was extracted)
expid
field
sca
fid
mjdobs
fluxfit
fluxerr
isdiffpos (0=negative difference image, 1=positive difference image)

Here is an example of a complete sources database record:

   sid    | id  |  pid   |        ra         |         dec         |   xfit    |   yfit    |  fluxfit  |  xerr  |  yerr  | fluxerr | npixfit |   qfit    |  cfit   | flags | sharpness  | roundness1  | roundness2  | npix |     peak     |  field  |  hp6  |   hp9   | expid | fid | sca |   mjdobs   | isdiffpos
----------+-----+--------+-------------------+---------------------+-----------+-----------+-----------+--------+--------+---------+---------+-----------+---------+-------+------------+-------------+-------------+------+--------------+---------+-------+---------+-------+-----+-----+------------+-----------
 69740901 | 193 | 262489 |  9.71589268001384 |  -43.84065969107064 | 2213.0532 | 1115.5831 | -5.212908 |  0.101 | 0.0928 | 0.49486 |     289 |  -11.5949 |  0.0349 |     4 | 0.52294546 | -0.06923953 |  0.43613455 |   25 | -0.049189996 | 5321340 | 35388 | 2264889 | 35509 |   4 |   5 | 62042.7656 | f
'''

import os
import pandas as pd
from nested_pandas import NestedFrame
import pyarrow as pa
import pyarrow.parquet as pq
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
from hats_import.catalog.file_readers import ParquetReader

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "generateLightCurveHATSCatalog.py"
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


# Get parameters associated with generating a lightcurve HATS catalog, including the
# AstroObjects-database-table and Sources-database-table columns to be exported to
# HATS catalog (can be a subset).

lc_astroobjects_cols = config_input['HATS_CATALOGS']['lc_astroobjects_cols']
lc_sources_cols = config_input['HATS_CATALOGS']['lc_sources_cols']
lc_input_filename_glob = config_input['HATS_CATALOGS']['lc_input_filename_glob']
lc_catalog_name = config_input['HATS_CATALOGS']['lc_catalog_name']
lowest_healpix_order = int(config_input['HATS_CATALOGS']['lowest_healpix_order'])
highest_healpix_order = int(config_input['HATS_CATALOGS']['highest_healpix_order'])
n_workers = int(config_input['HATS_CATALOGS']['n_workers'])
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']

s3_object_name_hats_catalog = f"s3://" + product_s3_bucket_base + "/" + lc_catalog_name
filepath_hats_catalog = rapid_work + "/" + lc_catalog_name


##############################################################
# Method to generate HATS catalog from set of input parquet files.
##############################################################

def generate_hats_catalog(catalog_parquet_path):

    print(f"catalog_parquet_path={catalog_parquet_path}")

    # Temporary directory for the intermediate/output files
    tmp_dir = Path.cwd() / "tmp"
    print(f"tmp_dir={tmp_dir}")


    # Specify import arguments

    args = ImportArguments(
        ra_column="ra",
        dec_column="dec",
        lowest_healpix_order=lowest_healpix_order,
        highest_healpix_order=highest_healpix_order,
        file_reader=ParquetReader(),
        input_file_list=catalog_parquet_path,
        output_artifact_name=lc_catalog_name,
        output_path=rapid_work,
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
    Generate RAPID light-curve HATS catalog from database dump of
    select columns in the AstroObjects, Merges, and Sources database table.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query for available fields.

    sql_queries = []
    sql_queries.append(f"select tablename from pg_tables where schemaname='public' and tablename like 'merges_%';")
    records = dbh.execute_sql_queries(sql_queries,debug)

    fields_list = []
    for record in records:
        field = record[0].replace("merges_","")
        fields_list.append(field)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to ascertain available fields =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Query database for lightcurve data by field.  For each field,
    # assure that no more than nrows_per_file are stored in a parquet file.

    nrows_per_file = 100000
    j = 0

    for field in fields_list:


        # Query for ordered list of unique IDs (aid) in AstroObjects_<field> database table.

        aid_list = []
        astroobjects_tablename = f"astroobjects_{field}"
        merges_tablename = f"merges_{field}"

        query = f"SELECT aid FROM {astroobjects_tablename} order by aid;"
        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        for record in records:

            aid = record[0]
            aid_list.append(aid)


        # Partition AstroObjects_<field> database records and lightcurve data into nrows_per_file chunks.

        n_aids = len(aid_list)

        print(f"n_aids = {n_aids}")

        nfiles = n_aids // nrows_per_file
        r = n_aids % nrows_per_file
        if r > 0:
            nfiles += 1

        print(f"nrows_per_file,n_aids,nfiles={nrows_per_file},{n_aids},{nfiles}")

        start_index = 0
        end_index = start_index + nrows_per_file - 1
        catalog_parquet_path = []

        for i in range(nfiles):
            file_num = j + 1
            output_parquet_filename = lc_input_filename_glob.replace("*",str(file_num))
            catalog_parquet_path.append(output_parquet_filename)

            start_aid = aid_list[start_index]
            try:
                end_aid = aid_list[end_index]
            except:
                end_aid = aid_list[n_aids - 1]

            print(f"file_num,start_index,end_index,start_aid,end_aid={file_num},{start_index},{end_index},{start_aid},{end_aid}")

            query = f"SELECT {lc_astroobjects_cols} FROM astroobjects WHERE aid >= {start_aid} and aid <= {end_aid} order by aid;"
            sql_queries = []
            sql_queries.append(query)
            astroobjects_records = dbh.execute_sql_queries(sql_queries,debug)

            astroobjects_cols = lc_astroobjects_cols.split(",")
            sources_cols = lc_sources_cols.split(",")
            sources_cols.insert(0,astroobjects_cols[0])             # Join index (aid)

            astroobjects_data = {}
            sources_data = {}

            for col in astroobjects_cols:
                astroobjects_data[col] = []

            for col in sources_cols:
                sources_data[col] = []

            for astroobjects_record in astroobjects_records:

                aid = astroobjects_record[0]

                for col,val in zip(cols,astroobjects_record):
                    astroobjects_data[col] = append(val)

                query = f"SELECT a.{lc_sources_cols} FROM sources a, {merges_tablename} b " +\
                        f"WHERE a.sid = b.sid and aid = {aid} order by mjdobs;"
                sql_queries = []
                sql_queries.append(query)
                sources_records = dbh.execute_sql_queries(sql_queries,debug)

                for sources_record in sources_records:

                    for col,val in zip(cols,sources_record):
                        sources_data[col] = append(val)


            # Create the nested data frame and the data frame to join with it.
            #
            # E.g.,
            #
            # 1. Create a base NestedFrame (e.g., astronomical objects).
            # objects_data = {'object_id': [1, 2, 3], 'ra': [10.1, 20.2, 30.3], 'dec': [5.5, 6.6, 7.7]}
            # object_nf = NestedFrame(objects_data).set_index('object_id')
            #
            # 2. Create a flat DataFrame with associated nested data (e.g., measurements).
            # The index values (object_id) must repeat to match the top-level frame.
            # measurements_data = {
            #     'object_id': [1, 1, 2, 2, 3, 3, 3],
            #     'time': [100, 101, 200, 201, 300, 301, 302],
            #     'flux': [0.5, 0.6, 1.2, 1.3, 2.1, 2.2, 2.3]
            # }
            # source_df = pd.DataFrame(measurements_data).set_index('object_id')
            #
            # 3. Use join_nested to pack the 'source_df' into a new nested column.
            # object_nf = object_nf.join_nested(obj=source_df, name="nested_measurements")
            # print(object_nf)
            #              ra  dec                   nested_measurements
            # object_id
            # 1          10.1  5.5  [{time: 100, flux: 0.5}; …] (2 rows)
            # 2          20.2  6.6  [{time: 200, flux: 1.2}; …] (2 rows)
            # 3          30.3  7.7  [{time: 300, flux: 2.1}; …] (3 rows)

            astroobjects_nf = NestedFrame(astroobjects_data).set_index('aid')
            sources_df = pd.DataFrame(sources_data).set_index('aid')
            astroobjects_nf = astroobjects_nf.join_nested(obj=sources_df,name="nested_lc_data")


            # Convert DataFrame to an Apache Arrow Table, and write to a Parquet file.

            apache_arrow_table = pa.Table.from_pandas(astroobjects_nf)
            print("\nPyArrow Table Schema:")
            print(f"apache_arrow_table.schema = {apache_arrow_table.schema}")

            pq.write_table(apache_arrow_table, output_parquet_filename)
            print(f"\nSuccessfully wrote lightcurve data to '{output_parquet_filename}'")

            # Increment the start and end indexes.

            start_index = start_index + nrows_per_file
            end_index = start_index + nrows_per_file - 1

            j += 1


    # Close database connection.

    dbh.close()


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to dumping database records into parquet files =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Call method to generate the HATS catalog.

    generate_hats_catalog(catalog_parquet_path)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to generate HATS catalog =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Upload HATS catalog to S3 bucket.

    hat_copy_cmd = f"aws s3 sync {filepath_hats_catalog} {s3_object_name_hats_catalog}"
    util.execute_command_in_shell(hat_copy_cmd)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to upload HATS catalog to S3 bucket =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds from {swname} start to finish =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)





