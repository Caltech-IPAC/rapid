"""Tests for rapidpipe.science.statistics.lightcurve: `dev`'s per-object statistics.

``compute_radec_statistics`` against hand-computed values (the RA 0/360
wrap, a pole, a plain pair), a known-answer literal that locks the ported
function's output, the flux statistics, the (aid, sid) accumulation and
the CSV line.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from rapidpipe.science.statistics.lightcurve import (
    accumulate,
    compute_radec_statistics,
    csv_line,
    object_statistics,
)


def test_ra_wrap_uses_the_mean_vector_not_the_arithmetic_mean():
    meanra, meandec, stdra, stddec, spread = compute_radec_statistics([359.9, 0.1], [0.0, 0.0])
    # The arithmetic mean would be 180; the mean vector points at RA 0.
    assert min(meanra, 360.0 - meanra) == pytest.approx(0.0, abs=1e-12)
    # dev's quirk, kept: a mean vector a hair below RA 0 gives arctan2 a tiny
    # negative angle, and `% 360.0` rounds it to exactly 360.0, not 0.0.
    assert 0.0 <= meanra <= 360.0
    assert meandec == pytest.approx(0.0, abs=1e-12)
    # Each source is 0.1 degree from the mean along RA at Dec 0.
    assert stdra == pytest.approx(0.1, abs=1e-12)
    assert stddec == pytest.approx(0.0, abs=1e-12)
    assert spread == pytest.approx(0.1, abs=1e-12)


def test_a_ring_around_the_pole_averages_to_the_pole():
    meanra, meandec, stdra, stddec, spread = compute_radec_statistics(
        [0.0, 90.0, 180.0, 270.0], [89.0, 89.0, 89.0, 89.0])
    assert meandec == pytest.approx(90.0, abs=1e-9)
    # Every source is one degree from the pole, and one degree below it in Dec.
    assert spread == pytest.approx(1.0, abs=1e-9)
    assert stddec == pytest.approx(0.0, abs=1e-9)
    assert all(math.isfinite(v) for v in (meanra, stdra))


def test_a_plain_pair_split_in_dec():
    meanra, meandec, stdra, stddec, spread = compute_radec_statistics([10.0, 10.0], [-1.0, 1.0])
    assert meanra == pytest.approx(10.0, abs=1e-12)
    assert meandec == pytest.approx(0.0, abs=1e-12)
    assert stddec == pytest.approx(1.0, abs=1e-12)
    assert stdra == pytest.approx(0.0, abs=1e-12)
    assert spread == pytest.approx(1.0, abs=1e-12)


def test_known_answer_three_sources_across_the_wrap():
    """A literal lock on the ported function (the selftest fixture's object 101)."""
    got = compute_radec_statistics([359.99995, 0.00002, 0.00008], [0.5, 0.50001, 0.49999])
    expected = (1.6666666694403534e-05, 0.5000000000002149, 5.312256873055041e-05,
                8.164965809262469e-06)
    for value, literal in zip(got[:4], expected):
        assert value == pytest.approx(literal, rel=1e-9, abs=1e-15)


def test_a_single_source_has_zero_spread_not_nan():
    stats = object_statistics(102, [10.25], [-3.5], [55.5])
    assert stats.nsources == 1
    assert stats.meanra == pytest.approx(10.25, abs=1e-9)
    assert stats.meandec == pytest.approx(-3.5, abs=1e-12)
    assert (stats.stdevra, stats.stdevdec, stats.stdevflux) == (0.0, 0.0, 0.0)
    assert stats.meanflux == 55.5


def test_flux_statistics_are_devs_numpy_mean_and_population_std():
    stats = object_statistics(7, [1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [1.0, 3.0, 8.0])
    assert stats.meanflux == pytest.approx(4.0)
    assert stats.stdevflux == pytest.approx(float(np.std([1.0, 3.0, 8.0])))
    assert stats.nsources == 3


def test_accumulate_groups_by_aid_and_counts_each_pair_once():
    grouped = accumulate([(2, 20, 1.0, 2.0, 3.0), (1, 10, 4.0, 5.0, 6.0),
                          (2, 21, 7.0, 8.0, 9.0), (2, 20, 1.0, 2.0, 3.0)])
    assert grouped.aids() == [1, 2]
    assert grouped.ras == {1: [4.0], 2: [1.0, 7.0]}
    assert grouped.decs[2] == [2.0, 8.0] and grouped.fluxes[2] == [3.0, 9.0]
    assert grouped.repeated_pairs == 1


def test_csv_line_is_devs_column_order_then_the_run_columns():
    stats = object_statistics(102, [10.5], [-3.5], [55.5])
    line = csv_line(stats, ("RUN", "ATTEMPT", "SET"))
    assert line.endswith("\n")
    fields = line.rstrip("\n").split(",")
    assert len(fields) == 11
    assert fields[0] == "102" and fields[7] == "1"
    assert fields[8:] == ["RUN", "ATTEMPT", "SET"]
    assert [float(v) for v in fields[1:7]] == pytest.approx([10.5, 0.0, -3.5, 0.0, 55.5, 0.0])
    # str() of a numpy float is the bare number, never "np.float64(...)".
    assert "np." not in line
