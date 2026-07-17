'''
Load into database sources table the PSF-fit catalogs made by the
Python photutils package from the SFFT difference images
(until a final decision on which image-differencing method is best):
'''

import boto3
import os
import numpy as np
import healpy as hp
import configparser
from astropy.table import QTable, join
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


swname = "loadPSFCatIntoDBSourcesTable.py"
swvers = "1.1"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

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


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Set DONTCHECKDONEFILE to skip existence-checking of the source_dbload_jid<jid>.done S3 bucket file.

skip_done_check = os.getenv('DONTCHECKDONEFILE')

do_done_check = False
if skip_done_check is None:
    do_done_check = True

print(f"do_done_check = {do_done_check}")


# Set SKIPLOADING to skip sources child database table creation and bulk loading of sources records.

skip_loading = os.getenv('SKIPLOADING')

do_loading = False
if skip_loading is None:
    do_loading = True

print(f"do_loading = {do_loading}")


# Print out basic information for log file.

print("proc_date =",proc_date)


# Ensure sqlite database that defines the Roman sky tessellation is available.
# TODO Decide if we need this
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
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_sfft_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_sfft_psfcat_finder_filename'])

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])

ppid = int(config_input['SCI_IMAGE']['ppid'])
match_radius = float(config_input['SOURCE_MATCHING']['match_radius'])


# Open database connections for parallel access.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores) #TODO default to 18 max?

print("num_cores =",num_cores)

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


# Get S3 client.

s3_client = boto3.client('s3')


# Define columns to be populated in sources tables.

cols = []
cols.append("id")
cols.append("ra")
cols.append("dec")
cols.append("xfit")
cols.append("yfit")
cols.append("fluxfit")
cols.append("xerr")
cols.append("yerr")
cols.append("fluxerr")
cols.append("npixfit")
cols.append("qfit")
cols.append("cfit")
cols.append("redchi")
cols.append("flags")
cols.append("sharpness")
cols.append("roundness1")
cols.append("roundness2")
cols.append("npix")
cols.append("peak")
cols.append("pid")
cols.append("isdiffpos")
cols.append("field") #TODO decide if I want (or can get) this field
cols.append("hp6")
cols.append("hp9")
cols.append("expid")
cols.append("fid")
cols.append("sca")
cols.append("mjdobs")

cols_comma_separated_string = ", ".join(cols)
columns = tuple(cols)

print(f"Sources columns: {cols_comma_separated_string}")

#-------------------------------------------------------------------------------------------------------------
# Convert a QTable to a buffer for bulk database ingest
#-------------------------------------------------------------------------------------------------------------
def table_to_buffer(tbl, colnames, null_string=r"\N", separator=","):
    cols = []
    for name in colnames:
        column_data = np.asarray(tbl[name])
        #use %r to create a string with minimum number of digits
        column_data_str = np.char.mod('%r', column_data) if column_data.dtype.kind == 'f' else column_data.astype(str)
        if column_data.dtype.kind == 'f':
            s = np.where(np.isnan(column_data), null_string, column_data_str)      # NaN -> NULL at the source
        cols.append(column_data_str)
    buf = io.StringIO()
    buf.write("\n".join(separator.join(row) for row in zip(*cols)))
    buf.write("\n")
    buf.seek(0)
    return buf

#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(jid,meta_list,negative_diffimg_flag,index_thread):


    # Handle sources from positive versus negative difference images.

    print("negative_diffimg_flag =",negative_diffimg_flag)

    if negative_diffimg_flag:
        isdiffpos = "false"
        output_psfcat_filename_to_use = output_psfcat_filename.replace(".txt","_negative.txt")
        output_psfcat_finder_filename_to_use = output_psfcat_finder_filename.replace(".txt","_negative.txt")
        done_suffix = "_negative"
    else:
        isdiffpos = "true"
        output_psfcat_filename_to_use = output_psfcat_filename
        output_psfcat_finder_filename_to_use = output_psfcat_finder_filename
        done_suffix = ""

    njobs = len(jids)

    print("index_thread,njobs =",index_thread,njobs)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    dbh = dbh_list[index_thread]

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")

    for index_job in range(njobs):
        print("Creating temporary sources table")
        tablename = f"temp_sources_{index_job}"
        sql_queries = []
        #TODO: verify that duplicate index_jobs will never exist
        #Create a temporary table that mirrors sources table for light-weight cross-matching to AstroObjects
        #don't worry about a unique sid, that will be added when injected into full table
        sql_queries = f"""DROP TABLE IF EXISTS {tablename} ;
                        CREATE UNLOGGED TABLE {tablename}
                        (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS)
                        ALTER TABLE {tablename} DROP sid,
                            ADD COLUMN aid bigint;"""

        dbh.execute_sql_queries(sql_queries)

        index_core = index_job % num_cores
        if index_thread != index_core:
            continue

        jid = jids[index_job]
        overlapping_fields = overlapping_fields_list[index_job]
        meta_dict = meta_list[index_job]

        jid_from_dict = meta_dict["jid"]

        if jid != jid_from_dict:
            fh.write(f"*** Error: jid is not equal to jid from meta dictionary; quitting...\n")
            fh.flush()
            exit(64)

        expid = meta_dict["expid"]
        sca = meta_dict["sca"]
        fid = meta_dict["fid"]
        field = meta_dict["field"]
        hp6 = meta_dict["hp6"]
        hp9 = meta_dict["hp9"]
        mjdobs = meta_dict["mjdobs"]
        pid = meta_dict["pid"]


        fh.write(f"Loop start: index_job,jid,overlapping_fields = {index_job},{jid},{overlapping_fields}\n")


        # Check whether done file exists in S3 bucket for job, and skip if it exists.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_done_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/source_dbload" + done_suffix + "_jid" +  str(jid)  + ".done"
        done_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

        if do_done_check and downloaded_from_bucket:
            fh.write("*** Warning: Done file exists ({}); skipping...\n".format(done_filename))
            fh.flush()
            continue


        # Download SFFT-difference-image PSF-fit catalog file from S3 bucket.

        output_psfcat_filename_for_jid = output_psfcat_filename_to_use.replace(".txt",f"_jid{jid}.txt")

        s3_full_name_psfcat_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/" +  output_psfcat_filename_to_use
        ret_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,
                                                                                             s3_full_name_psfcat_file,
                                                                                             output_psfcat_filename_for_jid)

        if not downloaded_from_bucket:
            fh.write("*** Warning: PSF-fit catalog file does not exist ({}); skipping...\n".format(output_psfcat_filename_to_use))
            fh.flush()
            continue


        # Download SFFT-difference-image PSF-fit finder catalog file from S3 bucket.

        output_psfcat_finder_filename_for_jid = output_psfcat_finder_filename_to_use.replace(".txt",f"_jid{jid}.txt")

        s3_full_name_psfcat_finder_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/" +  output_psfcat_finder_filename_to_use
        ret_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,
                                                                                             s3_full_name_psfcat_finder_file,
                                                                                             output_psfcat_finder_filename_for_jid)

        if not downloaded_from_bucket:
            fh.write("*** Warning: PSF-fit finder catalog file does not exist ({}); skipping...\n".format(output_psfcat_finder_filename_to_use))
            fh.flush()
            continue


        # Join catalogs and extract columns for sources database tables.

        psfcat_qtable = QTable.read(output_psfcat_filename_for_jid,format='ascii')
        psfcat_finder_qtable = QTable.read(output_psfcat_finder_filename_for_jid,format='ascii')

        joined_table_inner = join(psfcat_qtable, psfcat_finder_qtable, keys='id', join_type='inner')

        nrows = len(joined_table_inner)
        fh.write(f"nrows in PSF-fit catalog = {nrows}\n")


        # Here are what the columns in the photutils catalogs are called:
        # Main: id group_id group_size local_bkg x_init y_init flux_init x_fit y_fit flux_fit x_err y_err flux_err n_pixels_fit qfit cfit reduced_chi2 flags ra dec
        # Finder: id xcentroid ycentroid sharpness roundness1 roundness2 npix peak flux mag daofind_mag
        # Note that some catalog-column names have underscores that need to be dealt with specially
        # because the database columns do not have underscores.
        #
        # Prepare records into sources database tables.

        joined_table_inner.rename_columns(['x_fit',
                                        'y_fit',
                                        'flux_fit',
                                        'x_err',
                                        'y_err',
                                        'flux_err',
                                        'n_pixels_fit',
                                        'reduced_chi2',
                                        'n_pixels'],
                                        ['xfit',
                                        'yfit',
                                        'fluxfit',
                                        'xerr',
                                        'yerr',
                                        'fluxerr',
                                        'npixfit',
                                        'redchi',
                                        'npix'
                                        ])

        joined_table_inner.remove_columns(['group_id',
                                        'group_size',
                                        'local_bkg',
                                        'x_init',
                                        'y_init',
                                        'flux_init',
                                        'x_centroid',
                                        'y_centroid',
                                        'flux',
                                        'mag',
                                        'daofind_mag'])
        joined_table_inner['pid']=pid
        joined_table_inner['isdiffpos']=isdiffpos
        joined_table_inner['field']=field
        joined_table_inner['expid']=expid
        joined_table_inner['fid']=fid
        joined_table_inner['sca'] = sca
        joined_table_inner['mjdobs']= mjdobs
        # The field,hp6,hp9 indexes must be overridden with
        # the actual ra,dec position of the source.
        joined_table_inner['hp6']   = hp.ang2pix(2**6, joined_table_inner['ra'], joined_table_inner['dec'], nest=True, lonlat=True)
        joined_table_inner['hp9']   = hp.ang2pix(2**9, joined_table_inner['ra'], joined_table_inner['dec'], nest=True, lonlat=True)
        joined_table_inner['field'] = roman_tessellation_index(joined_table_inner['ra'], joined_table_inner['dec'])   # vectorized
        nums = joined_table_inner.colnames
        buffer = table_to_buffer(joined_table_inner, nums)

        # Check whether database connection is still alive.

        if dbh.is_connection_alive():

            fh.write("Database is responsive!\n")

        else:

            fh.write("Database is not responsive! Connection is dead. Re-establishment required...\n")


            # Open database connection.

            dbh = db.RAPIDDB()

            if dbh.exit_code >= 64:
                fh.flush()
                exit(dbh.exit_code)


        # Load records into sources database tables.

        dbh.copy_data_from_buffer_into_database(buffer,tablename,columns)

        if dbh.exit_code >= 64:
            fh.write(f"*** Error bulk-loading data from buffer into specified database table ({tablename}); quitting...\n")
            fh.flush()
            exit(dbh.exit_code)

        cross_match_sql = ""
        #Analyze new table so POSTGRE SQL plans correctly
        # DISTINCT ON coupled with ORDERED BY returns only the first closest match
        #"    match_dist = m.dist * 3600" #Add later, in arcsec
        cross_match_sql = f"""ANALYZE {tablename};
        UPDATE {tablename} s
        SET aid = m.aid,
        FROM (
            SELECT DISTINCT ON (s2.src_id)
            s2.src_id,
            a.aid,
            q3c_dist(s2.ra, s2.dec, a.ra0, a.dec0) AS dist
            FROM {tablename} s2
            JOIN astroobjects a
                ON q3c_join(s2.ra, s2.dec, a.ra0, a.dec0, {match_radius})
            WHERE s2.flags=0") #can remove if filter R/B first
            ORDER BY s2.src_id, dist
            ) m
        WHERE s.src_id = m.src_id;"""



        # Touch done file.  Upload done file to S3 bucket.

        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,proc_date,jid,s3_client)

        fh.write(f"Loop end: done_filename,product_s3_bucket_base,proc_date,jid = {done_filename},{product_s3_bucket_base},{proc_date},{jid}\n")


        # Flush write buffer.

        fh.flush()


        # Remove no-longer-needed intermediate files.

        file_paths = [output_psfcat_filename_for_jid,output_psfcat_finder_filename_for_jid,sources_table_file]
        for file_path in file_paths:

            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"File deleted successfully ({file_path}).")
            else:
                print(f"The file does not exist({file_path}).")


        # End of loop over job ID.


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(jids,rtids_list,meta_list,negative_diffimg_flag,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,jids,rtids_list,meta_list,negative_diffimg_flag,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")


#################
# Main program.
#################

if __name__ == '__main__':


    #
    # Launch parallel tasks to load sources database tables
    # for all RAPID science pipelines that already
    # ran on a given processing date.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all normal RAPID science-pipeline Jobs records
    # that are associated with the given processing date.
    # Returns a list of job IDs.

    # TODO Figure out how to pass in SCAs as they finish and limit to processing on exposure at a time?
    #How much will this hold things up if SCAs take different amounts of time to process?
    recs = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(proc_date)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Set up to launch multi-processing for loading sources database tables.

    jid_list = []
    overlapping_fields_list = []
    meta_list = []
    scas_dict = {}

    for jid in recs:

        job_dict = dbh.get_info_for_job(jid)

        rid = job_dict["rid"]
        expid = job_dict["expid"]

        l2file_dict = dbh.get_l2file_info_for_sources(rid)

        crval1 = l2file_dict['crval1']
        crval2 = l2file_dict['crval2']
        crpix1 = l2file_dict['crpix1']
        crpix2 = l2file_dict['crpix2']
        cd11 = l2file_dict['cd11']
        cd12 = l2file_dict['cd12']
        cd21 = l2file_dict['cd21']
        cd22 = l2file_dict['cd22']
        expid = l2file_dict["expid"]
        sca = l2file_dict["sca"]
        fid = l2file_dict["fid"]
        #field = l2file_dict["field"]
        hp6 = l2file_dict["hp6"]
        hp9 = l2file_dict["hp9"]
        mjdobs = l2file_dict["mjdobs"]

        diffimage_dict = dbh.get_best_difference_image(rid,ppid)

        pid = diffimage_dict['pid']

        scas_dict[sca] = 1


        # Load Sources record metadata into a dictionary that can be appended to a list,
        # and then unpacked later.

        meta_dict = {}

        meta_dict["jid"] = jid
        meta_dict["expid"] = expid
        meta_dict["sca"] = sca
        meta_dict["fid"] = fid
        meta_dict["field"] = field
        meta_dict["hp6"] = hp6
        meta_dict["hp9"] = hp9
        meta_dict["mjdobs"] = mjdobs
        meta_dict["pid"] = pid

        #TODO Replace this part? Do I need it?
        # Get field numbers (rtids) of all sky tiles containing sky positions
        # in given science image associated with job ID.

        rtid_dict = {}

        x_list = [*range(0,naxis1,500)]
        y_list = [*range(0,naxis2,500)]
        x_list.append(naxis1)
        y_list.append(naxis1)

        for y in y_list:
            for x in x_list:

                # x,y,crpix1,crpix2 must be zero-based.
                ra,dec = util.tan_proj2(x,y,crpix1-1,crpix2-1,crval1,crval2,cd11,cd12,cd21,cd22)

                roman_tessellation_db.get_rtid(ra,dec)
                rtid = str(roman_tessellation_db.rtid)

                rtid_dict[rtid] = 1

        keys_view = rtid_dict.keys()
        print("fields overlapping image =",keys_view)

        jid_list.append(jid)
        overlapping_fields_list.append(keys_view)
        meta_list.append(meta_dict)

        print("jid =",jid)

    scas_list = scas_dict.keys()


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Optionally skip sources child database table creation and bulk loading of sources records,
    # and just do the indexing, clustering, and applying grants to sources database tables for
    # all SCAs associated with processing date...")

    if do_loading:
        # #TODO move this to the SCA processing since we're going to create a temp tabl
        # # Create sources database tables for all SCAs associated with processing date.

        # print("Creating temp source database tables for all SCAs associated with processing date...")

        # sql_queries = []
        # sql_queries.append("SET default_tablespace = pipeline_data_01;")

        # for sca in scas_list:

        #     tablename = f"sources_{proc_date}_{sca}"

        #     sql_queries.append(f"SELECT to_regclass('public.{tablename}') IS NOT NULL;")
        #     sql_queries.append(f"CREATE TABLE {tablename} (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS);")
        #     sql_queries.append(f"ALTER TABLE {tablename} SET UNLOGGED;")
        #     sql_queries.append(f"ALTER TABLE {tablename} INHERIT sources;")

        # dbh.execute_sql_queries(sql_queries)

        #if dbh.exit_code >= 64:
        #    exit(dbh.exit_code)


        # Close main-program database connection before long episode of bulk-loading source records.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds to create sources database tables for all SCAs associated with processing date =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        ################################################################################
        # Execute sources-table-loading tasks for all science-pipeline jobs with jids on
        # a given processing date.  The execution is done in parallel, with the number
        # of parallel threads equal to the number of cores on the job-launcher machine.
        # First do for sources from positive difference images, and then from negative.
        ################################################################################

        if num_cores > 1:
            negative_diffimg_flag = False
            #AZ: Unit to parallelize over will be Exposure
            execute_parallel_processes(jid_list,overlapping_fields_list,meta_list,negative_diffimg_flag,num_cores)
            negative_diffimg_flag = True
            execute_parallel_processes(jid_list,overlapping_fields_list,meta_list,negative_diffimg_flag,num_cores)
        else:
            thread_index = 0
            negative_diffimg_flag = False
            run_single_core_job(jid_list,overlapping_fields_list,meta_list,negative_diffimg_flag,thread_index)
            negative_diffimg_flag = True
            run_single_core_job(jid_list,overlapping_fields_list,meta_list,negative_diffimg_flag,thread_index)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds to load all sources database tables =",
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


    # Index, cluster, and apply grants to sources database tables for all SCAs associated with processing date.

    print("Indexing, clustering, and applying grants to sources database tables for all SCAs associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for sca in scas_list:

        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_pid_idx ON sources_{proc_date}_{sca} (pid);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_expid_idx ON sources_{proc_date}_{sca} (expid);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_sca_idx ON sources_{proc_date}_{sca} (sca);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_field_idx ON sources_{proc_date}_{sca} (field);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_flags_idx ON sources_{proc_date}_{sca} (flags);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_mjdobs_idx ON sources_{proc_date}_{sca} (mjdobs);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_sid_idx ON sources_{proc_date}_{sca} (sid);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_radec_idx ON sources_{proc_date}_{sca} (q3c_ang2ipix(ra, dec));")
        sql_queries.append(f"CLUSTER sources_{proc_date}_{sca}_radec_idx ON sources_{proc_date}_{sca};")
        sql_queries.append(f"ANALYZE sources_{proc_date}_{sca};")
        #sql_queries.append(f"ALTER TABLE sources_{proc_date}_{sca} SET LOGGED;")                  # For speed, do not log.
        sql_queries.append(f"REVOKE ALL ON TABLE sources_{proc_date}_{sca} FROM rapidreadrole;")
        sql_queries.append(f"GRANT SELECT ON TABLE sources_{proc_date}_{sca} TO GROUP rapidreadrole;")
        sql_queries.append(f"REVOKE ALL ON TABLE sources_{proc_date}_{sca} FROM rapidadminrole;")
        sql_queries.append(f"GRANT ALL ON TABLE sources_{proc_date}_{sca} TO GROUP rapidadminrole;")
        sql_queries.append(f"REVOKE ALL ON TABLE sources_{proc_date}_{sca} FROM rapidporole;")
        sql_queries.append(f"GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE sources_{proc_date}_{sca} TO rapidporole;")

    dbh.execute_sql_queries(sql_queries)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to index, cluster, and apply grants to sources database tables for all SCAs associated with processing date =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to load all sources into database for {proc_date} =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    for tdbh in dbh_list:
        tdbh.close()

        if tdbh.exit_code >= 64:
            exit(tdbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
