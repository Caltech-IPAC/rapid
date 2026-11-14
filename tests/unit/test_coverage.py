"""The reference's coverage selection (``rapidpipe.products.coverage``), on synthetic footprints.

Contracts: the coverage fraction counts a pixel at the depth; a frame
larger than the reference covers all of it and one beside it none;
selection stops at the maximum coverage or the frame cap, skips a frame
that misses the reference without spending the cap, and judges the
selection build, verify (within the tolerance of the minimum) or reject; a
footprint across R.A. 0/360 covers what the same geometry covers at
R.A. 180; the reference's tiles straddle R.A. 0 there too, and the candidates
are ordered by ``mjdobs``, then distance, then their key; the settings
body carries the coverage keys with ``"nframes"`` as its base.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np
import pytest

from rapidpipe.products import coverage as cov
from rapidpipe.products.spatial import tessellation_field

REPO = Path(__file__).resolve().parents[2]
PIXSCALE = 0.11 / 3600.0      # deg/pixel
NAXIS = 1000                  # the reference image is NAXIS x NAXIS pixels


def _ref_wcs(ra0, dec0):
    return cov.make_refimage_wcs(ra0, dec0, NAXIS, NAXIS, -PIXSCALE, PIXSCALE, 0.0)


def _frame_corners(ra_c, dec_c, nx, ny):
    """Four corners, in order around the image, of an unrotated nx x ny frame centred on (ra_c, dec_c)."""
    w = cov.make_refimage_wcs(ra_c, dec_c, nx, ny, -PIXSCALE, PIXSCALE, 0.0)
    sky = w.pixel_to_world([-0.5, nx - 0.5, nx - 0.5, -0.5], [-0.5, -0.5, ny - 0.5, ny - 0.5])
    return list(sky.ra.deg), list(sky.dec.deg)


def _left_half_corners(ra0, dec0):
    """A frame covering the left (high R.A.) half of the reference image, extending past its edges."""
    w = _ref_wcs(ra0, dec0)
    sky = w.pixel_to_world([-100.0, NAXIS / 2 - 0.5, NAXIS / 2 - 0.5, -100.0],
                           [-100.0, -100.0, NAXIS + 100.0, NAXIS + 100.0])
    return list(sky.ra.deg), list(sky.dec.deg)


def test_coverage_fraction_is_inclusive():
    assert cov.coverage_fraction(np.array([[0.0, 5.0], [6.0, 4.9]]), 5) == 0.5


def test_frame_larger_than_reference_covers_everything():
    w = _ref_wcs(150.0, 2.0)
    x_grid, y_grid = cov.coverage_sample_grid(NAXIS, NAXIS)
    assert cov.compute_footprint_mask(w, x_grid, y_grid, *_frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)).all()


def test_frame_outside_reference_covers_nothing():
    w = _ref_wcs(150.0, 2.0)
    x_grid, y_grid = cov.coverage_sample_grid(NAXIS, NAXIS)
    assert not cov.compute_footprint_mask(w, x_grid, y_grid, *_frame_corners(150.2, 2.0, NAXIS, NAXIS)).any()


def test_selection_stops_once_maximum_reached():
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)
    result = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS, [full] * 5, 1, 0.5, 2, 1.0, 50)
    assert result == ([0, 1], 1.0, 1.0, "build")


def test_selection_stops_at_frame_cap():
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)
    result = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS, [full] * 5, 1, 0.5, 4, 1.0, 3)
    assert result == ([0, 1, 2], 1.0, 0.0, "build")


def test_frame_cap_before_minimum_is_rejected():
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)
    selected, fraction, _, decision = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS, [full] * 5,
                                                                 3, 0.5, 4, 1.0, 2)
    assert (selected, fraction, decision) == ([0, 1], 0.0, "reject")


def test_selection_skips_candidates_that_miss_reference():
    # Frames that miss the reference are skipped; the rest are selected in candidate order.
    miss = _frame_corners(150.2, 2.0, NAXIS, NAXIS)
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)
    selected, _, _, decision = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS,
                                                          [full, miss, full, full], 1, 0.5, 2, 1.0, 50)
    assert (selected, decision) == ([0, 2], "build")


def test_skipped_candidates_do_not_count_toward_frame_cap():
    miss = _frame_corners(150.2, 2.0, NAXIS, NAXIS)
    full = _frame_corners(150.0, 2.0, 2 * NAXIS, 2 * NAXIS)
    selected, fraction, _, decision = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS,
                                                                 [miss, miss, full, full, full], 1, 0.5, 4, 1.0, 2)
    assert (selected, fraction, decision) == ([2, 3], 1.0, "build")


def test_all_candidates_missing_reference_is_rejected():
    miss = _frame_corners(150.2, 2.0, NAXIS, NAXIS)
    selected, fraction, _, decision = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS, [miss] * 3,
                                                                 1, 0.5, 4, 1.0, 50)
    assert (selected, fraction, decision) == ([], 0.0, "reject")


def test_estimate_within_tolerance_of_minimum_is_verified():
    half = _left_half_corners(150.0, 2.0)
    selected, fraction, _, decision = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS, [half] * 3,
                                                                 3, 0.5, 4, 1.0, 50)
    assert fraction == pytest.approx(0.5)
    assert (selected, decision) == ([0, 1, 2], "verify")


def test_estimate_below_minimum_minus_tolerance_is_rejected():
    half = _left_half_corners(150.0, 2.0)
    selected, fraction, _, decision = cov.select_refimage_inputs(_ref_wcs(150.0, 2.0), NAXIS, NAXIS, [half] * 3,
                                                                 4, 0.5, 4, 1.0, 50)
    assert (selected, fraction, decision) == ([0, 1, 2], 0.0, "reject")


@pytest.mark.parametrize("ra0", [0.01, 359.99, 180.0])
def test_footprints_straddling_ra_zero_match_same_geometry_elsewhere(ra0):
    # At ra0 = 0.01 and 359.99 one frame crosses R.A. = 0/360; coverage must match ra0 = 180.

    def depth_map(ra_ref):
        w = _ref_wcs(ra_ref, -30.0)
        x_grid, y_grid = cov.coverage_sample_grid(NAXIS, NAXIS)
        depth = np.zeros(x_grid.shape, dtype=np.int32)
        for d_ra in (-0.006, 0.006):
            depth += cov.compute_footprint_mask(w, x_grid, y_grid,
                                                *_frame_corners((ra_ref + d_ra) % 360.0, -30.0, NAXIS, NAXIS))
        return depth

    depth = depth_map(ra0)
    np.testing.assert_array_equal(depth, depth_map(180.0))
    assert 0.0 < cov.coverage_fraction(depth, 2) < 1.0


def test_settings_carry_the_coverage_keys_with_nframes_the_base():
    """The settings body carries the coverage keys, and flight-1049 selects by coverage."""
    selection = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())["reference"]["selection"]
    assert selection["selection_type"] == "nframes"
    assert selection["min_coverage_depth"] >= 1
    assert 0.0 < selection["min_coverage_fraction"] <= 1.0
    assert selection["max_coverage_depth"] >= selection["min_coverage_depth"]
    assert 0.0 < selection["max_coverage_fraction"] <= 1.0
    assert selection["max_frames"] >= 1
    assert selection["coverage_estimate_tolerance"] == cov.COVERAGE_ESTIMATE_TOLERANCE
    flight = tomllib.loads((REPO / "rapidpipe/settings/datasets/flight-1049.toml").read_text())["reference"]["selection"]
    assert flight["selection_type"] == "coverage"


@pytest.mark.parametrize("ra0", [0.0, 359.995, 180.0])
def test_the_reference_tiles_hold_its_centres_tile_and_straddle_ra_zero_as_elsewhere(ra0):
    tiles = cov.reference_tiles(cov.make_refimage_wcs(ra0, -30.0, 9100, 9100, -PIXSCALE, PIXSCALE, 0.0),
                                9100, 9100)
    assert tessellation_field(ra0, -30.0) in tiles
    for d_ra, d_dec in ((-0.13, -0.13), (0.13, 0.13), (-0.13, 0.13), (0.13, -0.13)):
        corner = ((ra0 + d_ra / np.cos(np.radians(-30.0))) % 360.0, -30.0 + d_dec)
        assert tessellation_field(*corner) in tiles


def test_candidates_are_ordered_by_mjdobs_then_distance_across_ra_zero_then_key():
    center = (0.001, 10.0)
    near_across = cov.candidate_order(5.0, 359.999, 10.0, (3, 1, 8), center)     # 0.002 deg away, across R.A. 0
    far = cov.candidate_order(5.0, 0.1, 10.0, (1, 1, 8), center)
    earlier = cov.candidate_order(4.0, 1.0, 10.0, (9, 9, 8), center)
    tie_a = cov.candidate_order(5.0, 0.1, 10.0, (2, 1, 8), center)
    assert sorted([far, tie_a, near_across, earlier]) == [earlier, near_across, far, tie_a]
    assert cov.angular_distance(359.999, 10.0, 0.001, 10.0) == pytest.approx(0.002 * np.cos(np.radians(10.0)), rel=1e-3)
