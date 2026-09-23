"""Tests for the star/galaxy cut on by-image fake-source injection anchors.

A synthetic image holds PSF-sized "stars" and broader "galaxies".  With star_galaxy_cut set,
every injection anchor must be a galaxy; without it, every detected source is eligible; a
num_injections larger than the surviving sample must clamp rather than raise.

Run with:  python -m pytest test/test_rapid_source_injections_galaxy_cut.py
"""

import os
import random
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, 'modules', 'fake_src'))

from rapid_source_injections import detect_sources_in_image, generate_injection_positions_fluxes


N_STARS = 15
N_GALAXIES = 15
IMAGE_SIZE = (400, 400)


def make_image():

    """Return (image, star_xy, galaxy_xy): isolated Gaussians on flat noise, sigma 1 px for
    stars and 3-5 px for galaxies, laid out on a 6x5 grid so nearest-neighbour is unambiguous."""

    rng = np.random.default_rng(0)
    image = rng.normal(0.0, 1.0, IMAGE_SIZE)
    yy, xx = np.mgrid[:IMAGE_SIZE[0], :IMAGE_SIZE[1]]

    grid = [(50 + 60 * i, 50 + 60 * j) for i in range(6) for j in range(5)]
    rng.shuffle(grid)
    star_xy = grid[:N_STARS]
    galaxy_xy = grid[N_STARS:N_STARS + N_GALAXIES]

    for x0, y0 in star_xy:
        image += 2000.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * 1.0 ** 2))
    for x0, y0 in galaxy_xy:
        sigma = rng.uniform(3.0, 5.0)
        image += 400.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma ** 2))

    return image, np.array(star_xy, float), np.array(galaxy_xy, float)


def nearest_is_galaxy(x, y, star_xy, galaxy_xy):

    """True if the closest true object to (x, y) is a galaxy."""

    d_star = np.min(np.hypot(star_xy[:, 0] - x, star_xy[:, 1] - y))
    d_gal = np.min(np.hypot(galaxy_xy[:, 0] - x, galaxy_xy[:, 1] - y))
    return d_gal < d_star


@pytest.fixture(scope='module')
def detections():

    image, star_xy, galaxy_xy = make_image()
    table = detect_sources_in_image(image, detection_nsigma=10, npixels=8, bkg_box_size=100)
    return table, star_xy, galaxy_xy


def test_r50_column_is_present_and_finite(detections):

    table, star_xy, galaxy_xy = detections

    assert 'r50' in table.colnames
    assert np.all(np.isfinite(table['r50'].value))
    assert len(table) == N_STARS + N_GALAXIES


def test_cut_selects_only_galaxies(detections):

    table, star_xy, galaxy_xy = detections
    random.seed(0)

    xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                           num_injections=10, star_galaxy_cut=1.3)

    assert len(xpix) == 10
    assert all(nearest_is_galaxy(x, y, star_xy, galaxy_xy) for x, y in zip(xpix, ypix))


def test_no_cut_uses_every_detected_source(detections):

    """With star_galaxy_cut=None the historical behaviour holds: all detected sources are anchors."""

    table, star_xy, galaxy_xy = detections
    random.seed(0)

    xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                           num_injections=N_STARS + N_GALAXIES)

    assert len(xpix) == N_STARS + N_GALAXIES
    assert sum(not nearest_is_galaxy(x, y, star_xy, galaxy_xy) for x, y in zip(xpix, ypix)) == N_STARS


def test_num_injections_clamps_to_surviving_sources(detections):

    table, star_xy, galaxy_xy = detections
    random.seed(0)

    with pytest.warns(UserWarning, match=r'only 15 sources pass the cuts; injecting 15 instead of 100'):
        xpix, ypix, flux = generate_injection_positions_fluxes(table, IMAGE_SIZE, zeropoint=25.0,
                                                               num_injections=100, star_galaxy_cut=1.3)

    assert len(xpix) == N_GALAXIES
    assert all(nearest_is_galaxy(x, y, star_xy, galaxy_xy) for x, y in zip(xpix, ypix))
