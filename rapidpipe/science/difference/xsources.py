# ported-from: modules/utils/rapid_pipeline_subs.py, modules/utils/rapid_data_analysis.py, pipeline/loadSECatIntoDBSourcesTable.py @ d2657f7d
"""`dev`'s ``xsources``: the SFFT difference image's SExtractor sources, with their extra columns.

`dev` builds them in two places, and this module holds both:

- The science pipeline's ``compute_extra_cols_sxtractor`` adds extra
  columns to the positive and the negative SExtractor catalog of the SFFT
  difference image. The pixel columns ``sumrat``, ``nneg`` and ``nbad``
  come from :mod:`.columns`, at the one-based ``XWIN_IMAGE`` and
  ``YWIN_IMAGE``. Next come the PhotUtils-style columns
  (:func:`photutils_columns`): DAOStarFinder's ``sharpness``,
  ``roundness1`` and ``roundness2`` at the peak of the convolved image
  nearest the source, and a PSF fit's ``flux_fit``, ``snr_fit``,
  ``reduced_chi2``, ``n_pixels_fit``, ``flags`` and ``cfit``. These are
  computed only for the sources that pass the database prefilter and lie
  inside the image (:func:`fit_selection`, `dev`'s
  ``photutils_cols_prefilter_only``). Last, RuBR-AT's ``rb_score`` and
  ``rb_label``, from :func:`rb_input` through the classifier of
  :mod:`.realbogus`.
- The loader ``loadSECatIntoDBSourcesTable.py`` keeps a source that lies
  inside the image more than two pixels from its edge
  (:func:`positions_within_image`) and passes the prefilter
  (:func:`prefilter`: sumrat, rb and the 3 px aperture's S/N, each cut
  only where its value is finite). It maps the source onto the
  ``xsources`` columns (:func:`xsource_rows`) with a tessellation field and
  HEALPix indices of its own. An imported epoch's extra columns are the
  ones `dev` wrote into its catalogs, read back as the loader reads them
  (:func:`catalog_extra_columns`).

Three things differ from `dev`:

- A missing value is NaN in memory and null in ``xsources``, where `dev`
  writes ``-999`` into its text catalog and its loader loads NULL. The
  rows loaded are the same.
- The features reach the classifier at full precision, where `dev`
  reads them back from its text catalog (``.6f``), as for ``sources``
  (:mod:`.realbogus`).
- A failure fails the job, where `dev` prints a warning and loads the
  catalog without its extra columns and so without the sumrat cut
  (rapid_docs ``decisions.md``, xsources). A value that cannot be computed
  is not a failure: it is null, as `dev` loads its ``-999``.
- The PSF fit masks an uncertainty pixel of 0 or less as it masks a
  non-finite one, where photutils raises on it for the whole catalog in
  `dev` (which then loads none of the extra columns).
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from rapidpipe.science.difference import columns
from rapidpipe.science.load.catalogs import healpix_arrays, tessellation_ids

logger = logging.getLogger(__name__)

#: `dev`'s ``XSOURCES_EDGE_MARGIN_PIXELS``: a source whose nearest pixel is within this many
#: pixels of the image edge is dropped.
EDGE_MARGIN = 2

#: The PhotUtils-style columns, `dev`'s ``sxtractor_photutils_cols`` (``flags`` is the fit's).
PHOTUTILS_COLUMNS = ("sharpness", "roundness1", "roundness2", "x_dao_peak", "y_dao_peak",
                     "dao_flags", "flux_fit", "snr_fit", "reduced_chi2", "n_pixels_fit",
                     "flags", "cfit", "x_fit", "y_fit", "x_err", "y_err")

#: The SExtractor columns of ``xsources``: catalog name, column.
SEXTRACTOR_COLUMNS = (
    ("NUMBER", "num"), ("ALPHAWIN_J2000", "ra"), ("DELTAWIN_J2000", "dec"),
    ("XWIN_IMAGE", "x"), ("YWIN_IMAGE", "y"),
    ("FLUX_APER", "fluxap"), ("FLUX_APER_1", "fluxap1"), ("FLUX_APER_2", "fluxap2"),
    ("FLUX_APER_3", "fluxap3"), ("FLUX_APER_4", "fluxap4"), ("FLUX_APER_5", "fluxap5"),
    ("FLUXERR_APER", "fluxerrap"), ("FLUXERR_APER_1", "fluxerrap1"),
    ("FLUXERR_APER_2", "fluxerrap2"), ("FLUXERR_APER_3", "fluxerrap3"),
    ("FLUXERR_APER_4", "fluxerrap4"), ("FLUXERR_APER_5", "fluxerrap5"),
    ("AWIN_WORLD", "awinworld"), ("BWIN_WORLD", "bwinworld"), ("AWIN_IMAGE", "awinimage"),
    ("BWIN_IMAGE", "bwinimage"), ("FWHM_IMAGE", "fwhmimage"), ("CLASS_STAR", "classstar"),
    ("FLAGS", "flags"),
)

#: The extra columns of ``xsources``: computed name, column, integer-valued
#: (`dev`'s loader's ``extra_cols``, in its order).
EXTRA_COLUMNS = (
    ("rb_score", "rb", False), ("nneg", "nneg", True), ("nbad", "nbad", True),
    ("sumrat", "sumrat", False), ("sharpness", "sharpness", False),
    ("roundness1", "roundness1", False), ("roundness2", "roundness2", False),
    ("flux_fit", "fluxfit", False), ("snr_fit", "snrfit", False),
    ("reduced_chi2", "redchi", False), ("n_pixels_fit", "npixfit", True),
    ("flags", "flagsfit", True), ("cfit", "cfit", False), ("rb_label", "rblabel", True),
)


#: `dev`'s fill value for an extra column it could not compute (its ini's four
#: ``fill_value`` keys, all -999.0), which its XSources loader loads as null.
CATALOG_FILL_VALUE = -999.0


def read_catalog(path: Path):
    """A SExtractor ``ASCII_HEAD`` catalog as an astropy ``Table``, as `dev`'s loader reads it.

    Parameters
    ----------
    path : pathlib.Path
        The catalog. A vector column is named by its elements, ``FLUX_APER``,
        ``FLUX_APER_1`` and so on.

    Returns
    -------
    astropy.table.Table
        One row per source, in the catalog's order.
    """
    from astropy.table import Table

    return Table.read(path, format="ascii.sextractor")


def positions_within_image(x, y, naxis1: int, naxis2: int) -> np.ndarray:
    """`dev`'s ``sxtractor_positions_within_image``: inside the image and off its edge.

    Parameters
    ----------
    x, y : array_like of float
        One-based positions [pixels]; NaN is rejected.
    naxis1, naxis2 : int
        The image's size [pixels].

    Returns
    -------
    numpy.ndarray of bool
        True for a position within ``[0.5, naxis + 0.5]`` (to 1e-4 px) whose
        nearest pixel is more than :data:`EDGE_MARGIN` pixels from the edge.
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    tol = 0.0001
    keep = ((x >= 0.5 - tol) & (x <= naxis1 + 0.5 + tol)
            & (y >= 0.5 - tol) & (y <= naxis2 + 0.5 + tol))
    with np.errstate(invalid="ignore"):
        xnear = np.floor(x + 0.5)
        ynear = np.floor(y + 0.5)
    keep &= ((xnear >= 1 + EDGE_MARGIN) & (xnear <= naxis1 - EDGE_MARGIN)
             & (ynear >= 1 + EDGE_MARGIN) & (ynear <= naxis2 - EDGE_MARGIN))
    return keep


def prefilter(n: int, sumrat=None, rb=None, snr=None, *, sumrat_threshold: float | None = None,
              rb_threshold: float | None = None, snr_threshold: float | None = None) -> np.ndarray:
    """`dev`'s ``prefilter_database_records``: each cut only where its value is finite.

    Parameters
    ----------
    n : int
        The number of records.
    sumrat, rb, snr : array_like of float or None
        Each record's value, NaN where missing; None for no such cut.
    sumrat_threshold, rb_threshold, snr_threshold : float or None
        A record is rejected when its finite value is at or below this; None
        for no such cut.

    Returns
    -------
    numpy.ndarray of bool
        True for each record kept.
    """
    keep = np.ones(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        for values, threshold in ((sumrat, sumrat_threshold), (rb, rb_threshold),
                                  (snr, snr_threshold)):
            if values is not None and threshold is not None:
                values = np.asarray(values, dtype=np.float64)
                keep &= ~(np.isfinite(values) & (values <= threshold))
    return keep


def aperture_snr(catalog) -> np.ndarray:
    """``FLUX_APER_1 / FLUXERR_APER_1``, the 3 px aperture's S/N both `dev` cuts use.

    Parameters
    ----------
    catalog : astropy.table.Table
        A catalog as :func:`read_catalog` gives it.

    Returns
    -------
    numpy.ndarray of float
        One ratio per source; an error of 0 gives inf, which no cut rejects.
    """
    flux = np.asarray(catalog["FLUX_APER_1"], dtype=np.float64)
    err = np.asarray(catalog["FLUXERR_APER_1"], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return flux / err


def _unfiltered_daofinder(fwhm: float):
    """`dev`'s ``make_unfiltered_daofinder``: a DAOStarFinder whose sharpness and roundness filters are off."""
    from photutils.detection import DAOStarFinder

    try:
        return DAOStarFinder(threshold=1.0, fwhm=fwhm, min_separation=0,
                             sharpness_range=None, roundness_range=None)
    except TypeError:
        return DAOStarFinder(threshold=1.0, fwhm=fwhm, min_separation=0, sharplo=-np.inf,
                             sharphi=np.inf, roundlo=-np.inf, roundhi=np.inf)


def snap_to_daofind_peak(data: np.ndarray, xpix, ypix, fwhm: float,
                         snap_radius: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """`dev`'s ``snap_to_daofind_peak``: each pixel moved to the convolved image's maximum nearby.

    Parameters
    ----------
    data : numpy.ndarray
        The image, every pixel finite.
    xpix, ypix : array_like of int
        Zero-based pixels, all on the image.
    fwhm : float
        The DAOStarFinder kernel's FWHM [pixels].
    snap_radius : int, optional
        The square box's half-width [pixels]; 0 leaves the pixels where they are.

    Returns
    -------
    tuple of numpy.ndarray of int
        The snapped zero-based pixels; a pixel that ties the box's maximum stays.
    """
    from scipy.ndimage import convolve

    xpix = np.asarray(xpix, dtype=int)
    ypix = np.asarray(ypix, dtype=int)
    if snap_radius < 0:
        raise ValueError(f"snap_radius = {snap_radius} must be >= 0")
    if snap_radius == 0 or len(xpix) == 0:
        return xpix.copy(), ypix.copy()
    convolved = convolve(np.asarray(data, dtype=np.float64), _unfiltered_daofinder(fwhm).kernel.data,
                         mode="constant", cval=0.0)
    ny, nx = convolved.shape
    offsets = np.arange(-snap_radius, snap_radius + 1)
    dy, dx = np.meshgrid(offsets, offsets, indexing="ij")
    xcand = np.clip(xpix[:, np.newaxis] + dx.ravel()[np.newaxis, :], 0, nx - 1)
    ycand = np.clip(ypix[:, np.newaxis] + dy.ravel()[np.newaxis, :], 0, ny - 1)
    values = convolved[ycand, xcand]
    best = np.argmax(values, axis=1)
    rows = np.arange(len(xpix))
    keep = convolved[ypix, xpix] >= values[rows, best]
    return np.where(keep, xpix, xcand[rows, best]), np.where(keep, ypix, ycand[rows, best])


def daofind_columns(data: np.ndarray, xpix, ypix, fwhm: float) -> dict[str, np.ndarray]:
    """`dev`'s ``compute_daofind_cols_at_pixels``: sharpness, roundness1 and roundness2 at given pixels.

    Parameters
    ----------
    data : numpy.ndarray
        The image, every pixel finite.
    xpix, ypix : array_like of int
        Zero-based pixels, all on the image.
    fwhm : float
        The DAOStarFinder kernel's FWHM [pixels].

    Returns
    -------
    dict of str to numpy.ndarray
        ``sharpness``, ``roundness1`` and ``roundness2``, NaN where not computed.
    """
    names = ("sharpness", "roundness1", "roundness2")
    xycoords = np.column_stack((xpix, ypix)).astype(int)
    out = {name: np.full(len(xycoords), np.nan) for name in names}
    if len(xycoords) == 0:
        return out
    finder = _unfiltered_daofinder(fwhm)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            # Private photutils API, as dev uses it: find_stars drops a source with any
            # non-finite quantity, losing its finite ones.
            finder.xycoords = xycoords
            catalog = finder._get_raw_catalog(data)
            return {name: np.array(getattr(catalog, name), dtype=np.float64) for name in names}
        except Exception as exc:  # dev's fallback, source by source through the public API
            logger.warning("xsources: DAOStarFinder._get_raw_catalog failed (%s); per source", exc)
        h = max(finder.kernel.shape) // 2
        m = 2 * h + 1
        ny, nx = data.shape
        padded = np.zeros((ny + 2 * m, nx + 2 * m))
        padded[m:m + ny, m:m + nx] = data
        finder.xycoords = np.array([[m, m]])
        for i, (x, y) in enumerate(xycoords):
            table = finder.find_stars(padded[y:y + 2 * m + 1, x:x + 2 * m + 1])
            if table is not None and len(table) == 1:
                for name in names:
                    out[name][i] = float(table[name][0])
    return out


_FIT_INPUTS: dict[str, Any] | None = None


def _fit_chunk(index: np.ndarray) -> dict[str, np.ndarray]:
    """`dev`'s ``_fit_psf_chunk``: each source of ``index`` fitted alone, from :data:`_FIT_INPUTS`."""
    from astropy.table import QTable
    from photutils.psf import ImagePSF, PSFPhotometry

    inputs = _FIT_INPUTS
    psf = inputs["psf"]
    psf_ny, psf_nx = psf.shape
    model = ImagePSF(psf, flux=np.sum(psf), x_0=(psf_nx - 1) / 2, y_0=(psf_ny - 1) / 2)
    photometry = PSFPhotometry(psf_model=model, fit_shape=inputs["fit_shape"],
                               aperture_radius=inputs["aperture_radius"],
                               xy_bounds=inputs["xy_bounds"])
    index = np.asarray(index, dtype=int)
    init = QTable()
    init["id"] = index + 1
    init["x"] = inputs["x0"][index]
    init["y"] = inputs["y0"][index]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        phot = photometry(inputs["data"], mask=inputs["mask"], error=inputs["error"],
                          init_params=init)
    npix = "n_pixels_fit" if "n_pixels_fit" in phot.colnames else "npixfit"
    out = {"id": np.asarray(phot["id"], dtype=np.int64)}
    for name, phot_name in (("x_fit", "x_fit"), ("y_fit", "y_fit"), ("x_err", "x_err"),
                            ("y_err", "y_err"), ("flux_fit", "flux_fit"), ("flux_err", "flux_err"),
                            ("reduced_chi2", "reduced_chi2"), ("n_pixels_fit", npix),
                            ("flags", "flags"), ("cfit", "cfit")):
        out[name] = (np.asarray(phot[phot_name], dtype=np.float64) if phot_name in phot.colnames
                     else np.full(len(phot), np.nan))
    return out


def photutils_columns(data: np.ndarray, error: np.ndarray, psf: np.ndarray, x0, y0, *,
                      fwhm: float, fit_shape: Sequence[int], aperture_radius: float,
                      snap_radius: int = 1, xy_bounds=None,
                      num_processes: int = 1) -> dict[str, np.ndarray]:
    """`dev`'s ``compute_photutils_cols_for_diff_image``, on arrays and zero-based positions.

    Parameters
    ----------
    data : numpy.ndarray
        The difference image the sources were measured on.
    error : numpy.ndarray
        Its uncertainty image, which weights the fit.
    psf : numpy.ndarray
        The difference image's PSF, odd on both axes.
    x0, y0 : array_like of float
        Zero-based positions [pixels].
    fwhm : float
        The DAOStarFinder kernel's FWHM [pixels].
    fit_shape : sequence of int
        The fitted box (ny, nx), odd.
    aperture_radius : float
        The radius of the aperture that gives the fit's initial flux [pixels].
    snap_radius : int, optional
        The half-width of the box searched for the convolved image's peak [pixels].
    xy_bounds : float, tuple or None, optional
        PSFPhotometry's bound on how far a fitted position moves; None leaves it free.
    num_processes : int, optional
        Forked processes the fits are split among; the results do not depend on it.

    Returns
    -------
    dict of str to numpy.ndarray
        Each of :data:`PHOTUTILS_COLUMNS`, one value per position, NaN where
        the position is not finite, off the image, or the value not finite.
        ``x_dao_peak``, ``y_dao_peak``, ``x_fit`` and ``y_fit`` are zero-based.
    """
    global _FIT_INPUTS

    data = np.asarray(data, dtype=np.float64)
    error = np.asarray(error, dtype=np.float64)
    if error.shape != data.shape:
        raise ValueError(f"uncertainty image {error.shape} does not match the image {data.shape}")
    x0 = np.asarray(x0, dtype=np.float64)
    y0 = np.asarray(y0, dtype=np.float64)
    n = len(x0)
    ny, nx = data.shape
    out = {name: np.full(n, np.nan) for name in PHOTUTILS_COLUMNS}
    valid = np.isfinite(x0) & np.isfinite(y0)
    xind = np.zeros(n, dtype=int)
    yind = np.zeros(n, dtype=int)
    xind[valid] = np.floor(x0[valid] + 0.5).astype(int)
    yind[valid] = np.floor(y0[valid] + 0.5).astype(int)
    valid &= (xind >= 0) & (xind < nx) & (yind >= 0) & (yind < ny)
    index = np.flatnonzero(valid)
    if len(index) == 0:
        return out

    finite_data = np.where(np.isfinite(data), data, 0.0)
    xsnap, ysnap = snap_to_daofind_peak(finite_data, xind[index], yind[index], fwhm,
                                        snap_radius=snap_radius)
    for name, values in daofind_columns(finite_data, xsnap, ysnap, fwhm).items():
        out[name][index] = np.where(np.isfinite(values), values, np.nan)

    # dao_flags: a bad pixel in the plus-shaped core around the snapped peak (1) or around the
    # nearest pixel (2); a core pixel beyond the edge (4). The masks are padded by one pixel.
    beyond = np.ones((ny + 2, nx + 2), dtype=bool)
    beyond[1:ny + 1, 1:nx + 1] = False
    bad = np.zeros((ny + 2, nx + 2), dtype=bool)
    bad[1:ny + 1, 1:nx + 1] = ~np.isfinite(data)

    def core(mask, xc, yc):
        hits = np.zeros(len(xc), dtype=bool)
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            hits |= mask[yc + 1 + dy, xc + 1 + dx]
        return hits

    xnear, ynear = xind[index], yind[index]
    out["dao_flags"][index] = (1 * core(bad, xsnap, ysnap) + 2 * core(bad, xnear, ynear)
                               + 4 * (core(beyond, xsnap, ysnap) | core(beyond, xnear, ynear)))
    out["x_dao_peak"][index] = xsnap
    out["y_dao_peak"][index] = ysnap

    # An uncertainty of 0 or less cannot weight a pixel, and photutils raises for the whole
    # call on one inside any fit box, where dev's caller then loses every column of the
    # catalog; masked like a non-finite pixel, it costs only the sources it touches, whose
    # values are NaN, null in xsources, when nothing is left to fit.
    with np.errstate(invalid="ignore"):
        mask = ~np.isfinite(data) | ~(error > 0)
    _FIT_INPUTS = {"data": np.where(mask, 0.0, data), "mask": mask,
                   "error": np.where(mask, 1.0, error), "psf": np.asarray(psf, dtype=np.float64),
                   "fit_shape": tuple(int(s) for s in fit_shape),
                   "aperture_radius": aperture_radius, "xy_bounds": xy_bounds, "x0": x0, "y0": y0}
    processes = max(1, min(int(num_processes), len(index)))
    try:
        if processes > 1:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=processes,
                                     mp_context=multiprocessing.get_context("fork")) as pool:
                chunks = list(pool.map(_fit_chunk, np.array_split(index, processes)))
        else:
            chunks = [_fit_chunk(index)]
    finally:
        _FIT_INPUTS = None

    fitted = {name: np.concatenate([chunk[name] for chunk in chunks]) for name in chunks[0]}
    rows = fitted.pop("id") - 1
    flux_err = fitted.pop("flux_err")
    with np.errstate(divide="ignore", invalid="ignore"):
        fitted["snr_fit"] = fitted["flux_fit"] / flux_err
    for name, values in fitted.items():
        out[name][rows] = np.where(np.isfinite(values), values, np.nan)
    return out


def fit_selection(catalog, pixel: dict[str, np.ndarray], naxis1: int, naxis2: int, *,
                  sumrat_threshold: float | None, snr_threshold: float | None) -> np.ndarray:
    """`dev`'s ``_select_sources_passing_prefilter``: the sources the PSF fit is run on.

    Parameters
    ----------
    catalog : astropy.table.Table
        The SExtractor catalog.
    pixel : dict of str to numpy.ndarray
        Its pixel columns (:func:`rapidpipe.science.difference.columns.pixel_columns`).
    naxis1, naxis2 : int
        The difference image's own size [pixels].
    sumrat_threshold, snr_threshold : float or None
        The database prefilter's thresholds; no rb cut is made here.

    Returns
    -------
    numpy.ndarray of int
        Row indices of the sources that pass the sumrat and aperture S/N cuts
        and :func:`positions_within_image`.
    """
    n = len(catalog)
    keep = prefilter(n, pixel["sumrat"], None, aperture_snr(catalog),
                     sumrat_threshold=sumrat_threshold, snr_threshold=snr_threshold)
    keep &= positions_within_image(catalog["XWIN_IMAGE"], catalog["YWIN_IMAGE"], naxis1, naxis2)
    return np.flatnonzero(keep)


def extra_columns(catalog, data: np.ndarray, error: np.ndarray, psf: np.ndarray,
                  settings: dict[str, Any]) -> tuple[dict[str, np.ndarray], int]:
    """`dev`'s ``compute_extra_cols_sxtractor`` but RuBR-AT, which the stage adds (:func:`rb_input`).

    Parameters
    ----------
    catalog : astropy.table.Table
        The SExtractor catalog of the image ``data`` its sources are positive in.
    data, error, psf : numpy.ndarray
        That image, its uncertainty image and its PSF.
    settings : dict
        The stage's ``[difference]`` table: ``sumrat``, ``extra_catalog_cols``,
        ``psfcat_diffimage`` and ``database_prefilter``.

    Returns
    -------
    tuple
        The pixel and PhotUtils-style columns by name, one value per catalog
        row, NaN where not computed, positions zero-based; and the number of
        sources fitted.
    """
    x0 = np.asarray(catalog["XWIN_IMAGE"], dtype=np.float64) - 1.0
    y0 = np.asarray(catalog["YWIN_IMAGE"], dtype=np.float64) - 1.0
    out = dict(columns.pixel_columns(data, x0, y0, settings))
    e = settings["extra_catalog_cols"]
    p = settings["psfcat_diffimage"]
    f = settings["database_prefilter"]
    if e["photutils_cols_prefilter_only"]:
        ny, nx = np.shape(data)
        index = fit_selection(catalog, out, nx, ny,
                              sumrat_threshold=threshold(f, "sumrat_prefilter_threshold"),
                              snr_threshold=threshold(f, "snr_prefilter_threshold"))
    else:
        index = np.arange(len(catalog))
    fit_shape = e["photutils_cols_fit_shape"] or p["fit_shape"]
    fitted = photutils_columns(data, error, psf, x0[index], y0[index], fwhm=float(p["fwhm"]),
                               fit_shape=fit_shape, aperture_radius=float(p["aperture_radius"]),
                               snap_radius=int(e["photutils_cols_snap_radius"]),
                               xy_bounds=e["photutils_cols_xy_bounds"] or None,
                               num_processes=int(e["photutils_cols_num_processes"]))
    for name in PHOTUTILS_COLUMNS:
        values = np.full(len(catalog), np.nan)
        values[index] = fitted[name]
        out[name] = values
    return out, len(index)


def threshold(table: dict[str, Any], key: str) -> float | None:
    """A ``[difference.database_prefilter]`` threshold; ``""`` is none."""
    value = table.get(key, "")
    return None if value == "" else float(value)


def rb_input(extra: dict[str, np.ndarray]):
    """The table the RuBR-AT classifier scores, in :data:`.realbogus.CATALOG_COLUMNS`' names.

    Parameters
    ----------
    extra : dict of str to numpy.ndarray
        :func:`extra_columns`' columns. A source not fitted has NaN
        ``x_fit`` and is not scored (``rb_label`` -1), as in `dev`.

    Returns
    -------
    pyarrow.Table
        ``id`` (the row index) and the classifier's columns: the fitted
        zero-based position, ``flux_err`` as ``flux_fit / snr_fit`` (`dev`'s
        recovery), ``n_pixels_fit``, the fit's ``flags`` and DAOStarFinder's three.
    """
    import pyarrow as pa

    with np.errstate(divide="ignore", invalid="ignore"):
        flux_err = extra["flux_fit"] / extra["snr_fit"]
    names = ("x_fit", "y_fit", "flux_fit", "cfit", "reduced_chi2", "x_err", "y_err",
             "n_pixels_fit", "flags", "sharpness", "roundness1", "roundness2")
    table = {"id": np.arange(len(extra["x_fit"]), dtype=np.int64), "flux_err": flux_err}
    table.update({name: extra[name] for name in names})
    return pa.table(table)


def catalog_extra_columns(catalog) -> dict[str, np.ndarray]:
    """The extra columns `dev`'s pipeline wrote into a SExtractor catalog, as its XSources loader reads them.

    Parameters
    ----------
    catalog : astropy.table.Table
        A catalog as :func:`read_catalog` gives it, with the upper-case extra
        columns ``compute_extra_cols_sxtractor`` appends (``FLAGS_FIT`` for
        the fit's ``flags``), or without them where `dev` could not add them.

    Returns
    -------
    dict of str to numpy.ndarray
        Each of :data:`EXTRA_COLUMNS`' computed names, one float per row, NaN
        where the column is absent, holds :data:`CATALOG_FILL_VALUE` or is not finite.
    """
    out = {}
    for name, _, _ in EXTRA_COLUMNS:
        source = "FLAGS_FIT" if name == "flags" else name.upper()
        if source not in catalog.colnames:
            out[name] = np.full(len(catalog), np.nan)
            continue
        values = np.ma.filled(np.ma.asarray(catalog[source], dtype=np.float64), np.nan)
        values[(values == CATALOG_FILL_VALUE) | ~np.isfinite(values)] = np.nan
        out[name] = values
    return out


def xsource_rows(catalog, extra: dict[str, np.ndarray], *, isdiffpos: bool, key: dict, pid: int,
                 mjdobs: float, naxis1: int, naxis2: int,
                 thresholds: dict[str, float | None]) -> tuple[dict[str, np.ndarray], int]:
    """`dev`'s loader on one catalog: the kept sources as ``xsources`` columns.

    Parameters
    ----------
    catalog : astropy.table.Table
        The SExtractor catalog.
    extra : dict of str to numpy.ndarray
        Its extra columns, ``rb_score`` and ``rb_label`` among them when scored.
    isdiffpos : bool
        True for the positive catalog.
    key : dict
        The set's ``dataset``, ``expid``, ``sca`` and ``fid``.
    pid : int
        The difference-image row.
    mjdobs : float
        The exposure's MJD-OBS.
    naxis1, naxis2 : int
        The difference image's size [pixels].
    thresholds : dict
        ``sumrat``, ``rb`` and ``snr``, each a float or None.

    Returns
    -------
    tuple
        The columns of :data:`SEXTRACTOR_COLUMNS` and :data:`EXTRA_COLUMNS`
        and the set's own (``xsid`` not among them), for the kept rows; and
        the number of rows read.
    """
    n = len(catalog)
    keep = positions_within_image(catalog["XWIN_IMAGE"], catalog["YWIN_IMAGE"], naxis1, naxis2)
    keep &= prefilter(n, extra.get("sumrat"), extra.get("rb_score"), aperture_snr(catalog),
                      sumrat_threshold=thresholds["sumrat"], rb_threshold=thresholds["rb"],
                      snr_threshold=thresholds["snr"])
    rows = np.flatnonzero(keep)
    m = len(rows)
    out: dict[str, Any] = {name: np.asarray(catalog[source])[rows] for source, name in SEXTRACTOR_COLUMNS}
    for name, column, _ in EXTRA_COLUMNS:
        out[column] = (np.asarray(extra[name], dtype=np.float64)[rows] if name in extra
                       else np.full(m, np.nan))
    ra = np.asarray(out["ra"], dtype=np.float64)
    dec = np.asarray(out["dec"], dtype=np.float64)
    hp6, hp9 = healpix_arrays({"ra": ra, "dec": dec})
    out.update(dataset=np.full(m, key["dataset"], dtype=object), expid=np.full(m, key["expid"]),
               sca=np.full(m, key["sca"]), fid=np.full(m, key["fid"]), pid=np.full(m, pid),
               isdiffpos=np.full(m, isdiffpos), field=tessellation_ids(ra, dec), hp6=hp6, hp9=hp9,
               mjdobs=np.full(m, mjdobs))
    return out, n


def xsources_table(parts: Sequence[dict[str, np.ndarray]]):
    """The set's ``xsources`` rows as the parquet table the publisher loads.

    Parameters
    ----------
    parts : sequence of dict
        :func:`xsource_rows`' columns, the positive catalog's first.

    Returns
    -------
    pyarrow.Table
        Every ``xsources`` column but ``job``; ``xsid`` numbers the rows from
        1 in order; a missing extra value is null.
    """
    import pyarrow as pa

    def joined(name):
        return np.concatenate([np.asarray(p[name]) for p in parts]) if parts else np.array([])

    n = sum(len(p["num"]) for p in parts)
    table = {"xsid": np.arange(1, n + 1, dtype=np.int64),
             "dataset": pa.array([str(v) for v in joined("dataset")], pa.string()),
             "expid": pa.array(joined("expid"), pa.int32()),
             "sca": pa.array(joined("sca"), pa.int16()), "fid": pa.array(joined("fid"), pa.int16()),
             "pid": pa.array(joined("pid"), pa.int32()), "num": pa.array(joined("num"), pa.int32()),
             "isdiffpos": pa.array(joined("isdiffpos"), pa.bool_())}
    for _, name in SEXTRACTOR_COLUMNS[1:]:
        kind = pa.int16() if name == "flags" else pa.float64()
        table[name] = pa.array(joined(name), kind)
    table.update(field=pa.array(joined("field"), pa.int64()), hp6=pa.array(joined("hp6"), pa.int64()),
                 hp9=pa.array(joined("hp9"), pa.int64()), mjdobs=pa.array(joined("mjdobs"), pa.float64()))
    for _, name, integer in EXTRA_COLUMNS:
        values = joined(name).astype(np.float64)
        finite = np.isfinite(values)
        if integer:
            table[name] = pa.array(np.round(np.where(finite, values, 0)).astype(np.int16), mask=~finite)
        else:
            table[name] = pa.array(np.where(finite, values, 0.0), mask=~finite)
    return pa.table(table)


__all__: Sequence[str] = (
    "CATALOG_FILL_VALUE", "EDGE_MARGIN", "EXTRA_COLUMNS", "PHOTUTILS_COLUMNS", "SEXTRACTOR_COLUMNS", "aperture_snr",
    "catalog_extra_columns", "daofind_columns", "extra_columns", "fit_selection", "photutils_columns",
    "positions_within_image", "prefilter", "rb_input", "read_catalog", "snap_to_daofind_peak", "threshold",
    "xsource_rows", "xsources_table",
)
