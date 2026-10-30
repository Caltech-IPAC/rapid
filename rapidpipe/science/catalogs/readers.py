# ported-from: alerts/catalog_readers.py, alerts/providers.py @ a41c4d7b
"""Cone readers over the pipeline's copies of the external catalogs.

`dev`'s ``alerts/catalog_readers.py`` (Emily Everetts, Claude Code,
09/2026), ported whole, with the NED and NED-LVS column contracts and the
separation helper from `dev`'s ``alerts/providers.py`` beside them (the
Gaia contract is :mod:`rapidpipe.science.alerts.crossmatch`'s). No
default location: a reader is given the copy's root, which the pipeline
takes from a ``catalogs`` product.

A NedSliceReader over the local copy of the NED object directory: the
order-6 HEALPix parquet files that :mod:`rapidpipe.science.catalogs.store`
builds under ``<prefix>/objectdir_hp6/hp6=<pixel>/part.parquet`` (S3 or a
local dir).

    reader = Hp6NedReader("s3://<the catalogs bucket>/ned")

Contract (`dev`'s ``providers.NedSliceReader``): ``reader(ra_deg, dec_deg,
radius_arcsec)`` returns column arrays keyed by NED_COLUMNS for every NED
object in the cone, an EMPTY table when NED has nothing there, and None
only when the slice could not be obtained. With a complete local copy that
last case means the copy could not be read at all; a pixel with no file is
genuinely empty sky, because the repartition writes a file for every pixel
that has rows.

Why this exists: the web service (`dev`'s AstroqueryNedReader) took 3-24 s
per chip and timed out for hours at a time (2026-09-18/21). A cone here is
a few parquet reads, one file per order-6 pixel the cone touches, at most
four for a Roman chip, served from an in-process cache, so repeat visits to
a field (GBTDS every ~15 min) do not touch S3 again.

Also here: LvsReader, the same contract over NED-LVS (``<prefix>/lvs/
nedlvs.parquet``, built by ``ingest-lvs``), which at ~2.1 M rows is simply
held whole in memory rather than cut into pixel files.

    reader = LvsReader("s3://<the catalogs bucket>/ned")

And GaiaReader, the same contract over the local Gaia DR3 copy (its own
prefix, built by ``ingest-gaia`` or ``gaia-from-table``): LINCC's partition
layout kept as is, a cone read as the few parquet row groups it touches.

    reader = GaiaReader("s3://<the catalogs bucket>/gaia/DR3")
"""

import json
import logging
from collections import OrderedDict
from typing import Any

import healpy as hp
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from rapidpipe.science.alerts.crossmatch import (
    GAIA_BOOL_COLUMNS, GAIA_COLUMNS, GAIA_INT_COLUMNS, GAIA_NUMERIC_COLUMNS,
    GAIA_STRING_COLUMNS)
from rapidpipe.science.catalogs.store import (
    GAIA_INFO, HP6_ORDER, LVS_HP6_COLUMN, LVS_INFO, LVS_PARQUET, MANIFEST_NAME, Store,
    gaia_leaf_rel, hp6_file, parse_partition_info, read_properties)

logger = logging.getLogger(__name__)

NSIDE = 2 ** HP6_ORDER

# ---------------------------------------------------------------------------
# The NED and NED-LVS column contracts (`dev`'s providers.py): what a reader
# delivers and what its matcher takes.
# ---------------------------------------------------------------------------

NED_COLUMNS = ("prefname", "ra", "dec", "ptype", "z", "zunc", "zflag")
NED_REQUIRED_COLUMNS = ("prefname", "ra", "dec")

LVS_STRING_COLUMNS = ("objname", "objtype", "z_tech", "DistMpc_method")
LVS_BOOL_COLUMNS = ("z_qual", "Diam_qual")
LVS_NUMERIC_COLUMNS = ("ra", "dec", "z", "z_unc",
                       "DistMpc", "DistMpc_unc", "Diam", "Diam_ba", "Diam_pa",
                       "ebv", "m_Ks", "m_Ks_unc", "m_W1", "m_W1_unc",
                       "m_NUV", "m_NUV_unc", "SFR_hybrid", "SFR_hybrid_unc",
                       "Mstar", "Mstar_unc")
LVS_COLUMNS = LVS_STRING_COLUMNS + LVS_BOOL_COLUMNS + LVS_NUMERIC_COLUMNS
LVS_REQUIRED_COLUMNS = ("objname", "ra", "dec")


def sep_arcsec(ra: np.ndarray, dec: np.ndarray,
               ra0: float, dec0: float) -> np.ndarray:
    """Angular separation of positions from (ra0, dec0) [arcsec] (`dev`'s ``_sep_arcsec``)."""
    ra_r, dec_r = np.radians(ra), np.radians(dec)
    ra0_r, dec0_r = np.radians(ra0), np.radians(dec0)
    cos_sep = (np.sin(dec_r) * np.sin(dec0_r)
               + np.cos(dec_r) * np.cos(dec0_r) * np.cos(ra_r - ra0_r))
    return np.degrees(np.arccos(np.clip(cos_sep, -1.0, 1.0))) * 3600.0


# Columns read from each pixel file: the reader contract's columns only.
_STRING_COLUMNS = ("prefname", "ptype", "zflag")
_NUMERIC_COLUMNS = ("ra", "dec", "z", "zunc")
assert set(_STRING_COLUMNS) | set(_NUMERIC_COLUMNS) == set(NED_COLUMNS)


def _empty_columns() -> dict[str, np.ndarray]:
    return {**{c: np.array([], dtype=object) for c in _STRING_COLUMNS},
            **{c: np.array([], dtype=float) for c in _NUMERIC_COLUMNS}}


def table_to_columns(table: pa.Table) -> dict[str, np.ndarray]:
    """One pixel file's rows as the reader-contract arrays: numeric nulls
    become NaN, string nulls (and the empty strings the source catalog
    uses for "unclassified") become None."""
    out: dict[str, np.ndarray] = {}
    for name in _NUMERIC_COLUMNS:
        out[name] = np.asarray(table[name].to_numpy(zero_copy_only=False), dtype=float)
    for name in _STRING_COLUMNS:
        out[name] = np.array([None if v is None or v == "" else str(v)
                              for v in table[name].to_pylist()], dtype=object)
    return out


class Hp6NedReader:
    """Cone reads over the order-6 NED copy.

    Parameters
    ----------
    root : str
        ``s3://bucket/prefix`` or a local directory holding ``objectdir_hp6/``
        (and, if built, ``manifest.json``). For S3, AWS_DEFAULT_REGION must
        name the bucket's region (see store.Store).
    cache_pixels : int, optional
        How many pixel tables to keep in memory (LRU). A dense bulge pixel
        is ~200k rows / ~15 MB in memory; 64 pixels covers a night of
        GBTDS revisits comfortably.
    """

    def __init__(self, root: str, cache_pixels: int = 64) -> None:
        self.store = Store(root)
        self.root = root
        self.cache_pixels = cache_pixels
        self._cache: OrderedDict[int, dict[str, np.ndarray] | None] = OrderedDict()
        self.manifest: dict[str, Any] | None = None
        if self.store.exists(MANIFEST_NAME):
            self.manifest = json.loads(self.store.read_text(MANIFEST_NAME))
        self.complete = bool(self.manifest and self.manifest.get("hp6", {}).get("complete"))
        self.release = (self.manifest or {}).get("release")
        logger.info("NED local copy at %s: release %s, complete=%s", root,
                    self.release, self.complete)
        self.n_reads = 0          # pixel files actually fetched (tests, diagnostics)

    # -- pixel access -------------------------------------------------------
    def _pixel(self, pixel: int) -> dict[str, np.ndarray] | None:
        """The pixel's rows as contract arrays; None when it has no file."""
        if pixel in self._cache:
            self._cache.move_to_end(pixel)
            return self._cache[pixel]
        rel = hp6_file(pixel)
        if not self.store.exists(rel):
            columns = None
        else:
            table = self.store.read_table(rel, columns=list(NED_COLUMNS))
            columns = table_to_columns(table)
            self.n_reads += 1
        self._cache[pixel] = columns
        while len(self._cache) > self.cache_pixels:
            self._cache.popitem(last=False)
        return columns

    def pixels_for_cone(self, ra_deg: float, dec_deg: float,
                        radius_arcsec: float) -> list[int]:
        """The order-6 NESTED pixels a cone overlaps (inclusive, so a cone
        crossing a pixel edge sees both sides)."""
        vec = hp.ang2vec(ra_deg, dec_deg, lonlat=True)
        pixels = hp.query_disc(NSIDE, vec, np.radians(radius_arcsec / 3600.0),
                               inclusive=True, nest=True)
        return [int(p) for p in pixels]

    # -- the NedSliceReader protocol ----------------------------------------
    def __call__(self, ra_deg: float, dec_deg: float,
                 radius_arcsec: float) -> dict[str, np.ndarray] | None:
        """All NED rows within `radius_arcsec` of (ra, dec), or None when a
        needed pixel is missing from an incomplete copy."""
        parts = []
        for pixel in self.pixels_for_cone(ra_deg, dec_deg, radius_arcsec):
            columns = self._pixel(pixel)
            if columns is None:
                if not self.complete:
                    logger.warning("NED local copy at %s has no file for hp6=%d "
                                   "and is not marked complete; NED matching not run",
                                   self.root, pixel)
                    return None
                continue                      # complete copy: empty sky here
            parts.append(columns)
        if not parts:
            return _empty_columns()
        merged = {name: np.concatenate([p[name] for p in parts]) for name in NED_COLUMNS}
        sep = sep_arcsec(merged["ra"], merged["dec"], ra_deg, dec_deg)
        keep = sep <= radius_arcsec
        return {name: values[keep] for name, values in merged.items()}


# ---------------------------------------------------------------------------
# NED-LVS: the whole table in memory
# ---------------------------------------------------------------------------

def lvs_table_to_columns(table: pa.Table) -> dict[str, np.ndarray]:
    """NED-LVS rows as the LVS_COLUMNS contract arrays: numeric nulls become
    NaN, string nulls (and "") become None, logical nulls become False."""
    out: dict[str, np.ndarray] = {}
    for name in LVS_NUMERIC_COLUMNS:
        out[name] = np.asarray(table[name].to_numpy(zero_copy_only=False), dtype=float)
    for name in LVS_BOOL_COLUMNS:
        out[name] = np.array([bool(v) for v in table[name].to_pylist()], dtype=bool)
    for name in LVS_STRING_COLUMNS:
        out[name] = np.array([None if v is None or v == "" else str(v)
                              for v in table[name].to_pylist()], dtype=object)
    return out


class LvsReader:
    """Cone reads over the local NED-LVS table (``ingest-lvs``).

    NED-LVS is ~2.1 M rows, so unlike the object directory it is not cut
    into pixel files: the LVS_COLUMNS subset of ``<root>/lvs/nedlvs.parquet``
    is read once, on first use, and kept as an Arrow table (~0.3 GB; kept
    as Arrow rather than numpy object arrays because the four string
    columns would otherwise cost most of a gigabyte per process). A cone is
    an order-6 pixel pre-filter over the stored `_hp6` column, then an exact
    separation cut, and only the rows inside are converted to the contract
    arrays.

    Contract (NedSliceReader, LVS_COLUMNS): column arrays for every LVS
    galaxy in the cone, an EMPTY table when there is none. It never returns
    None: a table that cannot be read raises, and the caller reports that
    as "not run".

    Parameters
    ----------
    root : str
        ``s3://bucket/prefix`` or a local directory holding ``lvs/``, the
        same root as the object directory's copy. For S3, AWS_DEFAULT_REGION
        must name the bucket's region (see store.Store).
    """

    def __init__(self, root: str) -> None:
        self.store = Store(root)
        self.root = root
        if not self.store.exists(LVS_PARQUET):
            raise FileNotFoundError(
                f"no NED-LVS table at {root}/{LVS_PARQUET} "
                f"(build it: python -m rapidpipe.science.catalogs.store ingest-lvs --dest {root})")
        info = (json.loads(self.store.read_text(LVS_INFO))
                if self.store.exists(LVS_INFO) else {})
        self.release = info.get("release")
        self._table: pa.Table | None = None
        self._ra: np.ndarray | None = None
        self._dec: np.ndarray | None = None
        self._hp6: np.ndarray | None = None
        self.n_reads = 0          # table loads (tests, diagnostics)
        logger.info("NED-LVS local table at %s: release %s", root, self.release)

    def _load(self) -> pa.Table:
        """The table, read on first use."""
        if self._table is None:
            table = self.store.read_table(
                LVS_PARQUET, columns=list(LVS_COLUMNS) + [LVS_HP6_COLUMN])
            self._ra = np.asarray(table["ra"].to_numpy(), dtype=float)
            self._dec = np.asarray(table["dec"].to_numpy(), dtype=float)
            self._hp6 = np.asarray(table[LVS_HP6_COLUMN].to_numpy())
            self._table = table.drop_columns([LVS_HP6_COLUMN])
            self.n_reads += 1
            logger.info("NED-LVS table loaded: %s rows", f"{table.num_rows:,}")
        return self._table

    # -- the NedSliceReader protocol ----------------------------------------
    def __call__(self, ra_deg: float, dec_deg: float,
                 radius_arcsec: float) -> dict[str, np.ndarray]:
        """All NED-LVS rows within `radius_arcsec` of (ra, dec)."""
        table = self._load()
        vec = hp.ang2vec(ra_deg, dec_deg, lonlat=True)
        pixels = hp.query_disc(NSIDE, vec, np.radians(radius_arcsec / 3600.0),
                               inclusive=True, nest=True)
        candidates = np.flatnonzero(np.isin(self._hp6, pixels))
        if candidates.size:
            sep = sep_arcsec(self._ra[candidates], self._dec[candidates],
                             ra_deg, dec_deg)
            candidates = candidates[sep <= radius_arcsec]
        return lvs_table_to_columns(table.take(pa.array(candidates)))


# ---------------------------------------------------------------------------
# Gaia DR3: LINCC's partitions, read by row group
# ---------------------------------------------------------------------------

HP29_ORDER = 29
GAIA_KEY = "_healpix_29"

# The cone is expressed as order-12 pixels (~0.86' across) turned into
# ranges of the order-29 row key; a chip cone is ~150 pixels, ~20 ranges.
GAIA_QUERY_ORDER = 12


def gaia_table_to_columns(table: pa.Table) -> dict[str, np.ndarray]:
    """Gaia rows as the GAIA_COLUMNS contract arrays: numeric nulls become
    NaN, int nulls 0, bool nulls False, string nulls None."""
    out: dict[str, np.ndarray] = {}
    for name in GAIA_NUMERIC_COLUMNS:
        out[name] = np.asarray(table[name].to_numpy(zero_copy_only=False), dtype=float)
    for name in GAIA_INT_COLUMNS:
        out[name] = np.asarray(table[name].fill_null(0).to_numpy(), dtype=np.int64)
    for name in GAIA_BOOL_COLUMNS:
        out[name] = np.asarray(table[name].fill_null(False).to_numpy(zero_copy_only=False),
                               dtype=bool)
    for name in GAIA_STRING_COLUMNS:
        out[name] = np.array([None if v is None else str(v) for v in table[name].to_pylist()],
                             dtype=object)
    return out


def key_ranges_for_cone(ra_deg: float, dec_deg: float, radius_arcsec: float,
                        order: int = GAIA_QUERY_ORDER) -> list[tuple[int, int]]:
    """The cone as sorted, merged [lo, hi] ranges of the order-29 NESTED
    key: every order-`order` pixel the cone overlaps (inclusive), each
    covering a contiguous block of order-29 pixels."""
    vec = hp.ang2vec(ra_deg, dec_deg, lonlat=True)
    pixels = np.sort(hp.query_disc(2 ** order, vec, np.radians(radius_arcsec / 3600.0),
                                   inclusive=True, nest=True))
    shift = 2 * (HP29_ORDER - order)
    ranges: list[tuple[int, int]] = []
    for p in pixels:
        lo, hi = int(p) << shift, ((int(p) + 1) << shift) - 1
        if ranges and lo == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], hi)
        else:
            ranges.append((lo, hi))
    return ranges


class GaiaReader:
    """Cone reads over the local Gaia DR3 copy (``ingest-gaia``, ``gaia-from-table``).

    The copy keeps LINCC's HATS partitions (orders 2-6, <= 2 M rows each,
    sorted by `_healpix_29` across row groups of ~180k rows). A cone
    becomes key ranges (key_ranges_for_cone); each range maps to the one
    partition that contains it (the partition list, read once), and
    within that file to the row groups whose key statistics overlap it
    (the footer, read once per partition and cached). Only those row
    groups are read, each kept in an LRU cache so revisits to a field
    (GBTDS every ~15 min) do not touch S3. The densest bulge chip cone
    touches one row group of ~30 k rows; with the proper-motion padding,
    one or two.

    Contract (NedSliceReader, GAIA_COLUMNS): column arrays for every Gaia
    source in the cone, an EMPTY table when there is none, None when a
    needed partition file is missing from a copy whose ingest record does
    not say complete. A file that cannot be read raises, and the caller
    reports that as "not run". The reader's ``ref_epoch`` is the Julian
    year of the copy's positions (from the ingest record), for the
    caller's proper-motion propagation. ``reads`` lists every (order, npix,
    row group) fetched so far, for a job's record of what it read.

    Parameters
    ----------
    root : str
        ``s3://bucket/prefix`` or a local directory holding the copy
        (hats.properties, partition_info.csv, dataset/, gaia.json). For
        S3, AWS_DEFAULT_REGION must name the bucket's region.
    cache_row_groups : int, optional
        Row groups kept in memory (LRU); ~15 MB each for the contract
        columns.
    """

    def __init__(self, root: str, cache_row_groups: int = 32) -> None:
        self.store = Store(root)
        self.root = root
        if not self.store.exists("partition_info.csv"):
            raise FileNotFoundError(
                f"no Gaia copy at {root} (build it: python -m rapidpipe.science.catalogs.store "
                f"ingest-gaia --dest {root})")
        props = read_properties(self.store.read_text("hats.properties"))
        self.suffix = props.get("hats_npix_suffix", ".parquet")
        self.partitions = set(parse_partition_info(self.store.read_text("partition_info.csv")))
        info = json.loads(self.store.read_text(GAIA_INFO)) if self.store.exists(GAIA_INFO) else {}
        self.release = info.get("release") or props.get("obs_collection")
        self.complete = bool(info.get("complete"))
        self.ref_epoch: float | None = info.get("ref_epoch")
        if self.ref_epoch is None:
            logger.warning("Gaia copy at %s records no reference epoch; the caller "
                           "falls back to its default", root)
        self.cache_row_groups = cache_row_groups
        self._footers: dict[tuple[int, int], Any] = {}        # partition -> ParquetFile
        self._cache: OrderedDict[tuple[int, int, int], pa.Table] = OrderedDict()
        self.n_reads = 0          # row groups actually fetched (tests, diagnostics)
        self.reads: list[tuple[int, int, int]] = []   # every (order, npix, row group) fetched
        logger.info("Gaia local copy at %s: release %s, complete=%s, epoch %s, "
                    "%d partitions", root, self.release, self.complete,
                    self.ref_epoch, len(self.partitions))

    # -- partition lookup ---------------------------------------------------
    def partition_for_key(self, key: int) -> tuple[int, int] | None:
        """The (order, npix) partition holding order-29 pixel `key`: its
        ancestor at the highest order present in the partition list."""
        for order in range(HP6_ORDER, -1, -1):
            candidate = (order, key >> (2 * (HP29_ORDER - order)))
            if candidate in self.partitions:
                return candidate
        return None

    def _footer(self, partition: tuple[int, int]) -> Any:
        """The partition's ParquetFile (footer read once); None if no file."""
        if partition not in self._footers:
            rel = gaia_leaf_rel(*partition, suffix=self.suffix)
            if not self.store.exists(rel):
                self._footers[partition] = None
            else:
                self._footers[partition] = pq.ParquetFile(
                    self.store.fs.open_input_file(self.store.fs_path(rel)))
        return self._footers[partition]

    def _row_group(self, partition: tuple[int, int], index: int) -> pa.Table:
        """One row group's contract columns, from the LRU cache or the store."""
        key = (*partition, index)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        table = self._footer(partition).read_row_group(index, columns=list(GAIA_COLUMNS))
        self.n_reads += 1
        self.reads.append(key)
        self._cache[key] = table
        while len(self._cache) > self.cache_row_groups:
            self._cache.popitem(last=False)
        return table

    def row_groups_for_ranges(self, partition: tuple[int, int],
                              ranges: list[tuple[int, int]]) -> list[int]:
        """Indices of the partition's row groups whose key span overlaps
        any of `ranges` (from the footer statistics)."""
        pf = self._footer(partition)
        col = pf.schema_arrow.get_field_index(GAIA_KEY)
        out = []
        for i in range(pf.metadata.num_row_groups):
            stats = pf.metadata.row_group(i).column(col).statistics
            if stats is None or not stats.has_min_max:
                out.append(i)                       # no stats: cannot exclude it
                continue
            if any(lo <= stats.max and hi >= stats.min for lo, hi in ranges):
                out.append(i)
        return out

    # -- the NedSliceReader protocol ----------------------------------------
    def __call__(self, ra_deg: float, dec_deg: float,
                 radius_arcsec: float) -> dict[str, np.ndarray] | None:
        """All Gaia rows within `radius_arcsec` of (ra, dec), or None when a
        needed partition is missing from an incomplete copy."""
        by_partition: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for lo, hi in key_ranges_for_cone(ra_deg, dec_deg, radius_arcsec):
            partition = self.partition_for_key(lo)
            if partition is None:
                continue                            # sky the catalog has no partition for
            by_partition.setdefault(partition, []).append((lo, hi))
        parts: list[pa.Table] = []
        for partition, ranges in by_partition.items():
            if self._footer(partition) is None:
                if not self.complete:
                    logger.warning("Gaia copy at %s has no file for partition %s and "
                                   "is not marked complete; Gaia matching not run",
                                   self.root, partition)
                    return None
                continue                            # complete copy: empty sky here
            for i in self.row_groups_for_ranges(partition, ranges):
                parts.append(self._row_group(partition, i))
        if not parts:
            return gaia_table_to_columns(pa.table({c: pa.array([], type=pa.float64())
                                                   for c in GAIA_COLUMNS}))
        table = pa.concat_tables(parts)
        sep = sep_arcsec(table["ra"].to_numpy(), table["dec"].to_numpy(), ra_deg, dec_deg)
        return gaia_table_to_columns(table.filter(pa.array(sep <= radius_arcsec)))


# ---------------------------------------------------------------------------
# A catalogs product's location (rebuild only, 2026-10-29)
# ---------------------------------------------------------------------------

def mirror_root(location: str) -> str:
    """The store root a ``catalogs`` location names: ``s3://bucket/prefix``
    as it is, a ``file://`` URL as its directory, a plain path as itself."""
    if location.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(location).path)
    return location


def gaia_reader(location: str, cache_row_groups: int = 32) -> GaiaReader:
    """The :class:`GaiaReader` over the mirror a ``catalogs`` row's location names."""
    return GaiaReader(mirror_root(location), cache_row_groups=cache_row_groups)


def cone_of_corners(corners: tuple[float, ...]) -> tuple[float, float, float]:
    """The cone holding a footprint: its centre and the largest centre-to-corner separation.

    ``corners`` is ``(ra1, dec1, ..., ra4, dec4)`` in degrees. The centre
    is the mean of the corners' unit vectors; the radius is in arcseconds.
    """
    ra = np.radians(np.asarray(corners[0::2], dtype=float))
    dec = np.radians(np.asarray(corners[1::2], dtype=float))
    xyz = np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=1)
    centre = xyz.mean(axis=0)
    centre /= np.linalg.norm(centre)
    ra0 = float(np.degrees(np.arctan2(centre[1], centre[0])) % 360.0)
    dec0 = float(np.degrees(np.arcsin(np.clip(centre[2], -1.0, 1.0))))
    radius = float(sep_arcsec(np.degrees(ra), np.degrees(dec), ra0, dec0).max())
    return ra0, dec0, radius
