#!/usr/bin/env bash

# Writes all .avsc schema files using heredocs
# Uses variables for consistent entires
# Widescreen format (>>80 characters!)
# Reference alert schemas:
#   ZTF
#     https://github.com/Caltech-IPAC/ztf
#     src/pl/avroalerts/schema/combined
#   Rubin
#     https://github.com/lsst/alert_packet
#     python/lsst/alert/packet 

# Strict error handling
set -euo pipefail
IFS=$'\n\t'

# Schema version
VERSION=X.Y
echo Generating RAPID avro alert schema v${VERSION}

# Top record
cat << EOF > alert.avsc
{
	"namespace": "rapid",
	"name": "alert",
	"doc": "RAPID avro alert schema",
        "version": "${VERSION}",
        "type": "record",
	"fields": [
		{"name": "alertId",          "type": "string",                                                          "doc":     "unique alert identifer"},
                {"name": "candidate",        "type": "rapid.alert.candidate",                                           "default": null},
                {"name": "prv_candidates",   "type": [{"type": "array","items": "rapid.alert.prv_candidate"}, "null" ], "default": null},
                {"name": "fp_hists",         "type": [{"type": "array","items": "rapid.alert.fp_hist"},       "null" ], "default": null},
		{"name": "cutoutScience",    "type": ["rapid.alert.cutout", "null"],                                    "default": null},
		{"name": "cutoutTemplate",   "type": ["rapid.alert.cutout", "null"],                                    "default": null},
		{"name": "cutoutDifference", "type": ["rapid.alert.cutout", "null"],                                    "default": null}
     ]
}
EOF


# Candidate information
cat << EOF > candidate.avsc
{
	"namespace": "rapid.alert",
	"name": "candidate",
	"doc": "RAPID avro alert schema",
	"version": "${VERSION}",
	"type": "record",
	"fields": [
		{"name": "jd", "type": "double", "doc": "Observation Julian date at start of exposure [days]"},
		{"name": "fid", "type": "int", "doc": "Filter ID (1=g; 2=R; 3=i)"},
		{"name": "pid", "type": "long", "doc": "Processing ID for science image to facilitate archive retrieval"},
		{"name": "diffmaglim", "type": ["null", "float"], "default": null, "doc": "Expected 5-sigma mag limit in difference image based on global noise estimate [mag]"},
		{"name": "pdiffimfilename", "type": ["null", "string"], "default": null, "doc": "filename of positive (sci minus ref) difference image"},
		{"name": "candid", "type": "long", "doc": "Candidate ID from operations DB"},
		{"name": "isdiffpos", "type": "string", "doc": "t or 1 => candidate is from positive (sci minus ref) subtraction; f or 0 => candidate is from negative (ref minus sci) subtraction"},
		{"name": "tblid", "type": ["null", "long"], "default": null, "doc": "Internal pipeline table extraction ID"},
		{"name": "nid", "type": ["null", "int"], "default": null, "doc": "Night ID"},
		{"name": "xpos", "type": ["null", "float"], "default": null, "doc": "x-image position of candidate [pixels]"},
		{"name": "ypos", "type": ["null", "float"], "default": null, "doc": "y-image position of candidate [pixels]"},
		{"name": "ra", "type": "double", "doc": "Right Ascension of candidate; J2000 [deg]"},
		{"name": "dec", "type": "double", "doc": "Declination of candidate; J2000 [deg]"},
		{"name": "magpsf", "type": "float", "doc": "Magnitude from PSF-fit photometry [mag]"},
		{"name": "sigmapsf", "type": "float", "doc": "1-sigma uncertainty in magpsf [mag]"},
		{"name": "chipsf", "type": ["null", "float"], "default": null, "doc": "Reduced chi-square for PSF-fit"},
		{"name": "magap", "type": ["null", "float"], "default": null, "doc": "Aperture mag using 14 pixel diameter aperture [mag]"},
		{"name": "sigmagap", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magap [mag]"},
		{"name": "distnr", "type": ["null", "float"], "default": null, "doc": "distance to nearest source in reference image PSF-catalog [pixels]"},
		{"name": "magnr", "type": ["null", "float"], "default": null, "doc": "magnitude of nearest source in reference image PSF-catalog [mag]"},
		{"name": "sigmagnr", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magnr [mag]"},
		{"name": "chinr", "type": ["null", "float"], "default": null, "doc": "DAOPhot chi parameter of nearest source in reference image PSF-catalog"},
		{"name": "sharpnr", "type": ["null", "float"], "default": null, "doc": "DAOPhot sharp parameter of nearest source in reference image PSF-catalog"},
		{"name": "sky", "type": ["null", "float"], "default": null, "doc": "Local sky background estimate [DN]"},
		{"name": "magdiff", "type": ["null", "float"], "default": null, "doc": "Difference: magap - magpsf [mag]"},
		{"name": "fwhm", "type": ["null", "float"], "default": null, "doc": "Full Width Half Max assuming a Gaussian core, from SExtractor [pixels]"},
		{"name": "classtar", "type": ["null", "float"], "default": null, "doc": "Star/Galaxy classification score from SExtractor"},
		{"name": "mindtoedge", "type": ["null", "float"], "default": null, "doc": "Distance to nearest edge in image [pixels]"},
		{"name": "magfromlim", "type": ["null", "float"], "default": null, "doc": "Difference: diffmaglim - magap [mag]"},
		{"name": "seeratio", "type": ["null", "float"], "default": null, "doc": "Ratio: difffwhm / fwhm"},
		{"name": "aimage", "type": ["null", "float"], "default": null, "doc": "Windowed profile RMS afloat major axis from SExtractor [pixels]"},
		{"name": "bimage", "type": ["null", "float"], "default": null, "doc": "Windowed profile RMS afloat minor axis from SExtractor [pixels]"},
		{"name": "aimagerat", "type": ["null", "float"], "default": null, "doc": "Ratio: aimage / fwhm"},
		{"name": "bimagerat", "type": ["null", "float"], "default": null, "doc": "Ratio: bimage / fwhm"},
		{"name": "elong", "type": ["null", "float"], "default": null, "doc": "Ratio: aimage / bimage"},
		{"name": "nneg", "type": ["null", "int"], "default": null, "doc": "number of negative pixels in a 5 x 5 pixel stamp"},
		{"name": "nbad", "type": ["null", "int"], "default": null, "doc": "number of prior-tagged bad pixels in a 5 x 5 pixel stamp"},
		{"name": "rb", "type": ["null", "float"], "default": null, "doc": "RealBogus quality score from Random Forest classifier; range is 0 to 1 where closer to 1 is more reliable"},
		{"name": "ssdistnr", "type": ["null", "float"], "default": null, "doc": "distance to nearest known solar system object if exists within 30 arcsec [arcsec]"},
		{"name": "ssmagnr", "type": ["null", "float"], "default": null, "doc": "magnitude of nearest known solar system object if exists within 30 arcsec (usually V-band from MPC archive) [mag]"},
		{"name": "ssnamenr", "type": ["null", "string"], "default": null, "doc": "name of nearest known solar system object if exists within 30 arcsec (from MPC archive)"},
		{"name": "sumrat", "type": ["null", "float"], "default": null, "doc": "Ratio: sum(pixels) / sum(|pixels|) in a 5 x 5 pixel stamp where stamp is first median-filtered to mitigate outliers"},
		{"name": "magapbig", "type": ["null", "float"], "default": null, "doc": "Aperture mag using 18 pixel diameter aperture [mag]"},
		{"name": "sigmagapbig", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magapbig [mag]"},
		{"name": "ranr", "type": "double", "doc": "Right Ascension of nearest source in reference image PSF-catalog; J2000 [deg]"},
		{"name": "decnr", "type": "double", "doc": "Declination of nearest source in reference image PSF-catalog; J2000 [deg]"},
		{"name": "sgmag1", "type": ["null", "float"], "default": null, "doc": "g-band PSF-fit magnitude of closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "srmag1", "type": ["null", "float"], "default": null, "doc": "r-band PSF-fit magnitude of closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "simag1", "type": ["null", "float"], "default": null, "doc": "i-band PSF-fit magnitude of closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "szmag1", "type": ["null", "float"], "default": null, "doc": "z-band PSF-fit magnitude of closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "sgscore1", "type": ["null", "float"], "default": null, "doc": "Star/Galaxy score of closest source from PS1 catalog; if exists within 30 arcsec: 0 <= sgscore <= 1 where closer to 1 implies higher likelihood of being a star"},
		{"name": "distpsnr1", "type": ["null", "float"], "default": null, "doc": "Distance to closest source from PS1 catalog; if exists within 30 arcsec [arcsec]"},
		{"name": "ndethist", "type": "int", "doc": "Number of spatially-coincident detections falling within 1.5 arcsec going back to beginning of survey; only detections that fell on the same field and readout-channel ID where the input candidate was observed are counted. All raw detections down to a photometric S/N of ~ 3 are included."},
		{"name": "ncovhist", "type": "int", "doc": "Number of times input candidate position fell on any field and readout-channel going back to beginning of survey"},
		{"name": "jdstarthist", "type": ["null", "double"], "default": null, "doc": "Earliest Julian date of epoch corresponding to ndethist [days]"},
		{"name": "jdendhist", "type": ["null", "double"], "default": null, "doc": "Latest Julian date of epoch corresponding to ndethist [days]"},
		{"name": "scorr", "type": ["null", "double"], "default": null, "doc": "Peak-pixel signal-to-noise ratio in point source matched-filtered detection image"},
		{"name": "objectidps1", "type": ["null", "long"], "default": null, "doc": "Object ID of closest source from PS1 catalog; if exists within 30 arcsec"},
		{"name": "objectidps2", "type": ["null", "long"], "default": null, "doc": "Object ID of second closest source from PS1 catalog; if exists within 30 arcsec"},
		{"name": "sgmag2", "type": ["null", "float"], "default": null, "doc": "g-band PSF-fit magnitude of second closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "srmag2", "type": ["null", "float"], "default": null, "doc": "r-band PSF-fit magnitude of second closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "simag2", "type": ["null", "float"], "default": null, "doc": "i-band PSF-fit magnitude of second closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "szmag2", "type": ["null", "float"], "default": null, "doc": "z-band PSF-fit magnitude of second closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "sgscore2", "type": ["null", "float"], "default": null, "doc": "Star/Galaxy score of second closest source from PS1 catalog; if exists within 30 arcsec: 0 <= sgscore <= 1 where closer to 1 implies higher likelihood of being a star"},
		{"name": "distpsnr2", "type": ["null", "float"], "default": null, "doc": "Distance to second closest source from PS1 catalog; if exists within 30 arcsec [arcsec]"},
		{"name": "objectidps3", "type": ["null", "long"], "default": null, "doc": "Object ID of third closest source from PS1 catalog; if exists within 30 arcsec"},
		{"name": "sgmag3", "type": ["null", "float"], "default": null, "doc": "g-band PSF-fit magnitude of third closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "srmag3", "type": ["null", "float"], "default": null, "doc": "r-band PSF-fit magnitude of third closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "simag3", "type": ["null", "float"], "default": null, "doc": "i-band PSF-fit magnitude of third closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "szmag3", "type": ["null", "float"], "default": null, "doc": "z-band PSF-fit magnitude of third closest source from PS1 catalog; if exists within 30 arcsec [mag]"},
		{"name": "sgscore3", "type": ["null", "float"], "default": null, "doc": "Star/Galaxy score of third closest source from PS1 catalog; if exists within 30 arcsec: 0 <= sgscore <= 1 where closer to 1 implies higher likelihood of being a star"},
		{"name": "distpsnr3", "type": ["null", "float"], "default": null, "doc": "Distance to third closest source from PS1 catalog; if exists within 30 arcsec [arcsec]"},
		{"name": "nmtchps", "type": "int", "doc": "Number of source matches from PS1 catalog falling within 30 arcsec"},
		{"name": "rfid", "type": "long", "doc": "Processing ID for reference image to facilitate archive retrieval"},
		{"name": "jdstartref", "type": "double", "doc": "Observation Julian date of earliest exposure used to generate reference image [days]"},
		{"name": "jdendref", "type": "double", "doc": "Observation Julian date of latest exposure used to generate reference image [days]"},
		{"name": "nframesref", "type": "int", "doc": "Number of frames (epochal images) used to generate reference image"},
		{"name": "rbversion", "type": "string", "doc": "version of Random Forest classifier model used to assign RealBogus (rb) quality score"},
		{"name": "dsnrms", "type": ["null", "float"], "default": null, "doc": "Ratio: D/stddev(D) on event position where D = difference image"},
		{"name": "ssnrms", "type": ["null", "float"], "default": null, "doc": "Ratio: S/stddev(S) on event position where S = image of convolution: D (x) PSF(D)"},
		{"name": "dsdiff", "type": ["null", "float"], "default": null, "doc": "Difference of statistics: dsnrms - ssnrms"},
		{"name": "magzpsci", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point for photometry estimates [mag]"},
		{"name": "magzpsciunc", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point uncertainty (in magzpsci) [mag]"},
		{"name": "magzpscirms", "type": ["null", "float"], "default": null, "doc": "RMS (deviation from average) in all differences between instrumental photometry and matched photometric calibrators from science image processing [mag]"},
		{"name": "nmatches", "type": "int", "doc": "Number of PS1 photometric calibrators used to calibrate science image from science image processing"},
		{"name": "clrcoeff", "type": ["null", "float"], "default": null, "doc": "Color coefficient from linear fit from photometric calibration of science image"},
		{"name": "clrcounc", "type": ["null", "float"], "default": null, "doc": "Color coefficient uncertainty from linear fit (corresponding to clrcoeff)"},
		{"name": "zpclrcov", "type": ["null", "float"], "default": null, "doc": "Covariance in magzpsci and clrcoeff from science image processing [mag^2]"},
		{"name": "zpmed", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point from median of all differences between instrumental photometry and matched photometric calibrators from science image processing [mag]"},
		{"name": "clrmed", "type": ["null", "float"], "default": null, "doc": "Median color of all PS1 photometric calibrators used from science image processing [mag]: for filter (fid) = 1, 2, 3, PS1 color used = g-r, g-r, r-i respectively"},
		{"name": "clrrms", "type": ["null", "float"], "default": null, "doc": "RMS color (deviation from average) of all PS1 photometric calibrators used from science image processing [mag]"},
		{"name": "neargaia", "type": ["null", "float"], "default": null, "doc": "Distance to closest source from Gaia DR1 catalog irrespective of magnitude; if exists within 90 arcsec [arcsec]"},
		{"name": "neargaiabright", "type": ["null", "float"], "default": null, "doc": "Distance to closest source from Gaia DR1 catalog brighter than magnitude 14; if exists within 90 arcsec [arcsec]"},
		{"name": "maggaia", "type": ["null", "float"], "default": null, "doc": "Gaia (G-band) magnitude of closest source from Gaia DR1 catalog irrespective of magnitude; if exists within 90 arcsec [mag]"},
		{"name": "maggaiabright", "type": ["null", "float"], "default": null, "doc": "Gaia (G-band) magnitude of closest source from Gaia DR1 catalog brighter than magnitude 14; if exists within 90 arcsec [mag]"},
		{"name": "exptime", "type": ["null", "float"], "default": null, "doc": "Integration time of camera exposure [sec]"},
		{"name": "drb", "type": ["null", "float"], "default": null, "doc": "RealBogus quality score from Deep-Learning-based classifier; range is 0 to 1 where closer to 1 is more reliable"},
		{"name": "drbversion", "type": "string", "doc": "version of Deep-Learning-based classifier model used to assign RealBogus (drb) quality score"}
			]
}
EOF

# Previous candidates
cat << EOF > prv_candidates.avsc
{
	"namespace": "rapid.alert",
	"name": "prv_candidate",
        "doc": "RAPID avro alert schema",
        "version": "${VERSION}",
	"type": "record",
	"fields": [
		{"name": "jd", "type": "double", "doc": "Observation Julian date at start of exposure [days]"},
		{"name": "pid", "type": "long", "doc": "Processing ID for image"},
		{"name": "diffmaglim", "type": ["null", "float"], "default": null, "doc": "Expected 5-sigma mag limit in difference image based on global noise estimate [mag]"},
		{"name": "pdiffimfilename", "type": ["null", "string"], "default": null, "doc": "filename of positive (sci minus ref) difference image"},
		{"name": "candid", "type": ["null", "long"], "doc": "Candidate ID from operations DB"},
		{"name": "isdiffpos", "type": ["null", "string"], "doc": "t or 1 => candidate is from positive (sci minus ref) subtraction; f or 0 => candidate is from negative (ref minus sci) subtraction"},
		{"name": "tblid", "type": ["null", "long"], "default": null, "doc": "Internal pipeline table extraction ID"},
		{"name": "nid", "type": ["null", "int"], "default": null, "doc": "Night ID"},
		{"name": "xpos", "type": ["null", "float"], "default": null, "doc": "x-image position of candidate [pixels]"},
		{"name": "ypos", "type": ["null", "float"], "default": null, "doc": "y-image position of candidate [pixels]"},
		{"name": "ra", "type": ["null", "double"], "doc": "Right Ascension of candidate; J2000 [deg]"},
		{"name": "dec", "type": ["null", "double"], "doc": "Declination of candidate; J2000 [deg]"},
		{"name": "magpsf", "type": ["null", "float"], "doc": "Magnitude from PSF-fit photometry [mag]"},
		{"name": "sigmapsf", "type": ["null", "float"], "doc": "1-sigma uncertainty in magpsf [mag]"},
		{"name": "chipsf", "type": ["null", "float"], "default": null, "doc": "Reduced chi-square for PSF-fit"},
		{"name": "magap", "type": ["null", "float"], "default": null, "doc": "Aperture mag using 14 pixel diameter aperture [mag]"},
		{"name": "sigmagap", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magap [mag]"},
		{"name": "distnr", "type": ["null", "float"], "default": null, "doc": "distance to nearest source in reference image PSF-catalog [pixels]"},
		{"name": "magnr", "type": ["null", "float"], "default": null, "doc": "magnitude of nearest source in reference image PSF-catalog [mag]"},
		{"name": "sigmagnr", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magnr [mag]"},
		{"name": "chinr", "type": ["null", "float"], "default": null, "doc": "DAOPhot chi parameter of nearest source in reference image PSF-catalog"},
		{"name": "sharpnr", "type": ["null", "float"], "default": null, "doc": "DAOPhot sharp parameter of nearest source in reference image PSF-catalog"},
		{"name": "sky", "type": ["null", "float"], "default": null, "doc": "Local sky background estimate [DN]"},
		{"name": "magdiff", "type": ["null", "float"], "default": null, "doc": "Difference: magap - magpsf [mag]"},
		{"name": "fwhm", "type": ["null", "float"], "default": null, "doc": "Full Width Half Max assuming a Gaussian core, from SExtractor [pixels]"},
		{"name": "classtar", "type": ["null", "float"], "default": null, "doc": "Star/Galaxy classification score from SExtractor"},
		{"name": "mindtoedge", "type": ["null", "float"], "default": null, "doc": "Distance to nearest edge in image [pixels]"},
		{"name": "magfromlim", "type": ["null", "float"], "default": null, "doc": "Difference: diffmaglim - magap [mag]"},
		{"name": "seeratio", "type": ["null", "float"], "default": null, "doc": "Ratio: difffwhm / fwhm"},
		{"name": "aimage", "type": ["null", "float"], "default": null, "doc": "Windowed profile RMS afloat major axis from SExtractor [pixels]"},
		{"name": "bimage", "type": ["null", "float"], "default": null, "doc": "Windowed profile RMS afloat minor axis from SExtractor [pixels]"},
		{"name": "aimagerat", "type": ["null", "float"], "default": null, "doc": "Ratio: aimage / fwhm"},
		{"name": "bimagerat", "type": ["null", "float"], "default": null, "doc": "Ratio: bimage / fwhm"},
		{"name": "elong", "type": ["null", "float"], "default": null, "doc": "Ratio: aimage / bimage"},
		{"name": "nneg", "type": ["null", "int"], "default": null, "doc": "number of negative pixels in a 5 x 5 pixel stamp"},
		{"name": "nbad", "type": ["null", "int"], "default": null, "doc": "number of prior-tagged bad pixels in a 5 x 5 pixel stamp"},
		{"name": "rb", "type": ["null", "float"], "default": null, "doc": "RealBogus quality score; range is 0 to 1 where closer to 1 is more reliable"},
		{"name": "ssdistnr", "type": ["null", "float"], "default": null, "doc": "distance to nearest known solar system object if exists within 30 arcsec [arcsec]"},
		{"name": "ssmagnr", "type": ["null", "float"], "default": null, "doc": "magnitude of nearest known solar system object if exists within 30 arcsec (usually V-band from MPC archive) [mag]"},
		{"name": "ssnamenr", "type": ["null", "string"], "default": null, "doc": "name of nearest known solar system object if exists within 30 arcsec (from MPC archive)"},
		{"name": "sumrat", "type": ["null", "float"], "default": null, "doc": "Ratio: sum(pixels) / sum(|pixels|) in a 5 x 5 pixel stamp where stamp is first median-filtered to mitigate outliers"},
		{"name": "magapbig", "type": ["null", "float"], "default": null, "doc": "Aperture mag using 18 pixel diameter aperture [mag]"},
		{"name": "sigmagapbig", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magapbig [mag]"},
		{"name": "ranr", "type": ["null", "double"], "doc": "Right Ascension of nearest source in reference image PSF-catalog; J2000 [deg]"},
		{"name": "decnr", "type": ["null", "double"], "doc": "Declination of nearest source in reference image PSF-catalog; J2000 [deg]"},
		{"name": "scorr", "type": ["null", "double"], "default": null, "doc": "Peak-pixel signal-to-noise ratio in point source matched-filtered detection image"},
		{"name": "magzpsci", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point for photometry estimates [mag]"},
		{"name": "magzpsciunc", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point uncertainty (in magzpsci) [mag]"},
		{"name": "magzpscirms", "type": ["null", "float"], "default": null, "doc": "RMS (deviation from average) in all differences between instrumental photometry and matched photometric calibrators from science image processing [mag]"},
		{"name": "clrcoeff", "type": ["null", "float"], "default": null, "doc": "Color coefficient from linear fit from photometric calibration of science image"},
		{"name": "clrcounc", "type": ["null", "float"], "default": null, "doc": "Color coefficient uncertainty from linear fit (corresponding to clrcoeff)"},
		{"name": "rbversion", "type": "string", "doc": "version of RealBogus model/classifier used to assign rb quality score"}
			]
}
EOF

# Forced-photometry history 
cat << EOF > fp_hist.avsc
{
	"namespace": "rapid.alert",
	"name": "fp_hist",
	"doc": "RAPID avro alert schema",
	"version": "${VERSION}",
	"type": "record",
	"fields": [
		{"name": "pid", "type": "long", "doc": "Processing ID for image"},
		{"name": "rfid", "type": "long", "doc": "Processing ID for reference image to facilitate archive retrieval"},
		{"name": "sciinpseeing", "type": ["null", "float"], "default": null, "doc": "Effective FWHM of sci image [pixels]"},
		{"name": "scibckgnd", "type": ["null", "float"], "default": null, "doc": "Background level in sci image [DN]"},
		{"name": "scisigpix", "type": ["null", "float"], "default": null, "doc": "Robust sigma per pixel in sci image [DN]"},
		{"name": "magzpsci", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point for photometry estimates [mag]"},
		{"name": "magzpsciunc", "type": ["null", "float"], "default": null, "doc": "Magnitude zero point uncertainty (in magzpsci) [mag]"},
		{"name": "magzpscirms", "type": ["null", "float"], "default": null, "doc": "RMS (deviation from average) in all differences between instrumental photometry and matched photometric calibrators from science image processing [mag]"},
		{"name": "clrcoeff", "type": ["null", "float"], "default": null, "doc": "Color coefficient from linear fit from photometric calibration of science image"},
		{"name": "clrcounc", "type": ["null", "float"], "default": null, "doc": "Color coefficient uncertainty from linear fit (corresponding to clrcoeff)"},
		{"name": "exptime", "type": ["null", "float"], "default": null, "doc": "Integration time of camera exposure [sec]"},
		{"name": "adpctdif1", "type": ["null", "float"], "default": null, "doc": "Full sci image astrometric RMS along R.A. with respect to Gaia1 [arcsec]"},
		{"name": "adpctdif2", "type": ["null", "float"], "default": null, "doc": "Full sci image astrometric RMS along Dec. with respect to Gaia1 [arcsec]"},
		{"name": "diffmaglim", "type": ["null", "float"], "default": null, "doc": "Expected 5-sigma mag limit in difference image based on global noise estimate [mag]"},
		{"name": "programid", "type": "int", "doc": "Program ID: encodes either public, collab, or caltech mode"},
		{"name": "jd", "type": "double", "doc": "Observation Julian date at start of exposure [days]"},
		{"name": "forcediffimflux", "type": ["null", "float"], "default": null, "doc": "Forced difference image PSF-fit flux [DN]"},
		{"name": "forcediffimfluxunc", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in forcediffimflux [DN]"},
		{"name": "procstatus", "type": ["null", "string"], "default": null, "doc": "Forced photometry processing status codes (0 => no warnings); see documentation"},
		{"name": "distnr", "type": ["null", "float"], "default": null, "doc": "distance to nearest source in reference image PSF-catalog [arcsec]"},
		{"name": "ranr", "type": "double", "doc": "Right Ascension of nearest source in reference image PSF-catalog; J2000 [deg]"},
		{"name": "decnr", "type": "double", "doc": "Declination of nearest source in reference image PSF-catalog; J2000 [deg]"},
		{"name": "magnr", "type": ["null", "float"], "default": null, "doc": "magnitude of nearest source in reference image PSF-catalog [mag]"},
		{"name": "sigmagnr", "type": ["null", "float"], "default": null, "doc": "1-sigma uncertainty in magnr [mag]"},
		{"name": "chinr", "type": ["null", "float"], "default": null, "doc": "DAOPhot chi parameter of nearest source in reference image PSF-catalog"},
		{"name": "sharpnr", "type": ["null", "float"], "default": null, "doc": "DAOPhot sharp parameter of nearest source in reference image PSF-catalog"}
			]
}
EOF

# Postage stamp cutouts
cat << EOF > cutout.avsc
{
	"namespace": "rapid.alert",
	"name": "cutout",
	"doc": "RAPID avro alert schema",
	"version": "${VERSION}",
        "type": "record",
	"fields": [
		{"name": "fileName", "type": "string"},
		{"name": "stampData", "type": "bytes", "doc": "fits.gz"}
			]
}
EOF
