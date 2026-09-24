"""Tests for the star/galaxy and saturation cuts on by-image fake-source injection anchors.

A synthetic image holds PSF-sized "stars", broader "galaxies" and one saturated (flat-topped)
star.  With star_galaxy_cut set, every injection anchor must be a galaxy except the saturated
star, whose inflated half-light radius leaks through; saturation_level removes it.  Without any
cut every detected source is eligible, and a num_injections larger than the surviving sample
clamps with a warning rather than raising.

Run with:  python -m pytest test/test_rapid_source_injections_galaxy_cut.py
"""

import os
import random
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, 'modules', 'fake_src'))

from rapid_source_injections import detect_sources_in_image, generate_injection_positions_fluxes


N_STARS = 15          # unsaturated
N_GALAXIES = 15
N_SAT = 1             # saturated star, counted as a star
SAT_LEVEL = 5000.0    # above every unsaturated star's 2000 peak
IMAGE_SIZE = (450, 450)


def make_image():

    """Return (image, star_xy, galaxy_xy, sat_xy): isolated Gaussians on flat noise, sigma 1 px for
    stars and 3-5 px for galaxies, on a 7x5 grid so nearest-neighbour is unambiguous.  star_xy
    includes the saturated star, which is hard-clipped at SAT_LEVEL."""

    rng = np.random.default_rng(0)
    image = rng.normal(0.0, 1.0, IMAGE_SIZE)
    yy, xx = np.mgrid[:IMAGE_SIZE[0], :IMAGE_SIZE[1]]

    grid = [(50 + 60 * i, 50 + 60 * j) for i in range(7) for j in range(5)]
    rng.shuffle(grid)
    star_xy = grid[:N_STARS]
    galaxy_xy = grid[N_STARS:N_STARS + N_GALAXIES]
    sat_xy = grid[N_STARS + N_GALAXIES]

    for x0, y0 in star_xy:
        image += 2000.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * 1.0 ** 2))
    for x0, y0 in galaxy_xy:
        sigma = rng.uniform(3.0, 5.0)
        image += 400.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma ** 2))

    x0, y0 = sat_xy
    image += 200000.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * 1.0 ** 2))
    box = (slice(y0 - 10, y0 + 11), slice(x0 - 10, x0 + 11))
    image[box] = np.minimum(image[box], SAT_LEVEL)       # flat top, exactly at SAT_LEVEL

    return image, np.array(star_xy + [sat_xy], float), np.array(galaxy_xy, float), np.array(sat_xy, float)


def nearest_is_galaxy(x, y, star_xy, galaxy_xy):

    """True if the closest true object to (x, y) is a galaxy."""

    d_star = np.min(np.hypot(star_xy[:, 0] - x, star_xy[:, 1] - y))
    d_gal = np.min(np.hypot(galaxy_xy[:, 0] - x, galaxy_xy[:, 1] - y))
    return d_gal < d_star


def nearest_is_saturated(x, y, sat_xy, star_xy, galaxy_xy):

    """True if the closest true object to (x, y) is the saturated star."""

    d_sat = np.hypot(sat_xy[0] - x, sat_xy[1] - y)
    d_other = min(np.min(np.hypot(galaxy_xy[:, 0] - x, galaxy_xy[:, 1] - y)),
                  np.min(np.hypot(star_xy[:-1, 0] - x, star_xy[:-1, 1] - y)))
    return d_sat < d_other


@pytest.fixture(scope='module')
def detections():

    image, star_xy, galaxy_xy, sat_xy = make_image()
    table = detect_sources_in_image(image, detection_nsigma=10, npixels=8, bkg_box_size=100)
    return table, star_xy, galaxy_xy, sat_xy


def test_r50_and_peak_columns_are_present(detections):

    table, star_xy, galaxy_xy, sat_xy = detections

    assert 'r50' in table.colnames and 'peak' in table.colnames
    assert np.all(np.isfinite(table['r50'].value))
    assert len(table) == N_STARS + N_GALAXIES + N_SAT

    d = np.hypot(table['x_centroid'].value - sat_xy[0], table['y_centroid'].value - sat_xy[1])
    assert table['peak'].value[np.argmin(d)] == SAT_LEVEL          # raw, not background-subtracted
    assert np.sum(table['peak'].value >= SAT_LEVEL) == N_SAT


def test_cut_selects_only_galaxies(detections):

    table, star_xy, galaxy_xy, sat_xy = detections
    random.seed(0)

    xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                           num_injections=10, star_galaxy_cut=1.3,
                                                           saturation_level=SAT_LEVEL)

    assert len(xpix) == 10
    assert all(nearest_is_galaxy(x, y, star_xy, galaxy_xy) for x, y in zip(xpix, ypix))


def test_no_cut_uses_every_detected_source(detections):

    """With no cuts the historical behaviour holds: all detected sources are anchors."""

    table, star_xy, galaxy_xy, sat_xy = detections
    random.seed(0)
    n_all = N_STARS + N_GALAXIES + N_SAT

    xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                           num_injections=n_all)

    assert len(xpix) == n_all
    assert sum(not nearest_is_galaxy(x, y, star_xy, galaxy_xy) for x, y in zip(xpix, ypix)) == N_STARS + N_SAT


def test_saturated_star_leaks_through_galaxy_cut(detections):

    """Documents the leak: the flat-topped star's inflated r50 passes the galaxy cut alone."""

    table, star_xy, galaxy_xy, sat_xy = detections
    random.seed(0)

    with pytest.warns(UserWarning):
        xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                               num_injections=100, star_galaxy_cut=1.3)

    assert len(xpix) == N_GALAXIES + N_SAT
    assert any(nearest_is_saturated(x, y, sat_xy, star_xy, galaxy_xy) for x, y in zip(xpix, ypix))


def test_saturation_cut_removes_saturated_star(detections):

    table, star_xy, galaxy_xy, sat_xy = detections
    random.seed(0)

    with pytest.warns(UserWarning, match=r'only 15 sources pass the cuts; injecting 15 instead of 100'):
        xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                               num_injections=100, star_galaxy_cut=1.3,
                                                               saturation_level=SAT_LEVEL)

    assert len(xpix) == N_GALAXIES
    assert not any(nearest_is_saturated(x, y, sat_xy, star_xy, galaxy_xy) for x, y in zip(xpix, ypix))
    assert all(nearest_is_galaxy(x, y, star_xy, galaxy_xy) for x, y in zip(xpix, ypix))
