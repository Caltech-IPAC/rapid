"""
Single source of truth for the RAPID alert schema (current version: VERSION below).

Every field of every record in the alert packet is declared here once, with:

  - avro type + doc     -> gen_schema.py writes the .avsc files from these
  - status + source     -> report.py prints the implemented/stub inventory
  - getter              -> build.py fills record dicts from the normalized
                           records in records.py (stubs have no getter and
                           are always serialized as null)

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
    IMPLEMENTED = "implemented"   # real data ends up in produced alerts
    STUB = "stub"                 # in the schema, but always null for now
    NOT_USED = "not used"         # kept for reference; excluded from .avsc


@dataclass(frozen=True)
class Field:
    name: str
    avro: Any                     # Avro type (str, union list, or dict)
    doc: str
    status: Status
    source: Optional[str] = None  # implemented: where the value comes from
                                  # stub: what work would fill it in
    getter: Optional[Callable] = None


@dataclass(frozen=True)
class Record:
    name: str
    doc: str
    fields: tuple


IMPLEMENTED = Status.IMPLEMENTED
STUB = Status.STUB
NOT_USED = Status.NOT_USED

# Shared stub-source descriptions
_FP = "forced photometry (products not integrated)"
_SS = "solar-system processing (not run)"
_MPC = "MPC orbit ingest (not run)"


# ---------------------------------------------------------------------------
# diaSource -- getters take a records.Detection
# ---------------------------------------------------------------------------

DIA_SOURCE_FIELDS = (
    # --- Identifiers & associations -------------------------------------
    Field("diaSourceId",   "long",             "Unique identifier for this source detection",
                    IMPLEMENTED, "sources.sid",    lambda d: d.sid),
    Field("visit",         "long",             "Visit (exposure) identifier",
                    IMPLEMENTED, "sources.expid",  lambda d: int(d.expid)),
    Field("detector",      "int",              "Detector (SCA) number",
                    IMPLEMENTED, "sources.sca",    lambda d: int(d.sca)),
    Field("diaObjectId",   ["null", "long"],   "Associated diaObject identifier",
                    IMPLEMENTED, "merges_<field>.aid", lambda d: d.aid),
    Field("ssObjectId",    ["null", "long"],   "Associated solar system object identifier (stub)",
                    STUB, "solar-system cross-matching (not run)"),

    # --- Time ------------------------------------------------------------
    Field("midpointMjdTai", "double",          "Effective mid-observation time [TAI MJD]",
                    IMPLEMENTED, "sources.mjdobs", lambda d: float(d.mjdobs)),
    Field("timeProcessedMjdTai",  ["null", "double"],  "Time alert was processed [TAI MJD]",
                    STUB, "set at assembly time (decide TAI convention first)"),
    Field("timeWithdrawnMjdTai",  ["null", "double"],  "Time alert was withdrawn [TAI MJD]",
                    STUB, "alert-withdrawal mechanism (not designed)"),
    Field("exposureTime",  ["null", "float"],  "Exposure duration [s]",
                    STUB, "exposures table metadata (join not implemented)"),

    # --- Position (sky & pixel) -------------------------------------------
    Field("ra",            "double",           "Right ascension; ICRS [deg]",
                    IMPLEMENTED, "sources.ra",     lambda d: float(d.ra)),
    Field("dec",           "double",           "Declination; ICRS [deg]",
                    IMPLEMENTED, "sources.dec",    lambda d: float(d.dec)),
    Field("x",             "float",            "x-pixel position on detector [pixels]",
                    IMPLEMENTED, "sources.xfit",   lambda d: float(d.xfit)),
    Field("y",             "float",            "y-pixel position on detector [pixels]",
                    IMPLEMENTED, "sources.yfit",   lambda d: float(d.yfit)),
    Field("xErr",          ["null", "float"],  "Uncertainty in x [pixels]",
                    IMPLEMENTED, "sources.xerr",   lambda d: d.xerr),
    Field("yErr",          ["null", "float"],  "Uncertainty in y [pixels]",
                    IMPLEMENTED, "sources.yerr",   lambda d: d.yerr),
    Field("raErr",         ["null", "float"],  "Uncertainty in ra [deg]",
                    STUB, "not produced by PSF fit yet"),
    Field("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
                    STUB, "not produced by PSF fit yet"),

    # --- Photometry --------------------------------------------------------
    Field("band",          ["null", "string"], "Filter band name (F062, F087, F106, F129, F146, F158, F184, F213)",
                    IMPLEMENTED, "filters.filter", lambda d: d.band),
    Field("psfFlux",       ["null", "float"],  "Flux from PSF-fit on difference image [nJy]",
                    IMPLEMENTED, "sources.fluxfit (instrumental; nJy calibration pending)", lambda d: d.fluxfit),
    Field("psfFluxErr",    ["null", "float"],  "Uncertainty in psfFlux [nJy]",
                    IMPLEMENTED, "sources.fluxerr", lambda d: d.fluxerr),
    Field("snr",           ["null", "float"],  "Signal-to-noise ratio (psfFlux / psfFluxErr)",
                    IMPLEMENTED, "computed: fluxfit / fluxerr", lambda d: d.snr),
    Field("isNegative",    "boolean",          "true if source is from negative (ref minus sci) subtraction",
                    IMPLEMENTED, "sources.isdiffpos (inverted; renamed per schema spreadsheet)",
                    lambda d: not bool(d.isdiffpos)),
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
                    IMPLEMENTED, "sources.qfit",   lambda d: d.qfit),
    Field("cfit",          ["null", "float"],  "PSF-fit chi parameter",
                    IMPLEMENTED, "sources.cfit",   lambda d: d.cfit),
    Field("redchi",        ["null", "float"],  "Reduced chi-square of PSF fit",
                    IMPLEMENTED, "sources.redchi", lambda d: d.redchi),
    Field("npixfit",       ["null", "int"],    "Number of pixels used in PSF fit",
                    IMPLEMENTED, "sources.npixfit", lambda d: d.npixfit),
    Field("sharpness",     ["null", "float"],  "PSF-fit sharpness parameter",
                    IMPLEMENTED, "sources.sharpness", lambda d: d.sharpness),
    Field("roundness1",    ["null", "float"],  "PSF-fit roundness parameter 1",
                    IMPLEMENTED, "sources.roundness1", lambda d: d.roundness1),
    Field("roundness2",    ["null", "float"],  "PSF-fit roundness parameter 2",
                    IMPLEMENTED, "sources.roundness2", lambda d: d.roundness2),
    Field("peak",          ["null", "float"],  "Peak pixel value in source stamp [DN]",
                    IMPLEMENTED, "sources.peak",   lambda d: d.peak),
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
                    IMPLEMENTED, "sources.flags",  lambda d: int(d.flags)),
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
                    IMPLEMENTED, "sources.sca",    lambda d: int(d.sca)),
    Field("field",         "int",              "Roman field identifier",
                                          IMPLEMENTED, "sources.field",  lambda d: int(d.field)),
    Field("hp6",           "int",              "HEALPix index at nside=64 (order 6)",
                    IMPLEMENTED, "sources.hp6",    lambda d: int(d.hp6)),
    Field("hp9",           "int",              "HEALPix index at nside=512 (order 9)",
                    IMPLEMENTED, "sources.hp9",    lambda d: int(d.hp9)),
    Field("pid",           "long",             "Processing ID for science image",
                    IMPLEMENTED, "sources.pid",    lambda d: int(d.pid)),
    Field("expid",         "int",              "Exposure identifier",
                    IMPLEMENTED, "sources.expid",  lambda d: int(d.expid)),
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
# diaForcedSource -- getters take a records.ForcedPhot
#
# The whole record is a stub: builders are ready, but RAPID forced photometry
# currently produces FITS files rather than DB records, so no provider
# supplies ForcedPhot yet and prvDiaForcedSources is always null.
# ---------------------------------------------------------------------------

DIA_FORCED_SOURCE_FIELDS = (
    # --- Identifiers & associations ---
    Field("diaForcedSourceId", "long",            "Unique identifier for this forced source measurement",
                        STUB, _FP, lambda p: int(p.forced_id)),
    Field("diaObjectId",       "long",            "Associated diaObject identifier",
                        STUB, _FP, lambda p: int(p.aid)),
    Field("visit",             "long",            "Visit (exposure) identifier",
                        STUB, _FP, lambda p: int(p.expid)),
    Field("detector",          "int",             "Detector (SCA) number",
                        STUB, _FP, lambda p: int(p.sca)),

    # --- Position ---
    Field("ra",                "double",          "Right ascension of forced measurement position; ICRS [deg]",
                        STUB, _FP, lambda p: float(p.ra)),
    Field("dec",               "double",          "Declination of forced measurement position; ICRS [deg]",
                        STUB, _FP, lambda p: float(p.dec)),

    # --- Photometry ---
    Field("band",              ["null", "string"], "Filter band name",
                        STUB, _FP, lambda p: p.band),
    Field("psfFlux",           ["null", "float"], "Forced PSF flux on difference image [nJy]",
                        STUB, _FP, lambda p: p.flux),
    Field("psfFluxErr",        ["null", "float"], "Uncertainty in psfFlux [nJy]",
                        STUB, _FP, lambda p: p.fluxerr),
    Field("scienceFlux",       ["null", "float"], "Forced PSF flux on science image (stub) [nJy]",
                        STUB, _FP),
    Field("scienceFluxErr",    ["null", "float"], "Uncertainty in scienceFlux (stub) [nJy]",
                        STUB, _FP),

    # --- Time ---
    Field("midpointMjdTai",    "double",          "Effective mid-observation time [TAI MJD]",
                        STUB, _FP, lambda p: float(p.mjdobs)),
    Field("timeProcessedMjdTai", "double",        "Time measurement was processed [TAI MJD]",
                        STUB, _FP, lambda p: float(p.time_processed)),
    Field("timeWithdrawnMjdTai", ["null", "double"], "Time measurement was withdrawn [TAI MJD]",
                        STUB, "alert-withdrawal mechanism (not designed)"),
)


# ---------------------------------------------------------------------------
# diaObject -- getters take a records.ObjectRecord
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
                        IMPLEMENTED, "astroobjects_<field>.aid",  lambda o: int(o.aid)),
    Field("ra",            "double",           "Right ascension of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.ra0",  lambda o: float(o.ra0)),
    Field("dec",           "double",           "Declination of object centroid; ICRS [deg]",
                        IMPLEMENTED, "astroobjects_<field>.dec0", lambda o: float(o.dec0)),
    Field("raErr",         ["null", "float"],  "Uncertainty in ra [deg]",
                        STUB, "astroobjects stdevra is available but unused"),
    Field("decErr",        ["null", "float"],  "Uncertainty in dec [deg]",
                        STUB, "astroobjects stdevdec is available but unused"),

    # --- Source history ----------------------------------------------------
    Field("nDiaSources",   "int",              "Total number of associated DIASources",
                        IMPLEMENTED, "astroobjects_<field>.nsources", lambda o: int(o.nsources)),
    Field("firstDiaSourceMjdTai", ["null", "double"], "MJD of earliest associated diaSource [TAI MJD]",
                        IMPLEMENTED, "computed from source history", lambda o: o.first_mjd),
    Field("lastDiaSourceMjdTai",  ["null", "double"], "MJD of latest associated diaSource [TAI MJD]",
                        IMPLEMENTED, "computed from source history", lambda o: o.last_mjd),
    Field("validityStartMjdTai",  "double",    "Start of validity interval for this object summary [TAI MJD]",
                        IMPLEMENTED, "triggering source mjdobs", lambda o: float(o.validity_mjd)),
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
# alert (top level) -- structural fields, assembled in assemble.py
# ---------------------------------------------------------------------------

ALERT_FIELDS = (
    # --- Provenance ---------------------------------------------------------
    Field("schemaVersion",  ["null", "string"], "Version of the alert schema used to serialize this packet",
                        IMPLEMENTED, "fields.VERSION (set in assemble.py)"),
    Field("pipelineVersion", ["null", "string"], "Version of the RAPID pipeline that produced this alert (stub)",
                        STUB, "pipeline build/release identifier (not plumbed through)"),

    # --- Triggering detection --------------------------------------------
    Field("diaSourceId",    "long",           "Identifier for the triggering diaSource",
                        IMPLEMENTED, "assemble.py"),
    Field("diaSource",      "@diaSource",     "Triggering source detection",
                        IMPLEMENTED, "assemble.py"),

    # --- Object history ------------------------------------------------------
    Field("prvDiaSources",      ["null", {"type": "array", "items": "@diaSource"}],
                                "Previous detections of the same object within 12 months",
                        IMPLEMENTED, "assemble.py"),
    Field("diaObject",          ["null", "@diaObject"],  "Summary object record",
                        IMPLEMENTED, "assemble.py"),
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
