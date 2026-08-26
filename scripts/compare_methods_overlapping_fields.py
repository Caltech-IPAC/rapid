import boto3
import os
import configparser
from astropy.io import fits
from astropy.wcs import WCS
from datetime import datetime, timezone
from dateutil import tz
import time
import matplotlib.pyplot as plt

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite

swname = "compare_methods_overlapping_fields.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Print diagnostics.

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


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
# Methods.
#-------------------------------------------------------------------------------------------------------------

def wrap_ra(ra,pivot_ra):

    """
    Shift sky positions east of the pivot by -360 degrees, so that fields
    straddling RA = 0/360 plot as contiguous outlines instead of streaking
    across the full RA range.
    """

    if ra > pivot_ra:
        return ra - 360.0

    return ra


def compute_pivot_ra(ra0):

    """
    Position the RA pivot on the opposite side of the sky from the region of
    interest, so that it never falls inside the plotted fields.
    """

    pivot_ra = 180.0

    if ra0 >= 170.0 and ra0 < 190.0:
        pivot_ra = 320.0

    return pivot_ra


def plot_sca_outlines(ra,dec,symbol="-"):

    for i in range(0,len(ra),4):
        ra_sca = []
        dec_sca = []

        ra_sca.append(ra[i])
        dec_sca.append(dec[i])

        ra_sca.append(ra[i+1])
        dec_sca.append(dec[i+1])

        ra_sca.append(ra[i+2])
        dec_sca.append(dec[i+2])

        ra_sca.append(ra[i+3])
        dec_sca.append(dec[i+3])

        ra_sca.append(ra[i])
        dec_sca.append(dec[i])

        my_symbol = symbol
        if i == 0:
            my_symbol = "o"

        plt.plot(ra_sca,dec_sca,my_symbol)


def query_for_field_corners(dbh, field):

    query = f"SELECT ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 " +\
            f"FROM fields " +\
            f"WHERE field = {field};"

    sql_queries = [query]

    try:
        records = dbh.execute_sql_queries(sql_queries,debug)
    except Exception as e:
        print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
              f"(e={e});  quitting...")
        dbh.close()
        exit(64)

    if dbh.exit_code >= 64:
        print(f"*** Error: Exception raised in dbh.execute_sql_queries;  quitting...")
        dbh.close()
        exit(dbh.exit_code)

    if not records:
        print(f"*** Error: No Fields record found for field = {field};  quitting...")
        dbh.close()
        exit(64)

    record = records[0]

    ra1 = record[0]
    dec1 = record[1]
    ra2 = record[2]
    dec2 = record[3]
    ra3 = record[4]
    dec3 = record[5]
    ra4 = record[6]
    dec4 = record[7]

    return ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4


def make_plot(dbh,ra0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,sciimage_field,fields):

    # All sky positions are wrapped about a common pivot, which is computed
    # from the science-image center.

    pivot_ra = compute_pivot_ra(ra0)

    ra = []
    dec = []

    ra.append(wrap_ra(ra1,pivot_ra))
    dec.append(dec1)

    ra.append(wrap_ra(ra2,pivot_ra))
    dec.append(dec2)

    ra.append(wrap_ra(ra3,pivot_ra))
    dec.append(dec3)

    ra.append(wrap_ra(ra4,pivot_ra))
    dec.append(dec4)

    for my_field in fields:

        ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = query_for_field_corners(dbh,my_field)

        ra.append(wrap_ra(ra1,pivot_ra))
        dec.append(dec1)

        ra.append(wrap_ra(ra2,pivot_ra))
        dec.append(dec2)

        ra.append(wrap_ra(ra3,pivot_ra))
        dec.append(dec3)

        ra.append(wrap_ra(ra4,pivot_ra))
        dec.append(dec4)


    plt.figure(figsize=(8, 8))

    plot_sca_outlines(ra,dec)

    plt.xlabel('Right Ascension (degrees)')
    plt.ylabel('Declination (degrees)')

    plt.title(f"Roman Neighboring Sky Tiles (field = {sciimage_field})")

    plt.show()



#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    '''
    Compare methods of finding all fields that a given science image overlaps.
    '''


    s3_client = boto3.client('s3')


    # Open database connections.

    roman_tessellation_db = sqlite.RomanTessellationNSIDE512()

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

        query = f"SELECT rid,filename FROM l2files WHERE vbest > 0 AND field = {field} limit 1;"

        sql_queries = []
        sql_queries.append(query)
        l2files_record = dbh.execute_sql_queries(sql_queries,debug)

        rid = l2files_record[0][0]
        filename = l2files_record[0][1]


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
        rtids_list.sort()
        print(f"Method 1: Fields returned by method get_overlapping_rtids = {rtids_list}")


        # Method 2
        # Alternatively, compute all fields that overlap the science image using get_all_neighboring_rtids method.
        # This method identifies all surrounding sky tiles (which should be a superset of the actual overlapping fields).

        neighboring_rtids = roman_tessellation_db.get_all_neighboring_rtids(field)

        sciimg_overlapping_rtids = [field]
        for neighboring_rtid in neighboring_rtids:
            sciimg_overlapping_rtids.append(neighboring_rtid)
        sciimg_overlapping_rtids.sort()
        print(f"Method 2: Fields returned by method get_all_neighboring_rtids (field = {field}) = {sciimg_overlapping_rtids}")


        # Find union using set operations

        union_list = list(set(rtids_list).union(sciimg_overlapping_rtids))
        union_list.sort()

        # Compare lists.

        set_a = set(rtids_list)
        set_b = set(sciimg_overlapping_rtids)

        result = [item for item in rtids_list if item not in set_b]
        result.sort()
        print(f"Fields returned by method get_overlapping_rtids that are not returned by method get_all_neighboring_rtids = {result}")

        result = [item for item in sciimg_overlapping_rtids if item not in set_a]
        result.sort()
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
        x_list.append(naxis1 - 1)
        y_list.append(naxis2 - 1)

        for y in y_list:
            for x in x_list:

                # x,y,crpix1,crpix2 must be zero-based.
                ra,dec = util.tan_proj2(x,y,crpix1-1,crpix2-1,crval1,crval2,cd11,cd12,cd21,cd22)

                roman_tessellation_db.get_rtid(ra,dec)
                rtid = roman_tessellation_db.rtid

                rtid_dict[rtid] = 1

        keys_list = list(rtid_dict.keys())
        keys_list.sort()
        print(f"Method 3: Fields overlapping image = {keys_list}")


        # Method #4

        print(f"Try downloading {filename}...")

        science_image_filename_gz,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,filename)

        if not downloaded_from_bucket:
            print(f"Error: L2 file not downloaded ({filename}); quitting...")
            exit(64)

        science_image_filename = science_image_filename_gz.replace(".fits.gz",".fits")

        gunzip_cmd = ['gunzip', '-f', science_image_filename_gz]
        exitcode_from_gunzip = util.execute_command(gunzip_cmd)

        hdul = fits.open(science_image_filename)
        hdr = hdul[0].header
        data = hdul[0].data
        hdul.close()

        wcs = WCS(hdr) # Initialize WCS object from FITS header

        rtid_dict_method4 = {}

        x_list = [*range(0,naxis1,500)]
        y_list = [*range(0,naxis2,500)]
        x_list.append(naxis1 - 1)
        y_list.append(naxis2 - 1)

        for y in y_list:
            for x in x_list:

                # x,y must be zero-based.
                celestial_coords = wcs.pixel_to_world(x, y)

                ra = celestial_coords.ra.deg
                dec = celestial_coords.dec.deg

                roman_tessellation_db.get_rtid(ra,dec)
                rtid = roman_tessellation_db.rtid

                rtid_dict_method4[rtid] = 1

        keys_list_method4 = list(rtid_dict_method4.keys())
        keys_list_method4.sort()
        print(f"Method 4: Fields overlapping image = {keys_list_method4}")


        # Plot fields and L2File footprint.

        make_plot(dbh,ra0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,field,sciimg_overlapping_rtids)

        break


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to compare methods of computing science-image overlapping fields =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed total time in seconds to execute {swname} =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connections.

    roman_tessellation_db.close()

    dbh.close()

    if dbh.exit_code >= 64:
         exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
