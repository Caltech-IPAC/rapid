import boto3
import os
import numpy as np
import configparser
from astropy.table import QTable
from astropy.table import QTable, join
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite

swname = "loadPSFCatIntoDBSourcesTable.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Compute start time for benchmark.

start_time_benchmark = time.time()


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

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_finder_filename'])

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])

ppid = int(config_input['SCI_IMAGE']['ppid'])


# Open database connections for parallel access.

#num_cores = os.cpu_count()
num_cores = 1

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
cols.append("flags")
cols.append("sharpness")
cols.append("roundness1")
cols.append("roundness2")
cols.append("npix")
cols.append("peak")
cols.append("pid")
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
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(jids,overlapping_fields_list,meta_list,index_thread):

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

        index_core = index_job % num_cores
        if index_thread != index_core:
            continue

        jid = jids[index_job]
        overlapping_fields = overlapping_fields_list[index_job]
        meta_dict = meta_list[index_job]

        jid_from_dict = meta_dict["jid"]

        if jid != jid_from_dict:
            fh.write(f"*** Error: jid is not equal to jid from meta dictionary; quitting...\n")
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

        s3_full_name_done_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/source_dbload_jid" +  str(jid)  + ".done"
        done_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

        if downloaded_from_bucket:
            fh.write("*** Warning: Done file exists ({}); skipping...\n".format(done_filename))
            continue


        # Download ZOGY-difference-image PSF-fit catalog file from S3 bucket.

        output_psfcat_filename_for_jid = output_psfcat_filename.replace(".txt",f"_jid{jid}.txt")

        s3_full_name_psfcat_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/" +  output_psfcat_filename
        ret_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,
                                                                                             s3_full_name_psfcat_file,
                                                                                             output_psfcat_filename_for_jid)

        if not downloaded_from_bucket:
            fh.write("*** Warning: PSF-fit catalog file does not exist ({}); skipping...\n".format(output_psfcat_filename))
            continue


        # Download ZOGY-difference-image PSF-fit finder catalog file from S3 bucket.

        output_psfcat_finder_filename_for_jid = output_psfcat_finder_filename.replace(".txt",f"_jid{jid}.txt")

        s3_full_name_psfcat_finder_file = "s3://" + product_s3_bucket_base + "/" + proc_date + '/jid' + str(jid) + "/" +  output_psfcat_finder_filename
        ret_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,
                                                                                             s3_full_name_psfcat_finder_file,
                                                                                             output_psfcat_finder_filename_for_jid)

        if not downloaded_from_bucket:
            fh.write("*** Warning: PSF-fit finder catalog file does not exist ({}); skipping...\n".format(output_psfcat_finder_filename))
            continue


        # Join catalogs and extract columns for sources database tables.

        psfcat_qtable = QTable.read(output_psfcat_filename_for_jid,format='ascii')
        psfcat_finder_qtable = QTable.read(output_psfcat_finder_filename_for_jid,format='ascii')

        joined_table_inner = join(psfcat_qtable, psfcat_finder_qtable, keys='id', join_type='inner')

        nrows = len(joined_table_inner)
        fh.write(f"nrows in PSF-fit catalog = {nrows}\n")


        # Here are what the columns in the photutils catalogs are called:
        # Main: id group_id group_size local_bkg x_init y_init flux_init x_fit y_fit flux_fit x_err y_err flux_err npixfit qfit cfit flags ra dec
        # Finder: id xcentroid ycentroid sharpness roundness1 roundness2 npix peak flux mag daofind_mag
        # Note that some catalog-column names have underscores that need to be dealt with specially
        # because the database columns do not have underscores.
        #
        # Prepare records into sources database tables.

        sources_table = f"sources_{proc_date}_{sca}"
        sources_table_file = f"sources_{proc_date}_{sca}_{jid}" + ".csv"

        with open(sources_table_file, "w") as csv_fh:

            for row in joined_table_inner:
                nums = ""
                for col in cols:

                    cat_col = col

                    if cat_col == 'xfit':
                        cat_col = 'x_fit'
                    elif cat_col == 'yfit':
                        cat_col = 'y_fit'
                    elif cat_col == 'fluxfit':
                        cat_col = 'flux_fit'
                    elif cat_col == 'xerr':
                        cat_col = 'x_err'
                    elif cat_col == 'yerr':
                        cat_col = 'y_err'
                    elif cat_col == 'fluxerr':
                        cat_col = 'flux_err'

                    if cat_col == 'pid':
                        continue
                    if cat_col == 'field':
                        continue
                    if cat_col == 'hp6':
                        continue
                    if cat_col == 'hp9':
                        continue
                    if cat_col == 'expid':
                        continue
                    if cat_col == 'fid':
                        continue
                    if cat_col == 'sca':
                        continue
                    if cat_col == 'mjdobs':
                        continue

                    num = str(row[cat_col])
                    nums = nums + num + ","

                num = str(pid)
                nums = nums + num + ","
                num = str(field)
                nums = nums + num + ","
                num = str(hp6)
                nums = nums + num + ","
                num = str(hp9)
                nums = nums + num + ","
                num = str(expid)
                nums = nums + num + ","
                num = str(fid)
                nums = nums + num + ","
                num = str(sca)
                nums = nums + num + ","
                num = str(mjdobs)
                nums = nums + num + ","

                # Slice the string to get all but the last character, then add the newline character
                new_character = "\n"
                line_to_write_to_file = nums[:-1] + new_character

                csv_fh.write(line_to_write_to_file)


        # Load records into sources database tables.

        dbh.copy_sources_data_from_file_into_database(sources_table_file,sources_table,columns)


        # Touch done file.  Upload done file to S3 bucket.

        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,proc_date,jid,s3_client)

        fh.write(f"Loop end: done_filename,product_s3_bucket_base,proc_date,jid = {done_filename},{product_s3_bucket_base},{proc_date},{jid}\n")


        # End of loop over job ID.


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()


def execute_parallel_processes(jids,rtids_list,meta_list,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(para) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")


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

    recs = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(proc_date)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Launch multi-processing for loading sources database tables.

    jid_list = []
    overlapping_fields_list = []
    meta_list = []
    scas_dict = {}


    for jid in recs:

        job_dict = dbh.get_info_for_job(jid)

        rid = job_dict["rid"]

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
        field = l2file_dict["field"]
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


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Create sources database tables for all SCAs associated with processing date.

    print("Creating sources database tables for all SCAs associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_data_01;")

    scas_list = scas_dict.keys()

    for sca in scas_list:

        tablename = f"sources_{proc_date}_{sca}"

        sql_queries.append(f"SELECT to_regclass('public.{tablename}') IS NOT NULL;")
        sql_queries.append(f"CREATE TABLE {tablename} (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS);")
        sql_queries.append(f"ALTER TABLE {tablename} SET UNLOGGED;")
        sql_queries.append(f"ALTER TABLE {tablename} INHERIT sources;")

    dbh.execute_sql_queries(sql_queries)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to create sources database tables for all SCAs associated with processing date =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute sources-table-loading tasks for all science-pipeline jobs with jids on
    # a given processing date.  The execution is done in parallel, with the number
    # of parallel threads equal to the number of cores on the job-launcher machine.
    ################################################################################

    if num_cores > 1:
        execute_parallel_processes(jid_list,overlapping_fields_list,meta_list,num_cores)
    else:
        run_single_core_job(jid_list,overlapping_fields_list,meta_list,0)

    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to load all sources database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Index, cluster, and apply grants to sources database tables for all SCAs associated with processing date.

    print("Indexing, clustering, and applying grants to sources database tables for all SCAs associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for sca in scas_list:

        sql_queries.append(f"ALTER TABLE ONLY sources_{proc_date}_{sca} ADD CONSTRAINT sourcespk_{proc_date}_{sca} UNIQUE (ra, dec);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_pid_idx ON sources_{proc_date}_{sca} (pid);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_expid_idx ON sources_{proc_date}_{sca} (expid);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_sca_idx ON sources_{proc_date}_{sca} (sca);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_field_idx ON sources_{proc_date}_{sca} (field);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_mjdobs_idx ON sources_{proc_date}_{sca} (mjdobs);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_sid_idx ON sources_{proc_date}_{sca} (sid);")
        sql_queries.append(f"CREATE INDEX sources_{proc_date}_{sca}_radec_idx ON sources_{proc_date}_{sca} (q3c_ang2ipix(ra, dec));")
        sql_queries.append(f"CLUSTER sources_{proc_date}_{sca}_radec_idx ON sources_{proc_date}_{sca};")
        sql_queries.append(f"ANALYZE sources_{proc_date}_{sca};")
        sql_queries.append(f"ALTER TABLE sources_{proc_date}_{sca} SET LOGGED;")
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


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    for tdfh in dbh_list:
        tdfh.close()

        if tdbh.exit_code >= 64:
            exit(tdbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
