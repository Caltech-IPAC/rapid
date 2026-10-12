"""Tests for the [SEXTRACTOR_DIFFIMAGE_SFFT] detection overrides.

Run with:  python -m pytest pipeline/test/test_sfft_detection.py
"""

import configparser
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.sfftDetectionSubs import sfft_sextractor_dict

REPO = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
INI = os.path.join(REPO, "cdf", "awsBatchSubmitJobs_launchSingleSciencePipeline.ini")
CONV = os.path.join(REPO, "cdf", "rapidSexDiffImageSfftFilter.conv")

BASE = {"sextractor_filter": "N", "sextractor_filter_name": "/cfg/rapidSexDiffImageFilter.conv",
        "sextractor_detect_minarea": "5", "sextractor_detect_thresh": "2.5",
        "sextractor_analysis_thresh": "2.5", "sextractor_deblend_nthresh": "32"}
OVERRIDES = {"sextractor_filter": "Y", "sextractor_filter_name": "rapidSexDiffImageSfftFilter.conv",
             "sextractor_detect_minarea": "1", "sextractor_detect_thresh": "2.0",
             "sextractor_analysis_thresh": "2.0"}


def test_overrides_applied_and_filter_resolved_against_cfg_path():
    out = sfft_sextractor_dict(BASE, OVERRIDES, False, "/cfg")
    assert out["sextractor_filter"] == "Y"
    assert out["sextractor_filter_name"] == "/cfg/rapidSexDiffImageSfftFilter.conv"
    assert out["sextractor_detect_minarea"] == "1"
    assert out["sextractor_detect_thresh"] == "2.0"
    assert out["sextractor_analysis_thresh"] == "2.0"
    assert out["sextractor_deblend_nthresh"] == "32"


def test_base_dict_is_not_modified():
    before = dict(BASE)
    sfft_sextractor_dict(BASE, OVERRIDES, False, "/cfg")
    assert BASE == before


def test_absolute_filter_name_is_kept():
    out = sfft_sextractor_dict(BASE, {"sextractor_filter_name": "/elsewhere/k.conv"}, False, "/cfg")
    assert out["sextractor_filter_name"] == "/elsewhere/k.conv"


@pytest.mark.parametrize("overrides, crossconv", [(None, False), (OVERRIDES, True)])
def test_no_block_or_crossconv_leaves_settings_unchanged(overrides, crossconv):
    assert sfft_sextractor_dict(BASE, overrides, crossconv, "/cfg") == BASE


def test_ini_block_uncommented_parses_to_the_tested_settings():
    text = open(INI).read()
    start = text.index("#[SEXTRACTOR_DIFFIMAGE_SFFT]")
    block = text[start:text.index("\n\n", start)]
    cp = configparser.ConfigParser()
    cp.read_string("\n".join(line[1:] for line in block.splitlines()))
    assert dict(cp["SEXTRACTOR_DIFFIMAGE_SFFT"]) == OVERRIDES


def test_ini_ships_with_the_block_disabled():
    cp = configparser.ConfigParser()
    cp.read(INI)
    assert not cp.has_section("SEXTRACTOR_DIFFIMAGE_SFFT")


def test_conv_file_is_a_normalised_7x7_fwhm2_gaussian():
    lines = open(CONV).read().splitlines()
    assert lines[0] == "CONV NORM"
    k = np.loadtxt(CONV, skiprows=2)
    assert k.shape == (7, 7)
    sigma = 2.0 / 2.3548200450309493
    y, x = np.mgrid[-3:4, -3:4]
    g = np.exp(-(x * x + y * y) / (2 * sigma * sigma))
    assert np.allclose(k, g / g.sum(), atol=1e-8)
