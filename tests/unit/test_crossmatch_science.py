"""Tests for rapidpipe.science.crossmatch.catalog: `dev`'s ordering, CSV lines and object ids."""

from __future__ import annotations

import pytest

from rapidpipe.science.crossmatch import catalog
from rapidpipe.science.spatial import index_to_radec, radec_index

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
