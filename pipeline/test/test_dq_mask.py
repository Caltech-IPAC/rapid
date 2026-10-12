"""Tests for data-quality masking of L2 pixels at ingest.

Run with:  python -m pytest pipeline/test/test_dq_mask.py
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.dqMaskSubs import (apply_dq_mask, default_dq_mask_bits,
                                 find_dq_masked_pixels)

# Roman pixel DQ flag values (roman_datamodels.dqflags.pixel).
DO_NOT_USE = 0x1
SATURATED = 0x2
JUMP_DET = 0x4
GW_AFFECTED_DATA = 0x10
DEAD = 0x400
HOT = 0x800
NONLINEAR = 0x10000
NO_LIN_CORR = 0x100000
UNRELIABLE_FLAT = 0x2000000


def test_default_mask_is_the_agreed_flag_set():
    assert default_dq_mask_bits == (DO_NOT_USE | SATURATED | DEAD | HOT | NONLINEAR
                                    | NO_LIN_CORR | UNRELIABLE_FLAT)


@pytest.mark.parametrize("flag", [DO_NOT_USE, SATURATED, DEAD, HOT, NONLINEAR,
                                  NO_LIN_CORR, UNRELIABLE_FLAT])
def test_each_default_flag_is_masked(flag):
    dq = np.zeros((4, 4), dtype=np.uint32)
    dq[1, 2] = flag
    masked = find_dq_masked_pixels(dq)
    assert masked[1, 2]
    assert np.count_nonzero(masked) == 1


@pytest.mark.parametrize("flag", [JUMP_DET, GW_AFFECTED_DATA])
def test_jump_and_guide_window_stripe_are_not_masked(flag):
    dq = np.full((4, 4), flag, dtype=np.uint32)
    assert not find_dq_masked_pixels(dq).any()


def test_guide_window_box_is_masked_through_do_not_use():
    dq = np.zeros((4, 4), dtype=np.uint32)
    dq[:, 1] = GW_AFFECTED_DATA
    dq[2, 1] = GW_AFFECTED_DATA | DO_NOT_USE
    masked = find_dq_masked_pixels(dq)
    assert masked[2, 1]
    assert np.count_nonzero(masked) == 1


def test_high_bit_flags_survive_a_signed_dq_array():
    dq = np.array([[UNRELIABLE_FLAT, 0]], dtype=np.int32)
    assert find_dq_masked_pixels(dq).tolist() == [[True, False]]


def test_zero_mask_masks_nothing():
    dq = np.full((3, 3), 0xFFFFFFFF, dtype=np.uint32)
    assert not find_dq_masked_pixels(dq, 0).any()


@pytest.mark.parametrize("bits", [-1, 2**32])
def test_out_of_range_mask_bits_are_rejected(bits):
    with pytest.raises(ValueError):
        find_dq_masked_pixels(np.zeros((2, 2), dtype=np.uint32), bits)


def test_non_integer_dq_is_rejected():
    with pytest.raises(TypeError):
        find_dq_masked_pixels(np.zeros((2, 2), dtype=np.float32))


def test_apply_sets_nan_in_place_and_counts():
    data = np.arange(9, dtype=np.float32).reshape(3, 3)
    masked = np.zeros((3, 3), dtype=bool)
    masked[0, 0] = masked[2, 1] = True
    assert apply_dq_mask(data, masked) == 2
    assert np.isnan(data[0, 0]) and np.isnan(data[2, 1])
    assert np.count_nonzero(np.isnan(data)) == 2
    assert data[1, 1] == 4.0


def test_apply_refuses_integer_arrays():
    with pytest.raises(TypeError):
        apply_dq_mask(np.zeros((2, 2), dtype=np.int32), np.ones((2, 2), dtype=bool))


def test_apply_refuses_shape_mismatch():
    with pytest.raises(ValueError):
        apply_dq_mask(np.zeros((2, 2), dtype=np.float32), np.ones((3, 3), dtype=bool))
