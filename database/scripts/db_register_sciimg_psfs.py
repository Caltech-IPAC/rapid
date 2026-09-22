#!/usr/bin/env python3

"""
db_register_sciimg_psfs.py — register science-image PSFs in the PSFs
database table, which is where the science pipeline and the
limiting-magnitude computation both look them up (get_best_psf).

The psfs directory of the job-info S3 bucket holds two families:

    WFI_SCAnn_Fnnn_PSF_DET_DIST.fits    WebbPSF library, all 8 filters
                                        and all 18 SCAs
    sciimage_psf_f146_scann.fits        a different, broader PSF product,
                                        W146 only

Version 1.0 of this script registered the second family only, with the
filter hard-wired to fid 8, so every filter but W146 had no PSF at all.
This version registers either family, takes the filter from the filename
rather than a literal, and looks fid up in the Filters table.

By default it registers only (fid, sca) pairs that have no PSF yet, so
running it registers the missing filters without disturbing a pair that
is already serving the pipeline.  `--force` re-registers in scope, which
inserts a NEW version and moves vbest to it — that changes which PSF the
science pipeline uses, so it is never the default.

Filter names differ between the two places they appear: PSF filenames
use the Roman designations (F062, F106, ...) while the Filters table uses
the RAPID names (R062, Y106, ...).  The translation below is the same one
sims/src/socsims/convert_socsims.py applies to FITS headers.

Note the PSF need not be normalized here: the science pipeline normalizes
whatever it downloads (awsBatchSubmitJobs_runSingleSciencePipeline.py
calls normalize_image), and the number of noise pixels behind the
limiting magnitude is invariant to PSF scaling.

Exit codes: 64 environment or connection, 65 bad arguments or nothing
usable found, 67 a database operation failed, 0 clean.
"""

import argparse
import configparser
import os
import re
import sys
import time
from datetime import datetime, timezone

import boto3
from dateutil import tz

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

to_zone = tz.gettz('America/Los_Angeles')

swname = "db_register_sciimg_psfs.py"
swvers = "2.0"                # 2.0 registers every filter, not only W146.

cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


#: Filter token as it appears in a PSF filename -> filter name in the
#: Filters database table.  F184 is spelled the same in both.

psf_filter_to_db_filter = {
    "F062": "R062",
    "F087": "Z087",
    "F106": "Y106",
    "F129": "J129",
    "F146": "W146",
    "F158": "H158",
    "F184": "F184",
    "F213": "K213",
}


#: The PSF file families, each with a pattern that yields the SCA number
#: and the filter token.

psf_families = {
    "wfi": {
        "pattern": re.compile(r"^WFI_SCA(\d+)_(F\d{3})_PSF_DET_DIST\.fits$"),
        "groups": ("sca", "filter"),
        "description": "WebbPSF library, all filters and SCAs",
    },
    "sciimage": {
        "pattern": re.compile(r"^sciimage_psf_(f\d{3})_sca(\d+)\.fits$"),
        "groups": ("filter", "sca"),
        "description": "the broader W146-only product registered by v1.0",
    },
}


def parse_psf_filename(filename, family):

    """(sca, filter token) for a PSF filename, or (None, None) if it does
    not belong to `family`."""

    spec = psf_families[family]

    match = spec["pattern"].match(filename)

    if match is None:
        return None, None

    fields = dict(zip(spec["groups"], match.groups()))

    return int(fields["sca"]), fields["filter"].upper()


def parse_args(argv=None):

    p = argparse.ArgumentParser(
        description="Register science-image PSFs in the PSFs database table.")

    p.add_argument("--family", choices=sorted(psf_families), default="wfi",
                   help="which PSF file family to register (default "
                        "%(default)s: " + psf_families["wfi"]["description"] +
                        ")")
    p.add_argument("--filters", default=None,
                   help="comma-separated filters to register, named either "
                        "way (F062 or R062); default is every filter found")
    p.add_argument("--scas", default=None,
                   help="comma-separated SCA numbers; default is every SCA "
                        "found")
    p.add_argument("--force", action="store_true",
                   help="register even a (fid,sca) pair that already has a "
                        "PSF, which inserts a new version and moves vbest to "
                        "it, changing which PSF the pipeline uses")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be registered, write nothing and "
                        "download nothing")
    p.add_argument("--keep-downloads", action="store_true",
                   help="do not delete each PSF after registering it")

    return p.parse_args(argv)


def db_filter_name(psf_filter):

    """The Filters-table name for a PSF filename's filter token."""

    return psf_filter_to_db_filter.get(psf_filter.upper())


def wanted_filters(args):

    """The set of Filters-table names in scope, or None for all of them."""

    if args.filters is None:
        return None

    names = set()

    for token in args.filters.split(","):

        token = token.strip().upper()

        if not token:
            continue

        # Accept either spelling, so nobody has to remember which one
        # this particular script wants.

        name = db_filter_name(token) or token

        names.add(name)

    return names


def wanted_scas(args):

    if args.scas is None:
        return None

    return {int(s) for s in args.scas.split(",") if s.strip()}


#################
# Main program.
#################

def main(argv=None):

    args = parse_args(argv)

    print("swname =", swname)
    print("swvers =", swvers)
    print("family =", args.family)

    start_time_benchmark = time.time()

    datetime_utc_now = datetime.now(timezone.utc)
    proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
    datetime_pt_now = datetime_utc_now.astimezone(tz=to_zone)
    proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

    print("proc_utc_datetime =", proc_utc_datetime)
    print("proc_pt_datetime_started =", proc_pt_datetime_started)


    # Read environment variables.

    rapid_sw = os.getenv('RAPID_SW')

    if rapid_sw is None:
        print("*** Error: Env. var. RAPID_SW not set; quitting...")
        return 64

    rapid_work = os.getenv('RAPID_WORK')

    if rapid_work is None:
        print("*** Error: Env. var. RAPID_WORK not set; quitting...")
        return 64

    aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
    aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

    if aws_access_key_id is None:
        print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
        return 64

    if aws_secret_access_key is None:
        print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; "
              "quitting...")
        return 64

    cfg_path = rapid_sw + "/cdf"

    print("rapid_sw =", rapid_sw)
    print("cfg_path =", cfg_path)


    # Read input parameters from .ini file.

    config_input_filename = cfg_path + "/" + cfg_filename_only
    config_input = configparser.ConfigParser()
    config_input.read(config_input_filename)

    job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']


    # Set up.

    s3_client = boto3.client('s3')

    s3_subdir = 'psfs'
    s3_url = f"s3://{job_info_s3_bucket_base}/{s3_subdir}"

    print("s3_url =", s3_url)


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        return dbh.exit_code

    try:

        # Map filter name to fid from the database, never from a literal.

        filter_records = dbh.get_filters()

        if dbh.exit_code >= 64 or not filter_records:
            print("*** Error: Could not read the Filters table; quitting...")
            return 67

        fid_of_filter = {filter_name: int(fid)
                         for fid, filter_name in filter_records}

        print("fid_of_filter =", fid_of_filter)


        # (fid,sca) pairs that already have a PSF.  These are skipped unless
        # --force, so a run registers the missing filters and leaves a pair
        # that is already serving the pipeline exactly as it is.

        registered_records = dbh.get_distinct_fid_sca_from_psfs()

        if dbh.exit_code >= 64:
            print("*** Error: Could not read the PSFs table; quitting...")
            return 67

        already_registered = {(int(fid), int(sca))
                              for fid, sca in (registered_records or [])}

        print(f"already registered: {len(already_registered)} (fid,sca) "
              f"pair(s)")


        # List the PSF files in the S3 bucket.

        ls_cmd = f"aws s3 ls {s3_url}/"
        exitcode_from_ls, code_to_execute_stdout = \
            util.execute_command_in_shell(ls_cmd, print_output=False)

        if exitcode_from_ls != 0:
            print(f"*** Error: Could not list {s3_url}; quitting...")
            return 65

        filters_in_scope = wanted_filters(args)
        scas_in_scope = wanted_scas(args)

        candidates = []
        n_unparsed = 0

        for line in code_to_execute_stdout.splitlines():

            cols = line.split()

            if len(cols) < 4:
                continue

            filename = cols[3]

            sca, psf_filter = parse_psf_filename(filename, args.family)

            if sca is None:
                n_unparsed += 1
                continue

            filter_name = db_filter_name(psf_filter)

            if filter_name is None:
                print(f"*** Warning: {filename} has filter {psf_filter}, "
                      f"which has no Filters-table counterpart; skipping")
                continue

            fid = fid_of_filter.get(filter_name)

            if fid is None:
                print(f"*** Warning: filter {filter_name} is not in the "
                      f"Filters table; skipping {filename}")
                continue

            if filters_in_scope is not None and filter_name not in filters_in_scope:
                continue

            if scas_in_scope is not None and sca not in scas_in_scope:
                continue

            if (fid, sca) in already_registered and not args.force:
                continue

            candidates.append((filename, fid, filter_name, sca))

        print(f">> {len(candidates)} PSF(s) to register "
              f"({n_unparsed} file(s) in the directory are not of family "
              f"{args.family})")

        if not candidates:
            print(">> nothing to do")
            return 0

        by_filter = {}
        for _, _, filter_name, _ in candidates:
            by_filter[filter_name] = by_filter.get(filter_name, 0) + 1

        for filter_name in sorted(by_filter):
            print(f"   {filter_name}: {by_filter[filter_name]} SCA(s)")

        if args.dry_run:
            for filename, fid, filter_name, sca in candidates:
                print(f"   --dry-run: would register {filename} as "
                      f"fid={fid} ({filter_name}) sca={sca}")
            print(f">> --dry-run: {len(candidates)} PSF(s) would have been "
                  f"registered, nothing was")
            return 0


        # Download, checksum and register each PSF.

        n_registered = 0

        for filename, fid, filter_name, sca in candidates:

            s3_bucket_object_name = s3_subdir + '/' + filename

            print(f"Downloading s3://{job_info_s3_bucket_base}/"
                  f"{s3_bucket_object_name} into {filename}...")

            try:
                s3_client.download_file(job_info_s3_bucket_base,
                                        s3_bucket_object_name, filename)
            except Exception as error:
                print(f"*** Warning: Could not download {filename} "
                      f"({error}); skipping")
                continue

            print("Computing checksum of ", filename)
            checksum = db.compute_checksum(filename)

            status = 1

            psf_filename = f"{s3_url}/{filename}"


            # Insert record in PSFs database table.

            dbh.add_psf(fid, sca, status, psf_filename, checksum)

            if dbh.exit_code >= 64:
                print(f"*** Error: Could not insert the PSFs record for "
                      f"fid={fid} sca={sca}; quitting...")
                return dbh.exit_code

            psfid = dbh.psfid
            version_psf = dbh.version

            print(f"psfid = {psfid}, version_psf = {version_psf}, "
                  f"fid = {fid} ({filter_name}), sca = {sca}")


            # Finalize the record, which is what sets vbest = 1 for it.

            dbh.update_psf(psfid, psf_filename, checksum, status, version_psf)

            if dbh.exit_code >= 64:
                print(f"*** Error: Could not finalize the PSFs record for "
                      f"psfid={psfid}; quitting...")
                return dbh.exit_code

            n_registered += 1

            if not args.keep_downloads and os.path.exists(filename):
                os.remove(filename)

        print(f">> {n_registered} PSF(s) registered")

    finally:
        dbh.close()

    if dbh.exit_code >= 64:
        return dbh.exit_code


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to register PSFs database records =",
          end_time_benchmark - start_time_benchmark)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =", terminating_exitcode)

    return terminating_exitcode


if __name__ == '__main__':
    sys.exit(main())
