"""
File:     param_registry.py
Author:   Emily Everetts
Date:     07/2026

Generates RAPID alert schema (current version: VERSION below).
Every schema record and parameter is declared here once, with:

  - avro type + doc     -> gen_schema.py writes the .avsc files from these
  - status + source     -> the IMPLEMENTED/STUB inventory; enforced by
                           produce.py.
  - attr / getter       -> how produce.py reads the value from the normalized
                           record (providers.Source, Object,...). Default attr
                           is the parameter name, while getter must be set for
                           computed values. Checked against the record classes
                           when produce.py is imported.

Avro types are given version-independently; a leading "@" marks a reference
to another record in this schema ("@diaSource" -> "rapid.v01_01.diaSource").
Nullable union types automatically get "default": null in the .avsc.

To change the schema: edit this file, re-run gen_schema.py, and bump VERSION
if the change is not backward compatible. Do NOT edit the .avsc files by hand.

NOTE: declaration order matters -- if order changes, regenerate the .avsc files

Usage:
    python -m alerts.param_registry [--summary]

Gives a summary of fields and statuses in this file.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, TypeAlias

VERSION = "00.03"

# Keep for type checking and function hints
AvroType: TypeAlias = str | list["AvroType"] | dict[str, Any]

ROMAN_FILTERS = ["F062", "F087", "F106", "F129", "F146", "F158", "F184", "F213"]


class Status(Enum):
    """Implementation status of a param, enforced by produce.py.

    IMPLEMENTED
        Value is read from the normalized record; an error or a None in a
        non-nullable param raises instead of serializing null.
    STUB
        Always serialized as null, even if attr/getter is staged.
    NOT_USED
        Excluded from the .avsc schema and from built records entirely.
    """
    IMPLEMENTED = "implemented"
    STUB = "stub"
    NOT_USED = "not used"


@dataclass(frozen=True)
class Param:
    """Declaration of one alert-schema param (one Avro field).

    Attributes
    ----------
    name : str
        Param name as it appears in the .avsc schema and on the wire.
    avro : str or list or dict
        Version-independent Avro type, or an array dict. A leading "@"
        marks a reference to another record in this schema.
    doc : str
        Description string written into the .avsc schema.
    status : Status
        Implementation status (enum)
    source : str, optional
        Description of where the data comes from (not written to schema).
    attr : str, optional
        Attribute read from the normalized record in providers.py.
        Default: `name`.
    getter : callable, optional
        Getter for computed values, overrides `attr`.
    """
    name: str
    avro: AvroType
    doc: str
    status: Status
    source: str | None = None
    attr: str | None = None
    getter: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class Record:
    """One record of the alert packet: a named group of params.

    Attributes
    ----------
    name : str
        Record name in the schema namespace (e.g. "diaSource").
    doc : str
        The description written into the record's .avsc schema.
    params : tuple of Param
        The record's params, in wire order.
    """
    name: str
    doc: str
    params: tuple[Param, ...]


def is_nullable(avro_type: AvroType) -> bool:
    """Report whether an Avro type spec is nullable.

    Parameters
    ----------
    avro_type : str or list or dict
        Version-independent Avro type spec from a :class:`Param`.

    Returns
    -------
    bool
        True if the type is a union whose first member is "null" (such
        params get ``"default": null`` in the .avsc schema).
    """
    return isinstance(avro_type, list) and bool(avro_type) and avro_type[0] == "null"


IMPLEMENTED = Status.IMPLEMENTED
STUB = Status.STUB
NOT_USED = Status.NOT_USED

# Shared stub-source descriptions
_FP = "forced photometry (products not integrated)"

# PhotUtils PSFPhotometry bitwise fit-condition flags (sources.flags), as
# defined in photutils/psf/_components.py define_flags() for photutils 3.0.0.
# The pipeline installs photutils unpinned, so revisit these on upgrades.
# Bits not named here: 1 partial fit region, 64 no overlap with data,
# 128 fully masked, 256 too few pixels, 2048 non-finite local background.
_PSF_FLAG_POS_OUTSIDE_IMAGE = 1 << 1   # fitted position outside image bounds
_PSF_FLAG_NONPOS_FLUX = 1 << 2         # non-positive flux
_PSF_FLAG_NO_CONVERGENCE = 1 << 3      # possible non-convergence
_PSF_FLAG_NO_COVARIANCE = 1 << 4       # missing parameter covariance
_PSF_FLAG_POS_AT_BOUND = 1 << 5        # position near a positional bound
_PSF_FLAG_NONFINITE_POS = 1 << 9       # non-finite fitted position
_PSF_FLAG_NONFINITE_FLUX = 1 << 10     # non-finite fitted flux

# RAPID-defined semantics for the derived boolean flag params; keeps alert
# consumers insulated from photutils bit-layout changes.
_CENTROID_FAIL_BITS = (_PSF_FLAG_POS_OUTSIDE_IMAGE | _PSF_FLAG_POS_AT_BOUND
                       | _PSF_FLAG_NONFINITE_POS)
_PSFFLUX_FAIL_BITS = (_PSF_FLAG_NONPOS_FLUX | _PSF_FLAG_NO_CONVERGENCE
                      | _PSF_FLAG_NO_COVARIANCE | _PSF_FLAG_NONFINITE_FLUX)


# ---------------------------------------------------------------------------
# diaSource -- built from providers.Source
# ---------------------------------------------------------------------------

DIA_SOURCE_PARAMS = (
    # --- Identifiers & associations -------------------------------------
    Param("diaSourceId",   "long",             "Unique identifier for this source detection",
                    IMPLEMENTED, "sources.sid",    attr="sid"),
    Param("expId",         "long",             "RAPID-assigned exposure identifier (pipeline database serial, "
                                                "not a Roman SOC identifier; see observation_id)",
                    IMPLEMENTED, "sources.expid",  attr="expid"),
    Param("detector",      "int",              "Detector (SCA) number",
                    IMPLEMENTED, "sources.sca",    attr="sca"),
    Param("diaObjectId",   "long",   "Associated diaObject identifier",
                    IMPLEMENTED, "merges_<field>.aid", attr="aid"),
    Param("ssObjectId",    ["null", "long"],   "Associated solar system object identifier",
                    NOT_USED, "superseded by top-level ssMatches array (MPC designations are strings)"),

    # --- Time ------------------------------------------------------------
    #TODO - make sure final Roman decision is UTC, propagate to other times
    Param("midpointMjd", "double",          "Effective mid-observation time (UTC scale) [MJD]",
                    IMPLEMENTED, "sources.mjdobs", attr="mjdobs"),
    Param("timeProcessedMjd",  ["null", "double"],  "Time alert was processed (UTC scale) [MJD]",
                    IMPLEMENTED, "set at assembly time", attr="time_proc"),
    Param("exposureTime",  ["null", "float"],  "Exposure time [s]", #TODO: eventually move to alert params
                    IMPLEMENTED, "exposures.exptime", attr="exptime"),
    Param("timeWithdrawnMjd",  ["null", "double"],  "Time alert was withdrawn (UTC scale) [MJD]",
                    NOT_USED, "alert-withdrawal mechanism (not designed)"),

    # --- Position (sky & pixel) -------------------------------------------
    Param("ra",            "double",           "Right ascension; ICRS [deg]",
                    IMPLEMENTED, "sources.ra"),
    Param("dec",           "double",           "Declination; ICRS [deg]",
                    IMPLEMENTED, "sources.dec"),
    Param("x",             "float",            "x-pixel position on detector [pixels]",
                    IMPLEMENTED, "sources.xfit",   attr="xfit"),
    Param("y",             "float",            "y-pixel position on detector [pixels]",
                    IMPLEMENTED, "sources.yfit",   attr="yfit"),
    Param("xErr",          ["null", "float"],  "Uncertainty in x [pixels]",
                    IMPLEMENTED, "sources.xerr",   attr="xerr"),
    Param("yErr",          ["null", "float"],  "Uncertainty in y [pixels]",
                    IMPLEMENTED, "sources.yerr",   attr="yerr"),
    Param("raErr",         ["null", "float"],  "Uncertainty in ra [deg]", #TODO: implement in run_photutils.py?
                    STUB, "not produced by PSF fit yet"),
    Param("decErr",        ["null", "float"],  "Uncertainty in dec [deg]", #TODO: see raErr
                    STUB, "not produced by PSF fit yet"),

    # --- Photometry --------------------------------------------------------
    Param("band",          ["null", "string"], "Filter band name (F062, F087, F106, F129, F146, F158, F184, F213)",
                    IMPLEMENTED, "filters.filter"),
    Param("psfFlux",       ["null", "float"],  "Flux from PSF-fit on difference image [nJy]",
                    IMPLEMENTED, "sources.fluxfit (instrumental; nJy calibration pending)", attr="fluxfit"),
    Param("psfFluxErr",    ["null", "float"],  "Uncertainty in psfFlux [nJy]",
                    IMPLEMENTED, "sources.fluxerr", attr="fluxerr"),
    Param("snr",           ["null", "float"],  "Signal-to-noise ratio (psfFlux / psfFluxErr)",
                    IMPLEMENTED, "computed: fluxfit / fluxerr"),
    Param("isNegative",    "boolean",          "true if source is from negative (ref minus sci) subtraction",
                    IMPLEMENTED, "sources.isdiffpos (inverted; renamed per schema spreadsheet)",
                    getter=lambda d: not d.isdiffpos),
    Param("apFlux",        ["null", "float"],  "Aperture flux on difference image (stub) [nJy]",
                    STUB, "aperture photometry (not in DB flow; SExtractor MAG_AUTO in file flow)"),
    Param("apFluxErr",     ["null", "float"],  "Uncertainty in apFlux (stub) [nJy]",
                    STUB, "aperture photometry"),
    # --- Forced Photometry ---------------------------------------------------- #TODO
    Param("scienceFlux",   ["null", "float"],  "Forced PSF flux on science image (stub) [nJy]", #TODO: why is this not in diaForcedSource???
                    STUB, _FP),
    Param("scienceFluxErr", ["null", "float"], "Uncertainty in scienceFlux (stub) [nJy]",
                    STUB, _FP),
    Param("refFlux",  ["null", "float"],  "Forced PSF flux on reference image (stub) [nJy]",
                    STUB, _FP),
    Param("refFluxErr", ["null", "float"], "Uncertainty in reference Flux (stub) [nJy]",
                    STUB, _FP),
    Param("diffimglimmag", ["null", "float"],  "Expected 5-sigma limiting magnitude of difference image (stub) [mag]",
                    STUB, "difference-image depth estimate (not computed)"), #TODO: do we need this?

    # --- PSF-fit quality (photutils) ---------------------------------------
    Param("psfQfit",          ["null", "float"],  "PSF-fit quality parameter",
                    IMPLEMENTED, "sources.qfit", attr="qfit"),
    Param("psfCfit",          ["null", "float"],  "PSF-fit chi parameter",
                    IMPLEMENTED, "sources.cfit", attr="cfit"),
    Param("psfRChi2",        ["null", "float"],  "Reduced chi-square of PSF fit",
                    IMPLEMENTED, "sources.redchi", attr="redchi"),
    Param("psfNdata",       ["null", "int"],    "Number of pixels used in  PSF fit", #TODO: used to be npixfit
                    IMPLEMENTED, "sources.npixfit", attr="npixfit"),
    Param("sharpness",     ["null", "float"],  "PSF-fit sharpness parameter",
                    IMPLEMENTED, "sources.sharpness"),
    Param("roundness1",    ["null", "float"],  "PSF-fit roundness parameter 1", #TODO: better description
                    IMPLEMENTED, "sources.roundness1"),
    Param("roundness2",    ["null", "float"],  "PSF-fit roundness parameter 2",
                    IMPLEMENTED, "sources.roundness2"),
    Param("peak",          ["null", "float"],  "Peak pixel value in source stamp [DN]",
                    IMPLEMENTED, "sources.peak"),
    # Param("psfChi2",       ["null", "float"],  "Chi-square of PSF fit (stub)",
    #                 NOT_USED, "overlaps sources.redchi (reduced chi2) -- decide whether to derive or rename"),
    # Param("psfNdata",      ["null", "int"],    "Number of data points in PSF fit (stub)",
    #                 NOT_USED, "overlaps sources.npixfit -- decide whether to alias or rename"),

    # --- Classification (all stubs) -----------------------------------------
    Param("extendedness",  ["null", "float"],  "Probability of being extended (stub)",
                    STUB, "star/galaxy classification (not run)"),
    Param("reliability",   ["null", "float"],  "Reliability score from ML classifier",
                    STUB, "real/bogus classifier (not run)"),
    Param("reliabilityVersion", ["null", "string"],  "Reliability code version",
                    STUB, "from real/bogus classifier version (not run)"),

    # --- Trailed-source fit (all stubs) --------------------------------------
    # Unsure if we will have these in v1
    Param("trailFlux",     ["null", "float"],  "Trail-fit flux (stub) [nJy]",
                    NOT_USED, "trailed-source fitting (not run)"),
    Param("trailFluxErr",  ["null", "float"],  "Uncertainty in trailFlux (stub) [nJy]",
                    NOT_USED, "trailed-source fitting (not run)"),
    Param("trailLength",   ["null", "float"],  "Trail length (stub) [arcsec]",
                    NOT_USED, "trailed-source fitting (not run)"),
    Param("trailAngle",    ["null", "float"],  "Trail angle (stub) [deg]",
                    NOT_USED, "trailed-source fitting (not run)"),

    # --- Dipole fit (all stubs) -----------------------------------------------
    # Unsure if we will have these in v1
    Param("dipoleMeanFlux", ["null", "float"], "Dipole mean flux (stub) [nJy]",
                    NOT_USED, "dipole analysis (not run)"),
    Param("dipoleFluxErr", ["null", "float"],  "Uncertainty in dipoleMeanFlux (stub) [nJy]",
                    NOT_USED, "dipole analysis (not run)"),
    Param("dipoleLength",  ["null", "float"],  "Dipole separation (stub) [arcsec]",
                    NOT_USED, "dipole analysis (not run)"),
    Param("dipoleAngle",   ["null", "float"],  "Dipole orientation (stub) [deg]",
                    NOT_USED, "dipole analysis (not run)"),

    # --- Shape moments (all stubs) --------------------------------------------
    Param("ixx",           ["null", "float"],  "Adaptive second moment Ixx (stub) [arcsec^2]",
                    STUB, "shape measurement (not in DB flow; SExtractor moments in file flow)"),
    Param("iyy",           ["null", "float"],  "Adaptive second moment Iyy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Param("ixy",           ["null", "float"],  "Adaptive second moment Ixy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Param("ixxErr",        ["null", "float"],  "Uncertainty in ixx (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Param("iyyErr",        ["null", "float"],  "Uncertainty in iyy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Param("ixyErr",        ["null", "float"],  "Uncertainty in ixy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Param("elong",         ["null", "float"],  "Elongation of source: ratio of major to minor axis (stub)",
                    STUB, "shape measurement (SExtractor ELONGATION in file flow)"),

    # --- Flags -----------------------------------------------------------------
    Param("psfFitFlags",   "long",             "Bitmask of PhotUtils PSFPhotometry fit-condition flags; "
                                                "bit definitions follow the photutils version used by the pipeline",
                    IMPLEMENTED, "sources.flags (PhotUtils PSFPhotometry bitmask)", attr="flags"),
    Param("pixelFlags_saturated", ["null", "boolean"], "Source has saturated pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("pixelFlags_bad",       ["null", "boolean"], "Source has bad pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("pixelFlags_edge",      ["null", "boolean"], "Source is near detector edge (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("pixelFlags_cr",        ["null", "boolean"], "Source has cosmic ray pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("centroid_flag", ["null", "boolean"], "Centroid measurement failed (position outside image, "
                                                "at a fit bound, or non-finite)",
                    IMPLEMENTED, "derived from sources.flags (PhotUtils bits 2|32|512)",
                    getter=lambda d: bool(d.flags & _CENTROID_FAIL_BITS)),
    Param("apFlux_flag",   ["null", "boolean"], "Aperture flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Param("psfFlux_flag",  ["null", "boolean"], "PSF flux measurement failed (non-positive or non-finite "
                                                "flux, non-convergence, or missing covariance)",
                    IMPLEMENTED, "derived from sources.flags (PhotUtils bits 4|8|16|1024)",
                    getter=lambda d: bool(d.flags & _PSFFLUX_FAIL_BITS)),
    Param("scienceFlux_flag",  ["null", "boolean"], "Science flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Param("refFlux_flag",  ["null", "boolean"], "Reference flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Param("isSSCandidate", ["null", "boolean"], "Suspected solar system object: a known SS object is predicted "
                                                "within SS_CANDIDATE_SEP_ARCSEC of this source (see ssMatches); "
                                                "null if association was not run for this detection",
                    IMPLEMENTED, "computed: KONA predictions within providers.SS_CANDIDATE_SEP_ARCSEC",
                    attr="is_ss_candidate"), #TODO: tune the separation cut

    # --- Roman-specific identifiers & tiling ------------------------------------
    Param("field",         "int",              "Roman field identifier",
                    IMPLEMENTED, "sources.field"),
    Param("hp6",           "int",              "HEALPix index at nside=64 (order 6)",
                    IMPLEMENTED, "sources.hp6"),
    Param("hp9",           "int",              "HEALPix index at nside=512 (order 9)",
                    IMPLEMENTED, "sources.hp9"),
    Param("pid",           "long",             "Processing ID for science image",
                    IMPLEMENTED, "sources.pid"),

    # Roman observation ID hierarchy (meta.observation in the L2 ASDF files):
    # program > plan > pass > segment > observation > visit > exposure.
    # All stubs: dropped in the ASDF-to-FITS conversion, not in exposures table.
    Param("observation_id", ["null", "string"], "Roman observation ID: concatenated observation hierarchy "
                                                "including the exposure counter; uniquely identifies the "
                                                "Roman exposure (stub)",
                    STUB, "meta.observation.observation_id"),
    Param("program",       ["null", "int"],    "Roman program number (stub)",
                    STUB, "meta.observation.program"),
    Param("plan",          ["null", "int"],    "Roman execution plan number (stub)",
                    STUB, "meta.observation.execution_plan"),
    Param("pass",          ["null", "int"],    "Roman pass number (stub)",
                    STUB, "meta.observation.pass"),
    Param("segment",       ["null", "int"],    "Roman segment number (stub)",
                    STUB, "meta.observation.segment"),
    Param("observation",   ["null", "int"],    "Roman observation number within the segment (stub)",
                    STUB, "meta.observation.observation"),
    Param("visit",         ["null", "int"],    "Roman visit number; a visit groups multiple exposures, so "
                                                "this is NOT a per-image identifier -- use expId or "
                                                "observation_id for that (stub)",
                    STUB, "meta.observation.visit"),
    Param("exposure",      ["null", "int"],    "Roman exposure counter within the visit (stub)",
                    STUB, "meta.observation.exposure"), #TODO: name something else?
    Param("survey",        ["null", "string"], "Survey name (stub)",
                    STUB, "observation metadata (not available)"),
)


# ---------------------------------------------------------------------------
# diaForcedSource -- built from providers.ForcedPhot
# ---------------------------------------------------------------------------

DIA_FORCED_SOURCE_PARAMS = (
    # --- Identifiers & associations ---
    Param("diaForcedSourceId", "long",            "Unique identifier for this forced source measurement",
                        STUB, _FP, attr="forced_id"), #TODO: separate ID for forced source?
    Param("diaObjectId",       "long",            "Associated diaObject identifier",
                        STUB, _FP, attr="aid"), #TODO: already in diaSource?
    Param("expId",             "long",            "RAPID-assigned exposure identifier",
                        STUB, _FP, attr="expid"),
    Param("detector",          "int",             "Detector (SCA) number",
                        STUB, _FP, attr="sca"),

    # --- Position ---
    Param("ra",                "double",          "Right ascension of forced measurement position; ICRS [deg]",
                        STUB, _FP),
    Param("dec",               "double",          "Declination of forced measurement position; ICRS [deg]",
                        STUB, _FP),

    # --- Photometry ---
    Param("band",              ["null", "string"], "Filter band name",
                        STUB, _FP),
    Param("psfFlux",           ["null", "float"], "Forced PSF flux on difference image [nJy]",
                        STUB, _FP, attr="flux"),
    Param("psfFluxErr",        ["null", "float"], "Uncertainty in psfFlux [nJy]",
                        STUB, _FP, attr="fluxerr"),
    Param("scienceFlux",       ["null", "float"], "Forced PSF flux on science image (stub) [nJy]",
                        STUB, _FP),
    Param("scienceFluxErr",    ["null", "float"], "Uncertainty in scienceFlux (stub) [nJy]",
                        STUB, _FP),

    # --- Time ---
    Param("midpointMjd",    "double",          "Effective mid-observation time (UTC scale) [MJD]",
                        STUB, _FP, attr="mjdobs"),
    Param("timeProcessedMjd", "double",        "Time measurement was processed (UTC scale) [MJD]",
                        STUB, _FP, attr="time_proc"),
    Param("timeWithdrawnMjd", ["null", "double"], "Time measurement was withdrawn (UTC scale) [MJD]",
                        NOT_USED, "alert-withdrawal mechanism (not designed)"),
)


# ---------------------------------------------------------------------------
# diaObject -- built from providers.ObjectRecord
# ---------------------------------------------------------------------------

def _per_filter_flux_params():
    """Build the 8 filters x 8 statistics block of diaObject params.

    Returns
    -------
    tuple of Param
        One param per (Roman filter, flux statistic) pair, all stubs until
        nJy flux calibration is available.
    """
    src = ("per-filter statistics over associated sources; needs nJy flux "
           "calibration (see roman_rapid_alerts FILTER_ZP_EFF)")
    params = []
    for b in ROMAN_FILTERS:
        params += [
            Param(f"{b}PsfFluxMean",  ["null", "float"], f"Mean PSF flux in {b} [nJy]",
                        STUB, src),
            Param(f"{b}PsfFluxMeanErr", ["null", "float"], f"Uncertainty in {b}PsfFluxMean [nJy]",
                        STUB, src),
            Param(f"{b}PsfFluxSigma", ["null", "float"], f"Std dev of PSF flux in {b} [nJy]",
                        STUB, src),
            Param(f"{b}PsfFluxNdata", ["null", "int"],   f"Number of {b} measurements",
                        STUB, src),
            Param(f"{b}PsfFluxMin",   ["null", "float"], f"Minimum PSF flux in {b} [nJy]",
                        STUB, src),
            Param(f"{b}PsfFluxMax",   ["null", "float"], f"Maximum PSF flux in {b} [nJy]",
                        STUB, src),
            Param(f"{b}PsfFluxMaxSlope", ["null", "float"], f"Maximum slope of PSF flux between any two {b} epochs [nJy/day]",
                        STUB, src),
            Param(f"{b}PsfFluxErrMean", ["null", "float"], f"Mean of PSF flux errors in {b} [nJy]",
                        STUB, src),
            # Maybe add Von Neumann ratio: https://metricgate.com/docs/von-neumann-ratio-trend-test/
        ]
    return tuple(params)


DIA_OBJECT_PARAMS = (
    # --- Identifier & position -------------------------------------------
    Param("diaObjectId",   "long",             "Unique identifier for this object",
                        IMPLEMENTED, "astroobjects_<field>.aid",  attr="aid"),
    Param("ra0",            "double",           "First measured right ascension of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.ra0",  attr="ra0"),
    Param("dec0",           "double",           "First measured declination of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.dec0", attr="dec0"),
    Param("meanRa",            ["null", "double"], "Mean right ascension of object centroid; ICRS [deg]",
                        STUB, "in astroobjects_meta"),
    Param("meanDec",           ["null", "double"], "Mean declination of object centroid; ICRS [deg]",
                        STUB, "in astroobjects_meta"),
    Param("raErr",         ["null", "float"],  "Uncertainty in ra [deg]", # TODO: on-sky error or std dev in degrees? Need cos(dec) factor if former
                        IMPLEMENTED, "astroobjects_<field> stdevra", attr="stdevra"),
    Param("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
                        IMPLEMENTED, "astroobjects_<field> stdevdec", attr="stdevdec"),

    # --- Source history ----------------------------------------------------
    Param("nDiaSources",   "int",              "Total number of associated DIASources",
                        IMPLEMENTED, "astroobjects_<field>.nsources", attr="nsources"),
    Param("firstDiaSourceMjd", ["null", "double"], "MJD of earliest associated diaSource (UTC scale) [MJD]",
                        IMPLEMENTED, "computed from source history", attr="first_mjd"),
    Param("lastDiaSourceMjd",  ["null", "double"], "MJD of latest associated diaSource (UTC scale) [MJD]",
                        IMPLEMENTED, "computed from source history", attr="last_mjd"),
    Param("validityStartMjd",  "double",    "Start of validity interval for this object summary (UTC scale) [MJD]",
                        IMPLEMENTED, "triggering source mjdobs", attr="validity_mjd"),
    Param("ncovhist",      ["null", "int"],    "Number of times the object position fell on an observed image (stub)",
                        STUB, "coverage history (not computed)"),
    Param("firstRefMjd", ["null", "double"], "MJD of earliest exposure in the reference image (UTC scale) [MJD] (stub)",
                        STUB, "reference-image metadata (not tracked)"),
    Param("lastRefMjd", ["null", "double"], "MJD of latest exposure in the reference image (UTC scale) [MJD] (stub)",
                        STUB, "reference-image metadata (not tracked)"),

    # --- Per-filter flux statistics (all stubs) ------------------------------
) + _per_filter_flux_params()


# ---------------------------------------------------------------------------
# ssMatch -- known solar system object predicted near the triggering source,
# built from providers.SSMatch (KONA per-visit predictions, associated at
# alert-assembly time)
# ---------------------------------------------------------------------------

_SS_SRC = "computed at assembly from KONA predictions (modules/solarsystem/rapid_kona.py)"

SS_MATCH_PARAMS = (
    Param("designation",   "string",           "MPC designation of the solar system object",
                        IMPLEMENTED, _SS_SRC),
    Param("ra",            "double",           "Predicted right ascension of the object at midpointMjd; ICRS [deg]",
                        IMPLEMENTED, _SS_SRC),
    Param("dec",           "double",           "Predicted declination of the object at midpointMjd; ICRS [deg]",
                        IMPLEMENTED, _SS_SRC),
    Param("sep",           "float",            "Angular separation from the triggering source position [arcsec]",
                        IMPLEMENTED, _SS_SRC),
    Param("pa",            "float",            "Position angle from the triggering source to the object, East of North [deg]",
                        IMPLEMENTED, _SS_SRC),
    Param("predVMag",      ["null", "float"],  "Predicted V-band magnitude from MPC H/G photometric parameters; "
                                               "null when the object has no catalogued H [mag]",
                        IMPLEMENTED, _SS_SRC, attr="predvmag"),
)


# ---------------------------------------------------------------------------
# refMatch -- reference-image catalog source near the triggering source,
# built from providers.RefMatch (per-field mosaic SExtractor catalog, matched
# at alert-assembly time; see the cross-match section of providers.py).
#
# Match-record envelope convention (enforced by produce._validate_registry):
# every *Match record opens with a string identifier followed by
# ra, dec, sep, pa; survey/catalog-specific fields come after, keeping their
# native column semantics. Future external catalogs (Gaia, NED, ...) each
# get their own record and alert array following the same convention.
# ---------------------------------------------------------------------------

_REF_SRC = ("computed at assembly from the reference-image mosaic SExtractor "
            "catalog (refimcatalogs cattype=1, located via diffimages.rfid)")

REF_MATCH_PARAMS = (
    Param("sourceId",      "string",           "SExtractor NUMBER of the reference source, unique within "
                                               "this field's mosaic catalog",
                        IMPLEMENTED, _REF_SRC, attr="source_id"),
    Param("ra",            "double",           "Right ascension of the reference source; ICRS [deg]",
                        IMPLEMENTED, _REF_SRC),
    Param("dec",           "double",           "Declination of the reference source; ICRS [deg]",
                        IMPLEMENTED, _REF_SRC),
    Param("sep",           "float",            "Angular separation from the triggering source position [arcsec]",
                        IMPLEMENTED, _REF_SRC),
    Param("pa",            "float",            "Position angle from the triggering source to the reference "
                                               "source, East of North [deg]",
                        IMPLEMENTED, _REF_SRC),
    Param("classStar",     "float",            "SExtractor CLASS_STAR star/galaxy score (1 = point-like); the "
                                               "star/galaxy array split uses providers.REFCAT_STAR_MIN_CLASS, "
                                               "recorded here so consumers can re-cut",
                        IMPLEMENTED, _REF_SRC, attr="class_star"),
    Param("flags",         "int",              "SExtractor extraction FLAGS bitmask of the reference source "
                                               "(2 = blended, 4 = saturated, ...)",
                        IMPLEMENTED, _REF_SRC),
    Param("magAuto",       ["null", "float"],  "Kron-like automatic-aperture magnitude MAG_AUTO (instrumental; "
                                               "nJy calibration pending); null when the measurement failed [mag]",
                        IMPLEMENTED, _REF_SRC, attr="mag_auto"),
    Param("magErrAuto",    ["null", "float"],  "Uncertainty in magAuto; null with magAuto [mag]",
                        IMPLEMENTED, _REF_SRC, attr="mag_err_auto"),
    Param("elong",         ["null", "float"],  "SExtractor ELONGATION (major/minor axis ratio)",
                        IMPLEMENTED, _REF_SRC),
    Param("fwhm",          ["null", "float"],  "FWHM assuming a Gaussian core (FWHM_IMAGE) [arcsec]",
                        IMPLEMENTED, _REF_SRC),
    Param("halfLightRadius", ["null", "float"], "Radius enclosing half of the source flux (FLUX_RADIUS at "
                                               "fraction 0.5); with magAuto, supports size-normalized host "
                                               "separations [arcsec]",
                        IMPLEMENTED, _REF_SRC, attr="half_light_radius"),
    Param("kronRadius",    ["null", "float"],  "Kron aperture scale factor (KRON_RADIUS, dimensionless; "
                                               "multiplies the profile's A_IMAGE per SExtractor convention)",
                        IMPLEMENTED, _REF_SRC, attr="kron_radius"),
)


# ---------------------------------------------------------------------------
# alert (top level) -- structural params, filled by produce.assemble_alert(),
# ---------------------------------------------------------------------------

ALERT_PARAMS = (
    # --- Provenance ---------------------------------------------------------
    Param("schemaVersion",  ["null", "string"], "Version of the alert schema used to serialize this packet",
                        IMPLEMENTED, "param_registry.VERSION (set in produce.py)"),
    Param("pipelineVersion", ["null", "string"], "Version of the RAPID pipeline that produced this alert (stub)",
                        STUB, "pipeline build/release identifier (not plumbed through)"),

    # --- Triggering source detection --------------------------------------------
    Param("diaSourceId",    "long",           "Identifier for the triggering diaSource",
                        IMPLEMENTED, "produce.assemble_alert()"),
    Param("diaSource",      "@diaSource",     "Triggering source detection",
                        IMPLEMENTED, "produce.assemble_alert()"),

    # --- Object history ------------------------------------------------------
    Param("prvDiaSources",      ["null", {"type": "array", "items": "@diaSource"}],
                                "Previous detections of the same object within 12 months",
                        IMPLEMENTED, "produce.assemble_alert()"),
    Param("diaObject",          ["null", "@diaObject"],  "Summary object record",
                        IMPLEMENTED, "produce.assemble_alert()"),
    Param("prvDiaForcedSources", ["null", {"type": "array", "items": "@diaForcedSource"}],
                                 "Forced photometry history at the object position",
                        STUB, _FP),

    # --- Solar system -----------------------------------------------------------
    Param("ssMatches",          ["null", {"type": "array", "items": "@ssMatch"}],
                                "Nearest known solar system objects (max 3) within the cutout radius "
                                "(SS_MATCH_RADIUS_ARCSEC, ~7 arcsec) of the triggering source; "
                                "null if association was not run, empty if none found",
                        IMPLEMENTED, "produce.assemble_alert() via provider.get_ss_matches()"),

    # --- Reference-catalog cross-match -------------------------------------------
    Param("refStarMatches",     ["null", {"type": "array", "items": "@refMatch"}],
                                "Nearest star-classified reference-catalog sources (max 3, nearest "
                                "first) within REF_MATCH_RADIUS_ARCSEC of the triggering source; "
                                "null if matching was not run (catalog unavailable), empty if none "
                                "within the radius; a full array (3) means the neighborhood may "
                                "extend beyond what is reported (crowding)",
                        IMPLEMENTED, "produce.assemble_alert() via provider.get_ref_matches()"),
    Param("refGalaxyMatches",   ["null", {"type": "array", "items": "@refMatch"}],
                                "Nearest galaxy-classified reference-catalog sources (max 3, nearest "
                                "first) within REF_MATCH_RADIUS_ARCSEC of the triggering source; "
                                "null/empty semantics as refStarMatches",
                        IMPLEMENTED, "produce.assemble_alert() via provider.get_ref_matches()"),

    # --- Image cutouts ----------------------------------------------------------
    Param("cutoutDifference",   ["null", "bytes"],   "FITS cutout of difference image",
                        IMPLEMENTED, "provider get_cutouts()"),
    Param("cutoutScience",      ["null", "bytes"],   "FITS cutout of science image",
                        IMPLEMENTED, "provider get_cutouts()"),
    Param("cutoutReference",     ["null", "bytes"],   "FITS cutout of reference image",
                        IMPLEMENTED, "provider get_cutouts()"),

    # --- Observation metadata (all stubs) ----------------------------------------
    Param("observation_reason", ["null", "string"],     "Reason for observation (e.g. survey, ToO)",
                        STUB, "observation metadata (not propagated in pipeline)"),
    Param("target_name",        ["null", "string"],     "Target name if targeted observation",
                        STUB, "observation metadata (not propagated in pipeline)"),
)


# ---------------------------------------------------------------------------
# Record registry, in Avro dependency order (referenced records first)
# ---------------------------------------------------------------------------

RECORDS = (
    Record("diaSource",       "Individual source detection on a difference image",       DIA_SOURCE_PARAMS),
    Record("diaForcedSource", "Forced photometry measurement at a diaObject position",   DIA_FORCED_SOURCE_PARAMS),
    Record("diaObject",       "Astronomical object derived from DIASources",             DIA_OBJECT_PARAMS),
    Record("ssMatch",         "Known solar system object predicted near the triggering source (stub)", SS_MATCH_PARAMS),
    Record("refMatch",        "Reference-image catalog source near the triggering source",             REF_MATCH_PARAMS),
    Record("alert",           "Top-level alert record",                                  ALERT_PARAMS),
)


# ---------------------------------------------------------------------------
# Status report: python -m alerts.param_registry [--summary]
# ---------------------------------------------------------------------------

def print_report(summary: bool = False) -> None:
    """Print the implemented/stub status of every param, per record.

    Parameters
    ----------
    summary : bool, optional
        If True, print only the per-record counts; if False (the default),
        also list every param with its status mark and source.
    """
    mark = {IMPLEMENTED: "x", STUB: " ", NOT_USED: "-"}
    total_implemented = total_stub = 0

    for record in RECORDS:
        counts = {status: sum(1 for p in record.params if p.status is status)
                  for status in Status}
        total_implemented += counts[IMPLEMENTED]
        total_stub += counts[STUB]

        line = (f"{record.name}: {len(record.params)} params -- "
                f"{counts[IMPLEMENTED]} implemented, {counts[STUB]} stub")
        if counts[NOT_USED]:
            line += f", {counts[NOT_USED]} not used"
        print(f"\n{line}")

        if not summary:
            print("-" * len(line))
            for p in record.params:
                print(f"  [{mark[p.status]}] {p.name:<26} {p.source or ''}")

    print(f"\nTotal: {total_implemented} implemented, {total_stub} stub "
          f"([x] = implemented, [ ] = stub, [-] = not used)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Show implemented vs stub alert schema params")
    parser.add_argument("--summary", action="store_true",
                        help="per-record counts only")
    print_report(summary=parser.parse_args().summary)
