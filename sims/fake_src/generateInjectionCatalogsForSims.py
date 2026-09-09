import boto3
import os
import configparser
from astropy.io import fits
from astropy.wcs import WCS
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite
from pipeline.runtime.process import run_tool

swname = "generateInjectionCatalogsForSims.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

python_cmd = '/usr/bin/python3.11'
generate_injection_catalog_code = '/code/modules/fake_src/generateInjectionCatalogForField.py'


# Print diagnostics.

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)
print("python_cmd =", python_cmd)


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')
proc_date = datetime_pt_now.strftime('%Y%m%d')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)
# Processing date is always in Pacific time zone.
print("proc_date =",proc_date)



# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


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
# int, not the raw string (dev 1e3e02da): configparser returns "0", which is
# truthy, so SQL debug logging could never be turned off from the .ini.
debug = int(config_input['JOB_PARAMS']['debug'])

# Bucket subdirectory holding the per-field injection catalogues (dev 1e3e02da);
# a new catalogue set is a new subdirectory, so it is configuration, not code.
fake_sources_dict = config_input['FAKE_SOURCES']
injection_catalogs_subdir = fake_sources_dict['injection_catalogs_subdir']

# Set False to generate catalogues locally without publishing them (dev 7766b3bf).
upload_to_bucket = True



#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    '''
    Generate all fake-source injection catalogs with fixed sky positions for the fields covered
    by the simulations and upload them to s3://<job_info_s3_bucket_base>/<injection_catalogs_subdir>,
    both from the .ini ([JOB_PARAMS] and [FAKE_SOURCES]).
    Field number is also known as rtid (Roman tessellation ID).
    '''


    s3_client = boto3.client('s3')


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query RAPID operations database for the relevant fields.

    query = f"SELECT DISTINCT field FROM l2files WHERE vbest>0 AND status>0;"

    sql_queries = []
    sql_queries.append(query)
    records = dbh.execute_sql_queries(sql_queries,debug)


    # For the sources that were matched, create Merges_<field> record.

    for record in records:

        field = record[0]

        print(f"Start of loop: field = {field}")


        # Query RAPID operations database for representative science image,
        # in order to find the sky positions of its four corners.

        query = f"SELECT rid FROM l2files WHERE vbest>0 AND field = {field} limit 1;"

        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        rid = records[0][0]


        # Query database for associated L2FileMeta record.

        sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = dbh.get_l2filemeta_record(rid)

        if dbh.exit_code >= 64:
            print("*** Error from {}; quitting ".format(swname))
            exit(dbh.exit_code)


        # Compute all fields that overlap the science image, two ways, and
        # generate catalogues for the UNION (Russ Laher, dev b0124922 through
        # 84c95a6c; only the chain's end state is carried, the intermediate
        # commits each shipped a bug the next one fixed).
        #
        # get_overlapping_rtids returns one tuple per tile that the bounding
        # box of the five input points touches: (rtid, ramin, ramax, decmin,
        # decmax), e.g. (4649964, 268.02243, 268.11035, -28.58850, -28.50357).
        # get_all_neighboring_rtids returns the ring of tiles around the
        # image's own field, which should be a superset of the true overlap.
        # Neither is exact for a rotated footprint, hence the union and the
        # two set-difference diagnostics; compare_methods_overlapping_fields.py
        # is the study of which method to keep.

        rtid_records_list = roman_tessellation_db.get_overlapping_rtids(ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4)

        print(f"Fields returned for science-image field = {field}")

        rtids_list = [rtid_record[0] for rtid_record in rtid_records_list]
        print(f"Fields returned by method get_overlapping_rtids = {rtids_list}")

        neighboring_rtids = roman_tessellation_db.get_all_neighboring_rtids(field)
        sciimg_overlapping_rtids = [field] + list(neighboring_rtids)
        print(f"Fields returned by method get_all_neighboring_rtids = {sciimg_overlapping_rtids}")

        union_list = sorted(set(rtids_list).union(sciimg_overlapping_rtids))

        set_a = set(rtids_list)
        set_b = set(sciimg_overlapping_rtids)
        print("Fields returned by method get_overlapping_rtids that are not returned by method get_all_neighboring_rtids = " +
              f"{[item for item in rtids_list if item not in set_b]}")
        print("Fields returned by method get_all_neighboring_rtids that are not returned by method get_overlapping_rtids = " +
              f"{[item for item in sciimg_overlapping_rtids if item not in set_a]}")


        # Skip injection-catalog generation for given rtid in list if it
        # already exists in the S3 bucket.

        for rtid in union_list:

            # Dev 1e3e02da wrote the literal text "injection_catalogs_subdir/" into
            # this URL (no braces), so the existence check never hit and every
            # catalogue was regenerated on every run; never fixed on dev.
            s3_full_name_injection_catalog = f"s3://{job_info_s3_bucket_base}/{injection_catalogs_subdir}/injection_catalog_rtid{rtid}.json"

            print(f"Try downloading {s3_full_name_injection_catalog}...")

            injection_catalog_filename,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_injection_catalog)

            if downloaded_from_bucket:
                print(f"Injection catalog file {s3_full_name_injection_catalog} already exists; skipping...")
                continue


            # Launch script to generate injection catalog for field.

            generate_injection_catalog_cmd = [python_cmd,
                                              generate_injection_catalog_code,
                                              str(rtid)]

            run_tool(generate_injection_catalog_cmd)


            # Optionally upload fake-source injection catalog to product S3 bucket.

            if upload_to_bucket:

                s3_object_name_injection_catalog = f"{injection_catalogs_subdir}/" + injection_catalog_filename

                util.upload_files_to_s3_bucket(s3_client,job_info_s3_bucket_base,[injection_catalog_filename],[s3_object_name_injection_catalog])


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        diff_time_benchmark = end_time_benchmark - start_time_benchmark
        print(f"Elapsed time in seconds to compute all injection catalogs associated with field = {diff_time_benchmark}")
        start_time_benchmark = end_time_benchmark

        print(f"End of loop: field = {field}")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to generate all injection catalogs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed total time in seconds to execute {swname} =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
         exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
