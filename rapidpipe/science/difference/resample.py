"""SIP to PV, and SWarp of the reference bundle onto the science grid.

`dev`: ``convert_from_sip_to_pv``, ``build_swarp_command_line_args`` and
``resample_reference_image_to_science_image_with_pv_distortion``
(``modules/utils/rapid_pipeline_subs.py``). The science image's SIP
distortion is converted to PV (SWarp's convention) into ``*_pv.fits``; a
``.head`` symlink beside each SWarp output name points at that file, so
SWarp resamples onto the science grid; the reference image is swarped with
the ``[swarp]`` table as configured, then its coverage map and uncertainty
image with background subtraction turned off.

The ``[swarp]`` table holds `dev`'s configured strings unchanged. `dev`'s
option list passes ``"-COMBINE_BUFSIZE "`` with a trailing space; kept.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Mapping

from astropy.io import fits

from rapidpipe.science.difference.tools import ToolRunner

logger = logging.getLogger(__name__)

_SWARP_OPTIONS = (
    "IMAGEOUT_NAME", "WEIGHTOUT_NAME", "HEADER_ONLY", "HEADER_SUFFIX",
    "WEIGHT_TYPE", "RESCALE_WEIGHTS", "WEIGHT_SUFFIX", "WEIGHT_IMAGE",
    "WEIGHT_THRESH", "COMBINE", "COMBINE_TYPE", "CLIP_AMPFRAC", "CLIP_SIGMA",
    "CLIP_WRITELOG", "CLIP_LOGNAME", "BLANK_BADPIXELS", "CELESTIAL_TYPE",
    "PROJECTION_TYPE", "PROJECTION_ERR", "CENTER_TYPE", "CENTER",
    "PIXELSCALE_TYPE", "PIXEL_SCALE", "IMAGE_SIZE", "RESAMPLE", "RESAMPLE_DIR",
    "RESAMPLE_SUFFIX", "RESAMPLING_TYPE", "OVERSAMPLING", "INTERPOLATE",
    "FSCALASTRO_TYPE", "FSCALE_KEYWORD", "FSCALE_DEFAULT", "GAIN_KEYWORD",
    "GAIN_DEFAULT", "SATLEV_KEYWORD", "SATLEV_DEFAULT", "SUBTRACT_BACK",
    "BACK_TYPE", "BACK_DEFAULT", "BACK_SIZE", "BACK_FILTERSIZE",
    "BACK_FILTTHRESH", "VMEM_DIR", "VMEM_MAX", "MEM_MAX", "COMBINE_BUFSIZE",
    "DELETE_TMPFILES", "COPY_KEYWORDS", "WRITE_FILEINFO", "WRITE_XML",
    "VERBOSE_TYPE", "NNODES", "NODE_INDEX", "NTHREADS", "NOPENFILES_MAX",
)

#: `dev` spells this one option with a trailing space on the command line.
_OPTION_SPELLING = {"COMBINE_BUFSIZE": "-COMBINE_BUFSIZE "}


def build_swarp_command_line_args(swarp_dict: Mapping[str, str], executable: str = "swarp") -> list[str]:
    """`dev` ``build_swarp_command_line_args``."""
    args = [executable, swarp_dict["swarp_input_image"]]
    for option in _SWARP_OPTIONS:
        args.append(_OPTION_SPELLING.get(option, "-" + option))
        args.append(swarp_dict[f"swarp_{option}".lower()])
    logger.info("code_to_execute_args = %s", args)
    return args


def _default_sip_to_pv(header) -> None:
    # Imported here, not at module scope: the vendored converter pulls in
    # sympy, which only the stage's own run needs.
    from modules.sip_tpv.sip_tpv.sip_to_pv import sip_to_pv

    sip_to_pv(header)


def convert_from_sip_to_pv(
    input_fits_file_with_sip,
    hdu_index: int,
    output_fits_file_with_pv,
    sip_to_pv: Callable = _default_sip_to_pv,
) -> None:
    """`dev` ``convert_from_sip_to_pv``: the HDU's data under a PV header, as the primary HDU."""
    hdul = fits.open(input_fits_file_with_sip)
    sip_header = hdul[hdu_index].header
    sip_to_pv(sip_header)
    hdul[hdu_index].header = sip_header
    new_hdu = fits.PrimaryHDU(data=hdul[hdu_index].data, header=hdul[hdu_index].header)
    new_hdu.writeto(output_fits_file_with_pv, overwrite=True, checksum=True)
    hdul.close()


def _replace_symlink(link: Path, target: str) -> None:
    if link.is_symlink():
        link.unlink()
    os.symlink(target, link)


def resample_reference_image_to_science_image_with_pv_distortion(
    runner: ToolRunner,
    work_dir: Path,
    input_science_image: str,
    hdu_index_for_science_image_data: int,
    input_reference_image: str,
    input_reference_cov_map: str,
    input_reference_uncert_image: str,
    hdu_index_for_reference_image_data: int,
    pv_convert_flag_for_reference_image_data: bool,
    swarp_dict: Mapping[str, str],
    executable: str = "swarp",
    sip_to_pv: Callable = _default_sip_to_pv,
) -> tuple[str, str, str, str, str, str, str]:
    """`dev` ``resample_reference_image_to_science_image_with_pv_distortion``.

    File names are bare names in ``work_dir``, as `dev` uses them in its
    working directory. Returns `dev`'s seven names: the science, reference,
    coverage and uncertainty PV files, then the three resampled outputs.
    """
    output_resampled_reference_image = input_reference_image.replace(".fits", "_resampled.fits")
    output_resampled_reference_cov_map = input_reference_cov_map.replace(".fits", "_resampled.fits")
    output_resampled_reference_uncert_image = input_reference_uncert_image.replace(".fits", "_resampled.fits")

    sci_img_fits_file_with_pv = input_science_image.replace(".fits", "_pv.fits")
    ref_img_fits_file_with_pv = input_reference_image.replace(".fits", "_pv.fits")
    ref_cov_fits_file_with_pv = input_reference_cov_map.replace(".fits", "_pv.fits")
    ref_uncert_fits_file_with_pv = input_reference_uncert_image.replace(".fits", "_pv.fits")

    convert_from_sip_to_pv(
        work_dir / input_science_image, hdu_index_for_science_image_data,
        work_dir / sci_img_fits_file_with_pv, sip_to_pv)

    if pv_convert_flag_for_reference_image_data:
        for source, target in (
            (input_reference_image, ref_img_fits_file_with_pv),
            (input_reference_cov_map, ref_cov_fits_file_with_pv),
            (input_reference_uncert_image, ref_uncert_fits_file_with_pv),
        ):
            convert_from_sip_to_pv(
                work_dir / source, hdu_index_for_reference_image_data,
                work_dir / target, sip_to_pv)

    output_weight_file = output_resampled_reference_image.replace(".fits", "_wt.fits")
    output_cov_weight_file = output_resampled_reference_cov_map.replace(".fits", "_wt.fits")
    output_uncert_weight_file = output_resampled_reference_uncert_image.replace(".fits", "_wt.fits")

    # The .head beside each output name is how SWarp is told the target grid.
    for output in (output_resampled_reference_image,
                   output_resampled_reference_cov_map,
                   output_resampled_reference_uncert_image):
        _replace_symlink(work_dir / output.replace(".fits", ".head"), sci_img_fits_file_with_pv)

    d = dict(swarp_dict)

    d["swarp_input_image"] = (
        ref_img_fits_file_with_pv if pv_convert_flag_for_reference_image_data
        else input_reference_image)
    d["swarp_imageout_name"] = output_resampled_reference_image
    d["swarp_weightout_name"] = output_weight_file
    runner.run(build_swarp_command_line_args(d, executable), cwd=work_dir)

    # No background subtraction for the coverage map and uncertainty image.
    d["swarp_subtract_back"] = "N"
    d["swarp_back_type"] = "MANUAL"
    d["swarp_back_default"] = "0.0"

    d["swarp_input_image"] = (
        ref_cov_fits_file_with_pv if pv_convert_flag_for_reference_image_data
        else input_reference_cov_map)
    d["swarp_imageout_name"] = output_resampled_reference_cov_map
    d["swarp_weightout_name"] = output_cov_weight_file
    runner.run(build_swarp_command_line_args(d, executable), cwd=work_dir)

    d["swarp_input_image"] = (
        ref_uncert_fits_file_with_pv if pv_convert_flag_for_reference_image_data
        else input_reference_uncert_image)
    d["swarp_imageout_name"] = output_resampled_reference_uncert_image
    d["swarp_weightout_name"] = output_uncert_weight_file
    runner.run(build_swarp_command_line_args(d, executable), cwd=work_dir)

    return (sci_img_fits_file_with_pv,
            ref_img_fits_file_with_pv,
            ref_cov_fits_file_with_pv,
            ref_uncert_fits_file_with_pv,
            output_resampled_reference_image,
            output_resampled_reference_cov_map,
            output_resampled_reference_uncert_image)
