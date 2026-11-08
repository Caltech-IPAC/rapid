# ported-from: pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py, pipeline/referenceBackgroundSubs.py @ d6aac515
"""Local background subtraction of the science image and the resampled reference.

`dev`: the bkgest blocks inline in
``awsBatchSubmitJobs_runSingleSciencePipeline.py`` and
``pipeline/referenceBackgroundSubs.py`` (dev 4350bb5d). The science image
(its PV-header copy) is background-subtracted. With ``[bkgest]
subtract_from_reference`` (dev's default since 4350bb5d, paired with
``[swarp] swarp_subtract_back = "N"``), the resampled reference is
background-subtracted in place by the same estimator, so the estimator's
bias in crowded fields and around bright stars is common-mode and cancels
in the difference. SWarp writes 0 where the reference has no coverage and
the estimator skips NaN but not 0, so those pixels are blanked to NaN for
the estimate and given back their SWarp values afterwards.

Branch ``exp/bkgest-photutils`` (an experiment, never merged): photutils
``Background2D`` in place of the bkgest binary, matched to bkgest's
``-c 3 -f 2`` as closely as the two allow. bkgest takes, at nodes every
``local_clippedmean_grid_spacing`` pixels (the last node on the far edge),
a ``local_clippedmean_input_window`` window mirrored at the edges, one
2.5-sigma clip about the median with sigma half the 15.9 to 84.1
percentile range, and the mean of what is kept; a node with more than
half its window invalid takes the global clipped mean; the map is
bilinear between nodes. Here: boxes of the grid spacing stretched to tile
the image evenly (4088 / 8 = 511), the same one-pass clip and scale, the
mean, no median filter, boxes over half masked excluded, a first-order
(bilinear) zoom between box centres. The input-window and the global
table (``-ot``, read by nothing) have no counterpart.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Mapping

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.tools import ToolRunner

logger = logging.getLogger(__name__)

#: bkgest's clip, in its scale (bkgest_compute_results.c's ``sigma``).
CLIP_SIGMA = 2.5
#: bkgest's local bad-pixel tolerance, percent of a window (``D_NBadPixLocTol``).
EXCLUDE_PERCENTILE = 50.0


def _percentile_scale(data, axis=None):
    """bkgest's ``computescale``: half the 15.9 to 84.1 percentile range, NaN skipped."""
    lo, hi = np.nanpercentile(data, [15.9, 84.1], axis=axis)
    return 0.5 * (hi - lo)


def _box(n_pixels: int, spacing: int) -> int:
    """The box nearest ``spacing`` that tiles ``n_pixels`` in whole boxes (no padded edge box)."""
    n_boxes = max(1, round(n_pixels / spacing))
    return math.ceil(n_pixels / n_boxes)


def background_map(data: np.ndarray, bkgest_dict: Mapping[str, str]) -> np.ndarray:
    """The local clipped-mean background of ``data`` (NaN and inf excluded), photutils' way."""
    from astropy.stats import SigmaClip
    from photutils.background import Background2D, BkgZoomInterpolator, MeanBackground

    spacing = int(bkgest_dict["local_clippedmean_grid_spacing"])
    box = (_box(data.shape[0], spacing), _box(data.shape[1], spacing))
    clip = SigmaClip(sigma=CLIP_SIGMA, maxiters=1, cenfunc="median", stdfunc=_percentile_scale)
    bkg = Background2D(np.asarray(data, dtype=np.float64), box, filter_size=(1, 1),
                       exclude_percentile=EXCLUDE_PERCENTILE, sigma_clip=clip,
                       bkg_estimator=MeanBackground(sigma_clip=None),
                       interpolator=BkgZoomInterpolator(order=1))
    return bkg.background


def _subtract(work_dir: Path, input_image: str, output: str, bkgest_dict: Mapping[str, str]) -> None:
    """Write ``input_image`` minus its background as ``output``, NaN where the input is not finite."""
    with fits.open(work_dir / input_image) as hdul:
        data = np.asarray(hdul[0].data)
        header = hdul[0].header.copy()
    subtracted = data - background_map(data, bkgest_dict)
    subtracted[~np.isfinite(data)] = np.nan
    fits.writeto(work_dir / output, subtracted.astype(data.dtype), header, overwrite=True)


def subtract_background(
    runner: ToolRunner,
    work_dir: Path,
    bkgest_code: str,
    bkgest_include_dir: str,
    input_image: str,
    bkgest_dict: Mapping[str, str],
) -> str:
    """Background-subtract the science image; return the subtracted image's name.

    ``runner``, ``bkgest_code`` and ``bkgest_include_dir`` are the bkgest
    call's, kept so the stage is unchanged; photutils needs none of them.
    """
    output = bkgest_dict["filename_bkg_subbed_science_image"]
    _subtract(work_dir, input_image, output, bkgest_dict)
    return output


def uncovered_pixels(cov_map_file: Path) -> np.ndarray:
    """True where the resampled reference coverage map is not positive."""
    cov = fits.getdata(cov_map_file, 0)
    return ~(np.isfinite(cov) & (cov > 0))


def subtract_reference_background(
    runner: ToolRunner,
    work_dir: Path,
    bkgest_code: str,
    bkgest_include_dir: str,
    reference_image: str,
    reference_cov_map: str,
    bkgest_dict: Mapping[str, str],
) -> int:
    """Background-subtract the resampled reference in place; return the uncovered pixel count.

    `dev`'s block: the uncovered pixels excluded from the estimate and given
    back their original values, the header marked ``REFBKGSB``.
    """
    uncovered = uncovered_pixels(work_dir / reference_cov_map)
    with fits.open(work_dir / reference_image) as hdul:
        original = np.array(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    if original.shape != uncovered.shape:
        raise ValueError(f"reference image shape {original.shape} does not match coverage map "
                         f"shape {uncovered.shape}")
    blanked = original.copy()
    blanked[uncovered] = np.nan
    data = (blanked - background_map(blanked, bkgest_dict)).astype(np.float32)
    data[~np.isfinite(blanked)] = np.nan
    data[uncovered] = original[uncovered]
    header["REFBKGSB"] = (True, "background subtracted with the science estimator")
    fits.writeto(work_dir / reference_image, data, header, overwrite=True)
    count = int(np.count_nonzero(uncovered))
    logger.info("Resampled reference pixels without coverage, excluded from the background = %d",
                count)
    return count
