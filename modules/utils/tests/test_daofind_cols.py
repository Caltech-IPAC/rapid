"""
Tests of the PhotUtils-style catalog columns: the snapping of source pixels to the peak of the
DAOStarFinder-convolved image, the bad-core check, and the PSFPhotometry xy_bounds parameter
(modules/utils/rapid_data_analysis.py and modules/utils/rapid_pipeline_subs.py).

Run from the repository root:  python -m pytest modules/utils/tests
"""

import warnings

import numpy as np
import pytest
from astropy.io import fits

import modules.utils.rapid_data_analysis as rda
import modules.utils.rapid_pipeline_subs as util


FWHM = 2.0                  # DAOStarFinder kernel FWHM [pixels], as in [PSFCAT_DIFFIMAGE]
PSF_FWHM = 1.6              # FWHM of the synthetic stars [pixels]
SIGMA = PSF_FWHM / 2.3548
FILL = -999.0


def gaussian_image(shape, sources, sigma=SIGMA):
    """Sum of circular Gaussians, given as (x, y, peak) with zero-based x, y."""
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    image = np.zeros(shape)
    for x0, y0, peak in sources:
        image += peak * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma ** 2))
    return image


def write_fits(path, data):
    fits.PrimaryHDU(np.asarray(data, dtype=np.float32)).writeto(path, overwrite=True)
    return str(path)


def write_psf(path, size=25):
    c = (size - 1) / 2
    psf = gaussian_image((size, size), [(c, c, 1.0)])
    return write_fits(path, psf / psf.sum())


def raw_daofind(data, xpix, ypix):
    """Sharpness and roundness straight from DAOStarFinder at the given pixels."""
    finder = rda.make_unfiltered_daofinder(FWHM)
    finder.xycoords = np.column_stack((xpix, ypix)).astype(int)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cat = finder._get_raw_catalog(data)
    return {name: np.asarray(getattr(cat, name), dtype=float)
            for name in ("sharpness", "roundness1", "roundness2")}


def dao_cols(tmp_path, data, xy_zero_based, snap_radius=1, **kwargs):
    """compute_photutils_cols_for_diff_image on an image, with zero-based positions."""
    image = write_fits(tmp_path / "diff.fits", data)
    psf = write_psf(tmp_path / "psf.fits")
    return rda.compute_photutils_cols_for_diff_image(
        image, psf, xy_zero_based, coord_base=0, fwhm=FWHM, fit_shape=(9, 9),
        aperture_radius=3, snap_radius=snap_radius, fill_value=FILL, **kwargs)


#-------------------------------------------------------------------
# Snapping.

def test_snap_moves_off_peak_pixels_to_the_convolved_peak():
    data = gaussian_image((41, 41), [(20.2, 19.8, 100.0)])
    xs, ys = rda.snap_to_daofind_peak(data, np.array([19, 21, 20, 21]), np.array([20, 21, 19, 19]),
                                      FWHM, snap_radius=1)
    assert np.all(xs == 20) and np.all(ys == 20)


def test_snap_radius_0_and_ties_leave_pixels_unchanged():
    data = gaussian_image((41, 41), [(20.0, 20.0, 100.0)])
    xs, ys = rda.snap_to_daofind_peak(data, np.array([19]), np.array([20]), FWHM, snap_radius=0)
    assert (xs[0], ys[0]) == (19, 20)

    flat = np.ones((21, 21))
    xs, ys = rda.snap_to_daofind_peak(flat, np.array([10]), np.array([10]), FWHM, snap_radius=1)
    assert (xs[0], ys[0]) == (10, 10)


def test_snap_at_image_corner():
    data = gaussian_image((21, 21), [(0.0, 1.0, 100.0)])
    xs, ys = rda.snap_to_daofind_peak(data, np.array([0, 1]), np.array([0, 0]), FWHM, snap_radius=1)
    assert np.all((xs >= 0) & (ys >= 0))


def test_snapped_values_match_daostarfinder_at_its_peak(tmp_path):
    """An input position a pixel off gives DAOStarFinder's values at the true peak."""
    rng = np.random.default_rng(1)
    data = gaussian_image((41, 41), [(20.0, 20.0, 30.0)]) + rng.normal(0, 1, (41, 41))
    expected = raw_daofind(data, [20], [20])

    cols = dao_cols(tmp_path, data, [(19.4, 20.0), (20.0, 20.6)])
    for name in expected:
        assert cols[name] == pytest.approx([expected[name][0]] * 2, abs=1e-5)

    cols_nosnap = dao_cols(tmp_path, data, [(19.4, 20.0)], snap_radius=0)
    off_peak = raw_daofind(data, [19], [20])
    assert cols_nosnap["roundness1"][0] == pytest.approx(off_peak["roundness1"][0], abs=1e-5)
    assert cols_nosnap["roundness1"][0] != pytest.approx(expected["roundness1"][0], abs=1e-3)


#-------------------------------------------------------------------
# Snapped peak pixel and dao_flags.

def test_dao_peak_columns_follow_coord_base(tmp_path):
    data = gaussian_image((41, 41), [(20.0, 20.0, 100.0)])
    cols = dao_cols(tmp_path, data, [(19.4, 20.0)])
    assert (cols["x_dao_peak"][0], cols["y_dao_peak"][0]) == (20.0, 20.0)

    image = write_fits(tmp_path / "diff.fits", data)
    psf = write_psf(tmp_path / "psf.fits")
    cols1 = rda.compute_photutils_cols_for_diff_image(
        image, psf, [(20.4, 21.0)], coord_base=1, fwhm=FWHM, fit_shape=(9, 9), aperture_radius=3,
        fill_value=FILL)
    assert (cols1["x_dao_peak"][0], cols1["y_dao_peak"][0]) == (21.0, 21.0)
    assert cols1["sharpness"][0] == pytest.approx(cols["sharpness"][0], abs=1e-6)


@pytest.mark.parametrize("offset, flag", [((0, 0), 3), ((1, 0), 3), ((0, -1), 3),
                                          ((1, 1), 0), ((2, 0), 0), ((0, 3), 0)])
def test_bad_core_pixels_set_dao_flags(tmp_path, offset, flag):
    data = gaussian_image((41, 41), [(20.0, 20.0, 100.0)])
    data[20 + offset[1], 20 + offset[0]] = np.nan

    cols = dao_cols(tmp_path, data, [(20.0, 20.0)])

    # A NaN on the peak pulls the snap away, so the core around the nearest pixel (bit 2) catches
    # it even where the snapped core (bit 1) does not; check bit 2 always, and the exact value
    # where the snap stays put.
    assert (int(cols["dao_flags"][0]) & 2 == 2) == (flag != 0)
    if (cols["x_dao_peak"][0], cols["y_dao_peak"][0]) == (20.0, 20.0):
        assert int(cols["dao_flags"][0]) == flag

    # The values are kept, and the PSF fit masks the bad pixel rather than skipping the source.
    assert cols["sharpness"][0] != FILL
    assert cols["flux_fit"][0] != FILL


def test_nan_at_true_peak_snapped_diagonally_sets_bit_2(tmp_path):
    """The case bit 2 exists for: the snap leaves a bad peak diagonally, out of reach of bit 1."""
    data = gaussian_image((41, 41), [(20.3, 20.3, 100.0)])
    data[20, 20] = np.nan
    data[21, 21] += 50.0                    # make the diagonal neighbor win the snap

    cols = dao_cols(tmp_path, data, [(20.0, 20.0)])
    assert (cols["x_dao_peak"][0], cols["y_dao_peak"][0]) == (21.0, 21.0)
    assert int(cols["dao_flags"][0]) == 2


def test_peak_on_image_edge_sets_bit_4(tmp_path):
    data = gaussian_image((41, 41), [(20.0, 0.0, 100.0)])
    cols = dao_cols(tmp_path, data, [(20.0, 0.0)])
    assert int(cols["dao_flags"][0]) == 4
    assert cols["sharpness"][0] != FILL


def test_invalid_position_gets_fill_value_everywhere(tmp_path):
    data = gaussian_image((41, 41), [(20.0, 20.0, 100.0)])
    cols = dao_cols(tmp_path, data, [(np.nan, 20.0), (60.0, 20.0)])
    for name in rda.photutils_col_names:
        assert cols[name] == [FILL, FILL]


#-------------------------------------------------------------------
# PSFPhotometry xy_bounds.

def test_xy_bounds(tmp_path):
    rng = np.random.default_rng(2)
    data = gaussian_image((41, 41), [(20.0, 20.0, 50.0)]) + rng.normal(0, 1, (41, 41))
    unc = write_fits(tmp_path / "unc.fits", np.ones((41, 41)))

    # Started 1.2 pixels off: free, the fit finds the source; bounded at 0.5 pixel, it is held
    # at the bound (PSFPhotometry flag 32) and recovers less flux.
    free = dao_cols(tmp_path, data, [(21.2, 20.0)], input_unc_filename=unc)
    bounded = dao_cols(tmp_path, data, [(21.2, 20.0)], input_unc_filename=unc, xy_bounds=0.5)

    assert int(free["flags"][0]) & 32 == 0
    assert int(bounded["flags"][0]) & 32 == 32
    assert bounded["flux_fit"][0] < free["flux_fit"][0]

    default = dao_cols(tmp_path, data, [(21.2, 20.0)], input_unc_filename=unc, xy_bounds=None)
    assert default == free


@pytest.mark.parametrize("text, value", [("None", None), (" none ", None), ("1", 1.0),
                                         ("0.5", 0.5), ("(1, 2)", (1.0, 2.0)),
                                         ("(1, None)", (1.0, None))])
def test_parse_xy_bounds(text, value):
    assert util.parse_xy_bounds(text) == value


def test_parse_xy_bounds_rejects_three_values():
    with pytest.raises(ValueError):
        util.parse_xy_bounds("(1, 2, 3)")


#-------------------------------------------------------------------
# compute_extra_cols_sxtractor, with configs written before and after the new parameters.

def write_sextractor_catalog(path, xy_one_based):
    lines = ["#   1 NUMBER                 Running object number",
             "#   2 XWIN_IMAGE             Windowed position estimate along x   [pixel]",
             "#   3 YWIN_IMAGE             Windowed position estimate along y   [pixel]",
             "#   4 FLAGS                  Extraction flags"]
    for i, (x, y) in enumerate(xy_one_based):
        lines.append(f"{i + 1:10d} {x:11.4f} {y:11.4f} {0:3d}")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


@pytest.mark.parametrize("new_keys", [True, False])
def test_compute_extra_cols_sxtractor(tmp_path, new_keys):
    rng = np.random.default_rng(3)
    data = gaussian_image((61, 61), [(20.0, 20.0, 30.0), (40.0, 35.0, 30.0)]) \
        + rng.normal(0, 1, (61, 61))
    data[35, 41] = np.nan                                   # next to the second peak

    image = write_fits(tmp_path / "diff.fits", data)
    unc = write_fits(tmp_path / "unc.fits", np.ones(data.shape))
    psf = write_psf(tmp_path / "psf.fits")

    # One-based SExtractor positions, the first 0.6 pixel off its peak.
    catalog = write_sextractor_catalog(tmp_path / "cat.txt", [(20.4, 21.0), (41.0, 36.0)])

    extra_cols_dict = {"extra_cols": "sumrat",
                     "sextractor_x_col": "XWIN_IMAGE", "sextractor_y_col": "YWIN_IMAGE",
                     "sxtractor_photutils_cols": "sharpness, roundness1, roundness2, x_dao_peak, "
                                                 "y_dao_peak, dao_flags, flux_fit, flags",
                     "photutils_cols_fill_value": "-999.0", "photutils_cols_format": ".6f"}
    if new_keys:
        extra_cols_dict["photutils_cols_snap_radius"] = "1"
        extra_cols_dict["photutils_cols_xy_bounds"] = "None"

    sumrat_dict = {"stamp_size": "5", "filter_size": "3", "lower_median": "False",
                   "fill_value": "-999.0", "col_format": ".6f"}
    psfcat_dict = {"fwhm": "2.0", "fit_shape": "(9, 9)", "aperture_radius": "3"}

    extra_cols = util.compute_extra_cols_sxtractor(image, catalog, extra_cols_dict, sumrat_dict,
                                               diff_psf_filename=psf, diff_unc_filename=unc,
                                               psfcat_dict=psfcat_dict)

    expected = raw_daofind(np.where(np.isfinite(data), data, 0.0), [20], [20])
    assert extra_cols["sharpness"][0] == pytest.approx(expected["sharpness"][0], abs=1e-5)
    assert extra_cols["roundness1"][0] == pytest.approx(expected["roundness1"][0], abs=1e-5)
    assert (extra_cols["x_dao_peak"][0], extra_cols["y_dao_peak"][0]) == (21.0, 21.0)
    assert extra_cols["dao_flags"][0] == 0
    assert int(extra_cols["dao_flags"][1]) & 3 != 0
    assert extra_cols["sharpness"][1] != FILL
    assert extra_cols["flux_fit"][1] != FILL

    header = [line for line in open(catalog) if line.startswith("#")]
    for name in ("SHARPNESS", "X_DAO_PEAK", "Y_DAO_PEAK", "DAO_FLAGS", "FLAGS_FIT"):
        assert any(f" {name} " in line for line in header)

    # Integer columns are written without decimals.
    row = open(catalog).read().splitlines()[-1].split()
    names = [line.split()[2] for line in header]
    assert row[names.index("DAO_FLAGS")] == str(int(extra_cols["dao_flags"][1]))
    assert row[names.index("X_DAO_PEAK")] == "41"
