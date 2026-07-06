"""
Single source of truth for the RAPID alert schema (current version: VERSION below).

Every field of every record in the alert packet is declared here once, with:

  - avro type + doc     -> gen_schema.py writes the .avsc files from these
  - status + source     -> the implemented/stub inventory; enforced by
                           produce.py (see Status below). Print it with:
                           python -m rapid_alerts.fields [--summary]
  - attr / getter       -> how produce.py reads the value from the normalized
                           record (providers.Detection etc.). Most fields just
                           name an attribute via attr (default: the field's own
                           name); getter is the escape hatch for computed
                           values. Only runs for IMPLEMENTED fields, and attr
                           names are checked against the record classes when
                           produce.py is imported.

To change the schema: edit this file, re-run gen_schema.py, and bump VERSION
if the change is not backward compatible. Do NOT edit the .avsc files by hand.

Fields are grouped into commented blocks (identifiers, position, photometry,
...) with implemented fields first and stubs at the end of each block.
NOTE: the declaration order here IS the Avro field order on the wire, so
reordering fields is a schema change -- regenerate the .avsc files and treat
it like any other schema edit.

Avro types are given version-independently; a leading "@" marks a reference
to another record in this schema ("@diaSource" -> "rapid.v01_01.diaSource").
Nullable union fields automatically get "default": null in the .avsc.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

VERSION = "01.01"

ROMAN_FILTERS = ["F062", "F087", "F106", "F129", "F146", "F158", "F184", "F213"]


class Status(Enum):
    # Statuses are enforced by produce.py, not just reported:
    #   IMPLEMENTED: value is read from the record; an error or a None in a
    #                non-nullable field raises instead of serializing null
    #   STUB:        always serialized as null, even if attr/getter is staged
    #   NOT_USED:    excluded from the .avsc schema and from built records
    IMPLEMENTED = "implemented"
    STUB = "stub"
    NOT_USED = "not used"


@dataclass(frozen=True)
class Field:
    name: str
    avro: Any                     # Avro type (str, union list, or dict)
    doc: str
    status: Status
    source: Optional[str] = None  # implemented: where the value comes from
                                  # stub: what work would fill it in
    attr: Optional[str] = None    # attribute read from the normalized record
                                  # (providers.Detection etc.); default: name
    getter: Optional[Callable] = None  # computed values; overrides attr


@dataclass(frozen=True)
class Record:
    name: str
    doc: str
    fields: tuple


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
# diaSource -- built from a providers.Detection
# ---------------------------------------------------------------------------

DIA_SOURCE_FIELDS = (
    # --- Identifiers & associations -------------------------------------
    Field("diaSourceId",   "long",             "Unique identifier for this source detection",
                    IMPLEMENTED, "sources.sid",    attr="sid"),
    Field("visit",         "long",             "Visit (exposure) identifier",
                    IMPLEMENTED, "sources.expid",  attr="expid"),
    Field("detector",      "int",              "Detector (SCA) number",
                    IMPLEMENTED, "sources.sca",    attr="sca"),
    Field("diaObjectId",   ["null", "long"],   "Associated diaObject identifier",
                    IMPLEMENTED, "merges_<field>.aid", attr="aid"),
    Field("ssObjectId",    ["null", "long"],   "Associated solar system object identifier (stub)",
                    STUB, "solar-system cross-matching (not run)"),

    # --- Time ------------------------------------------------------------
    Field("midpointMjdTai", "double",          "Effective mid-observation time [TAI MJD]",
                    IMPLEMENTED, "sources.mjdobs", attr="mjdobs"),
    Field("timeProcessedMjdTai",  ["null", "double"],  "Time alert was processed [TAI MJD]",
                    STUB, "set at assembly time (decide TAI convention first)"),
    Field("timeWithdrawnMjdTai",  ["null", "double"],  "Time alert was withdrawn [TAI MJD]",
                    STUB, "alert-withdrawal mechanism (not designed)"),
    Field("exposureTime",  ["null", "float"],  "Exposure duration [s]",
                    STUB, "exposures table metadata (join not implemented)"),

    # --- Position (sky & pixel) -------------------------------------------
    Field("ra",            "double",           "Right ascension; ICRS [deg]",
                    IMPLEMENTED, "sources.ra"),
    Field("dec",           "double",           "Declination; ICRS [deg]",
                    IMPLEMENTED, "sources.dec"),
    Field("x",             "float",            "x-pixel position on detector [pixels]",
                    IMPLEMENTED, "sources.xfit",   attr="xfit"),
    Field("y",             "float",            "y-pixel position on detector [pixels]",
                    IMPLEMENTED, "sources.yfit",   attr="yfit"),
    Field("xErr",          ["null", "float"],  "Uncertainty in x [pixels]",
                    IMPLEMENTED, "sources.xerr",   attr="xerr"),
    Field("yErr",          ["null", "float"],  "Uncertainty in y [pixels]",
                    IMPLEMENTED, "sources.yerr",   attr="yerr"),
    Field("raErr",         ["null", "float"],  "Uncertainty in ra [deg]",
                    STUB, "not produced by PSF fit yet"),
    Field("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
                    STUB, "not produced by PSF fit yet"),

    # --- Photometry --------------------------------------------------------
    Field("band",          ["null", "string"], "Filter band name (F062, F087, F106, F129, F146, F158, F184, F213)",
                    IMPLEMENTED, "filters.filter"),
    Field("psfFlux",       ["null", "float"],  "Flux from PSF-fit on difference image [nJy]",
                    IMPLEMENTED, "sources.fluxfit (instrumental; nJy calibration pending)", attr="fluxfit"),
    Field("psfFluxErr",    ["null", "float"],  "Uncertainty in psfFlux [nJy]",
                    IMPLEMENTED, "sources.fluxerr", attr="fluxerr"),
    Field("snr",           ["null", "float"],  "Signal-to-noise ratio (psfFlux / psfFluxErr)",
                    IMPLEMENTED, "computed: fluxfit / fluxerr"),
    Field("isNegative",    "boolean",          "true if source is from negative (ref minus sci) subtraction",
                    IMPLEMENTED, "sources.isdiffpos (inverted; renamed per schema spreadsheet)",
                    getter=lambda d: not d.isdiffpos),
    Field("apFlux",        ["null", "float"],  "Aperture flux on difference image (stub) [nJy]",
                    STUB, "aperture photometry (not in DB flow; SExtractor MAG_AUTO in file flow)"),
    Field("apFluxErr",     ["null", "float"],  "Uncertainty in apFlux (stub) [nJy]",
                    STUB, "aperture photometry"),
    Field("scienceFlux",   ["null", "float"],  "Forced PSF flux on science image (stub) [nJy]",
                    STUB, _FP),
    Field("scienceFluxErr", ["null", "float"], "Uncertainty in scienceFlux (stub) [nJy]",
                    STUB, _FP),
    Field("templateFlux",  ["null", "float"],  "Forced PSF flux on template image (stub) [nJy]",
                    STUB, _FP),
    Field("templateFluxErr", ["null", "float"], "Uncertainty in templateFlux (stub) [nJy]",
                    STUB, _FP),
    Field("diffimglimmag", ["null", "float"],  "Expected 5-sigma limiting magnitude of difference image (stub) [mag]",
                    STUB, "difference-image depth estimate (not computed)"),

    # --- PSF-fit quality (photutils) ---------------------------------------
    Field("qfit",          ["null", "float"],  "PSF-fit quality parameter",
                    IMPLEMENTED, "sources.qfit"),
    Field("cfit",          ["null", "float"],  "PSF-fit chi parameter",
                    IMPLEMENTED, "sources.cfit"),
    Field("redchi",        ["null", "float"],  "Reduced chi-square of PSF fit",
                    IMPLEMENTED, "sources.redchi"),
    Field("npixfit",       ["null", "int"],    "Number of pixels used in PSF fit",
                    IMPLEMENTED, "sources.npixfit"),
    Field("sharpness",     ["null", "float"],  "PSF-fit sharpness parameter",
                    IMPLEMENTED, "sources.sharpness"),
    Field("roundness1",    ["null", "float"],  "PSF-fit roundness parameter 1",
                    IMPLEMENTED, "sources.roundness1"),
    Field("roundness2",    ["null", "float"],  "PSF-fit roundness parameter 2",
                    IMPLEMENTED, "sources.roundness2"),
    Field("peak",          ["null", "float"],  "Peak pixel value in source stamp [DN]",
                    IMPLEMENTED, "sources.peak"),
    Field("psfChi2",       ["null", "float"],  "Chi-square of PSF fit (stub)",
                    STUB, "overlaps sources.redchi (reduced chi2) -- decide whether to derive or rename"),
    Field("psfNdata",      ["null", "int"],    "Number of data points in PSF fit (stub)",
                    STUB, "overlaps sources.npixfit -- decide whether to alias or rename"),

    # --- Classification (all stubs) -----------------------------------------
    Field("extendedness",  ["null", "float"],  "Probability of being extended (stub)",
                    STUB, "star/galaxy classification (not run)"),
    Field("reliability",   ["null", "float"],  "Reliability score (stub)",
                    STUB, "real/bogus classifier (not run)"),

    # --- Trailed-source fit (all stubs) --------------------------------------
    Field("trailFlux",     ["null", "float"],  "Trail-fit flux (stub) [nJy]",
                    STUB, "trailed-source fitting (not run)"),
    Field("trailFluxErr",  ["null", "float"],  "Uncertainty in trailFlux (stub) [nJy]",
                    STUB, "trailed-source fitting (not run)"),
    Field("trailLength",   ["null", "float"],  "Trail length (stub) [arcsec]",
                    STUB, "trailed-source fitting (not run)"),
    Field("trailAngle",    ["null", "float"],  "Trail angle (stub) [deg]",
                    STUB, "trailed-source fitting (not run)"),

    # --- Dipole fit (all stubs) -----------------------------------------------
    Field("dipoleMeanFlux", ["null", "float"], "Dipole mean flux (stub) [nJy]",
                    STUB, "dipole analysis (not run)"),
    Field("dipoleFluxErr", ["null", "float"],  "Uncertainty in dipoleMeanFlux (stub) [nJy]",
                    STUB, "dipole analysis (not run)"),
    Field("dipoleLength",  ["null", "float"],  "Dipole separation (stub) [arcsec]",
                    STUB, "dipole analysis (not run)"),
    Field("dipoleAngle",   ["null", "float"],  "Dipole orientation (stub) [deg]",
                    STUB, "dipole analysis (not run)"),

    # --- Shape moments (all stubs) --------------------------------------------
    Field("ixx",           ["null", "float"],  "Adaptive second moment Ixx (stub) [arcsec^2]",
                    STUB, "shape measurement (not in DB flow; SExtractor moments in file flow)"),
    Field("iyy",           ["null", "float"],  "Adaptive second moment Iyy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Field("ixy",           ["null", "float"],  "Adaptive second moment Ixy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Field("ixxErr",        ["null", "float"],  "Uncertainty in ixx (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Field("iyyErr",        ["null", "float"],  "Uncertainty in iyy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Field("ixyErr",        ["null", "float"],  "Uncertainty in ixy (stub) [arcsec^2]",
                    STUB, "shape measurement"),
    Field("elong",         ["null", "float"],  "Elongation of source: ratio of major to minor axis (stub)",
                    STUB, "shape measurement (SExtractor ELONGATION in file flow)"),

    # --- Flags -----------------------------------------------------------------
    Field("flags",         "long",             "Bitmask of processing flags",
                    IMPLEMENTED, "sources.flags"),
    Field("pixelFlags_saturated", ["null", "boolean"], "Source has saturated pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Field("pixelFlags_bad",       ["null", "boolean"], "Source has bad pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Field("pixelFlags_edge",      ["null", "boolean"], "Source is near detector edge (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Field("pixelFlags_cr",        ["null", "boolean"], "Source has cosmic ray pixels (stub)",
                    STUB, "pixel-mask analysis (not run)"),
    Field("centroid_flag", ["null", "boolean"], "Centroid measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Field("apFlux_flag",   ["null", "boolean"], "Aperture flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),
    Field("psfFlux_flag",  ["null", "boolean"], "PSF flux measurement failed (stub)",
                    STUB, "may fold into flags bitmask (spreadsheet: 'flags dict?')"),

    # --- Nearest reference-image source (all stubs) ------------------------------
    Field("distnr",        ["null", "float"],  "Distance to nearest reference-image source (stub) [arcsec]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Field("ranr",          ["null", "double"], "RA of nearest reference-image source (stub) [deg]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Field("decnr",         ["null", "double"], "Dec of nearest reference-image source (stub) [deg]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Field("magnr",         ["null", "float"],  "Magnitude of nearest reference-image source (stub) [mag]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Field("sigmagnr",      ["null", "float"],  "1-sigma uncertainty in magnr (stub) [mag]",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Field("chinr",         ["null", "float"],  "Chi parameter of nearest reference-image source (stub)",
                    STUB, "cross-match to reference-image catalog (not run)"),
    Field("sharpnr",       ["null", "float"],  "Sharpness parameter of nearest reference-image source (stub)",
                    STUB, "cross-match to reference-image catalog (not run)"),

    # --- Roman-specific identifiers & tiling ------------------------------------
    Field("sca",           "int",              "Roman SCA detector number",
                    IMPLEMENTED, "sources.sca"),
    Field("field",         "int",              "Roman field identifier",
                    IMPLEMENTED, "sources.field"),
    Field("hp6",           "int",              "HEALPix index at nside=64 (order 6)",
                    IMPLEMENTED, "sources.hp6"),
    Field("hp9",           "int",              "HEALPix index at nside=512 (order 9)",
                    IMPLEMENTED, "sources.hp9"),
    Field("pid",           "long",             "Processing ID for science image",
                    IMPLEMENTED, "sources.pid"),
    Field("expid",         "int",              "Exposure identifier",
                    IMPLEMENTED, "sources.expid"),
    Field("pass",          ["null", "int"],    "Roman survey pass number (stub)",
                    STUB, "Roman observation ID components (exposure metadata; not in sources table)"),
    Field("segment",       ["null", "int"],    "Roman survey segment number (stub)",
                    STUB, "Roman observation ID components (exposure metadata; not in sources table)"),
    Field("program",       ["null", "int"],    "Roman program identifier (stub)",
                    STUB, "Roman observation ID components (exposure metadata; not in sources table)"),
    Field("survey",        ["null", "string"], "Survey name (stub)",
                    STUB, "observation metadata (not available)"),
)


# ---------------------------------------------------------------------------
# diaForcedSource -- built from a providers.ForcedPhot
#
# The whole record is a stub: attrs are staged, but RAPID forced photometry
# currently produces FITS files rather than DB records, so no provider
# supplies ForcedPhot yet and prvDiaForcedSources is always null.
# ---------------------------------------------------------------------------

DIA_FORCED_SOURCE_FIELDS = (
    # --- Identifiers & associations ---
    Field("diaForcedSourceId", "long",            "Unique identifier for this forced source measurement",
                        STUB, _FP, attr="forced_id"),
    Field("diaObjectId",       "long",            "Associated diaObject identifier",
                        STUB, _FP, attr="aid"),
    Field("visit",             "long",            "Visit (exposure) identifier",
                        STUB, _FP, attr="expid"),
    Field("detector",          "int",             "Detector (SCA) number",
                        STUB, _FP, attr="sca"),

    # --- Position ---
    Field("ra",                "double",          "Right ascension of forced measurement position; ICRS [deg]",
                        STUB, _FP),
    Field("dec",               "double",          "Declination of forced measurement position; ICRS [deg]",
                        STUB, _FP),

    # --- Photometry ---
    Field("band",              ["null", "string"], "Filter band name",
                        STUB, _FP),
    Field("psfFlux",           ["null", "float"], "Forced PSF flux on difference image [nJy]",
                        STUB, _FP, attr="flux"),
    Field("psfFluxErr",        ["null", "float"], "Uncertainty in psfFlux [nJy]",
                        STUB, _FP, attr="fluxerr"),
    Field("scienceFlux",       ["null", "float"], "Forced PSF flux on science image (stub) [nJy]",
                        STUB, _FP),
    Field("scienceFluxErr",    ["null", "float"], "Uncertainty in scienceFlux (stub) [nJy]",
                        STUB, _FP),

    # --- Time ---
    Field("midpointMjdTai",    "double",          "Effective mid-observation time [TAI MJD]",
                        STUB, _FP, attr="mjdobs"),
    Field("timeProcessedMjdTai", "double",        "Time measurement was processed [TAI MJD]",
                        STUB, _FP, attr="time_processed"),
    Field("timeWithdrawnMjdTai", ["null", "double"], "Time measurement was withdrawn [TAI MJD]",
                        STUB, "alert-withdrawal mechanism (not designed)"),
)


# ---------------------------------------------------------------------------
# diaObject -- built from a providers.ObjectRecord
# ---------------------------------------------------------------------------

def _per_filter_flux_fields():
    """The 8 filters x 8 statistics block of diaObject fields."""
    src = ("per-filter statistics over associated sources; needs nJy flux "
           "calibration (see roman_rapid_alerts FILTER_ZP_EFF)")
    fields = []
    for b in ROMAN_FILTERS:
        fields += [
            Field(f"{b}PsfFluxMean",  ["null", "float"], f"Mean PSF flux in {b} [nJy]",
                        STUB, src),
            Field(f"{b}PsfFluxMeanErr", ["null", "float"], f"Uncertainty in {b}PsfFluxMean [nJy]",
                        STUB, src),
            Field(f"{b}PsfFluxSigma", ["null", "float"], f"Std dev of PSF flux in {b} [nJy]",
                        STUB, src),
            Field(f"{b}PsfFluxNdata", ["null", "int"],   f"Number of {b} measurements",
                        STUB, src),
            Field(f"{b}PsfFluxMin",   ["null", "float"], f"Minimum PSF flux in {b} [nJy]",
                        STUB, src),
            Field(f"{b}PsfFluxMax",   ["null", "float"], f"Maximum PSF flux in {b} [nJy]",
                        STUB, src),
            Field(f"{b}PsfFluxMaxSlope", ["null", "float"], f"Maximum slope of PSF flux between any two {b} epochs [nJy/day]",
                        STUB, src),
            Field(f"{b}PsfFluxErrMean", ["null", "float"], f"Mean of PSF flux errors in {b} [nJy]",
                        STUB, src),
        ]
    return tuple(fields)


DIA_OBJECT_FIELDS = (
    # --- Identifier & position -------------------------------------------
    Field("diaObjectId",   "long",             "Unique identifier for this object",
                        IMPLEMENTED, "astroobjects_<field>.aid",  attr="aid"),
    Field("ra",            "double",           "Right ascension of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.ra0",  attr="ra0"),
    Field("dec",           "double",           "Declination of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.dec0", attr="dec0"),
    Field("raErr",         ["null", "float"],  "Uncertainty in ra [deg]",
                        STUB, "astroobjects stdevra is available but unused"),
    Field("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
                        STUB, "astroobjects stdevdec is available but unused"),

    # --- Source history ----------------------------------------------------
    Field("nDiaSources",   "int",              "Total number of associated DIASources",
                        IMPLEMENTED, "astroobjects_<field>.nsources", attr="nsources"),
    Field("firstDiaSourceMjdTai", ["null", "double"], "MJD of earliest associated diaSource [TAI MJD]",
                        IMPLEMENTED, "computed from source history", attr="first_mjd"),
    Field("lastDiaSourceMjdTai",  ["null", "double"], "MJD of latest associated diaSource [TAI MJD]",
                        IMPLEMENTED, "computed from source history", attr="last_mjd"),
    Field("validityStartMjdTai",  "double",    "Start of validity interval for this object summary [TAI MJD]",
                        IMPLEMENTED, "triggering source mjdobs", attr="validity_mjd"),
    Field("ncovhist",      ["null", "int"],    "Number of times the object position fell on an observed image (stub)",
                        STUB, "coverage history (not computed)"),
    Field("firstRefMjdTai", ["null", "double"], "MJD of earliest exposure in the reference image [TAI MJD] (stub)",
                        STUB, "reference-image metadata (not tracked)"),
    Field("lastRefMjdTai", ["null", "double"], "MJD of latest exposure in the reference image [TAI MJD] (stub)",
                        STUB, "reference-image metadata (not tracked)"),

    # --- Per-filter flux statistics (all stubs) ------------------------------
) + _per_filter_flux_fields()


# ---------------------------------------------------------------------------
# ssSource / mpc_orbits -- entire records are stubs
# ---------------------------------------------------------------------------

SS_SOURCE_FIELDS = (
    # --- Identifiers & associations ---
    Field("ssSourceId",       "long",             "Unique identifier for this solar system source",
                        STUB, _SS),
    Field("diaSourceId",      "long",             "Associated diaSource identifier",
                        STUB, _SS),
    Field("ssObjectId",       ["null", "long"],   "Associated solar system object identifier",
                        STUB, _SS),

    # --- Geometry ---
    Field("heliocentricX",    ["null", "double"], "Heliocentric x position [AU]",
                        STUB, _SS),
    Field("heliocentricY",    ["null", "double"], "Heliocentric y position [AU]",
                        STUB, _SS),
    Field("heliocentricZ",    ["null", "double"], "Heliocentric z position [AU]",
                        STUB, _SS),
    Field("phaseAngle",       ["null", "float"],  "Phase angle [deg]",
                        STUB, _SS),
    Field("heliocentricDist", ["null", "float"],  "Heliocentric distance [AU]",
                        STUB, _SS),
    Field("topocentricDist",  ["null", "float"],  "Topocentric distance [AU]",
                        STUB, _SS),
)

MPC_ORBITS_FIELDS = (
    # --- Identifier ---
    Field("id",    "string",           "MPC designation or packed designation",
                        STUB, _MPC),

    # --- Orbital elements ---
    Field("a",     ["null", "double"], "Semi-major axis [AU]",
                        STUB, _MPC),
    Field("e",     ["null", "double"], "Eccentricity",
                        STUB, _MPC),
    Field("incl",  ["null", "double"], "Inclination [deg]",
                        STUB, _MPC),
    Field("Omega", ["null", "double"], "Longitude of ascending node [deg]",
                        STUB, _MPC),
    Field("omega", ["null", "double"], "Argument of perihelion [deg]",
                        STUB, _MPC),
    Field("M",     ["null", "double"], "Mean anomaly [deg]",
                        STUB, _MPC),
    Field("epoch", ["null", "double"], "Epoch of orbital elements [MJD]",
                        STUB, _MPC),

    # --- Photometric parameters ---
    Field("H",     ["null", "float"],  "Absolute magnitude [mag]",
                        STUB, _MPC),
    Field("G",     ["null", "float"],  "Slope parameter",
                        STUB, _MPC),
)


# ---------------------------------------------------------------------------
# alert (top level) -- structural fields, filled directly by
# produce.assemble_alert(), which also verifies its keys match this list
# ---------------------------------------------------------------------------

ALERT_FIELDS = (
    # --- Provenance ---------------------------------------------------------
    Field("schemaVersion",  ["null", "string"], "Version of the alert schema used to serialize this packet",
                        IMPLEMENTED, "fields.VERSION (set in produce.py)"),
    Field("pipelineVersion", ["null", "string"], "Version of the RAPID pipeline that produced this alert (stub)",
                        STUB, "pipeline build/release identifier (not plumbed through)"),

    # --- Triggering detection --------------------------------------------
    Field("diaSourceId",    "long",           "Identifier for the triggering diaSource",
                        IMPLEMENTED, "produce.assemble_alert()"),
    Field("diaSource",      "@diaSource",     "Triggering source detection",
                        IMPLEMENTED, "produce.assemble_alert()"),

    # --- Object history ------------------------------------------------------
    Field("prvDiaSources",      ["null", {"type": "array", "items": "@diaSource"}],
                                "Previous detections of the same object within 12 months",
                        IMPLEMENTED, "produce.assemble_alert()"),
    Field("diaObject",          ["null", "@diaObject"],  "Summary object record",
                        IMPLEMENTED, "produce.assemble_alert()"),
    Field("prvDiaForcedSources", ["null", {"type": "array", "items": "@diaForcedSource"}],
                                 "Forced photometry history at the object position",
                        STUB, _FP),

    # --- Solar system (all stubs) ---------------------------------------------
    Field("ssSource",           ["null", "@ssSource"],   "Solar system source association (stub)",
                        STUB, _SS),
    Field("mpc_orbits",         ["null", "@mpc_orbits"], "MPC orbital elements (stub)",
                        STUB, _MPC),

    # --- Image cutouts ----------------------------------------------------------
    Field("cutoutDifference",   ["null", "bytes"],   "FITS cutout of difference image",
                        IMPLEMENTED, "provider get_cutouts()"),
    Field("cutoutScience",      ["null", "bytes"],   "FITS cutout of science image",
                        IMPLEMENTED, "provider get_cutouts()"),
    Field("cutoutTemplate",     ["null", "bytes"],   "FITS cutout of template image",
                        IMPLEMENTED, "provider get_cutouts()"),

    # --- Observation metadata (all stubs) ----------------------------------------
    Field("observation_reason", ["null", "string"],     "Reason for observation (e.g. survey, ToO)",
                        STUB, "observation metadata (not available)"),
    Field("target_name",        ["null", "string"],     "Target name if targeted observation",
                        STUB, "observation metadata (not available)"),
)


# ---------------------------------------------------------------------------
# Record registry, in Avro dependency order (referenced records first)
# ---------------------------------------------------------------------------

RECORDS = (
    Record("diaSource",       "RAPID alert schema: individual source detection on a difference image",       DIA_SOURCE_FIELDS),
    Record("diaForcedSource", "RAPID alert schema: forced photometry measurement at a diaObject position",   DIA_FORCED_SOURCE_FIELDS),
    Record("diaObject",       "RAPID alert schema: astronomical object derived from DIASources",             DIA_OBJECT_FIELDS),
    Record("ssSource",        "RAPID alert schema: solar system source association (stub)",                  SS_SOURCE_FIELDS),
    Record("mpc_orbits",      "RAPID alert schema: MPC orbital elements (stub)",                             MPC_ORBITS_FIELDS),
    Record("alert",           "RAPID alert schema: top-level alert record (LSST-compatible)",                ALERT_FIELDS),
)


# ---------------------------------------------------------------------------
# Status report: python -m rapid_alerts.fields [--summary]
# ---------------------------------------------------------------------------

def print_report(summary=False):
    """Print the implemented/stub status of every field, per record."""
    mark = {IMPLEMENTED: "x", STUB: " ", NOT_USED: "-"}
    total_implemented = total_stub = 0

    for record in RECORDS:
        counts = {status: sum(1 for f in record.fields if f.status is status)
                  for status in Status}
        total_implemented += counts[IMPLEMENTED]
        total_stub += counts[STUB]

        line = (f"{record.name}: {len(record.fields)} fields -- "
                f"{counts[IMPLEMENTED]} implemented, {counts[STUB]} stub")
        if counts[NOT_USED]:
            line += f", {counts[NOT_USED]} not used"
        print(f"\n{line}")

        if not summary:
            print("-" * len(line))
            for f in record.fields:
                print(f"  [{mark[f.status]}] {f.name:<26} {f.source or ''}")

    print(f"\nTotal: {total_implemented} implemented, {total_stub} stub "
          f"([x] = implemented, [ ] = stub, [-] = not used)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Show implemented vs stub alert schema fields")
    parser.add_argument("--summary", action="store_true",
                        help="per-record counts only")
    print_report(summary=parser.parse_args().summary)
