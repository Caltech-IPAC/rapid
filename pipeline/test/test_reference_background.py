"""Tests for the reference-image background-subtraction helpers.

Run with:  python -m pytest pipeline/test/test_reference_background.py

bkgest itself is not run here (it is a Linux binary); see the PR for the container test.
"""

import os
import sys

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.referenceBackgroundSubs import (restore_uncovered_pixels, uncovered_pixels,
                                              write_bkgest_input)


@pytest.fixture
def files(tmp_path):
    ref = np.full((6, 8), 5.0, np.float32)
    ref[:, :2] = 0.0                          # SWarp writes 0 off coverage
    cov = np.ones((6, 8), np.float32)
    cov[:, :2] = 0.0
    cov[0, 7] = np.nan
    hdr = fits.Header()
    hdr["CTYPE1"] = "RA---TPV"
    paths = {k: str(tmp_path / f"{k}.fits") for k in ("ref", "cov", "inp", "bkg", "out")}
    fits.writeto(paths["ref"], ref, hdr)
    fits.writeto(paths["cov"], cov)
    return paths


def test_uncovered_is_nonpositive_or_nonfinite_coverage(files):
    unc = uncovered_pixels(files["cov"])
    assert unc[:, :2].all() and unc[0, 7]
    assert np.count_nonzero(unc) == 13


def test_bkgest_input_blanks_only_uncovered_and_keeps_header(files):
    unc = uncovered_pixels(files["cov"])
    write_bkgest_input(files["ref"], unc, files["inp"])
    data = fits.getdata(files["inp"])
    assert np.isnan(data[unc]).all()
    assert (data[~unc] == 5.0).all()
    assert fits.getheader(files["inp"])["CTYPE1"] == "RA---TPV"


def test_restore_puts_back_swarp_values_and_marks_header(files):
    unc = uncovered_pixels(files["cov"])
    bkg_subbed = np.full((6, 8), 0.25, np.float32)
    bkg_subbed[unc] = np.nan
    fits.writeto(files["bkg"], bkg_subbed)
    restore_uncovered_pixels(files["bkg"], files["ref"], unc, files["out"])
    out = fits.getdata(files["out"])
    hdr = fits.getheader(files["out"])
    assert (out[:, :2] == 0.0).all() and out[0, 7] == 5.0
    assert (out[~unc] == 0.25).all()
    assert hdr["CTYPE1"] == "RA---TPV" and hdr["REFBKGSB"] is True


def test_restore_can_overwrite_the_reference_in_place(files):
    unc = uncovered_pixels(files["cov"])
    fits.writeto(files["bkg"], np.zeros((6, 8), np.float32))
    restore_uncovered_pixels(files["bkg"], files["ref"], unc, files["ref"])
    out = fits.getdata(files["ref"])
    assert (out[~unc] == 0.0).all() and (out[:, :2] == 0.0).all()


def test_shape_mismatch_is_an_error(files):
    with pytest.raises(ValueError):
        write_bkgest_input(files["ref"], np.zeros((3, 3), bool), files["inp"])
