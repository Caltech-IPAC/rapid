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
ingested".  A file counts as ingested when a row for its output FITS file
already exists in the L2Files database table, which is the only record that
survives a restart of this script.  The output bucket is consulted as well, but
never to decide whether a file has to be ingested: a converted file sitting
there without a database row was left by a run that stopped between the upload
and the registration, and is downloaded and registered rather than converted a
second time.


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
DBPORT, DBNAME, DBUSER, DBPASS, DBSERVER
                        Database connection, as for every RAPID script.
ROMANTESSELLATIONDBNAME
                        SQLite database defining the Roman sky tessellation.
CRDS_PATH, CRDS_SERVER_URL
                        Only needed if the gWCS has to be assigned here; the
                        simulated inputs already carry a correct gWCS.

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
    exit(64)

bucket_name_output = os.getenv('RAPIDL2OUTPUTBUCKET')

if bucket_name_output is None:
    print("*** Error: Env. var. RAPIDL2OUTPUTBUCKET not set; quitting...")
    exit(64)

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

sip_distortion_degree_str = os.getenv('SIPDISTORTIONDEGREE')

if sip_distortion_degree_str is None:
    sip_distortion_degree = 5
else:
    sip_distortion_degree = int(sip_distortion_degree_str)

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


# Number of parallel processes.

num_cores_str = os.getenv('NUM_CORES')

if num_cores_str is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores_str)

print("num_cores =",num_cores)


# Optional cap on the number of files ingested in one run, for short tests.

max_files_str = os.getenv('MAXFILESTOINGEST')

if max_files_str is None:
    max_files_to_ingest = None
else:
    max_files_to_ingest = int(max_files_str)

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


    # A non-finite float cannot be written to a FITS card at all.

    if isinstance(value,float) and not np.isfinite(value):
        return None

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

    fill_missing_sip_keywords(hdr,sip_distortion_degree)

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


def build_image_hdus(dm,primary_hdr,exptime):

    '''
    Return the list of image HDUs, the science image first, each converted from the DN/s of
    the L2 product into DN by the exponent of EXPTIME that its units call for, and each array
    of the science image's shape carrying the same WCS.
    '''

    hdus = []

    image_nodes = asdf_image_nodes(dm)

    if len(image_nodes) == 0 or image_nodes[0][0] != science_node_name:
        print(f"*** Error: ASDF file has no {science_node_name} array")
        return None

    science_shape = image_nodes[0][1].shape

    wcs_header = gwcs_to_fits_sip_header(dm.meta.wcs,science_shape,sip_distortion_degree)

    for node_name,array in image_nodes:

        extname = "SCI" if node_name == science_node_name else node_name.upper()

        exponent = exptime_scaling_exponent.get(node_name,0)

        if exponent == 0:
            data = np.array(array)
        else:
            data = np.array(array,dtype=np.float64) * (float(exptime) ** exponent)
            data = data.astype(np.float32)


        # FITS has no half-precision floating-point format, so a float16 ASDF array is
        # widened rather than silently truncated to an integer type by the writer.

        if data.dtype == np.float16:
            data = data.astype(np.float32)


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

        hdus.append(fits.ImageHDU(data=data,header=hdr,name=extname))

    return hdus


def build_table_hdus(dm):

    '''
    Return the list of binary-table HDUs for the tabular nodes of the ASDF file.
    '''

    hdus = []

    for node_name,table in asdf_table_nodes(dm):

        extname = node_name.upper()

        try:
            hdu = fits.BinTableHDU(data=Table(table),name=extname)
        except Exception as e:
            print(f"*** Warning: Could not convert ASDF table {node_name} to a FITS table ({e}); skipping...")
            continue

        hdu.header["ASDFNODE"] = (node_name,"node this table came from in the ASDF file")

        hdus.append(hdu)

    return hdus


def asdf_to_fits(asdf_path,fits_path):

    '''
    Convert one ASDF file into a multi-extension FITS file.  Returns True on success and
    False if the file could not be converted, which leaves it unregistered and so still on
    the work list of the next run.
    '''

    print(f"Reading {asdf_path}...")

    dm = rdm.open(asdf_path)

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

    image_hdus = build_image_hdus(dm,primary_hdr,exptime)

    if image_hdus is None:
        print(f"*** Error: Could not build the image HDUs for {asdf_path}")
        return False

    table_hdus = build_table_hdus(dm)


    # The primary HDU holds keywords only, so it is given no data at all.

    primary_hdu = fits.PrimaryHDU(header=primary_hdr)

    hdul = fits.HDUList([primary_hdu] + image_hdus + table_hdus)

    hdul.writeto(fits_path,overwrite=True,checksum=True)

    print(f"Wrote {fits_path} with {len(hdul)} HDUs: " +
          ",".join([hdu.name for hdu in hdul]))

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


    # The SIP coefficients, which fill_missing_sip_keywords has guaranteed are all present.

    sip_values = {}

    for prefix in ("A","B"):

        sip_values[f"{prefix}_ORDER"] = get_keyword_value(header,f"{prefix}_ORDER")

        for i in range(0,sip_distortion_degree + 1):
            for j in range(0,sip_distortion_degree + 1):
                if i + j > sip_distortion_degree:
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


def list_bucket_object_names(bucket_name,prefix="",suffixes=None):

    '''
    Return the names of the objects in an S3 bucket, paging through a listing of any length
    and keeping only the ones ending in one of the given suffixes.  All the suffixes are
    matched in one pass, because these buckets hold tens of thousands of objects and a
    separate listing per suffix would page through all of them again.
    '''

    s3_client = boto3.client('s3')

    object_names = []

    paginator = s3_client.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=bucket_name,Prefix=prefix):

        for entry in page.get('Contents',[]):

            key = str(entry['Key'])

            if suffixes and not key.endswith(tuple(suffixes)):
                continue

            object_names.append(key)

    return object_names


def get_already_ingested_fits_files(dbh):

    '''
    Return the set of L2 FITS filenames, without their S3-bucket names, that already have a
    row in the L2Files database table.

    The test is on a row existing at all, not on it being the best version, because a row
    that exists means the file has been ingested; re-ingesting it would add a second row for
    the same file rather than repair the first.
    '''

    query = "select (regexp_match(filename, '.+/(.+)'))[1] from l2files;"

    records = dbh.execute_sql_queries([query],debug)

    already_ingested_fits_files = set()

    for record in records:
        already_ingested_fits_files.add(record[0])

    return already_ingested_fits_files


#-------------------------------------------------------------------------------------------------------------
# Methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(asdf_files,index_thread,existing_output_fits_files=None):

    '''
    Convert and register the share of the work list belonging to one process.

    The database connection and the sky-tessellation database are opened here, inside the
    child process, rather than inherited from the parent, so that no two processes can end up
    sharing one connection.

    `existing_output_fits_files` is the set of objects already in the output bucket.  A file
    named in it was converted by an earlier run (or by convert_socsims.py, before this script
    replaced it) but never registered, so it is downloaded and registered rather than
    converted again -- the conversion is by far the most expensive step.
    '''

    if existing_output_fits_files is None:
        existing_output_fits_files = set()

    thread_start_time_benchmark = time.time()

    n_asdf_files = len(asdf_files)

    print("index_thread,n_asdf_files =",index_thread,n_asdf_files)

    thread_work_file = f"{subdir_work}/" + swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file,'w',encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

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

        local_files = [local_asdf_file,local_fits_file,local_gzipped_fits_file]

        try:

            if s3_object_name in existing_output_fits_files:


                # Already converted by an earlier run, but not registered, so take the
                # converted file back rather than paying for the conversion a second time.

                fh.write(f"{s3_object_name} is already in {bucket_name_output}; "
                         "downloading it instead of converting\n")

                s3_client.download_file(bucket_name_output,s3_object_name,local_gzipped_fits_file)

                with gzip.open(local_gzipped_fits_file,'rb') as fh_in:
                    with open(local_fits_file,'wb') as fh_out:
                        shutil.copyfileobj(fh_in,fh_out)

            else:


                # Download the ASDF file from the input S3 bucket.

                s3_client.download_file(bucket_name_input,input_asdf_file,local_asdf_file)


                # Gunzip it, if it is gzipped.  roman_datamodels reads only uncompressed ASDF.

                if local_asdf_file.endswith(".gz"):

                    gunzipped_asdf_file = local_asdf_file[:-len(".gz")]

                    with gzip.open(local_asdf_file,'rb') as fh_in:
                        with open(gunzipped_asdf_file,'wb') as fh_out:
                            shutil.copyfileobj(fh_in,fh_out)

                    local_files.append(gunzipped_asdf_file)

                else:

                    gunzipped_asdf_file = local_asdf_file


                # Convert from ASDF format to multi-extension FITS format.

                converted = asdf_to_fits(gunzipped_asdf_file,local_fits_file)

                if not converted:
                    raise RuntimeError(f"Could not convert {input_asdf_file}")


                # Gzip the output FITS file, keeping the uncompressed one for the registration.

                gzip_file(local_fits_file,local_gzipped_fits_file)


                # Upload the gzipped file to the output S3 bucket.

                uploaded = util.upload_files_to_s3_bucket(s3_client,bucket_name_output,
                                                          [local_gzipped_fits_file],[s3_object_name])

                if not uploaded:
                    raise RuntimeError(f"Could not upload {s3_object_name} to {bucket_name_output}")


            # Register the FITS file in the database.  This comes last, so that the row in
            # L2Files -- which is what marks the file as ingested -- is written only once the
            # file it points at is actually in the bucket.

            registered = register_fits_file(dbh,roman_tessellation_db,
                                            local_fits_file,local_gzipped_fits_file,s3_object_name)

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


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to ingest L2 file = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark

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

    return f"Finish normally for index_thread = {index_thread}: n_ingested = {n_ingested}, n_failed = {n_failed}"


def execute_parallel_processes(asdf_files_list,num_cores=None,existing_output_fits_files=None):

    if num_cores is None:
        num_cores = os.cpu_count()

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:

        # Submit all tasks to the executor and store the futures in a list.

        futures = [executor.submit(run_single_core_job,asdf_files_list,thread_index,existing_output_fits_files)
                   for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress.

        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Ensure the sqlite database that defines the Roman sky tessellation is available.

    if os.getenv('ROMANTESSELLATIONDBNAME') is None:
        print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
        exit(64)


    # Ensure the work directory exists before any process tries to write into it.

    if not os.path.isdir(subdir_work):
        print(f"*** Error: Work directory {subdir_work} does not exist; quitting...")
        exit(64)


    # Query the database for the files that have already been ingested.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    already_ingested_fits_files = set()

    if do_already_ingested_check:

        already_ingested_fits_files = get_already_ingested_fits_files(dbh)

        print(f"n_ingested_fits_files = {len(already_ingested_fits_files)}")

    dbh.close()


    # The FITS files already in the output bucket.  These never decide whether a file still
    # has to be ingested -- the database, above, decides that -- but a file that is in the
    # bucket and not in the database was converted by an earlier run that did not get as far
    # as registering it, and is downloaded rather than converted again.

    existing_output_fits_files = set(list_bucket_object_names(bucket_name_output,
                                                             suffixes=(".fits.gz",)))

    print(f"n_existing_output_fits_files = {len(existing_output_fits_files)}")


    # Parse the ASDF files in the input S3 bucket, and keep the ones not yet ingested.

    input_asdf_object_names = list_bucket_object_names(bucket_name_input,
                                                      prefix=input_prefix,
                                                      suffixes=(".asdf",".asdf.gz"))

    print(f"n_input_asdf_files = {len(input_asdf_object_names)}")

    input_asdf_files = []
    root_names = []
    sca_nums = []

    n_already_ingested = 0

    for input_asdf_file in sorted(input_asdf_object_names):

        s3_object_name = output_fits_object_name(input_asdf_file)

        if os.path.basename(s3_object_name) in already_ingested_fits_files:
            n_already_ingested += 1
            continue

        fname_fields = os.path.basename(s3_object_name).split("_")

        if len(fname_fields) < 3:
            print(f"*** Warning: Unexpected filename {input_asdf_file}; skipping...")
            continue

        input_asdf_files.append(input_asdf_file)
        root_names.append(fname_fields[0] + fname_fields[1])
        sca_nums.append(fname_fields[2])

        if max_files_to_ingest is not None and len(input_asdf_files) >= max_files_to_ingest:
            print(f"Reached MAXFILESTOINGEST = {max_files_to_ingest}; stopping the work list here...")
            break

    print(f"n_already_ingested = {n_already_ingested}")
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
        execute_parallel_processes(sorted_input_asdf_files,num_cores,existing_output_fits_files)
    else:
        thread_index = 0
        print(run_single_core_job(sorted_input_asdf_files,thread_index,existing_output_fits_files))


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to ingest L2 files =",
        end_time_benchmark - start_time_benchmark)


    # Termination.

    exit(0)
