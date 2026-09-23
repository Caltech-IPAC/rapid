"""bkgest: local background subtraction of the science image.

`dev`: the bkgest block inline in
``awsBatchSubmitJobs_runSingleSciencePipeline.py``. The science image (its
PV-header copy) is background-subtracted; the reference, already swarped
with background subtraction, is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from rapidpipe.science.difference.tools import ToolRunner


def build_bkgest_command(
    bkgest_code: str,
    bkgest_include_dir: str,
    input_image: str,
    bkgest_dict: Mapping[str, str],
) -> list[str]:
    """`dev`'s bkgest argument list; option values are the configured strings."""
    return [bkgest_code,
            "-i", input_image,
            "-f", bkgest_dict["output_image_type"],
            "-c", bkgest_dict["clippedmean_calc_type"],
            "-g", bkgest_dict["local_clippedmean_grid_spacing"],
            "-w", bkgest_dict["local_clippedmean_input_window"],
            "-a", bkgest_include_dir,
            "-ot", bkgest_dict["filename_global_clippedmean_sciimage_tbl"],
            "-o2", bkgest_dict["filename_bkg_subbed_science_image"]]


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
