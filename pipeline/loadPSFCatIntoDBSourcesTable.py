import os
import numpy as np
import configparser
from astropy.table import QTable
from astropy.table import QTable, join
from astropy import units as u
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

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_psfcat_finder_filename'])

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])

ppid = int(config_input['SCI_IMAGE']['ppid'])











# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.

def run_script(jid):

    """
    Load unique value of jid into the environment variable RAPID_JOB_ID.
    Launch single instance of script with given environment-variable setting for RAPID_JOB_ID.
    """



    psfcat_qtable = QTable.read(output_psfcat_filename,format='ascii')
    psfcat_finder_qtable = QTable.read(output_psfcat_finder_filename,format='ascii')

    # Inner join on 'id'

    joined_table_inner = join(psfcat_qtable, psfcat_finder_qtable, keys='id', join_type='inner')
    print("Inner Join:")
    print(joined_table_inner)

    nrows = len(joined_table_inner)
    print("nrows =",nrows)


    for row in joined_table_inner:
        id = row['id']
        ra = row['ra']
        dec = row['dec']
        fluxfit = row['flux_fit']
        roundness1 = row['roundness1']

        print(id,ra,dec,fluxfit,roundness1)




    #    print OUT "COPY objects_$j (ra,\"dec\",mag,sigx,sigy,ang,chipid,field) FROM stdin;\n";




    # Load RAPID_JOB_ID into the environment.

    os.environ['RAPID_JOB_ID'] = str(jid)


    # Launch single pipeline from within Docker container.

    python_cmd = 'python3.11'
    launch_single_pipeline_instance_code = '/code/pipeline/awsBatchSubmitJobs_launchSinglePostProcPipeline.py'

    launch_cmd = [python_cmd,
                  launch_single_pipeline_instance_code]

    exitcode_from_launch_cmd = util.execute_command(launch_cmd)


def launch_parallel_processes(jids, num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_script,jid) for jid in jids]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")


#################
# Main program.
#################

if __name__ == '__main__':


    #
    # Launch RAPID post-processing pipelines
    # for all RAPID science pipelines that already
    # ran on a given processing date.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all normal RAPID science-pipeline Jobs records
    # that are associated with the given processing date.
    # recs list is [jid,expid,sca,fid,field,rid].

    recs = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(proc_date)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Launch pipeline instances to be run under AWS Batch.

    jid_list = []

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

        diffimage_dict = get_best_difference_image(rid,ppid)

        pid = diffimage_dict['pid']


        # Get field numbers (rtids) of sky tile containing sky position in image.

        rtid_dict = {}

        for y in range(0,naxis2,500):
            for x in range(0,naxis1,500):

                # x,y,crpix1,crpix2 must be zero-based.
                ra,dec = util.tan_proj2(x,y,crpix1-1,crpix2-1,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2)

                roman_tessellation_db.get_rtid(ra,dec)
                rtid = str(roman_tessellation_db.rtid)

                rtid_dict['rtid'] = 1

        keys_view = rtid_dict.keys()
        print(keys_view)

        exit(0)


        jid_list.append(jid)

        print("jid =",jid)



    # The job launching is done in parallel, taking advantage of multiple cores on the job-launcher machine.

    launch_parallel_processes(jid_list)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch all pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
