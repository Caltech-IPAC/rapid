"""Gain matching of the science and resampled reference images.

`dev`: ``gainMatchScienceAndReferenceImages`` and ``SourceMatchRefSci``
(``pipeline/differenceImageSubs.py``). SExtractor catalogs of the
background-subtracted science image and the resampled reference are
matched; the median flux ratio of isolated, point-like, unsaturated
reference sources gives the scale factor, and the matched offsets give the
x and y RMS and median separations. When too few sources match, the scale
factor falls back to the zero points and the offsets to the configured
astrometric uncertainties (RMS) and 0.0 (medians).

`dev` hard-codes the SExtractor parameter, filter and classifier files
under ``/code/cdf``; here they sit under the stage's ``cfg_path`` setting,
whose default is that same directory. `dev`'s S3 upload of the two
catalogs is not ported (no S3 here); the catalogs stay in the work
directory, which the stage publishes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.sextractor import (
    build_sextractor_command_line_args,
    parse_ascii_text_sextractor_catalog,
)
from rapidpipe.science.difference.tools import ToolRunner

logger = logging.getLogger(__name__)

IAM = "Sub gainMatchScienceAndReferenceImages"


def gain_match_science_and_reference_images(
    runner: ToolRunner,
    work_dir: Path,
    filename_sci_image: str,
    filename_sci_uncert: str,
    filename_scigainmatchsexcat_catalog: str,
    filename_ref_image: str,
    filename_ref_uncert: str,
    filename_refgainmatchsexcat_catalog: str,
    zprefimg: float,
    gainmatch: Mapping[str, Any],
    sextractor_gainmatch_dict: Mapping[str, str],
    fwhm_sci: float,
    fwhm_ref: float,
    astrometric_uncert_x: float,
    astrometric_uncert_y: float,
    cfg_path: str,
    executable: str = "sex",
) -> tuple[float, float, float, float, float]:
    """`dev` ``gainMatchScienceAndReferenceImages``.

    Returns ``(scalefac, dxrmsfin, dyrmsfin, dxmedianfin, dymedianfin)``.
    ``gainmatch`` is the ``[gainmatch]`` settings table (typed values).
    """
    verbose = int(gainmatch["verbose"])

    params_file = cfg_path + "/rapidSexParamsGainMatch.inp"
    filter_conv_file = cfg_path + "/rapidSexGainMatchFilter.conv"
    classifier_nnw_file = cfg_path + "/rapidSexGainMatchStarGalaxyClassifier.nnw"
    params_to_get_vals_scicat = ["XWIN_IMAGE", "YWIN_IMAGE", "FLUX_APER_6"]
    params_to_get_vals_refcat = ["XWIN_IMAGE", "YWIN_IMAGE", "FLUX_APER_6", "MAG_APER_6",
                                 "CLASS_STAR", "ISOAREAF_IMAGE", "AWIN_WORLD", "BWIN_WORLD"]

    magrefthresmin = float(gainmatch["magrefthresmin"])
    magrefthresmax = float(gainmatch["magrefthresmax"])
    refexclbox = float(gainmatch["refexclbox"])
    refmagkeep = float(gainmatch["refmagkeep"])
    edgebuffer = int(gainmatch["edgebuffer"])
    numsrcgmatchmin = int(gainmatch["numsrcgmatchmin"])
    radscirefmatch = float(gainmatch["radscirefmatch"])
    min_class_star_thresh = float(gainmatch["min_class_star_thresh"])
    max_isoareaf_image_thresh = int(gainmatch["max_isoareaf_image_thresh"])
    max_awin_to_bwin_world_ratio_thresh = float(gainmatch["max_awin_to_bwin_world_ratio_thresh"])
    zero_point_sci_keyword = gainmatch["zero_point_sci_keyword"]

    with fits.open(work_dir / filename_sci_image) as hdul_sci:
        hdr_sci = hdul_sci[0].header
        naxis1 = hdr_sci["NAXIS1"]
        naxis2 = hdr_sci["NAXIS2"]
        magzpsci = hdr_sci[zero_point_sci_keyword]
    logger.info("magzpsci=%s", magzpsci)

    # The swarped reference's header is the science image's, so its zero
    # point comes from the configured reference-image zero point.
    magzpref = float(zprefimg)
    logger.info("magzpref=%s", magzpref)

    for image, uncert, catalog, fwhm in (
        (filename_sci_image, filename_sci_uncert, filename_scigainmatchsexcat_catalog, fwhm_sci),
        (filename_ref_image, filename_ref_uncert, filename_refgainmatchsexcat_catalog, fwhm_ref),
    ):
        d = dict(sextractor_gainmatch_dict)
        d["sextractor_detection_image"] = "None"
        d["sextractor_input_image"] = image
        d["sextractor_weight_image"] = uncert
        d["sextractor_parameters_name"] = params_file
        d["sextractor_filter_name"] = filter_conv_file
        d["sextractor_starnnw_name"] = classifier_nnw_file
        d["sextractor_catalog_name"] = catalog
        d["sextractor_seeing_fwhm"] = str(fwhm)
        runner.run(build_sextractor_command_line_args(d, executable), cwd=work_dir)

    sci_vals = parse_ascii_text_sextractor_catalog(
        work_dir / filename_scigainmatchsexcat_catalog, params_file, params_to_get_vals_scicat)
    ref_vals = parse_ascii_text_sextractor_catalog(
        work_dir / filename_refgainmatchsexcat_catalog, params_file, params_to_get_vals_refcat)

    num_rows_sci = len(sci_vals)
    sci_x_vals = [float(row[0]) for row in sci_vals]
    sci_y_vals = [float(row[1]) for row in sci_vals]
    sci_flux_vals = [float(row[2]) for row in sci_vals]

    nrefcat = len(ref_vals)
    xsci_val = np.array([float(row[0]) for row in ref_vals])
    ysci_val = np.array([float(row[1]) for row in ref_vals])
    magref_val = np.array([float(row[3]) for row in ref_vals])
    classstarref_val = np.array([float(row[4]) for row in ref_vals])
    isoareafimageref_val = np.array([float(row[5]) for row in ref_vals])
    awintobwinworldratioref_val = np.array(
        [float(row[6]) / float(row[7]) for row in ref_vals])

    magref_val += magzpref

    for i in range(len(magref_val)):
        logger.debug(
            "i,magref_val,classstarref_val,isoareafimageref_val,awintobwinworldratioref_val = "
            "%s,%s,%s,%s,%s", i, magref_val[i], classstarref_val[i],
            isoareafimageref_val[i], awintobwinworldratioref_val[i])

    # Filter the reference catalog to a subset of point sources.
    if verbose:
        logger.info("%s: filtering %s ref-image catalog sources to support gain-matching...",
                    IAM, nrefcat)

    idxref_val = np.where((magref_val >= magrefthresmin) & (magref_val <= magrefthresmax) &
                          (classstarref_val >= min_class_star_thresh) &
                          (isoareafimageref_val <= max_isoareaf_image_thresh) &
                          (awintobwinworldratioref_val <= max_awin_to_bwin_world_ratio_thresh))

    xref_val = xsci_val[idxref_val]
    yref_val = ysci_val[idxref_val]
    magreffilt_val = magref_val[idxref_val]
    fluxref_val = 10**(0.4 * (magzpref - magreffilt_val))
    nrefcat = len(xref_val)

    if verbose:
        logger.info("%s: number of ref-image catalog sources after filtering to support "
                    "gain-matching: %s", IAM, nrefcat)
    if nrefcat == 0:
        logger.warning("%s: === Warning: no ref-image catalog sources remain after filtering "
                       "to support gain-matching; continuing...", IAM)

    # Keep only isolated sources, brighter than refmagkeep, away from edges.
    xrefkeep = []
    yrefkeep = []
    fluxrefkeep = []

    idxkeep_val = np.array(range(nrefcat))
    nrefcatn = 0

    for i in range(nrefcat):
        mi = magreffilt_val[i]
        xi = xref_val[i]
        yi = yref_val[i]

        if (mi <= refmagkeep) and \
                (xi > edgebuffer) and \
                (xi < (naxis1 - edgebuffer - 1)) and \
                (yi > edgebuffer) and \
                (yi < (naxis2 - edgebuffer - 1)):

            fi = fluxref_val[i]

            idxkeepn_val = np.where(idxkeep_val != i)
            xrefn_val = xref_val[idxkeepn_val]
            yrefn_val = yref_val[idxkeepn_val]

            insidebox = np.where((np.abs(xrefn_val - xi) <= refexclbox) &
                                 (np.abs(yrefn_val - yi) <= refexclbox))

            if len(insidebox[0]) == 0:
                xrefkeep.append(xi)
                yrefkeep.append(yi)
                fluxrefkeep.append(fi)
                nrefcatn += 1

    if nrefcatn > 0:
        xrefkeep_val = np.array(xrefkeep)
        yrefkeep_val = np.array(yrefkeep)
        fluxrefkeep_val = np.array(fluxrefkeep)

    if verbose > 0:
        logger.info("%s: number of ref-image catalog sources retained following removal of "
                    "those with close neighbors for gain-matching: %s", IAM, nrefcatn)

    # Default: the image zero points.
    scalefac = 10**(0.4 * (magzpref - magzpsci))
    if verbose:
        logger.info("%s: default scale factor for gain-matching sci and ref images based on "
                    "global image MAGZP values = %s", IAM, scalefac)

    dxrmsfin = astrometric_uncert_x
    dyrmsfin = astrometric_uncert_y
    dxmedianfin = 0.0
    dymedianfin = 0.0

    if num_rows_sci > 0 and nrefcatn > 0:
        dscirefnear, fluxscinearest, nmtchsciref, dxrms, dyrms, dxmedian, dymedian = \
            source_match_ref_sci(sci_x_vals, sci_y_vals, xrefkeep_val, yrefkeep_val,
                                 sci_flux_vals, nrefcatn, radscirefmatch, verbose)

        dscirefnear_val = np.array(dscirefnear)
        fluxscinearest_val = np.array(fluxscinearest)

        if nmtchsciref >= numsrcgmatchmin:
            # Matched science fluxes are those != -999; dev selects > 0.
            idxgood_val = np.where(fluxscinearest_val > 0)
            medradsep = np.median(dscirefnear_val[idxgood_val])
            flxrat_val = fluxrefkeep_val[idxgood_val] / fluxscinearest_val[idxgood_val]
            scalefac = np.median(flxrat_val)

            dxrmsfin = dxrms
            dyrmsfin = dyrms
            dxmedianfin = dxmedian
            dymedianfin = dymedian

            if verbose:
                logger.info("%s: median separation of %s sci to filtered ref-catalog matches = "
                            "%s pixels; initial match radius was %s pixels",
                            IAM, nmtchsciref, medradsep, radscirefmatch)
                logger.info("%s: final scale factor for gain-matching sci and ref images based "
                            "on flux ratios = %s ", IAM, scalefac)
                logger.info("%s: final RMSs along axes to use: dxrms, dyrms = %s, %s pixels",
                            IAM, dxrmsfin, dyrmsfin)
        else:
            logger.warning("%s: === Warning: number of matched sci and (filtered) ref catalog "
                           "sources (nmtchsciref) is below threshold of %s", IAM, numsrcgmatchmin)
            logger.warning("%s: === Warning: gain-matching science and reference image pixels "
                           "using matched fluxes not possible; using their image-based MAGZP "
                           "values...", IAM)
            logger.warning("%s: === Warning: furthermore, assuming default RMSs for axial "
                           "separations (~ registration errors) of dxrms, dyrms = %s, %s pixels",
                           IAM, dxrmsfin, dyrmsfin)
    else:
        logger.warning("%s: === Warning: input number of sci or (filtered) ref catalog sources "
                       "is zero", IAM)
        logger.warning("%s: === Warning: gain-matching science and reference image pixels using "
                       "matched fluxes not possible; using their image-based MAGZP values...", IAM)
        logger.warning("%s: === Warning: furthermore, assuming default RMSs for axial separations "
                       "(~ registration errors) of dxrms, dyrms = %s, %s pixels",
                       IAM, dxrmsfin, dyrmsfin)

    return scalefac, dxrmsfin, dyrmsfin, dxmedianfin, dymedianfin


def source_match_ref_sci(xf_val, yf_val, xp_val, yp_val, fluxsci_val, nrefcat,
                         radscirefmatch, verbose):
    """`dev` ``SourceMatchRefSci``: nearest science match per reference source.

    Returns ``(mdnear, mfluxsci, nmtch, dxrms, dyrms, dxmedian, dymedian)``;
    unmatched entries of the first two are -999. Offsets are reference
    minus science. RMS and medians stay 0.0 below three matches.
    """
    radsq = radscirefmatch * radscirefmatch
    radaxis = radscirefmatch / np.sqrt(2.0)

    mdnear = []
    mdxnear = []
    mdynear = []
    mfluxsci = []

    nmtch = 0

    x_sci = np.array(xf_val)
    y_sci = np.array(yf_val)
    x_ref = np.array(xp_val)
    y_ref = np.array(yp_val)
    flux_sci = np.array(fluxsci_val)

    for i in range(nrefcat):
        mdnear.append(-999)
        mfluxsci.append(-999)

        dxi_val = np.where(np.abs(x_ref[i] - x_sci) <= radaxis)

        if len(dxi_val[0]) != 0:
            xfsub_val = x_sci[tuple(dxi_val)]
            yfsub_val = y_sci[tuple(dxi_val)]
            fluxscisub_val = flux_sci[tuple(dxi_val)]

            dx_val = x_ref[i] - xfsub_val
            dy_val = y_ref[i] - yfsub_val
            radsq_val = (dx_val * dx_val) + (dy_val * dy_val)

            idxmin = np.argmin(radsq_val)
            minradsq = radsq_val[idxmin]

            if minradsq <= radsq:
                mdnear[i] = np.sqrt(minradsq)
                mdxnear.append(dx_val[idxmin])
                mdynear.append(dy_val[idxmin])
                mfluxsci[i] = fluxscisub_val[idxmin]
                nmtch += 1

    dxrms = 0.0
    dyrms = 0.0
    dxmedian = 0.0
    dymedian = 0.0

    if nmtch >= 3:
        mdxnear_val = np.array(mdxnear)
        dxrms = np.sqrt(np.mean(mdxnear_val * mdxnear_val))
        dxmedian = np.median(mdxnear_val)

        mdynear_val = np.array(mdynear)
        dyrms = np.sqrt(np.mean(mdynear_val * mdynear_val))
        dymedian = np.median(mdynear_val)

    if verbose > 0:
        logger.info("Sub SourceMatchRefSci: SourceMatchRefSci: number of matches = %s", nmtch)
        logger.info("Sub SourceMatchRefSci: SourceMatchRefSci: DxRMS = %s pixels", dxrms)
        logger.info("Sub SourceMatchRefSci: SourceMatchRefSci: DyRMS = %s pixels", dyrms)

    return mdnear, mfluxsci, nmtch, dxrms, dyrms, dxmedian, dymedian
