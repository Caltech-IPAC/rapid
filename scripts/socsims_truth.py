# ported-from: none
"""Write the truth table of `dev`'s socsims fake-source dataset from its injection catalogs.

One-time, outside the pipeline: `dev`'s injection (``sims/src/socsims/
inject_fake_sources_into_l2_asdf_files.py``, 2026-08-11 to 08-18) drew one
JSON catalog per Roman tessellation field, ``injection_catalog_rtid<N>.json``
(100 sources each, 50 ``sinusoidal`` and 50 ``gaussian``, their light-curve
parameters), evaluated the catalogs of the image's centre field and its
neighbours at the exposure's start MJD and rendered the sources that fell
within 50 px of the image (``modules/fake_src/rapid_l2_injections.py``,
``_evaluate_catalogs_at_mjd``); it kept no record per exposure. This script
repeats the evaluation, never the rendering, for every lite file of the
dataset and writes the truth table ``inject_dataset.py`` writes (``src_id,
ra, dec, x, y, mag_ab, dnps, zp, expid, sca, fid, mjd, dataset``) so
``completeness.py`` reads it unchanged:

- the file's first 32 KB (one ranged read; both headers, nothing of the
  image) give ``MJD-OBS`` (the start, as `dev` evaluated), ``ZPTMAG``, the
  shape and the TAN-SIP WCS `dev` fitted to the re-derived gWCS the sources
  were placed through; ``x``, ``y`` are zero-based pixels through it;
- the centre field is the closed-form tessellation's (`dev` used the SQLite
  class; tile identity is certified identical, neighbours verified
  tile-for-tile), the catalogs are read once from ``--catalogs``, and a field
  whose catalog is absent fails the file: `dev` generated every catalog it
  needed and uploaded it to the prefix the copy came from;
- the models are `dev`'s: a sinusoid in magnitude, ``m + A sin(2 pi (t / P +
  phase))``; a gaussian burst in flux over a static magnitude, ``f (1 + (10 ^
  (0.4 A) - 1) exp(-(t - t0)^2 / 2 sigma^2))``; ``mag_ab`` is the evaluated
  AB magnitude and ``dnps`` its DN/s through the file's zero point, the
  convention ``inject_dataset.py`` writes (the rendering's own conversion
  is romanisim's, not read back).

The table is written to ``--truth`` and uploaded once to
``<source-root>catalog/<its name>`` with ``If-None-Match: *``, never
rewritten.

    python scripts/socsims_truth.py --source-root s3://bucket/g0005-fits-20260821/ \\
        --catalogs s3://bucket/g0005-fits-20260821/catalog/injection_catalogs_20260811/ \\
        --dataset gbtds-r00340-fakesrc --truth DIR/gbtds-r00340-fakesrc.parquet \\
        [--filters F146] [--scas 1,...,18] [--passes all] [--expids all] [--workers 16]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from rapidpipe.foundation.loop import LITE_SUFFIX, SOCSIMS_LITE, STEM, Selection, list_stems, stem_expid
from rapidpipe.products import storage
from rapidpipe.stages.intake import FIDS

#: `dev`'s on-image test keeps a source within this many pixels outside the image.
DEV_EDGE = 50.0
#: Bytes of the gzipped file that hold both headers (16 KB does on every file read; twice that).
HEAD_BYTES = 32768
BLOCK = 2880
END_CARD = b"END" + b" " * 77
TRUTH_COLUMNS = ("src_id", "ra", "dec", "x", "y", "mag_ab", "dnps", "zp", "expid", "sca", "fid",
                 "mjd", "dataset")
FILTER_NAMES = {"f062": "R062", "f087": "Z087", "f106": "Y106", "f129": "J129", "f146": "W146",
                "f158": "H158", "f184": "F184", "f213": "K213"}


def split(url: str) -> tuple[str, str]:
    bucket, _, prefix = url.removeprefix("s3://").partition("/")
    return bucket, prefix


def headers_from_gz(raw: bytes) -> list:
    """The FITS headers the start of a gzipped file holds, each ended by its END card; a file whose
    bytes end inside a header gives only the complete ones."""
    from astropy.io import fits

    data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
    headers, start = [], 0
    for at in range(0, len(data) - BLOCK + 1, BLOCK):
        block = data[at:at + BLOCK]
        if any(block[i:i + 80] == END_CARD for i in range(0, BLOCK, 80)):
            headers.append(fits.Header.fromstring(data[start:at + BLOCK].decode("ascii")))
            start = at + BLOCK
    return headers


def evaluate(sources: list[dict], mjd: float) -> np.ndarray:
    """`dev`'s light curves at ``mjd``, in maggies (``_evaluate_catalogs_at_mjd``)."""
    flux = np.empty(len(sources))
    for i, s in enumerate(sources):
        p = s["parameters"]
        if s["type"] == "sinusoidal":
            mag = p["magnitude"] + p["amplitude"] * np.sin(2 * np.pi * (mjd / p["period"] + p["phase"]))
            flux[i] = 10 ** (-0.4 * mag)
        elif s["type"] == "gaussian":
            static = 10 ** (-0.4 * p["magnitude"])
            burst = static * (10 ** (0.4 * p["peak_amplitude"]) - 1.0)
            flux[i] = static + burst * np.exp(-0.5 * ((mjd - p["peak_time"]) / p["sigma"]) ** 2)
        else:
            raise ValueError(f"unknown light curve type {s['type']!r}")
    return flux


def image(header, stem: str) -> dict:
    """What one lite file's image header gives the truth: shape, start MJD, zero point, the stored
    WCS as ``to_pixel`` (zero-based) and ``centre`` (RA, Dec), and the stem's keys."""
    from astropy.wcs import WCS

    wcs = WCS(header)
    ny, nx = int(header["NAXIS2"]), int(header["NAXIS1"])
    m = STEM.match(stem)
    return {"shape": (ny, nx), "mjd": float(header["MJD-OBS"]), "zp": float(header["ZPTMAG"]),
            "to_pixel": lambda ra, dec: wcs.all_world2pix(ra, dec, 0),
            "centre": tuple(float(v) for v in wcs.all_pix2world((nx - 1) / 2.0, (ny - 1) / 2.0, 0)),
            "expid": stem_expid(stem), "sca": int(m["sca"]), "fid": FIDS[FILTER_NAMES[m["filter"]]]}


def fields(ra: float, dec: float) -> list[int]:
    """The tessellation field holding (``ra``, ``dec``) and its neighbours, as `dev` chose the
    catalogs to inject from."""
    tess = RomanTessellationClosedForm()
    centre = int(tess.get_rtid(ra, dec))
    return [centre] + [int(r) for r in tess.get_all_neighboring_rtids(centre)]


def truth_rows(catalogs: dict[int, dict], image: dict, dataset: str) -> list[dict]:
    """One row per source of the image's fields' catalogs that `dev`'s function placed on
    ``image``, at its evaluated magnitude."""
    sources = [(int(k), s) for f in fields(*image["centre"]) for k, s in catalogs[f].items()]
    if not sources:
        return []
    ra = np.array([s["ra"] for _, s in sources])
    dec = np.array([s["dec"] for _, s in sources])
    x, y = (np.atleast_1d(v) for v in image["to_pixel"](ra, dec))
    ny, nx = image["shape"]
    on = (x >= -DEV_EDGE) & (x < nx + DEV_EDGE) & (y >= -DEV_EDGE) & (y < ny + DEV_EDGE)
    mag = -2.5 * np.log10(evaluate([s for (_, s), k in zip(sources, on) if k], image["mjd"]))
    rows = []
    for (sid, s), xi, yi, mi in zip([p for p, k in zip(sources, on) if k], x[on], y[on], mag):
        rows.append({"src_id": sid, "ra": s["ra"], "dec": s["dec"], "x": float(xi), "y": float(yi),
                     "mag_ab": float(mi), "dnps": float(10 ** (-0.4 * (mi - image["zp"]))),
                     "zp": image["zp"], "expid": image["expid"], "sca": image["sca"], "fid": image["fid"],
                     "mjd": image["mjd"], "dataset": dataset})
    return rows


def load_catalogs(client, where: str) -> dict[int, dict]:
    """Every ``injection_catalog_rtid<N>.json`` under ``where`` (an S3 prefix or a directory), by field."""
    out = {}
    if where.startswith("s3://"):
        bucket, prefix = split(where.rstrip("/") + "/")
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                name = item["Key"].rsplit("/", 1)[-1]
                if name.startswith("injection_catalog_rtid") and name.endswith(".json"):
                    body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
                    out[int(name[len("injection_catalog_rtid"):-len(".json")])] = json.loads(body)
        return out
    for path in Path(where).glob("injection_catalog_rtid*.json"):
        out[int(path.stem[len("injection_catalog_rtid"):])] = json.loads(path.read_text())
    return out


def file_rows(client, source: str, stem: str, catalogs: dict[int, dict], dataset: str) -> list[dict]:
    """The truth rows of one lite file, from its first :data:`HEAD_BYTES`."""
    bucket, key = split(source + stem + LITE_SUFFIX)
    raw = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{HEAD_BYTES - 1}")["Body"].read()
    headers = headers_from_gz(raw)
    if len(headers) < 2:
        raise ValueError(f"{stem}: the first {HEAD_BYTES} bytes hold {len(headers)} complete header(s), not 2")
    return truth_rows(catalogs, image(headers[1], stem), dataset)


def put_once(client, url: str, body) -> bool:
    """One ``put-object`` with ``If-None-Match: *``; False when the object already exists."""
    bucket, key = split(url)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ChecksumAlgorithm="SHA256")
    except Exception as exc:  # noqa: BLE001 - ClientError-shaped
        if storage.client_error_code(exc) in ("PreconditionFailed", "412"):
            return False
        raise
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source-root", required=True, help="the lite files' prefix, s3://bucket/prefix/")
    p.add_argument("--catalogs", required=True, help="the injection catalogs, an s3:// prefix or a directory")
    p.add_argument("--dataset", required=True)
    p.add_argument("--truth", type=Path, required=True, help="local Parquet; uploaded as <source-root>catalog/<its name>")
    p.add_argument("--filters", default="F146")
    p.add_argument("--scas", default=",".join(str(n) for n in range(1, 19)), help="comma-separated")
    p.add_argument("--passes", default="all")
    p.add_argument("--expids", default="all")
    p.add_argument("--workers", type=int, default=16)
    a = p.parse_args(argv)
    source = a.source_root.rstrip("/") + "/"

    import pyarrow as pa
    import pyarrow.parquet as pq

    t_start = time.perf_counter()
    client = storage.s3_client()
    selection = Selection.parse(a.filters, a.scas, "all", a.passes, reader=SOCSIMS_LITE, expids=a.expids)
    stems = sorted(s for s in list_stems(client, source, selection) if selection.admits_stem(s))
    catalogs = load_catalogs(client, a.catalogs)
    print(f"selection {selection.summary()}: {len(stems)} files, {len(catalogs)} catalogs", flush=True)
    rows = []
    with ThreadPoolExecutor(a.workers) as pool:
        for stem, got in zip(stems, pool.map(lambda s: file_rows(client, source, s, catalogs, a.dataset), stems)):
            rows += got
            print(f"{stem} {len(got)} sources", flush=True)
    a.truth.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=pa.schema(
        [(c, pa.string() if c == "dataset" else pa.int64() if c in ("src_id", "expid", "sca", "fid")
          else pa.float64()) for c in TRUTH_COLUMNS]))
    pq.write_table(table, a.truth)
    with a.truth.open("rb") as body:
        uploaded = bool(rows) and put_once(client, source + "catalog/" + a.truth.name, body)
    print(f"summary: {len(stems)} files, {len(rows)} truth rows to {a.truth}"
          f" ({'uploaded to ' + source + 'catalog/' + a.truth.name if uploaded else 'not uploaded'}),"
          f" {time.perf_counter() - t_start:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
