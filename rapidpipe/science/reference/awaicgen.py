"""awaicgen: the mosaic geometry and the command line, as `dev` builds them.

`dev`: the launcher fills awaicgen's mosaic size, centre and rotation
(``pipeline/awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py``:
``pixel_scale = math.fabs(cdelt1_refimage)``, size = ``pixel_scale *
float(naxis)``, centre = the sky tile's centre, rotation = ``crota2``) and
``build_awaicgen_command_line_args`` (``modules/utils/rapid_pipeline_subs.py``)
builds the command in a fixed flag order from the resulting dictionary.
The values pass through ``str()`` exactly as `dev` passes them, so the
command line is the same string for string.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def awaicgen_dict_for_mosaic(awaicgen: Mapping[str, Any], *, naxis1: int, naxis2: int,
                             cdelt1: float, rotation: float, ra_center: float,
                             dec_center: float) -> dict[str, Any]:
    """`dev`'s launcher: the ``[awaicgen]`` table with the mosaic fields filled.

    ``awaicgen_mosaic_size_x``/``_y`` are ``|cdelt1|`` times the pixel
    counts (degrees), ``awaicgen_mosaic_rotation`` is ``crota2``, and the
    centre is the one given. Values are stored as `dev` stores them, the
    ``str()`` of the float.
    """
    d = dict(awaicgen)
    pixel_scale = math.fabs(cdelt1)
    awaicgen_mosaic_size_x = pixel_scale * float(naxis1)
    awaicgen_mosaic_size_y = pixel_scale * float(naxis2)
    d["awaicgen_mosaic_size_x"] = str(awaicgen_mosaic_size_x)
    d["awaicgen_mosaic_size_y"] = str(awaicgen_mosaic_size_y)
    d["awaicgen_mosaic_rotation"] = str(rotation)
    d["awaicgen_ra_center"] = str(ra_center)
    d["awaicgen_dec_center"] = str(dec_center)
    return d


def build_awaicgen_command_line_args(awaicgen_dict: Mapping[str, Any],
                                     executable: str = "awaicgen") -> list[str]:
    """`dev` ``build_awaicgen_command_line_args``: the flag order and conversions.

    Keys are the lower-case forms `dev`'s configparser gives them
    (``awaicgen_ra_center`` for ``awaicgen_RA_center``).
    """
    awaicgen_input_images_list_file = awaicgen_dict["awaicgen_input_images_list_file"]
    awaicgen_input_uncert_list_file = awaicgen_dict["awaicgen_input_uncert_list_file"]
    awaicgen_mosaic_size_x = float(awaicgen_dict["awaicgen_mosaic_size_x"])
    awaicgen_mosaic_size_y = float(awaicgen_dict["awaicgen_mosaic_size_y"])
    awaicgen_RA_center = float(awaicgen_dict["awaicgen_ra_center"])
    awaicgen_Dec_center = float(awaicgen_dict["awaicgen_dec_center"])
    awaicgen_mosaic_rotation = float(awaicgen_dict["awaicgen_mosaic_rotation"])
    awaicgen_pixelscale_absolute = float(awaicgen_dict["awaicgen_pixelscale_absolute"])
    awaicgen_inv_var_weight_flag = int(awaicgen_dict["awaicgen_inv_var_weight_flag"])
    awaicgen_pixelflux_scale_flag = int(awaicgen_dict["awaicgen_pixelflux_scale_flag"])
    awaicgen_simple_coadd_flag = int(awaicgen_dict["awaicgen_simple_coadd_flag"])
    awaicgen_num_threads = int(awaicgen_dict["awaicgen_num_threads"])
    awaicgen_output_mosaic_image_file = awaicgen_dict["awaicgen_output_mosaic_image_file"]
    awaicgen_output_mosaic_cov_map_file = awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
    awaicgen_output_mosaic_uncert_image_file = awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]

    code_to_execute_args = [executable]
    code_to_execute_args.append("-f1")
    code_to_execute_args.append(awaicgen_input_images_list_file)
    code_to_execute_args.append("-f3")
    code_to_execute_args.append(awaicgen_input_uncert_list_file)
    code_to_execute_args.append("-X")
    code_to_execute_args.append(str(awaicgen_mosaic_size_x))
    code_to_execute_args.append("-Y")
    code_to_execute_args.append(str(awaicgen_mosaic_size_y))
    code_to_execute_args.append("-R")
    code_to_execute_args.append(str(awaicgen_RA_center))
    code_to_execute_args.append("-D")
    code_to_execute_args.append(str(awaicgen_Dec_center))
    code_to_execute_args.append("-C")
    code_to_execute_args.append(str(awaicgen_mosaic_rotation))
    code_to_execute_args.append("-pa")
    code_to_execute_args.append(str(awaicgen_pixelscale_absolute))
    code_to_execute_args.append("-wf")
    code_to_execute_args.append(str(awaicgen_inv_var_weight_flag))
    code_to_execute_args.append("-sf")
    code_to_execute_args.append(str(awaicgen_pixelflux_scale_flag))
    code_to_execute_args.append("-sc")
    code_to_execute_args.append(str(awaicgen_simple_coadd_flag))
    code_to_execute_args.append("-nt")
    code_to_execute_args.append(str(awaicgen_num_threads))
    code_to_execute_args.append("-o1")
    code_to_execute_args.append(awaicgen_output_mosaic_image_file)
    code_to_execute_args.append("-o2")
    code_to_execute_args.append(awaicgen_output_mosaic_cov_map_file)
    code_to_execute_args.append("-o3")
    code_to_execute_args.append(awaicgen_output_mosaic_uncert_image_file)
    code_to_execute_args.append("-v")

    logger.info("code_to_execute_args = %s", code_to_execute_args)
    return code_to_execute_args
