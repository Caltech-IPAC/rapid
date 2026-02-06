'''
Compute forced photometry for sky positions in a given RAPID field (a.k.a. sky tile).
The input sky_positions_csv_file has 3 columns: reqid,ra,dec, and these sky positions
are required to be within the input field.
'''

import boto3
import os
import numpy as np
import healpy as hp
import configparser
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


swname = "forcedPhotometryForField.py"
swvers = "1.0"
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


# Read environment variables.

field = os.getenv('FIELD')

if field is None:

    print("*** Error: Env. var. FIELD not set; quitting...")
    exit(64)

sky_positions_csv_file = os.getenv('SKYPOSITIONSCSVFILE')

if sky_positions_csv_file is None:

    print("*** Error: Env. var. sky_positions_csv_file not set; quitting...")
    exit(64)


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

print("field =",field)
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

ppid_sci = int(config_input['SCI_IMAGE']['ppid'])


# Use SExtractor reference-image catalogs for now, since PhotUtils catalogs are not made
# for reference images at this time.  TODO

cattype = 1


#=================================================================
# Parameters to control processing.

# Match radius in degrees for cone search around field center, an overestimate,
# to get all difference images that may overlap the field (a.k.a. sky tile).
match_radius = float(config_input['FORCED_PHOTOMETRY']['match_radius'])

# switch to increase verbosity to stdout.
verbose = int(config_input['FORCED_PHOTOMETRY']['verbose'])

# debug switch: set to 1 if want to save intermediate products.
debug = int(config_input['FORCED_PHOTOMETRY']['debug'])

# simulation mode switch: set to 1 if want to simulate point source
# of fixed magnitude $simmag at each epoch using input PSFs & ZPs on
# requested position and JD range.
simflag = int(config_input['FORCED_PHOTOMETRY']['simflag'])

# magnitude of source to simulate if simulation  switch was set.
simmag = float(config_input['FORCED_PHOTOMETRY']['simmag'])

# diff-image cutout size (side length) [native pixels]
# this is also set by the PSF stamp size we plan to use.
stampsz = int(config_input['FORCED_PHOTOMETRY']['stampsz'])

# upsampling factor for stamp image pixels (on side).
stampupsamplefac = int(config_input['FORCED_PHOTOMETRY']['stampupsamplefac'])

# diameter of aperture for aperture photometry [native pixels].
apdiam = float(config_input['FORCED_PHOTOMETRY']['apdiam'])

# correction factor for PSF-fit flux uncertainty to obtain ~
# consistency with expected uncertainty in mag (~ 1.0857/snr).
corrunc = float(config_input['FORCED_PHOTOMETRY']['corrunc'])

# SNR for thresholding PSF-fit and aperture SNRs so can compute
# possible correction factor to correct for biases in PSF flux.
snrforcor = float(config_input['FORCED_PHOTOMETRY']['snrforcor'])

# minimum number of good aperture to PSF-fit flux ratios for
# computing possible flux correction to psf fluxes.
minnumrats = int(config_input['FORCED_PHOTOMETRY']['minnumrats'])

# flag to apply actual filter-dependent corrections to PSF-fit fluxes
# to obtain consistency with aperture fluxes.
applyflxcorr = int(config_input['FORCED_PHOTOMETRY']['applyflxcorr'])

# flag to correct fluxes using $simcalpsffluxcor and $simcalapfluxcor
# factors derived from comparing simulated fluxes to measured fluxes
# when $simflag = 1.
applysimcalcorr = int(config_input['FORCED_PHOTOMETRY']['applysimcalcorr'])

# flux correction factor for psf-fluxes using simulated source.
simcalpsffluxcor = float(config_input['FORCED_PHOTOMETRY']['simcalpsffluxcor'])

# flux correction factor for aperture-fluxes using simulated source.
simcalapfluxcor = float(config_input['FORCED_PHOTOMETRY']['simcalapfluxcor'])

# official start of survey (Julian Date) => use as earliest
# possible epoch for querying DB for photometry.
d_earliest = config_input['FORCED_PHOTOMETRY']['d_earliest']
jd_earliest = float(config_input['FORCED_PHOTOMETRY']['jd_earliest'])

# maximum matching radius for finding and reporting nearest
# reference-image PSF-catalog source [arcsec].
refmatchrad = float(config_input['FORCED_PHOTOMETRY']['refmatchrad'])

# maximum tolerable fraction of bad pixels in difference-image
# cutout above which photometry not possible and epoch is skipped.
maxbadpixfrac = float(config_input['FORCED_PHOTOMETRY']['maxbadpixfrac'])

# Radius from input ra,dec position to coarsely seed (and optimize)
# image queries on subtractions DB table [deg].
radthres = float(config_input['FORCED_PHOTOMETRY']['radthres'])

#=================================================================

print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)
print("field =",field)


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Compute forced photometry for given field (all available filters).
    Another name for field is sky tile.
    '''

    rtid = field

    exitcode = 0


    # Read input sky positions.

    with open(sky_positions_csv_file, newline='') as csvfile:

        sky_postions_reader = csv.reader(csvfile, delimiter=',')

        reqid_list = []
        ra_list = []
        dec_list = []

        for row in sky_postions_reader:
            reqid = row[0]
            ra = row[1]
            dec = row[2]
            print(f"reqid,ra,dec = {reqid},{ra},{dec}")
            reqid_list.append(reqid)
            ra_list.append(ra)
            dec_list.append(dec)


    # Get sky positions of center and four corners of sky tile.

    roman_tessellation_db.get_center_sky_position(rtid)
    ra0_field = roman_tessellation_db.ra0
    dec0_field = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(rtid)
    ra1_field = roman_tessellation_db.ra1
    dec1_field = roman_tessellation_db.dec1
    ra2_field = roman_tessellation_db.ra2
    dec2_field = roman_tessellation_db.dec2
    ra3_field = roman_tessellation_db.ra3
    dec3_field = roman_tessellation_db.dec3
    ra4_field = roman_tessellation_db.ra4
    dec4_field = roman_tessellation_db.dec4


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Get difference images that possibly overlap the field.  Returns the following
    # columns:
    # pid,expid,sca,fid,field,jd,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,
    # filename,checksum,infobitssci,infobitsref,rfid,refimfilename,refimchecksum,
    # dist_field_sciimg_center (degrees).

    records = dbh.get_possible_overlapping_diffimages(ppid_sci,
                                                      cattype,
                                                      jd_earliest,
                                                      ra0_field,
                                                      dec0_field,
                                                      match_radius)

    i = 0
    for record in records:
        pid = record[0]
        expid = record[1]
        sca = record[2]
        fid = record[3]
        field = record[4]
        jd = record[5]
        ra0 = record[6]
        dec0 = record[7]
        ra1 = record[8]
        dec1 = record[9]
        ra2 = record[10]
        dec2 = record[11]
        ra3 = record[12]
        dec3 = record[13]
        ra4 = record[14]
        dec4 = record[15]
        filename = record[16]
        checksum = record[17]
        infobitssci = record[18]
        infobitsref = record[19]
        rfid = record[20]
        refimfilename = record[21]
        refimchecksum = record[22]
        ppid_ref = record[23]
        dist_field_sciimg_center = record[24]

        i += 1

        print(f"i,filename,refimfilename,ppid_ref,dist = {i},{filename},{refimfilename},{ppid_ref},{dist_field_sciimg_center}")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to compute forced photometry =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)
