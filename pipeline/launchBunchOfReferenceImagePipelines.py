import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "launchBunchOfReferenceImagePipelines.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"
launch_single_pipeline_instance_code = 'awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py'

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of RAPID science-pipeline jobs.  Processing date is always in Pacific time zone.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Print out basic information for log file.

print("proc_date =",proc_date)


# If RUNFID is set, then process just the specified filter.

run_fid_str = os.getenv('RUNFID')

if run_fid_str is None:
    run_fid = None
    print("*** Message: Will process all filters...")
else:
    try:
        run_fid = int(run_fid_str)
    except:
        print(f"*** Error: run_fid cannot be converted to integer (run_fid={run_fid_str}); quitting...")
        exit(64)
    print(f"*** Message: Will process only fid={run_fid}...")


# Get optional DRYRUN.

dry_run_str = os.getenv('DRYRUN')

if dry_run_str is None:
    dry_run = False
else:
    dry_run = True


# Determine number of parallel processes.

num_cores_str = os.getenv('NUM_CORES')

if num_cores_str is None:
    num_cores = os.cpu_count()
    if num_cores is None:
        num_cores = 1
else:
    try:
        num_cores = int(num_cores_str)
    except:
        print(f"*** Error: num_cores cannot be converted to integer (num_cores_str={num_cores_str}); quitting...")
        exit(64)

print("num_cores =",num_cores)


# Print parameters.

print("run_fid =",run_fid)
print("dry_run =",dry_run)


# Get env. var. RAPID_SW and assign cfg_path.

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

ppid_sciimage = int(config_input['SCI_IMAGE']['ppid'])

ppid_refimage = int(config_input['REF_IMAGE']['ppid'])
min_n_images_to_coadd = int(config_input['REF_IMAGE']['min_n_images_to_coadd'])
max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])

print("min_n_images_to_coadd =",min_n_images_to_coadd)
print("max_n_images_to_coadd =",max_n_images_to_coadd)


# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.

def run_single_core_job(fields,fids,num_cores,index_thread):

    """
    Load unique value of field,fid into the environment variables FIELD,FID, respectively.
    Launch single instance of script with given environment-variable settings for FIELD,FID.
    """

    njobs = len(fields)

    print("index_thread,njobs =",index_thread,njobs)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    with open(thread_work_file, 'w', encoding="utf-8") as fh:

        fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}\n")

        for index_job in range(njobs):

            index_core = index_job % num_cores
            if index_thread != index_core:
                continue

            field = fields[index_job]
            fid = fids[index_job]


            # Load FIELD,FID into the environment.

            os.environ['FIELD'] = str(field)
            os.environ['FID'] = str(fid)


            # Launch single pipeline from within Docker container.

            python_cmd = 'python3.11'
            python_script = f"{rapid_sw}/pipeline/{launch_single_pipeline_instance_code}"

            launch_cmd = [python_cmd,
                          python_script]

            if dry_run:
                fh.write(f"Skipped launching reference-image pipeline for dry_run,field,fid = {dry_run},{field},{fid}\n")
            else:
                exitcode_from_launch_cmd = util.execute_command(launch_cmd)


                if exitcode_from_launch_cmd == 0:
                    fh.write(f"Launched reference-image pipeline for dry_run,field,fid = {dry_run},{field},{fid}\n")
                else:
                    fh.write(f"*** Error from launch_cmd = {launch_cmd}: " +
                             f"exitcode_from_launch_cmd,dry_run,field,fid = " +
                             f"{exitcode_from_launch_cmd},{dry_run},{field},{fid}\n")

            fh.flush()

            # End of loop over fields,fids.


        fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")


    message = f"Finished normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(fields,fids,num_cores):

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,fields,fids,num_cores,thread_index) for thread_index in range(num_cores)]

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


    '''
    Launch reference-image pipelines for all field,fid,nframes combinations
    with minimum number of frames in coadd stack and no MJD-range restrictions.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all field/filter/nframes combinations with
    # minimum number of frames in coadd stack and no MJD-range restrictions.

    print("Querying database for all field/filter/nframes combinations with " +
          "minimum number of frames in coadd stack and no MJD-range restrictions.")

    n_filters = 8

    num = 0

    field_list = []
    fid_list = []

    for fid in range(1,n_filters + 1):

        if run_fid is not None:
            if run_fid != fid:
                print(f"*** Message: Skipping fid={fid}; continuing...")
                continue

        recs = dbh.get_field_fid_nframes_records(min_n_images_to_coadd,fid)

        if dbh.exit_code >= 64:
            print("*** Error from query for field/filter/nframes combinations {}; quitting ".format(swname))
            exit(dbh.exit_code)

        for rec in recs:

            field = rec[0]
            _ = rec[1]      # Parse from query to avoid confusion; will be same as fid.
            nframes = rec[2]

            print("field,fid =",field,fid)


            # Query RefImages database table for the best version of reference image
            # (which is usually the latest unless a prior version is locked).
            # A reference image depends only on pipeline number, field, filter, and version.
            # If a reference image already exists, then do not launch a referene-image pipeline for it.
            # First, check for reference images made by the dedicated reference-image pipeline (ppid=12).
            # If no reference imag is found, check whether there is one made by the science pipeline (ppid=15).

            rfid = None

            db_refimages_rec_dict = dbh.get_best_reference_image(ppid_refimage,field,fid)
            ppid_existing_refimg = ppid_refimage

            if dbh.exit_code == 7:
                print(f"No database record from dbh.get_best_reference_image for " +
                      f"ppid={ppid_refimage} called by {swname}; continuing with rfid = None...")

                db_refimages_rec_dict = dbh.get_best_reference_image(ppid_sciimage,field,fid)
                ppid_existing_refimg = ppid_sciimage

            if dbh.exit_code == 7:
                print(f"No database record from dbh.get_best_reference_image for " +
                      f"ppid={ppid_sciimage} called by {swname}; continuing with rfid = None...")
                ppid_existing_refimg = ppid_sciimage
            elif dbh.exit_code >= 64:
                print("*** Error from {}; quitting ".format(swname))
                exit(dbh.exit_code)
            else:
                rfid = db_refimages_rec_dict["rfid"]
                filename_refimage = db_refimages_rec_dict["filename"]
                infobits_refimage = db_refimages_rec_dict["infobits"]


            if rfid is not None:
                print(f"*** Message: Reference image found in database for " +
                      "field,fid.ppid_existing_refimg={field},{fid},{ppid_existing_refimg} (rfid={rfid})")
                continue


            # Append to lists of fields and fids for which to launch reference-image pipelines.

            field_list.append(field)
            fid_list.append(fid)

            num += 1

            print("num,field,fid,nframes =",num,field,fid,nframes)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # The job launching is done in parallel, taking advantage of multiple cores
    # on the job-launcher machine.

    number_pipeline_instances = len(field_list)
    print(f"number_pipeline_instances = {number_pipeline_instances}")


    if num_cores > 1:
        print(f"*** Message: Calling method execute_parallel_processes...")
        execute_parallel_processes(field_list,fid_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job(field_list,fid_list,num_cores,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch all reference-image pipelines =",
        end_time_benchmark - start_time_benchmark)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
