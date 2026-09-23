"""SFFT: the command line, run as `dev` runs it.

`dev`: ``build_sfft_command_args`` (``pipeline/sfftCommandSubs.py``) and
the SFFT block inline in ``awsBatchSubmitJobs_runSingleSciencePipeline.py``.
SFFT's science (``modules/sfft/sfft_rapid_rimtimsim.py``) is not ported:
the stage builds `dev`'s command and handles its output files as `dev`
does. A non-zero exit is SFFT failing, which is not fatal (`dev` exits 4,
which Batch counts as success; the rebuild notes it in the execution
record).

`dev` ran SFFT in its own ``/sfft_env`` virtual environment (`` source
/sfft_env/bin/activate && ... && deactivate``, with ``python3.11``). The
rebuild's default is different: the pipeline image installs sfft 1.7.3
into the main conda environment the stage itself runs under (see
``containers/rapid-pipeline/Containerfile``'s note on
``RAPID_SFFT_VENV``), so by default SFFT runs directly, with no
activation step, using the stage's own interpreter --- the same
convention ``[paths] python`` uses for ZOGY. `dev`'s values remain
selectable, e.g. for a venv-based image; see ``[sfft]`` in
``rapidpipe/settings/difference.toml``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_sfft_command_args(python_cmd,
                            sfft_code,
                            filename_scifile,
                            filename_reffile,
                            filename_scicat,
                            filename_refcat,
                            filename_scipsf,
                            filename_refpsf,
                            filename_scisegm,
                            filename_refsegm,
                            science_image_filename,
                            crossconv_flag,
                            sfft_bsmask_value: str,
                            sfft_bsmask_radius: str,
                            sfft_use_gainmatch_catalogs: bool,
                            sfft_use_segmentation: bool | None) -> list[str]:
    """`dev` ``build_sfft_command_args``.

    `dev` falls back to a science-file-name test when its ``[SFFT]`` table
    has no ``sfft_bsmask_value``; here an empty ``sfft_bsmask_value``
    setting selects that same fallback. ``sfft_use_segmentation`` of
    ``None`` defaults to ``crossconv_flag``, as an absent `dev` key does.
    """
    if sfft_bsmask_value == "":
        logger.warning("*** Warning: [sfft] sfft_bsmask_value is empty; falling back to the "
                       "legacy science-image-filename test.")
        if "r" == science_image_filename[0]:
            sfft_bsmask_value = "20000.0"
            sfft_bsmask_radius = "30.0"
            sfft_use_gainmatch_catalogs = False
        else:
            sfft_bsmask_value = "50.0"
            sfft_bsmask_radius = "100.0"
            sfft_use_gainmatch_catalogs = True

    # A quirk in the SFFT software requires "./" before the positional inputs.
    sfft_cmd = [python_cmd,
                sfft_code,
                "./" + filename_scifile,
                "./" + filename_reffile]

    if sfft_use_gainmatch_catalogs:
        sfft_cmd += ["--scicat", filename_scicat, "--refcat", filename_refcat]

    sfft_cmd += ["--bsmaskvalue", str(sfft_bsmask_value),
                 "--bsmaskradius", str(sfft_bsmask_radius)]

    # Without cross-convolution the SFFT difference PSF is the science PSF.
    sfft_cmd += ["--scipsf", filename_scipsf]

    if crossconv_flag:
        sfft_cmd += ["--crossconv", "--refpsf", filename_refpsf]

    if sfft_use_segmentation is None:
        sfft_use_segmentation = crossconv_flag

    if sfft_use_segmentation:
        sfft_cmd += ["--scisegm", filename_scisegm, "--refsegm", filename_refsegm]

    return sfft_cmd


def sfft_file_names(crossconv_flag: bool) -> dict[str, str]:
    """`dev`'s fixed SFFT output names."""
    if crossconv_flag:
        names = {"diffimage": "sfftdiffimage_dconv_masked.fits",
                 "soln": "sfftsoln_cconv.fits",
                 "diffpsf": "sfftdiffpsf_dconv.fits"}
    else:
        names = {"diffimage": "sfftdiffimage_masked.fits",
                 "soln": "sfftsoln.fits",
                 "diffpsf": "sfftdiffpsf.fits"}
    names["cconvdiff"] = "sfftdiffimage_cconv_masked.fits"   # only made with crossconv_flag
    names["scisegm"] = "sfftscisegm.fits"
    names["refsegm"] = "sfftrefsegm.fits"
    names["diffimage_unc"] = "sfftdiffimage_uncert_masked.fits"
    return names


def shell_command(activate_cmd: str, sfft_cmd: list[str], deactivate_cmd: str = "deactivate") -> str:
    """The command `run_shell` executes.

    An empty ``activate_cmd`` means no activation and no deactivation: the
    command runs directly, as it does in the stage's own environment. A
    non-empty ``activate_cmd`` reproduces `dev`'s one-shell command,
    activating, running, then deactivating, joined by ``&&``.
    """
    if not activate_cmd:
        return " ".join(sfft_cmd)
    return activate_cmd + " && " + " ".join(sfft_cmd) + " && " + deactivate_cmd
