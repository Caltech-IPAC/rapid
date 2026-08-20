"""Equivalence test for the SFFT command-line construction.

The [SFFT] bright-source-masking and segmentation parameters replace two implicit behaviours:

  * bright-source masking was selected by testing whether the science-image filename began
    with a lower-case "r", which conflated the socsims with the rimtimsims;
  * segmentation masking was appended only inside the cross-convolution branch, making it an
    accidental side effect of crossconv_flag.

`legacy_sfft_command_args` below is the pre-change behaviour, frozen here as the specification.
The tests assert that `build_sfft_command_args` reproduces it exactly both when the new
parameters are absent (back-compatibility) and when they are present and set to the values
matching the data set.

Run with:  python -m pytest pipeline/test/test_sfft_command_equivalence.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.sfftCommandSubs import build_sfft_command_args

PY = '/usr/bin/python3.11'
CODE = '/code/modules/sfft/sfft_rapid_rimtimsim.py'
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

SOCSIMS_CFG = {'sfft_bsmask_value': '20000.0',
               'sfft_bsmask_radius': '30.0',
               'sfft_use_gainmatch_catalogs': 'False',
               'sfft_use_segmentation': 'False'}

OPENUNIVERSE_CFG = {'sfft_bsmask_value': '50.0',
                    'sfft_bsmask_radius': '100.0',
                    'sfft_use_gainmatch_catalogs': 'True',
                    'sfft_use_segmentation': 'True'}


def legacy_sfft_command_args(science_image_filename, crossconv_flag):

    """Behaviour prior to the introduction of the explicit [SFFT] parameters."""

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

    return build_sfft_command_args(PY, CODE, SCI, REF, SCICAT, REFCAT, SCIPSF, REFPSF,
                                   SCISEGM, REFSEGM, science_image_filename,
                                   crossconv_flag, sfft_dict)


def test_absent_parameters_reproduce_legacy_behaviour():

    """With no new parameters the command must be identical for every data set."""

    for filename in (SOCSIMS, RIMTIMSIM, OPENUNIVERSE):
        for crossconv_flag in (False, True):
            assert build(filename, crossconv_flag, {}) == \
                   legacy_sfft_command_args(filename, crossconv_flag), \
                   'mismatch for {} crossconv={}'.format(filename, crossconv_flag)


def test_configured_parameters_reproduce_legacy_behaviour():

    """The per-data-set configuration blocks must reproduce the same command."""

    assert build(SOCSIMS, False, SOCSIMS_CFG) == \
           legacy_sfft_command_args(SOCSIMS, False)

    assert build(RIMTIMSIM, False, SOCSIMS_CFG) == \
           legacy_sfft_command_args(RIMTIMSIM, False)

    assert build(OPENUNIVERSE, True, OPENUNIVERSE_CFG) == \
           legacy_sfft_command_args(OPENUNIVERSE, True)


def test_segmentation_is_independent_of_crossconv():

    """Segmentation masking must now be selectable without cross-convolution, and vice versa."""

    cfg = dict(SOCSIMS_CFG, sfft_use_segmentation='True')
    cmd = build(SOCSIMS, False, cfg)
    assert "--scisegm" in cmd and "--crossconv" not in cmd

    cfg = dict(OPENUNIVERSE_CFG, sfft_use_segmentation='False')
    cmd = build(OPENUNIVERSE, True, cfg)
    assert "--crossconv" in cmd and "--scisegm" not in cmd


def test_bright_source_masking_no_longer_depends_on_the_filename():

    """The same configuration must give the same command whatever the filename."""

    a = build(SOCSIMS, False, SOCSIMS_CFG)
    b = build(OPENUNIVERSE, False, SOCSIMS_CFG)
    assert a == b
