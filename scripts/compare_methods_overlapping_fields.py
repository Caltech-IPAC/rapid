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

swname = "compare_methods_overlapping_fields.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

python_cmd = '/usr/bin/python3.11'
generate_injection_catalog_code = '/code/modules/fake_src/generateInjectionCatalogForField.py'




upload_to_bucket = True




# Print diagnostics.

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)
print("python_cmd =", python_cmd)


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.astimezone(tz=to_zone)
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
debug = int(config_input['JOB_PARAMS']['debug'])

fake_sources_dict = config_input['FAKE_SOURCES']
injection_catalogs_subdir = fake_sources_dict['injection_catalogs_subdir']

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    '''
    Compare methods of finding all fields that a given science image overlaps.
    '''


    s3_client = boto3.client('s3')


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query RAPID operations database for the relevant fields.

    query = f"SELECT DISTINCT field FROM l2files WHERE vbest > 0 AND status > 0;"

    sql_queries = []
    sql_queries.append(query)
    records = dbh.execute_sql_queries(sql_queries,debug)


    # For the fields associated with L2Files records, find all fields that the
    # L2 file overlaps.

    for record in records:

        field = record[0]

        print(f"Start of loop: field = {field}")


        # Query RAPID operations database for representative science image,
        # in order to find the sky positions of its four corners.

        query = f"SELECT rid FROM l2files WHERE vbest > 0 AND field = {field} limit 1;"

        sql_queries = []
        sql_queries.append(query)
        l2files_record = dbh.execute_sql_queries(sql_queries,debug)

        rid = l2files_record[0][0]


        # Query database for associated L2FileMeta record.

        sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = dbh.get_l2filemeta_record(rid)

        if dbh.exit_code >= 64:
            print("*** Error from {}; quitting ".format(swname))
            exit(dbh.exit_code)


        # Method 1
        # Compute all fields that overlap the science image using get_overlapping_rtids method.
        # This method returns a tuple per record: (rtid,ramin,ramax,decmin,decmax);
        # E.g.,(4649964, 268.0224304199219, 268.1103515625, -28.588502883911133, -28.503568649291992)

        print(f"Method 1: Fields returned for science-image field = {field}")

        rtid_records_list = roman_tessellation_db.get_overlapping_rtids(ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4)

        rtids_list = []
        for rtid_record in rtid_records_list:
            rtid = rtid_record[0]
            rtids_list.append(rtid)
        print(f"Method 1: Fields returned by method get_overlapping_rtids = {rtids_list}")


        # Method 2
        # Alternatively, compute all fields that overlap the science image using get_all_neighboring_rtids method.
        # This method identifies all surrounding sky tiles (which should be a superset of the actual overlapping fields).

        neighboring_rtids = roman_tessellation_db.get_all_neighboring_rtids(field)

        sciimg_overlapping_rtids = [field]
        for neighboring_rtid in neighboring_rtids:
            sciimg_overlapping_rtids.append(neighboring_rtid)
        print(f"Method 2: Fields returned by method get_all_neighboring_rtids = {sciimg_overlapping_rtids}")


        # Find union using set operations

        union_list = list(set(rtids_list).union(neighboring_rtids))


        # Compare lists.

        set_a = set(rtids_list)
        set_b = set(sciimg_overlapping_rtids)

        result = [item for item in rtids_list if item not in set_b]
        print(f"Fields returned by method get_overlapping_rtids that are not returned by method get_all_neighboring_rtids = {result}")

        result = [item for item in sciimg_overlapping_rtids if item not in set_a]
        print(f"Fields returned by method get_all_neighboring_rtids that are not returned by method get_overlapping_rtids = {result}")

        print(f"Methods 1 and 2: Union of fields = {union_list}")


        # Method #3
        # Get field numbers (rtids) of all sky tiles containing sky positions
        # in given science image.

        l2file_dict = dbh.get_l2file_info_for_sources(rid)

        crval1 = l2file_dict['crval1']
        crval2 = l2file_dict['crval2']
        crpix1 = l2file_dict['crpix1']
        crpix2 = l2file_dict['crpix2']
        cd11 = l2file_dict['cd11']
        cd12 = l2file_dict['cd12']
        cd21 = l2file_dict['cd21']
        cd22 = l2file_dict['cd22']

        rtid_dict = {}

        x_list = [*range(0,naxis1,500)]
        y_list = [*range(0,naxis2,500)]
        x_list.append(naxis1)
        y_list.append(naxis2)

        for y in y_list:
            for x in x_list:

                # x,y,crpix1,crpix2 must be zero-based.
                ra,dec = util.tan_proj2(x,y,crpix1-1,crpix2-1,crval1,crval2,cd11,cd12,cd21,cd22)

                roman_tessellation_db.get_rtid(ra,dec)
                rtid = roman_tessellation_db.rtid

                rtid_dict[rtid] = 1

        keys_list = list(rtid_dict.keys())
        print("Method 3: Fields overlapping image =",keys_list)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to compare methods of computing science-image overlapping fields =",
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
