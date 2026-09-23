"""Tests for the GalSim-derived Roman AB zeropoint table.

The table in modules/utils/rapid_pipeline_subs is written out rather than
derived at run time, so that a GalSim upgrade cannot quietly re-calibrate newly
processed data, and so that the module stays importable where GalSim is not
installed.  The cost of writing it out is that it can drift from the GalSim it
came from, which is what this test exists to catch.

Run with:  python -m pytest pipeline/test/test_galsim_zeropoints.py
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from modules.utils.rapid_pipeline_subs import (galsim_roman_ab_zeropoints,
                                               get_galsim_roman_ab_zeropoint)

#: The two spellings of each filter, as the table is keyed.
FILTER_PAIRS = (("F062", "R062"), ("F087", "Z087"), ("F106", "Y106"),
                ("F129", "J129"), ("F158", "H158"), ("F213", "K213"),
                ("F146", "W146"))


def test_both_spellings_agree():

    """F062 and R062 are the same filter and must carry the same zeropoint."""

    for roman_name, rapid_name in FILTER_PAIRS:
        assert galsim_roman_ab_zeropoints[roman_name] == \
            galsim_roman_ab_zeropoints[rapid_name]


def test_all_eight_filters_present():

    rapid_names = {"R062", "Z087", "Y106", "J129", "H158", "F184", "K213",
                   "W146"}

    assert rapid_names <= set(galsim_roman_ab_zeropoints)


def test_lookup_is_case_and_whitespace_tolerant():

    """FITS headers pad their string values, so the lookup has to cope."""

    assert get_galsim_roman_ab_zeropoint("Y106 ") == \
        get_galsim_roman_ab_zeropoint("y106")

    assert get_galsim_roman_ab_zeropoint(None) is None
    assert get_galsim_roman_ab_zeropoint("NOTAFILTER") is None


def test_table_matches_installed_galsim():

    """The table must still equal what the installed GalSim derives.

    Skipped where GalSim is absent, which is the whole reason the values are
    written out; where it is present, a mismatch means either GalSim changed
    its throughput files or the table was edited by hand, and both want a
    deliberate decision rather than a silent shift in calibration.
    """

    roman = pytest.importorskip("galsim.roman",
                                reason="GalSim is not installed here")

    bandpasses = roman.getBandpasses(AB_zeropoint=True)

    area_term = 2.5 * math.log10(roman.collecting_area)

    for filter_name, bandpass in bandpasses.items():

        expected = bandpass.zeropoint + area_term

        assert filter_name in galsim_roman_ab_zeropoints, \
            f"GalSim knows filter {filter_name}, which the table does not"

        assert galsim_roman_ab_zeropoints[filter_name] == \
            pytest.approx(expected, abs=1.0e-9), \
            (f"{filter_name}: table has "
             f"{galsim_roman_ab_zeropoints[filter_name]}, installed GalSim "
             f"derives {expected}")
