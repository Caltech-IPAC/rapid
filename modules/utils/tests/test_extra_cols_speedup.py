"""
Tests of the faster extra catalog columns: the vectorized sumrat, the PSF fits split among
processes, and the PSF fitting of only the SExtractor sources that pass the database prefilter
(modules/utils/rapid_data_analysis.py and modules/utils/rapid_pipeline_subs.py).

Run from the repository root:  python -m pytest modules/utils/tests
"""

import math

import numpy as np
from astropy.io import fits

import modules.utils.rapid_data_analysis as rda
import modules.utils.rapid_pipeline_subs as util


SIGMA = 1.6 / 2.3548
FILL = -999.0


def write_fits(path, data):
    fits.PrimaryHDU(np.asarray(data, dtype=np.float32)).writeto(path, overwrite=True)
    return str(path)


def star_field(shape=(160, 160), nstars=60, seed=1):
    """Noise plus Gaussian stars; returns the image and the zero-based star positions."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    image = rng.normal(0.0, 1.0, shape)
    xy = np.column_stack((rng.uniform(4, shape[1] - 5, nstars), rng.uniform(4, shape[0] - 5, nstars)))
    for (x0, y0), peak in zip(xy, 10 ** rng.uniform(0.5, 2.5, nstars)):
        image += peak * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * SIGMA ** 2))
    image[20:24, 30:40] = np.nan
    return image, xy


def write_psf(path, size=25):
    c = (size - 1) / 2
    yy, xx = np.mgrid[:size, :size]
    psf = np.exp(-((xx - c) ** 2 + (yy - c) ** 2) / (2 * SIGMA ** 2))
    return write_fits(path, psf / psf.sum())


def sumrat_one_by_one(data, xy, coord_base, stamp_size, filter_size, lower_median):
    """sumrat source by source with median_filter_truncate, as the code computed it before."""
    ny, nx = data.shape
    h = stamp_size // 2
    out = []
    for x, y in xy:
        if not (np.isfinite(x) and np.isfinite(y)):
            out.append(FILL)
            continue
        xi = int(math.floor(x + 0.5)) - coord_base
        yi = int(math.floor(y + 0.5)) - coord_base
        if xi < h or xi > nx - h - 1 or yi < h or yi > ny - h - 1:
            out.append(FILL)
            continue
        stamp = data[yi - h:yi + h + 1, xi - h:xi + h + 1]
        if filter_size > 1:
            stamp = rda.median_filter_truncate(stamp, filter_size=filter_size, lower_median=lower_median)
        good = np.isfinite(stamp)
        total, total_abs = np.sum(stamp[good]), np.sum(np.abs(stamp[good]))
        out.append(float(total / total_abs) if np.any(good) and total_abs > 0.0 else FILL)
    return out


def test_vectorized_sumrat_matches_one_by_one(tmp_path):
    image, xy = star_field()
    data = np.asarray(image, dtype=np.float32).astype(np.float64)
    path = write_fits(tmp_path / "diff.fits", image)
    positions = [tuple(p + 1.0) for p in xy] + [(1.0, 1.0), (3.0, 3.0), (160.4, 80.0), (np.nan, 5.0), (35.0, 22.0)]
    for filter_size, stamp_size, lower_median in ((3, 5, False), (3, 5, True), (1, 5, False), (3, 7, False)):
        got = rda.compute_sumrat_for_diff_image(path, positions, coord_base=1, stamp_size=stamp_size,
                                                filter_size=filter_size, lower_median=lower_median,
                                                fill_value=FILL)
        assert got == sumrat_one_by_one(data, positions, 1, stamp_size, filter_size, lower_median)


def test_median_filter_truncate_stack_matches_single():
    rng = np.random.default_rng(4)
    stamps = rng.normal(size=(6, 5, 5))
    stamps[2, 1, 3] = np.nan
    stamps[4] = np.nan
    stack = rda.median_filter_truncate_stack(stamps)
    for i in range(len(stamps)):
        assert np.array_equal(stack[i], rda.median_filter_truncate(stamps[i]), equal_nan=True)


def test_psf_fits_in_two_processes_match_one(tmp_path):
    image, xy = star_field()
    path = write_fits(tmp_path / "diff.fits", image)
    unc = write_fits(tmp_path / "unc.fits", np.ones_like(image))
    psf = write_psf(tmp_path / "psf.fits")
    positions = [tuple(p) for p in xy] + [(np.nan, 3.0), (200.0, 5.0)]
    kwargs = dict(coord_base=0, fwhm=2.0, fit_shape=(5, 5), aperture_radius=4.0,
                  input_unc_filename=unc, fill_value=FILL)
    one = rda.compute_photutils_cols_for_diff_image(path, psf, positions, **kwargs)
    two = rda.compute_photutils_cols_for_diff_image(path, psf, positions, num_processes=2, **kwargs)
    assert sorted(one) == sorted(two)
    for name in one:
        assert np.array_equal(np.array(one[name]), np.array(two[name]), equal_nan=True), name
    assert np.all(np.array(one["flux_fit"][:-2]) != FILL)
    assert one["flux_fit"][-2:] == [FILL, FILL]


def write_sextractor_catalog(path, xy_one_based, flux_aper_1, fluxerr_aper_1):
    """Minimal ASCII_HEAD catalog with XWIN_IMAGE, YWIN_IMAGE, and 2-element FLUX_APER/FLUXERR_APER."""
    lines = ["#   1 NUMBER                 Running object number",
             "#   2 XWIN_IMAGE             Windowed position estimate along x",
             "#   3 YWIN_IMAGE             Windowed position estimate along y",
             "#   4 FLUX_APER              Flux vector within fixed circular aperture(s)",
             "#   6 FLUXERR_APER           RMS error vector for aperture flux(es)"]
    for i, ((x, y), f, e) in enumerate(zip(xy_one_based, flux_aper_1, fluxerr_aper_1)):
        lines.append(f"{i + 1:6d} {x:10.4f} {y:10.4f} {0.5 * f:12.4f} {f:12.4f} {0.5 * e:10.4f} {e:10.4f}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return str(path)


def test_prefilter_only_fits_just_the_sources_that_pass(tmp_path):
    image, xy = star_field()
    path = write_fits(tmp_path / "diff.fits", image)
    unc = write_fits(tmp_path / "unc.fits", np.ones_like(image))
    psf = write_psf(tmp_path / "psf.fits")
    n = len(xy)
    flux = np.where(np.arange(n) % 3 == 0, 2.0, 100.0)            # Every third source has SNR 1.
    catalog = write_sextractor_catalog(tmp_path / "cat.txt", xy + 1.0, flux, np.ones(n))
    extra_cols_dict = {"extra_cols": "sumrat, nneg, nbad",
                       "sextractor_x_col": "XWIN_IMAGE", "sextractor_y_col": "YWIN_IMAGE",
                       "sxtractor_photutils_cols": "flux_fit, x_fit",
                       "photutils_cols_fit_shape": "(5, 5)", "photutils_cols_fill_value": str(FILL),
                       "photutils_cols_format": ".4f",
                       "pixel_cols_bkg_box": "None"}
    sumrat_dict = {"stamp_size": "5", "filter_size": "3", "lower_median": "False",
                   "fill_value": str(FILL), "col_format": ".4f"}
    psfcat_dict = {"fwhm": "2.0", "fit_shape": "(17, 17)", "aperture_radius": "4.0"}
    common = dict(diff_psf_filename=psf, diff_unc_filename=unc, psfcat_dict=psfcat_dict,
                  prefilter_sumrat_threshold=0.4, prefilter_snr_threshold=5.0)

    all_cols = util.compute_extra_cols_sxtractor(path, catalog, dict(extra_cols_dict), sumrat_dict,
                                                 str(tmp_path / "all.txt"), **common)
    pre_cols = util.compute_extra_cols_sxtractor(path, catalog,
                                                 dict(extra_cols_dict, photutils_cols_prefilter_only="True",
                                                      photutils_cols_num_processes="2"),
                                                 sumrat_dict, str(tmp_path / "pre.txt"), **common)

    sumrat = np.array(all_cols["sumrat"])
    sumrat[sumrat == FILL] = np.nan
    keep = util.prefilter_database_records(n, sumrat, None, 0.4, None,
                                           snr=flux / np.ones(n), snr_prefilter_threshold=5.0)
    assert 0 < keep.sum() < n

    for name in ("sumrat", "nneg", "nbad"):
        assert pre_cols[name] == all_cols[name]
    for name in ("flux_fit", "x_fit"):
        got = np.array(pre_cols[name])
        assert np.array_equal(got[keep], np.array(all_cols[name])[keep])
        assert np.all(got[~keep] == FILL)


def test_prefilter_only_edge_and_missing_values(tmp_path):

    # As in the XSources loader: a source whose nearest pixel is within 2 pixels of the edge is
    # not fitted, even though its sumrat is missing (-999, its 5x5 stamp does not fit) and so
    # passes the sumrat cut; one just inside (3 pixels from the edge) is fitted.  A missing
    # aperture flux (FLUX_APER_1 = -999) is not cut: the source is fitted if its sumrat passes.

    image, xy = star_field()
    yy, xx = np.mgrid[:image.shape[0], :image.shape[1]]
    edge_stars = [(1.0, 80.0), (1.0, 120.0)]                      # Zero-based x = 1: one-based pixel 2.
    inside_star = (2.0, 140.0)                                    # Zero-based x = 2: one-based pixel 3.
    for x0, y0 in edge_stars + [inside_star]:
        image += 200.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * SIGMA ** 2))
    path = write_fits(tmp_path / "diff.fits", image)
    unc = write_fits(tmp_path / "unc.fits", np.ones_like(image))
    psf = write_psf(tmp_path / "psf.fits")

    # Rows: the field stars (SNR 100), then
    #   n:   edge star, SNR 100              -> within 2 px of the edge             -> not fitted
    #   n+1: edge star, SNR 1                -> within 2 px of the edge, SNR fails  -> not fitted
    #   n+2: field star 0, FLUX_APER_1 = -999 -> SNR missing, sumrat passes        -> fitted
    #   n+3: noise spot, FLUX_APER_1 = -999   -> SNR missing, sumrat fails         -> not fitted
    #   n+4: star 3 px from the edge, SNR 100 -> sumrat computed and passes        -> fitted

    n = len(xy)
    noise_spot = (80.0, 150.0)
    positions = np.vstack((xy, edge_stars, xy[:1], [noise_spot], [inside_star]))
    flux = np.concatenate((np.full(n, 100.0), [100.0, 1.0, -999.0, -999.0, 100.0]))
    catalog = write_sextractor_catalog(tmp_path / "cat.txt", positions + 1.0, flux, np.ones(len(positions)))

    extra_cols_dict = {"extra_cols": "sumrat, nneg, nbad",
                       "sextractor_x_col": "XWIN_IMAGE", "sextractor_y_col": "YWIN_IMAGE",
                       "sxtractor_photutils_cols": "flux_fit, x_fit",
                       "photutils_cols_fit_shape": "(5, 5)", "photutils_cols_fill_value": str(FILL),
                       "photutils_cols_format": ".4f",
                       "pixel_cols_bkg_box": "None"}
    sumrat_dict = {"stamp_size": "5", "filter_size": "3", "lower_median": "False",
                   "fill_value": str(FILL), "col_format": ".4f"}
    psfcat_dict = {"fwhm": "2.0", "fit_shape": "(17, 17)", "aperture_radius": "4.0"}
    common = dict(diff_psf_filename=psf, diff_unc_filename=unc, psfcat_dict=psfcat_dict,
                  prefilter_sumrat_threshold=0.4, prefilter_snr_threshold=5.0)

    all_cols = util.compute_extra_cols_sxtractor(path, catalog, dict(extra_cols_dict), sumrat_dict,
                                                 str(tmp_path / "all.txt"), **common)
    pre_cols = util.compute_extra_cols_sxtractor(path, catalog,
                                                 dict(extra_cols_dict, photutils_cols_prefilter_only="True"),
                                                 sumrat_dict, str(tmp_path / "pre.txt"), **common)

    sumrat = np.array(pre_cols["sumrat"])
    flux_fit_all = np.array(all_cols["flux_fit"])
    flux_fit_pre = np.array(pre_cols["flux_fit"])

    edge_ok, edge_low_snr, aper_fill_star, aper_fill_noise, inside = n, n + 1, n + 2, n + 3, n + 4

    # The edge sources have no sumrat; the source 3 px from the edge has one.
    assert sumrat[edge_ok] == FILL and sumrat[edge_low_snr] == FILL
    assert sumrat[inside] > 0.4
    assert sumrat[aper_fill_star] > 0.4 and sumrat[aper_fill_noise] <= 0.4

    # Fitted, with the same values as when all sources are fitted.
    for i in (aper_fill_star, inside):
        assert flux_fit_all[i] != FILL
        assert flux_fit_pre[i] == flux_fit_all[i]
        assert pre_cols["x_fit"][i] == all_cols["x_fit"][i]

    # Not fitted (the edge stars could be: all_cols has a fit for them).
    assert flux_fit_all[edge_ok] != FILL
    for i in (edge_ok, edge_low_snr, aper_fill_noise):
        assert flux_fit_pre[i] == FILL and pre_cols["x_fit"][i] == FILL

    # The same decision as the XSources loader makes from the written catalog: its position
    # cuts and its prefilter.
    from astropy.io import ascii
    table = ascii.read(str(tmp_path / "pre.txt"), format="sextractor")
    fills = (FILL,)
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = util.catalog_float_values(table, "FLUX_APER_1", fills) / util.catalog_float_values(table, "FLUXERR_APER_1", fills)
    keep = util.prefilter_database_records(len(table), util.catalog_float_values(table, "SUMRAT", fills), None,
                                           0.4, None, snr=snr, snr_prefilter_threshold=5.0)
    keep &= util.sxtractor_positions_within_image(np.asarray(table["XWIN_IMAGE"], float),
                                                  np.asarray(table["YWIN_IMAGE"], float),
                                                  image.shape[1], image.shape[0])
    assert np.isnan(snr[aper_fill_star])
    assert list(keep[[edge_ok, edge_low_snr, aper_fill_star, aper_fill_noise, inside]]) == [False, False, True, False, True]
    assert np.array_equal(keep, flux_fit_pre != FILL)
