"""
Tests of the nneg and nbad catalog columns (compute_nneg_nbad_for_diff_image in
modules/utils/rapid_data_analysis.py, wired in through _compute_extra_catalog_cols in
modules/utils/rapid_pipeline_subs.py).

Run from the repository root:  python -m pytest modules/utils/tests
"""

import numpy as np
import pytest
from astropy.io import fits

import modules.utils.rapid_data_analysis as rda
import modules.utils.rapid_pipeline_subs as util


FILL = -999.0


def write_fits(path, data):
    fits.PrimaryHDU(np.asarray(data, dtype=np.float32)).writeto(path, overwrite=True)
    return str(path)


def stamp_image():
    """A 21x21 image of ones with a known 5x5 stamp around zero-based pixel (10, 10)."""
    data = np.ones((21, 21))
    data[8, 8] = -1.0           # negative
    data[8, 12] = -0.5          # negative
    data[12, 10] = -3.0         # negative
    data[10, 11] = np.nan       # bad
    data[11, 9] = np.inf        # bad
    data[9, 9] = 0.0            # zero: neither negative nor bad
    data[10, 13] = -5.0         # negative, but outside the 5x5 stamp
    return data


def test_counts_in_hand_built_stamp(tmp_path):
    image = write_fits(tmp_path / "diff.fits", stamp_image())
    nneg, nbad = rda.compute_nneg_nbad_for_diff_image(image, [(10.0, 10.0)], coord_base=0)
    assert (nneg, nbad) == ([3.0], [2.0])


def test_nearest_pixel_and_coord_base(tmp_path):
    image = write_fits(tmp_path / "diff.fits", stamp_image())

    # Zero-based (10.4, 9.6) and one-based (11.4, 10.6) both round to zero-based pixel (10, 10).
    zero = rda.compute_nneg_nbad_for_diff_image(image, [(10.4, 9.6)], coord_base=0)
    one = rda.compute_nneg_nbad_for_diff_image(image, [(11.4, 10.6)], coord_base=1)
    assert zero == one == ([3.0], [2.0])

    # One pixel to the right, the stamp takes in data[10, 13] and loses data[8, 8].
    shifted = rda.compute_nneg_nbad_for_diff_image(image, [(11.0, 10.0)], coord_base=0)
    assert shifted == ([3.0], [2.0])
    shifted2 = rda.compute_nneg_nbad_for_diff_image(image, [(9.0, 10.0)], coord_base=0)
    assert shifted2 == ([2.0], [2.0])


def test_stamp_size(tmp_path):
    image = write_fits(tmp_path / "diff.fits", stamp_image())
    nneg, nbad = rda.compute_nneg_nbad_for_diff_image(image, [(10.0, 10.0)], coord_base=0,
                                                       stamp_size=3)
    assert (nneg, nbad) == ([0.0], [2.0])

    with pytest.raises(ValueError):
        rda.compute_nneg_nbad_for_diff_image(image, [(10.0, 10.0)], coord_base=0, stamp_size=4)


def test_invalid_positions_get_fill_value(tmp_path):
    image = write_fits(tmp_path / "diff.fits", stamp_image())
    # Not finite; stamp off the left edge; stamp off the right edge; stamp just inside the bottom
    # edge (rows 0-4, all ones); the hand-built stamp.
    xy = [(np.nan, 10.0), (1.0, 10.0), (19.0, 10.0), (10.0, 2.0), (10.0, 10.0)]
    nneg, nbad = rda.compute_nneg_nbad_for_diff_image(image, xy, coord_base=0, fill_value=FILL)
    assert nneg == [FILL, FILL, FILL, 0.0, 3.0]
    assert nbad == [FILL, FILL, FILL, 0.0, 2.0]


def test_pure_noise_is_about_half_negative(tmp_path):
    rng = np.random.default_rng(1)
    image = write_fits(tmp_path / "diff.fits", rng.normal(0, 1, (201, 201)))
    xy = [(float(x), float(y)) for x in range(10, 191, 10) for y in range(10, 191, 10)]
    nneg, nbad = rda.compute_nneg_nbad_for_diff_image(image, xy, coord_base=0)
    assert np.mean(nneg) == pytest.approx(12.5, abs=0.5)
    assert set(nbad) == {0.0}


#-------------------------------------------------------------------
# Through the catalog umbrella methods.

def write_sextractor_catalog(path, xy_one_based):
    lines = ["#   1 NUMBER                 Running object number",
             "#   2 XWIN_IMAGE             Windowed position estimate along x   [pixel]",
             "#   3 YWIN_IMAGE             Windowed position estimate along y   [pixel]"]
    for i, (x, y) in enumerate(xy_one_based):
        lines.append(f"{i + 1:10d} {x:11.4f} {y:11.4f}")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


SUMRAT_DICT = {"stamp_size": "5", "filter_size": "3", "lower_median": "False",
               "fill_value": "-999.0", "col_format": ".6f"}


@pytest.mark.parametrize("new_keys", [True, False])
def test_compute_extra_cols_sxtractor(tmp_path, new_keys):
    image = write_fits(tmp_path / "diff.fits", stamp_image())
    catalog = write_sextractor_catalog(tmp_path / "cat.txt", [(11.0, 11.0), (2.0, 11.0)])

    extra_cols_dict = {"extra_cols": "sumrat, nneg, nbad",
                     "sextractor_x_col": "XWIN_IMAGE", "sextractor_y_col": "YWIN_IMAGE"}
    if new_keys:
        extra_cols_dict["nneg_nbad_stamp_size"] = "5"
        extra_cols_dict["nneg_nbad_fill_value"] = "-999.0"

    extra_cols = util.compute_extra_cols_sxtractor(image, catalog, extra_cols_dict, SUMRAT_DICT)
    assert list(extra_cols) == ["sumrat", "nneg", "nbad"]
    assert extra_cols["nneg"] == [3.0, FILL] and extra_cols["nbad"] == [2.0, FILL]

    lines = open(catalog).read().splitlines()
    header = [line for line in lines if line.startswith("#")]
    names = [line.split()[2] for line in header]
    assert names[-2:] == ["NNEG", "NBAD"]
    row = lines[len(header)].split()
    assert row[names.index("NNEG")] == "3" and row[names.index("NBAD")] == "2"
    assert lines[-1].split()[names.index("NNEG")] == "-999"


def test_compute_extra_cols_photutils(tmp_path):
    image = write_fits(tmp_path / "diff.fits", stamp_image())
    catalog = tmp_path / "psfcat.txt"
    catalog.write_text("id x_fit y_fit flux_fit\n1 10.0 10.0 5.0\n")

    extra_cols_dict = {"extra_cols": "nbad, nneg", "photutils_x_col": "x_fit", "photutils_y_col": "y_fit"}
    extra_cols = util.compute_extra_cols_photutils(image, str(catalog), extra_cols_dict, SUMRAT_DICT)
    assert extra_cols == {"nbad": [2.0], "nneg": [3.0]}

    lines = catalog.read_text().splitlines()
    assert lines[0].split()[-2:] == ["nbad", "nneg"]
    assert lines[1].split()[-2:] == ["2", "3"]
