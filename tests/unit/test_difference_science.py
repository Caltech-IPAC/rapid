"""Tests for rapidpipe.science.difference, one step at a time.

Each test checks a step against `dev`'s behaviour on small synthetic
images: the same formula, the same command line, the same file
conventions. External tools are faked (tests/unit/fakedifftools.py);
photutils is exercised for real where it is installed.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from rapidpipe.science.difference import (
    background,
    gainmatch,
    masking,
    naive,
    offsets,
    psf,
    reformat,
    repair,
    resample,
    sextractor,
    sfft,
    statistics,
    uncertainty,
    zogy,
)
from rapidpipe.science.difference.fitsops import scale_image_data

from .fakedifftools import CDF_DIR, FakeToolRunner, fake_sip_to_pv, wcs_header


def _write(path: Path, data, header=None) -> Path:
    fits.PrimaryHDU(data=np.asarray(data), header=header).writeto(path, overwrite=True)
    return path


def _read(path: Path) -> np.ndarray:
    return np.array(fits.getdata(path), dtype=np.float64)


# ----------------------------------------------------------------------
# statistics
# ----------------------------------------------------------------------


def test_clip_correction_is_seedable_and_near_devs_value():
    a = statistics.compute_clip_corr(3.0, seed=7)
    b = statistics.compute_clip_corr(3.0, seed=7)
    assert a == b
    # The variance of a 3-sigma-clipped unit normal is ~0.973.
    assert 1.02 < a < 1.035


def test_clipped_statistics_match_a_direct_computation(tmp_path):
    rng = np.random.default_rng(1)
    data = rng.normal(10.0, 2.0, size=(50, 50))
    data[0, 0] = np.nan
    data[1, 1] = 1.0e6
    path = _write(tmp_path / "img.fits", data)
    stats = statistics.fits_data_statistics_with_clipping(path, 3.0, 0, 50000.0, seed=3)

    med = np.nanmedian(data)
    sigma = 0.5 * (np.nanpercentile(data, 84) - np.nanpercentile(data, 16))
    keep = np.isfinite(data) & (data >= med - 3 * sigma) & (data <= med + 3 * sigma)
    assert stats["clippedavg"] == pytest.approx(data[keep].mean())
    assert stats["clippedstd"] == pytest.approx(
        data[keep].std() * np.sqrt(statistics.compute_clip_corr(3.0, seed=3)))
    assert stats["nkept"] == keep.sum()
    assert stats["nancount"] == 1
    assert stats["satcount"] == 1
    assert stats["noutliers"] == 2500 - 1 - keep.sum()


# ----------------------------------------------------------------------
# reformat
# ----------------------------------------------------------------------


def test_reformat_pads_normalises_and_models_uncertainty(tmp_path):
    data = np.arange(16, dtype=np.float32).reshape(4, 4) + 100.0
    header = fits.Header()
    header["EXPTIME"] = 10.0
    src = tmp_path / "sim.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data=data, header=header)]).writeto(src)

    out, unc = tmp_path / "r.fits", tmp_path / "u.fits"
    reformat.reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model(
        src, 2.0, 9.4, 50.0, out, unc)

    with fits.open(out) as hdul:
        assert hdul[0].header["BUNIT"] == "DN/s"
        r = hdul[0].data
    assert r.shape == (5, 5) and r.dtype.kind == "f" and r.dtype.itemsize == 4
    assert r[4, 0] == pytest.approx(5.0) and r[0, 4] == pytest.approx(5.0)   # 50 / 10
    assert r[1, 2] == pytest.approx(106.0 / 10.0)
    u = fits.getdata(unc)
    expected = np.sqrt(10.6 * 10.0 / 2.0 + 9.4 ** 2 / 2.0 ** 2) / 10.0
    assert u[1, 2] == pytest.approx(expected, rel=1e-6)


# ----------------------------------------------------------------------
# sextractor
# ----------------------------------------------------------------------


def _sex_dict() -> dict:
    import tomllib

    with open(Path(__file__).resolve().parents[2] / "rapidpipe/settings/difference.toml", "rb") as fh:
        table = dict(tomllib.load(fh)["sextractor_diffimage"])
    table["sextractor_detection_image"] = "None"
    return table


def test_sextractor_command_follows_devs_option_order():
    d = _sex_dict()
    d["sextractor_input_image"] = "in.fits"
    args = sextractor.build_sextractor_command_line_args(d)
    assert args[:2] == ["sex", "in.fits"]
    options = args[2::2]
    assert options[0] == "-CATALOG_NAME" and options[-1] == "-INTERP_TYPE"
    assert "-FLAG_IMAGE" not in options and "-FLAG_TYPE" not in options
    assert len(options) == 63
    assert options[options.index("-WEIGHT_TYPE") - 1] == "-MASK_TYPE"

    d["sextractor_detection_image"] = "det.fits"
    assert sextractor.build_sextractor_command_line_args(d)[1] == "det.fits,in.fits"


def test_sextractor_command_needs_the_flag_keys_dev_reads():
    d = _sex_dict()
    d["sextractor_input_image"] = "in.fits"
    del d["sextractor_flag_type"]
    with pytest.raises(KeyError):
        sextractor.build_sextractor_command_line_args(d)


def test_parse_catalog_by_translated_parameter_names(tmp_path):
    params = tmp_path / "p.inp"
    params.write_text("XWIN_IMAGE\nYWIN_IMAGE\nFLUX_APER(6)\n")
    catalog = tmp_path / "c.txt"
    catalog.write_text("#   1 XWIN_IMAGE\n#   2 YWIN_IMAGE\n#   3 FLUX_APER\n1.5 2.5 300\n4 5 6\n")
    rows = sextractor.parse_ascii_text_sextractor_catalog(catalog, params, ["FLUX_APER_6", "XWIN_IMAGE"])
    assert rows == [["300", "1.5"], ["6", "4"]]


@pytest.mark.parametrize("values,expected", [
    (["2.5", "3.5", "3.0"], 3.0),
    (["nan", "nan"], 2.0),
    (["-1.0", "-3.0"], 2.0),
])
def test_fwhm_falls_back_to_two_pixels(tmp_path, values, expected):
    params = tmp_path / "p.inp"
    params.write_text("FWHM_IMAGE\n")
    catalog = tmp_path / "c.txt"
    catalog.write_text("#   1 FWHM_IMAGE\n" + "".join(v + "\n" for v in values))
    with np.errstate(all="ignore"), pytest.warns(RuntimeWarning) if "nan" in values else _null():
        fwhm, n = sextractor.fwhm_from_catalog(catalog, params, "x")
    assert fwhm == expected and n == len(values)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ----------------------------------------------------------------------
# resample
# ----------------------------------------------------------------------


def _swarp_dict() -> dict:
    import tomllib

    with open(Path(__file__).resolve().parents[2] / "rapidpipe/settings/difference.toml", "rb") as fh:
        return dict(tomllib.load(fh)["swarp"])


def test_swarp_command_keeps_devs_spelling():
    d = _swarp_dict()
    d.update(swarp_input_image="in.fits", swarp_imageout_name="o.fits", swarp_weightout_name="w.fits")
    args = resample.build_swarp_command_line_args(d)
    assert args[:4] == ["swarp", "in.fits", "-IMAGEOUT_NAME", "o.fits"]
    assert "-COMBINE_BUFSIZE " in args                      # dev's trailing space
    assert args[args.index("-COMBINE_BUFSIZE ") + 1] == "256"
    assert len(args) == 2 + 2 * 56


def test_resample_swarps_three_images_background_off_after_the_first(tmp_path):
    runner = FakeToolRunner()
    n = 11
    _write(tmp_path / "sci.fits", np.ones((n, n)), wcs_header(n, sip=True))
    for name in ("ref.fits", "cov.fits", "unc.fits"):
        _write(tmp_path / name, np.full((n, n), 3.0), wcs_header(n, sip=False))
    names = resample.resample_reference_image_to_science_image_with_pv_distortion(
        runner, tmp_path, "sci.fits", 0, "ref.fits", "cov.fits", "unc.fits", 0, False,
        _swarp_dict(), sip_to_pv=fake_sip_to_pv)
    assert names == ("sci_pv.fits", "ref_pv.fits", "cov_pv.fits", "unc_pv.fits",
                     "ref_resampled.fits", "cov_resampled.fits", "unc_resampled.fits")
    assert fits.getheader(tmp_path / "sci_pv.fits")["CTYPE1"] == "RA---TPV"
    assert os.readlink(tmp_path / "ref_resampled.head") == "sci_pv.fits"
    inputs = [c[1] for c in runner.calls]
    assert inputs == ["ref.fits", "cov.fits", "unc.fits"]
    subtract = [c[c.index("-SUBTRACT_BACK") + 1] for c in runner.calls]
    back_type = [c[c.index("-BACK_TYPE") + 1] for c in runner.calls]
    assert subtract == ["Y", "N", "N"]
    assert back_type == ["AUTO", "MANUAL", "MANUAL"]


# ----------------------------------------------------------------------
# background
# ----------------------------------------------------------------------


def test_bkgest_command_is_devs():
    table = {"output_image_type": "2", "clippedmean_calc_type": "3",
             "local_clippedmean_grid_spacing": "500", "local_clippedmean_input_window": "501",
             "filename_global_clippedmean_sciimage_tbl": "g.tbl",
             "filename_bkg_subbed_science_image": "bkg.fits"}
    assert background.build_bkgest_command("/code/c/bin/bkgest", "/code/c/include", "in.fits", table) == [
        "/code/c/bin/bkgest", "-i", "in.fits", "-f", "2", "-c", "3", "-g", "500", "-w", "501",
        "-a", "/code/c/include", "-ot", "g.tbl", "-o2", "bkg.fits"]


# ----------------------------------------------------------------------
# gain matching
# ----------------------------------------------------------------------


def test_source_match_offsets_are_reference_minus_science():
    xs = [10.0, 20.0, 30.0, 40.0]
    ys = [10.0, 20.0, 30.0, 40.0]
    xr = np.array([10.2, 20.2, 30.2, 80.0])
    yr = np.array([9.9, 19.9, 29.9, 80.0])
    mdnear, flux, nmtch, dxrms, dyrms, dxmed, dymed = gainmatch.source_match_ref_sci(
        xs, ys, xr, yr, [1.0, 2.0, 3.0, 4.0], 4, 1.0, 0)
    assert nmtch == 3
    assert flux == [1.0, 2.0, 3.0, -999]
    assert dxmed == pytest.approx(0.2) and dymed == pytest.approx(-0.1)
    assert dxrms == pytest.approx(0.2) and dyrms == pytest.approx(0.1)


def test_source_match_below_three_matches_reports_zeros():
    *_, dxrms, dyrms, dxmed, dymed = gainmatch.source_match_ref_sci(
        [1.0, 2.0], [1.0, 2.0], np.array([1.1, 2.1]), np.array([1.0, 2.0]), [1.0, 1.0], 2, 1.0, 0)
    assert (dxrms, dyrms, dxmed, dymed) == (0.0, 0.0, 0.0, 0.0)


class _CatalogRunner(FakeToolRunner):
    """Writes given rows as the two gain-match catalogs."""

    def __init__(self, sci_rows, ref_rows):
        super().__init__()
        self.rows = [sci_rows, ref_rows]

    def run(self, args, *, cwd):
        self.calls.append(list(args))
        params = [p.strip() for p in Path(args[args.index("-PARAMETERS_NAME") + 1]).read_text().splitlines()
                  if p.strip()]
        rows = self.rows[len(self.calls) - 1]
        lines = [f"#{i + 1:4d} {p}\n" for i, p in enumerate(params)]
        for row in rows:
            lines.append(" ".join(str(row.get(p.split("(")[0], 0.0)) for p in params) + "\n")
        (Path(cwd) / args[args.index("-CATALOG_NAME") + 1]).write_text("".join(lines))
        return 0


def _gainmatch_table(**overrides):
    table = dict(verbose=1, zero_point_sci_keyword="ZPTMAG", magrefthresmin=19.0, magrefthresmax=24.0,
                 refexclbox=10.5, refmagkeep=23.5, edgebuffer=5, numsrcgmatchmin=3,
                 radscirefmatch=1.0, min_class_star_thresh=0.5, max_isoareaf_image_thresh=100,
                 max_awin_to_bwin_world_ratio_thresh=1.3)
    table.update(overrides)
    return table


def _gainmatch_images(tmp_path, zptmag=17.5):
    header = wcs_header(100, sip=False)
    header["ZPTMAG"] = zptmag
    for name in ("sci.fits", "sciu.fits", "ref.fits", "refu.fits"):
        _write(tmp_path / name, np.zeros((100, 100), dtype=np.float32), header)


def test_gain_match_from_matched_fluxes(tmp_path):
    _gainmatch_images(tmp_path)
    # Reference sources at mag 20..22 (instrumental + zprefimg 17); science
    # fluxes half the reference fluxes, offset by (+0.2, -0.1).
    ref_rows, sci_rows = [], []
    for i, mag in enumerate((20.0, 21.0, 22.0, 20.5)):
        x, y = 20.0 + 15 * i, 30.0 + 10 * i
        flux_ref = 10 ** (0.4 * (17.0 - mag))
        ref_rows.append({"XWIN_IMAGE": x + 0.2, "YWIN_IMAGE": y - 0.1, "MAG_APER": mag - 17.0,
                         "CLASS_STAR": 0.9, "ISOAREAF_IMAGE": 10, "AWIN_WORLD": 1.0, "BWIN_WORLD": 1.0})
        sci_rows.append({"XWIN_IMAGE": x, "YWIN_IMAGE": y, "FLUX_APER": flux_ref / 2.0})
    runner = _CatalogRunner(sci_rows, ref_rows)
    scalefac, dxrms, dyrms, dxmed, dymed = gainmatch.gain_match_science_and_reference_images(
        runner, tmp_path, "sci.fits", "sciu.fits", "scicat.txt", "ref.fits", "refu.fits",
        "refcat.txt", 17.0, _gainmatch_table(), {**_sex_dict(), "sextractor_seeing_fwhm": "1.2"},
        2.1, 2.3, 0.05, 0.05, str(CDF_DIR))
    assert scalefac == pytest.approx(2.0)
    assert dxmed == pytest.approx(0.2) and dymed == pytest.approx(-0.1)
    assert dxrms == pytest.approx(0.2) and dyrms == pytest.approx(0.1)
    seeing = [c[c.index("-SEEING_FWHM") + 1] for c in runner.calls]
    assert seeing == ["2.1", "2.3"]
    assert all(c[1] in ("sci.fits", "ref.fits") for c in runner.calls)


def test_gain_match_falls_back_to_zero_points(tmp_path):
    _gainmatch_images(tmp_path, zptmag=17.5)
    runner = _CatalogRunner([], [])
    result = gainmatch.gain_match_science_and_reference_images(
        runner, tmp_path, "sci.fits", "sciu.fits", "scicat.txt", "ref.fits", "refu.fits",
        "refcat.txt", 17.0, _gainmatch_table(), _sex_dict(), 2.0, 2.0, 0.05, 0.07, str(CDF_DIR))
    assert result == pytest.approx((10 ** (0.4 * (17.0 - 17.5)), 0.05, 0.07, 0.0, 0.0))


# ----------------------------------------------------------------------
# repair
# ----------------------------------------------------------------------


def test_nans_replaced_then_restored(tmp_path):
    data = np.ones((5, 5), dtype=np.float32)
    data[1, 2] = np.nan
    data[3, 4] = np.nan
    path = _write(tmp_path / "i.fits", data)
    indices = repair.replace_nans_with_value(path, 7.0)
    replaced = _read(path)
    assert replaced[1, 2] == 7.0 and not np.isnan(replaced).any()
    repair.restore_nans(path, indices)
    restored = _read(path)
    assert np.isnan(restored[1, 2]) and np.isnan(restored[3, 4])
    assert np.isnan(restored).sum() == 2


def test_no_nans_returns_none(tmp_path):
    assert repair.replace_nans_with_value(_write(tmp_path / "i.fits", np.ones((3, 3))), 0.0) is None


def test_extreme_artifacts_repaired_with_the_neighbour_median(tmp_path):
    data = np.full((20, 20), 5.0, dtype=np.float32)
    data[10, 10] = 1.0e6
    data[4, 4] = -2.6e6
    path = _write(tmp_path / "i.fits", data)
    assert repair.repair_extreme_artifact_pixels(path, 10000.0) == 2
    with fits.open(path) as hdul:
        assert hdul[0].header["NARTRPR"] == 2
        assert hdul[0].data[10, 10] == 5.0 and hdul[0].data[4, 4] == 5.0


# ----------------------------------------------------------------------
# offsets, psf
# ----------------------------------------------------------------------


def test_subpixel_offset_skipped_below_a_tenth_of_a_pixel(tmp_path):
    header = wcs_header(8, sip=False)
    path = _write(tmp_path / "i.fits", np.arange(64, dtype=np.float64).reshape(8, 8), header)
    before = path.read_bytes()
    offsets.apply_subpixel_orthogonal_offsets(path, 0.05, -0.1)
    assert path.read_bytes() == before


def test_subpixel_offset_shifts_and_corrects_crpix(tmp_path):
    header = wcs_header(8, sip=False)
    data = np.zeros((8, 8))
    data[4, 4] = 36.0
    path = _write(tmp_path / "i.fits", data, header)
    offsets.apply_subpixel_orthogonal_offsets(path, 0.5, 0.0)
    with fits.open(path) as hdul:
        assert hdul[0].header["CRPIX1"] == pytest.approx(header["CRPIX1"] - 0.5)
        shifted = hdul[0].data
    # Half the flux moves one pixel left; total flux is conserved.
    assert shifted[4, 3] == pytest.approx(18.0) and shifted[4, 4] == pytest.approx(18.0)
    assert shifted.sum() == pytest.approx(36.0)


def test_psf_normalised_and_transposed(tmp_path):
    path = _write(tmp_path / "p.fits", np.arange(9, dtype=np.float64).reshape(3, 3) + 1)
    out = tmp_path / "n.fits"
    psf.normalize_image(path, 0, out)
    with fits.open(out) as hdul:
        assert hdul[0].data.sum() == pytest.approx(1.0)
        assert hdul[0].header["NRMLZSUM"] == pytest.approx(1.0)
    before = _read(out)
    psf.transpose_image_data(out)
    assert np.array_equal(_read(out), before.T)


# ----------------------------------------------------------------------
# zogy
# ----------------------------------------------------------------------


def test_zogy_sigmas_from_image_statistics():
    assert zogy.zogy_background_sigmas(False, "x", "y", 2.0, 3.0, 0.5) == (2.0, 1.5)


def test_zogy_sigmas_from_uncertainty_maps(tmp_path):
    rng = np.random.default_rng(2)
    sci = _write(tmp_path / "s.fits", rng.uniform(1.0, 2.0, size=(40, 40)))
    ref = _write(tmp_path / "r.fits", rng.uniform(3.0, 4.0, size=(40, 40)))
    sn, sr = zogy.zogy_background_sigmas(True, sci, ref, 99.0, 99.0, 1.0)
    interior_s = _read(sci)[5:35, 5:35]
    assert sn == pytest.approx(np.percentile(interior_s, 10.0))
    assert 3.0 < sr < 3.2


def test_zogy_command_passes_numbers_through_str():
    args = zogy.build_zogy_command("py", "z.py", "n", "r", "np", "rp", "ns", "rs",
                                   1.5, 2.25, 0.0, 0.0, "d", "dp", "s")
    assert args == ["py", "z.py", "n", "r", "np", "rp", "ns", "rs", "1.5", "2.25", "0.0", "0.0",
                    "d", "dp", "s"]


# ----------------------------------------------------------------------
# masking, uncertainty, naive, fitsops
# ----------------------------------------------------------------------


def test_coverage_mask_blanks_below_threshold(tmp_path):
    img = _write(tmp_path / "i.fits", np.full((3, 3), 2.0))
    cov = np.ones((3, 3))
    cov[0, :] = 0.4
    mask = _write(tmp_path / "c.fits", cov)
    out = tmp_path / "o.fits"
    masking.mask_difference_image_with_resampled_reference_cov_map(img, mask, out, 0.5)
    result = _read(out)
    assert np.isnan(result[0]).all() and (result[1:] == 2.0).all()


def test_difference_uncertainty_formula(tmp_path):
    rng = np.random.default_rng(4)
    diff = _write(tmp_path / "d.fits", rng.normal(0.0, 1.0, size=(30, 30)))
    sci_data = np.full((30, 30), -1.0)
    sci_data[5, 5] = 100.0
    sci = _write(tmp_path / "s.fits", sci_data)
    ref = _write(tmp_path / "r.fits", np.full((30, 30), -4.0))
    cov_data = np.full((30, 30), 2.0)
    cov_data[0, 0] = 0.1
    cov = _write(tmp_path / "c.fits", cov_data)
    out = tmp_path / "u.fits"
    uncertainty.compute_diffimage_uncertainty(10.0, sci, ref, cov, 0.5, diff, out, seed=5)
    std = statistics.fits_data_statistics_with_clipping(diff, 3.0, 0, seed=5)["clippedstd"]
    u = _read(out)
    assert u[5, 5] == pytest.approx(np.sqrt(100.0 / 10.0 + 4.0 / 20.0 + std ** 2), rel=1e-5)
    assert u[1, 1] == pytest.approx(np.sqrt(4.0 / 20.0 + std ** 2), rel=1e-5)   # negative sci -> 0
    assert u[0, 0] == pytest.approx(np.sqrt(4.0 / 10.0 + std ** 2), rel=1e-5)   # cov below thresh -> 1


def test_naive_difference(tmp_path):
    sci = _write(tmp_path / "s.fits", np.full((4, 4), 5.0))
    ref = _write(tmp_path / "r.fits", np.full((4, 4), 2.0))
    naive.compute_naive_difference_image(sci, ref, tmp_path / "n.fits")
    assert (_read(tmp_path / "n.fits") == 3.0).all()


def test_scale_image_data_writes_float32(tmp_path):
    src = _write(tmp_path / "s.fits", np.full((2, 2), 3.0))
    scale_image_data(src, -1.0, tmp_path / "n.fits")
    out = fits.getdata(tmp_path / "n.fits")
    assert out.dtype == np.dtype(">f4") and (out == -3.0).all()


# ----------------------------------------------------------------------
# sfft
# ----------------------------------------------------------------------


def _sfft(**kwargs):
    args = dict(python_cmd="python3.11", sfft_code="s.py", filename_scifile="sci.fits",
                filename_reffile="ref.fits", filename_scicat="sc.txt", filename_refcat="rc.txt",
                filename_scipsf="sp.fits", filename_refpsf="rp.fits", filename_scisegm="ss.fits",
                filename_refsegm="rs.fits", science_image_filename="Roman.fits",
                crossconv_flag=False, sfft_bsmask_value="20000.0", sfft_bsmask_radius="30.0",
                sfft_use_gainmatch_catalogs=False, sfft_use_segmentation=False)
    args.update(kwargs)
    return sfft.build_sfft_command_args(**args)


def test_sfft_default_command():
    assert _sfft() == ["python3.11", "s.py", "./sci.fits", "./ref.fits", "--bsmaskvalue", "20000.0",
                       "--bsmaskradius", "30.0", "--scipsf", "sp.fits"]


def test_sfft_crossconv_and_segmentation():
    args = _sfft(crossconv_flag=True, sfft_use_segmentation=None, sfft_use_gainmatch_catalogs=True)
    assert args[4:8] == ["--scicat", "sc.txt", "--refcat", "rc.txt"]
    assert args[-8:] == ["sp.fits", "--crossconv", "--refpsf", "rp.fits",
                         "--scisegm", "ss.fits", "--refsegm", "rs.fits"]


def test_sfft_empty_bsmask_falls_back_to_the_filename_test():
    assert _sfft(sfft_bsmask_value="", science_image_filename="rimtimsim.fits")[4:8] == [
        "--bsmaskvalue", "20000.0", "--bsmaskradius", "30.0"]
    assert _sfft(sfft_bsmask_value="", science_image_filename="Roman.fits")[4:8] == [
        "--scicat", "sc.txt", "--refcat", "rc.txt"]


def test_sfft_shell_command():
    assert sfft.shell_command("source /a", ["p", "x"]) == "source /a && p x && deactivate"


def test_sfft_shell_command_with_no_activation():
    # The rebuild's default: run directly in the stage's own environment,
    # no source, no deactivate.
    assert sfft.shell_command("", ["p", "x"]) == "p x"


# ----------------------------------------------------------------------
# psfcat, with photutils where it is installed
# ----------------------------------------------------------------------


def test_photutils_catalog_on_a_synthetic_difference_image(tmp_path):
    pytest.importorskip("photutils")
    pytest.importorskip("pyarrow")
    from rapidpipe.science.difference import psfcat

    from .fakedifftools import _gaussian

    rng = np.random.default_rng(11)
    n = 64
    data = rng.normal(0.0, 1.0, size=(n, n))
    for x0, y0 in ((20.0, 20.0), (44.0, 40.0)):
        data += 400.0 * _gaussian(n, x0, y0, sigma=1.5) / (2 * np.pi * 2.25)
    header = wcs_header(n, sip=False)
    image = _write(tmp_path / "d.fits", data.astype(np.float32), header)
    unc = _write(tmp_path / "u.fits", np.ones((n, n), dtype=np.float32))
    kernel = _gaussian(17, 8, 8, sigma=1.5)
    psf_path = _write(tmp_path / "p.fits", (kernel / kernel.sum()).astype(np.float32))
    settings = psfcat.PsfCatalogSettings(3.0, 5.0, 2.0, (17, 17), 8.0, 0.2, 1.0, -1.0, 1.0, 1.0)
    result = psfcat.psf_catalog(
        settings, image=str(image), uncertainty=str(unc), psf=str(psf_path),
        sky_coords_image=str(image), catalog=str(tmp_path / "c.txt"),
        finder=str(tmp_path / "f.txt"), residual=str(tmp_path / "r.fits"),
        parquet=str(tmp_path / "c.parquet"), label="test", seed=1)
    assert result.produced and result.nsources == 2
    assert (tmp_path / "c.txt").exists() and (tmp_path / "f.txt").exists()
    assert (tmp_path / "r.fits").exists()


def test_photutils_catalog_none_when_no_sources_pass_filtering(tmp_path):
    """photutils' PSFPhotometry.__call__ returns ``None``, not an exception,

    when DAOStarFinder detects candidate sources but none survive its
    sharpness/roundness filter (``NoDetectionsWarning``) -- a real, valid
    outcome on a near-empty image, found live on the difference stage's
    negative-image catalog against the fixture's synthetic data
    (LEDGER-fixture-real.md, 2026-09-23). psf_catalog() must report this
    the same way as the constructor/fit exceptions above: not produced,
    not a crash.
    """
    pytest.importorskip("photutils")
    from rapidpipe.science.difference import psfcat

    from .fakedifftools import _gaussian

    rng = np.random.default_rng(3)
    n = 64
    data = rng.normal(0.0, 1.0, size=(n, n)).astype(np.float32)
    header = wcs_header(n, sip=False)
    image = _write(tmp_path / "d.fits", data, header)
    unc = _write(tmp_path / "u.fits", np.ones((n, n), dtype=np.float32))
    kernel = _gaussian(9, 4, 4, sigma=1.5)
    psf_path = _write(tmp_path / "p.fits", (kernel / kernel.sum()).astype(np.float32))
    # A low detection threshold with a tight sharpness/roundness window:
    # DAOStarFinder finds noise-spike candidates, all of which the filter
    # rejects, so PSFPhotometry.__call__ returns None before ever fitting.
    settings = psfcat.PsfCatalogSettings(3.0, 1.0, 2.0, (9, 9), 8.0, 0.9, 1.0, -0.1, 0.1, 1.0)
    result = psfcat.psf_catalog(
        settings, image=str(image), uncertainty=str(unc), psf=str(psf_path),
        sky_coords_image=str(image), catalog=str(tmp_path / "c.txt"),
        finder=str(tmp_path / "f.txt"), residual=str(tmp_path / "r.fits"),
        parquet=None, label="negative test", seed=1)
    assert result == psfcat.PsfCatalogResult(produced=False, nsources=None)
    assert not (tmp_path / "c.txt").exists()
