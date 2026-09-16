"""
Produce alert packets for every difference image made by the RAPID science pipelines
that ran normally on a given processing date.

This is a launcher-side stage (no AWS Batch), in the same shape as crossMatchSources.py
and pruneNotBestMerges.py: run it inside the RAPID-pipeline Docker container on the
job-launcher machine, after pruneNotBestMerges.py, because the alert packets read the
Sources, Merges_<field>, AstroObjects_<field> and AstroObjectsMeta_<field> database tables
that the preceding stages populate.  The virtual pipeline operator
(virtualPipelineOperator.py) runs it as its last stage.

Environment variable JOBPROCDATE specifies the processing date.  For each normal
science-pipeline job (ppid=15) that ended on that date, the best difference image (pid)
is looked up and every alertable source on it goes into one Avro object-container
archive, which is uploaded beside the job's other products:

    s3://<product_s3_bucket_base>/<proc_date>/jid<N>/alerts_jid<N>.avro
    s3://<product_s3_bucket_base>/<proc_date>/jid<N>/alerts_jid<N>_summary.json

The summary JSON holds the production statistics for the chip (alerts.produce.BatchStats:
alert count, bytes, history depth, cross-match states, cutouts present) and lists every
source that produced no alert, with the reason.  The same statistics are logged at INFO
level in the per-thread log files produceAlertsForProcDate_thread<T>.out, and a run-level
summary is written to <RAPID_WORK>/produceAlertsForProcDate_<proc_date>_summary.json.

Parameters are read from the [ALERTS] section of
cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini.

Kafka publication is NOT implemented in this stage: alerts are archived to S3 only, and
the stage refuses to start if publish_to_kafka is True in the config file.

Exit codes:
    0   Normal termination.
    7   No science-pipeline jobs with a best difference image for the processing date.
    32  One or more chips failed (warning; the VPO continues).  See the run summary.
    64  Fatal error: environment, config, database connection, or a worker crash.
"""

import os
import json
import time
import logging
import configparser
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from dateutil import tz

import boto3

import database.modules.utils.rapid_db as db
from alerts.cli import make_provider
from alerts.produce import BatchStats, batch_produce, open_alert_archive

to_zone = tz.gettz('America/Los_Angeles')

swname = "produceAlertsForProcDate.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

EXIT_NORMAL = 0
EXIT_NOTHING_TO_DO = 7
EXIT_CHIPS_FAILED = 32
EXIT_FATAL = 64


#-------------------------------------------------------------------------------------------------------------
# Configuration.
#-------------------------------------------------------------------------------------------------------------

def read_alert_settings(config_input):

    '''
    Collect the stage's parameters from the parsed config file into one dictionary.

    Raises NotImplementedError if publish_to_kafka is True: this stage archives alerts to S3
    only, and must not silently run with publication switched on.
    '''

    alerts = config_input['ALERTS']
    job_params = config_input['JOB_PARAMS']

    if alerts.getboolean('publish_to_kafka', fallback=False):
        raise NotImplementedError(
            "publish_to_kafka = True in [ALERTS], but Kafka publication is not implemented "
            "in this stage; alerts are archived to S3 only")

    kona_file = alerts.get('kona_file', fallback='').strip() or None

    settings = {
        'ppid': int(config_input['SCI_IMAGE']['ppid']),
        'diff_flavor': alerts.get('diff_flavor', fallback='sfft'),
        'refcat_match': alerts.getboolean('refcat_match', fallback=True),
        'ned_match': alerts.getboolean('ned_match', fallback=True),
        'kona_file': kona_file,
        'archive_filename_base': alerts.get('archive_filename_base', fallback='alerts_jid'),
        'archive_codec': alerts.get('archive_codec', fallback='deflate'),
        'log_level': alerts.get('log_level', fallback='INFO').upper(),
        'upload_to_s3_bucket': job_params.getboolean('upload_to_s3_bucket', fallback=True),
        'product_s3_bucket_base': job_params['product_s3_bucket_base'],
    }

    return settings


#-------------------------------------------------------------------------------------------------------------
# Database lookup of the chips to alert on.
#-------------------------------------------------------------------------------------------------------------

def lookup_chips_for_processing_date(dbh, proc_date, ppid):

    '''
    Return one dictionary per chip to alert on (jid, rid, pid, expid, sca, field, fid),
    for the normal science-pipeline jobs that ended on the processing date.  Jobs without
    a best difference image are skipped with a warning.  Raises RuntimeError on a database error.
    '''

    jids = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(proc_date)

    if dbh.exit_code >= 64:
        raise RuntimeError(f"*** Error getting Jobs records for proc_date={proc_date} "
                           f"(dbh.exit_code={dbh.exit_code}); quitting...")

    chips = []

    for jid in jids or []:

        job = dbh.get_info_for_job(jid)

        if dbh.exit_code >= 64 or job is None:
            raise RuntimeError(f"*** Error getting Jobs record for jid={jid} "
                               f"(dbh.exit_code={dbh.exit_code}); quitting...")

        rid = job["rid"]

        diffimage = dbh.get_best_difference_image(rid, ppid)

        if dbh.exit_code >= 64:
            raise RuntimeError(f"*** Error getting best difference image for rid={rid} "
                               f"(dbh.exit_code={dbh.exit_code}); quitting...")

        if not diffimage or diffimage.get("pid") is None:
            print(f"*** Warning: No best difference image for jid={jid}, rid={rid}; skipping...")
            continue

        chips.append({'jid': int(jid),
                      'rid': int(rid),
                      'pid': int(diffimage["pid"]),
                      'expid': job["expid"],
                      'sca': job["sca"],
                      'field': job["field"],
                      'fid': job["fid"]})

    return chips


#-------------------------------------------------------------------------------------------------------------
# Per-chip production.
#-------------------------------------------------------------------------------------------------------------

def chip_product_names(proc_date, chip, settings):

    '''
    Local filenames and S3 object prefix of one chip's alert archive and summary.
    '''

    base = f"{settings['archive_filename_base']}{chip['jid']}"

    return {'archive_filename': base + ".avro",
            'summary_filename': base + "_summary.json",
            's3_prefix': f"{proc_date}/jid{chip['jid']}/"}


def produce_chip(provider, chip, settings, proc_date, work_dir):

    '''
    Write one chip's alert archive into work_dir.  Returns (stats, archive_path).
    A partially written archive is removed if production raises.
    '''

    names = chip_product_names(proc_date, chip, settings)
    archive_path = os.path.join(work_dir, names['archive_filename'])

    stats = BatchStats(pid=chip['pid'])

    try:
        with open_alert_archive(archive_path, codec=settings['archive_codec']) as archive:
            batch_produce(provider, chip['pid'], archive=archive, stats=stats)
    except BaseException:
        if os.path.exists(archive_path):
            os.remove(archive_path)
        raise

    return stats, archive_path


def write_chip_summary(path, summary):

    with open(path, 'w', encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)


def upload_chip_product(s3_client, bucket, s3_prefix, local_path):

    '''
    Upload one file beside the chip's other products and return its s3:// URL.
    '''

    object_name = s3_prefix + os.path.basename(local_path)
    s3_client.upload_file(local_path, bucket, object_name)

    return f"s3://{bucket}/{object_name}"


def configure_thread_logging(path, level_name):

    '''
    Send the alerts package's log records (the per-chip statistics at INFO, dropped sources at
    WARNING) to the thread's log file.  Returns the handler so the caller can detach it.
    '''

    handler = logging.FileHandler(path, mode='w', encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(min(root.level or logging.INFO, getattr(logging, level_name, logging.INFO)))

    return handler


def run_single_core_job(chips, index_thread, num_cores, settings, proc_date, work_dir):

    '''
    Produce alerts for the chips assigned to this thread (every num_cores-th chip starting at
    index_thread), with one alert data provider and one log file per thread.  A chip that fails
    is recorded with status "failed" and the thread moves on; only being unable to start at all
    raises.  Returns the list of per-chip summary dictionaries.
    '''

    thread_start_time_benchmark = time.time()

    thread_work_file = os.path.join(work_dir, swname.replace(".py", "_thread") + str(index_thread) + ".out")
    handler = configure_thread_logging(thread_work_file, settings['log_level'])
    log = logging.getLogger(swname)

    my_chips = chips[index_thread::num_cores]

    log.info("Start of run_single_core_job: index_thread=%d, %d chips assigned", index_thread, len(my_chips))

    results = []

    try:

        if len(my_chips) == 0:
            return results

        try:
            provider = make_provider(diff_flavor=settings['diff_flavor'],
                                     kona_file=settings['kona_file'],
                                     refcat=settings['refcat_match'],
                                     ned=settings['ned_match'])
        except SystemExit as e:
            raise RuntimeError(f"*** Error opening alert data provider in index_thread={index_thread}: {e}")

        s3_client = boto3.client('s3') if settings['upload_to_s3_bucket'] else None
        bucket = settings['product_s3_bucket_base']

        with provider:

            for chip in my_chips:

                chip_start_time = time.time()
                names = chip_product_names(proc_date, chip, settings)
                summary_path = os.path.join(work_dir, names['summary_filename'])

                log.info("Chip start: jid=%s, pid=%s", chip['jid'], chip['pid'])

                summary = {'swname': swname, 'swvers': swvers, 'proc_date': proc_date, **chip}

                try:

                    stats, archive_path = produce_chip(provider, chip, settings, proc_date, work_dir)

                    summary['status'] = "ok"
                    summary['archive_filename'] = names['archive_filename']
                    summary['archive_bytes'] = os.path.getsize(archive_path)
                    summary['stats'] = stats.as_dict()

                    if s3_client is not None:
                        summary['archive_s3_url'] = upload_chip_product(s3_client, bucket, names['s3_prefix'], archive_path)

                    summary['elapsed_seconds'] = round(time.time() - chip_start_time, 3)
                    write_chip_summary(summary_path, summary)

                    if s3_client is not None:
                        upload_chip_product(s3_client, bucket, names['s3_prefix'], summary_path)
                        os.remove(archive_path)
                        os.remove(summary_path)

                    log.info("Chip end: jid=%s, pid=%s, %d alerts, %d sources dropped, %.1f s",
                             chip['jid'], chip['pid'], stats.n_alerts, stats.n_failed,
                             summary['elapsed_seconds'])

                except Exception as e:

                    summary['status'] = "failed"
                    summary['error'] = type(e).__name__
                    summary['message'] = str(e)
                    summary['elapsed_seconds'] = round(time.time() - chip_start_time, 3)
                    log.exception("*** Error: chip failed: jid=%s, pid=%s", chip['jid'], chip['pid'])

                results.append(summary)

        log.info("End of run_single_core_job: index_thread=%d, elapsed %.1f s",
                 index_thread, time.time() - thread_start_time_benchmark)

    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    return results


def execute_parallel_processes(chips, num_cores, settings, proc_date, work_dir):

    '''
    Run run_single_core_job in num_cores worker processes and gather their per-chip summaries.
    A worker that raises is fatal (RuntimeError), since its chips were never attempted.
    '''

    print("num_cores =", num_cores)

    results = []
    failures = []

    with ProcessPoolExecutor(max_workers=num_cores) as executor:

        futures = [executor.submit(run_single_core_job, chips, thread_index, num_cores, settings, proc_date, work_dir)
                   for thread_index in range(num_cores)]

        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)
            print(f"Completed: {i+1} processes, lastly for index={index}")

    for index, future in enumerate(futures):
        try:
            results.extend(future.result())
        except Exception as e:
            failures.append(e)
            print(f"*** Error in thread index {index} = {e}")

    if failures:
        raise RuntimeError(f"*** Error(s) from {len(failures)} worker(s); quitting...")

    return results


#-------------------------------------------------------------------------------------------------------------
# Run summary.
#-------------------------------------------------------------------------------------------------------------

def summarize_run(chip_summaries):

    '''
    Aggregate the per-chip summaries.  Returns (exitcode, aggregate dictionary).
    '''

    ok = [s for s in chip_summaries if s.get('status') == "ok"]
    failed = [s for s in chip_summaries if s.get('status') != "ok"]

    aggregate = {
        'n_chips': len(chip_summaries),
        'n_chips_ok': len(ok),
        'n_chips_failed': len(failed),
        'n_alerts': sum(s['stats']['n_alerts'] for s in ok),
        'n_sources_dropped': sum(s['stats']['n_failed'] for s in ok),
        'n_archive_bytes': sum(s['archive_bytes'] for s in ok),
        'failed_chips': [{'jid': s.get('jid'), 'pid': s.get('pid'),
                          'error': s.get('error'), 'message': s.get('message')} for s in failed],
    }

    exitcode = EXIT_CHIPS_FAILED if failed else EXIT_NORMAL

    return exitcode, aggregate


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

def main():

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

    print("proc_utc_datetime =", proc_utc_datetime)
    print("proc_pt_datetime_started =", proc_pt_datetime_started)


    # JOBPROCDATE of RAPID science-pipeline jobs that already ran.

    proc_date = os.getenv('JOBPROCDATE')

    if proc_date is None:

        print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
        return EXIT_FATAL

    print("proc_date =", proc_date)


    # Other required environment variables.

    rapid_sw = os.getenv('RAPID_SW')

    if rapid_sw is None:

        print("*** Error: Env. var. RAPID_SW not set; quitting...")
        return EXIT_FATAL

    rapid_work = os.getenv('RAPID_WORK')

    if rapid_work is None:

        print("*** Error: Env. var. RAPID_WORK not set; quitting...")
        return EXIT_FATAL

    cfg_path = rapid_sw + "/cdf"

    print("rapid_sw =", rapid_sw)
    print("rapid_work =", rapid_work)
    print("cfg_path =", cfg_path)


    # Read input parameters from .ini file.

    config_input_filename = cfg_path + "/" + cfg_filename_only
    config_input = configparser.ConfigParser()
    config_input.read(config_input_filename)

    try:
        settings = read_alert_settings(config_input)
    except (NotImplementedError, KeyError, ValueError) as e:
        print(f"*** Error reading [ALERTS] settings from {config_input_filename}: {e}; quitting...")
        return EXIT_FATAL

    for key, value in settings.items():
        print(f"{key} = {value}")


    # Number of worker processes, taking advantage of multiple cores on the job-launcher machine.

    num_cores = os.getenv('NUM_CORES')

    if num_cores is None:
        num_cores = os.cpu_count() or 1
    else:
        num_cores = int(num_cores)

    print("num_cores =", num_cores)


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        print(f"*** Error: Could not open DB connection (dbh.exit_code = {dbh.exit_code}); quitting...")
        return EXIT_FATAL


    # Look up the chips (best difference images of normal science-pipeline jobs) for the processing date.

    try:
        chips = lookup_chips_for_processing_date(dbh, proc_date, settings['ppid'])
    except RuntimeError as e:
        print(e)
        dbh.close()
        return EXIT_FATAL


    # Close the main database connection before the parallel phase, rather than leaving it idle
    # for its duration.  The worker processes open their own connections.

    dbh.close()

    if dbh.exit_code >= 64:
        return dbh.exit_code

    print("n_chips =", len(chips))

    if len(chips) == 0:
        print("*** Warning: No chips to produce alerts for; quitting...")
        return EXIT_NOTHING_TO_DO


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
          end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Produce alerts for chips in parallel, with the number of worker processes
    # equal to the number of cores on the job-launcher machine (or fewer chips).
    ################################################################################

    num_workers = min(num_cores, len(chips))

    try:
        if num_workers > 1:
            chip_summaries = execute_parallel_processes(chips, num_workers, settings, proc_date, rapid_work)
        else:
            chip_summaries = run_single_core_job(chips, 0, 1, settings, proc_date, rapid_work)
    except RuntimeError as e:
        print(e)
        return EXIT_FATAL

    chip_summaries.sort(key=lambda s: s['jid'])


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to produce alerts for all chips =",
          end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Summarize the run and write the run summary file.

    terminating_exitcode, aggregate = summarize_run(chip_summaries)

    for key, value in aggregate.items():
        if key != 'failed_chips':
            print(f"{key} = {value}")

    for failed in aggregate['failed_chips']:
        print(f"*** Warning: chip failed: jid={failed['jid']}, pid={failed['pid']}: "
              f"{failed['error']}: {failed['message']}")

    run_summary = {'swname': swname,
                   'swvers': swvers,
                   'proc_date': proc_date,
                   'started': proc_pt_datetime_started,
                   'settings': settings,
                   **aggregate,
                   'chips': chip_summaries}

    run_summary_filename = os.path.join(rapid_work, swname.replace(".py", "") + "_" + proc_date + "_summary.json")

    try:
        write_chip_summary(run_summary_filename, run_summary)
        print("run_summary_filename =", run_summary_filename)
    except OSError as e:
        print(f"*** Warning: Could not write run summary {run_summary_filename} ({e}); continuing...")


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to produce alerts for processing date =",
          end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    print("terminating_exitcode =", terminating_exitcode)

    return terminating_exitcode


if __name__ == '__main__':

    exit(main())
