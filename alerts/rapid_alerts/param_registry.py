"""
Single source of truth for the RAPID alert schema (current version: VERSION below).

A "param" here is one Avro schema field. We say param instead of field
because "field" is overloaded in RAPID (it usually means the Roman sky
field / tessellation index -- there is even a schema param literally named
"field"). The generated .avsc files still contain a "fields" array because
that token belongs to the Avro specification.

Every param of every record in the alert packet is declared here once, with:

  - avro type + doc     -> gen_schema.py writes the .avsc files from these
  - status + source     -> the implemented/stub inventory; enforced by
                           produce.py (see Status below). Print it with:
                           python -m rapid_alerts.param_registry [--summary]
  - attr / getter       -> how produce.py reads the value from the normalized
                           record (providers.Source etc.). Most params just
                           name an attribute via attr (default: the param's own
                           name); getter is the escape hatch for computed
                           values. Only runs for IMPLEMENTED params, and attr
                           names are checked against the record classes when
                           produce.py is imported.

To change the schema: edit this file, re-run gen_schema.py, and bump VERSION
if the change is not backward compatible. Do NOT edit the .avsc files by hand.

Params are grouped into commented blocks (identifiers, position, photometry,
...) with implemented params first and stubs at the end of each block.
NOTE: the declaration order here IS the Avro field order on the wire, so
reordering params is a schema change -- regenerate the .avsc files and treat
it like any other schema edit.

Avro types are given version-independently; a leading "@" marks a reference
to another record in this schema ("@diaSource" -> "rapid.v01_01.diaSource").
Nullable union types automatically get "default": null in the .avsc.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

VERSION = "01.01"

ROMAN_FILTERS = ["F062", "F087", "F106", "F129", "F146", "F158", "F184", "F213"]


class Status(Enum):
    # Statuses are enforced by produce.py, not just reported:
    #   IMPLEMENTED: value is read from the record; an error or a None in a
    #                non-nullable parameter raises instead of serializing null
    #   STUB:        always serialized as null, even if attr/getter is staged
    #   NOT_USED:    excluded from the .avsc schema and from built records
    IMPLEMENTED = "implemented"
    STUB = "stub"
    NOT_USED = "not used"


@dataclass(frozen=True)
class Param:
    name: str
    avro: Any                     # Avro type (str, union list, or dict)
    doc: str
    status: Status
    source: Optional[str] = None  # implemented: where the value comes from
                                  # stub: what work would fill it in
    attr: Optional[str] = None    # attribute read from the normalized record
                                  # (providers.Source etc.); default: name
    getter: Optional[Callable] = None  # computed values; overrides attr


@dataclass(frozen=True)
class Record:
    name: str
    doc: str
    params: tuple


def is_nullable(avro_type):
    """True if the Avro type is a union whose first member is null."""
    return isinstance(avro_type, list) and bool(avro_type) and avro_type[0] == "null"


IMPLEMENTED = Status.IMPLEMENTED
STUB = Status.STUB
NOT_USED = Status.NOT_USED

# Shared stub-source descriptions
_FP = "forced photometry (products not integrated)"
_SS = "solar-system processing (not run)"
_MPC = "MPC orbit ingest (not run)"


# ---------------------------------------------------------------------------
# diaSource -- built from a providers.Source
# ---------------------------------------------------------------------------

DIA_SOURCE_PARAMS = (
    # --- Identifiers & associations -------------------------------------
    Param("diaSourceId",   "long",             "Unique identifier for this source detection",
                    IMPLEMENTED, "sources.sid",    attr="sid"),
    Param("expId",         "long",             "Visit (exposure) identifier", #TODO: is this called visit or exposure
                    IMPLEMENTED, "sources.expid",  attr="expid"),
    Param("detector",      "int",              "Detector (SCA) number",
                    IMPLEMENTED, "sources.sca",    attr="sca"),
    Param("diaObjectId",   ["null", "long"],   "Associated diaObject identifier",
                    IMPLEMENTED, "merges_<field>.aid", attr="aid"),
    Param("ssObjectId",    ["null", "long"],   "Associated solar system object identifier (stub)",
                    STUB, "solar-system cross-matching (not run yet)"),

    # --- Time ------------------------------------------------------------
    #TODO - make sure final Roman decision is UTC, propagate to other times
    Param("midpointMjd", "double",          "Effective mid-observation time (UTC scale) [MJD]",
                    IMPLEMENTED, "sources.mjdobs", attr="mjdobs"),
    Param("timeProcessedMjd",  ["null", "double"],  "Time alert was processed (UTC scale) [MJD]",
                    NOT_USED, "set at assembly time"), #TODO: do we actually need this?
    Param("exposureTime",  ["null", "float"],  "Exposure time [s]",
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
    Param("raErr",         ["null", "float"],  "Uncertainty in ra [deg]",
                    STUB, "not produced by PSF fit yet"),
    Param("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
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
    Param("scienceFlux",   ["null", "float"],  "Forced PSF flux on science image (stub) [nJy]",
                    STUB, _FP),
    Param("scienceFluxErr", ["null", "float"], "Uncertainty in scienceFlux (stub) [nJy]",
                    STUB, _FP),
    Param("templateFlux",  ["null", "float"],  "Forced PSF flux on template image (stub) [nJy]",
                    STUB, _FP),
    Param("templateFluxErr", ["null", "float"], "Uncertainty in templateFlux (stub) [nJy]",
                    STUB, _FP),
    Param("diffimglimmag", ["null", "float"],  "Expected 5-sigma limiting magnitude of difference image (stub) [mag]",
                    STUB, "difference-image depth estimate (not computed)"),

    # --- PSF-fit quality (photutils) ---------------------------------------
    Param("qfit",          ["null", "float"],  "PSF-fit quality parameter",
                    IMPLEMENTED, "sources.qfit"),
    Param("cfit",          ["null", "float"],  "PSF-fit chi parameter",
                    IMPLEMENTED, "sources.cfit"),
    Param("redchi",        ["null", "float"],  "Reduced chi-square of PSF fit",
                    IMPLEMENTED, "sources.redchi"),
    Param("npixfit",       ["null", "int"],    "Number of pixels used in PSF fit",
                    IMPLEMENTED, "sources.npixfit"),
    Param("sharpness",     ["null", "float"],  "PSF-fit sharpness parameter",
                    IMPLEMENTED, "sources.sharpness"),
    Param("roundness1",    ["null", "float"],  "PSF-fit roundness parameter 1",
                    IMPLEMENTED, "sources.roundness1"),
    Param("roundness2",    ["null", "float"],  "PSF-fit roundness parameter 2",
                    IMPLEMENTED, "sources.roundness2"),
    Param("peak",          ["null", "float"],  "Peak pixel value in source stamp [DN]",
                    IMPLEMENTED, "sources.peak"),
    Param("psfChi2",       ["null", "float"],  "Chi-square of PSF fit (stub)",
                    STUB, "overlaps sources.redchi (reduced chi2) -- decide whether to derive or rename"),
    Param("psfNdata",      ["null", "int"],    "Number of data points in PSF fit (stub)",
                    STUB, "overlaps sources.npixfit -- decide whether to alias or rename"),

    # --- Classification (all stubs) -----------------------------------------
    Param("extendedness",  ["null", "float"],  "Probability of being extended (stub)",
                    STUB, "star/galaxy classification (not run)"),
    Param("reliability",   ["null", "float"],  "Reliability score (stub)",
                    STUB, "real/bogus classifier (not run)"),

    # --- Trailed-source fit (all stubs) --------------------------------------
    Param("trailFlux",     ["null", "float"],  "Trail-fit flux (stub) [nJy]",
                    STUB, "trailed-source fitting (not run)"),
    Param("trailFluxErr",  ["null", "float"],  "Uncertainty in trailFlux (stub) [nJy]",
                    STUB, "trailed-source fitting (not run)"),
    Param("trailLength",   ["null", "float"],  "Trail length (stub) [arcsec]",
                    STUB, "trailed-source fitting (not run)"),
    Param("trailAngle",    ["null", "float"],  "Trail angle (stub) [deg]",
                    STUB, "trailed-source fitting (not run)"),

    # --- Dipole fit (all stubs) -----------------------------------------------
    Param("dipoleMeanFlux", ["null", "float"], "Dipole mean flux (stub) [nJy]",
                    STUB, "dipole analysis (not run)"),
    Param("dipoleFluxErr", ["null", "float"],  "Uncertainty in dipoleMeanFlux (stub) [nJy]",
                    STUB, "dipole analysis (not run)"),
    Param("dipoleLength",  ["null", "float"],  "Dipole separation (stub) [arcsec]",
                    STUB, "dipole analysis (not run)"),
    Param("dipoleAngle",   ["null", "float"],  "Dipole orientation (stub) [deg]",
                    STUB, "dipole analysis (not run)"),

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
    Param("flags",         "long",             "Bitmask of processing flags",
                    IMPLEMENTED, "sources.flags"),
    Param("pixelFlags_saturated", ["null", "boolean"], "Source has saturated pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("pixelFlags_bad",       ["null", "boolean"], "Source has bad pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("pixelFlags_edge",      ["null", "boolean"], "Source is near detector edge (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("pixelFlags_cr",        ["null", "boolean"], "Source has cosmic ray pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Param("centroid_flag", ["null", "boolean"], "Centroid measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Param("apFlux_flag",   ["null", "boolean"], "Aperture flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Param("psfFlux_flag",  ["null", "boolean"], "PSF flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),

    # --- Nearest reference-image source (all stubs) ------------------------------
    Param("distnr",        ["null", "float"],  "Distance to nearest reference-image source (stub) [arcsec]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Param("ranr",          ["null", "double"], "RA of nearest reference-image source (stub) [deg]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Param("decnr",         ["null", "double"], "Dec of nearest reference-image source (stub) [deg]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Param("magnr",         ["null", "float"],  "Magnitude of nearest reference-image source (stub) [mag]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Param("sigmagnr",      ["null", "float"],  "1-sigma uncertainty in magnr (stub) [mag]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Param("chinr",         ["null", "float"],  "Chi parameter of nearest reference-image source (stub)",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Param("sharpnr",       ["null", "float"],  "Sharpness parameter of nearest reference-image source (stub)",
                    STUB, "cross-match to reference-image catalog (not run)"),

    # --- Roman-specific identifiers & tiling ------------------------------------
    Param("sca",           "int",              "Roman SCA detector number",
                    IMPLEMENTED, "sources.sca"),
    Param("field",         "int",              "Roman field identifier",
                    IMPLEMENTED, "sources.field"),
    Param("hp6",           "int",              "HEALPix index at nside=64 (order 6)",
                    IMPLEMENTED, "sources.hp6"),
    Param("hp9",           "int",              "HEALPix index at nside=512 (order 9)",
                    IMPLEMENTED, "sources.hp9"),
    Param("pid",           "long",             "Processing ID for science image",
                    IMPLEMENTED, "sources.pid"),
    Param("expid",         "int",              "Exposure identifier",
                    IMPLEMENTED, "sources.expid"),
    Param("pass",          ["null", "int"],    "Roman survey pass number (stub)",
                    STUB, "Roman observation ID components (exposure metadata; not in sources table)"),
    Param("segment",       ["null", "int"],    "Roman survey segment number (stub)",
                    STUB, "Roman observation ID components (exposure metadata; not in sources table)"),
    Param("program",       ["null", "int"],    "Roman program identifier (stub)",
                    STUB, "Roman observation ID components (exposure metadata; not in sources table)"),
    Param("survey",        ["null", "string"], "Survey name (stub)",
                    STUB, "observation metadata (not available)"),
)


# ---------------------------------------------------------------------------
# diaForcedSource -- built from a providers.ForcedPhot
#
# The whole record is a stub: attrs are staged, but RAPID forced photometry
# currently produces FITS files rather than DB records, so no provider
# supplies ForcedPhot yet and prvDiaForcedSources is always null.
# ---------------------------------------------------------------------------

DIA_FORCED_SOURCE_PARAMS = (
    # --- Identifiers & associations ---
    Param("diaForcedSourceId", "long",            "Unique identifier for this forced source measurement",
                        STUB, _FP, attr="forced_id"),
    Param("diaObjectId",       "long",            "Associated diaObject identifier",
                        STUB, _FP, attr="aid"),
    Param("visit",             "long",            "Visit (exposure) identifier",
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
                        STUB, _FP, attr="time_processed"),
    Param("timeWithdrawnMjd", ["null", "double"], "Time measurement was withdrawn (UTC scale) [MJD]",
                        NOT_USED, "alert-withdrawal mechanism (not designed)"),
)


# ---------------------------------------------------------------------------
# diaObject -- built from a providers.ObjectRecord
# ---------------------------------------------------------------------------

def _per_filter_flux_params():
    """The 8 filters x 8 statistics block of diaObject params."""
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
        ]
    return tuple(params)


DIA_OBJECT_PARAMS = (
    # --- Identifier & position -------------------------------------------
    Param("diaObjectId",   "long",             "Unique identifier for this object",
                        IMPLEMENTED, "astroobjects_<field>.aid",  attr="aid"),
    Param("ra",            "double",           "Right ascension of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.ra0",  attr="ra0"),
    Param("dec",           "double",           "Declination of object centroid; ICRS [deg]", #TODO should these be mean?
                        IMPLEMENTED, "astroobjects_<field>.dec0", attr="dec0"),
    Param("raErr",         ["null", "float"],  "Uncertainty in ra [deg]", # TODO: on-sky error or std dev in degrees? Need cos(dec) factor if former
                        IMPLEMENTED, "astroobjects_<field> stdevra", attr="stdevdec"),
    Param("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
                        IMPLEMENTED, "astroobjects_<field> stdevdec", attr="stdevra"),

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
# ssSource / mpc_orbits -- entire records are stubs
# ---------------------------------------------------------------------------

SS_SOURCE_PARAMS = (
    # --- Identifiers & associations ---
    Param("ssSourceId",       "long",             "Unique identifier for this solar system source",
                        STUB, _SS),
    Param("diaSourceId",      "long",             "Associated diaSource identifier",
                        STUB, _SS),
    Param("ssObjectId",       ["null", "long"],   "Associated solar system object identifier",
                        STUB, _SS),

    # --- Geometry ---
    Param("heliocentricX",    ["null", "double"], "Heliocentric x position [AU]",
                        STUB, _SS),
    Param("heliocentricY",    ["null", "double"], "Heliocentric y position [AU]",
                        STUB, _SS),
    Param("heliocentricZ",    ["null", "double"], "Heliocentric z position [AU]",
                        STUB, _SS),
    Param("phaseAngle",       ["null", "float"],  "Phase angle [deg]",
                        STUB, _SS),
    Param("heliocentricDist", ["null", "float"],  "Heliocentric distance [AU]",
                        STUB, _SS),
    Param("topocentricDist",  ["null", "float"],  "Topocentric distance [AU]",
                        STUB, _SS),
)

MPC_ORBITS_PARAMS = (
    # --- Identifier ---
    Param("id",    "string",           "MPC designation or packed designation",
                        STUB, _MPC),

    # --- Orbital elements ---
    Param("a",     ["null", "double"], "Semi-major axis [AU]",
                        STUB, _MPC),
    Param("e",     ["null", "double"], "Eccentricity",
                        STUB, _MPC),
    Param("incl",  ["null", "double"], "Inclination [deg]",
                        STUB, _MPC),
    Param("Omega", ["null", "double"], "Longitude of ascending node [deg]",
                        STUB, _MPC),
    Param("omega", ["null", "double"], "Argument of perihelion [deg]",
                        STUB, _MPC),
    Param("M",     ["null", "double"], "Mean anomaly [deg]",
                        STUB, _MPC),
    Param("epoch", ["null", "double"], "Epoch of orbital elements [MJD]",
                        STUB, _MPC),

    # --- Photometric parameters ---
    Param("H",     ["null", "float"],  "Absolute magnitude [mag]",
                        STUB, _MPC),
    Param("G",     ["null", "float"],  "Slope parameter",
                        STUB, _MPC),
)


# ---------------------------------------------------------------------------
# alert (top level) -- structural params, filled directly by
# produce.assemble_alert(), which also verifies its keys match this list
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

    # --- Solar system (all stubs) ---------------------------------------------
    Param("ssSource",           ["null", "@ssSource"],   "Solar system source association (stub)",
                        STUB, _SS),
    Param("mpc_orbits",         ["null", "@mpc_orbits"], "MPC orbital elements (stub)",
                        STUB, _MPC),

    # --- Image cutouts ----------------------------------------------------------
    Param("cutoutDifference",   ["null", "bytes"],   "FITS cutout of difference image",
                        IMPLEMENTED, "provider get_cutouts()"),
    Param("cutoutScience",      ["null", "bytes"],   "FITS cutout of science image",
                        IMPLEMENTED, "provider get_cutouts()"),
    Param("cutoutTemplate",     ["null", "bytes"],   "FITS cutout of template image",
                        IMPLEMENTED, "provider get_cutouts()"),

    # --- Observation metadata (all stubs) ----------------------------------------
    Param("observation_reason", ["null", "string"],     "Reason for observation (e.g. survey, ToO)",
                        STUB, "observation metadata (not available)"),
    Param("target_name",        ["null", "string"],     "Target name if targeted observation",
                        STUB, "observation metadata (not available)"),
)


# ---------------------------------------------------------------------------
# Record registry, in Avro dependency order (referenced records first)
# ---------------------------------------------------------------------------

RECORDS = (
    Record("diaSource",       "RAPID alert schema: individual source detection on a difference image",       DIA_SOURCE_PARAMS),
    Record("diaForcedSource", "RAPID alert schema: forced photometry measurement at a diaObject position",   DIA_FORCED_SOURCE_PARAMS),
    Record("diaObject",       "RAPID alert schema: astronomical object derived from DIASources",             DIA_OBJECT_PARAMS),
    Record("ssSource",        "RAPID alert schema: solar system source association (stub)",                  SS_SOURCE_PARAMS),
    Record("mpc_orbits",      "RAPID alert schema: MPC orbital elements (stub)",                             MPC_ORBITS_PARAMS),
    Record("alert",           "RAPID alert schema: top-level alert record",                                  ALERT_PARAMS),
)


# ---------------------------------------------------------------------------
# Status report: python -m rapid_alerts.param_registry [--summary]
# ---------------------------------------------------------------------------

def print_report(summary=False):
    """Print the implemented/stub status of every param, per record."""
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
