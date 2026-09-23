"""Tests for the per-filter reference-image zeropoint.

The reference image is built by scaling every input frame by
10 ** (0.4 * (zprefimg - ZPTMAG)), so zprefimg decides both the scale the coadd
lands on and, through MAGZP, what later reads that coadd believes about it.
These tests cover the resolution of the per-filter value and check that the
shipped configuration still says what the code expects it to.

Run with:  python -m pytest pipeline/test/test_reference_image_zeropoint.py
"""

import configparser
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from modules.utils.rapid_pipeline_subs import (galsim_roman_ab_zeropoints,
                                               get_reference_image_zeropoint)

MASTER_INI = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "cdf",
                          "awsBatchSubmitJobs_launchSingleSciencePipeline.ini")

RAPID_FILTER_NAMES = ("R062", "Z087", "Y106", "J129", "H158", "F184", "K213",
                      "W146")


def _awaicgen_block():

    """The [AWAICGEN] zprefimg entries, as the launch scripts copy them."""

    config = configparser.ConfigParser()
    config.read(MASTER_INI)

    return {key: value for key, value in config["AWAICGEN"].items()
            if key.startswith("zprefimg")}


def test_per_filter_entry_is_preferred():

    block = {"zprefimg": "17.0", "zprefimg_y106": "26.4606501787"}

    assert get_reference_image_zeropoint(block, "Y106") == 26.4606501787


def test_either_spelling_of_the_filter_resolves():

    """The config file and the FITS headers need not agree on the spelling."""

    block = {"zprefimg": "17.0", "zprefimg_y106": "26.4606501787"}

    assert get_reference_image_zeropoint(block, "F106") == \
        get_reference_image_zeropoint(block, "Y106")

    block = {"zprefimg": "17.0", "zprefimg_f106": "26.4606501787"}

    assert get_reference_image_zeropoint(block, "Y106") == 26.4606501787


def test_header_value_padding_is_tolerated():

    """FITS string values come back padded."""

    block = {"zprefimg": "17.0", "zprefimg_y106": "26.4606501787"}

    assert get_reference_image_zeropoint(block, "Y106  ") == 26.4606501787


def test_filter_without_an_entry_falls_back_to_the_scalar():

    block = {"zprefimg": "17.0", "zprefimg_y106": "26.4606501787"}

    assert get_reference_image_zeropoint(block, "H158") == 17.0
    assert get_reference_image_zeropoint(block, None) == 17.0


def test_nothing_configured_raises():

    """Better to stop than to coadd onto a zeropoint nobody chose."""

    with pytest.raises(KeyError):
        get_reference_image_zeropoint({"zprefimg_y106": "26.46"}, "H158")


def test_master_ini_carries_every_filter():

    block = _awaicgen_block()

    for filter_name in RAPID_FILTER_NAMES:
        assert f"zprefimg_{filter_name.lower()}" in block, \
            f"the master .ini has no zprefimg entry for {filter_name}"


def test_master_ini_values_match_the_galsim_table():

    """The shipped values are the GalSim-derived zeropoints.

    They are configuration, so an operator may retune them per data set; this
    test states what the repository ships, so that an edit is a deliberate act
    rather than a drift nobody notices.
    """

    block = _awaicgen_block()

    for filter_name in RAPID_FILTER_NAMES:
        assert get_reference_image_zeropoint(block, filter_name) == \
            pytest.approx(galsim_roman_ab_zeropoints[filter_name], abs=1.0e-9)


def test_scale_factor_is_unity_for_a_corrected_openuniverse_frame():

    """The point of the change: frames scale by about one, not by 1e-4.

    An OpenUniverse frame whose ZPTMAG has been corrected carries exactly the
    GalSim-derived zeropoint, so the reference image is built on the scale the
    frames already have.
    """

    block = _awaicgen_block()

    for filter_name in RAPID_FILTER_NAMES:

        zprefimg = get_reference_image_zeropoint(block, filter_name)
        zptmag = galsim_roman_ab_zeropoints[filter_name]

        scale = 10.0 ** (0.4 * (zprefimg - zptmag))

        assert scale == pytest.approx(1.0, abs=1.0e-9)
