# ported-from: alerts/catalog_store.py @ a41c4d7b
"""The external catalogs behind the alert cross-matches, each copied into S3
or a local directory without ever staging it on a disk: the NED object
directory (mirror, verify, repartition), NED-LVS (ingest-lvs) and Gaia DR3
(ingest-gaia, gaia-from-table). `dev`'s ``alerts/catalog_store.py`` (Emily
Everetts, Claude Code, 09/2026), ported whole; the departures are listed at
the end of this docstring. NED first.

Caltech/IPAC-IRSA publishes NED as a HATS collection (HEALPix-partitioned
parquet). Its 264 leaves are order-2/3 pixels of 50-200 square degrees, up to
8 million rows each, and, despite hats.properties, not sorted within a
file. Fine for archiving, far too coarse for a per-chip cone read (one chip
cone overlaps ~6 million rows). So the collection is mirrored, re-cut, and
the mirrored leaves are then deleted, leaving one copy of the rows:

    <prefix>/objectdir/hats/            the collection's metadata files, verbatim
                                        (hats.properties, partition_info.csv,
                                        schema.txt, md5sums.txt); its leaves live
                                        here only between mirror and repartition
    <prefix>/objectdir_hp6/hp6=<p>/part.parquet
                                        the rows re-cut into order-6 NESTED HEALPix
                                        pixels (~0.84 deg^2; a chip touches at most
                                        four): what the alert reader opens
    <prefix>/manifest.json              release, dates, md5s, hp6 inventory

A new NED release overwrites in place and the manifest says which release
is there. Every step is idempotent, so a rerun after an interruption, or for
a new release, only does the missing work.

Usage, end to end for a new release (``python -m rapidpipe.science.catalogs.store`` is this
module's command line):

    # on a machine that can reach the collection URL (IPAC network) AND S3;
    # streams HTTP -> S3, nothing is written locally
    python -m rapidpipe.science.catalogs.store mirror --base-url <collection-url> \\
                                    --dest s3://<the inputs bucket>/catalogs/ned
    python -m rapidpipe.science.catalogs.store verify --dest s3://<the inputs bucket>/catalogs/ned

    # on EC2, next to the bucket; reads each leaf into memory (<= ~1 GB) and
    # deletes it from S3 once its order-6 files are written
    python -m rapidpipe.science.catalogs.store repartition --dest s3://<the inputs bucket>/catalogs/ned \\
                                         --jobs 4 --delete-leaves
    python -m rapidpipe.science.catalogs.store manifest    --dest s3://<the inputs bucket>/catalogs/ned

    # NED-LVS (the Local Volume Sample, one FITS table; see that section):
    # mirrored the same way, then converted to parquet streaming from the
    # store -- no local disk, ~0.5 GB of memory, runs from any machine
    python -m rapidpipe.science.catalogs.store ingest-lvs  --dest s3://<the inputs bucket>/catalogs/ned
    python -m rapidpipe.science.catalogs.store manifest    --dest s3://<the inputs bucket>/catalogs/ned

    # Gaia DR3 (LINCC's HATS copy; see that section) goes to a prefix of its
    # own: each of its 2,016 partitions is read over HTTP one row group at a
    # time, cut down to GAIA_COLUMNS and written to the store -- no local
    # disk, runs from any machine, resumable
    python -m rapidpipe.science.catalogs.store ingest-gaia --dest s3://<the inputs bucket>/catalogs/gaia/DR3 --jobs 8

``--dest`` also accepts a local directory, which is how the tests and dry
runs work (``repartition --leaves`` and ``ingest-gaia --leaves`` limit a run
to named leaves).

Row keys: ``_healpix_29`` is the NESTED order-29 pixel index (verified against
healpy on release 36.1_20260527_v2), so the order-6 pixel is
``_healpix_29 >> 46`` and every order-6 pixel lies inside exactly one leaf:
leaves repartition independently, no merge step.

Departures from `dev` (rapid_docs ``decision-gaia-mirror``, 2026-10-29):

- No bucket name is written here; a destination is always the caller's.
- :meth:`Store._bucket_region` trusts ``AWS_DEFAULT_REGION`` with a warning
  when ``GetBucketLocation`` is denied, which a job role may be; `dev`
  refused. An unset or contradicted region still refuses.
- :func:`ingest_gaia` recovers a leaf that is present without its done
  marker (an interrupted run on a bucket that refuses overwrites, where the
  leaf cannot be written again) instead of rewriting it.
- :func:`gaia_from_table` writes the Gaia layout from one Parquet table of
  rows on Gaia's schema: a synthetic catalog, or a box extract for a local
  walkthrough.
- The command line's parser is :class:`rapidpipe.exitcodes.ArgumentParser`.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import healpy as hp
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.fs
import pyarrow.parquet as pq
import requests
from astropy.io import fits

from rapidpipe.exitcodes import ArgumentParser

logger = logging.getLogger(__name__)

TOOL_VERSION = "1.0"
TOOL = f"rapidpipe.science.catalogs.store {TOOL_VERSION}"

# The collection's own metadata files, mirrored first and kept verbatim.
COLLECTION_FILES = ("hats.properties", "partition_info.csv", "schema.txt",
                    "md5sums.txt")

# Layout under the prefix.
HATS_SUBDIR = "objectdir/hats"
HP6_SUBDIR = "objectdir_hp6"
DONE_SUBDIR = "_repartition_done"       # one marker per finished leaf, under HP6_SUBDIR
MANIFEST_NAME = "manifest.json"

# Repartition target: HEALPix order 6 (NESTED), 49,152 pixels of ~0.84 deg^2.
HP6_ORDER = 6
HP29_ORDER = 29
HP6_SHIFT = 2 * (HP29_ORDER - HP6_ORDER)      # 46 bits: order 29 -> order 6

STREAM_CHUNK = 8 << 20                         # bytes per HTTP read / hash update


def hp6_file(pixel: int) -> str:
    """Path, relative to the prefix, of the order-6 partition file for `pixel`."""
    return f"{HP6_SUBDIR}/hp6={int(pixel)}/part.parquet"


# ---------------------------------------------------------------------------
# md5sums.txt / partition_info.csv / hats.properties
# ---------------------------------------------------------------------------

def parse_md5sums(text: str) -> dict[str, str]:
    """``md5sums.txt`` -> {relative path: md5}; lines are ``<md5>  <path>``."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line:
            md5, _, path = line.partition(" ")
            out[path.strip().lstrip("*")] = md5.strip()
    return out


def parse_partition_info(text: str) -> list[tuple[int, int]]:
    """``partition_info.csv`` -> [(Norder, Npix), ...], by column NAME.

    The published file is ``Norder,Dir,Npix,num_rows`` (release 36.1);
    older/hand-made ones are ``Norder,Npix``. Positional parsing of the
    former read ``Dir`` (0 for every leaf) as the pixel and sent all 264
    leaves to pixel 0 (live, 2026-09-22). Raises ValueError on a missing
    column or a duplicated leaf.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [name.strip() for name in lines[0].split(",")]
    try:
        i_order, i_npix = header.index("Norder"), header.index("Npix")
    except ValueError:
        raise ValueError(f"partition_info.csv header lacks Norder/Npix: {header}")
    rows = []
    for line in lines[1:]:
        fields = line.split(",")
        rows.append((int(fields[i_order]), int(fields[i_npix])))
    if len(set(rows)) != len(rows):
        raise ValueError("partition_info.csv lists the same leaf more than once")
    return rows


def leaf_path(order: int, npix: int) -> str:
    """Relative path of a HATS leaf inside the collection."""
    return (f"dataset/Norder={order}/Dir={10_000 * (npix // 10_000)}/"
            f"Npix={npix}/part0.snappy.parquet")


def read_properties(text: str) -> dict[str, str]:
    """``hats.properties`` (java-properties style) -> dict."""
    props = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


# ---------------------------------------------------------------------------
# A destination: s3://bucket/prefix or a local directory
# ---------------------------------------------------------------------------

class Store:
    """Object-store-shaped access to ``s3://bucket/prefix`` or a local dir.

    Streams go in through :meth:`put_stream` (multipart to S3, no local
    file); the md5 computed while streaming is kept as object metadata so
    :meth:`stored_md5` can verify without re-reading. Parquet goes through
    pyarrow's filesystem layer.

    Parameters
    ----------
    root : str
        ``s3://bucket/prefix`` or a local directory path.
    """

    MD5_META = "md5"

    def __init__(self, root: str) -> None:
        self.root = root.rstrip("/")
        self.is_s3 = self.root.startswith("s3://")
        if self.is_s3:
            self.bucket, _, self.prefix = self.root[len("s3://"):].partition("/")
            self.prefix = self.prefix.strip("/")
            self._client: Any = None
            # The region must be stated, and must be the bucket's. pyarrow's
            # S3 layer does not follow S3's region redirect (an unset region
            # means us-east-1 and every request fails with HTTP 301), and a
            # session pointed at the wrong region is the setup under which
            # a 70 GB repartition would run as cross-region traffic. So no
            # default and no silent lookup: refuse, and say what to export.
            region = os.environ.get("AWS_DEFAULT_REGION")
            actual = self._bucket_region()
            if not region:
                raise RuntimeError(
                    f"AWS_DEFAULT_REGION is not set; bucket {self.bucket} is in "
                    f"{actual}. Export AWS_DEFAULT_REGION={actual} and rerun.")
            if actual is not None and region != actual:
                raise RuntimeError(
                    f"AWS_DEFAULT_REGION={region} but bucket {self.bucket} is in "
                    f"{actual}; refusing to run across regions. Export "
                    f"AWS_DEFAULT_REGION={actual} and run from compute in that region.")
            self.fs: pyarrow.fs.FileSystem = pyarrow.fs.S3FileSystem(region=region)
        else:
            self.bucket, self.prefix = "", str(Path(self.root).resolve())
            self.fs = pyarrow.fs.LocalFileSystem()

    # -- naming -------------------------------------------------------------
    def key(self, rel: str) -> str:
        """Object key (S3) or absolute path (local) of `rel`."""
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def fs_path(self, rel: str) -> str:
        """The pyarrow filesystem path of `rel`."""
        return f"{self.bucket}/{self.key(rel)}" if self.is_s3 else self.key(rel)

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            self._client = boto3.client("s3")
        return self._client

    def _bucket_region(self) -> str | None:
        """The bucket's region per S3 (GetBucketLocation reports None for
        us-east-1), or None when the caller may not ask (a job role without
        ``s3:GetBucketLocation``): then ``AWS_DEFAULT_REGION`` is trusted."""
        try:
            location = self.client.get_bucket_location(Bucket=self.bucket)
        except self.client.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "AccessDenied":
                raise
            logger.warning("GetBucketLocation denied on %s; trusting AWS_DEFAULT_REGION=%s",
                           self.bucket, os.environ.get("AWS_DEFAULT_REGION"))
            return None
        return location.get("LocationConstraint") or "us-east-1"

    # -- small objects ------------------------------------------------------
    def exists(self, rel: str) -> bool:
        return self.fs.get_file_info(self.fs_path(rel)).type != pyarrow.fs.FileType.NotFound

    def size(self, rel: str) -> int | None:
        info = self.fs.get_file_info(self.fs_path(rel))
        return None if info.type == pyarrow.fs.FileType.NotFound else info.size

    def read_text(self, rel: str) -> str:
        with self.fs.open_input_stream(self.fs_path(rel)) as f:
            return f.read().decode()

    def write_text(self, rel: str, text: str) -> None:
        self.put_bytes(rel, text.encode())

    def put_bytes(self, rel: str, data: bytes) -> str:
        md5 = hashlib.md5(data).hexdigest()
        if self.is_s3:
            self.client.put_object(Bucket=self.bucket, Key=self.key(rel), Body=data,
                                   Metadata={self.MD5_META: md5})
        else:
            path = Path(self.key(rel))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._write_local_md5(rel, md5)
        return md5

    # -- streamed objects (the leaves) --------------------------------------
    def put_stream(self, rel: str, chunks: Iterator[bytes]) -> str:
        """Store a byte stream under `rel`; returns the md5 of what was stored.

        On S3 this is a multipart upload fed directly from `chunks`; the
        md5 is recorded as object metadata. Locally the stream goes to a
        file next to a ``.md5`` sidecar.
        """
        h = hashlib.md5()

        def hashed() -> Iterator[bytes]:
            for chunk in chunks:
                h.update(chunk)
                yield chunk

        if self.is_s3:
            from boto3.s3.transfer import TransferConfig
            reader = _IterReader(hashed())
            # The md5 is only known at the end, so it is attached afterwards
            # with a metadata-only copy (cheap, server side).
            self.client.upload_fileobj(
                reader, self.bucket, self.key(rel),
                Config=TransferConfig(multipart_chunksize=64 << 20,
                                      multipart_threshold=64 << 20,
                                      max_concurrency=2))
            md5 = h.hexdigest()
            self.client.copy_object(
                Bucket=self.bucket, Key=self.key(rel),
                CopySource={"Bucket": self.bucket, "Key": self.key(rel)},
                Metadata={self.MD5_META: md5}, MetadataDirective="REPLACE")
            return md5
        path = Path(self.key(rel))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            for chunk in hashed():
                f.write(chunk)
        md5 = h.hexdigest()
        self._write_local_md5(rel, md5)
        return md5

    def stored_md5(self, rel: str) -> str | None:
        """The md5 recorded when `rel` was stored, or None if absent."""
        if self.is_s3:
            try:
                head = self.client.head_object(Bucket=self.bucket, Key=self.key(rel))
            except self.client.exceptions.ClientError:
                return None
            return head.get("Metadata", {}).get(self.MD5_META)
        sidecar = Path(self.key(rel) + ".md5")
        return sidecar.read_text().strip() if sidecar.exists() else None

    def delete(self, rel: str) -> None:
        if self.is_s3:
            self.client.delete_object(Bucket=self.bucket, Key=self.key(rel))
        else:
            Path(self.key(rel)).unlink(missing_ok=True)
            Path(self.key(rel) + ".md5").unlink(missing_ok=True)

    def _write_local_md5(self, rel: str, md5: str) -> None:
        Path(self.key(rel) + ".md5").write_text(md5 + "\n")

    # -- parquet ------------------------------------------------------------
    def read_table(self, rel: str, columns: list[str] | None = None) -> pa.Table:
        return pq.read_table(self.fs_path(rel), filesystem=self.fs, columns=columns)

    def write_table(self, table: pa.Table, rel: str) -> None:
        if not self.is_s3:
            Path(self.key(rel)).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, self.fs_path(rel), filesystem=self.fs,
                       compression="snappy")

    def list_files(self, rel_dir: str) -> list[str]:
        """Relative paths of the files under `rel_dir` (recursive)."""
        selector = pyarrow.fs.FileSelector(self.fs_path(rel_dir), recursive=True,
                                           allow_not_found=True)
        base = self.fs_path("").rstrip("/") + "/"
        return [info.path[len(base):] for info in self.fs.get_file_info(selector)
                if info.is_file]

    def __repr__(self) -> str:
        return f"Store({self.root!r})"


class _IterReader:
    """Minimal file-like object over an iterator of bytes, for upload_fileobj."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = self._buf + b"".join(self._chunks)
            self._buf = b""
            return data
        while len(self._buf) < n:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        data, self._buf = self._buf[:n], self._buf[n:]
        return data


# ---------------------------------------------------------------------------
# mirror / verify
# ---------------------------------------------------------------------------

def http_chunks(url: str, session: Any, timeout_s: float = 300.0) -> Iterator[bytes]:
    """Yield the body of `url` in STREAM_CHUNK pieces."""
    with session.get(url, stream=True, timeout=timeout_s) as r:
        r.raise_for_status()
        yield from r.iter_content(chunk_size=STREAM_CHUNK)


def mirror_file(url: str, store: Store, dest_rel: str,
                expected_md5: str | None = None, session: Any = None,
                retries: int = 5) -> str:
    """Stream `url` into the store under `dest_rel`, without local staging.

    Returns ``"skipped"`` when the store already holds it with the expected
    md5, else ``"mirrored"``. A stored object whose md5 disagrees with the
    manifest is deleted and re-streamed; after `retries` failures a
    RuntimeError names the file. Used for every file of the object-
    directory collection (mirror()) and for the NED-LVS FITS (ingest_lvs()).
    """
    session = session or requests.Session()
    if expected_md5 and store.stored_md5(dest_rel) == expected_md5:
        return "skipped"
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            got = store.put_stream(dest_rel, http_chunks(url, session))
            if expected_md5 and got != expected_md5:
                store.delete(dest_rel)
                raise RuntimeError(f"md5 mismatch: got {got}, expected {expected_md5}")
            return "mirrored"
        except Exception as exc:
            last = exc
            logger.warning("%s: attempt %d/%d failed (%s)", dest_rel, attempt,
                           retries, exc)
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"giving up on {dest_rel}: {last}")


class _ThreadSessions:
    """One ``requests.Session`` per worker thread, all closed at the end
    (a session per file leaked sockets: ResourceWarning in the tests).
    Shared by mirror() and ingest_gaia()."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._all: list[Any] = []
        self._lock = threading.Lock()

    def get(self) -> Any:
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            with self._lock:
                self._all.append(self._local.session)
        return self._local.session

    def close_all(self) -> None:
        for s in self._all:
            s.close()


def mirror(base_url: str, dest: str, jobs: int = 4) -> dict[str, int]:
    """Mirror the whole collection at `base_url` into ``<dest>/objectdir/hats``.

    Nothing is written locally. Idempotent: files already present with the
    manifest's md5 are skipped, so a rerun finishes an interrupted mirror
    and a new release replaces only what changed.
    """
    base_url = base_url.rstrip("/") + "/"
    store = Store(dest)
    sessions = _ThreadSessions()

    def one(rel: str, md5: str) -> str:
        return mirror_file(base_url + rel, store, f"{HATS_SUBDIR}/{rel}", md5,
                           session=sessions.get())

    try:
        for name in COLLECTION_FILES:              # small; always refreshed
            mirror_file(base_url + name, store, f"{HATS_SUBDIR}/{name}",
                        session=sessions.get())
        md5s = parse_md5sums(store.read_text(f"{HATS_SUBDIR}/md5sums.txt"))
        counts = {"skipped": 0, "mirrored": 0, "failed": 0}
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(one, rel, md5): rel for rel, md5 in md5s.items()}
            for i, fut in enumerate(as_completed(futures), start=1):
                rel = futures[fut]
                try:
                    counts[fut.result()] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    logger.error("%s: %s", rel, exc)
                if i % 10 == 0 or i == len(futures):
                    logger.info("mirror: %d/%d files %s", i, len(futures), counts)
    finally:
        sessions.close_all()
    return counts


def consumed_leaves(store: Store) -> set[str]:
    """Leaves whose done marker says the mirrored copy was deleted after
    repartitioning (``repartition --delete-leaves``)."""
    out = set()
    for rel in store.list_files(f"{HP6_SUBDIR}/{DONE_SUBDIR}"):
        if rel.endswith(".json"):
            summary = json.loads(store.read_text(rel))
            if summary.get("source_deleted"):
                out.add(summary["leaf"])
    return out


def verify(dest: str) -> list[str]:
    """Files in ``md5sums.txt`` that are missing from the store or whose
    stored md5 differs from the manifest's. Leaves deliberately deleted
    after repartitioning are not reported."""
    store = Store(dest)
    md5s = parse_md5sums(store.read_text(f"{HATS_SUBDIR}/md5sums.txt"))
    consumed = consumed_leaves(store)
    bad = [rel for rel, md5 in md5s.items()
           if rel not in consumed and store.stored_md5(f"{HATS_SUBDIR}/{rel}") != md5]
    logger.info("verify: %d files listed, %d consumed by repartition, "
                "%d missing or mismatched", len(md5s), len(consumed), len(bad))
    return bad


# ---------------------------------------------------------------------------
# repartition
# ---------------------------------------------------------------------------

def normalise_ptype(table: pa.Table) -> pa.Table:
    """Empty-string ``ptype`` -> null, so "unclassified" is one value."""
    if "ptype" not in table.column_names:
        return table
    col = table["ptype"]
    empty = pc.fill_null(pc.equal(col, ""), False)
    fixed = pc.if_else(empty, pa.scalar(None, col.type), col)
    return table.set_column(table.schema.get_field_index("ptype"), "ptype", fixed)


def split_by_hp6(table: pa.Table) -> Iterator[tuple[int, pa.Table]]:
    """Yield ``(order-6 pixel, its rows sorted by _healpix_29)`` for one leaf."""
    table = table.sort_by("_healpix_29")
    hp6 = pc.shift_right(table["_healpix_29"], HP6_SHIFT)
    table = table.append_column("_hp6", hp6)
    for pixel in pc.unique(hp6).to_pylist():
        rows = table.filter(pc.equal(table["_hp6"], pixel))
        yield int(pixel), rows.drop_columns(["_hp6"])


def done_marker(leaf_rel: str) -> str:
    return f"{HP6_SUBDIR}/{DONE_SUBDIR}/{leaf_rel.replace('/', '__')}.json"


def repartition_leaf(dest: str, leaf_rel: str,
                     delete_source: bool = False) -> dict[str, Any]:
    """Re-cut one mirrored leaf into order-6 files under ``<dest>/objectdir_hp6``.

    Reads the leaf into memory (largest in release 36.1: 7.8 M rows,
    ~0.7 GB), groups by order-6 pixel and writes one file per pixel. A
    done marker per leaf makes reruns skip finished leaves.

    With `delete_source` the mirrored leaf is removed once its order-6
    files and marker exist, so only one copy of the rows is kept (the
    marker records ``source_deleted``). The collection's small metadata
    files stay, and the leaf can always be mirrored again from the source.
    A leaf whose marker exists but which is still present, a run that
    stopped between marker and delete, is deleted on the next run.
    """
    store = Store(dest)
    marker = done_marker(leaf_rel)
    source_rel = f"{HATS_SUBDIR}/{leaf_rel}"
    if store.exists(marker):
        summary = json.loads(store.read_text(marker))
        if delete_source and store.exists(source_rel):
            store.delete(source_rel)
            summary["source_deleted"] = True
            store.write_text(marker, json.dumps(summary))
        return {**summary, "status": "skipped"}
    t0 = time.time()
    table = normalise_ptype(store.read_table(source_rel))
    pixels = []
    for pixel, rows in split_by_hp6(table):
        store.write_table(rows, hp6_file(pixel))
        pixels.append({"hp6": pixel, "rows": rows.num_rows})
    summary = {"leaf": leaf_rel, "rows": table.num_rows, "n_hp6": len(pixels),
               "pixels": pixels, "seconds": round(time.time() - t0, 1),
               "finished_at": datetime.now(timezone.utc).isoformat(),
               "source_deleted": False}
    store.write_text(marker, json.dumps(summary))
    if delete_source:
        store.delete(source_rel)
        summary["source_deleted"] = True
        store.write_text(marker, json.dumps(summary))
    return {**summary, "status": "done"}


def repartition(dest: str, jobs: int = 2, leaves: list[str] | None = None,
                delete_leaves: bool = False) -> dict[str, int]:
    """Repartition every leaf of the mirrored collection under `dest`.

    Parameters
    ----------
    dest : str
        The prefix holding ``objectdir/hats`` (s3://... or a local dir).
    jobs : int
        Worker processes; each holds one leaf in memory.
    leaves : list of str, optional
        Leaf paths relative to the collection root (dry runs, tests);
        default is every leaf in ``partition_info.csv``.
    delete_leaves : bool
        Remove each mirrored leaf after it has been re-cut, keeping a
        single copy of the catalog (see repartition_leaf).
    """
    store = Store(dest)
    if leaves is None:
        leaves = [leaf_path(o, p) for o, p in
                  parse_partition_info(store.read_text(f"{HATS_SUBDIR}/partition_info.csv"))]
    counts = {"done": 0, "skipped": 0, "failed": 0, "rows": 0}
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(repartition_leaf, dest, leaf, delete_leaves): leaf
                   for leaf in leaves}
        for i, fut in enumerate(as_completed(futures), start=1):
            leaf = futures[fut]
            try:
                result = fut.result()
                counts[result["status"]] += 1
                counts["rows"] += result["rows"]
                logger.info("repartition %d/%d: %s %s (%s rows, %d hp6 files, %.0f s)",
                            i, len(leaves), result["status"], leaf,
                            f"{result['rows']:,}", result["n_hp6"],
                            result.get("seconds", 0))
            except Exception as exc:
                counts["failed"] += 1
                logger.error("repartition %d/%d: FAILED %s: %s", i, len(leaves), leaf, exc)
    return counts


# ---------------------------------------------------------------------------
# NED-LVS (Local Volume Sample)
#
# A second, much smaller NED product: the ~2.1 M galaxies within 1000 Mpc,
# carrying the distances, angular diameters, photometry, SFR and stellar
# mass that the object directory lacks (Cook et al. 2023, DOI 10.26132/NED8).
# Every LVS object is a NED object (`objname` is the object directory's
# `prefname`), so this is less a second catalog to match than a second
# table to join to a NED match. NED serves it as one ~1.2 GB FITS table at
# a fixed "current" URL (LVS_URL): Content-Disposition names the dated file
# (NEDLVS_20260424.fits), the primary header carries VERSION/EXTRACT/DOI,
# HDU 1 is a BINTABLE of 84 columns.
#
# Same design as the object directory, minus the repartition: the file is
# mirrored into the store exactly as a collection leaf is (mirror_file:
# streamed, md5 recorded, no local staging), then converted from the store
# to one parquet file, also streaming: a FITS binary table is a fixed-width
# structured array, so it is read back in ~64 MB pieces, each decoded with
# numpy straight from the bytes and written as one parquet row group.
# Memory is one piece plus the writer's buffers, local disk is not used,
# and the whole thing runs from any machine with credentials for the
# bucket: a new NED-LVS release is a laptop job.
#
#     <prefix>/lvs/source/nedlvs.fits     the served file, verbatim, kept so a
#                                         rerun can skip the download
#     <prefix>/lvs/source/nedlvs.json     served filename, size, Last-Modified,
#                                         md5: the idempotency key
#     <prefix>/lvs/nedlvs.parquet         every column, native types, in file
#                                         order, plus an order-6 pixel column
#                                         `_hp6` (the object-directory scheme)
#     <prefix>/lvs/nedlvs.json            version, url, rows, columns, md5:
#                                         merged into manifest.json as "lvs"
#
# All 84 columns are kept on purpose: the table is ~2 M rows, so pruning
# saves nothing worth a re-ingest, and which columns reach an alert is the
# schema's decision (param_registry.py), not this file's. The alert reader
# loads the whole parquet once per process; a chip cone touches a handful
# of its rows.
# ---------------------------------------------------------------------------

LVS_URL = "https://ned.ipac.caltech.edu/NED::LVS/fits/Current/"
LVS_SUBDIR = "lvs"
LVS_SOURCE_FILE = f"{LVS_SUBDIR}/source/nedlvs.fits"
LVS_SOURCE_INFO = f"{LVS_SUBDIR}/source/nedlvs.json"
LVS_PARQUET = f"{LVS_SUBDIR}/nedlvs.parquet"
LVS_INFO = f"{LVS_SUBDIR}/nedlvs.json"
LVS_HP6_COLUMN = "_hp6"
LVS_HEADER_KEYS = ("VERSION", "EXTRACT", "DOI", "REFERENC", "OBJECT")

# Bytes of FITS rows decoded per step (~110k of NED-LVS's 568-byte rows).
LVS_CHUNK_BYTES = 64 << 20

# FITS binary-table column codes -> big-endian numpy dtypes. NED-LVS uses
# D, E, L and nA; the integer codes cost nothing to accept. Anything else
# (vector columns, X, C, M, P/Q) stops the ingest with a clear error rather
# than guessing.
FITS_TFORM_DTYPES = {"D": ">f8", "E": ">f4", "I": ">i2", "J": ">i4",
                     "K": ">i8", "B": "u1", "L": "S1"}
FITS_BLOCK = 2880
FITS_CARD = 80


def lvs_served_file(url: str, session: Any = None) -> dict[str, Any]:
    """What the server would send for `url`, without downloading it.

    A one-byte Range request; NED answers it with the dated filename in
    Content-Disposition, the total size in Content-Range, and Last-Modified.
    """
    session = session or requests.Session()
    with session.get(url, headers={"Range": "bytes=0-0"}, stream=True,
                     timeout=60) as r:
        r.raise_for_status()
        match = re.search(r'filename="?([^";]+)"?',
                          r.headers.get("Content-Disposition", ""))
        total = (r.headers.get("Content-Range", "").rpartition("/")[2]
                 or r.headers.get("Content-Length", ""))
        return {"url": url,
                "filename": match.group(1).strip() if match else None,
                "size": int(total) if total.isdigit() else None,
                "last_modified": r.headers.get("Last-Modified")}


def read_fits_header(f: Any) -> Any:
    """Read one FITS header from the current position of a binary stream,
    leaving it at the start of that HDU's data. Blocks of 2880 bytes up
    to the one holding the END card, parsed by astropy."""
    blocks = []
    end = b"END".ljust(FITS_CARD)
    while True:
        block = f.read(FITS_BLOCK)
        if len(block) < FITS_BLOCK:
            raise ValueError("truncated FITS header")
        blocks.append(block)
        if any(block[i:i + FITS_CARD] == end for i in range(0, FITS_BLOCK, FITS_CARD)):
            break
    return fits.Header.fromstring(b"".join(blocks).decode("latin-1"))


def fits_row_dtype(header: Any) -> tuple[np.dtype, set[str]]:
    """The structured dtype of one BINTABLE row, from TTYPEn/TFORMn, plus
    the names of the logical (L) columns, which arrive as b'T'/b'F'.
    Cross-checked against NAXIS1 so a misread format cannot silently
    shear every row."""
    fields, logical = [], set()
    for i in range(1, header["TFIELDS"] + 1):
        name = header[f"TTYPE{i}"].strip()
        tform = header[f"TFORM{i}"].strip()
        match = re.fullmatch(r"(\d*)([A-Z])", tform)
        count = int(match.group(1) or 1) if match else 1
        code = match.group(2) if match else ""
        if code == "A":
            fields.append((name, f"S{count}"))
        elif code in FITS_TFORM_DTYPES and count == 1:
            fields.append((name, FITS_TFORM_DTYPES[code]))
        else:
            raise ValueError(f"column {name!r}: unsupported TFORM {tform!r}")
        if code == "L":
            logical.add(name)
    dtype = np.dtype(fields)
    if dtype.itemsize != header["NAXIS1"]:
        raise ValueError(f"row dtype is {dtype.itemsize} bytes but NAXIS1 is "
                         f"{header['NAXIS1']}")
    return dtype, logical


def _fits_column_to_arrow(values: np.ndarray, logical: bool = False) -> pa.Array:
    """One FITS binary-table column as an Arrow array in native types.

    Strings are stripped and the empty string becomes null (the table uses
    "" for "not available"); floats keep NaN as their missing value, which
    is what the alert readers expect; logicals become bool; big-endian
    numerics are byte-swapped.
    """
    arr = np.asarray(values)
    if logical:
        return pa.array(arr == b"T")
    if arr.dtype.kind == "S":
        text = [v.decode("ascii", "replace").strip() for v in arr]
        return pa.array([t or None for t in text], type=pa.string())
    if arr.dtype.byteorder not in ("=", "|"):
        arr = arr.astype(arr.dtype.newbyteorder("="))
    return pa.array(arr)


def lvs_fits_to_parquet(store: Store, source_rel: str,
                        parquet_rel: str) -> dict[str, Any]:
    """Convert the stored NED-LVS FITS to parquet, streaming both ways.

    Reads `source_rel` from the store in LVS_CHUNK_BYTES pieces (whole
    numbers of rows, decoded with numpy straight from the bytes), converts
    each piece to Arrow (see _fits_column_to_arrow) with an added `_hp6`
    column, and writes it as one row group of `parquet_rel`. Rows keep the
    file's order. Returns the row count, the column names and the primary-
    header facts (LVS_HEADER_KEYS, lower-cased; VERSION is the release).
    """
    with store.fs.open_input_file(store.fs_path(source_rel)) as f:
        primary = read_fits_header(f)
        if primary.get("NAXIS", 0):
            raise ValueError("expected an empty primary HDU before the table")
        facts = {key.lower(): str(primary[key]).strip()
                 for key in LVS_HEADER_KEYS if key in primary}
        header = read_fits_header(f)
        if header.get("XTENSION", "").strip() != "BINTABLE":
            raise ValueError(f"expected a BINTABLE, got {header.get('XTENSION')!r}")
        dtype, logical = fits_row_dtype(header)
        n_rows, row_bytes = int(header["NAXIS2"]), int(header["NAXIS1"])
        rows_per_chunk = max(1, LVS_CHUNK_BYTES // row_bytes)
        if not store.is_s3:
            Path(store.key(parquet_rel)).parent.mkdir(parents=True, exist_ok=True)
        writer = None
        done = 0
        try:
            while done < n_rows:
                n = min(rows_per_chunk, n_rows - done)
                buf = f.read(n * row_bytes)
                if len(buf) != n * row_bytes:
                    raise ValueError(f"truncated FITS data after {done + len(buf) // row_bytes} rows")
                rec = np.frombuffer(buf, dtype=dtype)
                columns = {name: _fits_column_to_arrow(rec[name], name in logical)
                           for name in dtype.names}
                hp6 = hp.ang2pix(2 ** HP6_ORDER, rec["ra"].astype(float),
                                 rec["dec"].astype(float), nest=True, lonlat=True)
                columns[LVS_HP6_COLUMN] = pa.array(hp6.astype("int32"))
                chunk = pa.table(columns)
                if writer is None:
                    writer = pq.ParquetWriter(store.fs_path(parquet_rel), chunk.schema,
                                              filesystem=store.fs, compression="snappy")
                writer.write_table(chunk)
                done += n
                logger.info("lvs: converted %s / %s rows", f"{done:,}", f"{n_rows:,}")
        finally:
            if writer is not None:
                writer.close()
    return {"rows": n_rows, "columns": list(dtype.names) + [LVS_HP6_COLUMN],
            "fits_header": facts}


def ingest_lvs(dest: str, url: str = LVS_URL, force: bool = False) -> dict[str, Any]:
    """Mirror the current NED-LVS FITS into the store and convert it to parquet.

    Idempotent on the served file: when the store already holds the source
    the server reports now (same served filename and size) together with
    its parquet, nothing is transferred. An unchanged source whose parquet
    is missing (an interrupted run) is converted again without a new
    download. `force` redoes both. Returns the ingest record (what
    ``<prefix>/lvs/nedlvs.json`` holds) plus a ``status`` of "done" or
    "skipped".
    """
    store = Store(dest)
    session = requests.Session()
    try:
        served = lvs_served_file(url, session)
        previous = (json.loads(store.read_text(LVS_SOURCE_INFO))
                    if store.exists(LVS_SOURCE_INFO) else {})
        unchanged = (store.exists(LVS_SOURCE_FILE)
                     and previous.get("filename") == served["filename"]
                     and previous.get("size") == served["size"])
        if unchanged and not force:
            logger.info("lvs: %s (%s bytes) already mirrored",
                        served["filename"], served["size"])
            source = previous
            if store.exists(LVS_PARQUET) and store.exists(LVS_INFO):
                logger.info("lvs: its parquet is present too; nothing to do")
                return {**json.loads(store.read_text(LVS_INFO)), "status": "skipped"}
        else:
            logger.info("lvs: mirroring %s (%s bytes) from %s",
                        served["filename"], served["size"], url)
            mirror_file(url, store, LVS_SOURCE_FILE, session=session)
            size = store.size(LVS_SOURCE_FILE)
            if served["size"] is not None and size != served["size"]:
                store.delete(LVS_SOURCE_FILE)
                raise RuntimeError(f"lvs: stored {size} bytes but the server "
                                   f"announced {served['size']}; deleted, rerun")
            source = {**served, "md5": store.stored_md5(LVS_SOURCE_FILE),
                      "file": LVS_SOURCE_FILE,
                      "stored_at": datetime.now(timezone.utc).isoformat()}
            store.write_text(LVS_SOURCE_INFO, json.dumps(source, indent=1))
    finally:
        session.close()

    t0 = time.time()
    converted = lvs_fits_to_parquet(store, LVS_SOURCE_FILE, LVS_PARQUET)
    info = {
        "release": converted["fits_header"].get("version"),
        "fits_header": converted["fits_header"],
        "source": source,
        "parquet": LVS_PARQUET,
        "rows": converted["rows"],
        "columns": converted["columns"],
        "hp6_column": LVS_HP6_COLUMN,
        "seconds": round(time.time() - t0, 1),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tool": TOOL,
        "git_sha": git_sha(),
    }
    store.write_text(LVS_INFO, json.dumps(info, indent=1))
    logger.info("lvs: release %s, %s rows, %d columns -> %s (%.0f s)",
                info["release"], f"{info['rows']:,}", len(info["columns"]),
                LVS_PARQUET, info["seconds"])
    return {**info, "status": "done"}


# ---------------------------------------------------------------------------
# Gaia DR3
#
# LINCC Frameworks publishes Gaia DR3 gaia_source as a HATS catalog
# (https://data.lsdb.io/hats/gaia_dr3/gaia/): 1.81 billion rows, 153
# columns, ~750 GB, in 2,016 partitions of at most 2 million rows at
# HEALPix orders 2-6 (denser sky, finer pixels). A partition is one parquet
# file of ~6 row groups, so it can be read over HTTP a row group at a time
# with Range requests: pyarrow does that through a seekable file object
# (_HttpRangeFile) and, with ``pre_buffer``, fetches the wanted column
# chunks of a row group in a few requests.
#
# Only GAIA_COLUMNS are kept (~104 bytes/row zstd, measured on the densest
# bulge partition: ~190 GB for the whole sky instead of ~750 GB). That is a
# STORAGE decision made here; which of them reach an alert is the schema's
# (param_registry.py). Adding a column means re-running the ingest with
# --force. The catalog gets a prefix of its own (not NED's), keeping the
# partition layout as LINCC publishes it with the two metadata files copied
# verbatim, so the alert reader locates a cone's partitions the way any
# HATS reader does:
#
#     <prefix>/hats.properties, partition_info.csv      verbatim
#     <prefix>/dataset/Norder=<o>/Dir=<d>/Npix=<p>.parquet
#                                                      GAIA_COLUMNS, zstd
#     <prefix>/_ingest_done/N<o>_P<p>.json             one per finished leaf
#     <prefix>/gaia.json                               the ingest record
#
# Idempotent and resumable: a leaf with a done marker is skipped, an
# interrupted leaf has no marker and is redone; --force redoes everything.
# On a bucket that refuses overwrites (the inputs bucket), a leaf that is
# present without its marker is recovered, never rewritten: a multipart
# upload that did not complete leaves no object, so a present leaf is whole.
#
#     python -m rapidpipe.science.catalogs.store ingest-gaia --dest s3://<the inputs bucket>/catalogs/gaia/DR3 --jobs 8
# ---------------------------------------------------------------------------

GAIA_URL = "https://data.lsdb.io/hats/gaia_dr3/gaia/"
GAIA_DONE_SUBDIR = "_ingest_done"
GAIA_INFO = "gaia.json"
GAIA_METADATA_FILES = ("hats.properties", "partition_info.csv")

# The columns kept: identity, astrometry (to propagate the 2016.0 positions
# to the Roman epoch), astrometric quality, mean photometry, and the
# star/galaxy/quasar/variable classification. `_healpix_29` is the row key
# every HATS reader prefilters on. `ref_epoch` is new on the rebuild
# (2026-10-29): the epoch travels with the rows, so a leaf is complete alone.
GAIA_COLUMNS = (
    "_healpix_29", "source_id", "ra", "dec", "ra_error", "dec_error",
    "ref_epoch",
    "pmra", "pmdec", "pmra_error", "pmdec_error", "parallax", "parallax_error",
    "ruwe", "astrometric_excess_noise", "ipd_frac_multi_peak", "duplicated_source",
    "phot_g_mean_mag", "phot_bp_mean_mag", "phot_rp_mean_mag", "bp_rp",
    "phot_g_mean_flux_over_error",
    "phot_variable_flag", "non_single_star", "in_qso_candidates",
    "in_galaxy_candidates", "classprob_dsc_combmod_star",
    "classprob_dsc_combmod_galaxy", "classprob_dsc_combmod_quasar",
)
GAIA_COMPRESSION = "zstd"

#: Columns a table given to :func:`gaia_from_table` must hold; the rest of
#: GAIA_COLUMNS are filled with nulls when absent.
GAIA_TABLE_REQUIRED = ("source_id", "ra", "dec")

#: The partition order :func:`gaia_from_table` writes (768 leaves over the sky).
GAIA_TABLE_ORDER = 3


class _HttpRangeFile:
    """A read-only, seekable file over an HTTP URL, for ``pa.PythonFile``.

    Every ``read`` is one Range request; pyarrow's ``pre_buffer`` keeps the
    count per row group to a handful. The size comes from a one-byte probe,
    which also checks that the server honours ranges.
    """

    def __init__(self, url: str, session: Any, timeout_s: float = 300.0) -> None:
        self.url, self.session, self.timeout_s = url, session, timeout_s
        self.pos = 0
        self.closed = False
        with session.get(url, headers={"Range": "bytes=0-0"}, timeout=timeout_s) as r:
            r.raise_for_status()
            if r.status_code != 206:
                raise RuntimeError(f"{url}: server does not honour Range requests")
            self.size = int(r.headers["Content-Range"].rpartition("/")[2])

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self.size - self.pos
        if n == 0 or self.pos >= self.size:
            return b""
        end = min(self.size, self.pos + n) - 1
        r = self.session.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"},
                             timeout=self.timeout_s)
        r.raise_for_status()
        data = r.content
        if len(data) != end - self.pos + 1:
            raise RuntimeError(f"{self.url}: short Range read "
                               f"({len(data)} of {end - self.pos + 1} bytes)")
        self.pos += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def tell(self) -> int:
        return self.pos

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def open_parquet_source(url: str, session: Any = None) -> Any:
    """A pyarrow random-access file for `url`: Range reads for http(s),
    a plain file otherwise (local collections: dry runs and tests)."""
    if url.startswith(("http://", "https://")):
        return pa.PythonFile(_HttpRangeFile(url, session or requests.Session()), mode="r")
    return pa.OSFile(url.removeprefix("file://"), "rb")


def copy_source_file(url: str, store: Store, dest_rel: str, session: Any = None) -> None:
    """Copy one small file at `url` (http(s), or a local path) into the store."""
    if url.startswith(("http://", "https://")):
        mirror_file(url, store, dest_rel, session=session)
    else:
        store.put_bytes(dest_rel, Path(url.removeprefix("file://")).read_bytes())


def gaia_leaf_rel(order: int, npix: int, suffix: str = ".parquet") -> str:
    """Relative path of a leaf inside LINCC's layout (``hats_npix_suffix``
    names the file; NED's collection uses a directory per pixel instead,
    see leaf_path())."""
    return f"dataset/Norder={order}/Dir={10_000 * (npix // 10_000)}/Npix={npix}{suffix}"


def gaia_done_marker(order: int, npix: int) -> str:
    return f"{GAIA_DONE_SUBDIR}/N{order}_P{npix}.json"


def project_leaf(source: Any, store: Store, dest_rel: str,
                 columns: tuple[str, ...] = GAIA_COLUMNS) -> dict[str, Any]:
    """Copy `columns` of the parquet `source` (a pyarrow random-access
    file) to `dest_rel` in the store, one row group at a time, so memory
    stays at one row group (a few hundred thousand rows). Returns the row
    count, row groups and bytes written."""
    reader = pq.ParquetFile(source, pre_buffer=True)
    missing = [c for c in columns if c not in reader.schema_arrow.names]
    if missing:
        raise ValueError(f"source lacks columns {missing}")
    if not store.is_s3:
        Path(store.key(dest_rel)).parent.mkdir(parents=True, exist_ok=True)
    writer = None
    rows = 0
    try:
        for i in range(reader.num_row_groups):
            chunk = reader.read_row_group(i, columns=list(columns))
            if writer is None:
                writer = pq.ParquetWriter(store.fs_path(dest_rel), chunk.schema,
                                          filesystem=store.fs,
                                          compression=GAIA_COMPRESSION)
            writer.write_table(chunk)
            rows += chunk.num_rows
    finally:
        if writer is not None:
            writer.close()
    return {"rows": rows, "row_groups": reader.num_row_groups,
            "bytes": store.size(dest_rel)}


def source_ref_epoch(url: str, leaf_rel: str, session: Any = None) -> float | None:
    """The reference epoch (Julian year) of the catalog's positions, from
    the `ref_epoch` column statistics of one source leaf's first row group,
    a footer-sized read. None, with a warning, when the column or its
    statistics are absent. Every DR3 row says 2016.0; a later release
    changes it, and the reader needs the value to propagate proper motions.
    """
    with open_parquet_source(url + leaf_rel, session) as source:
        pf = pq.ParquetFile(source)
        if "ref_epoch" not in pf.schema_arrow.names or pf.metadata.num_row_groups == 0:
            logger.warning("gaia: %s has no ref_epoch column; epoch not recorded", leaf_rel)
            return None
        col = pf.schema_arrow.get_field_index("ref_epoch")
        stats = pf.metadata.row_group(0).column(col).statistics
        if stats is None or not stats.has_min_max or stats.min != stats.max:
            logger.warning("gaia: %s has no single ref_epoch; epoch not recorded", leaf_rel)
            return None
        return float(stats.min)


def _leaf_summary(store: Store, rel: str) -> dict[str, Any]:
    """A present leaf's row count, row groups and bytes, from its footer."""
    pf = pq.ParquetFile(store.fs.open_input_file(store.fs_path(rel)))
    return {"rows": pf.metadata.num_rows, "row_groups": pf.metadata.num_row_groups,
            "bytes": store.size(rel)}


def _write_gaia_info(store: Store, props: dict[str, str], all_leaves: list[tuple[int, int]],
                     ref_epoch: float | None, source_url: str) -> dict[str, Any]:
    """Gather the done markers into the ingest record and write ``gaia.json``."""
    done = [json.loads(store.read_text(rel)) for rel in store.list_files(GAIA_DONE_SUBDIR)
            if rel.endswith(".json")]
    rows = sum(d["rows"] for d in done)
    info = {
        "release": props.get("obs_collection"),
        "hats_release_date": props.get("hats_release_date"),
        "source_url": source_url,
        "hats_nrows": int(props.get("hats_nrows", 0)),
        "ref_epoch": ref_epoch,
        "columns": list(GAIA_COLUMNS),
        "compression": GAIA_COMPRESSION,
        "leaves_done": len(done), "leaves_total": len(all_leaves),
        "complete": len(done) == len(all_leaves),
        "rows": rows,
        "rows_match_hats_nrows": (rows == int(props.get("hats_nrows", 0))
                                  if len(done) == len(all_leaves) else None),
        "bytes": sum(d["bytes"] for d in done),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tool": TOOL,
        "git_sha": git_sha(),
    }
    store.write_text(GAIA_INFO, json.dumps(info, indent=1))
    return info


def ingest_gaia(dest: str, url: str = GAIA_URL, jobs: int = 4,
                leaves: list[tuple[int, int]] | None = None,
                force: bool = False) -> dict[str, Any]:
    """Project LINCC's Gaia DR3 HATS catalog at `url` into `dest`.

    The two metadata files are copied first, then every leaf of
    partition_info.csv (or just `leaves`, for dry runs) is projected to
    GAIA_COLUMNS by `jobs` threads. A leaf whose done marker exists is
    skipped unless `force`; a leaf that is present without its marker is
    recovered (its marker written from its footer), never rewritten.
    Returns the ingest record, which is also written to ``<dest>/gaia.json``:
    it carries the column list and, once every leaf is done, the row total
    checked against the catalog's own ``hats_nrows``.
    """
    url = url.rstrip("/") + "/"
    store = Store(dest)
    sessions = _ThreadSessions()
    t0 = time.time()
    try:
        for name in GAIA_METADATA_FILES:
            if not store.exists(name) or not store.is_s3:
                copy_source_file(url + name, store, name, sessions.get())
        props = read_properties(store.read_text("hats.properties"))
        suffix = props.get("hats_npix_suffix", ".parquet")
        all_leaves = parse_partition_info(store.read_text("partition_info.csv"))
        todo = all_leaves if leaves is None else [lf for lf in all_leaves if lf in set(leaves)]
        if leaves is not None and len(todo) != len(leaves):
            raise ValueError(f"leaves not in partition_info.csv: "
                             f"{sorted(set(leaves) - set(todo))}")

        def one(order: int, npix: int) -> str:
            marker = gaia_done_marker(order, npix)
            if store.exists(marker) and not force:
                return "skipped"
            rel = gaia_leaf_rel(order, npix, suffix)
            if store.exists(rel) and not force:
                result, status = _leaf_summary(store, rel), "recovered"
            else:
                with open_parquet_source(url + rel, sessions.get()) as source:
                    result, status = project_leaf(source, store, rel), "done"
            store.write_text(marker, json.dumps({
                "order": order, "npix": npix, "file": rel, **result,
                "written_at": datetime.now(timezone.utc).isoformat()}, indent=1))
            return status

        counts = {"skipped": 0, "done": 0, "recovered": 0, "failed": 0}
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(one, o, p): (o, p) for o, p in todo}
            for i, fut in enumerate(as_completed(futures), start=1):
                try:
                    counts[fut.result()] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    logger.error("gaia leaf %s: %s", futures[fut], exc)
                if i % 10 == 0 or i == len(futures):
                    logger.info("gaia: %d/%d leaves %s (%.0f s)", i, len(futures),
                                counts, time.time() - t0)
        ref_epoch = source_ref_epoch(url, gaia_leaf_rel(*all_leaves[0], suffix=suffix),
                                     sessions.get()) if all_leaves else None
    finally:
        sessions.close_all()

    info = _write_gaia_info(store, props, all_leaves, ref_epoch, url)
    logger.info("gaia: %s, %d/%d leaves, %s rows, %.1f GB, this run %s (%.0f s)",
                info["release"], info["leaves_done"], info["leaves_total"],
                f"{info['rows']:,}", info["bytes"] / 1e9, counts, time.time() - t0)
    return {**info, "counts": counts}


def gaia_from_table(dest: str, table: pa.Table, release: str,
                    ref_epoch: float | None = None,
                    order: int = GAIA_TABLE_ORDER) -> dict[str, Any]:
    """Write the Gaia layout under `dest` from one table of rows on Gaia's schema.

    For a synthetic catalog (a simulation's truth stars on Gaia's columns)
    or a box extract for a local walkthrough. ``source_id``, ``ra`` and
    ``dec`` are required; every other GAIA_COLUMNS column absent from the
    table is filled with nulls; ``_healpix_29`` is computed from the
    positions. Rows are cut into order-`order` leaves, each sorted by the
    key, and the two metadata files and the ingest record are written as
    the ingest writes them, marked complete. The reference epoch is
    `ref_epoch`, else the table's single ``ref_epoch`` value, else 2016.0
    (Gaia DR3's); the ``ref_epoch`` column is written with it.
    """
    missing = [c for c in GAIA_TABLE_REQUIRED if c not in table.column_names]
    if missing:
        raise ValueError(f"table lacks columns {missing}")
    if ref_epoch is None:
        if "ref_epoch" in table.column_names and table.num_rows:
            epochs = pc.unique(table["ref_epoch"].drop_null()).to_pylist()
            if len(epochs) != 1:
                raise ValueError(f"table holds {len(epochs)} reference epochs; pass --ref-epoch")
            ref_epoch = float(epochs[0])
        else:
            ref_epoch = 2016.0
    ra = np.asarray(table["ra"].to_numpy(zero_copy_only=False), dtype=float)
    dec = np.asarray(table["dec"].to_numpy(zero_copy_only=False), dtype=float)
    key = hp.ang2pix(2 ** HP29_ORDER, ra, dec, nest=True, lonlat=True).astype(np.int64)
    columns: dict[str, pa.Array] = {"_healpix_29": pa.array(key)}
    for name in GAIA_COLUMNS:
        if name == "_healpix_29":
            continue
        if name == "ref_epoch":
            columns[name] = pa.array(np.full(table.num_rows, ref_epoch))
        elif name in table.column_names:
            columns[name] = table[name].combine_chunks()
        else:
            columns[name] = pa.nulls(table.num_rows, pa.float64())
    full = pa.table(columns).sort_by("_healpix_29")
    store = Store(dest)
    shift = 2 * (HP29_ORDER - order)
    npix = pc.shift_right(full["_healpix_29"], shift)
    leaves = []
    for pixel in pc.unique(npix).to_pylist():
        rows = full.filter(pc.equal(npix, pixel))
        rel = gaia_leaf_rel(order, int(pixel))
        if not store.is_s3:
            Path(store.key(rel)).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(rows, store.fs_path(rel), filesystem=store.fs,
                       compression=GAIA_COMPRESSION)
        store.write_text(gaia_done_marker(order, int(pixel)), json.dumps({
            "order": order, "npix": int(pixel), "file": rel, "rows": rows.num_rows,
            "row_groups": pq.ParquetFile(store.fs.open_input_file(store.fs_path(rel))).metadata.num_row_groups,
            "bytes": store.size(rel),
            "written_at": datetime.now(timezone.utc).isoformat()}, indent=1))
        leaves.append((order, int(pixel)))
    store.write_text("partition_info.csv",
                     "Norder,Npix\n" + "".join(f"{o},{p}\n" for o, p in leaves))
    props = {"obs_collection": release, "hats_nrows": str(full.num_rows),
             "hats_npix_suffix": ".parquet", "hats_release_date": datetime.now(timezone.utc).date().isoformat()}
    store.write_text("hats.properties", "".join(f"{k}={v}\n" for k, v in props.items()))
    info = _write_gaia_info(store, props, leaves, ref_epoch, "table")
    logger.info("gaia: %s from a table, %d leaves at order %d, %s rows",
                release, len(leaves), order, f"{info['rows']:,}")
    return info


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10,
                             cwd=str(Path(__file__).resolve().parent))
        return out.stdout.strip() or None
    except Exception:
        return None


def build_manifest(dest: str) -> dict[str, Any]:
    """Describe what is under `dest`: release, md5s, and the hp6 inventory
    gathered from the per-leaf done markers."""
    store = Store(dest)
    hats = f"{HATS_SUBDIR}/"
    props = read_properties(store.read_text(hats + "hats.properties"))
    md5s = parse_md5sums(store.read_text(hats + "md5sums.txt"))
    leaves = parse_partition_info(store.read_text(hats + "partition_info.csv"))
    done = [json.loads(store.read_text(rel))
            for rel in store.list_files(f"{HP6_SUBDIR}/{DONE_SUBDIR}")
            if rel.endswith(".json")]
    rows_per_file = sorted(p["rows"] for d in done for p in d["pixels"])
    hp6_pixels = sorted({p["hp6"] for d in done for p in d["pixels"]})
    n_pixels_total = 12 * 4 ** HP6_ORDER               # 49,152 at order 6
    return {
        "release": props.get("obs_collection"),
        "hats_nrows": int(props.get("hats_nrows", 0)),
        "hats_properties": props,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tool": TOOL,
        "git_sha": git_sha(),
        "hats": {"subdir": HATS_SUBDIR, "n_leaves": len(leaves), "md5sums": md5s},
        "hp6": {"subdir": HP6_SUBDIR, "order": HP6_ORDER,
                "file_pattern": hp6_file(0).replace("hp6=0", "hp6=<pixel>"),
                "leaves_done": len(done), "leaves_total": len(leaves),
                "complete": len(done) == len(leaves),
                "source_leaves_deleted": sum(bool(d.get("source_deleted")) for d in done),
                "rows": sum(d["rows"] for d in done),
                "n_files": len(hp6_pixels),
                # summary in place of the file list (49k entries at order 6);
                # the reader tests a pixel's file directly
                "pixel_min": hp6_pixels[0] if hp6_pixels else None,
                "pixel_max": hp6_pixels[-1] if hp6_pixels else None,
                "sky_coverage": round(len(hp6_pixels) / n_pixels_total, 4),
                "rows_per_file_median": (rows_per_file[len(rows_per_file) // 2]
                                         if rows_per_file else 0),
                "rows_per_file_max": rows_per_file[-1] if rows_per_file else 0},
        # NED-LVS, once ingest_lvs() has run (see the NED-LVS section)
        **({"lvs": json.loads(store.read_text(LVS_INFO))}
           if store.exists(LVS_INFO) else {}),
    }


def write_manifest(dest: str) -> dict[str, Any]:
    manifest = build_manifest(dest)
    Store(dest).write_text(MANIFEST_NAME, json.dumps(manifest, indent=1))
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="catalog_store",
        description="Build the pipeline's copies of the external catalogs: mirror, verify "
                    "and repartition the NED object directory (HATS collection), ingest "
                    "NED-LVS, and ingest Gaia DR3; no local staging.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mirror", help="stream the collection into the store")
    p.add_argument("--base-url", required=True,
                   help="URL of the collection root (the directory holding hats.properties)")
    p.add_argument("--dest", required=True, help="s3://bucket/prefix or a local dir")
    p.add_argument("--jobs", type=int, default=4, help="parallel transfers")

    p = sub.add_parser("verify", help="check stored md5s against md5sums.txt")
    p.add_argument("--dest", required=True)

    p = sub.add_parser("repartition", help="re-cut the leaves into order-6 files")
    p.add_argument("--dest", required=True)
    p.add_argument("--jobs", type=int, default=2, help="worker processes (one leaf each)")
    p.add_argument("--leaves", nargs="*", default=None,
                   help="leaf paths relative to the collection root (dry runs)")
    p.add_argument("--delete-leaves", action="store_true",
                   help="remove each mirrored leaf once it is re-cut, so only "
                        "the order-6 copy remains (the metadata files stay)")

    p = sub.add_parser("manifest", help="write manifest.json under the prefix")
    p.add_argument("--dest", required=True)

    p = sub.add_parser("ingest-lvs", help="stream the current NED-LVS FITS into "
                                          "the store and convert it to parquet")
    p.add_argument("--dest", required=True, help="s3://bucket/prefix or a local dir")
    p.add_argument("--url", default=LVS_URL,
                   help="the NED-LVS FITS (default: NED's fixed 'current' URL)")
    p.add_argument("--force", action="store_true",
                   help="re-download and re-convert even if the stored source "
                        "matches what the server reports")

    p = sub.add_parser("ingest-gaia", help="project LINCC's Gaia DR3 HATS catalog "
                                           "(GAIA_COLUMNS only) into the store")
    p.add_argument("--dest", required=True, help="s3://bucket/prefix or a local dir")
    p.add_argument("--url", default=GAIA_URL,
                   help="catalog root (the directory holding hats.properties); "
                        "a local directory works too")
    p.add_argument("--jobs", type=int, default=4, help="parallel leaves")
    p.add_argument("--leaves", nargs="*", default=None, metavar="ORDER/NPIX",
                   help="only these leaves, e.g. 6/28823 (dry runs, a local copy)")
    p.add_argument("--force", action="store_true",
                   help="redo leaves that already have a done marker")

    p = sub.add_parser("gaia-from-table", help="write the Gaia layout from one Parquet "
                                               "table of rows on Gaia's schema")
    p.add_argument("--dest", required=True, help="s3://bucket/prefix or a local dir")
    p.add_argument("--table", required=True, help="the Parquet file (source_id, ra, dec at least)")
    p.add_argument("--release", required=True, help="the name recorded as the release")
    p.add_argument("--ref-epoch", type=float, default=None,
                   help="the positions' Julian epoch (default: the table's ref_epoch, else 2016.0)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "mirror":
        counts = mirror(args.base_url, args.dest, jobs=args.jobs)
        print(counts)
        return 1 if counts["failed"] else 0
    if args.command == "verify":
        bad = verify(args.dest)
        for rel in bad:
            print("BAD", rel)
        print(f"{len(bad)} missing or mismatched")
        return 1 if bad else 0
    if args.command == "repartition":
        counts = repartition(args.dest, jobs=args.jobs, leaves=args.leaves,
                             delete_leaves=args.delete_leaves)
        print(counts)
        return 1 if counts["failed"] else 0
    if args.command == "manifest":
        manifest = write_manifest(args.dest)
        print(json.dumps({k: v for k, v in manifest.items()
                          if k not in ("hats", "hats_properties")}, indent=1))
        return 0
    if args.command == "ingest-lvs":
        info = ingest_lvs(args.dest, url=args.url, force=args.force)
        print(json.dumps({k: info.get(k) for k in
                          ("status", "release", "rows", "parquet")}, indent=1))
        return 0
    if args.command == "ingest-gaia":
        leaves = ([tuple(int(x) for x in s.split("/")) for s in args.leaves]
                  if args.leaves is not None else None)
        info = ingest_gaia(args.dest, url=args.url, jobs=args.jobs,
                           leaves=leaves, force=args.force)
        print(json.dumps({k: info.get(k) for k in
                          ("release", "leaves_done", "leaves_total", "complete",
                           "rows", "rows_match_hats_nrows", "bytes", "counts")},
                         indent=1))
        return 1 if info["counts"]["failed"] else 0
    if args.command == "gaia-from-table":
        info = gaia_from_table(args.dest, pq.read_table(args.table), args.release,
                               ref_epoch=args.ref_epoch)
        print(json.dumps({k: info.get(k) for k in
                          ("release", "leaves_total", "complete", "rows", "ref_epoch")},
                         indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
