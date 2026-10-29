"""Tests for selecting reference-image inputs by coverage, using synthetic footprints (no database or S3)."""

import configparser
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

try:
    import psycopg2  # noqa: F401
except ImportError:
    stub = types.ModuleType("psycopg2")
    stub.DatabaseError = Exception
    stub.ProgrammingError = Exception
    sys.modules["psycopg2"] = stub

import pipeline.referenceImageSubs as rfis

MASTER_INI = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "cdf",
                          "awsBatchSubmitJobs_launchSingleSciencePipeline.ini")

PIXSCALE = 0.11 / 3600.0      # deg/pixel
NAXIS = 1000                  # reference image is NAXIS x NAXIS pixels


def _ref_wcs(ra0, dec0):
    return rfis.make_refimage_wcs(ra0, dec0, NAXIS, NAXIS, -PIXSCALE, PIXSCALE, 0.0)


def _frame_corners(ra_c, dec_c, nx, ny):

    """Four corners, in order around the image, of an unrotated nx x ny frame centered on (ra_c,dec_c)."""

    w = rfis.make_refimage_wcs(ra_c, dec_c, nx, ny, -PIXSCALE, PIXSCALE, 0.0)
    x = [-0.5, nx - 0.5, nx - 0.5, -0.5]
    y = [-0.5, -0.5, ny - 0.5, ny - 0.5]
    sky = w.pixel_to_world(x, y)

    return list(sky.ra.deg), list(sky.dec.deg)


def _left_half_corners(ra0, dec0):

    """A frame covering the left (high R.A.) half of the reference image, extending past its edges."""

    w = _ref_wcs(ra0, dec0)
    x = [-100.0, NAXIS / 2 - 0.5, NAXIS / 2 - 0.5, -100.0]
    y = [-100.0, -100.0, NAXIS + 100.0, NAXIS + 100.0]
    sky = w.pixel_to_world(x, y)

    return list(sky.ra.deg), list(sky.dec.deg)


def test_coverage_fraction_is_inclusive():
    cov_map = np.array([[0.0, 5.0], [6.0, 4.9]])
    assert rfis.coverage_fraction(cov_map, 5) == 0.5


def test_frame_larger_than_reference_covers_everything():
    w = _ref_wcs(150.0, 2.0)
    x_grid, y_grid = rfis.coverage_sample_grid(NAXIS, NAXIS)
    ras, decs = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)

    mask = rfis.compute_footprint_mask(w, x_grid, y_grid, ras, decs)

    assert mask.all()


def test_frame_outside_reference_covers_nothing():
    w = _ref_wcs(150.0, 2.0)
    x_grid, y_grid = rfis.coverage_sample_grid(NAXIS, NAXIS)
    ras, decs = _frame_corners(150.2, 2.0, NAXIS, NAXIS)

    assert not rfis.compute_footprint_mask(w, x_grid, y_grid, ras, decs).any()


def test_selection_stops_once_target_plus_tolerance_reached():
    w = _ref_wcs(150.0, 2.0)
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)

    n, fraction, decision = rfis.select_refimage_inputs(w, NAXIS, NAXIS, [full] * 5, 2, 0.5)

    assert (n, fraction, decision) == (2, 1.0, "build")


def test_selection_keeps_candidate_order():

    # The first frame misses the reference but is still counted.

    w = _ref_wcs(150.0, 2.0)
    miss = _frame_corners(150.2, 2.0, NAXIS, NAXIS)
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)

    n, fraction, decision = rfis.select_refimage_inputs(w, NAXIS, NAXIS, [miss, full, full], 1, 0.5)

    assert (n, decision) == (2, "build")


def test_estimate_within_tolerance_of_target_is_verified():
    w = _ref_wcs(150.0, 2.0)
    half = _left_half_corners(150.0, 2.0)

    n, fraction, decision = rfis.select_refimage_inputs(w, NAXIS, NAXIS, [half] * 3, 3, 0.5)

    assert fraction == pytest.approx(0.5)
    assert (n, decision) == (3, "verify")


def test_estimate_below_target_minus_tolerance_is_rejected():
    w = _ref_wcs(150.0, 2.0)
    half = _left_half_corners(150.0, 2.0)

    n, fraction, decision = rfis.select_refimage_inputs(w, NAXIS, NAXIS, [half] * 3, 4, 0.5)

    assert (n, fraction, decision) == (3, 0.0, "reject")


@pytest.mark.parametrize("ra0", [0.01, 359.99, 180.0])
def test_footprints_straddling_ra_zero_match_same_geometry_elsewhere(ra0):

    # At ra0 = 0.01 and 359.99 one frame crosses R.A. = 0/360; coverage must match ra0 = 180.

    def depth_map(ra_ref):
        w = _ref_wcs(ra_ref, -30.0)
        x_grid, y_grid = rfis.coverage_sample_grid(NAXIS, NAXIS)
        depth = np.zeros(x_grid.shape, dtype=np.int32)
        for d_ra in (-0.006, 0.006):
            ras, decs = _frame_corners((ra_ref + d_ra) % 360.0, -30.0, NAXIS, NAXIS)
            depth += rfis.compute_footprint_mask(w, x_grid, y_grid, ras, decs)
        return depth

    depth = depth_map(ra0)

    np.testing.assert_array_equal(depth, depth_map(180.0))
    assert 0.0 < rfis.coverage_fraction(depth, 2) < 1.0


def test_master_ini_has_coverage_settings():
    config = configparser.ConfigParser()
    config.read(MASTER_INI)
    ref_image = config["REF_IMAGE"]

    assert float(ref_image["min_coverage_depth"]) >= 1
    assert 0.0 < float(ref_image["coverage_target_fraction"]) <= 1.0
    assert "min_n_images_to_coadd" not in ref_image
    assert "max_n_images_to_coadd" not in ref_image
