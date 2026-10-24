"""Write an injected dataset: constant sources in every epoch after a bound, the epochs before it copied.

One-time, outside the pipeline (plan Part 6 rows 13 and 14, Ben 2026-10-22:
injection outside the pipeline as a dataset): the pipeline never sees this
script, only a source root whose files carry sources a truth table names.
For a selection of a SOC L2 source root (``--filter``, ``--scas``,
``--pointings``, the loop's grammar), every file whose exposure starts
after ``--bound-mjd`` (read from its ``_wcs.asdf`` sidecar) gets constant
sources by dev's ``inject_variable_stars_into_l2`` as it stands
(``scripts/rapid_l2_injections.py``, its CLI's ``fix_wcs=False``); every
file before the bound is copied byte for byte. Each goes to
``<dest>l2/<stem>_cal.asdf``, the name and the file's ``meta`` unchanged, so
the intake reads it as it reads the source root; nothing else goes under
``l2/`` (the loop lists the whole prefix).

The catalog, per pointing and SCA (visit, exposure, SCA): ``--per-image``
positions uniform in pixels with a 100-pixel margin, through the earliest
injected file's own stored WCS to RA/Dec, and AB magnitudes uniform in
``--mag-range``, drawn from a generator seeded by (``--seed``, visit,
exposure, SCA), so a rerun draws the same catalog and the same source sits
at the same sky position in every later epoch of its pointing (each file
places it through its own stored WCS, as dev's function does). The catalog
is dev's JSON shape, ``sinusoidal`` with ``amplitude`` 0 (dev has no
constant type; a zero-amplitude sinusoid is one).

The truth table, one row per source per injected epoch (``src_id, ra, dec,
x, y, mag_ab, dnps, zp, expid, sca, fid, mjd, dataset``; ``x``, ``y`` the
zero-based pixel the file's stored WCS gives, where the source was rendered;
``dnps`` the injected DN/s from the file's own photometric zero point
``zp``), is written to ``--truth`` and uploaded once to
``<dest>catalog/<its file name>``, never rewritten; ``completeness.py``
reads every file there.

Writes are one ``put-object`` per object with ``If-None-Match: *`` (the
inputs bucket's policy refuses anything else): an object already there is
skipped and reported, never overwritten. ``--dry-run`` lists what would be
injected, copied and skipped and writes nothing. Prints one line per file
(stem, injected|copied|skipped, seconds) and a summary.

    python scripts/inject_dataset.py --source-root s3://.../l2/ --filter F146 --scas 8 \\
        --pointings 001_0001 --bound-mjd 61591.4056 --dest s3://bucket/prefix/r00342-inj0/ \\
        --dataset r00342-inj0 --truth DIR/truth-sca8.parquet [--seed 20261024] [--per-image 200] \\
        [--mag-range 21 27] [--workers 8] [--dry-run]

The CRDS server and cache come from ``CRDS_SERVER_URL`` (default the public
Roman server) and ``CRDS_PATH`` (default ``$TMPDIR/inject-dataset/crds``);
``CRDS_CONTEXT`` is pinned per file to the file's own
``meta.ref_file.crds.context``, so the ePSF and gain are the file's own
references; the first two are set before any import, since crds reads them
when it is first imported. Working files go under ``--work`` (default ``$TMPDIR``), one
file's input and output at a time per worker, removed after the upload.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable

CRDS_SERVER = "https://roman-crds.stsci.edu"
os.environ.setdefault("CRDS_SERVER_URL", CRDS_SERVER)
os.environ.setdefault("CRDS_PATH", str(Path(os.environ.get("TMPDIR", ".")) / "inject-dataset" / "crds"))

import numpy as np  # noqa: E402 - after the CRDS environment

from rapidpipe.foundation.loop import STEM, Selection, list_stems, stem_expid  # noqa: E402
from rapidpipe.stages.intake import FIDS  # noqa: E402
from rapidpipe.products import storage  # noqa: E402

#: Pixels kept clear of the edge when drawing positions (the packet stamp is 129 px).
MARGIN = 100
#: Dev's on-image test keeps a source within this many pixels outside the image.
DEV_EDGE = 50.0
#: The intake's filter names by the stem's ``fNNN``.
FILTER_NAMES = {"f062": "R062", "f087": "Z087", "f106": "Y106", "f129": "J129", "f146": "W146",
                "f158": "H158", "f184": "F184", "f213": "K213"}
TRUTH_COLUMNS = ("src_id", "ra", "dec", "x", "y", "mag_ab", "dnps", "zp", "expid", "sca", "fid",
                 "mjd", "dataset")


def split(url: str) -> tuple[str, str]:
    bucket, _, prefix = url.removeprefix("s3://").partition("/")
    return bucket, prefix


def pointing_key(stem: str) -> tuple[int, int, int]:
    """(visit, exposure, SCA): the catalog's unit."""
    m = STEM.match(stem)
    return int(m["visit"]), int(m["exposure"]), int(m["sca"])


def draw_catalog(seed: int, key: tuple[int, int, int], n: int, mag_range: tuple[float, float],
                 shape: tuple[int, int], to_sky: Callable) -> list[dict]:
    """``n`` sources for one pointing and SCA: pixels uniform inside the margin, through ``to_sky``
    (the earliest injected file's stored WCS, zero-based) to RA/Dec, magnitudes uniform in the range."""
    visit, exposure, sca = key
    rng = np.random.default_rng([seed, visit, exposure, sca])
    ny, nx = shape
    x = rng.uniform(MARGIN, nx - 1 - MARGIN, n)
    y = rng.uniform(MARGIN, ny - 1 - MARGIN, n)
    mag = rng.uniform(mag_range[0], mag_range[1], n)
    ra, dec = to_sky(x, y)
    return [{"src_id": int(f"{visit:03d}{exposure:04d}{sca:02d}{i:04d}"), "ra": float(r), "dec": float(d),
             "mag": float(m)} for i, (r, d, m) in enumerate(zip(np.atleast_1d(ra), np.atleast_1d(dec), mag))]


def catalog_json(sources: list[dict]) -> dict:
    """Dev's injection catalog JSON for constant sources (a sinusoid of amplitude 0)."""
    return {str(s["src_id"]): {"type": "sinusoidal", "ra": s["ra"], "dec": s["dec"],
                               "parameters": {"period": 10.0, "amplitude": 0.0, "magnitude": s["mag"],
                                              "phase": 0.0}}
            for s in sources}


def truth_rows(sources: list[dict], image: dict, dataset: str) -> list[dict]:
    """One row per source dev's function places on ``image`` (its ``to_pixel``, ``shape``, ``mjd``,
    ``zp``, ``expid``, ``sca``, ``fid``)."""
    ra = np.array([s["ra"] for s in sources])
    dec = np.array([s["dec"] for s in sources])
    x, y = image["to_pixel"](ra, dec)
    ny, nx = image["shape"]
    rows = []
    for s, xi, yi in zip(sources, np.atleast_1d(x), np.atleast_1d(y)):
        if not (-DEV_EDGE <= xi < nx + DEV_EDGE and -DEV_EDGE <= yi < ny + DEV_EDGE):
            continue
        rows.append({"src_id": s["src_id"], "ra": s["ra"], "dec": s["dec"], "x": float(xi), "y": float(yi),
                     "mag_ab": s["mag"], "dnps": float(10 ** (-0.4 * (s["mag"] - image["zp"]))),
                     "zp": image["zp"], "expid": image["expid"], "sca": image["sca"], "fid": image["fid"],
                     "mjd": image["mjd"], "dataset": dataset})
    return rows


def file_seed(seed: int, stem: str) -> int:
    """The injection's noise seed for one file, from ``--seed`` and the file's expid and SCA."""
    return int(np.random.SeedSequence([seed, stem_expid(stem), pointing_key(stem)[2]]).generate_state(1)[0])


def put_once(client, url: str, body: Any) -> bool:
    """One ``put-object`` with ``If-None-Match: *``; False when the object already exists."""
    bucket, key = split(url)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ChecksumAlgorithm="SHA256")
    except Exception as exc:  # noqa: BLE001 - ClientError-shaped
        if storage.client_error_code(exc) in ("PreconditionFailed", "412"):
            return False
        raise
    return True


def open_image(path: Path, stem: str) -> dict:
    """The stored WCS, shape, start MJD, zero point and keys of one L2 file (the real reader)."""
    import roman_datamodels as rdm
    import romanisim.wcs

    dm = rdm.open(path)
    wcs = romanisim.wcs.GWCS(dm.meta.wcs)
    phot = dm.meta.photometry
    zp = -2.5 * np.log10(float(phot.conversion_megajanskys) * 1e6 * float(phot.pixel_area) / 3631.0)
    image = {"shape": tuple(dm.data.shape), "mjd": float(dm.meta.exposure.start_time.mjd), "zp": float(zp),
             "context": str(dm.meta.ref_file.crds.context),
             "to_sky": lambda x, y: dm.meta.wcs(x, y),
             "to_pixel": lambda ra, dec: wcs.toImage(ra, dec, units="deg")}
    return keyed(image, stem)


def keyed(image: dict, stem: str) -> dict:
    m = STEM.match(stem)
    return {**image, "expid": stem_expid(stem), "sca": int(m["sca"]), "fid": FIDS[FILTER_NAMES[m["filter"]]]}


def inject_file(path: Path, list_file: Path, out: Path, seed: int, context: str) -> None:
    """Dev's injection, its CRDS context pinned to the file's own."""
    os.environ["CRDS_CONTEXT"] = context
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import rapid_l2_injections  # noqa: E402 - the copy of dev's module beside this script

    rapid_l2_injections.inject_variable_stars_into_l2(str(path), str(list_file), str(out), fix_wcs=False,
                                                      seed=seed)


def process_pointing(stems: list[str], actions: dict[str, str], ctx: dict) -> tuple[list[str], list[dict]]:
    """Copy and inject one pointing and SCA's files; its report lines and truth rows.

    ``ctx``: ``source``/``dest`` (``s3://`` prefixes), ``source_client``, ``dest_client``,
    ``existing`` (names already under ``dest``'s ``l2/``), ``work``, ``seed``, ``per_image``,
    ``mag_range``, ``dataset``, and the readers ``open_image`` and ``inject``.
    """
    lines, rows = [], []
    src_bucket, src_prefix = split(ctx["source"])
    work = Path(ctx["work"])
    work.mkdir(parents=True, exist_ok=True)

    def fetch(stem: str) -> Path:
        path = work / f"{stem}_cal.asdf"
        body = ctx["source_client"].get_object(Bucket=src_bucket, Key=src_prefix + stem + "_cal.asdf")["Body"]
        path.write_bytes(body.read())
        return path

    for stem in [s for s in stems if actions[s] == "copy"]:
        t0 = time.perf_counter()
        name = stem + "_cal.asdf"
        if name in ctx["existing"]:
            lines.append(f"{stem} skipped 0.0")
            continue
        body = ctx["source_client"].get_object(Bucket=src_bucket, Key=src_prefix + name)["Body"].read()
        done = put_once(ctx["dest_client"], ctx["dest"] + "l2/" + name, body)
        lines.append(f"{stem} {'copied' if done else 'skipped'} {time.perf_counter() - t0:.1f}")

    inject = [s for s in stems if actions[s] == "inject"]
    todo = [s for s in inject if s + "_cal.asdf" not in ctx["existing"]]
    lines += [f"{s} skipped 0.0" for s in inject if s not in todo]
    if not todo:
        return lines, rows
    first = inject[0]
    t0 = time.perf_counter()
    path = fetch(first)
    image = ctx["open_image"](path, first)
    sources = draw_catalog(ctx["seed"], pointing_key(first), ctx["per_image"], ctx["mag_range"],
                           image["shape"], image["to_sky"])
    list_file = work / f"{first}.list"
    catalog_file = work / f"{first}.json"
    catalog_file.write_text(json.dumps(catalog_json(sources)))
    list_file.write_text(str(catalog_file) + "\n")
    for stem in todo:
        if stem != first:
            t0 = time.perf_counter()
            path.unlink()
            path = fetch(stem)
            image = ctx["open_image"](path, stem)
        out = work / f"{stem}_injected_cal.asdf"
        ctx["inject"](path, list_file, out, file_seed(ctx["seed"], stem), image["context"])
        with out.open("rb") as body:
            done = put_once(ctx["dest_client"], ctx["dest"] + "l2/" + stem + "_cal.asdf", body)
        out.unlink()
        if done:
            rows += truth_rows(sources, image, ctx["dataset"])
        lines.append(f"{stem} {'injected' if done else 'skipped'} {time.perf_counter() - t0:.1f}")
    path.unlink()
    for f in (list_file, catalog_file):
        f.unlink()
    return lines, rows


def sidecar_mjd(client, source: str, stem: str) -> float:
    """The exposure's start MJD from the file's ``_wcs.asdf`` sidecar (49 KB, not the 200 MB file)."""
    import asdf
    from astropy.time import Time

    bucket, prefix = split(source)
    data = client.get_object(Bucket=bucket, Key=prefix + stem + "_wcs.asdf")["Body"].read()
    with asdf.open(io.BytesIO(data), lazy_load=False, memmap=False) as af:
        return float(Time(af.tree["roman"]["meta"]["exposure"]["start_time"]).mjd)


def plan(stems: list[str], mjds: dict[str, float], bound: float) -> tuple[dict, dict]:
    """Each stem's action (``inject`` after the bound, else ``copy``) and the stems per pointing and SCA,
    in start order."""
    actions = {s: ("inject" if mjds[s] > bound else "copy") for s in stems}
    groups: dict[tuple, list[str]] = defaultdict(list)
    for s in sorted(stems, key=lambda s: mjds[s]):
        groups[pointing_key(s)].append(s)
    return actions, dict(groups)


def _worker(args: tuple) -> tuple[list[str], list[dict]]:
    stems, actions, ctx = args
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    ctx = {**ctx, "source_client": boto3.client("s3", config=Config(signature_version=UNSIGNED)),
           "dest_client": boto3.client("s3"), "open_image": open_image, "inject": inject_file}
    return process_pointing(stems, actions, ctx)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source-root", required=True, help="the public L2 prefix, s3://bucket/prefix/ (read unsigned)")
    p.add_argument("--filter", required=True)
    p.add_argument("--scas", required=True, help="comma-separated")
    p.add_argument("--pointings", required=True, help="VVV_EEEE,... or all")
    p.add_argument("--bound-mjd", type=float, required=True)
    p.add_argument("--dest", required=True, help="the dataset's prefix, s3://bucket/prefix/; l2/ and catalog/ under it")
    p.add_argument("--dataset", required=True)
    p.add_argument("--truth", type=Path, required=True, help="local Parquet; uploaded as <dest>catalog/<its name>")
    p.add_argument("--seed", type=int, default=20261024)
    p.add_argument("--per-image", type=int, default=200)
    p.add_argument("--mag-range", type=float, nargs=2, default=(21.0, 27.0))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--work", type=Path, default=Path(os.environ.get("TMPDIR", ".")) / "inject-dataset")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    source = a.source_root.rstrip("/") + "/"
    dest = a.dest.rstrip("/") + "/"

    import boto3
    import pyarrow as pa
    import pyarrow.parquet as pq
    from botocore import UNSIGNED
    from botocore.config import Config

    t_start = time.perf_counter()
    unsigned = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    signed = boto3.client("s3")
    selection = Selection.parse(a.filter, a.scas, a.pointings)
    stems = sorted(s for s in list_stems(unsigned, source, selection) if selection.admits_stem(s))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(16) as pool:
        mjds = dict(zip(stems, pool.map(lambda s: sidecar_mjd(unsigned, source, s), stems)))
    actions, groups = plan(stems, mjds, a.bound_mjd)
    bucket, prefix = split(dest + "l2/")
    existing = {item["Key"].rsplit("/", 1)[-1]
                for page in signed.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
                for item in page.get("Contents", [])}
    n_inject = sum(1 for s in stems if actions[s] == "inject")
    print(f"selection {selection.summary()}: {len(stems)} files, {n_inject} to inject, "
          f"{len(stems) - n_inject} to copy, {sum(1 for s in stems if s + '_cal.asdf' in existing)} already "
          f"under {dest}l2/, {len(groups)} pointings x SCAs", flush=True)
    if a.dry_run:
        for s in stems:
            state = "skip" if s + "_cal.asdf" in existing else actions[s]
            print(f"{s} {state} mjd {mjds[s]:.5f}")
        return 0

    ctx = {"source": source, "dest": dest, "existing": existing, "seed": a.seed, "per_image": a.per_image,
           "mag_range": tuple(a.mag_range), "dataset": a.dataset}
    jobs = [(g, {s: actions[s] for s in g}, {**ctx, "work": str(a.work / f"{k[0]:03d}_{k[1]:04d}_{k[2]:02d}")})
            for k, g in sorted(groups.items())]
    counts, rows = defaultdict(int), []
    with ProcessPoolExecutor(a.workers) as pool:
        for lines, got in pool.map(_worker, jobs):
            for line in lines:
                print(line, flush=True)
                counts[line.split()[1]] += 1
            rows += got
    a.truth.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=pa.schema(
        [(c, pa.string() if c == "dataset" else pa.int64() if c in ("src_id", "expid", "sca", "fid")
          else pa.float64()) for c in TRUTH_COLUMNS]))
    pq.write_table(table, a.truth)
    with a.truth.open("rb") as body:
        uploaded = bool(rows) and put_once(signed, dest + "catalog/" + a.truth.name, body)
    print(f"summary: {dict(counts)}, {len(rows)} truth rows to {a.truth}"
          f" ({'uploaded to ' + dest + 'catalog/' + a.truth.name if uploaded else 'not uploaded'}),"
          f" {time.perf_counter() - t_start:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
