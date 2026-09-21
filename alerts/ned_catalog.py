"""
File    : ned_catalog.py
Author  : Emily Everetts
Date    : 09/26

Mirror, verify and repartition the NED object directory for the alert
cross-match (providers.py, nedMatches) -- without ever staging it on a disk.

Caltech/IPAC-IRSA publishes NED as a HATS collection (HEALPix-partitioned
parquet). Its 264 leaves are order-2/3 pixels of 50-200 square degrees, up to
8 million rows each, and -- despite hats.properties -- not sorted within a
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

Usage, end to end for a new release:

    # on a machine that can reach the collection URL (IPAC network) AND S3;
    # streams HTTP -> S3, nothing is written locally
    python -m alerts.ned_catalog mirror --base-url <collection-url> \\
                                        --dest s3://rapid-pipeline-files/ned
    python -m alerts.ned_catalog verify --dest s3://rapid-pipeline-files/ned

    # on EC2, next to the bucket; reads each leaf into memory (<= ~1 GB) and
    # deletes it from S3 once its order-6 files are written
    python -m alerts.ned_catalog repartition --dest s3://rapid-pipeline-files/ned \\
                                             --jobs 4 --delete-leaves
    python -m alerts.ned_catalog manifest    --dest s3://rapid-pipeline-files/ned

``--dest`` also accepts a local directory, which is how the tests and dry
runs work (``repartition --leaves`` limits a run to named leaves).

Row keys: ``_healpix_29`` is the NESTED order-29 pixel index (verified against
healpy on release 36.1_20260527_v2), so the order-6 pixel is
``_healpix_29 >> 46`` and every order-6 pixel lies inside exactly one leaf --
leaves repartition independently, no merge step.
"""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.fs
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

TOOL_VERSION = "1.0"

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
    """``partition_info.csv`` -> [(Norder, Npix), ...]."""
    rows = []
    for i, line in enumerate(text.splitlines()):
        if i and line.strip():
            order, npix = line.split(",")[:2]
            rows.append((int(order), int(npix)))
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
            self.fs: pyarrow.fs.FileSystem = pyarrow.fs.S3FileSystem(
                region=os.environ.get("AWS_DEFAULT_REGION"))
            self._client: Any = None
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


def mirror_file(base_url: str, store: Store, rel: str,
                expected_md5: str | None = None, session: Any = None,
                retries: int = 5) -> str:
    """Stream one collection file into the store under ``objectdir/hats/<rel>``.

    Returns ``"skipped"`` when the store already holds it with the expected
    md5, else ``"mirrored"``. A stored object whose md5 disagrees with the
    manifest is deleted and re-streamed; after `retries` failures a
    RuntimeError names the file.
    """
    import requests
    session = session or requests.Session()
    dest_rel = f"{HATS_SUBDIR}/{rel}"
    if expected_md5 and store.stored_md5(dest_rel) == expected_md5:
        return "skipped"
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            got = store.put_stream(dest_rel, http_chunks(base_url + rel, session))
            if expected_md5 and got != expected_md5:
                store.delete(dest_rel)
                raise RuntimeError(f"md5 mismatch: got {got}, expected {expected_md5}")
            return "mirrored"
        except Exception as exc:
            last = exc
            logger.warning("%s: attempt %d/%d failed (%s)", rel, attempt, retries, exc)
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"giving up on {rel}: {last}")


def mirror(base_url: str, dest: str, jobs: int = 4) -> dict[str, int]:
    """Mirror the whole collection at `base_url` into ``<dest>/objectdir/hats``.

    Nothing is written locally. Idempotent: files already present with the
    manifest's md5 are skipped, so a rerun finishes an interrupted mirror
    and a new release replaces only what changed.
    """
    import requests
    base_url = base_url.rstrip("/") + "/"
    store = Store(dest)
    session = requests.Session()
    for name in COLLECTION_FILES:                  # small; always refreshed
        mirror_file(base_url, store, name, session=session)
    md5s = parse_md5sums(store.read_text(f"{HATS_SUBDIR}/md5sums.txt"))
    counts = {"skipped": 0, "mirrored": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(mirror_file, base_url, store, rel, md5,
                               requests.Session()): rel
                   for rel, md5 in md5s.items()}
        for i, fut in enumerate(as_completed(futures), start=1):
            rel = futures[fut]
            try:
                counts[fut.result()] += 1
            except Exception as exc:
                counts["failed"] += 1
                logger.error("%s: %s", rel, exc)
            if i % 10 == 0 or i == len(futures):
                logger.info("mirror: %d/%d files %s", i, len(futures), counts)
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
    A leaf whose marker exists but which is still present -- a run that
    stopped between marker and delete -- is deleted on the next run.
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
    hp6_pixels = sorted({p["hp6"] for d in done for p in d["pixels"]})
    return {
        "release": props.get("obs_collection"),
        "hats_nrows": int(props.get("hats_nrows", 0)),
        "hats_properties": props,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tool": f"alerts.ned_catalog {TOOL_VERSION}",
        "git_sha": git_sha(),
        "hats": {"subdir": HATS_SUBDIR, "n_leaves": len(leaves), "md5sums": md5s},
        "hp6": {"subdir": HP6_SUBDIR, "order": HP6_ORDER,
                "file_pattern": hp6_file(0).replace("hp6=0", "hp6=<pixel>"),
                "leaves_done": len(done), "leaves_total": len(leaves),
                "complete": len(done) == len(leaves),
                "source_leaves_deleted": sum(bool(d.get("source_deleted")) for d in done),
                "rows": sum(d["rows"] for d in done),
                "n_files": len(hp6_pixels), "pixels": hp6_pixels},
    }


def write_manifest(dest: str) -> dict[str, Any]:
    manifest = build_manifest(dest)
    Store(dest).write_text(MANIFEST_NAME, json.dumps(manifest, indent=1))
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror, verify and repartition the NED object directory "
                    "(HATS collection) for the alert cross-match; no local staging.")
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
