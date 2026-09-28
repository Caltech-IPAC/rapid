"""Tests for rapidpipe.products.units: `register`'s producer-keyed unit id
(runs page, "Rules").
"""

from __future__ import annotations

from rapidpipe.products.units import (
    REGISTER,
    nominal_unit_id,
    register_unit_id,
    takes_producer_unit,
)
from rapidpipe.stages.contract import STAGE_NAMES


def test_register_is_the_stage_name():
    assert REGISTER == "register"


def test_takes_producer_unit_is_true_only_for_register():
    assert takes_producer_unit("register") is True
    for stage in STAGE_NAMES:
        if stage != "register":
            assert takes_producer_unit(stage) is False


def test_register_unit_id_joins_producer_and_unit():
    assert register_unit_id("admit", "r0034001002001001001/SCA01") == (
        "admit/r0034001002001001001/SCA01")


def test_nominal_unit_id_strips_the_producer_prefix():
    assert nominal_unit_id("admit/r0034001002001001001/SCA01") == (
        "r0034001002001001001/SCA01")


def test_nominal_unit_id_leaves_a_slash_free_unit_id_unchanged():
    assert nominal_unit_id("20260927") == "20260927"


def test_register_unit_id_and_nominal_unit_id_round_trip():
    unit_id = "r0034001002001001001/SCA01"
    derived = register_unit_id("difference", unit_id)
    assert nominal_unit_id(derived) == unit_id
