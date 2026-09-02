'''
Load into database XSources table the SExtractor catalogs made from SFFT difference images.
'''

import boto3
import io
import os
import numpy as np
import healpy as hp
import configparser
from astropy.table import QTable
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures import ThreadPoolExecutor
import psycopg2
import ast

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


# Sources with image positions outside [xy_fit_min, naxis + xy_fit_max_offset]
# are rejected (in pixel units, for x against naxis1 and y against naxis2).
# Remember, SExtractor pixels are one-based.

tol = 0.0001                    # Fractional pixels
xy_fit_min = 0.5 - tol
xy_fit_max_offset = 0.5 + tol


swname = "loadSECatIntoDBSourcesTable.py"
swvers = "1.1"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

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

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Set DONTCHECKDONEFILE to skip existence-checking of the xsource_dbload_jid<jid>.done S3 bucket file.

skip_done_check = os.getenv('DONTCHECKDONEFILE')

do_done_check = False
if skip_done_check is None:
    do_done_check = True

print(f"do_done_check = {do_done_check}")


# Set SKIPLOADING to skip xsources child database table creation and bulk loading of xsources records.

skip_loading = os.getenv('SKIPLOADING')

do_loading = False
if skip_loading is None:
    do_loading = True

print(f"do_loading = {do_loading}")


# Print out basic information for log file.

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
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

crossconv_flag = ast.literal_eval(config_input['SFFT']['crossconv_flag'])

if crossconv_flag:
    output_secat_filename = 'sfftdiffimage_cconv_masked.txt'
else:
    output_secat_filename = 'sfftdiffimage_masked.txt'


# An extra row and column has been added to SFFT input images.

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage']) + 1
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage']) + 1

ppid = int(config_input['SCI_IMAGE']['ppid'])


# Set debug = 1 here to get debug messages in main program.

debug = 1


# Get number of cores for parallel processing.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# Define columns to read from SExtractor catalogs.

params_to_get = ["NUMBER",
                 "FLAGS",
                 "ALPHAWIN_J2000",
                 "DELTAWIN_J2000",
                 "XWIN_IMAGE",
                 "YWIN_IMAGE",
                 "AWIN_WORLD",
                 "BWIN_WORLD",
                 "AWIN_IMAGE",
                 "BWIN_IMAGE",
                 "FWHM_IMAGE",
                 "CLASS_STAR",
                 "FLUX_APER",
                 "FLUX_APER_1",
                 "FLUX_APER_2",
                 "FLUX_APER_3",
                 "FLUX_APER_4",
                 "FLUX_APER_5",
                 "FLUXERR_APER",
                 "FLUXERR_APER_1",
                 "FLUXERR_APER_2",
                 "FLUXERR_APER_3",
                 "FLUXERR_APER_4",
                 "FLUXERR_APER_5",
                ]


# Define columns to be populated in xsources tables.

cols = []
cols.append("num")
cols.append("pid")
cols.append("isdiffpos")
cols.append("ra")
cols.append("dec")
cols.append("x")
cols.append("y")
cols.append("fluxap")
cols.append("fluxap1")
cols.append("fluxap2")
cols.append("fluxap3")
cols.append("fluxap4")
cols.append("fluxap5")
cols.append("fluxerrap")
cols.append("fluxerrap1")
cols.append("fluxerrap2")
cols.append("fluxerrap3")
cols.append("fluxerrap4")
cols.append("fluxerrap5")
cols.append("awinworld")
cols.append("bwinworld")
cols.append("awinimage")
cols.append("bwinimage")
cols.append("fwhmimage")
cols.append("classstar")
cols.append("flags")
cols.append("field")
cols.append("hp6")
cols.append("hp9")
cols.append("expid")
cols.append("fid")
cols.append("sca")
cols.append("mjdobs")

cols_comma_separated_string = ", ".join(cols)
columns = tuple(cols)

print(f"XSources columns: {cols_comma_separated_string}")


# Get database connection parameters from environment parallel index generation.

dbport = os.getenv('DBPORT')
dbname = os.getenv('DBNAME')
dbuser = os.getenv('DBUSER')
dbpass = os.getenv('DBPASS')
dbserver = os.getenv('DBSERVER')

print("dbserver,dbname,dbport,dbuser =",dbserver,dbname,dbport,dbuser)

if dbport is None:
    print("*** Error: Env. var. DBPORT not set; quitting...")
    exit(64)

if dbname is None:
    print("*** Error: Env. var. DBNAME not set; quitting...")
    exit(64)

if dbuser is None:
    print("*** Error: Env. var. DBUSER not set; quitting...")
    exit(64)

if dbpass is None:
    print("*** Error: Env. var. DBPASS not set; quitting...")
    exit(64)

if dbserver is None:
    print("*** Error: Env. var. DBSERVER not set; quitting...")
    exit(64)


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def execute_sql_queries_for_given_sca(sql_queries_dict,obs_date,sca):


    # Connect to database.  Each thread MUST have its own independent database connection for parallelism.

    try:
        conn = psycopg2.connect(host=dbserver,database=dbname,port=dbport,user=dbuser,password=dbpass)
    except Exception:
        print("*** Error: Could not connect to database; quitting...")
        exitcode = 64
        return exitcode


    # Execute the SQL query.

    try:

        sql_queries_key = (obs_date,sca)
        sql_queries = sql_queries_dict[sql_queries_key]

        for sql_query in sql_queries:

            with conn.cursor() as cur:
                print(f"Starting: {sql_query}")
                cur.execute(sql_query)
                print(f"Finished: {sql_query}")
                conn.commit()           # Commit database transaction

    except Exception as e:
        print(f"Error running query for {sql_queries_key}: {e}")
        conn.rollback()                             # Rollback database transaction
        exitcode = 64
        return exitcode
    finally:
        conn.close()

    exitcode = 0
    return exitcode


def write_secat_qtable_to_csv_file(isdiffpos,
                                   expid,
                                   sca,
                                   fid,
                                   mjdobs,
                                   pid,
                                   csv_fh,
                                   secat_qtable,
                                   hp6_arr,
                                   hp9_arr,
                                   roman_tessellation_db):

    nrows = len(secat_qtable)
    if nrows == 0:
        return


    # Reject xsources with image positions outside the acceptable pixel range.
    # NaN fit positions compare False here, and so are rejected as well.
    # The healpix arrays are row-aligned with the table, so mask them too.

    XWIN_IMAGE_arr = np.array(secat_qtable['XWIN_IMAGE'], dtype=np.float64)
    YWIN_IMAGE_arr = np.array(secat_qtable['YWIN_IMAGE'], dtype=np.float64)

    keep = ((XWIN_IMAGE_arr >= xy_fit_min) & (XWIN_IMAGE_arr <= naxis1 + xy_fit_max_offset) &
            (YWIN_IMAGE_arr >= xy_fit_min) & (YWIN_IMAGE_arr <= naxis2 + xy_fit_max_offset))

    nkeep = int(np.count_nonzero(keep))

    print(f"write_secat_qtable_to_csv_file: isdiffpos={isdiffpos}, "
          f"rejected {nrows - nkeep} of {nrows} xsources with out-of-range xfit,yfit")

    if nkeep == 0:
        return

    if nkeep < nrows:
        secat_qtable = secat_qtable[keep]
        hp6_arr = hp6_arr[keep]
        hp9_arr = hp9_arr[keep]
        nrows = nkeep


    # Column mapping (catalog name -> db name) applied once
    t = secat_qtable
    ra_arr = np.array(t['ALPHAWIN_J2000'], dtype=np.float64)
    dec_arr = np.array(t['DELTAWIN_J2000'], dtype=np.float64)

    # Vectorize the rtid lookup instead of one SQLite query per row
    field_arr = roman_tessellation_db.get_rtids(ra_arr, dec_arr)

    # Build entire CSV block at once using numpy column stacking
    data = np.column_stack([
        np.array(t['NUMBER']),
        np.full(nrows, pid), np.full(nrows, isdiffpos, dtype=object),
        ra_arr, dec_arr,
        np.array(t['XWIN_IMAGE']), np.array(t['YWIN_IMAGE']),
        np.array(t['FLUX_APER']),
        np.array(t['FLUX_APER_1']),
        np.array(t['FLUX_APER_2']),
        np.array(t['FLUX_APER_3']),
        np.array(t['FLUX_APER_4']),
        np.array(t['FLUX_APER_5']),
        np.array(t['FLUXERR_APER']),
        np.array(t['FLUXERR_APER_1']),
        np.array(t['FLUXERR_APER_2']),
        np.array(t['FLUXERR_APER_3']),
        np.array(t['FLUXERR_APER_4']),
        np.array(t['FLUXERR_APER_5']),
        np.array(t['AWIN_WORLD']), np.array(t['BWIN_WORLD']),
        np.array(t['AWIN_IMAGE']), np.array(t['BWIN_IMAGE']),
        np.array(t['FWHM_IMAGE']),
        np.array(t['CLASS_STAR']),
        np.array(t['FLAGS']),
        field_arr, hp6_arr, hp9_arr,
        np.full(nrows, expid), np.full(nrows, fid),
        np.full(nrows, sca), np.full(nrows, mjdobs),
    ])

    np.savetxt(csv_fh, data, delimiter=',', fmt='%s')


def run_single_core_job(jids,meta_list,index_thread):

    '''
    For efficiency, this method handles both positive and negative difference-image
    PSF-fits catalogs.
    '''


    # Get S3 client.

    s3_client = boto3.client('s3')

    njobs = len(jids)

    print("index_thread,njobs =",index_thread,njobs)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    fh = None
    dbh = None

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")


        # Open database connections.

        roman_tessellation_db = sqlite.RomanTessellationNSIDE512()

        dbh = db.RAPIDDB()

        if dbh.exit_code >= 64:
            fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
            fh.flush()
            raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")


        fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")

        my_jobs = list(range(index_thread, njobs, num_cores))
        for index_job in my_jobs:

            jid = jids[index_job]
            meta_dict = meta_list[index_job]

            fh.write(f"Loop start: index_job,jid = {index_job},{jid}\n")

            jid_from_dict = meta_dict["jid"]

            if jid != jid_from_dict:
                fh.write(f"*** Error: jid is not equal to jid from meta dictionary; quitting...\n")
                fh.flush()
                raise RuntimeError(f"*** Error: jid is not equal to jid from meta dictionary; quitting...\n")

            expid = meta_dict["expid"]
            sca = meta_dict["sca"]
            fid = meta_dict["fid"]
            field = meta_dict["field"]
            hp6 = meta_dict["hp6"]
            hp9 = meta_dict["hp9"]
            mjdobs = meta_dict["mjdobs"]
            dateobs = meta_dict["dateobs"]
            pid = meta_dict["pid"]

            obs_date = str(dateobs).split()[0].replace("-","")


            # Check whether done file exists in S3 bucket for job, and skip if it exists.
            # This is done by attempting to download the done file.  Regardless the sub
            # always returns the filename and subdirs by parsing the s3_full_name.

            s3_full_name_done_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/xsource_dbload"  + "_jid" +  str(jid)  + ".done"
            done_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

            if do_done_check and downloaded_from_bucket:
                os.remove(done_filename)
                fh.write("*** Warning: Done file exists ({}); skipping...\n".format(done_filename))
                fh.flush()
                continue


            # Parallel S3-bucket downloads:
            # 1. SFFT-difference-image PSF-fit catalog file for positive difference image
            # 2. SFFT-difference-image PSF-fit catalog file for negative difference image
            #
            # dl_executor returns tuples.  E.g.,
            # ret_secat = ('sfftdiffimage_masked_secat_jid130875.txt', '20260722/jid130875', True)
            # ret_secat = ('sfftdiffimage_masked_secat_negative_jid130875.txt', '20260722/jid130875', True)


            # isdiffpos = "true"
            output_secat_filename_to_use = output_secat_filename

            output_secat_filename_for_jid = output_secat_filename_to_use.replace(".txt",f"_jid{jid}.txt")

            s3_full_name_secat_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/" +  output_secat_filename_to_use


            # isdiffpos = "false"
            output_secat_filename_negative_to_use = output_secat_filename.replace(".txt","_negative.txt")


            output_secat_filename_negative_for_jid = output_secat_filename_negative_to_use.replace(".txt",f"_jid{jid}.txt")

            s3_full_name_secat_file_negative = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/" +  output_secat_filename_negative_to_use


            # Perform parallel S3-bucket downloads:

            s3_client_1 = boto3.client('s3')
            s3_client_2 = boto3.client('s3')

            with ThreadPoolExecutor(max_workers=4) as dl_executor:
                future_secat = dl_executor.submit(util.download_file_from_s3_bucket, s3_client_1, s3_full_name_secat_file, output_secat_filename_for_jid)
                future_secat_negative = dl_executor.submit(util.download_file_from_s3_bucket, s3_client_2, s3_full_name_secat_file_negative, output_secat_filename_negative_for_jid)

            ret_secat = future_secat.result()
            ret_secat_negative = future_secat_negative.result()

            fh.write(f"ret_secat = {ret_secat}\n")
            fh.write(f"ret_secat_negative = {ret_secat_negative}\n")

            downloaded_from_bucket_secat = ret_secat[2]
            downloaded_from_bucket_secat_negative = ret_secat_negative[2]

            if not downloaded_from_bucket_secat:
                fh.write("*** Warning: Positive difference-image PSF-fit catalog file does not exist ({}); skipping...\n".format(output_secat_filename_to_use))
                fh.flush()
                continue

            if not downloaded_from_bucket_secat_negative:
                fh.write("*** Warning: Negative difference-image PSF-fit catalog file does not exist ({}); skipping...\n".format(output_secat_filename_negative_to_use))
                fh.flush()
                continue


            # Parse positive difference-image catalog and extract columns for xsources database tables.

            secat_qtable = QTable.read(output_secat_filename_for_jid,format='ascii.sextractor',
                                       fast_reader=True, include_names=params_to_get)

            nrows = len(secat_qtable)
            fh.write(f"nrows in positive difference-image PSF-fit catalog = {nrows}\n")


            # Vectorize hp.ang2pix calls for positive difference-image catalogs.

            ra_arr = np.array(secat_qtable['ALPHAWIN_J2000'], dtype=np.float64)
            dec_arr = np.array(secat_qtable['DELTAWIN_J2000'], dtype=np.float64)

            hp6_arr = hp.ang2pix(nside6, ra_arr, dec_arr, nest=True, lonlat=True)
            hp9_arr = hp.ang2pix(nside9, ra_arr, dec_arr, nest=True, lonlat=True)


            # Parse negative difference-image catalog and extract columns for xsources database tables.

            secat_qtable_negative = QTable.read(output_secat_filename_negative_for_jid,format='ascii.sextractor',
                                                fast_reader=True, include_names=params_to_get)

            nrows = len(secat_qtable_negative)
            fh.write(f"nrows in negative difference-image PSF-fit catalog = {nrows}\n")


            # Vectorize hp.ang2pix calls for negative difference-image catalogs.

            ra_arr_negative = np.array(secat_qtable_negative['ALPHAWIN_J2000'], dtype=np.float64)
            dec_arr_negative = np.array(secat_qtable_negative['DELTAWIN_J2000'], dtype=np.float64)

            hp6_arr_negative = hp.ang2pix(nside6, ra_arr_negative, dec_arr_negative, nest=True, lonlat=True)
            hp9_arr_negative = hp.ang2pix(nside9, ra_arr_negative, dec_arr_negative, nest=True, lonlat=True)


            # The columns in the SExtractor catalogs are defined here: rapid/cdf/rapidSexParamsDiffImage.inp
            # Note that some catalog-column names have underscores that need to be dealt with specially
            # because the database columns do not have underscores.
            #
            # Prepare records for loading into xsources database tables.

            xsources_table = f"xsources_{obs_date}_{sca}"

            csv_fh = io.StringIO()

            isdiffpos = "true"
            write_secat_qtable_to_csv_file(isdiffpos,expid,sca,fid,mjdobs,pid,csv_fh,secat_qtable,hp6_arr,hp9_arr,roman_tessellation_db)

            isdiffpos = "false"
            write_secat_qtable_to_csv_file(isdiffpos,expid,sca,fid,mjdobs,pid,csv_fh,secat_qtable_negative,hp6_arr_negative,hp9_arr_negative,roman_tessellation_db)


            # Load records into xsources database tables.

            dbh.copy_data_from_buffer_into_database(csv_fh,xsources_table,columns)

            csv_fh.close()

            if dbh.exit_code >= 64:
                fh.write(f"*** Error bulk-loading data into specified database table ({xsources_table}); quitting...\n")
                fh.flush()
                raise RuntimeError(f"*** Error bulk-loading data into specified database table ({xsources_table}); quitting...\n")


            # Touch done file.  Upload done file to S3 bucket.

            util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,proc_date,jid,s3_client)

            fh.write(f"Loop end: done_filename,product_s3_bucket_base,proc_date,jid = {done_filename},{product_s3_bucket_base},{proc_date},{jid}\n")


            # Flush write buffer.

            fh.flush()


            # Remove no-longer-needed intermediate files.

            file_paths = [output_secat_filename_for_jid,
                          output_secat_filename_negative_for_jid]
            for file_path in file_paths:

                if os.path.exists(file_path):
                    os.remove(file_path)
                    fh.write(f"File deleted successfully ({file_path})...\n")
                    fh.flush()
                else:
                    fh.write(f"The file does not exist({file_path})...\n")
                    fh.flush()


            # End of loop over job ID.


    except Exception as e:
        print(f"*** Error in method run_single_core_job {thread_work_file} ({e}); quitting...")
        raise

    finally:

        if dbh is not None:

            # Close database connections.

            roman_tessellation_db.close()

            dbh.close()

            if dbh.exit_code >= 64:
                fh.write(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
                fh.flush()
                fh.close()
                raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")

        if fh is not None:
            fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")
            fh.close()


    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(jids,meta_list,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,jids,meta_list,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    failures = []
    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            failures.append(e)
            print(f"*** Error in thread index {index} = {e}")

    if failures:
        print(f"*** Error(s) from {len(failures)} worker(s); quitting...")
        exit(64)


#################
# Main program.
#################

if __name__ == '__main__':


    #
    # Launch parallel tasks to load xsources database tables
    # for all RAPID science pipelines that already
    # ran on a given processing date.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Look up for the given processing date the Sources child table names
    # that need to be loaded (some or all of such tables may need to be created).

    xsources_tables_to_load_tuples_list,_,jid_list,meta_list = \
        util.lookup_source_tables_to_crossmatch_and_distinct_fields(dbh,proc_date,ppid)

    if len(xsources_tables_to_load_tuples_list) == 0:
        print(f"*** Error: No Sources child tables to be loaded;  quitting...")
        dbh.close()
        exit(7)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Assume astroobjects_<field> and merges_<field> database tables are created in tandem,
    # so we only need to test for the existence of the former table.

    already_made_dict = {}

    for table_load_tuple in xsources_tables_to_load_tuples_list:

        obs_date = table_load_tuple[0]
        sca = table_load_tuple[1]

        tablename1 = f"xsources_{obs_date}_{sca}"

        sql_queries = []
        sql_queries.append(f"SELECT to_regclass('public.{tablename1}') IS NOT NULL;")

        try:
            records = dbh.execute_sql_queries(sql_queries,debug)
        except Exception as e:
            print(f"*** Error: Exception raised in dbh.execute_sql_queries " +
                  f"(e={e});  quitting...")
            dbh.close()
            exit(64)

        if dbh.exit_code >= 64:
            print("*** Error from {}; quitting ".format(swname))
            dbh.close()
            exit(dbh.exit_code)

        table_exists_flag = records[0][0]

        already_made_dict[table_load_tuple] = table_exists_flag


    # Optionally skip xsources child database table creation and bulk loading of xsources records,
    # and just do the indexing, clustering, and applying grants to xsources database tables for
    # all SCAs associated with observing date...")

    if do_loading and jid_list:

        for table_load_tuple in xsources_tables_to_load_tuples_list:

            obs_date = table_load_tuple[0]
            sca = table_load_tuple[1]

            table_exists_flag = already_made_dict[table_load_tuple]

            if table_exists_flag:
                print(f"XSources_<obs_date>_<sca> database table has already been made " +
                      f" for obs_date={obs_date} and sca={sca}; continuing...")
                continue


            # Create xsources database tables for all SCAs associated with observing dates
            # that are covered under the processing date.

            print("Creating xsources database tables for all SCAs associated with processing date...")

            sql_queries = []
            sql_queries.append("SET default_tablespace = pipeline_data_01;")

            tablename = f"xsources_{obs_date}_{sca}"

            sql_queries.append(f"CREATE TABLE {tablename} (LIKE xsources " +
                               f"INCLUDING DEFAULTS INCLUDING CONSTRAINTS);")
            sql_queries.append(f"ALTER TABLE {tablename} OWNER TO rapidporole;")
            sql_queries.append(f"ALTER TABLE {tablename} SET UNLOGGED;")
            sql_queries.append(f"ALTER TABLE {tablename} INHERIT xsources;")

            dbh.execute_sql_queries(sql_queries)

            if dbh.exit_code >= 64:
                exit(dbh.exit_code)


        # Close main-program database connection before long episode of bulk-loading xsource records.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds to create xsources database tables for all SCAs associated with processing date =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        ################################################################################
        # Execute xsources-table-loading tasks for all science-pipeline jobs with jids on
        # a given processing date.  The execution is done in parallel, with the number
        # of parallel threads equal to the number of cores on the job-launcher machine.
        # First do for xsources from positive difference images, and then from negative.
        ################################################################################

        if num_cores > 1:
            execute_parallel_processes(jid_list,meta_list,num_cores)
        else:
            thread_index = 0
            run_single_core_job(jid_list,meta_list,thread_index)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds to load all xsources database tables =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark

    else:


        # Close main-program database connection.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


    # Reopen main-program database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    if jid_list:


        # Index xsources database tables for all SCAs associated with processing date.

        print("Indexing xsources database tables for all SCAs associated with processing date...")


        # Define the various CREATE INDEX queries for different tables.  These cannot be
        # run in parallel on the same table.  Execute them using parallel worker threads.

        sql_queries_dict = {}

        xsources_tables_to_index_tuples_list = []

        for table_load_tuple in xsources_tables_to_load_tuples_list:

            obs_date = table_load_tuple[0]
            sca = table_load_tuple[1]

            table_exists_flag = already_made_dict[table_load_tuple]

            if table_exists_flag:
                print(f"Sources_<obs_date>_<sca> database table has already been indexed " +
                      f" for obs_date={obs_date} and sca={sca}; continuing...")
                continue

            xsources_tables_to_index_tuples_list.append(table_load_tuple)

            sql_queries = []
            sql_queries.append("SET default_tablespace = pipeline_indx_01;")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_pid_idx ON xsources_{obs_date}_{sca} (pid);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_expid_idx ON xsources_{obs_date}_{sca} (expid);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_sca_idx ON xsources_{obs_date}_{sca} (sca);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_field_idx ON xsources_{obs_date}_{sca} (field);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_flags_idx ON xsources_{obs_date}_{sca} (flags);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_mjdobs_idx ON xsources_{obs_date}_{sca} (mjdobs);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_xsid_idx ON xsources_{obs_date}_{sca} (xsid);")
            sql_queries.append(f"CREATE INDEX xsources_{obs_date}_{sca}_radec_idx ON xsources_{obs_date}_{sca} (q3c_ang2ipix(ra, dec));")
            table_create_key = (obs_date,sca)
            sql_queries_dict[table_create_key] = sql_queries

        if xsources_tables_to_index_tuples_list:
            futures = []
            with ThreadPoolExecutor(max_workers = min(num_cores,len(xsources_tables_to_index_tuples_list))) as executor:
                for table_index_tuple in xsources_tables_to_index_tuples_list:
                    obs_date = table_index_tuple[0]
                    sca = table_index_tuple[1]
                    futures.append(executor.submit(execute_sql_queries_for_given_sca, sql_queries_dict, obs_date, sca))

            for future in futures:
                exitcode = future.result()
                if exitcode >= 64:
                    print(f"*** Error: Index creation failed (exitcode={exitcode}); quitting...")
                    dbh.close()
                    exit(exitcode)


        # Cluster, analyze, and apply grants to xsources database tables for all SCAs associated with processing date.

        print("Clustering, analyzing, and applying grants to xsources database tables for all SCAs associated with processing date...")

        sql_queries = []
        for table_load_tuple in xsources_tables_to_load_tuples_list:
            obs_date = table_load_tuple[0]
            sca = table_load_tuple[1]

            sql_queries.append(f"CLUSTER xsources_{obs_date}_{sca} USING xsources_{obs_date}_{sca}_radec_idx;")
            sql_queries.append(f"ANALYZE xsources_{obs_date}_{sca};")
            #sql_queries.append(f"ALTER TABLE xsources_{obs_date}_{sca} SET LOGGED;")                  # For speed, do not log.
            sql_queries.append(f"REVOKE ALL ON TABLE xsources_{obs_date}_{sca} FROM rapidreadrole;")
            sql_queries.append(f"GRANT SELECT ON TABLE xsources_{obs_date}_{sca} TO GROUP rapidreadrole;")
            sql_queries.append(f"REVOKE ALL ON TABLE xsources_{obs_date}_{sca} FROM rapidadminrole;")
            sql_queries.append(f"GRANT ALL ON TABLE xsources_{obs_date}_{sca} TO GROUP rapidadminrole;")
            sql_queries.append(f"REVOKE ALL ON TABLE xsources_{obs_date}_{sca} FROM rapidporole;")
            sql_queries.append(f"GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE xsources_{obs_date}_{sca} TO rapidporole;")

        dbh.execute_sql_queries(sql_queries)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds to index, cluster, and apply grants to xsources database tables for all SCAs associated with processing date =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to load all xsources into database for {proc_date} =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
