# ported-from: pipeline/loadPSFCatIntoDBSourcesTable.py @ c740f3e3
"""`dev`'s catalog join and source-row derivations (loadPSFCatIntoDBSourcesTable.py).

`dev` reads, for one job, the Photutils PSF-fit catalog and its finder
catalog for the positive difference image and again for the negative,
inner-joins each pair on ``id`` (L531-534, L551-554), computes level-6 and
level-9 NESTED HEALPix indexes from each joined row's RA/Dec (L542-546,
L562-566), and writes one CSV block per sign
(``write_joined_table_inner_to_csv_file``, L287-356): rows whose PSF-fit
position lies outside ``[xy_fit_min, naxis - xy_fit_max_offset]`` are
dropped (NaN positions fail the comparison and are dropped too), the Roman
tessellation id is looked up per row, and every row is stamped with the
difference image's ``pid``, the sign as ``isdiffpos``, and the exposure's
``expid``, ``fid``, ``sca`` and ``mjdobs``.

Two things differ from `dev`:

- The tessellation id comes from ``RomanTessellationClosedForm`` (the
  certified closed form already in this tree, rapidpipe.science.spatial),
  not the SQLite ``RomanTessellationNSIDE512`` `dev` queries per row. Tile
  identity is certified identical except in the one-ULP bands where the
  SQLite R-tree's outward float32 rounding let traversal order decide.
- Every row also carries the rebuild's run-model columns (``run``,
  ``attempt``, ``result_set``), appended after `dev`'s 28.
"""

from __future__ import annotations

from typing import IO, Any, Sequence

import healpy as hp
import numpy as np
from astropy.table import QTable, join

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm

#: `dev`'s module-level HEALPix levels (L25-29): NSIDE = 2**level.
LEVEL6 = 6
LEVEL9 = 9

#: `dev`'s fit-position bounds (L35-36), in pixels.
XY_FIT_MIN = -0.5
XY_FIT_MAX_OFFSET = 0.5

#: `dev`'s catalog-column to database-column map, in `dev`'s CSV order
#: (L339-349). The finder catalog supplies sharpness, roundness1,
#: roundness2, n_pixels and peak; the PSF-fit catalog the rest.
CATALOG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "id"),
    ("ra", "ra"),
    ("dec", "dec"),
    ("x_fit", "xfit"),
    ("y_fit", "yfit"),
    ("flux_fit", "fluxfit"),
    ("x_err", "xerr"),
    ("y_err", "yerr"),
    ("flux_err", "fluxerr"),
    ("n_pixels_fit", "npixfit"),
    ("qfit", "qfit"),
    ("cfit", "cfit"),
    ("reduced_chi2", "redchi"),
    ("flags", "flags"),
    ("sharpness", "sharpness"),
    ("roundness1", "roundness1"),
    ("roundness2", "roundness2"),
    ("n_pixels", "npix"),
    ("peak", "peak"),
)


#: `dev`'s pixel columns (rapidpipe.science.difference.columns), loaded as
#: ``loadPSFCatIntoDBSourcesTable.py`` loads them: a missing value is null.
PIXEL_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("sumrat", np.float64), ("nneg", np.int16), ("nbad", np.int16))

#: `dev`'s real/bogus columns (rapidpipe.science.difference.realbogus), catalog to database
#: (963eb440: ``rb_score`` -> ``rb``, ``rb_label`` -> ``rblabel``): an unscored source's NaN
#: score is null, its label -1; a catalog without them (ZOGY's) loads both null.
RB_COLUMNS: tuple[tuple[str, str, Any], ...] = (
    ("rb_score", "rb", np.float64), ("rb_label", "rblabel", np.int16))

#: `dev`'s edge margin of the database prefilter, in pixels (57559572,
#: ``loadSECatIntoDBSourcesTable.py``): a source whose nearest pixel is
#: within it of the image edge is rejected, its 5 x 5 sumrat stamp not
#: fitting there.
EDGE_MARGIN = 2


def read_joined_catalog(catalog_path, finder_path) -> QTable:
    """`dev`'s read and inner join of a PSF-fit catalog and its finder catalog on ``id``."""
    psfcat_qtable = QTable.read(str(catalog_path), format="ascii", fast_reader=True)
    psfcat_finder_qtable = QTable.read(str(finder_path), format="ascii", fast_reader=True)
    return join(psfcat_qtable, psfcat_finder_qtable, keys="id", join_type="inner")


def healpix_arrays(joined: QTable, *, level6: int = LEVEL6,
                   level9: int = LEVEL9) -> tuple[np.ndarray, np.ndarray]:
    """`dev`'s vectorised ``hp.ang2pix`` at NSIDE 2**6 and 2**9, NESTED, lonlat."""
    ra_arr = np.array(joined["ra"], dtype=np.float64)
    dec_arr = np.array(joined["dec"], dtype=np.float64)
    hp6_arr = hp.ang2pix(2 ** level6, ra_arr, dec_arr, nest=True, lonlat=True)
    hp9_arr = hp.ang2pix(2 ** level9, ra_arr, dec_arr, nest=True, lonlat=True)
    return hp6_arr, hp9_arr


def tessellation_ids(ra_arr: np.ndarray, dec_arr: np.ndarray) -> np.ndarray:
    """The Roman tessellation id per source (`dev`: ``get_rtid`` per row, L335-336)."""
    return RomanTessellationClosedForm().get_rtid_array(
        np.asarray(ra_arr, dtype=np.float64), np.asarray(dec_arr, dtype=np.float64))


def write_joined_table_inner_to_csv_file(
    isdiffpos: str,
    expid: Any,
    sca: Any,
    fid: Any,
    mjdobs: Any,
    pid: Any,
    csv_fh: IO[str],
    joined_table_inner: QTable,
    hp6_arr: np.ndarray,
    hp9_arr: np.ndarray,
    *,
    naxis1: int,
    naxis2: int,
    xy_fit_min: float = XY_FIT_MIN,
    xy_fit_max_offset: float = XY_FIT_MAX_OFFSET,
    run_columns: Sequence[Any] = (),
    log=None,
) -> int:
    """`dev`'s CSV block for one sign; returns the number of rows written.

    ``naxis1``/``naxis2`` are the limits `dev` compares against: the science
    image's axes plus the one row and column `dev` adds (L149-152).
    ``run_columns`` are appended to every row after `dev`'s 28 columns.
    """
    nrows = len(joined_table_inner)
    if nrows == 0:
        return 0

    x_fit_arr = np.array(joined_table_inner["x_fit"], dtype=np.float64)
    y_fit_arr = np.array(joined_table_inner["y_fit"], dtype=np.float64)

    keep = ((x_fit_arr >= xy_fit_min) & (x_fit_arr <= naxis1 - xy_fit_max_offset) &
            (y_fit_arr >= xy_fit_min) & (y_fit_arr <= naxis2 - xy_fit_max_offset))

    nkeep = int(np.count_nonzero(keep))

    if log is not None:
        log.info("write_joined_table_inner_to_csv_file: isdiffpos=%s, rejected %s of %s "
                 "sources with out-of-range xfit,yfit", isdiffpos, nrows - nkeep, nrows)

    if nkeep == 0:
        return 0

    if nkeep < nrows:
        joined_table_inner = joined_table_inner[keep]
        hp6_arr = hp6_arr[keep]
        hp9_arr = hp9_arr[keep]
        nrows = nkeep

    t = joined_table_inner
    ra_arr = np.array(t["ra"], dtype=np.float64)
    dec_arr = np.array(t["dec"], dtype=np.float64)

    field_arr = tessellation_ids(ra_arr, dec_arr)

    columns = [
        np.array(t["id"]),
        ra_arr, dec_arr,
        np.array(t["x_fit"]), np.array(t["y_fit"]), np.array(t["flux_fit"]),
        np.array(t["x_err"]), np.array(t["y_err"]), np.array(t["flux_err"]),
        np.array(t["n_pixels_fit"]),
        np.array(t["qfit"]), np.array(t["cfit"]),
        np.array(t["reduced_chi2"]),
        np.array(t["flags"]),
        np.array(t["sharpness"]), np.array(t["roundness1"]), np.array(t["roundness2"]),
        np.array(t["n_pixels"]), np.array(t["peak"]),
        np.full(nrows, pid), np.full(nrows, isdiffpos, dtype=object),
        field_arr, hp6_arr, hp9_arr,
        np.full(nrows, expid), np.full(nrows, fid),
        np.full(nrows, sca), np.full(nrows, mjdobs),
    ]
    columns += [np.full(nrows, value, dtype=object) for value in run_columns]
    data = np.column_stack(columns)

    np.savetxt(csv_fh, data, delimiter=",", fmt="%s")
    return nrows


def prefilter(catalog, naxis1: int, naxis2: int, sumrat_prefilter_threshold: float) -> np.ndarray:
    """`dev`'s database prefilter on a PSF-fit catalog: the rows it keeps.

    ``sumrat > sumrat_prefilter_threshold`` (`dev` c931b001
    ``prefilter_database_records``, called by
    ``loadPSFCatIntoDBSourcesTable.py``), with a null or non-finite sumrat
    rejected, and the nearest pixel ``floor(x + 0.5)`` of the zero-based fit
    position at least :data:`EDGE_MARGIN` from every edge (57559572, `dev`'s
    XSources loader). Both differ from `dev`'s PSF path, which passes a
    missing sumrat and has no edge cut (decisions.md, dev-prefilter). `dev`'s
    ``rb > 0`` is not applied: nothing is cut on the score (decisions.md, rb-port).
    """
    n = catalog.num_rows
    sumrat = (catalog.column("sumrat").to_numpy().astype(float)
              if "sumrat" in catalog.column_names else np.full(n, np.nan))
    x, y = (catalog.column(c).to_numpy().astype(float) for c in ("x_fit", "y_fit"))
    with np.errstate(invalid="ignore"):
        xnear, ynear = np.floor(x + 0.5), np.floor(y + 0.5)
        return (np.isfinite(sumrat) & (sumrat > sumrat_prefilter_threshold)
                & (xnear >= EDGE_MARGIN) & (xnear <= naxis1 - 1 - EDGE_MARGIN)
                & (ynear >= EDGE_MARGIN) & (ynear <= naxis2 - 1 - EDGE_MARGIN))


def source_set(catalog, key: dict, pid: int, mjdobs: float, naxis1: int, naxis2: int, *,
               max_sources: int | None, xy_fit_min: float = XY_FIT_MIN,
               xy_fit_max_offset: float = XY_FIT_MAX_OFFSET,
               sumrat_prefilter_threshold: float | None = None):
    """A foundation ``sources`` set from a joined PSF-fit catalog (a ``pyarrow.Table``).

    `dev`'s fit-position bounds, then `dev`'s database prefilter when
    ``sumrat_prefilter_threshold`` is a number (``sumrat`` above it, finite,
    and the nearest pixel at least :data:`EDGE_MARGIN` from the edge;
    ``None`` applies none of it), then the brightest ``max_sources`` by
    ``flux_fit / flux_err`` (0 or ``None`` keeps every source), mapped as
    :data:`CATALOG_COLUMNS` maps them. ``key`` is the set's ``{dataset,
    expid, sca, fid}``; every source is positive (``isdiffpos``), as the
    positive catalog is the one read. Returns ``(table, candidates)``, the
    candidates being the sources inside the bounds with a finite ratio that
    pass the prefilter, before the cap. `dev`'s ``sumrat``, ``nneg`` and
    ``nbad`` are carried from the catalog, null where it has no value or no
    such column, and its ``rb_score`` and ``rb_label`` as ``rb`` and
    ``rblabel`` (:data:`RB_COLUMNS`).
    """
    import pyarrow as pa

    def column(name):  # a null float (pandas' NaN on write) reads back as NaN, as dev's text has it
        return catalog.column(name).to_numpy()

    x, y, flux, err = (column(n).astype(float) for n in ("x_fit", "y_fit", "flux_fit", "flux_err"))
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = np.where(err > 0, flux / err, np.nan)
    keep = ((x >= xy_fit_min) & (x <= naxis1 - xy_fit_max_offset) & (y >= xy_fit_min)
            & (y <= naxis2 - xy_fit_max_offset) & np.isfinite(snr))
    if sumrat_prefilter_threshold is not None:
        keep &= prefilter(catalog, naxis1, naxis2, sumrat_prefilter_threshold)
    candidates = np.flatnonzero(keep)
    chosen = candidates[np.argsort(-snr[candidates], kind="stable")][:max_sources or None]

    columns = {db: column(name)[chosen] for name, db in CATALOG_COLUMNS if db != "npix"}
    ra, dec = columns["ra"].astype(float), columns["dec"].astype(float)
    hp6, hp9 = healpix_arrays({"ra": ra, "dec": dec})
    n = len(chosen)
    for name, kind in PIXEL_COLUMNS:  # NaN, or a catalog made before them: null, as dev loads -999
        values = column(name)[chosen].astype(float) if name in catalog.column_names else np.full(n, np.nan)
        columns[name] = pa.array(np.nan_to_num(values).astype(kind), mask=~np.isfinite(values))
    for name, db, kind in RB_COLUMNS:
        values = column(name)[chosen].astype(float) if name in catalog.column_names else np.full(n, np.nan)
        columns[db] = pa.array(np.nan_to_num(values).astype(kind), mask=~np.isfinite(values))
    return pa.table({
        "sid": columns["id"].astype(np.int64),
        "dataset": [key["dataset"]] * n, "expid": np.full(n, key["expid"]),
        "sca": np.full(n, key["sca"]), "fid": np.full(n, key["fid"]), "pid": np.full(n, pid),
        "isdiffpos": np.full(n, True), "field": tessellation_ids(ra, dec).astype(np.int64),
        "hp6": hp6.astype(np.int64), "hp9": hp9.astype(np.int64), "mjdobs": np.full(n, mjdobs),
        **columns,
    }), len(candidates)
