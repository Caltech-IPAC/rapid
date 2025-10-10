import boto3
import os
import numpy as np
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite

swname = "computeStatisticsForAstroObjects.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages for creating and setting up Merges and AstroObjects tables.

debug = 1


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

num_cores = os.cpu_count()

print("num_cores =",num_cores)

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


# Get S3 client.

s3_client = boto3.client('s3')


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(fields,index_thread):

    '''
    Remove records from Merges_<field> database tables associated with sources that are no longer best
    (vbest=0 in associated Diffimages table).
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    nfields = len(fields)

    print("index_thread,nfields =",index_thread,nfields)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    dbh = dbh_list[index_thread]

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    # Requires all sources child tables be tied to parent sources table through inheritance.

    sources_tablename = f"sources"


    # Loop over all fields associated with this thread and compute statistics for astroobjects:
    # 1. Query for all records in each Merges_<field> table.
    # 2. Determine unique pids (primary key of DiffImages table).
    # 3. Determine unique aids (primary key of AstroObjects_<field> table).
    # 4. Check associated DiffImages records for those that are best (vbest>0).
    # 5. Populate vbest dictionary keyed by unique pid.
    # 6. Compute statistics for all Merges_<field> records with best sources.

    for index_field in range(nfields):

        index_core = index_field % num_cores
        if index_thread != index_core:
            continue

        field = fields[index_field]


        fh.write(f"Loop start: index_field,field = {index_field},{field}\n")

        merges_tablename = f"merges_{field}"
        astroobjects_tablename = f"merges_{field}"


        query = f"SELECT a.aid,a.sid,b.pid,b.ra,b.dec,b.fluxfit FROM {merges_tablename} AS a, " +\
            f"{sources_tablename} AS b " +\
            f"WHERE a.sid = b.sid;"

        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,thread_debug)

        pids_dict = {}
        sids_for_aid_dict = {}
        pids_for_aid_dict = {}
        ras_for_aid_dict = {}
        decs_for_aid_dict = {}
        fluxes_for_aid_dict = {}

        for record in records:

            aid = record[0]
            sid = record[1]
            pid = record[2]
            ra = record[3]
            dec = record[4]
            fluxfit = record[5]

            pids_dict[pid] = 1

            try:
                sids_for_aid_dict[aid].append(sid)
                pids_for_aid_dict[aid].append(pid)
                ras_for_aid_dict[aid].append(ra)
                decs_for_aid_dict[aid].append(dec)
                fluxes_for_aid_dict[aid].append(fluxfit)
            except:
                sids_for_aid_dict[aid] = [sid]
                pids_for_aid_dict[aid] = [pid]
                ras_for_aid_dict[aid] = [ra]
                decs_for_aid_dict[aid] = [dec]
                fluxes_for_aid_dict[aid] = [fluxfit]


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to select all records from {merges_tablename} and {sources_tablename} database tables = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # Query for all DiffImages records associated with unique list of pids.

        unique_pids_list = list(pids_dict.keys())

        vbest_dict = {}

        for pid in unique_pids_list:

            query = f"SELECT vbest FROM diffimages WHERE pid = {pid};"

            sql_queries = []
            sql_queries.append(query)
            records = dbh.execute_sql_queries(sql_queries,thread_debug)

            vbest = records[0][0]

            vbest_dict[pid] = vbest


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to determine not-best DiffImages database records = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # Loop over astroobjects for current field:
        # 1. Filter out not-best sources.
        # 2. Compute statistics.
        # 3. Update AstroObjects record.

        aids_list = list(sids_for_aid_dict.keys())

        for aid in aids_list:

            sids_list = sids_for_aid_dict[aid]
            pids_list = pids_for_aid_dict[aid]
            ras_list = ras_for_aid_dict[aid]
            decs_list = decs_for_aid_dict[aid]
            fluxes_list = fluxes_for_aid_dict[aid]
            nsources = 0

            filtered_ras_list =[]
            filtered_decs_list =[]
            filtered_fluxes_list =[]

            for sid,pid,ra,dec,flux in zip(sids_list,pids_list,ras_list,decs_list,fluxes_list):

                vbest = vbest_dict[pid]


                # Skip source that is associated with a not-best DiffImages record.

                if vbest == 0:
                    continue


                # Source is best, so include it in lists for statistical computations.

                nsources += 1
                filtered_ras_list.append(ra)
                filtered_decs_list.append(dec)
                filtered_fluxes_list.append(flux)

            flag1 = False
            flag2 = False
            for ra in filtered_ras_list:
                if ra < 10.0:
                    flag1 = True
                elif ra > 350.0:
                    flag2 = True

            if flag1 and flag2:
                i = 0
                for ra in filtered_ras_list:
                    if ra > 350.0:
                        filtered_ras_list[i] -= 360.0
                    i += 1

            meanra = np.mean(filtered_ras_list)
            meandec = np.mean(filtered_decs_list)
            meanflux = np.mean(filtered_fluxes_list)
            stdra = np.std(filtered_ras_list)
            stddec = np.std(filtered_decs_list)
            stdflux = np.std(filtered_fluxes_list)

            dbh.update_astroobject_statistics(astroobjects_tablename,
                                              aid,
                                              meanra,
                                              stdra,
                                              meandec,
                                              stddec,
                                              meanflux,
                                              stdflux,
                                              nsources,
                                              thread_debug)


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to update statistics in {astroobjects_tablename} database table\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over fields.

        fh.write(f"Loop end: index_field,field = {index_field},{field}\n")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(fields_list,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,fields_list,thread_index) for thread_index in range(num_cores)]

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
    Launch parallel tasks to delete not-best Merges database records for all fields
    (for which merges tables exist).
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    sql_queries = []
    sql_queries.append(f"select tablename from pg_tables where schemaname='public' and tablename like 'merges_%';")
    records = dbh.execute_sql_queries(sql_queries,debug)

    fields_list = []
    for record in records:
        field = record[0].replace("merges_","")
        fields_list.append(field)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to ascertain available fields =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute tasks for fields in parallel, with the number of parallel threads
    # equal to the number of cores on the job-launcher machine.
    ################################################################################

    if num_cores > 1:
        execute_parallel_processes(fields_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job(fields_list,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to complete parallel processing =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Vacuum and analyze merges database tables for all fields.

    print("Vacuuming and analyzing merges database tables for all fields...")

    for field in fields_list:

        tablename = f"merges_{field}"

        sql_queries = []
        sql_queries.append(f"VACUUM ANALYZE {tablename};")
        dbh.execute_sql_queries(sql_queries,debug)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to vacuum and analyze all merges database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to update all astroobjects statistics =",
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
