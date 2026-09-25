"""
File    : ned_reader.py
Author  : Emily Everetts, Claude Code
Date    : 09/26

A NedSliceReader over the local copy of the NED object directory: the
order-6 HEALPix parquet files that alerts.ned_catalog builds under
``<prefix>/objectdir_hp6/hp6=<pixel>/part.parquet`` (S3 or a local dir).

    reader = Hp6NedReader("s3://rapid-pipeline-files/ned")
    provider = AlertDataProvider(db, ned_reader=reader)      # or make_provider(ned_source=...)

Contract (providers.NedSliceReader): ``reader(ra_deg, dec_deg, radius_arcsec)``
returns column arrays keyed by providers.NED_COLUMNS for every NED object
in the cone, an EMPTY table when NED has nothing there, and None only when
the slice could not be obtained. With a complete local copy that last case
means the copy could not be read at all; a pixel with no file is genuinely
empty sky, because the repartition writes a file for every pixel that has
rows.

Why this exists: the web service (AstroqueryNedReader) took 3-24 s per chip
and timed out for hours at a time (2026-09-18/21). A cone here is a few
parquet reads -- one file per order-6 pixel the cone touches, at most four
for a Roman chip -- served from an in-process cache, so repeat visits to a
field (GBTDS every ~15 min) do not touch S3 again.

Also here: LvsReader, the same contract over NED-LVS (``<prefix>/lvs/
nedlvs.parquet``, built by ``alerts.ned_catalog ingest-lvs``), which at
~2.1 M rows is simply held whole in memory rather than cut into pixel files.

    reader = LvsReader("s3://rapid-pipeline-files/ned")
    provider = AlertDataProvider(db, lvs_reader=reader)    # or make_provider(lvs=True)
"""

import json
import logging
from collections import OrderedDict
from typing import Any

import healpy as hp
import numpy as np
import pyarrow as pa

from .ned_catalog import (HP6_ORDER, LVS_HP6_COLUMN, LVS_INFO, LVS_PARQUET,
                          MANIFEST_NAME, Store, hp6_file)
from .providers import (LVS_BOOL_COLUMNS, LVS_COLUMNS, LVS_NUMERIC_COLUMNS,
                        LVS_STRING_COLUMNS, NED_COLUMNS, _sep_arcsec)

logger = logging.getLogger(__name__)

NSIDE = 2 ** HP6_ORDER

# Where the pipeline's copy lives; the CLI (--ned-source) and the alert
# stage ([ALERTS] ned_source) default to this.
DEFAULT_NED_SOURCE = "s3://rapid-pipeline-files/ned"

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
        name the bucket's region (see ned_catalog.Store).
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
        sep = _sep_arcsec(merged["ra"], merged["dec"], ra_deg, dec_deg)
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
    """Cone reads over the local NED-LVS table (alerts.ned_catalog ingest-lvs).

    NED-LVS is ~2.1 M rows, so unlike the object directory it is not cut
    into pixel files: the LVS_COLUMNS subset of ``<root>/lvs/nedlvs.parquet``
    is read once, on first use, and kept as an Arrow table (~0.3 GB; kept
    as Arrow rather than numpy object arrays because the four string
    columns would otherwise cost most of a gigabyte per process). A cone is
    an order-6 pixel pre-filter over the stored `_hp6` column, then an exact
    separation cut, and only the rows inside are converted to the contract
    arrays.

    Contract (providers.NedSliceReader, LVS_COLUMNS): column arrays for
    every LVS galaxy in the cone, an EMPTY table when there is none. It
    never returns None -- a table that cannot be read raises, and the
    provider reports that as "not run".

    Parameters
    ----------
    root : str
        ``s3://bucket/prefix`` or a local directory holding ``lvs/`` -- the
        same root as the object directory's copy. For S3, AWS_DEFAULT_REGION
        must name the bucket's region (see ned_catalog.Store).
    """

    def __init__(self, root: str) -> None:
        self.store = Store(root)
        self.root = root
        if not self.store.exists(LVS_PARQUET):
            raise FileNotFoundError(
                f"no NED-LVS table at {root}/{LVS_PARQUET} "
                f"(build it: python -m alerts.ned_catalog ingest-lvs --dest {root})")
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
            sep = _sep_arcsec(self._ra[candidates], self._dec[candidates],
                              ra_deg, dec_deg)
            candidates = candidates[sep <= radius_arcsec]
        return lvs_table_to_columns(table.take(pa.array(candidates)))
