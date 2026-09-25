"""
Ingest Roman WFI L2 files: convert the ASDF files in an input S3 bucket into
multi-extension FITS files in an output S3 bucket, and register them in the
RAPID operations database.

This script combines, in a single pass over each file, what used to be done by
two separate scripts run one after the other:

    sims/src/socsims/convert_socsims.py      ASDF -> FITS, plus the FITS
                                             keywords the pipeline requires.
    database/sims/db_register_socsim_files.py
                                             Registration of the FITS file in
                                             the Exposures, L2Files and
                                             L2FileMeta database tables.

Doing both in one pass means each file is downloaded once, is converted, is
uploaded, and is registered while it is still on local disk, instead of being
written to S3 by one script and pulled back down by the other.  It also removes
the window in which a converted file sits in the output bucket unregistered.

The work list is "every ASDF file in the input bucket that has not yet been
ingested".  Two things put a file on it, and both are needed:

  * It has no current (vbest > 0) row in the L2Files database table.  Selecting
    on vbest rather than on a row merely existing is what lets a run that died
    between addL2File and updateL2File be picked up again: the half-written row
    it left behind still reads vbest = 0.

  * Or it has one, but the ASDF file in the bucket has been modified since that
    row was created, which means it has been redelivered.  The vbest flag alone
    cannot catch this.  updateL2File demotes the old row to vbest = 0 as part of
    registering the new version, so the demotion is a CONSEQUENCE of the ingest
    and cannot also be its trigger -- until the redelivery is ingested, the row
    for the previous delivery still reads vbest = 1.  The S3 last-modified time
    of the ASDF file, against l2files.created, is what tells them apart.

Ingesting a redelivered file registers a new L2Files version and demotes the
previous one, which is the intended behaviour: the file keeps its name, so its
new pixels would otherwise never reach the database.  Set IGNOREASDFTIMESTAMPS
to turn the recency comparison off, for a bucket whose objects have been bulk-
copied and whose timestamps therefore say nothing about deliveries.

The output bucket is consulted as well, but never to decide whether a file has
to be ingested.  A converted FITS file sitting there with no current database
row, and NEWER than the ASDF file it came from, was left by a run that stopped
between the upload and the registration; it is downloaded and registered rather
than converted a second time.  One that is OLDER than its ASDF file was made
from a previous delivery of that file and is stale, so it is converted afresh.


Output FITS layout
------------------

HDU 0, PRIMARY   Keywords only, no data (NAXIS = 0).  It carries the standard
                 short keywords the RAPID pipeline and database registration
                 read (FILTER, EXPTIME, DATE-OBS, MJD-OBS, SCA_NUM, ZPTMAG,
                 ...) and, beneath them, every scalar leaf of the ASDF
                 metadata tree written verbatim as a HIERARCH card whose
                 keyword is the ASDF path to that leaf, e.g.

                     HIERARCH meta exposure exposure_time = 54.72

                 so nothing that can be gleaned from the ASDF header is lost
                 in the conversion.

HDU 1, SCI       The science image, in DN, with the FITS-SIP representation of
                 the ASDF gWCS and the subset of the primary keywords that mean
                 something for an image.  The database registration reads this
                 header, and so does every downstream pipeline step.

HDU 2..n         The remaining ASDF arrays, one image HDU each, named for the
                 ASDF node they came from (ERR, DQ, VAR_POISSON, VAR_RNOISE,
                 VAR_FLAT, AREA, ...), followed by any ASDF tables as binary
                 table HDUs (CAL_LOGS, ...).  Arrays that share the shape of
                 the science image carry the same WCS.


Units
-----

The ASDF L2 data are in DN/s.  The pipeline works in DN, so the science image
is multiplied by EXPTIME, and, so that the uncertainty planes stay consistent
with it, the error array is scaled by the same factor and the variance arrays
by its square.  The data-quality array and everything else are passed through
unscaled.  BUNIT records the result.


Environment
-----------

RAPIDL2INPUTBUCKET      Input S3 bucket holding the ASDF files.  Required.
RAPIDL2OUTPUTBUCKET     Output S3 bucket for the FITS files.  Required.
RAPIDL2INPUTPREFIX      Optional key prefix, to ingest one subdirectory of the
                        input bucket rather than all of it.
RAPID_WORK              Local work directory.  Defaults to /work.
NUM_CORES               Number of parallel processes.  Defaults to the number
                        of CPUs.
SIPDISTORTIONDEGREE     Degree of the SIP fit to the gWCS.  Defaults to 5,
                        which is what the L2Files table stores.
MAXFILESTOINGEST        Stop after this many files, for short tests.
DONTCHECKALREADYINGESTED
                        Set to skip the L2Files query for already-ingested
                        files and (re-)ingest everything in the input bucket.
IGNOREASDFTIMESTAMPS    Set to ignore the S3 last-modified times of the ASDF
                        files, so that a file with a current L2Files row is
                        never re-ingested as a redelivery.
DBPORT, DBNAME, DBUSER, DBPASS, DBSERVER
                        Database connection, as for every RAPID script.
ROMANTESSELLATIONDBNAME
                        SQLite database defining the Roman sky tessellation.
CRDS_PATH, CRDS_SERVER_URL
                        Only needed if the gWCS has to be assigned here; the
                        simulated inputs already carry a correct gWCS.

Timing
------

Each major step of each file is timed, and every one of those lines reads

    Elapsed time in seconds to <step> = <seconds> (<ASDF file>)

so that one grep collects a single step across a whole run and the elapsed times
can be summed or averaged straight out of the log:

    grep "^Elapsed time in seconds to convert" ingestL2Files.log

The file in parentheses is the input ASDF object name throughout, including on
the steps that handle the FITS file, so every line belonging to one unit of work
carries the same identifier.  It is what makes the log usable at all: NUM_CORES
workers share one output stream and their lines interleave.


Exit codes
----------

 0   Finished.  Files that individually failed are logged and skipped, and
     reappear on the next run's work list; they do not make the run a failure.
64   Bad or missing configuration: a required environment variable is not set.
66   A required input is not there: the work directory does not exist.
70   A worker process did not finish, so its whole share of the work list went
     unattempted.  Unlike a failed file, this has to be reported: the work was
     not merely deferred, nobody looked at it.  Where the worker chose its own
     exit code, that code is reported instead of this one.
73   A file could not be created: a per-process log.
67, 69
     Passed through from rapid_db when the database could not be used.

Usage:

    python3 pipeline/ingestL2Files.py
"""

import os
import gzip
import shutil
import time
import numpy as np
import boto3
import healpy as hp
import asdf
import roman_datamodels as rdm
from astropy.io import fits
from astropy.table import Table
from astropy.time import Time
from astropy.wcs import WCS
from datetime import datetime, timezone
from dateutil import tz
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import modules.utils.rapid_data_analysis as rda
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite
from database.modules.utils.overlapping_fields import overlapping_fields


# Define code name and version.

swname = "ingestL2Files.py"
swvers = "1.0"

debug = 1

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Exit codes, following the BSD sysexits values that RAPID already uses, so that a caller --
# ingestL2FilesDaemon.py above all -- can tell from the code alone what kind of failure this
# was, without parsing the log.  A database failure exits with whatever code rapid_db chose
# (67 for a query error, 69 for an unexpected result), which is passed straight through.

exit_code_config = 64             # Bad or missing configuration: a required env. var. is not set.
exit_code_no_input = 66           # A required input is not there: the work directory does not exist.
exit_code_database = 67           # The database could not be used, when rapid_db reports no code of its own.
exit_code_worker_failed = 70      # A worker process did not finish, so part of the work list went undone.
exit_code_cannot_create = 73      # A file could not be created: the per-process log.


def get_int_from_env(name,default,minimum=None):

    '''
    Return an integer environment variable, or the default if it is unset.

    A value that is not an integer, or is below the minimum, quits with a message rather than
    being quietly defaulted or raising a bare ValueError.  A run started with a misspelled
    NUM_CORES should say which variable is wrong and exit with the code that means
    "misconfigured", not hand back a traceback that tells the daemon only that something
    went wrong.
    '''

    value_str = os.getenv(name)

    if value_str is None:
        return default

    try:
        value = int(value_str)
    except ValueError:
        print(f"*** Error: Env. var. {name} = {value_str} is not an integer; quitting...")
        exit(exit_code_config)

    if minimum is not None and value < minimum:
        print(f"*** Error: Env. var. {name} = {value} is less than {minimum}; quitting...")
        exit(exit_code_config)

    return value


# The SIP order the L2Files database table stores, which add_l2file_fifth_order writes a
# column per coefficient for.  The FITS headers are always filled to at least this order, so
# that the registration finds a complete set whatever degree the fit itself was done to.

database_sip_degree = 5


# Global variables.

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


# Input and output S3 buckets.  These are required rather than defaulted, so that a
# misconfigured run cannot silently write into, or re-ingest from, the wrong bucket.

bucket_name_input = os.getenv('RAPIDL2INPUTBUCKET')

if bucket_name_input is None:
    print("*** Error: Env. var. RAPIDL2INPUTBUCKET not set; quitting...")
    exit(exit_code_config)

bucket_name_output = os.getenv('RAPIDL2OUTPUTBUCKET')

if bucket_name_output is None:
    print("*** Error: Env. var. RAPIDL2OUTPUTBUCKET not set; quitting...")
    exit(exit_code_config)

input_prefix = os.getenv('RAPIDL2INPUTPREFIX')

if input_prefix is None:
    input_prefix = ""

print("bucket_name_input =",bucket_name_input)
print("bucket_name_output =",bucket_name_output)
print("input_prefix =",input_prefix)


# Local work directory.

subdir_work = os.getenv('RAPID_WORK')

if subdir_work is None:
    subdir_work = "/work"

print("subdir_work =",subdir_work)


# Degree of the SIP fit to the gWCS.  The L2Files database table stores SIP coefficients
# up to fifth order, so a different degree here would leave the row and the file disagreeing.

sip_distortion_degree = get_int_from_env('SIPDISTORTIONDEGREE',database_sip_degree,minimum=1)


# A higher degree than the table holds would be fitted into the file and then silently
# dropped on the way into the database, leaving the row describing a different distortion
# from the file it points at.  A lower one is allowed: its higher coefficients are genuinely
# zero, and are registered as zero.

if sip_distortion_degree > database_sip_degree:
    print(f"*** Error: Env. var. SIPDISTORTIONDEGREE = {sip_distortion_degree} is above the "
          f"order the L2Files table stores ({database_sip_degree}); quitting...")
    exit(exit_code_config)

print("sip_distortion_degree =",sip_distortion_degree)


# Ensure the CRDS environment is defined, which romancal needs if the gWCS ever has to be
# assigned here.  The simulated inputs already carry a corrected gWCS (see
# sims/src/socsims/inject_fake_sources_into_l2_asdf_files.py), so nothing is downloaded
# in the normal case.

if os.getenv('CRDS_PATH') is None:
    os.environ['CRDS_PATH'] = f"{os.getenv('HOME')}/crds_cache"

if os.getenv('CRDS_SERVER_URL') is None:
    os.environ['CRDS_SERVER_URL'] = "https://roman-crds.stsci.edu"


# Set DONTCHECKALREADYINGESTED to skip the existence check of the files already ingested,
# which are found by querying the L2Files database table for filenames (without S3-bucket names).

do_already_ingested_check = os.getenv('DONTCHECKALREADYINGESTED') is None

print("do_already_ingested_check =",do_already_ingested_check)


# Set IGNOREASDFTIMESTAMPS to fall back on a filename-only already-ingested check, which
# ingests a file only if it has no current L2Files row at all and never re-ingests on the
# strength of a timestamp.  Worth reaching for when the input bucket has been bulk-copied or
# re-synced, which restamps every object and would otherwise present the whole bucket as
# redelivered.

do_asdf_recency_check = os.getenv('IGNOREASDFTIMESTAMPS') is None

print("do_asdf_recency_check =",do_asdf_recency_check)


# Number of parallel processes.

num_cores = get_int_from_env('NUM_CORES',os.cpu_count() or 1,minimum=1)

print("num_cores =",num_cores)


# Optional cap on the number of files ingested in one run, for short tests.

max_files_to_ingest = get_int_from_env('MAXFILESTOINGEST',None,minimum=1)

print("max_files_to_ingest =",max_files_to_ingest)


# Translate the Roman optical-element names into the filter names used by the RAPID
# database Filters table, which follow the Open Universe simulations:
#
# fid | filter
# ----+--------
#   1 | F184
#   2 | H158
#   3 | J129
#   4 | K213
#   5 | R062
#   6 | Y106
#   7 | Z087
#   8 | W146

filter_translation = {"F062": "R062",
                      "F087": "Z087",
                      "F106": "Y106",
                      "F129": "J129",
                      "F146": "W146",
                      "F158": "H158",
                      "F184": "F184",
                      "F213": "K213"}


# Nominal WFI AB zeropoints [mag], used only when the ASDF file carries no photometric
# calibration of its own.  See compute_zptmag for why the file's own calibration is preferred.
#
# Filter  Wavelength (micron)   AB zeropoint (mag)
# F062    0.48 - 0.76           26.4
# F087    0.76 - 0.98           26.3
# F106    0.93 - 1.19           26.4
# F129    1.13 - 1.45           26.3
# F158    1.38 - 1.77           26.4
# F184    1.68 - 2.00           25.9
# F213    1.95 - 2.30           25.4
# F146    0.93 - 2.00           27.5

nominal_zptmag = {"F062": 26.4,
                  "F087": 26.3,
                  "F106": 26.4,
                  "F129": 26.3,
                  "F146": 27.5,
                  "F158": 26.4,
                  "F184": 25.9,
                  "F213": 25.4}


# The ASDF node holding the science image, and the exponent of the EXPTIME factor that
# converts each node from the DN/s of the L2 product into the DN the pipeline works in.
# Nodes not listed here are written out unscaled, which is right for the data-quality
# array, the pixel-area array and the reference-pixel borders.

science_node_name = "data"

exptime_scaling_exponent = {"data": 1,
                            "err": 1,
                            "var_poisson": 2,
                            "var_rnoise": 2,
                            "var_flat": 2}


# The keywords copied from the primary header into the science-image header: those that
# describe the image itself, as opposed to the observation-level metadata that belongs
# only in the primary.  The database registration and the downstream pipeline steps read
# their keywords from the science HDU, so this list is what they are guaranteed to find.
#
# BUNIT is deliberately absent: build_image_hdus gives every array the unit that array
# actually has, and copying the primary's BUNIT here would label the data-quality array DN.

science_image_keywords = ["FILTER",
                          "EXPTIME",
                          "DATE-OBS",
                          "DATE-END",
                          "MJD-OBS",
                          "TSTART",
                          "TEND",
                          "DETECTOR",
                          "SCA_NUM",
                          "TARGAPER",
                          "TARGRA",
                          "TARGDEC",
                          "ZPTMAG",
                          "ORIGFILE",
                          "SWNAME",
                          "SWVERS",
                          "CREATED"]


#-------------------------------------------------------------------------------------------------------------
# Methods for translating the ASDF metadata tree into FITS keywords.
#-------------------------------------------------------------------------------------------------------------

def coerce_header_value(value):

    '''
    Return value in a form a FITS card can hold, or None if it has no sensible scalar
    representation.  Returning None rather than raising keeps one unconvertible leaf of a
    large metadata tree from costing the whole file.
    '''

    if value is None:
        return None


    # Before the scalar branch below, which would otherwise return a NaN unexamined and leave
    # fits.Card to raise on it: FITS headers cannot hold a non-finite floating-point value at
    # all, so there is nothing to write and the leaf is skipped.

    if isinstance(value,(float,np.floating)) and not np.isfinite(value):
        return None

    if isinstance(value,(bool,int,float,str)):
        return value

    if isinstance(value,np.bool_):
        return bool(value)

    if isinstance(value,np.integer):
        return int(value)

    if isinstance(value,np.floating):
        return float(value)

    if isinstance(value,np.str_):
        return str(value)


    if isinstance(value,Time):
        return str(value.isot)

    if isinstance(value,datetime):
        return value.isoformat()


    # An astropy Quantity keeps only its numeric value; its unit is recorded in the card
    # comment by asdf_metadata_cards.

    if hasattr(value,"unit") and hasattr(value,"value"):
        try:
            return float(value.value)
        except (TypeError,ValueError):
            return None


    # A short sequence of scalars is worth keeping as a comma-separated string; anything
    # longer, and any array, belongs in its own HDU, not in a keyword.

    if isinstance(value,(list,tuple)) and len(value) <= 8:

        parts = []

        for element in value:

            coerced = coerce_header_value(element)

            if coerced is None:
                return None

            parts.append(str(coerced))

        return ",".join(parts)

    return None


def is_array_like(value):

    '''
    True if value is an array of one or more dimensions.  The arrays in an ASDF file are
    asdf.tags.core.ndarray.NDArrayType, which is a lazy proxy for a numpy array and is NOT a
    numpy subclass, so the test has to be on behaviour rather than on type.
    '''

    if isinstance(value,(str,bytes)):
        return False

    if not (hasattr(value,"shape") and hasattr(value,"dtype")):
        return False

    return getattr(value,"ndim",0) >= 1


def is_table_like(value):

    '''
    True if value is a table: an astropy Table, or an array whose dtype has named fields.
    '''

    if isinstance(value,Table):
        return True

    if not is_array_like(value):
        return False

    return getattr(value.dtype,"names",None) is not None


def is_metadata_container(value):

    '''
    True if value is a branch of the ASDF metadata tree to be descended into.  ASDF nodes
    and roman_datamodels stnodes are mapping-like without being dicts, so the test is on
    behaviour rather than on type.
    '''

    if isinstance(value,dict):
        return True

    if isinstance(value,(str,bytes)):
        return False

    if is_array_like(value):
        return False

    return hasattr(value,"keys") and hasattr(value,"__getitem__")


def flatten_asdf_metadata(node,prefix="meta"):

    '''
    Walk the ASDF metadata tree and yield (dotted_path, value, unit) for every leaf, where
    unit is the astropy unit of a Quantity leaf and None otherwise.  Arrays are skipped:
    they become HDUs of their own, not keywords.
    '''

    if not is_metadata_container(node):
        return

    for key in sorted(node.keys()):

        try:
            value = node[key]
        except Exception as e:
            print(f"*** Warning: Could not read ASDF metadata {prefix}.{key} ({e}); skipping...")
            continue

        path = f"{prefix}.{key}"

        if is_array_like(value):
            continue

        if is_metadata_container(value):
            yield from flatten_asdf_metadata(value,path)
            continue


        # The gWCS object is a transform, not metadata; its FITS-SIP representation is
        # written to the science HDU instead.

        if key == "wcs":
            continue

        unit = None

        if hasattr(value,"unit") and hasattr(value,"value"):
            unit = str(value.unit)

        yield path,value,unit


def asdf_metadata_cards(meta_node):

    '''
    Return the list of FITS cards holding every scalar leaf of the ASDF metadata tree, each
    keyword being the ASDF path to that leaf, so that the mapping back to the ASDF file is
    exact and no two leaves collide.  Keywords longer than eight characters become HIERARCH
    cards, which is how the whole tree fits in a FITS header without being renamed into
    eight characters and losing its provenance.

    The path is spelled with spaces rather than dots, which is the ESO HIERARCH convention
    (HIERARCH meta exposure exposure_time).  A dot would be worse than cosmetic: astropy
    reads KEYWORD.FIELD with a numeric value as a record-valued keyword card, and would
    silently rewrite such a card as the string "FIELD: value" under the eight-character
    keyword, losing both the path and the numeric type.
    '''

    cards = []

    n_skipped = 0

    for path,value,unit in flatten_asdf_metadata(meta_node):

        coerced = coerce_header_value(value)

        if coerced is None:
            n_skipped += 1
            continue

        comment = ""

        if unit is not None:
            comment = f"[{unit}]"

        keyword = path.replace(".", " ")

        try:
            card = fits.Card(keyword,coerced,comment)
            str(card)                            # Forces the card to be formatted, and to fail here if it cannot be.
        except Exception as e:
            print(f"*** Warning: Could not make a FITS card for ASDF metadata {path} ({e}); skipping...")
            n_skipped += 1
            continue

        cards.append(card)

    print(f"asdf_metadata_cards: n_cards,n_skipped = {len(cards)},{n_skipped}")

    return cards


#-------------------------------------------------------------------------------------------------------------
# Methods for handling the ASDF-to-FITS conversion.
#-------------------------------------------------------------------------------------------------------------

def gwcs_to_fits_sip_header(wcs_obj,shape,degree):

    '''
    Return the FITS-SIP header representing an ASDF gWCS object, fitted to the given degree
    over the full extent of an image of the given shape.
    '''

    fits_wcs_header = wcs_obj.to_fits_sip(
        bounding_box=((0,shape[-1] - 1),(0,shape[-2] - 1)),
        max_pix_error=0.1,
        degree=degree
        )

    return fits_wcs_header


def fill_missing_sip_keywords(hdr,degree):

    '''
    Give every SIP coefficient up to the given degree an explicit value, defaulting the ones
    the fit left out to zero.  awaicgen ignores the SIP distortion altogether unless the
    coefficient set is complete, and the L2Files table has a column per coefficient.
    '''

    for prefix in ("A","B","AP","BP"):

        for i in range(0,degree + 1):

            for j in range(0,degree + 1):

                keyword = f"{prefix}_{i}_{j}"

                if keyword not in hdr:
                    hdr[keyword] = 0.0


def coerce_to_astropy_time(value):

    '''
    Return value as an astropy Time, or None if it cannot be read as one.  The ASDF exposure
    times are already astropy Time objects in current roman_datamodels, but older files carry
    them as ISO strings, so both are accepted.
    '''

    if value is None:
        return None

    if isinstance(value,Time):
        return value

    if isinstance(value,datetime):
        return Time(value)

    try:
        return Time(str(value),format="isot")
    except Exception:
        pass

    try:
        return Time(datetime.strptime(str(value),"%Y-%m-%dT%H:%M:%S.%f"))
    except Exception:
        return None


def compute_zptmag(dm,optical_element):

    '''
    Return the AB zeropoint [mag] for flux in DN/s.

    It is derived from the data's OWN photometric calibration in meta.photometry, so that it
    is self-consistent with romancal's CRDS photom and with the source injection.  The nominal
    per-filter table was found to be 0.5 to 0.6 mag off the true calibration of these
    simulations (F146, in particular), so it is used only as a fallback.

        SB[MJy/sr] = conversion_megajanskys * S[DN/s]
        F_pt[Jy]   = conversion_megajanskys * 1e6 * pixel_area[sr] * S[DN/s]
        ZPTMAG     = -2.5 * log10(conversion_megajanskys * 1e6 * pixel_area / 3631)
    '''

    phot = getattr(dm.meta,"photometry",None)
    conv = getattr(phot,"conversion_megajanskys",None)
    pixarea = getattr(phot,"pixel_area",None)

    if conv is not None and pixarea is not None and conv > 0 and pixarea > 0:
        return -2.5 * np.log10(float(conv) * 1.0e6 * float(pixarea) / 3631.0)

    print("*** Warning: meta.photometry unavailable; using the nominal ZPTMAG table")

    if optical_element not in nominal_zptmag:
        print(f"*** Error: Unexpected optical element = {optical_element}")
        return None

    return nominal_zptmag[optical_element]


def build_primary_header(dm,asdf_tree,input_asdf_file):

    '''
    Return the primary FITS header: the standard short keywords the RAPID pipeline reads,
    followed by every scalar leaf of the ASDF metadata tree.  Returns None if a keyword the
    pipeline cannot do without could not be determined.
    '''

    hdr = fits.Header()


    # Provenance of the conversion itself, which is not in the ASDF file.

    hdr["SWNAME"] = (swname,"software that made this file")
    hdr["SWVERS"] = (swvers,"version of that software")
    hdr["CREATED"] = (proc_utc_datetime,"UTC datetime this file was made")
    hdr["ORIGFILE"] = (os.path.basename(input_asdf_file),"ASDF file this file was made from")

    asdf_library = asdf_tree.get("asdf_library") if hasattr(asdf_tree,"get") else None

    if asdf_library is not None:
        version = asdf_library.get("version") if hasattr(asdf_library,"get") else None
        if version is not None:
            hdr["ASDFVERS"] = (str(version),"asdf library version of the input file")


    # Filter.

    optical_element = str(dm.meta.instrument.optical_element).strip()

    if optical_element not in filter_translation:
        print(f"*** Error: Unexpected optical element = {optical_element}")
        return None

    hdr["FILTER"] = (filter_translation[optical_element],"filter used")


    # Detector and SCA number.

    detector = str(dm.meta.instrument.detector).strip()

    hdr["DETECTOR"] = (detector,"detector assembly")

    try:
        hdr["SCA_NUM"] = (int(detector.replace("WFI","")),"sensor chip assembly number")
    except ValueError:
        print(f"*** Error: Could not get an SCA number from detector = {detector}")
        return None


    # Exposure timing.  MJD-OBS is computed here rather than taken from the ASDF file so that
    # it is guaranteed to refer to the same instant as DATE-OBS.

    exptime = float(dm.meta.exposure.exposure_time)


    # An exposure time that is not a positive, finite number cannot be scaled by.  This has
    # to be refused rather than used: the Roman data model fills an unset float with
    # -999999.0, and multiplying the science image by that produces a FITS file that looks
    # perfectly well formed, converts without complaint, and carries nothing but garbage --
    # which then gets uploaded, registered, and given a limiting magnitude.  Silence is the
    # danger here, so the file is failed and left on the work list instead.

    if not np.isfinite(exptime) or exptime <= 0.0:
        print(f"*** Error: Exposure time = {exptime} is not a positive, finite number")
        return None

    time_start = coerce_to_astropy_time(dm.meta.exposure.start_time)
    time_end = coerce_to_astropy_time(dm.meta.exposure.end_time)

    if time_start is None:
        print(f"*** Error: Could not parse observation start time = {dm.meta.exposure.start_time}")
        return None

    dateobs = str(time_start.isot)
    dateend = str(time_end.isot) if time_end is not None else dateobs


    # MJD-OBS is computed from the same astropy Time as DATE-OBS, rather than read from its
    # own ASDF entry, so that the two cannot end up describing different instants.

    mjdobs = float(time_start.mjd)

    hdr["EXPTIME"] = (exptime,"[s] time on source")
    hdr["DATE-OBS"] = (dateobs,"observation start in UTC calendar date")
    hdr["DATE-END"] = (dateend,"observation end in UTC calendar date")
    hdr["MJD-OBS"] = (mjdobs,"[d] observation start as MJD")


    # Pointing.  The target aperture is normally WFI_CEN, but a proposal may move it, so it
    # is recorded alongside the target sky position rather than assumed.

    pointing = getattr(dm.meta,"pointing",None)

    targ_aper = getattr(pointing,"target_aperture",None)
    targ_ra = getattr(pointing,"target_ra",None)
    targ_dec = getattr(pointing,"target_dec",None)

    if targ_aper is not None:
        hdr["TARGAPER"] = (str(targ_aper),"target aperture")

    if targ_ra is not None:
        hdr["TARGRA"] = (float(targ_ra),"[deg] right ascension of target aperture")

    if targ_dec is not None:
        hdr["TARGDEC"] = (float(targ_dec),"[deg] declination of target aperture")


    # Photometric zeropoint.

    zptmag = compute_zptmag(dm,optical_element)

    if zptmag is None:
        return None

    hdr["ZPTMAG"] = (zptmag,"[mag] AB zeropoint for flux in DN/s")


    # Units of the science image, after the conversion from DN/s done by build_image_hdus.

    hdr["BUNIT"] = ("DN","units of the science image")

    hdr["EQUINOX"] = (2000.0,"[yr] equinox of equatorial coordinates")
    hdr["RADESYS"] = ("ICRS","equatorial coordinate system")


    # Everything else the ASDF header holds, keyed by its dotted ASDF path.

    hdr.append(fits.Card("COMMENT","-" * 60))
    hdr.append(fits.Card("COMMENT","ASDF metadata below, keyed by its path in the ASDF tree"))
    hdr.append(fits.Card("COMMENT","e.g. HIERARCH meta exposure exposure_time = 54.72"))
    hdr.append(fits.Card("COMMENT","-" * 60))

    for card in asdf_metadata_cards(dm.meta):
        hdr.append(card)

    return hdr


def build_science_header(primary_hdr,wcs_header,shape,extname):

    '''
    Return the header of an image HDU: the FITS-SIP WCS, plus the keywords of the primary
    header that describe the image rather than the observation as a whole.
    '''

    hdr = fits.Header()

    hdr.update(wcs_header)

    hdr["NAXIS"] = 2
    hdr["NAXIS1"] = shape[-1]
    hdr["NAXIS2"] = shape[-2]

    # Filled to the order the database stores, not merely to the order fitted: a lower-degree
    # fit leaves the higher coefficients genuinely zero, and the registration below reads a
    # fixed fifth-order set that has to find every one of them.

    fill_missing_sip_keywords(hdr,max(sip_distortion_degree,database_sip_degree))

    for keyword in science_image_keywords:

        if keyword in primary_hdr:
            hdr[keyword] = (primary_hdr[keyword],primary_hdr.comments[keyword])

    hdr["EXTNAME"] = extname

    return hdr


def asdf_image_nodes(dm):

    '''
    Return the list of (node_name, array) for every image array in the ASDF file, with the
    science image first and the rest in the order the ASDF file gives them.
    '''

    nodes = []

    for key in dm.keys():

        if key == "meta":
            continue

        try:
            value = dm[key]
        except Exception as e:
            print(f"*** Warning: Could not read ASDF node {key} ({e}); skipping...")
            continue

        if not is_array_like(value) or is_table_like(value):
            continue

        if value.ndim < 2:
            continue


        # A degenerate array carries nothing, and an axis of length zero has no valid FITS
        # representation, so it is left out rather than written as an empty HDU.

        if value.size == 0:
            print(f"*** Warning: ASDF node {key} has shape {value.shape} and no pixels; skipping...")
            continue

        nodes.append((key,value))

    nodes.sort(key=lambda node: (node[0] != science_node_name,node[0]))

    return nodes


def asdf_table_nodes(dm):

    '''
    Return the list of (node_name, table) for every tabular node in the ASDF file: structured
    arrays, astropy tables, and the lists of strings that romancal writes for its logs.
    '''

    nodes = []

    for key in dm.keys():

        if key == "meta":
            continue

        try:
            value = dm[key]
        except Exception as e:
            print(f"*** Warning: Could not read ASDF node {key} ({e}); skipping...")
            continue

        if is_table_like(value):
            nodes.append((key,value))
            continue


        # romancal's cal_logs is a list of strings, which is a one-column table.

        if isinstance(value,(list,tuple)) and len(value) > 0 and \
           all(isinstance(element,str) for element in value):
            nodes.append((key,Table({key: [str(element) for element in value]})))

    return nodes


def build_image_hdu(node_name,array,primary_hdr,wcs_header,science_shape,exptime):

    '''
    Return the image HDU for one ASDF array, converted from the DN/s of the L2 product into
    the DN the pipeline works in by the exponent of EXPTIME that its units call for, and
    carrying the science WCS if it is laid out like the science image.

    One HDU at a time, rather than a list of all of them, because at the real SCA size of
    4088 x 4088 each array is 67 MB as float32 and there are ten of them; holding the whole
    set in memory to hand to writeto is what made a single conversion peak at 1.7 GB, and
    num_cores of those at once is what a machine does not have.
    '''

    extname = "SCI" if node_name == science_node_name else node_name.upper()

    exponent = exptime_scaling_exponent.get(node_name,0)

    if exponent == 0:


        # FITS has no half-precision floating-point format, so a float16 ASDF array is
        # widened rather than silently truncated to an integer type by the writer.

        if array.dtype == np.float16:
            data = np.array(array,dtype=np.float32)
        else:
            data = np.array(array)

    else:


        # Read straight into the float32 the file is written in and scale in place.  Going
        # through float64 doubled the working set of every array for no gain: the result is
        # rounded back to float32 regardless, and float32 carries about seven significant
        # digits, far beyond what these data are known to.

        data = np.array(array,dtype=np.float32)

        data *= np.float32(float(exptime) ** exponent)


    # Only an array laid out like the science image can share its WCS; a reference-pixel
    # border or an amplifier array covers different pixels and would be mislocated by it.

    if array.shape == science_shape:
        hdr = build_science_header(primary_hdr,wcs_header,array.shape,extname)
    else:
        hdr = fits.Header()
        hdr["EXTNAME"] = extname

    hdr["ASDFNODE"] = (node_name,"node this array came from in the ASDF file")

    if exponent == 0:

        # An unscaled array keeps whatever unit the ASDF file gave it.  A data-quality
        # array or a segmentation map has none, and is left without a BUNIT rather than
        # given a made-up one.

        unit = str(getattr(array,"unit","")).strip()

        if unit:
            hdr["BUNIT"] = (unit,"units of this array")

    elif exponent == 1:
        hdr["BUNIT"] = ("DN","units of this array")
    else:
        hdr["BUNIT"] = ("DN**2","units of this array")

    hdr["EXPTSCAL"] = (float(exptime) ** exponent,"factor applied to convert from the ASDF units")

    return fits.ImageHDU(data=data,header=hdr,name=extname)


def build_table_hdu(node_name,table):

    '''
    Return the binary-table HDU for one tabular ASDF node, or None if it could not be
    converted, which costs that table rather than the whole file.
    '''

    extname = node_name.upper()

    try:
        hdu = fits.BinTableHDU(data=Table(table),name=extname)
    except Exception as e:
        print(f"*** Warning: Could not convert ASDF table {node_name} to a FITS table ({e}); skipping...")
        return None

    hdu.header["ASDFNODE"] = (node_name,"node this table came from in the ASDF file")

    return hdu


def asdf_to_fits(asdf_path,fits_path):

    '''
    Convert one ASDF file into a multi-extension FITS file.  Returns True on success and
    False if the file could not be converted, which leaves it unregistered and so still on
    the work list of the next run.
    '''

    print(f"Reading {asdf_path}...")


    # Memory-mapped, so that reading an array does not also buy a permanent heap copy of it.
    # roman_datamodels caches every node it is asked for, and at the real SCA size the ten
    # arrays of an L2 file are most of a gigabyte; mapped, their pages are file backed and
    # the kernel can drop them again once each has been converted and written.  Every array
    # is copied into its HDU before dm is closed, so nothing outlives the mapping.

    dm = rdm.open(asdf_path,memmap=True)

    try:
        return build_fits_file(dm,asdf_path,fits_path)
    finally:
        dm.close()


def build_fits_file(dm,asdf_path,fits_path):

    '''
    Write the multi-extension FITS file for an open Roman data model.  Every array is copied
    into its HDU here, while the data model is still open, so that asdf_to_fits can close it.
    '''


    # The raw ASDF tree is opened alongside the data model only for the few entries that sit
    # outside the roman node, such as the version of the asdf library that wrote the file.

    try:
        with asdf.open(asdf_path) as af:
            asdf_tree = dict(af.tree)
    except Exception as e:
        print(f"*** Warning: Could not read the raw ASDF tree of {asdf_path} ({e}); continuing...")
        asdf_tree = {}

    primary_hdr = build_primary_header(dm,asdf_tree,asdf_path)

    if primary_hdr is None:
        print(f"*** Error: Could not build the primary header for {asdf_path}")
        return False

    exptime = primary_hdr["EXPTIME"]

    image_nodes = asdf_image_nodes(dm)

    if len(image_nodes) == 0 or image_nodes[0][0] != science_node_name:
        print(f"*** Error: ASDF file has no {science_node_name} array")
        return False

    science_shape = image_nodes[0][1].shape

    wcs_header = gwcs_to_fits_sip_header(dm.meta.wcs,science_shape,sip_distortion_degree)


    # The file is written one HDU at a time rather than assembled into an HDUList and handed
    # to writeto, so that only one image array is ever in memory.  At the real SCA size the
    # whole set is well over a gigabyte, and this script runs num_cores conversions at once.
    #
    # A partly written file is worse than no file, since the registration that follows would
    # checksum and register it, so the HDUs go to a temporary name and are moved into place
    # only once the last one is down.

    partial_fits_path = fits_path + ".partial"

    if os.path.exists(partial_fits_path):
        os.remove(partial_fits_path)

    extnames = []

    try:


        # The primary HDU holds keywords only, so it is given no data at all.  It is written
        # with writeto rather than appended because fits.append refuses a None data argument;
        # with no data to write there is nothing to gain from appending it anyway.

        fits.HDUList([fits.PrimaryHDU(header=primary_hdr)]).writeto(partial_fits_path,
                                                                    overwrite=True,
                                                                    checksum=True)

        extnames.append("PRIMARY")

        for node_name,array in image_nodes:

            hdu = build_image_hdu(node_name,array,primary_hdr,wcs_header,science_shape,exptime)

            fits.append(partial_fits_path,hdu.data,hdu.header,checksum=True)

            extnames.append(hdu.name)


            # Drop this array before reading the next one, which is the whole point of
            # writing incrementally.

            del hdu

        for node_name,table in asdf_table_nodes(dm):

            hdu = build_table_hdu(node_name,table)

            if hdu is None:
                continue

            fits.append(partial_fits_path,hdu.data,hdu.header,checksum=True)

            extnames.append(hdu.name)

            del hdu

    except Exception:

        if os.path.exists(partial_fits_path):
            os.remove(partial_fits_path)

        raise

    os.replace(partial_fits_path,fits_path)

    print(f"Wrote {fits_path} with {len(extnames)} HDUs: " + ",".join(extnames))

    return True


def gzip_file(input_filename,output_filename):

    '''
    Write a gzipped copy of a file, keeping the original, which the registration still needs
    to read the header from and to compute the limiting magnitude with.
    '''

    with open(input_filename,'rb') as fh_in:
        with gzip.open(output_filename,'wb') as fh_out:
            shutil.copyfileobj(fh_in,fh_out)


#-------------------------------------------------------------------------------------------------------------
# Methods for L2-file database registration.
#-------------------------------------------------------------------------------------------------------------

def get_keyword_value(header,key):

    try:
        value = header[key]
    except:
        value = 'null'

    return value


def get_fits_header(fits_filename):

    hdul_input = fits.open(fits_filename)

    header = hdul_input[1].header         # Not the PRIMARY header, but the science-image header.

    hdul_input.close()

    return header


def compute_center_sky_position(header,wcs):

    key = "NAXIS1"
    naxis1 = get_keyword_value(header,key)

    key = "NAXIS2"
    naxis2 = get_keyword_value(header,key)

    x0 = 0.5 * naxis1 + 0.5 - 1.0     # Integer pixel coordinates are zero-based and centered on pixel.
    y0 = 0.5 * naxis2 + 0.5 - 1.0

    sky0 = wcs.pixel_to_world(x0, y0)

    return sky0


def compute_corner_sky_positions(header,wcs):

    key = "NAXIS1"
    naxis1 = get_keyword_value(header,key)

    key = "NAXIS2"
    naxis2 = get_keyword_value(header,key)

    # Integer pixel coordinates are zero-based and centered on pixel.

    x1 = 0.5 - 1.0     # We want the extreme outer image edges.
    y1 = 0.5 - 1.0

    x2 = naxis1 + 0.5 - 1.0
    y2 = 0.5 - 1.0

    x3 = naxis1 + 0.5 - 1.0
    y3 = naxis2 + 0.5 - 1.0

    x4 = 0.5 - 1.0
    y4 = naxis2 + 0.5 - 1.0

    sky1 = wcs.pixel_to_world(x1, y1)
    sky2 = wcs.pixel_to_world(x2, y2)
    sky3 = wcs.pixel_to_world(x3, y3)
    sky4 = wcs.pixel_to_world(x4, y4)

    return sky1,sky2,sky3,sky4


def register_exposure(dbh,roman_tessellation_db,header,wcs):

    '''
    Insert or update the record of this exposure in the Exposures database table, and return
    its expid and fid.
    '''

    key = "DATE-OBS"
    dateobs = get_keyword_value(header,key)

    key = "MJD-OBS"
    mjdobs = get_keyword_value(header,key)

    key = "FILTER"
    filter = get_keyword_value(header,key)

    key = "EXPTIME"
    exptime = get_keyword_value(header,key)

    if 'null' in (dateobs,mjdobs,filter,exptime):
        print("*** Error: Exposure is missing DATE-OBS, MJD-OBS, FILTER or EXPTIME")
        return None,None

    infobits = 0
    status = 1


    # Look up sky position of the WFI center.

    key = "TARGRA"
    targra = get_keyword_value(header,key)

    key = "TARGDEC"
    targdec = get_keyword_value(header,key)

    if targra == 'null' or targdec == 'null':

        print(f"*** Warning: Exposure for dateobs,mjdobs = {dateobs},{mjdobs} has missing TARGRA or TARGDEC; continuing...")

        sky0 = compute_center_sky_position(header,wcs)        # Fall back on the image center.

        ra0 = sky0.ra.degree
        dec0 = sky0.dec.degree

    else:

        ra0 = targra
        dec0 = targdec


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Compute field.

    roman_tessellation_db.get_rtid(ra0,dec0)
    field = roman_tessellation_db.rtid


    # Insert or update record in Exposures database table.

    print("dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status = ",\
        dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status)

    dbh.add_exposure(dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status)

    expid = dbh.expid
    fid = dbh.fid

    print("expid =",expid)
    print("fid =",fid)

    return expid,fid


# Cache of PSF files already downloaded by this process, keyed by (fid,sca).  The ingest runs
# one process per core, so this is per-process; the local filenames carry the process id so
# that two processes downloading the same PSF cannot collide on one file.

psf_file_cache = {}


def get_psf_file(dbh,fid,sca):

    '''
    Local path of the science-image PSF for a filter and SCA, or None if there is none.

    The PSF comes from the PSFs database table, the same source the science pipeline uses, so
    the limiting magnitude is computed with the very PSF the photometry will later use.
    '''

    key = (fid,sca)

    if key in psf_file_cache:
        return psf_file_cache[key]

    exit_code_before = dbh.exit_code

    psfid,s3_full_name_psf = dbh.get_best_psf(sca,fid)


    # A missing PSF is a normal condition for a filter and SCA that has none registered yet,
    # not a database failure, so do not let get_best_psf's exit code leak into the ingest.

    dbh.exit_code = exit_code_before

    if psfid is None or s3_full_name_psf is None:
        print(f"*** Warning: No PSF registered for fid,sca = {fid},{sca}; "
              "limiting magnitude will not be computed")
        psf_file_cache[key] = None
        return None

    local_psf_filename = f"{subdir_work}/psf_fid{fid}_sca{sca}_pid{os.getpid()}.fits"

    download_cmd = ['aws','s3','cp',s3_full_name_psf,local_psf_filename]
    exitcode_from_download_cmd = util.execute_command(download_cmd)

    if exitcode_from_download_cmd != 0 or not os.path.exists(local_psf_filename):
        print(f"*** Warning: Could not download PSF {s3_full_name_psf}; "
              "limiting magnitude will not be computed")
        psf_file_cache[key] = None
        return None

    psf_file_cache[key] = local_psf_filename

    return local_psf_filename


def compute_limmag(dbh,fits_filename,fid,sca):

    '''
    The 5-sigma point-source limiting magnitude of an L2 file, or None.

    The L2 data are in DN and ZPTMAG is the zeropoint for DN/s, so the conversion by EXPTIME
    is left to rapid_data_analysis, which reads both from the header.  Returns None rather
    than raising, because a limiting magnitude that cannot be computed must leave the column
    NULL without stopping the ingest of an otherwise good file.
    '''

    psf_filename = get_psf_file(dbh,fid,sca)

    if psf_filename is None:
        return None

    try:
        limmag_dict = rda.compute_limiting_magnitude_for_l2_image(fits_filename,
                                                                 psf_filename,
                                                                 hdu_index = 1)
    except Exception as e:
        print(f"*** Warning: Could not compute limiting magnitude for {fits_filename} ({e}); "
              "registering a NULL")
        return None

    limmag = limmag_dict["maglimit"]

    print(f"limmag = {limmag}")

    return limmag


def register_l2file(dbh,roman_tessellation_db,header,wcs,fits_filename,s3_object_name,expid,fid):

    '''
    Insert the record of this L2 file in the L2Files database table, and return its rid,
    version, registered filename and checksum.
    '''

    key = "DATE-OBS"
    dateobs = get_keyword_value(header,key)

    key = "MJD-OBS"
    mjdobs = get_keyword_value(header,key)

    key = "EXPTIME"
    exptime = get_keyword_value(header,key)

    key = "SCA_NUM"
    sca = get_keyword_value(header,key)

    key = "CRVAL1"
    crval1 = get_keyword_value(header,key)

    key = "CRVAL2"
    crval2 = get_keyword_value(header,key)

    key = "CRPIX1"
    crpix1 = get_keyword_value(header,key)

    key = "CRPIX2"
    crpix2 = get_keyword_value(header,key)

    key = "CD1_1"
    cd11 = get_keyword_value(header,key)

    key = "CD1_2"
    cd12 = get_keyword_value(header,key)

    key = "CD2_1"
    cd21 = get_keyword_value(header,key)

    key = "CD2_2"
    cd22 = get_keyword_value(header,key)

    key = "CTYPE1"
    ctype1 = get_keyword_value(header,key)

    key = "CTYPE2"
    ctype2 = get_keyword_value(header,key)

    key = "CUNIT1"
    cunit1 = get_keyword_value(header,key)

    key = "CUNIT2"
    cunit2 = get_keyword_value(header,key)


    # The SIP coefficients.  Collected over the order the DATABASE stores rather than the
    # order the fit was done to, because add_l2file_fifth_order below asks for a fixed
    # fifth-order set by name; iterating the fitted degree instead would leave the higher
    # coefficients absent from this dictionary and every one of those lookups a KeyError.
    # build_science_header has filled the header to the same order, so all of them are there.

    sip_values = {}

    for prefix in ("A","B"):

        sip_values[f"{prefix}_ORDER"] = get_keyword_value(header,f"{prefix}_ORDER")

        for i in range(0,database_sip_degree + 1):
            for j in range(0,database_sip_degree + 1):
                if i + j > database_sip_degree:
                    continue
                sip_values[f"{prefix}_{i}_{j}"] = get_keyword_value(header,f"{prefix}_{i}_{j}")

    key = "ZPTMAG"
    zptmag = get_keyword_value(header,key)

    equinox = 2000
    paobsy = 0.0
    pafpa = 0.0
    skymean = 0.0


    # Compute the checksum of the very file that was uploaded, which is the gzipped one.

    print("fits_filename =",fits_filename)

    checksum = db.compute_checksum(fits_filename)

    if checksum == 65 or checksum == 68 or checksum == 66:
        print("*** Error: Unexpected value for checksum =",checksum)
        return None,None,None,None

    filename = "s3://" + bucket_name_output + "/" + s3_object_name
    infobits = 0
    status = 0         # Keep status = 0 until vbest is updated in a later step.


    # Compute sky position of image center.

    sky0 = compute_center_sky_position(header,wcs)

    ra0 = sky0.ra.degree
    dec0 = sky0.dec.degree


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Compute field.

    roman_tessellation_db.get_rtid(ra0,dec0)
    field = roman_tessellation_db.rtid


    # Compute the sky tiles the image OVERLAPS, not just the one holding its centre.  `field`
    # above is one tile chosen by one point; an SCA covers several (median 7), and the
    # l2files table has a column for the whole footprint.  Computed here from the same WCS
    # values about to be written to the row, so the footprint and the WCS it derives from are
    # always consistent -- and from the header's own NAXIS1/NAXIS2 rather than a configured
    # detector size, which is the truest extent for this particular file.

    overlapfields = overlapping_fields(crval1,crval2,crpix1,crpix2,
                                       cd11,cd12,cd21,cd22,
                                       get_keyword_value(header,"NAXIS1"),
                                       get_keyword_value(header,"NAXIS2"),
                                       field=field)


    # Compute the limiting magnitude while the FITS file is still on local disk; the caller
    # deletes it as soon as registration finishes.

    limmag = compute_limmag(dbh,fits_filename,fid,sca)

    dbh.add_l2file_fifth_order(expid,sca,field,overlapfields,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
        status,filename,checksum,crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22,
        ctype1,ctype2,cunit1,cunit2,
        sip_values["A_ORDER"],
        sip_values["A_0_1"],sip_values["A_0_2"],sip_values["A_0_3"],sip_values["A_0_4"],sip_values["A_0_5"],
        sip_values["A_1_0"],sip_values["A_1_1"],sip_values["A_1_2"],sip_values["A_1_3"],sip_values["A_1_4"],
        sip_values["A_2_0"],sip_values["A_2_1"],sip_values["A_2_2"],sip_values["A_2_3"],
        sip_values["A_3_0"],sip_values["A_3_1"],sip_values["A_3_2"],
        sip_values["A_4_0"],sip_values["A_4_1"],
        sip_values["A_5_0"],
        sip_values["B_ORDER"],
        sip_values["B_0_1"],sip_values["B_0_2"],sip_values["B_0_3"],sip_values["B_0_4"],sip_values["B_0_5"],
        sip_values["B_1_0"],sip_values["B_1_1"],sip_values["B_1_2"],sip_values["B_1_3"],sip_values["B_1_4"],
        sip_values["B_2_0"],sip_values["B_2_1"],sip_values["B_2_2"],sip_values["B_2_3"],
        sip_values["B_3_0"],sip_values["B_3_1"],sip_values["B_3_2"],
        sip_values["B_4_0"],sip_values["B_4_1"],
        sip_values["B_5_0"],
        equinox,ra0,dec0,paobsy,pafpa,zptmag,skymean,limmag=limmag)

    rid = dbh.rid
    version = dbh.version

    print("rid =",rid)
    print("version =",version)

    return rid,version,filename,checksum


def finalize_l2file(dbh,rid,version,filename,checksum):

    status = 1


    # Update record in L2Files database table.

    dbh.update_l2file(rid,filename,checksum,status,version)


def compute_and_register_l2filemeta(dbh,header,wcs,rid,fid):

    key = "SCA_NUM"
    sca = get_keyword_value(header,key)

    key = "MJD-OBS"
    mjdobs = get_keyword_value(header,key)

    sky0 = compute_center_sky_position(header,wcs)
    sky1,sky2,sky3,sky4 = compute_corner_sky_positions(header,wcs)

    ra0 = sky0.ra.degree
    dec0 = sky0.dec.degree
    ra1 = sky1.ra.degree
    dec1 = sky1.dec.degree
    ra2 = sky2.ra.degree
    dec2 = sky2.dec.degree
    ra3 = sky3.ra.degree
    dec3 = sky3.dec.degree
    ra4 = sky4.ra.degree
    dec4 = sky4.dec.degree

    x,y,z = util.compute_xyz(ra0,dec0)


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Register record in database.

    dbh.register_l2filemeta(rid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,x,y,z,hp6,hp9,fid,sca,mjdobs)


def register_fits_file(dbh,roman_tessellation_db,local_fits_file,local_gzipped_fits_file,s3_object_name):

    '''
    Register one converted L2 FITS file in the Exposures, L2Files and L2FileMeta database
    tables.  Returns True on success.

    The header is read from the uncompressed file and the checksum from the gzipped one,
    which is the file that was uploaded, so the checksum in the database is the checksum of
    the object in the bucket.
    '''

    header = get_fits_header(local_fits_file)

    wcs = WCS(header)

    expid,fid = register_exposure(dbh,roman_tessellation_db,header,wcs)

    if expid is None:
        return False

    rid,version,filename,checksum = register_l2file(dbh,roman_tessellation_db,header,wcs,
                                                    local_gzipped_fits_file,s3_object_name,
                                                    expid,fid)

    if rid is None:
        return False

    finalize_l2file(dbh,rid,version,filename,checksum)     # Keep same filename and version for now.

    compute_and_register_l2filemeta(dbh,header,wcs,rid,fid)

    return True


#-------------------------------------------------------------------------------------------------------------
# Methods for the work list.
#-------------------------------------------------------------------------------------------------------------

def output_fits_object_name(input_asdf_file):

    '''
    Return the output S3 object name for an input ASDF object name.  Both the uncompressed
    and the gzipped form of the input are accepted, and the output is always gzipped.
    '''

    name = input_asdf_file

    if name.endswith(".gz"):
        name = name[:-len(".gz")]

    if name.endswith(".asdf"):
        name = name[:-len(".asdf")]

    return name + ".fits.gz"


def list_bucket_objects(bucket_name,prefix="",suffixes=None):

    '''
    Return {object name: last-modified datetime} for the objects in an S3 bucket, paging
    through a listing of any length and keeping only the ones ending in one of the given
    suffixes.  All the suffixes are matched in one pass, because these buckets hold tens of
    thousands of objects and a separate listing per suffix would page through all of them
    again.

    The modification times come free with the listing, and are what lets the caller tell a
    converted file still waiting to be registered from one left over from an earlier
    delivery of the same ASDF file.
    '''

    s3_client = boto3.client('s3')

    objects = {}

    paginator = s3_client.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=bucket_name,Prefix=prefix):

        for entry in page.get('Contents',[]):

            key = str(entry['Key'])

            if suffixes and not key.endswith(tuple(suffixes)):
                continue

            objects[key] = entry['LastModified']

    return objects


def get_ingested_l2file_times(dbh):

    '''
    Return {L2 FITS filename without its S3-bucket name: when it was ingested, in UTC} for
    every file with a CURRENT row in the L2Files database table.  The time is None where the
    row has no `created` timestamp.

    Two things about that query are worth saying plainly, because they decide what gets
    ingested and neither is obvious from the column names.

    First, the row has to be selected on vbest > 0 rather than on merely existing.  addL2File
    inserts a row with vbest = 0, and it is updateL2File -- the finalize step -- that promotes
    it to 1.  So a run that died between the two leaves a vbest = 0 row behind, and that row
    has to let the file back onto the work list; re-ingesting it registers a fresh version and
    promotes that.  A locked record (vbest = 2) matches as well, and correctly so: it is
    current, and must not be touched.

    Second, and the reason this returns times rather than a set: a vbest > 0 row does NOT mean
    the file on hand has been ingested, only that SOME delivery of it has.  The demotion of an
    old row to vbest = 0 happens as a consequence of ingesting the new version -- it is
    updateL2File that sets the old rows to 0 when it promotes the new one -- so it cannot also
    be the signal that an ingest is due.  Until this script ingests a redelivered file, the
    row for the previous delivery still reads vbest = 1.  A redelivery therefore has to be
    recognised some other way, and `created` against the S3 last-modified time of the ASDF
    file is that way.

    `created` is a timestamp without a time zone, holding local time in whatever zone the
    database is set to (America/Los_Angeles; see database/schema/rapidOpsTimeZone.sql).  The
    cast to timestamptz resolves it using that same session zone, DST included, and the result
    is converted to UTC so it can be compared with the UTC times S3 reports.
    '''

    query = ("select (regexp_match(filename, '.+/(.+)'))[1], "
             "max(created::timestamptz at time zone 'UTC') "
             "from l2files where vbest > 0 group by 1;")

    records = dbh.execute_sql_queries([query],debug)


    # rapid_db returns None, having set its own exit code, when the query failed.  Falling
    # through on that would make an empty result indistinguishable from "nothing has been
    # ingested", and the next thing this script does with that answer is re-ingest every file
    # in the bucket.  Report it instead and let the caller stop.

    if records is None:
        print("*** Error: Could not query L2Files for the files already ingested")
        return None

    ingested_l2file_times = {}

    for record in records:

        created = record[1]

        if created is not None:
            created = created.replace(tzinfo=timezone.utc)

        ingested_l2file_times[record[0]] = created

    return ingested_l2file_times


#-------------------------------------------------------------------------------------------------------------
# Methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def log_elapsed_time(action,start_time,l2_file):

    '''
    Report how long one step of one file's ingest took, and return the time it ended, which is
    the start of the next step.

    Every line reads

        Elapsed time in seconds to <action> = <seconds> (<file>)

    The common opening is what makes the log greppable: one grep collects a single step across
    a whole run, and the elapsed times can be summed or averaged straight out of it.  The file
    in parentheses is what makes that possible at all, since num_cores workers share this
    output stream and their lines interleave; without it a line could not be attributed to the
    file it describes.
    '''

    end_time = time.time()

    print(f"Elapsed time in seconds to {action} = {end_time - start_time} ({l2_file})")

    return end_time


def run_single_core_job(asdf_files,index_thread,reusable_output_fits_files=None):

    '''
    Convert and register the share of the work list belonging to one process.

    The database connection and the sky-tessellation database are opened here, inside the
    child process, rather than inherited from the parent, so that no two processes can end up
    sharing one connection.

    `reusable_output_fits_files` is the set of output objects the main program found already
    in the output bucket AND newer than the ASDF file they came from.  Such a file was
    converted by an earlier run (or by convert_socsims.py, before this script replaced it)
    but never registered, so it is downloaded and registered rather than converted again --
    the conversion is by far the most expensive step.  An output object older than its ASDF
    file is stale, is not in this set, and is converted afresh.
    '''

    if reusable_output_fits_files is None:
        reusable_output_fits_files = set()

    thread_start_time_benchmark = time.time()

    n_asdf_files = len(asdf_files)

    print("index_thread,n_asdf_files =",index_thread,n_asdf_files)

    thread_work_file = f"{subdir_work}/" + swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file,'w',encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(exit_code_cannot_create)

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.close()
        exit(dbh.exit_code)

    roman_tessellation_db = sqlite.RomanTessellationNSIDE512()

    s3_client = boto3.client('s3')

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}\n")

    n_ingested = 0
    n_failed = 0


    # Loop over input ASDF files.

    for index_asdf_file in range(n_asdf_files):

        index_core = index_asdf_file % num_cores

        if index_thread != index_core:
            continue

        input_asdf_file = asdf_files[index_asdf_file]

        fh.write(f"index_asdf_file,input_asdf_file = {index_asdf_file},{input_asdf_file}\n")
        fh.flush()

        s3_object_name = output_fits_object_name(input_asdf_file)

        local_asdf_file = f"{subdir_work}/" + os.path.basename(input_asdf_file)
        local_fits_file = f"{subdir_work}/" + os.path.basename(s3_object_name)[:-len(".gz")]
        local_gzipped_fits_file = local_fits_file + ".gz"

        local_files = [local_asdf_file,local_fits_file,local_fits_file + ".partial",
                       local_gzipped_fits_file]


        # The clock each major step below is timed from.  Every step advances it to its own
        # end, so the steps partition the file's ingest between them rather than each being
        # measured from the start.

        step_start_time = time.time()

        try:

            if s3_object_name in reusable_output_fits_files:


                # Already converted by an earlier run, but not registered, so take the
                # converted file back rather than paying for the conversion a second time.

                fh.write(f"{s3_object_name} is already in {bucket_name_output}; "
                         "downloading it instead of converting\n")

                s3_client.download_file(bucket_name_output,s3_object_name,local_gzipped_fits_file)

                step_start_time = log_elapsed_time("download already-converted FITS file from S3 bucket",
                                                   step_start_time,input_asdf_file)

                with gzip.open(local_gzipped_fits_file,'rb') as fh_in:
                    with open(local_fits_file,'wb') as fh_out:
                        shutil.copyfileobj(fh_in,fh_out)

                step_start_time = log_elapsed_time("gunzip already-converted FITS file",
                                                   step_start_time,input_asdf_file)

            else:


                # Download the ASDF file from the input S3 bucket.

                s3_client.download_file(bucket_name_input,input_asdf_file,local_asdf_file)

                step_start_time = log_elapsed_time("download ASDF file from S3 bucket",
                                                   step_start_time,input_asdf_file)


                # Gunzip it, if it is gzipped.  roman_datamodels reads only uncompressed ASDF.

                if local_asdf_file.endswith(".gz"):

                    gunzipped_asdf_file = local_asdf_file[:-len(".gz")]

                    with gzip.open(local_asdf_file,'rb') as fh_in:
                        with open(gunzipped_asdf_file,'wb') as fh_out:
                            shutil.copyfileobj(fh_in,fh_out)

                    local_files.append(gunzipped_asdf_file)

                    step_start_time = log_elapsed_time("gunzip ASDF file",
                                                       step_start_time,input_asdf_file)

                else:

                    gunzipped_asdf_file = local_asdf_file


                # Convert from ASDF format to multi-extension FITS format.

                converted = asdf_to_fits(gunzipped_asdf_file,local_fits_file)

                step_start_time = log_elapsed_time("convert ASDF file to FITS file",
                                                   step_start_time,input_asdf_file)

                if not converted:
                    raise RuntimeError(f"Could not convert {input_asdf_file}")


                # Gzip the output FITS file, keeping the uncompressed one for the registration.

                gzip_file(local_fits_file,local_gzipped_fits_file)

                step_start_time = log_elapsed_time("gzip FITS file",
                                                   step_start_time,input_asdf_file)


                # Upload the gzipped file to the output S3 bucket.

                uploaded = util.upload_files_to_s3_bucket(s3_client,bucket_name_output,
                                                          [local_gzipped_fits_file],[s3_object_name])

                step_start_time = log_elapsed_time("upload FITS file to S3 bucket",
                                                   step_start_time,input_asdf_file)

                if not uploaded:
                    raise RuntimeError(f"Could not upload {s3_object_name} to {bucket_name_output}")


            # Register the FITS file in the database.  This comes last, so that the row in
            # L2Files -- which is what marks the file as ingested -- is written only once the
            # file it points at is actually in the bucket.

            registered = register_fits_file(dbh,roman_tessellation_db,
                                            local_fits_file,local_gzipped_fits_file,s3_object_name)

            step_start_time = log_elapsed_time("register L2 file in database",
                                               step_start_time,input_asdf_file)

            if not registered:
                raise RuntimeError(f"Could not register {s3_object_name} in the database")

            n_ingested += 1

        except Exception as e:

            n_failed += 1

            message = f"*** Error: Could not ingest {input_asdf_file} ({e}); continuing..."

            print(message)
            fh.write(message + "\n")

        finally:


            # Clean up the work directory, whether or not the file was ingested, so that a
            # long run cannot fill the disk with the leavings of its failures.

            for local_file in local_files:
                if os.path.exists(local_file):
                    os.remove(local_file)

            log_elapsed_time("clean up work directory for L2 file",
                             step_start_time,input_asdf_file)


        # The whole file, timed from where the previous file left off, so that these totals
        # account for every second the worker spent rather than only the steps above.

        thread_start_time_benchmark = log_elapsed_time("ingest L2 file",
                                                       thread_start_time_benchmark,
                                                       input_asdf_file)

        fh.write(f"Loop end over asdf_files: index_asdf_file,input_asdf_file = {index_asdf_file},{input_asdf_file}\n")
        fh.flush()


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}, "
             f"n_ingested={n_ingested}, n_failed={n_failed}\n")

    fh.close()


    # Remove the PSFs this process downloaded, which are shared by every file of the same
    # filter and SCA and so are kept for the whole run rather than per file.

    for psf_filename in psf_file_cache.values():
        if psf_filename is not None and os.path.exists(psf_filename):
            os.remove(psf_filename)

    dbh.close()

    # The counts go back as numbers rather than only inside a message, so that the main
    # program can total them across the workers and say what the run as a whole did.

    return index_thread,n_ingested,n_failed


def execute_parallel_processes(asdf_files_list,num_cores=None,reusable_output_fits_files=None):

    '''
    Run the work list across num_cores processes, and return (exit code, files ingested, files
    failed) totalled over the workers.  The exit code is 0 if every worker finished, and
    otherwise a code describing how they did not.

    A worker that dies contributes no counts, so the two totals describe the files actually
    attempted rather than the whole work list; the caller says how many were left untouched.

    A worker that does not finish is a different thing from a file that fails.  A failed file
    is logged, skipped, and picked up by the next run, and does not make the run a failure.  A
    worker that dies takes its whole share of the work list with it, unattempted and
    unreported, and the run has to say so -- otherwise it exits 0, the daemon reads that as
    success, and a worker dying every time would never surface.
    '''

    if num_cores is None:
        num_cores = os.cpu_count() or 1

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:

        # Submit all tasks to the executor and store the futures in a list.

        futures = [executor.submit(run_single_core_job,asdf_files_list,thread_index,reusable_output_fits_files)
                   for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress.

        for i, future in enumerate(as_completed(futures)):
            print(f"Completed: {i+1} of {num_cores} processes")

    n_failed_workers = 0

    n_ingested_total = 0
    n_failed_total = 0

    worker_exit_code = None

    for index,future in enumerate(futures):

        try:

            index_thread,n_ingested,n_failed = future.result()

            n_ingested_total += n_ingested
            n_failed_total += n_failed

            print(f"Finish normally for index_thread = {index_thread}: "
                  f"n_ingested = {n_ingested}, n_failed = {n_failed}")


        # BaseException, not Exception: run_single_core_job quits with exit() when it cannot
        # open its log or reach the database, and that arrives here as SystemExit, which is
        # not an Exception.  Catching only Exception would let it escape this loop, leaving
        # the remaining workers unreported and the run's summary unprinted -- and those are
        # exactly the failures that hit every worker at once.

        except BaseException as e:

            n_failed_workers += 1

            print(f"*** Error in thread index {index} = {type(e).__name__}: {e}")


            # A worker that chose its own exit code knew something about why it stopped, so
            # keep the first such code rather than flattening every failure into one.

            if worker_exit_code is None and isinstance(e,SystemExit):
                if isinstance(e.code,int) and e.code >= 64:
                    worker_exit_code = e.code

    if n_failed_workers == 0:
        return 0,n_ingested_total,n_failed_total

    exit_code = worker_exit_code if worker_exit_code is not None else exit_code_worker_failed

    print(f"*** Error: {n_failed_workers} of {num_cores} worker(s) did not finish, so part of "
          f"the work list was not attempted; exiting {exit_code}...")

    return exit_code,n_ingested_total,n_failed_total


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Ensure the sqlite database that defines the Roman sky tessellation is available.

    if os.getenv('ROMANTESSELLATIONDBNAME') is None:
        print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
        exit(exit_code_config)


    # Ensure the work directory exists before any process tries to write into it.

    if not os.path.isdir(subdir_work):
        print(f"*** Error: Work directory {subdir_work} does not exist; quitting...")
        exit(exit_code_no_input)


    # Query the database for the files that have already been ingested.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    ingested_l2file_times = {}

    if do_already_ingested_check:

        ingested_l2file_times = get_ingested_l2file_times(dbh)

        if ingested_l2file_times is None:

            exit_code = dbh.exit_code if dbh.exit_code >= 64 else exit_code_database

            dbh.close()

            print("*** Error: Cannot tell what has already been ingested; quitting...")
            exit(exit_code)

        print(f"n_ingested_fits_files = {len(ingested_l2file_times)}")

    dbh.close()


    # The FITS files already in the output bucket.  These never decide whether a file still
    # has to be ingested -- the database, above, decides that -- but a file that is in the
    # bucket and not in the database was converted by an earlier run that did not get as far
    # as registering it, and can be downloaded rather than converted again.

    existing_output_fits_objects = list_bucket_objects(bucket_name_output,
                                                       suffixes=(".fits.gz",))

    print(f"n_existing_output_fits_objects = {len(existing_output_fits_objects)}")


    # Parse the ASDF files in the input S3 bucket, and keep the ones not yet ingested.

    input_asdf_objects = list_bucket_objects(bucket_name_input,
                                             prefix=input_prefix,
                                             suffixes=(".asdf",".asdf.gz"))

    print(f"n_input_asdf_files = {len(input_asdf_objects)}")

    input_asdf_files = []
    root_names = []
    sca_nums = []

    reusable_output_fits_files = set()

    n_already_ingested = 0
    n_redelivered = 0

    for input_asdf_file in sorted(input_asdf_objects):

        s3_object_name = output_fits_object_name(input_asdf_file)

        input_last_modified = input_asdf_objects[input_asdf_file]


        # A current L2Files row says some delivery of this file has been ingested, not that
        # THIS one has.  The row is what was ingested last; if the ASDF file in the bucket is
        # newer than that, it has been redelivered since and has to be ingested again, which
        # registers a new version and demotes the row that matched here.
        #
        # A row whose `created` is unknown is taken as ingested rather than redelivered: a
        # null timestamp is no evidence of a redelivery, and guessing the other way would put
        # the whole bucket back on the work list.

        if os.path.basename(s3_object_name) in ingested_l2file_times:

            created = ingested_l2file_times[os.path.basename(s3_object_name)]

            if not do_asdf_recency_check or created is None or input_last_modified <= created:
                n_already_ingested += 1
                continue

            print(f"{input_asdf_file} was modified at {input_last_modified}, after its "
                  f"L2Files record was created at {created}; ingesting it as a new version...")

            n_redelivered += 1

        fname_fields = os.path.basename(s3_object_name).split("_")

        if len(fname_fields) < 3:
            print(f"*** Warning: Unexpected filename {input_asdf_file}; skipping...")
            continue


        # A converted FITS file in the output bucket may be reused only if it is newer than
        # the ASDF file it came from.  An older one was made from a PREVIOUS delivery of that
        # ASDF file -- the redelivery that put this file back on the work list is exactly the
        # case where the object name is unchanged but the pixels are not -- and reusing it
        # would register the superseded data as the new version.

        output_last_modified = existing_output_fits_objects.get(s3_object_name)

        if output_last_modified is not None and output_last_modified > input_last_modified:
            reusable_output_fits_files.add(s3_object_name)

        input_asdf_files.append(input_asdf_file)
        root_names.append(fname_fields[0] + fname_fields[1])
        sca_nums.append(fname_fields[2])

        if max_files_to_ingest is not None and len(input_asdf_files) >= max_files_to_ingest:
            print(f"Reached MAXFILESTOINGEST = {max_files_to_ingest}; stopping the work list here...")
            break

    print(f"n_already_ingested = {n_already_ingested}")
    print(f"n_redelivered = {n_redelivered}")
    print(f"n_reusable_output_fits_files = {len(reusable_output_fits_files)}")
    print(f"Total number of L2 files to ingest = {len(input_asdf_files)}")

    if len(input_asdf_files) == 0:
        print("Nothing to ingest; exiting...")
        exit(0)


    # Sort by SCA, then by observation, in order to avoid the possible database race
    # condition of two processes inserting identical Exposures records at the same time:
    # the files of one exposure are then spread across the work list instead of adjacent
    # in it, and so are not handed out to the processes together.
    #
    # lexsort uses (secondary sort key, primary sort key).

    fname_indexes = np.lexsort((root_names,sca_nums))

    sorted_input_asdf_files = [input_asdf_files[i] for i in fname_indexes]


    ###############################################################################################
    # Execute the ingest tasks.  The execution is done for input ASDF files in parallel, with the
    # number of parallel processes equal to the number of cores on the job-launcher machine.
    ###############################################################################################

    if num_cores > 1:

        exit_code,n_ingested,n_failed = execute_parallel_processes(sorted_input_asdf_files,
                                                                   num_cores,
                                                                   reusable_output_fits_files)

    else:

        # The single-process path reports the same way, so that a run reads the same whether
        # it used one core or many.

        exit_code = 0

        n_ingested = 0
        n_failed = 0

        thread_index = 0

        try:

            index_thread,n_ingested,n_failed = run_single_core_job(sorted_input_asdf_files,
                                                                   thread_index,
                                                                   reusable_output_fits_files)

            print(f"Finish normally for index_thread = {index_thread}: "
                  f"n_ingested = {n_ingested}, n_failed = {n_failed}")

        except BaseException as e:
            print(f"*** Error in thread index {thread_index} = {type(e).__name__}: {e}")
            if isinstance(e,SystemExit) and isinstance(e.code,int) and e.code >= 64:
                exit_code = e.code
            else:
                exit_code = exit_code_worker_failed
            print(f"*** Error: The one worker did not finish, so part of the work list was "
                  f"not attempted; exiting {exit_code}...")


    # What the run actually did.  Printed whatever happened above, so that a run that lost a
    # worker still says how far it got.

    n_not_attempted = len(sorted_input_asdf_files) - n_ingested - n_failed

    print("")
    print("Number of L2 files on the work list =",len(sorted_input_asdf_files))
    print("Number of L2 files ingested =",n_ingested)
    print("Number of L2 files failed =",n_failed)


    # Only ever non-zero when a worker died holding part of the work list.  Those files were
    # not attempted at all, as against the failed ones that were attempted and skipped; both
    # are back on the next run's work list, since neither got an L2Files row.

    if n_not_attempted != 0:
        print("Number of L2 files not attempted =",n_not_attempted)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to ingest L2 files =",
        end_time_benchmark - start_time_benchmark)


    # Termination.  A file that individually failed does not come out here -- it was logged,
    # skipped, and left on the next run's work list.  Only a worker that did not finish does.

    exit(exit_code)
