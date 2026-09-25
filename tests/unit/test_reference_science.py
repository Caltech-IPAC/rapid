"""Tests for rapidpipe.science.reference: dev's arithmetic, command lines, stamp, identity.

The expected values are recomputed here from `dev`'s formulas
(``pipeline/referenceImageSubs.py`` and ``modules/utils/rapid_pipeline_subs.py``
on origin/dev), not read back from the code under test.
"""

from __future__ import annotations

import gzip
import hashlib
import math
import warnings
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.utils.exceptions import AstropyUserWarning

from rapidpipe.science.reference import awaicgen, catalog, header, identity, measure, prep

AWAICGEN = {
    "awaicgen_input_images_list_file": "refimage_sci_inputs.txt",
    "awaicgen_input_uncert_list_file": "refimage_unc_inputs.txt",
    "awaicgen_pixelscale_absolute": 0.11,
    "awaicgen_inv_var_weight_flag": 0,
    "awaicgen_pixelflux_scale_flag": 1,
    "awaicgen_simple_coadd_flag": 1,
    "awaicgen_num_threads": 2,
    "awaicgen_output_mosaic_image_file": "awaicgen_output_mosaic_image.fits",
    "awaicgen_output_mosaic_cov_map_file": "awaicgen_output_mosaic_cov_map.fits",
    "awaicgen_output_mosaic_uncert_image_file": "awaicgen_output_mosaic_uncert_image.fits",
    "zprefimg_w146": 26.8630336165,
    "zprefimg_f184": 25.1999194557,
    "zprefimg": 17.0,
}


# ----------------------------------------------------------------------
# Filters and zero points
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["W146", "F146", "w146", " f146 "])
def test_zero_point_resolves_either_spelling(name):
    assert prep.get_reference_image_zeropoint(AWAICGEN, name) == 26.8630336165


def test_zero_point_falls_back_to_the_scalar():
    assert prep.get_reference_image_zeropoint(AWAICGEN, "H158") == 17.0
    assert prep.get_reference_image_zeropoint(AWAICGEN, None) == 17.0


def test_zero_point_without_scalar_or_entry_raises():
    d = {k: v for k, v in AWAICGEN.items() if k != "zprefimg"}
    with pytest.raises(KeyError):
        prep.get_reference_image_zeropoint(d, "H158")


def test_same_filter_accepts_both_spellings_only():
    assert prep.same_filter("F146", "W146")
    assert prep.same_filter("W146", "w146")
    assert prep.same_filter("F184", "F184")
    assert not prep.same_filter("F146", "F184")


def test_mjd_to_jd():
    assert prep.convert_mjd_to_jd(61679.086) == 61679.086 + 2400000.5


# ----------------------------------------------------------------------
# Per-frame preparation (R4): dev's arithmetic
# ----------------------------------------------------------------------


def _frame(path: Path, data: np.ndarray, *, exptime=140.0, zptmag=26.84, mjd=61679.086,
           filt="W146", gz=True) -> str:
    sci = fits.ImageHDU(data=data, name="SCI")
    sci.header["EXPTIME"] = exptime
    sci.header["ZPTMAG"] = zptmag
    sci.header["MJD-OBS"] = mjd
    sci.header["FILTER"] = filt
    raw = path.with_suffix("") if gz else path
    fits.HDUList([fits.PrimaryHDU(), sci]).writeto(raw)
    if gz:
        path.write_bytes(gzip.compress(raw.read_bytes()))
        raw.unlink()
    return path.name


def test_reformat_frame_matches_devs_formulas(tmp_path):
    rng = np.random.default_rng(1)
    data = rng.normal(1000.0, 50.0, size=(16, 12)).astype(np.float32)
    data[3, 4] = -200.0  # a negative pixel: dev takes |data_norm|
    name = _frame(tmp_path / "frame_a.fits.gz", data)
    gain, rn = 2.0, 9.4

    result = prep.reformat_frame(tmp_path, name, AWAICGEN, gain, rn)

    exptime, zptmag, zp = 140.0, 26.84, 26.8630336165
    scale = 10 ** (0.4 * (zp - zptmag))
    norm = np.array(data) / exptime
    expected = norm * scale
    expected_unc = (np.sqrt(np.abs(norm) * exptime / gain + rn ** 2) / exptime) * scale

    assert result.reformatted == "frame_a_reformatted.fits"
    assert result.reformatted_unc == "frame_a_reformatted_unc.fits"
    assert result.flux_scale_factor == scale
    assert result.zprefimg == zp and result.exptime == exptime
    assert result.jd == 61679.086 + 2400000.5
    with fits.open(tmp_path / result.reformatted, checksum=True) as hdul:
        assert len(hdul) == 1
        assert hdul[0].header["BUNIT"] == "DN/s"
        assert hdul[0].header["FILTER"] == "W146"
        np.testing.assert_array_equal(hdul[0].data, expected)
        assert hdul[0].data.dtype.newbyteorder("=") == expected.dtype == np.float32
    with fits.open(tmp_path / result.reformatted_unc, checksum=True) as hdul:
        np.testing.assert_array_equal(hdul[0].data, expected_unc)
    # The delivered and unzipped files are gone, as dev removes them.
    assert not (tmp_path / "frame_a.fits").exists()
    assert not (tmp_path / "frame_a.fits.gz").exists()


def test_reformat_frame_takes_an_unzipped_frame(tmp_path):
    name = _frame(tmp_path / "frame_b.fits", np.ones((4, 4), dtype=np.float32), gz=False)
    result = prep.reformat_frame(tmp_path, name, AWAICGEN, 2.0, 9.4)
    assert result.reformatted == "frame_b_reformatted.fits"


def test_list_files_stop_at_max(tmp_path):
    frames = [prep.PreparedFrame(f"f{i}_reformatted.fits", f"f{i}_reformatted_unc.fits",
                                 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(4)]
    prep.write_list_files(tmp_path, AWAICGEN, frames, 3)
    assert (tmp_path / "refimage_sci_inputs.txt").read_text() == \
        "f0_reformatted.fits\nf1_reformatted.fits\nf2_reformatted.fits\n"
    assert (tmp_path / "refimage_unc_inputs.txt").read_text().splitlines()[0] == \
        "f0_reformatted_unc.fits"


# ----------------------------------------------------------------------
# awaicgen
# ----------------------------------------------------------------------


def test_awaicgen_command_is_devs_for_the_default_mosaic():
    d = awaicgen.awaicgen_dict_for_mosaic(
        AWAICGEN, naxis1=7000, naxis2=7000, cdelt1=-0.000030555555556, rotation=0.0,
        ra_center=267.53906, dec_center=-29.827858)
    size = str(math.fabs(-0.000030555555556) * float(7000))
    assert awaicgen.build_awaicgen_command_line_args(d) == [
        "awaicgen", "-f1", "refimage_sci_inputs.txt", "-f3", "refimage_unc_inputs.txt",
        "-X", size, "-Y", size, "-R", "267.53906", "-D", "-29.827858", "-C", "0.0",
        "-pa", "0.11", "-wf", "0", "-sf", "1", "-sc", "1", "-nt", "2",
        "-o1", "awaicgen_output_mosaic_image.fits",
        "-o2", "awaicgen_output_mosaic_cov_map.fits",
        "-o3", "awaicgen_output_mosaic_uncert_image.fits", "-v"]
    # awaicgen truncates size / scale to the pixel count: dev's cdelt gives 7000.
    assert int(float(size) / (0.11 / 3600.0)) == 7000


def test_awaicgen_executable_is_configurable():
    d = awaicgen.awaicgen_dict_for_mosaic(
        AWAICGEN, naxis1=10, naxis2=20, cdelt1=-0.1, rotation=5.0, ra_center=1.0,
        dec_center=2.0)
    args = awaicgen.build_awaicgen_command_line_args(d, "/opt/rapid/bin/awaicgen")
    assert args[0] == "/opt/rapid/bin/awaicgen"
    assert args[args.index("-X") + 1] == str(0.1 * 10.0)
    assert args[args.index("-Y") + 1] == str(0.1 * 20.0)
    assert args[args.index("-C") + 1] == "5.0"


# ----------------------------------------------------------------------
# SExtractor
# ----------------------------------------------------------------------


def test_sextractor_command_sets_devs_per_call_values():
    table = {f"sextractor_{k}": "x" for k in (
        "input_image", "catalog_name", "catalog_type", "parameters_name", "detect_type",
        "detect_minarea", "detect_maxarea", "thresh_type", "detect_thresh",
        "analysis_thresh", "filter", "filter_name", "filter_thresh", "deblend_nthresh",
        "deblend_mincont", "clean", "clean_param", "mask_type", "weight_type",
        "rescale_weights", "weight_image", "weight_gain", "weight_thresh", "flag_image",
        "flag_type", "phot_apertures", "phot_autoparams", "phot_petroparams",
        "phot_autoapers", "phot_fluxfrac", "satur_level", "satur_key", "mag_zeropoint",
        "mag_gamma", "gain", "gain_key", "pixel_scale", "seeing_fwhm", "starnnw_name",
        "back_type", "back_value", "back_size", "back_filtersize", "backphoto_type",
        "backphoto_thick", "back_filtthresh", "checkimage_type", "checkimage_name",
        "memory_objstack", "memory_pixstack", "memory_bufsize", "assoc_name", "assoc_data",
        "assoc_params", "assoccoord_type", "assoc_radius", "assoc_type", "assocselec_type",
        "verbose_type", "header_suffix", "write_xml", "nthreads", "fits_unsigned",
        "interp_maxxlag", "interp_maxylag", "interp_type")}
    args, name = catalog.sextractor_refimage_command(
        table, "/code/cdf", "awaicgen_output_mosaic_image.fits",
        "awaicgen_output_mosaic_uncert_image.fits")
    assert name == "awaicgen_output_mosaic_refimsexcat.txt"
    assert args[:2] == ["sex", "awaicgen_output_mosaic_image.fits"]

    def opt(o):
        return args[args.index(o) + 1]
    assert opt("-CATALOG_NAME") == name
    assert opt("-WEIGHT_IMAGE") == "awaicgen_output_mosaic_uncert_image.fits"
    assert opt("-PARAMETERS_NAME") == "/code/cdf/rapidSexParamsRefImage.inp"
    assert opt("-FILTER_NAME") == "/code/cdf/rapidSexRefImageFilter.conv"
    assert opt("-STARNNW_NAME") == "/code/cdf/rapidSexRefImageStarGalaxyClassifier.nnw"


def test_fwhm_statistics_as_dev(tmp_path):
    params = tmp_path / "p.inp"
    params.write_text("X_IMAGE\nFWHM_IMAGE\nFLUX_APER(6)\n")
    cat = tmp_path / "c.txt"
    cat.write_text("#   1 X_IMAGE\n#   2 FWHM_IMAGE\n#   3 FLUX_APER\n"
                   "1.0 2.0 5\n2.0 4.0 5\n3.0 nan 5\n4.0 3.0 5\n")
    stats = catalog.fwhm_statistics(cat, params)
    assert (stats.fwhmmedpix, stats.fwhmminpix, stats.fwhmmaxpix) == (3.0, 2.0, 4.0)
    assert stats.nsexcatsources == 4
    assert stats.fwhm_ref == 3.0


def test_fwhm_statistics_empty_catalog_raises_as_dev(tmp_path):
    params = tmp_path / "p.inp"
    params.write_text("FWHM_IMAGE\n")
    cat = tmp_path / "c.txt"
    cat.write_text("#   1 FWHM_IMAGE\n")
    with pytest.raises(ValueError):
        catalog.fwhm_statistics(cat, params)


# ----------------------------------------------------------------------
# Measurements
# ----------------------------------------------------------------------


def test_cov5percent_on_a_synthetic_map(tmp_path):
    cov = np.array([[0, 1, 5], [7, 10, 2]], dtype=np.float32)
    path = tmp_path / "cov.fits"
    fits.PrimaryHDU(data=cov).writeto(path)
    # capped: 0+1+5+5+5+2 = 18 over 5 * 6 pixels
    assert measure.compute_cov5percent(path) == pytest.approx(18 / 30 * 100)


def test_cov5percent_full_depth_is_100(tmp_path):
    path = tmp_path / "cov.fits"
    fits.PrimaryHDU(data=np.full((4, 4), 9.0, dtype=np.float32)).writeto(path)
    assert measure.compute_cov5percent(path) == 100.0


def test_saturation_rate_is_devs_stopgap():
    assert measure.saturation_level_refimage_rate(100000.0) == 100000.0 / 60.0


def test_measure_mosaic_reports_refimmeta_fields(tmp_path):
    image = np.full((20, 20), 3.0, dtype=np.float32)
    image[0, :] = np.nan
    cov = np.full((20, 20), 2.0, dtype=np.float32)
    unc = np.full((20, 20), 0.5, dtype=np.float32)
    for name, data in (("i.fits", image), ("c.fits", cov), ("u.fits", unc)):
        fits.PrimaryHDU(data=data).writeto(tmp_path / name)
    m = measure.measure_mosaic(tmp_path / "i.fits", tmp_path / "c.fits", tmp_path / "u.fits",
                               sextractor_satur_level=100000.0, seed=7)
    assert m.cov5percent == pytest.approx(40.0)
    assert m.medncov == 2.0 and m.medpixunc == 0.5
    assert m.npixnan == 20 and m.gmedian == 3.0 and m.gmin == 3.0 and m.gmax == 3.0
    assert set(m.to_dict()) == {"cov5percent", "medncov", "medpixunc", "npixnan", "clmean",
                                "clstddev", "clnoutliers", "gmedian", "datascale", "gmin",
                                "gmax"}


# ----------------------------------------------------------------------
# Header stamp (R4)
# ----------------------------------------------------------------------


def _values(n: int = 3) -> header.StampValues:
    return header.StampValues(
        field=4711398, exposure_filter="W146", cov5percent=33.75, nframes=n,
        refimage_input_filenames=tuple(f"frame{i}.fits.gz" for i in range(n)),
        jdstart=2461679.586, jdend=2461679.606, zprefimg=26.8630336165,
        total_refimage_exptime=420.0, run="RUN", attempt="ATT", instance="INST")


def test_infile_keywords_are_devs():
    assert header.infile_keyword(1) == "INFIL001"
    assert header.infile_keyword(10) == "INFIL010"
    assert header.infile_keyword(99) == "INFIL099"
    assert header.infile_keyword(100) == "INFIL100"


def test_stamp_writes_devs_keywords_without_fid_plus_the_run_model(tmp_path):
    path = tmp_path / "image.fits"
    data = np.arange(12, dtype=np.float64).reshape(3, 4)
    fits.PrimaryHDU(data=data).writeto(path)
    header.add_keywords_to_reference_image_header(path, _values())
    with warnings.catch_warnings():
        warnings.simplefilter("error", AstropyUserWarning)
        with fits.open(path, checksum=True) as hdul:
            hdr = hdul[0].header
            assert hdul[0].data.dtype == np.dtype(">f4")
            np.testing.assert_array_equal(hdul[0].data, data.astype(np.float32))
    assert "FID" not in hdr
    assert hdr["BUNIT"] == "DN/s" and hdr["FIELD"] == 4711398 and hdr["FILTER"] == "W146"
    assert hdr.comments["FIELD"] == "Roman sky-tile number"
    assert hdr["COV5PERC"] == 33.75 and hdr["NFRAMES"] == 3
    assert hdr["JDSTART"] == 2461679.586 and hdr["JDEND"] == 2461679.606
    assert hdr["MAGZP"] == 26.8630336165 and hdr["TOTEXPTM"] == 420.0
    assert [hdr[f"INFIL00{i}"] for i in (1, 2, 3)] == \
        ["frame0.fits.gz", "frame1.fits.gz", "frame2.fits.gz"]
    assert (hdr["RPRUN"], hdr["RPATTMPT"], hdr["RPINST"], hdr["RPSTAGE"]) == \
        ("RUN", "ATT", "INST", "reference")
    assert "CHECKSUM" in hdr and "DATASUM" in hdr


# ----------------------------------------------------------------------
# Identity (R5)
# ----------------------------------------------------------------------


def test_selection_digest_is_the_ruled_formula():
    ids = ["01B", "01A", "01C"]
    expected = hashlib.sha256(("01A\n01B\n01C\n" + "ab" * 32).encode()).hexdigest()[:16]
    assert identity.selection_digest(ids, "ab" * 32) == expected
    assert identity.selection_digest(ids, "sha256:" + "ab" * 32) == expected


def test_selection_digest_ignores_order_but_not_membership_or_settings():
    base = identity.selection_digest(["a", "b"], "s1")
    assert identity.selection_digest(["b", "a"], "s1") == base
    assert identity.selection_digest(["a", "c"], "s1") != base
    assert identity.selection_digest(["a", "b"], "s2") != base
    assert len(base) == 16 and int(base, 16) >= 0


def test_logical_key():
    assert identity.logical_key(field=4711398, filter_name="F146", digest="0123456789abcdef") == {
        "field": "4711398", "filter": "F146", "recipe": "awaicgen",
        "version": "0123456789abcdef"}
