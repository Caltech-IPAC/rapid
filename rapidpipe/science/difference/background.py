# ported-from: pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py, pipeline/referenceBackgroundSubs.py @ d6aac515
"""bkgest: local background subtraction of the science image and the resampled reference.

`dev`: the bkgest blocks inline in
``awsBatchSubmitJobs_runSingleSciencePipeline.py`` and
``pipeline/referenceBackgroundSubs.py`` (dev 4350bb5d). The science image
(its PV-header copy) is background-subtracted. With ``[bkgest]
subtract_from_reference`` (dev's default since 4350bb5d, paired with
``[swarp] swarp_subtract_back = "N"``), the resampled reference is
background-subtracted in place by the same estimator, so the estimator's
bias in crowded fields and around bright stars is common-mode and cancels
in the difference. SWarp writes 0 where the reference has no coverage and
bkgest skips NaN but not 0, so those pixels are blanked to NaN for the
bkgest run and given back their SWarp values afterwards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.tools import ToolRunner

logger = logging.getLogger(__name__)


def build_bkgest_command(
    bkgest_code: str,
    bkgest_include_dir: str,
    input_image: str,
    bkgest_dict: Mapping[str, str],
    *,
    table: str | None = None,
    output: str | None = None,
) -> list[str]:
    """`dev`'s bkgest argument list; option values are the configured strings.

    ``table`` and ``output`` default to the science image's names.
    """
    return [bkgest_code,
            "-i", input_image,
            "-f", bkgest_dict["output_image_type"],
            "-c", bkgest_dict["clippedmean_calc_type"],
            "-g", bkgest_dict["local_clippedmean_grid_spacing"],
            "-w", bkgest_dict["local_clippedmean_input_window"],
            "-a", bkgest_include_dir,
            "-ot", table or bkgest_dict["filename_global_clippedmean_sciimage_tbl"],
            "-o2", output or bkgest_dict["filename_bkg_subbed_science_image"]]


def subtract_background(
    runner: ToolRunner,
    work_dir: Path,
    bkgest_code: str,
    bkgest_include_dir: str,
    input_image: str,
    bkgest_dict: Mapping[str, str],
) -> str:
    """Run bkgest; return the background-subtracted image's name."""
    runner.run(
        build_bkgest_command(bkgest_code, bkgest_include_dir, input_image, bkgest_dict),
        cwd=work_dir)
    return bkgest_dict["filename_bkg_subbed_science_image"]


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

    `dev`'s block: the uncovered pixels set to NaN in a copy, bkgest on the
    copy, the uncovered pixels given back their original values, the
    header marked ``REFBKGSB``. A bkgest that writes nothing fails the job,
    as in `dev`.
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
    bkgest_input = reference_image.replace(".fits", "_bkgest_input.fits")
    bkgest_output = reference_image.replace(".fits", "_bkgest_output.fits")
    fits.writeto(work_dir / bkgest_input, blanked, header, overwrite=True)

    exit_code = runner.run(build_bkgest_command(
        bkgest_code, bkgest_include_dir, bkgest_input, bkgest_dict,
        table=bkgest_dict["filename_global_clippedmean_refimage_tbl"], output=bkgest_output),
        cwd=work_dir)
    if not (work_dir / bkgest_output).exists():
        raise RuntimeError(f"bkgest on the reference image wrote no output (exit code {exit_code})")

    data = np.array(fits.getdata(work_dir / bkgest_output, 0), dtype=np.float32)
    if data.shape != original.shape:
        raise ValueError(f"bkgest output shape {data.shape} does not match reference shape "
                         f"{original.shape}")
    data[uncovered] = original[uncovered]
    header["REFBKGSB"] = (True, "background subtracted with the science bkgest")
    fits.writeto(work_dir / reference_image, data, header, overwrite=True)
    count = int(np.count_nonzero(uncovered))
    logger.info("Resampled reference pixels without coverage, excluded from bkgest = %d", count)
    return count
