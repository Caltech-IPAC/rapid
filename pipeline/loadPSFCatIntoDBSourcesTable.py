'''
Load into database sources table the PSF-fit catalogs made by the
Python photutils package from the SFFT difference images
(until a final decision on which image-differencing method is best):
'''

import boto3
import os
import io
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

datetime_utc_now = datetime.now(timezone.utc)
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

# Print out basic information for log file.

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

config_input_filename = os.path.join(cfg_path,cfg_filename_only)
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


# Get number of cores for parallel processing.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


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
cols.append("field")
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
    """
    Convert a QTable to a buffer for bulk database ingest
    """
    cols = []
    for name in colnames:
        column_data = np.asarray(tbl[name])
        #use %r to create a string with minimum number of digits
        column_data_str = np.char.mod('%s', column_data) if column_data.dtype.kind == 'f' else column_data.astype(str)
        cols.append(column_data_str)
    buf = io.StringIO()
    buf.write("\n".join(separator.join(row) for row in zip(*cols)))
    buf.write("\n")
    buf.seek(0)
    return buf

def roman_tessellation_index(ra, dec):
    """

    """
    ra  = np.atleast_1d(np.asarray(ra, dtype=float))
    dec = np.atleast_1d(np.asarray(dec, dtype=float))
    if ra.shape != dec.shape:
        raise ValueError(f"ra and dec shapes differ: {ra.shape} vs {dec.shape}")
    out = []
    for i, (ira, idec) in enumerate(zip(ra, dec)):
        roman_tessellation_db.get_rtid(ira, idec)
        if roman_tessellation_db.rtid is None:
            raise ValueError(f"no tessellation tile for ra={ira}, dec={idec} "
                             f"(exit_code={roman_tessellation_db.exit_code})")
        else:
            out.append(roman_tessellation_db.rtid)
    return np.array(out)

#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(meta_list,negative_diffimg_flag,index_thread):


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

    njobs = len(meta_list)

    print("index_thread,njobs =",index_thread,njobs)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        raise RuntimeError(f"*** Error: Could not open output file {thread_work_file}; quitting...")


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")


    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")

    for index_job in range(njobs):

        meta_dict = meta_list[index_job]
        jid = meta_dict['jid']
        index_core = index_job % num_cores
        if index_thread != index_core:
            continue

        print("Creating temporary sources table")
        tablename = f"temp_sources_{jid}"
        #Create a temporary table that mirrors sources table for light-weight cross-matching to AstroObjects
        #don't worry about a unique sid, that will be added when injected into full table
        sql_queries = [f"DROP TABLE IF EXISTS {tablename};",
                        f"""CREATE UNLOGGED TABLE {tablename}
                        (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS);""",
                        f"""ALTER TABLE {tablename} ALTER COLUMN aid DROP NOT NULL,
                        ADD COLUMN is_new boolean NOT NULL DEFAULT false;"""]
        dbh.execute_sql_queries(sql_queries)

        expid = meta_dict["expid"]
        sca = meta_dict["sca"]
        fid = meta_dict["fid"]
        mjdobs = meta_dict["mjdobs"]
        pid = meta_dict["pid"]


        fh.write(f"Loop start: index_job,jid= {index_job},{jid}\n")


        # Check whether done file exists in S3 bucket for job, and skip if it exists.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

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

        dbh.copy_data_from_buffer_into_database(buffer,tablename,nums)

        if dbh.exit_code >= 64:
            fh.write(f"*** Error bulk-loading data from buffer into specified database table ({tablename}); quitting...\n")
            fh.flush()
            exit(dbh.exit_code)

        #----------------------------------
        # Cross match temp_sources to AstroObjects to get aid
        #----------------------------------
        #Analyze new table so POSTGRE SQL plans correctly
        # DISTINCT ON coupled with ORDERED BY returns only the first closest match
        # match_dist = m.dist * 3600 #Add later, in arcsec
        cross_match_time_benchmark_start = time.time()
        #can remove if s2.flags filter R/B first
        cross_match_sql = [f"ANALYZE {tablename};"
            f"""UPDATE {tablename} s
            SET aid = m.aid
            FROM (
                SELECT DISTINCT ON (s2.id)
                s2.id,
                a.aid,
                q3c_dist(s2.ra, s2.dec, a.ra0, a.dec0) AS dist
                FROM {tablename} s2
                JOIN astroobjects a
                    ON q3c_join(s2.ra, s2.dec, a.ra0, a.dec0, {match_radius})
                WHERE s2.flags=0
                ORDER BY s2.id, dist
                ) m
            WHERE s.id = m.id;"""]
        dbh.execute_sql_queries(cross_match_sql)
        cross_match_time_benchmark_end = time.time()
        fh.write(f"Elapsed time in seconds to crossmatch = {cross_match_time_benchmark_end-cross_match_time_benchmark_start}\n")
        #----------------------------------
        # For rows in temp_sources not cross-matched, create a new aid
        #----------------------------------
        update_astroobjects_time_benchmark_start = time.time()
        create_new_aid_sql = f"""UPDATE {tablename}
            SET aid = nextval('astroobjects_aid_seq'),
                is_new = true
            WHERE aid IS NULL;
            INSERT INTO astroobjects (aid, ra0, dec0, flux0, field, hp6, hp9)
            SELECT aid, ra, dec, fluxfit, field, hp6, hp9
            FROM {tablename}
            WHERE is_new;"""
        dbh.execute_sql_queries([create_new_aid_sql])
        update_astrobjects_time_benchmark_end = time.time()
        fh.write(f"Elapsed time in seconds to update astroobjects Table = {update_astrobjects_time_benchmark_end-update_astroobjects_time_benchmark_start}\n")

        #----------------------------------
        # Update sources table with temp_sources
        #----------------------------------
        insert_sources_time_benchmark_start = time.time()
        update_source_table_sql = f"""INSERT INTO sources (sid, aid, id, pid, isdiffpos, ra, dec,
                xfit, yfit, fluxfit, xerr, yerr, fluxerr,
                npixfit, qfit, cfit, redchi, flags,
                sharpness, roundness1, roundness2, npix, peak,
                field, hp6, hp9, expid, fid, sca, mjdobs)
            SELECT sid, aid, id, pid, isdiffpos, ra, dec,
                xfit, yfit, fluxfit, xerr, yerr, fluxerr,
                npixfit, qfit, cfit, redchi, flags,
                sharpness, roundness1, roundness2, npix, peak,
                field, hp6, hp9, expid, fid, sca, mjdobs
            FROM {tablename};"""
        dbh.execute_sql_queries([update_source_table_sql])
        insert_sources_time_benchmark_end = time.time()
        fh.write(f"Elapsed time in seconds to insert new detections into Source table = {insert_sources_time_benchmark_end-insert_sources_time_benchmark_start}\n")


        #----------------------------------
        # For temp_sources not cross-matched, create new astroobjectmeta rows
        #----------------------------------
        update_astroobjectsmeta_time_benchmark_start = time.time()
        create_new_aid_to_astroobjectmeta_sql = f"""INSERT INTO astroobjectsmeta (aid, nsources, fluxmean, fluxsum2, stdevflux, cos_sum, sin_sum, meanra, meandec, mjdmin, mjdmax)
            SELECT aid, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, NULL, NULL
            FROM {tablename}
            WHERE is_new;"""
        dbh.execute_sql_queries([create_new_aid_to_astroobjectmeta_sql])

        #----------------------------------
        # Update astroobjectmeta table with temp_sources
        #----------------------------------
        # Use circular mean for meanra = arctan2(sum(sin(ra)), sum(cos(ra)))
        # for stdev - use GREATEST to protect against really small neg numbers blowing up
        update_existing_astroobjectmeta_sql = f"""UPDATE astroobjectsmeta m
                SET nsources  = m.nsources + agg.n,
                    fluxmean  = (m.fluxmean*m.nsources + agg.fsum) / (m.nsources + agg.n),
                    fluxsum2  = m.fluxsum2 + agg.fsum2,
                    stdevflux = sqrt(GREATEST(
                                    (m.fluxsum2 + agg.fsum2) / (m.nsources + agg.n)
                                    - ((m.fluxmean*m.nsources + agg.fsum) / (m.nsources + agg.n))^2,
                                    0.0)),
                    cos_sum   = m.cos_sum + agg.cossum,
                    sin_sum   = m.sin_sum + agg.sinsum,
                    meanra    = mod(degrees(atan2(m.sin_sum + agg.sinsum,
                                  m.cos_sum + agg.cossum)) + 360.0, 360.0),
                    meandec   = (m.meandec*m.nsources + agg.sumdec) / (m.nsources + agg.n),
                    mjdmin    = LEAST(m.mjdmin, agg.minmjd),
                    mjdmax    = GREATEST(m.mjdmax, agg.maxmjd)
                FROM (
                    SELECT aid,
                        count(*)                        AS n,
                        sum(fluxfit::float8)            AS fsum,
                        sum(fluxfit::float8 * fluxfit)  AS fsum2,
                        sum(sind(ra))                   AS sinsum,
                        sum(cosd(ra))                   AS cossum,
                        sum(dec::float8)                AS sumdec,
                        min(mjdobs)                     AS minmjd,
                        max(mjdobs)                     AS maxmjd
                    FROM {tablename}
                    GROUP BY aid
                ) agg
                WHERE m.aid = agg.aid;"""
        dbh.execute_sql_queries([update_existing_astroobjectmeta_sql])
        update_astroobjectsmeta_time_benchmark_end = time.time()
        fh.write(f"Elapsed time in seconds to update the AstroObjectsMeta table = {update_astroobjectsmeta_time_benchmark_end-update_astroobjectsmeta_time_benchmark_start}\n")
        # Touch done file.  Upload done file to S3 bucket.
        dbh.mark_psfcat_uploaded(rec['qid'])
        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,proc_date,jid,s3_client)

        fh.write(f"Loop end: done_filename,product_s3_bucket_base,proc_date,jid = {done_filename},{product_s3_bucket_base},{proc_date},{jid}\n")


        # Flush write buffer.

        fh.flush()


        # Remove no-longer-needed intermediate files.
        file_paths = [output_psfcat_filename_for_jid,output_psfcat_finder_filename_for_jid]
        for file_path in file_paths:

            if os.path.exists(file_path):
                os.remove(file_path)
                fh.write(f"File deleted successfully ({file_path})...\n")
                fh.flush()
            else:
                fh.write(f"The file does not exist({file_path})...\n")
                fh.flush()


        # End of loop over job ID.


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(jids,meta_list,negative_diffimg_flag,num_cores):
    """
    This does not currently work with the implementation because it is possible that the same object
    detected in two different images could be added to the DB at the same time with different aids
    """
    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,jids,meta_list,negative_diffimg_flag,thread_index) for thread_index in range(num_cores)]

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


    # Query database for all normal RAPID science-pipeline pids that have PSFCatalogs
    # thave have not been ingested
    # Returns a list of job IDs.

    recs = dbh.get_pending_psfcat_uploads()

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Set up to launch multi-processing for loading sources database tables.

    meta_list = []

    for rec in recs:
        l2file_dict = dbh.get_l2file_info_for_sources(rec['rid'])
        meta_dict = rec
        meta_dict['expid'] = l2file_dict["expid"]
        meta_dict['mjdobs'] = l2file_dict["mjdobs"]
        # Load Sources record metadata into a dictionary that can be appended to a list,
        # and then unpacked later.
        meta_list.append(meta_dict)

        print(f"jid ={rec['jid']}")

    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark



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
    # a given processing date.  Parallel execution should not be performed at this time
    # as there is no check for two different files creating an aid on the same object
    # detected in two images. If this is overcome, the execution is done in parallel, with the number
    # of parallel threads equal to the number of cores on the job-launcher machine.
    # First do for sources from positive difference images, and then from negative.
    ################################################################################

    if num_cores > 1:
        raise NotImplementedError("Work needs to be done to handle ingestion of the same field and " \
        "and therefore potentially same object at the same time, creating multiple aids")
        negative_diffimg_flag = False
        #AZ: Unit to parallelize over will be Exposure
        execute_parallel_processes(recs['jid'],meta_list,negative_diffimg_flag,num_cores)
        negative_diffimg_flag = True
        execute_parallel_processes(recs['jid'],meta_list,negative_diffimg_flag,num_cores)
    else:
        thread_index = 0
        negative_diffimg_flag = False
        run_single_core_job(meta_list,negative_diffimg_flag,thread_index)
        negative_diffimg_flag = True
        run_single_core_job(meta_list,negative_diffimg_flag,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to load all sources database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Reopen main-program database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to load all sources into database for {proc_date} =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
