# ported-from: modules/utils/rapid_pipeline_subs.py, pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py, pipeline/sfftDetectionSubs.py @ d6aac515
"""SExtractor: the command line, the catalogs the stage makes, parsing.

`dev`: ``build_sextractor_command_line_args``,
``parse_ascii_text_sextractor_catalog`` and ``generateScienceImageCatalog``
(``modules/utils/rapid_pipeline_subs.py``), and the difference-image
catalog blocks and FWHM statistics inline in
``awsBatchSubmitJobs_runSingleSciencePipeline.py``.

`dev` passes each option as the string its job configuration holds; the
settings tables (``[sextractor_sciimage]``, ``[sextractor_gainmatch]``,
``[sextractor_diffimage]``) carry those strings unchanged, keyed as
`dev`'s configparser keys them (lower case, ``sextractor_`` prefix). `dev`
mutates one shared dictionary per table across calls; each function here
takes a copy and sets the same keys `dev` sets before its call, which
gives the same command line because `dev` resets or reverts every key it
changes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from rapidpipe.science.difference import fwhm
from rapidpipe.science.difference.tools import ToolRunner

logger = logging.getLogger(__name__)

#: `dev`'s option order. FLAG_IMAGE and FLAG_TYPE are read from the
#: dictionary but omitted from the command line ("OMIT FLAGS" in `dev`).
_SEXTRACTOR_OPTIONS = (
    "CATALOG_NAME", "CATALOG_TYPE", "PARAMETERS_NAME", "DETECT_TYPE",
    "DETECT_MINAREA", "DETECT_MAXAREA", "THRESH_TYPE", "DETECT_THRESH",
    "ANALYSIS_THRESH", "FILTER", "FILTER_NAME", "FILTER_THRESH",
    "DEBLEND_NTHRESH", "DEBLEND_MINCONT", "CLEAN", "CLEAN_PARAM",
    "MASK_TYPE", "WEIGHT_TYPE", "RESCALE_WEIGHTS", "WEIGHT_IMAGE",
    "WEIGHT_GAIN", "WEIGHT_THRESH", "PHOT_APERTURES", "PHOT_AUTOPARAMS",
    "PHOT_PETROPARAMS", "PHOT_AUTOAPERS", "PHOT_FLUXFRAC", "SATUR_LEVEL",
    "SATUR_KEY", "MAG_ZEROPOINT", "MAG_GAMMA", "GAIN", "GAIN_KEY",
    "PIXEL_SCALE", "SEEING_FWHM", "STARNNW_NAME", "BACK_TYPE", "BACK_VALUE",
    "BACK_SIZE", "BACK_FILTERSIZE", "BACKPHOTO_TYPE", "BACKPHOTO_THICK",
    "BACK_FILTTHRESH", "CHECKIMAGE_TYPE", "CHECKIMAGE_NAME",
    "MEMORY_OBJSTACK", "MEMORY_PIXSTACK", "MEMORY_BUFSIZE", "ASSOC_NAME",
    "ASSOC_DATA", "ASSOC_PARAMS", "ASSOCCOORD_TYPE", "ASSOC_RADIUS",
    "ASSOC_TYPE", "ASSOCSELEC_TYPE", "VERBOSE_TYPE", "HEADER_SUFFIX",
    "WRITE_XML", "NTHREADS", "FITS_UNSIGNED", "INTERP_MAXXLAG",
    "INTERP_MAXYLAG", "INTERP_TYPE",
)
_READ_BUT_OMITTED = ("FLAG_IMAGE", "FLAG_TYPE")


def _key(option: str) -> str:
    return f"sextractor_{option}".lower()


def build_sextractor_command_line_args(
    sextractor_dict: Mapping[str, str], executable: str = "sex",
) -> list[str]:
    """`dev` ``build_sextractor_command_line_args``.

    The first positional argument is the input image, or
    ``detection,input`` when a detection image is given (the string
    ``"None"`` means none, as in `dev`).
    """
    detection_image = sextractor_dict["sextractor_detection_image"]
    input_image = sextractor_dict["sextractor_input_image"]
    for option in _READ_BUT_OMITTED:
        sextractor_dict[_key(option)]  # `dev` reads these; a missing key fails there too.

    args = [executable]
    if detection_image == "None":
        args.append(input_image)
    else:
        args.append(detection_image + "," + input_image)
    for option in _SEXTRACTOR_OPTIONS:
        args.append("-" + option)
        args.append(sextractor_dict[_key(option)])
    logger.info("code_to_execute_args = %s", args)
    return args


def parse_ascii_text_sextractor_catalog(
    catalog_filename, params_filename, params_to_parse: Sequence[str],
) -> list[list[str]]:
    """`dev` ``parse_ascii_text_sextractor_catalog``: selected columns as strings.

    Parameter names like ``FLUX_APER(6)`` are addressed as ``FLUX_APER_6``.
    """
    with open(params_filename, "r") as file:
        i = 0
        idx = {}
        for line in file:
            param = line.strip()
            param = param.replace("(", "_")
            param = param.replace(")", "")
            idx[param] = i
            i += 1

    rows = []
    with open(catalog_filename, "r") as file:
        for line in file:
            if re.match(r"^#(.+)", line):
                continue
            all_cols = line.strip().split()
            rows.append([all_cols[idx[p]] for p in params_to_parse])
    return rows


def fwhm_from_catalog(catalog_filename, params_filename, label: str) -> tuple[float, int]:
    """The image FWHM in arcseconds from its catalog, with `dev`'s fallback of 2 pixels.

    `dev` computes this inline twice (science and reference image): the
    point-source FWHM of :func:`rapidpipe.science.difference.fwhm.catalog_fwhm_stats`
    in pixels (dev 1822789e), times the WFI plate scale (dev 095c45d8,
    46fc651a), replaced by 2 pixels' worth when it is NaN or negative.
    Returns ``(fwhm_arcsec, nsources)``.
    """
    vals = parse_ascii_text_sextractor_catalog(
        catalog_filename, params_filename, fwhm.POINT_SOURCE_CATALOG_PARAMS)
    nsources = len(vals)
    fwhm_minpix, fwhm_maxpix, fwhm_medpix = fwhm.catalog_fwhm_stats(vals, label=label)
    logger.info(
        "fwhm_%s_medpix,fwhm_%s_minpix,fwhm_%s_maxpix = %s %s %s",
        label, label, label, fwhm_medpix, fwhm_minpix, fwhm_maxpix)

    value = fwhm_medpix * fwhm.ROMAN_WFI_PLATESCALE
    if np.isnan(value) or value < 0.0:
        value = 2.0 * fwhm.ROMAN_WFI_PLATESCALE
    logger.info("fwhm_%s = %s", label, value)
    return float(value), nsources


def generate_science_image_catalog(
    runner: ToolRunner,
    work_dir: Path,
    filename_sciimage_image: str,
    filename_sciimage_uncert: str,
    cfg_path: str,
    sextractor_sciimage_dict: Mapping[str, str],
    filename_sciimage_catalog: str,
    executable: str = "sex",
) -> None:
    """`dev` ``generateScienceImageCatalog``."""
    d = dict(sextractor_sciimage_dict)
    d["sextractor_detection_image"] = "None"
    d["sextractor_input_image"] = filename_sciimage_image
    d["sextractor_weight_image"] = filename_sciimage_uncert
    d["sextractor_parameters_name"] = cfg_path + "/rapidSexParamsSciImage.inp"
    d["sextractor_filter_name"] = cfg_path + "/rapidSexSciImageFilter.conv"
    d["sextractor_starnnw_name"] = cfg_path + "/rapidSexSciImageStarGalaxyClassifier.nnw"
    d["sextractor_catalog_name"] = filename_sciimage_catalog
    runner.run(build_sextractor_command_line_args(d, executable), cwd=work_dir)


#: The catalog columns `dev` parses from every difference-image catalog.
DIFFIMAGE_PARAMS = ("XWIN_IMAGE", "YWIN_IMAGE", "FLUX_APER_6")


def difference_image_catalog(
    runner: ToolRunner,
    work_dir: Path,
    sextractor_diffimage_dict: Mapping[str, str],
    cfg_path: str,
    *,
    detection_image: str,
    input_image: str,
    weight_image: str,
    catalog_name: str,
    override_weight_type_and_filter: bool,
    overrides: Mapping[str, str] | None = None,
    executable: str = "sex",
) -> int:
    """One `dev` difference-image SExtractor block; returns the source count.

    ZOGY and SFFT catalogs override ``WEIGHT_TYPE`` to ``NONE,MAP_RMS`` and
    ``FILTER`` to ``N`` for the call; the naive catalogs do not, and run
    with the table's own values (`dev` reverts the overrides after each
    ZOGY/SFFT call, so the naive calls see the configured ones).
    ``overrides`` (`dev`'s ``[SEXTRACTOR_DIFFIMAGE_SFFT]`` or
    ``[SEXTRACTOR_DIFFIMAGE_NAIVE]``, as :func:`detection_overrides` gives
    them) are laid over last.
    """
    params_file = cfg_path + "/rapidSexParamsDiffImage.inp"
    d = dict(sextractor_diffimage_dict)
    d["sextractor_detection_image"] = detection_image
    d["sextractor_input_image"] = input_image
    if override_weight_type_and_filter:
        d["sextractor_weight_type"] = "NONE,MAP_RMS"
    d["sextractor_weight_image"] = weight_image
    d["sextractor_parameters_name"] = params_file
    if override_weight_type_and_filter:
        d["sextractor_filter"] = "N"
    d["sextractor_filter_name"] = cfg_path + "/rapidSexDiffImageFilter.conv"
    d["sextractor_starnnw_name"] = cfg_path + "/rapidSexDiffImageStarGalaxyClassifier.nnw"
    d["sextractor_catalog_name"] = catalog_name
    d.update(overrides or {})
    runner.run(build_sextractor_command_line_args(d, executable), cwd=work_dir)

    vals = parse_ascii_text_sextractor_catalog(
        work_dir / catalog_name, params_file, list(DIFFIMAGE_PARAMS))
    return len(vals)


def detection_overrides(override_table: Mapping[str, str] | None, crossconv_flag: bool,
                        cfg_path: str) -> dict[str, str]:
    """`dev` ``sfftDetectionSubs.sfft_sextractor_dict``'s overrides, as a dictionary.

    The keys of ``override_table`` to lay over ``[sextractor_diffimage]`` for
    the SFFT (or naive) passes: none when the table is empty, or when SFFT
    cross-convolution is on (detection then runs on an image that is
    already matched-filtered). A relative ``sextractor_filter_name`` is a
    file in ``cfg_path``.
    """
    if not override_table or crossconv_flag:
        return {}
    merged = {}
    for key, value in override_table.items():
        if key == "sextractor_filter_name" and not value.startswith("/"):
            value = cfg_path + "/" + value
        merged[key] = value
    return merged
