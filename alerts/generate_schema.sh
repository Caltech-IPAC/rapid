#!/usr/bin/env bash

# Generates RAPID v01.00 Avro alert schema files (LSST-compatible)
# Uses LSST record names and structure with Roman-appropriate content
# Reference:
#   LSST alert_packet v10.0
#     https://github.com/lsst/alert_packet
#   Roman filters: F062, F087, F106, F129, F146, F158, F184, F213

# Strict error handling
set -euo pipefail
IFS=$'\n\t'

# Schema version
VERSION=01.00
MAJOR=01
MINOR=00
NAMESPACE="rapid.v01_00"

OUTDIR="schema/${MAJOR}/${MINOR}"
echo "Generating RAPID avro alert schema v${VERSION}"
mkdir -p "${OUTDIR}"

# --- diaSource ---
cat << 'EOF' > "${OUTDIR}/rapid.v01_00.diaSource.avsc"
{
	"namespace": "rapid.v01_00",
	"name": "diaSource",
	"doc": "RAPID alert schema: individual source detection on a difference image",
	"version": "01.00",
	"type": "record",
	"fields": [
		{"name": "diaSourceId",        "type": "long",              "doc": "Unique identifier for this source detection"},
		{"name": "visit",              "type": "long",              "doc": "Visit (exposure) identifier"},
		{"name": "detector",           "type": "int",               "doc": "Detector (SCA) number"},
		{"name": "diaObjectId",        "type": ["null", "long"],    "default": null, "doc": "Associated diaObject identifier"},
		{"name": "ssObjectId",         "type": ["null", "long"],    "default": null, "doc": "Associated solar system object identifier (stub)"},
		{"name": "parentDiaSourceId",  "type": ["null", "long"],    "default": null, "doc": "Parent diaSource if deblended (stub)"},
		{"name": "midpointMjdTai",     "type": "double",            "doc": "Effective mid-observation time [TAI MJD]"},
		{"name": "ra",                 "type": "double",            "doc": "Right ascension; ICRS [deg]"},
		{"name": "dec",                "type": "double",            "doc": "Declination; ICRS [deg]"},
		{"name": "raErr",              "type": ["null", "float"],   "default": null, "doc": "Uncertainty in ra [deg]"},
		{"name": "decErr",             "type": ["null", "float"],   "default": null, "doc": "Uncertainty in dec [deg]"},
		{"name": "x",                  "type": "float",             "doc": "x-pixel position on detector [pixels]"},
		{"name": "y",                  "type": "float",             "doc": "y-pixel position on detector [pixels]"},
		{"name": "xErr",               "type": ["null", "float"],   "default": null, "doc": "Uncertainty in x [pixels]"},
		{"name": "yErr",               "type": ["null", "float"],   "default": null, "doc": "Uncertainty in y [pixels]"},
		{"name": "band",               "type": ["null", "string"],  "default": null, "doc": "Filter band name (F062, F087, F106, F129, F146, F158, F184, F213)"},
		{"name": "psfFlux",            "type": ["null", "float"],   "default": null, "doc": "Flux from PSF-fit on difference image [nJy]"},
		{"name": "psfFluxErr",         "type": ["null", "float"],   "default": null, "doc": "Uncertainty in psfFlux [nJy]"},
		{"name": "snr",                "type": ["null", "float"],   "default": null, "doc": "Signal-to-noise ratio (psfFlux / psfFluxErr)"},
		{"name": "extendedness",       "type": ["null", "float"],   "default": null, "doc": "Probability of being extended (stub)"},
		{"name": "reliability",        "type": ["null", "float"],   "default": null, "doc": "Reliability score (stub)"},
		{"name": "flags",              "type": "long",              "doc": "Bitmask of processing flags"},

		{"name": "apFlux",             "type": ["null", "float"],   "default": null, "doc": "Aperture flux on difference image (stub) [nJy]"},
		{"name": "apFluxErr",          "type": ["null", "float"],   "default": null, "doc": "Uncertainty in apFlux (stub) [nJy]"},
		{"name": "trailFlux",          "type": ["null", "float"],   "default": null, "doc": "Trail-fit flux (stub) [nJy]"},
		{"name": "trailFluxErr",       "type": ["null", "float"],   "default": null, "doc": "Uncertainty in trailFlux (stub) [nJy]"},
		{"name": "trailLength",        "type": ["null", "float"],   "default": null, "doc": "Trail length (stub) [arcsec]"},
		{"name": "trailAngle",         "type": ["null", "float"],   "default": null, "doc": "Trail angle (stub) [deg]"},
		{"name": "scienceFlux",        "type": ["null", "float"],   "default": null, "doc": "Forced PSF flux on science image (stub) [nJy]"},
		{"name": "scienceFluxErr",     "type": ["null", "float"],   "default": null, "doc": "Uncertainty in scienceFlux (stub) [nJy]"},
		{"name": "templateFlux",       "type": ["null", "float"],   "default": null, "doc": "Forced PSF flux on template image (stub) [nJy]"},
		{"name": "templateFluxErr",    "type": ["null", "float"],   "default": null, "doc": "Uncertainty in templateFlux (stub) [nJy]"},
		{"name": "dipoleMeanFlux",     "type": ["null", "float"],   "default": null, "doc": "Dipole mean flux (stub) [nJy]"},
		{"name": "dipoleFluxErr",      "type": ["null", "float"],   "default": null, "doc": "Uncertainty in dipoleMeanFlux (stub) [nJy]"},
		{"name": "dipoleLength",       "type": ["null", "float"],   "default": null, "doc": "Dipole separation (stub) [arcsec]"},
		{"name": "dipoleAngle",        "type": ["null", "float"],   "default": null, "doc": "Dipole orientation (stub) [deg]"},

		{"name": "ixx",                "type": ["null", "float"],   "default": null, "doc": "Adaptive second moment Ixx (stub) [arcsec^2]"},
		{"name": "iyy",                "type": ["null", "float"],   "default": null, "doc": "Adaptive second moment Iyy (stub) [arcsec^2]"},
		{"name": "ixy",                "type": ["null", "float"],   "default": null, "doc": "Adaptive second moment Ixy (stub) [arcsec^2]"},
		{"name": "ixxErr",             "type": ["null", "float"],   "default": null, "doc": "Uncertainty in ixx (stub) [arcsec^2]"},
		{"name": "iyyErr",             "type": ["null", "float"],   "default": null, "doc": "Uncertainty in iyy (stub) [arcsec^2]"},
		{"name": "ixyErr",             "type": ["null", "float"],   "default": null, "doc": "Uncertainty in ixy (stub) [arcsec^2]"},

		{"name": "pixelFlags_saturated",  "type": ["null", "boolean"], "default": null, "doc": "Source has saturated pixels (stub)"},
		{"name": "pixelFlags_bad",        "type": ["null", "boolean"], "default": null, "doc": "Source has bad pixels (stub)"},
		{"name": "pixelFlags_edge",       "type": ["null", "boolean"], "default": null, "doc": "Source is near detector edge (stub)"},
		{"name": "pixelFlags_cr",         "type": ["null", "boolean"], "default": null, "doc": "Source has cosmic ray pixels (stub)"},

		{"name": "timeProcessedMjdTai",   "type": ["null", "double"], "default": null, "doc": "Time alert was processed [TAI MJD]"},
		{"name": "timeWithdrawnMjdTai",   "type": ["null", "double"], "default": null, "doc": "Time alert was withdrawn [TAI MJD]"},

		{"name": "sca",               "type": "int",               "doc": "Roman SCA detector number"},
		{"name": "field",              "type": "int",               "doc": "Roman field identifier"},
		{"name": "hp6",                "type": "int",               "doc": "HEALPix index at nside=64 (order 6)"},
		{"name": "hp9",                "type": "int",               "doc": "HEALPix index at nside=512 (order 9)"},
		{"name": "pid",                "type": "long",              "doc": "Processing ID for science image"},
		{"name": "expid",              "type": "int",               "doc": "Exposure identifier"},
		{"name": "isdiffpos",          "type": "boolean",           "doc": "true if source is from positive (sci minus ref) subtraction"},
		{"name": "qfit",               "type": ["null", "float"],   "default": null, "doc": "PSF-fit quality parameter"},
		{"name": "cfit",               "type": ["null", "float"],   "default": null, "doc": "PSF-fit chi parameter"},
		{"name": "redchi",             "type": ["null", "float"],   "default": null, "doc": "Reduced chi-square of PSF fit"},
		{"name": "npixfit",            "type": ["null", "int"],     "default": null, "doc": "Number of pixels used in PSF fit"},
		{"name": "sharpness",          "type": ["null", "float"],   "default": null, "doc": "PSF-fit sharpness parameter"},
		{"name": "roundness1",         "type": ["null", "float"],   "default": null, "doc": "PSF-fit roundness parameter 1"},
		{"name": "roundness2",         "type": ["null", "float"],   "default": null, "doc": "PSF-fit roundness parameter 2"},
		{"name": "peak",               "type": ["null", "float"],   "default": null, "doc": "Peak pixel value in source stamp [DN]"}
	]
}
EOF

# --- diaForcedSource ---
cat << 'EOF' > "${OUTDIR}/rapid.v01_00.diaForcedSource.avsc"
{
	"namespace": "rapid.v01_00",
	"name": "diaForcedSource",
	"doc": "RAPID alert schema: forced photometry measurement at a diaObject position",
	"version": "01.00",
	"type": "record",
	"fields": [
		{"name": "diaForcedSourceId",  "type": "long",              "doc": "Unique identifier for this forced source measurement"},
		{"name": "diaObjectId",        "type": "long",              "doc": "Associated diaObject identifier"},
		{"name": "visit",              "type": "long",              "doc": "Visit (exposure) identifier"},
		{"name": "detector",           "type": "int",               "doc": "Detector (SCA) number"},
		{"name": "ra",                 "type": "double",            "doc": "Right ascension of forced measurement position; ICRS [deg]"},
		{"name": "dec",                "type": "double",            "doc": "Declination of forced measurement position; ICRS [deg]"},
		{"name": "band",               "type": ["null", "string"],  "default": null, "doc": "Filter band name"},
		{"name": "psfFlux",            "type": ["null", "float"],   "default": null, "doc": "Forced PSF flux on difference image [nJy]"},
		{"name": "psfFluxErr",         "type": ["null", "float"],   "default": null, "doc": "Uncertainty in psfFlux [nJy]"},
		{"name": "scienceFlux",        "type": ["null", "float"],   "default": null, "doc": "Forced PSF flux on science image (stub) [nJy]"},
		{"name": "scienceFluxErr",     "type": ["null", "float"],   "default": null, "doc": "Uncertainty in scienceFlux (stub) [nJy]"},
		{"name": "midpointMjdTai",     "type": "double",            "doc": "Effective mid-observation time [TAI MJD]"},
		{"name": "timeProcessedMjdTai","type": "double",            "doc": "Time measurement was processed [TAI MJD]"},
		{"name": "timeWithdrawnMjdTai","type": ["null", "double"],  "default": null, "doc": "Time measurement was withdrawn [TAI MJD]"}
	]
}
EOF

# --- diaObject ---
cat << 'EOF' > "${OUTDIR}/rapid.v01_00.diaObject.avsc"
{
	"namespace": "rapid.v01_00",
	"name": "diaObject",
	"doc": "RAPID alert schema: astronomical object derived from DIASources",
	"version": "01.00",
	"type": "record",
	"fields": [
		{"name": "diaObjectId",            "type": "long",              "doc": "Unique identifier for this object"},
		{"name": "ra",                     "type": "double",            "doc": "Right ascension of object centroid; ICRS [deg]"},
		{"name": "dec",                    "type": "double",            "doc": "Declination of object centroid; ICRS [deg]"},
		{"name": "raErr",                  "type": ["null", "float"],   "default": null, "doc": "Uncertainty in ra [deg]"},
		{"name": "decErr",                 "type": ["null", "float"],   "default": null, "doc": "Uncertainty in dec [deg]"},
		{"name": "nDiaSources",            "type": "int",               "doc": "Total number of associated DIASources"},
		{"name": "firstDiaSourceMjdTai",   "type": ["null", "double"],  "default": null, "doc": "MJD of earliest associated diaSource [TAI MJD]"},
		{"name": "lastDiaSourceMjdTai",    "type": ["null", "double"],  "default": null, "doc": "MJD of latest associated diaSource [TAI MJD]"},
		{"name": "validityStartMjdTai",    "type": "double",            "doc": "Start of validity interval for this object summary [TAI MJD]"},

		{"name": "F062PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F062 [nJy]"},
		{"name": "F062PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F062 [nJy]"},
		{"name": "F062PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F062 measurements"},
		{"name": "F062PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F062 [nJy]"},
		{"name": "F062PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F062 [nJy]"},

		{"name": "F087PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F087 [nJy]"},
		{"name": "F087PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F087 [nJy]"},
		{"name": "F087PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F087 measurements"},
		{"name": "F087PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F087 [nJy]"},
		{"name": "F087PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F087 [nJy]"},

		{"name": "F106PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F106 [nJy]"},
		{"name": "F106PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F106 [nJy]"},
		{"name": "F106PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F106 measurements"},
		{"name": "F106PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F106 [nJy]"},
		{"name": "F106PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F106 [nJy]"},

		{"name": "F129PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F129 [nJy]"},
		{"name": "F129PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F129 [nJy]"},
		{"name": "F129PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F129 measurements"},
		{"name": "F129PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F129 [nJy]"},
		{"name": "F129PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F129 [nJy]"},

		{"name": "F146PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F146 [nJy]"},
		{"name": "F146PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F146 [nJy]"},
		{"name": "F146PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F146 measurements"},
		{"name": "F146PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F146 [nJy]"},
		{"name": "F146PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F146 [nJy]"},

		{"name": "F158PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F158 [nJy]"},
		{"name": "F158PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F158 [nJy]"},
		{"name": "F158PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F158 measurements"},
		{"name": "F158PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F158 [nJy]"},
		{"name": "F158PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F158 [nJy]"},

		{"name": "F184PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F184 [nJy]"},
		{"name": "F184PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F184 [nJy]"},
		{"name": "F184PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F184 measurements"},
		{"name": "F184PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F184 [nJy]"},
		{"name": "F184PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F184 [nJy]"},

		{"name": "F213PsfFluxMean",   "type": ["null", "float"], "default": null, "doc": "Mean PSF flux in F213 [nJy]"},
		{"name": "F213PsfFluxSigma",  "type": ["null", "float"], "default": null, "doc": "Std dev of PSF flux in F213 [nJy]"},
		{"name": "F213PsfFluxNdata",  "type": ["null", "int"],   "default": null, "doc": "Number of F213 measurements"},
		{"name": "F213PsfFluxMin",    "type": ["null", "float"], "default": null, "doc": "Minimum PSF flux in F213 [nJy]"},
		{"name": "F213PsfFluxMax",    "type": ["null", "float"], "default": null, "doc": "Maximum PSF flux in F213 [nJy]"}
	]
}
EOF

# --- ssSource (stub) ---
cat << 'EOF' > "${OUTDIR}/rapid.v01_00.ssSource.avsc"
{
	"namespace": "rapid.v01_00",
	"name": "ssSource",
	"doc": "RAPID alert schema: solar system source association (stub)",
	"version": "01.00",
	"type": "record",
	"fields": [
		{"name": "ssSourceId",    "type": "long",              "doc": "Unique identifier for this solar system source"},
		{"name": "diaSourceId",   "type": "long",              "doc": "Associated diaSource identifier"},
		{"name": "ssObjectId",    "type": ["null", "long"],    "default": null, "doc": "Associated solar system object identifier"},
		{"name": "heliocentricX", "type": ["null", "double"],  "default": null, "doc": "Heliocentric x position [AU]"},
		{"name": "heliocentricY", "type": ["null", "double"],  "default": null, "doc": "Heliocentric y position [AU]"},
		{"name": "heliocentricZ", "type": ["null", "double"],  "default": null, "doc": "Heliocentric z position [AU]"},
		{"name": "phaseAngle",    "type": ["null", "float"],   "default": null, "doc": "Phase angle [deg]"},
		{"name": "heliocentricDist", "type": ["null", "float"],"default": null, "doc": "Heliocentric distance [AU]"},
		{"name": "topocentricDist", "type": ["null", "float"], "default": null, "doc": "Topocentric distance [AU]"}
	]
}
EOF

# --- mpc_orbits (stub) ---
cat << 'EOF' > "${OUTDIR}/rapid.v01_00.mpc_orbits.avsc"
{
	"namespace": "rapid.v01_00",
	"name": "mpc_orbits",
	"doc": "RAPID alert schema: MPC orbital elements (stub)",
	"version": "01.00",
	"type": "record",
	"fields": [
		{"name": "id",            "type": "string",             "doc": "MPC designation or packed designation"},
		{"name": "a",             "type": ["null", "double"],   "default": null, "doc": "Semi-major axis [AU]"},
		{"name": "e",             "type": ["null", "double"],   "default": null, "doc": "Eccentricity"},
		{"name": "incl",          "type": ["null", "double"],   "default": null, "doc": "Inclination [deg]"},
		{"name": "Omega",         "type": ["null", "double"],   "default": null, "doc": "Longitude of ascending node [deg]"},
		{"name": "omega",         "type": ["null", "double"],   "default": null, "doc": "Argument of perihelion [deg]"},
		{"name": "M",             "type": ["null", "double"],   "default": null, "doc": "Mean anomaly [deg]"},
		{"name": "epoch",         "type": ["null", "double"],   "default": null, "doc": "Epoch of orbital elements [MJD]"},
		{"name": "H",             "type": ["null", "float"],    "default": null, "doc": "Absolute magnitude [mag]"},
		{"name": "G",             "type": ["null", "float"],    "default": null, "doc": "Slope parameter"}
	]
}
EOF

# --- alert (top-level) ---
cat << 'EOF' > "${OUTDIR}/rapid.v01_00.alert.avsc"
{
	"namespace": "rapid.v01_00",
	"name": "alert",
	"doc": "RAPID alert schema: top-level alert record (LSST-compatible)",
	"version": "01.00",
	"type": "record",
	"fields": [
		{"name": "diaSourceId",        "type": "long",                                                                         "doc": "Identifier for the triggering diaSource"},
		{"name": "observation_reason",  "type": ["null", "string"],                                                            "default": null, "doc": "Reason for observation (e.g. survey, ToO)"},
		{"name": "target_name",         "type": ["null", "string"],                                                            "default": null, "doc": "Target name if targeted observation"},
		{"name": "diaSource",          "type": "rapid.v01_00.diaSource",                                                         "doc": "Triggering source detection"},
		{"name": "prvDiaSources",      "type": ["null", {"type": "array", "items": "rapid.v01_00.diaSource"}],                   "default": null, "doc": "Previous detections of the same object within 12 months"},
		{"name": "prvDiaForcedSources","type": ["null", {"type": "array", "items": "rapid.v01_00.diaForcedSource"}],              "default": null, "doc": "Forced photometry history at the object position"},
		{"name": "diaObject",          "type": ["null", "rapid.v01_00.diaObject"],                                               "default": null, "doc": "Summary object record"},
		{"name": "ssSource",           "type": ["null", "rapid.v01_00.ssSource"],                                                "default": null, "doc": "Solar system source association (stub)"},
		{"name": "mpc_orbits",         "type": ["null", "rapid.v01_00.mpc_orbits"],                                              "default": null, "doc": "MPC orbital elements (stub)"},
		{"name": "cutoutDifference",   "type": ["null", "bytes"],                                                              "default": null, "doc": "FITS cutout of difference image"},
		{"name": "cutoutScience",      "type": ["null", "bytes"],                                                              "default": null, "doc": "FITS cutout of science image"},
		{"name": "cutoutTemplate",     "type": ["null", "bytes"],                                                              "default": null, "doc": "FITS cutout of template image"}
	]
}
EOF

# --- Version pointer ---
echo "${VERSION}" > schema/latest.txt

echo "Schema v${VERSION} written to ${OUTDIR}/"
echo "Files:"
ls -1 "${OUTDIR}/"*.avsc
