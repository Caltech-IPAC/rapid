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

swname = "generateInjectionCatalogsForOpenUnivSims.py"
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
debug = config_input['JOB_PARAMS']['debug']



#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    '''
    Generate all fake-source injection catalogs with fixed sky positions for the fields covered
    by the OpenUniverse sims and upload them to s3://rapid-pipeline-files/injection_catalogs.
    Field number is also known as rtid (Roman tessellation ID).
    '''


    s3_client = boto3.client('s3')


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query RAPID operations database for the relevant fields.

    query = f"SELECT DISTINCT(field) FROM refimages WHERE vbest>0;"

    sql_queries = []
    sql_queries.append(query)
    records = dbh.execute_sql_queries(sql_queries,debug)


    # For the sources that were matched, create Merges_<field> record.

    for record in records:

        field = record[0]

        print(f"Start of loop: field = {field}")


        # Query RAPID operations database for representative reference image,
        # in order to find the sky positions of its four corners.  This is,
        # unfortunately, because the sky positions are not stored in the database.

        query = f"SELECT filename FROM refimages WHERE vbest>0 AND field = {field} limit 1;"

        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        s3_full_name_reference_image = records[0][0]


        # Download reference image from S3 bucket.

        refimage_filename,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_reference_image)


        # Read FITS file.

        hdu_index_diff = 0

        with fits.open(refimage_filename) as hdul:

            hdr = hdul[hdu_index_diff].header

            wcs_refimg = WCS(hdr) # Initialize WCS object from FITS header

            naxis1_refimg = hdr['NAXIS1']
            naxis1_refimg = hdr['NAXIS2']

            print("naxis1_refimg,naxis1_refimg =",naxis1_refimg,naxis1_refimg)

            crpix1 = wcs_refimg.wcs.crpix[0]
            crpix2 = wcs_refimg.wcs.crpix[1]

            crval1 = hdr['CRVAL1']
            crval2 = hdr['CRVAL2']

            print(f"crval1,crval2 = {crval1},{crval2}")


        # Example of converting pixel coordinates to celestial coordinates
        # The following should reproduce CRVAL1,CRVAL2.

        pixel_x, pixel_y = crpix1 - 1, crpix2 - 1
        celestial_coords = wcs_refimg.pixel_to_world(pixel_x, pixel_y)
        print(f"CRVAL1,CRVAL2 Pixel ({pixel_x}, {pixel_y}) corresponds to {celestial_coords.ra.deg:.12f} RA and {celestial_coords.dec.deg:.12f} Dec.")


        # Compute pixel coordinates of ref-image center and four corners.

        x0,y0,x1,y1,x2,y2,x3,y3,x4,y4 = util.compute_pix_image_center_and_four_corners(naxis1_refimg,naxis1_refimg)


        # Compute sky coordinates of reference-image center and four corners.

        ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = \
            util.compute_sky_image_center_and_four_corners(wcs_refimg,x0,y0,x1,y1,x2,y2,x3,y3,x4,y4)



        # Compute all fields that overlap the reference image.

        rtid_records_list = roman_tessellation_db.get_overlapping_rtids(ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4)


        # Skip injection-catalog generation for given rtid in list if it
        # already exists in the S3 bucket.

        for rtid_record in rtid_records_list:

            rtid = rtid_record[0]

            s3_full_name_injection_catalog = f"s3://{job_info_s3_bucket_base}/injection_catalogs/injection_catalog_rtid{rtid}.json"

            print("Try downloading {s3_full_name_injection_catalog}...")

            injection_catalog_filename,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_injection_catalog)

            if downloaded_from_bucket:
                print("Injection catalog file {s3_full_name_injection_catalog} already exists; skipping...")
                continue


            # Launch script to generate injection catalog for field.

            generate_injection_catalog_cmd = [python_cmd,
                                              generate_injection_catalog_code,
                                              str(rtid)]

            exitcode_from_generate_injection_catalog_cmd = util.execute_command(generate_injection_catalog_cmd)


            # Upload fake-source injection catalog to product S3 bucket.

            s3_object_name_injection_catalog = "injection_catalogs/" + injection_catalog_filename

            util.upload_files_to_s3_bucket(s3_client,job_info_s3_bucket_base,[injection_catalog_filename],[s3_object_name_injection_catalog])


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        diff_time_benchmark = end_time_benchmark - start_time_benchmark
        fh.write(f"Elapsed time in seconds to compute all injection catalogs associated with field = {diff_time_benchmark}\n")
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
