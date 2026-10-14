"""Tests for the point-source image FWHM.

Run with:  python -m pytest pipeline/test/test_psf_fwhm.py
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.psfFwhmSubs import (catalog_fwhm_stats, fwhm_mode, point_source_catalog_params,
                                  select_point_sources)


def _row(fwhm, flags=0, flux=1000.0, fluxerr=10.0, elongation=1.05):
    # Strings, as parse_ascii_text_sextractor_catalog returns them.
    return [str(v) for v in (fwhm, flags, flux, fluxerr, elongation)]


def test_params_match_row_layout():
    assert point_source_catalog_params == ["FWHM_IMAGE", "FLAGS", "FLUX_AUTO",
                                           "FLUXERR_AUTO", "ELONGATION"]


def test_elongated_galaxies_are_cut():
    stars = [_row(1.9) for _ in range(60)]
    galaxies = [_row(4.0, elongation=1.8) for _ in range(120)]
    minpix, maxpix, medpix, _ = catalog_fwhm_stats(stars + galaxies, percentile=90.0)
    assert medpix == pytest.approx(1.9, abs=0.05)
    assert minpix == pytest.approx(1.9)
    assert maxpix == pytest.approx(4.0)


def test_mode_finds_stars_among_more_numerous_round_galaxies():
    rng = np.random.default_rng(1)
    stars = rng.normal(1.86, 0.04, 300)
    galaxies = rng.uniform(2.0, 6.0, 1000)
    assert np.median(np.r_[stars, galaxies]) > 2.5
    rows = [_row(f) for f in np.r_[stars, galaxies]]
    _, _, medpix, _ = catalog_fwhm_stats(rows, percentile=90.0)
    assert medpix == pytest.approx(1.86, abs=0.05)
    assert fwhm_mode(np.r_[stars, galaxies]) == pytest.approx(1.86, abs=0.05)


def test_between_ten_and_fifty_point_sources_uses_their_median():
    rows = [_row(1.9) for _ in range(20)] + [_row(4.0, elongation=1.8) for _ in range(100)]
    _, _, medpix, _ = catalog_fwhm_stats(rows, percentile=90.0)
    assert medpix == pytest.approx(1.9)


@pytest.mark.parametrize("bad", [dict(flags=2), dict(flux=100.0), dict(elongation=1.5),
                                 dict(fluxerr=0.0), dict(fwhm=0.0), dict(fwhm="nan")])
def test_each_cut_rejects(bad):
    kw = dict(fwhm=1.9, flags=0, flux=1000.0, fluxerr=10.0, elongation=1.05)
    kw.update(bad)
    rows = np.array([_row(**kw)], dtype=float).T
    assert not select_point_sources(*rows).any()


def test_thresholds_are_inclusive():
    rows = np.array([_row(1.9, flux=500.0, fluxerr=10.0, elongation=1.3)], dtype=float).T
    assert select_point_sources(*rows).all()


def test_too_few_point_sources_falls_back_to_all_sources():
    rows = [_row(1.9) for _ in range(5)] + [_row(4.0, elongation=1.8) for _ in range(20)]
    _, _, medpix, _ = catalog_fwhm_stats(rows, percentile=90.0)
    assert medpix == pytest.approx(4.0)


def test_empty_catalog_gives_nan():
    stats = catalog_fwhm_stats([], percentile=90.0)
    assert len(stats) == 4
    assert all(np.isnan(v) for v in stats)


def test_percentile_is_over_point_sources_only():
    stars = [_row(f) for f in np.linspace(1.8, 2.0, 60)]
    galaxies = [_row(4.0, elongation=1.8) for _ in range(120)]
    _, _, _, pctlpix = catalog_fwhm_stats(stars + galaxies, percentile=90.0)
    assert pctlpix == pytest.approx(np.percentile(np.linspace(1.8, 2.0, 60), 90.0))


def test_percentile_argument_is_respected():
    fwhm = np.linspace(1.5, 2.5, 100)
    rows = [_row(f) for f in fwhm]
    for percentile in (10.0, 50.0, 90.0):
        _, _, _, pctlpix = catalog_fwhm_stats(rows, percentile=percentile)
        assert pctlpix == pytest.approx(np.percentile(fwhm, percentile))


def test_percentile_falls_back_to_all_valid_sources():
    rows = ([_row(1.9) for _ in range(5)] + [_row(4.0, elongation=1.8) for _ in range(20)] +
            [_row(-1.0, elongation=1.8)])
    _, _, _, pctlpix = catalog_fwhm_stats(rows, percentile=90.0)
    assert pctlpix == pytest.approx(np.percentile([1.9] * 5 + [4.0] * 20, 90.0))


def test_percentile_is_required():
    with pytest.raises(TypeError):
        catalog_fwhm_stats([_row(1.9)])
