"""SExtractor on the reference mosaic and the catalog's FWHM statistics.

`dev`: ``generateSExtractorReferenceImageCatalog``
(``pipeline/referenceImageSubs.py``) sets the detection/input/weight
images, the params, filter and star/galaxy files and the catalog name on
the ``[SEXTRACTOR_REFIMAGE]`` dictionary and runs ``sex``; the catalog is
the mosaic's name with ``image.fits`` replaced by ``refimsexcat.txt``.
The FWHM block is inline in
``awsBatchSubmitJobs_runSingleReferenceImagePipeline.py``: ``FWHM_IMAGE``
parsed with ``parse_ascii_text_sextractor_catalog``, its ``nanmin``,
``nanmax`` and ``nanmedian`` recorded, the source count the row count.

`dev` hard-codes ``/code/cdf/...`` for the three files; here they sit under
the ``[paths] cfg_path`` setting (default ``/code/cdf``), as the
difference stage's catalogs do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from rapidpipe.science.difference.sextractor import (
    build_sextractor_command_line_args,
    parse_ascii_text_sextractor_catalog,
)

logger = logging.getLogger(__name__)

PARAMS_FILE = "rapidSexParamsRefImage.inp"
FILTER_FILE = "rapidSexRefImageFilter.conv"
STARNNW_FILE = "rapidSexRefImageStarGalaxyClassifier.nnw"


def catalog_name_for(filename_refimage_image: str) -> str:
    """`dev`: ``filename_refimage_image.replace("image.fits", "refimsexcat.txt")``."""
    return filename_refimage_image.replace("image.fits", "refimsexcat.txt")


def sextractor_refimage_command(sextractor_refimage_dict: Mapping[str, Any], cfg_path: str,
                                filename_refimage_image: str, filename_refimage_uncert: str,
                                executable: str = "sex") -> tuple[list[str], str]:
    """The command `dev` runs and the catalog it names; ``(args, catalog)``."""
    filename_refimage_catalog = catalog_name_for(filename_refimage_image)
    d = {k: str(v) for k, v in sextractor_refimage_dict.items()}
    d["sextractor_detection_image"] = "None"
    d["sextractor_input_image"] = filename_refimage_image
    d["sextractor_weight_image"] = filename_refimage_uncert
    d["sextractor_parameters_name"] = cfg_path + "/" + PARAMS_FILE
    d["sextractor_filter_name"] = cfg_path + "/" + FILTER_FILE
    d["sextractor_starnnw_name"] = cfg_path + "/" + STARNNW_FILE
    d["sextractor_catalog_name"] = filename_refimage_catalog
    return build_sextractor_command_line_args(d, executable), filename_refimage_catalog


def generate_reference_image_catalog(runner, work_dir: Path,
                                     sextractor_refimage_dict: Mapping[str, Any], cfg_path: str,
                                     filename_refimage_image: str, filename_refimage_uncert: str,
                                     executable: str = "sex") -> tuple[str, int]:
    """`dev` ``generateSExtractorReferenceImageCatalog``; ``(catalog name, exit code)``."""
    args, catalog = sextractor_refimage_command(
        sextractor_refimage_dict, cfg_path, filename_refimage_image, filename_refimage_uncert,
        executable)
    exit_code = runner.run(args, cwd=work_dir)
    return catalog, int(exit_code)


@dataclass(frozen=True)
class FwhmStatistics:
    """The catalog measurements refimmeta records."""

    fwhmmedpix: float
    fwhmminpix: float
    fwhmmaxpix: float
    nsexcatsources: int
    fwhm_ref: float           # the median with `dev`'s 2.0 fallback; logged, not recorded


def fwhm_statistics(catalog_path: Path, params_path: str | Path) -> FwhmStatistics:
    """`dev`'s inline FWHM block over the reference catalog.

    An empty catalog makes ``nanmin`` raise :class:`ValueError`, as it does
    in `dev`; the stage reports that as its own error.
    """
    vals_refimage = parse_ascii_text_sextractor_catalog(catalog_path, params_path, ["FWHM_IMAGE"])
    nsexcatsources_refimage = len(vals_refimage)

    vals_fwhm = [float(val[0]) for val in vals_refimage]
    np_vals_fwhm = np.array(vals_fwhm)

    fwhm_ref_minpix = np.nanmin(np_vals_fwhm)
    fwhm_ref_maxpix = np.nanmax(np_vals_fwhm)
    fwhm_ref_medpix = np.nanmedian(np_vals_fwhm)
    logger.info("fwhm_ref_medpix,fwhm_ref_minpix,fwhm_ref_maxpix = %s %s %s",
                fwhm_ref_medpix, fwhm_ref_minpix, fwhm_ref_maxpix)

    fwhm_ref = fwhm_ref_medpix
    if np.isnan(fwhm_ref) or fwhm_ref < 0.0:
        fwhm_ref = 2.0
    logger.info("fwhm_ref = %s", fwhm_ref)

    return FwhmStatistics(
        fwhmmedpix=float(fwhm_ref_medpix), fwhmminpix=float(fwhm_ref_minpix),
        fwhmmaxpix=float(fwhm_ref_maxpix), nsexcatsources=int(nsexcatsources_refimage),
        fwhm_ref=float(fwhm_ref))
