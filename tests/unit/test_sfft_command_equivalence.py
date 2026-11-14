"""The SFFT command line: the builder's equivalence with the legacy command.

``legacy_sfft_command_args`` is the command before the explicit
``[SFFT]`` bright-source and segmentation keys, frozen as the specification;
the builder takes each key as an argument, and the
kernel-fit masking keys (``sfft_zero_margin``, ``sfft_bsmask_isoarea``,
``sfft_bsmask_isofactor``) are off when empty, so off they must leave the
command unchanged.
"""

from __future__ import annotations

from pathlib import Path

from rapidpipe.foundation.layers import resolve
from rapidpipe.science.difference.sfft import build_sfft_command_args

SETTINGS = Path(__file__).resolve().parents[2] / "rapidpipe" / "settings"

PY = '/usr/bin/python3.11'
CODE = '/code/modules/sfft/sfft_rapid_crowded.py'
SCI = 'bkg_subbed_science_image.fits'
REF = 'awaicgen_output_mosaic_image_resampled_gainmatched.fits'
SCICAT = 'bkg_subbed_science_image_scigainmatchsexcat.txt'
REFCAT = 'awaicgen_output_mosaic_image_resampled_refgainmatchsexcat.txt'
SCIPSF = 'WFI_SCA07_F146_PSF_DET_DIST_normalized.fits'
REFPSF = 'refimage_psf_fid8.fits'
SCISEGM = 'sfftscisegm.fits'
REFSEGM = 'sfftrefsegm.fits'

SOCSIMS = 'r0034001002001003059_0001_wfi07_f146_cal_lite_reformatted.fits'
RIMTIMSIM = 'rimtimsim_wfi_lvl02_K213_stars_only_SCA02_2027_02_14.fits'
OPENUNIVERSE = 'Roman_TDS_simple_model_H158_26041_8.fits'

# An [SFFT] with none of the keys: empty bsmask value selects the filename fallback, None segmentation follows crossconv.
ABSENT_CFG = {'sfft_bsmask_value': '', 'sfft_bsmask_radius': '', 'sfft_use_gainmatch_catalogs': False,
              'sfft_use_segmentation': None}

SOCSIMS_CFG = {'sfft_bsmask_value': '20000.0',
               'sfft_bsmask_radius': '30.0',
               'sfft_use_gainmatch_catalogs': False,
               'sfft_use_segmentation': False}

OPENUNIVERSE_CFG = {'sfft_bsmask_value': '50.0',
                    'sfft_bsmask_radius': '100.0',
                    'sfft_use_gainmatch_catalogs': True,
                    'sfft_use_segmentation': True}

MASKING_OFF = {'sfft_zero_margin': '', 'sfft_bsmask_isoarea': '', 'sfft_bsmask_isofactor': ''}


def legacy_sfft_command_args(science_image_filename, crossconv_flag):
    """The command before the explicit [SFFT] keys, frozen as the specification.

    Parameters
    ----------
    science_image_filename : str
        The science file name the legacy bright-source test reads.
    crossconv_flag : bool
        Whether SFFT cross-convolves.

    Returns
    -------
    list of str
        The command's arguments.
    """
    if "r" == science_image_filename[0]:
        cmd = [PY, CODE, "./" + SCI, "./" + REF,
               "--bsmaskvalue", "20000.0", "--bsmaskradius", "30.0"]
    else:
        cmd = [PY, CODE, "./" + SCI, "./" + REF,
               "--scicat", SCICAT, "--refcat", REFCAT,
               "--bsmaskvalue", "50.0", "--bsmaskradius", "100.0"]

    cmd += ["--scipsf", SCIPSF]

    if crossconv_flag:
        cmd += ["--crossconv", "--refpsf", REFPSF,
                "--scisegm", SCISEGM, "--refsegm", REFSEGM]

    return cmd


def build(science_image_filename, crossconv_flag, sfft_dict):
    """The builder's command for one [difference.sfft] table.

    Parameters
    ----------
    science_image_filename : str
        The science file name.
    crossconv_flag : bool
        Whether SFFT cross-convolves.
    sfft_dict : dict
        The table's keys; the masking keys absent are taken as off.

    Returns
    -------
    list of str
        The command's arguments.
    """
    return build_sfft_command_args(PY, CODE, SCI, REF, SCICAT, REFCAT, SCIPSF, REFPSF,
                                   SCISEGM, REFSEGM, science_image_filename,
                                   crossconv_flag, **dict(MASKING_OFF, **sfft_dict))


def test_absent_parameters_reproduce_legacy_behaviour():
    for filename in (SOCSIMS, RIMTIMSIM, OPENUNIVERSE):
        for crossconv_flag in (False, True):
            assert build(filename, crossconv_flag, ABSENT_CFG) == \
                   legacy_sfft_command_args(filename, crossconv_flag), \
                   'mismatch for {} crossconv={}'.format(filename, crossconv_flag)


def test_configured_parameters_reproduce_legacy_behaviour():
    assert build(SOCSIMS, False, SOCSIMS_CFG) == legacy_sfft_command_args(SOCSIMS, False)
    assert build(RIMTIMSIM, False, SOCSIMS_CFG) == legacy_sfft_command_args(RIMTIMSIM, False)
    assert build(OPENUNIVERSE, True, OPENUNIVERSE_CFG) == legacy_sfft_command_args(OPENUNIVERSE, True)


def test_segmentation_is_independent_of_crossconv():
    cmd = build(SOCSIMS, False, dict(SOCSIMS_CFG, sfft_use_segmentation=True))
    assert "--scisegm" in cmd and "--crossconv" not in cmd

    cmd = build(OPENUNIVERSE, True, dict(OPENUNIVERSE_CFG, sfft_use_segmentation=False))
    assert "--crossconv" in cmd and "--scisegm" not in cmd


def test_bright_source_masking_no_longer_depends_on_the_filename():
    assert build(SOCSIMS, False, SOCSIMS_CFG) == build(OPENUNIVERSE, False, SOCSIMS_CFG)


def test_commissioning_masking_options_are_passed():
    cfg = dict(OPENUNIVERSE_CFG, sfft_zero_margin='2', sfft_bsmask_isoarea='2000', sfft_bsmask_isofactor='2.0')
    cmd = build(OPENUNIVERSE, False, cfg)
    assert cmd[cmd.index("--zeromargin") + 1] == "2"
    assert cmd[cmd.index("--bsmaskisoarea") + 1] == "2000"
    assert cmd[cmd.index("--bsmaskisofactor") + 1] == "2.0"


def test_the_isofactor_is_passed_only_with_the_isoarea():
    cmd = build(OPENUNIVERSE, False, dict(OPENUNIVERSE_CFG, sfft_bsmask_isofactor='2.0'))
    assert "--bsmaskisofactor" not in cmd and "--bsmaskisoarea" not in cmd


def _shipped_command(overlay):
    sfft = resolve(SETTINGS / "foundation-thin.toml", overlay)[0]["difference"]["sfft"]
    return build_sfft_command_args(
        PY, CODE, SCI, REF, SCICAT, REFCAT, SCIPSF, REFPSF, SCISEGM, REFSEGM, OPENUNIVERSE,
        bool(sfft["crossconv_flag"]), sfft["sfft_bsmask_value"], sfft["sfft_bsmask_radius"],
        bool(sfft["sfft_use_gainmatch_catalogs"]), bool(sfft["sfft_use_segmentation"]),
        sfft["sfft_zero_margin"], sfft["sfft_bsmask_isoarea"], sfft["sfft_bsmask_isofactor"])


def test_the_base_leaves_the_masking_off_and_the_command_unchanged():
    assert _shipped_command(None) == legacy_sfft_command_args(OPENUNIVERSE, True)


def test_the_flight_overlay_passes_commissionings_values():
    cmd = _shipped_command(SETTINGS / "datasets" / "flight.toml")
    assert cmd[cmd.index("--zeromargin") + 1] == "2"
    assert cmd[cmd.index("--bsmaskisoarea") + 1] == "2000"
    assert cmd[cmd.index("--bsmaskisofactor") + 1] == "2.0"
