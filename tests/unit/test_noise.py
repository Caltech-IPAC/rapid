"""The difference image's noise correlation (``rapidpipe.science.difference.noise``).

Contracts: white noise gives 1; noise smoothed by a 2 x 2 box gives 1.75 (per axis the block
sum weights its 5 inputs 0.5, 1, 1, 1, 0.5);
outliers and NaN do not move it (clipped, and a block with a NaN pixel is
not averaged); an image with fewer than ``MIN_BLOCKS`` full blocks has no
value.
"""

from __future__ import annotations

import numpy as np
import pytest

from rapidpipe.science.difference import noise


def white(n: int = 800, seed: int = 5) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 1.0, (n, n))


def test_white_noise_is_one():
    assert noise.noise_correlation(white()) == pytest.approx(1.0, abs=0.03)


def test_noise_smoothed_by_a_2x2_box_is_1_75():
    w = white(801)
    smoothed = (w[:-1, :-1] + w[1:, :-1] + w[:-1, 1:] + w[1:, 1:]) / 4
    assert noise.noise_correlation(smoothed) == pytest.approx(1.75, rel=0.03)


def test_nan_and_outliers_do_not_move_it():
    image = white()
    image[100:300, 100:300] = np.nan
    image[tuple(np.random.default_rng(1).integers(0, 800, (2, 200)))] = 1e4
    assert noise.noise_correlation(image) == pytest.approx(1.0, abs=0.03)


def test_too_few_full_blocks_has_no_value():
    image = white(36)
    assert noise.noise_correlation(image) is None
