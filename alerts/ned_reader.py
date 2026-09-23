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
"""

import json
import logging
from collections import OrderedDict
from typing import Any

import healpy as hp
import numpy as np
import pyarrow as pa

from .ned_catalog import HP6_ORDER, MANIFEST_NAME, Store, hp6_file
from .providers import NED_COLUMNS, _sep_arcsec

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
