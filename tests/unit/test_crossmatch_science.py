"""Tests for rapidpipe.science.crossmatch.catalog: `dev`'s ordering, CSV lines and cone."""

from __future__ import annotations

import math

import pytest

from rapidpipe.science.crossmatch import catalog
from rapidpipe.science.spatial import (
    compute_angular_separation,
    field_center,
    field_corners,
    field_neighbours,
    index_to_radec,
    radec_index,
)

RUN_COLUMNS = ("01RUN", "01ATTEMPT", "01SET")


def test_expid_order_is_ascending_mjd_not_expid():
    assert catalog.expid_order([(2002, 61000.1), (1001, 61000.2), (1500, 60999.9)]) == [
        1500, 2002, 1001]


def test_expid_order_later_pair_overwrites_as_devs_dict_does():
    # dev: expids_dict[expid] = mjdobs, table after table.
    assert catalog.expid_order([(1, 5.0), (2, 3.0), (1, 1.0)]) == [1, 2]


def test_expid_order_ties_keep_first_seen_order():
    assert catalog.expid_order([(9, 1.0), (3, 1.0), (7, 0.5)]) == [7, 9, 3]


def test_expid_order_of_nothing_is_empty():
    assert catalog.expid_order([]) == []


def test_merges_line_is_devs_line_plus_run_columns():
    assert catalog.merges_line(123, 45, RUN_COLUMNS) == "123,45,01RUN,01ATTEMPT,01SET\n"


def test_astroobjects_line_uses_str_of_each_value():
    line = catalog.astroobjects_line(7, 269.45, -28.77, 101.25, RUN_COLUMNS)
    assert line == "7,269.45,-28.77,101.25,01RUN,01ATTEMPT,01SET\n"


def test_astroobjects_line_with_a_numpy_aid():
    aid = radec_index(269.45, -28.77)
    assert catalog.astroobjects_line(aid, 1.0, 2.0, 3.0, RUN_COLUMNS).startswith(f"{int(aid)},")


def test_new_object_id_is_radec_index_as_an_int():
    aid = catalog.new_object_id(269.45, -28.77)
    assert isinstance(aid, int)
    assert aid == int(radec_index(269.45, -28.77))
    ra, dec = index_to_radec(aid)
    assert ra == pytest.approx(269.45, abs=1e-7)
    assert dec == pytest.approx(-28.77, abs=1e-7)


def test_equal_positions_make_equal_aids():
    assert catalog.new_object_id(269.46, -28.78) == catalog.new_object_id(269.46, -28.78)


def test_inclusion_cone_radius_is_max_corner_separation_plus_match_radius():
    center = (10.0, 0.0)
    corners = [(9.9, -0.1), (10.1, -0.1), (10.1, 0.1), (9.95, 0.05)]
    expected = max(compute_angular_separation(10.0, 0.0, ra, dec) for ra, dec in corners)
    assert catalog.inclusion_cone_radius(center, corners, 0.001) == expected + 0.001


def test_field_inclusion_cone_uses_the_closed_form_tile():
    field = 4662268
    assert len(field_neighbours(field)) == 8
    ra0, dec0, radius = catalog.field_inclusion_cone(field, 0.00001528)
    assert (ra0, dec0) == field_center(field)
    seps = [compute_angular_separation(ra0, dec0, ra, dec) for ra, dec in field_corners(field)]
    assert radius == max(seps) + 0.00001528
    # A tile is ~0.088 deg wide in RA at dec -28.8: the half-diagonal.
    assert 0.05 < radius < 0.07
    assert math.isfinite(radius)
