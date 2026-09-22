"""
Tests for alerts/ned_catalog.py: mirroring the NED HATS collection into a
store without local staging, verifying it, re-cutting leaves into order-6
files, and describing the result.

Everything runs against a local-directory Store and a throwaway HTTP
server over a synthetic two-leaf collection; no S3, no network. The one
piece not covered here is boto3's multipart upload itself (Store.put_stream
on s3://), which the real mirror exercises.
"""

import hashlib
import http.server
import json
import threading
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from alerts import ned_catalog as nc


# ---------------------------------------------------------------------------
# a synthetic collection: two leaves, rows deliberately unsorted, ptype ""
# ---------------------------------------------------------------------------

def _leaf_table(order, npix, n=300, seed=0):
    """Rows whose _healpix_29 lie inside HEALPix (order, npix), unsorted,
    spread over several order-6 pixels, with the real schema's key columns."""
    rng = np.random.default_rng(seed)
    lo = npix << (2 * (29 - order))
    hi = (npix + 1) << (2 * (29 - order))
    hp29 = rng.integers(lo, hi, size=n, dtype=np.int64)
    hp9 = hp29 >> 40
    ptype = rng.choice(["", "", "", "G", "QSO", "*"], size=n).tolist()
    z = np.where(rng.random(n) < 0.2, rng.random(n), np.nan)
    return pa.table({
        "_healpix_29": pa.array(hp29, pa.int64()),
        "_healpix_9": pa.array(hp9.astype(np.int32), pa.int32()),
        "prefname": [f"OBJ{i:05d}" for i in range(n)],
        "ra": rng.uniform(0, 360, n), "dec": rng.uniform(-90, 90, n),
        "ptype": pa.array(ptype, pa.string()),
        "z": z, "zunc": np.full(n, np.nan), "zflag": pa.array([None] * n, pa.string()),
        "uncmaja": rng.random(n),
    })


LEAVES = [(2, 5), (3, 200)]


@pytest.fixture()
def collection(tmp_path):
    """A HATS-shaped collection directory: leaves + the four metadata files."""
    root = tmp_path / "collection"
    md5_lines = []
    for order, npix in LEAVES:
        rel = nc.leaf_path(order, npix)
        path = root / rel
        path.parent.mkdir(parents=True)
        pq.write_table(_leaf_table(order, npix, seed=npix), path)
        md5_lines.append(f"{hashlib.md5(path.read_bytes()).hexdigest()}  {rel}")
    (root / "partition_info.csv").write_text(
        "Norder,Npix\n" + "".join(f"{o},{p}\n" for o, p in LEAVES))
    (root / "hats.properties").write_text(
        "# test\nobs_collection=NED_TEST_1.0\nhats_nrows=600\nhats_order=3\n")
    (root / "schema.txt").write_text("Name Type\n_healpix_29 int64\n")
    (root / "md5sums.txt").write_text("\n".join(md5_lines) + "\n")
    return root


@pytest.fixture()
def served(collection):
    """The collection over HTTP on a random local port; yields its base URL."""
    handler = type("H", (http.server.SimpleHTTPRequestHandler,), {
        "__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(
            self, *a, directory=str(collection), **k),
        "log_message": lambda self, *a: None,
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# parsers and layout
# ---------------------------------------------------------------------------

def test_parsers():
    md5s = nc.parse_md5sums("abc  dataset/x.parquet\n\ndef  *dataset/y.parquet\n")
    assert md5s == {"dataset/x.parquet": "abc", "dataset/y.parquet": "def"}
    assert nc.parse_partition_info("Norder,Npix\n2,0\n3,200\n\n") == [(2, 0), (3, 200)]
    # the published layout (release 36.1): positional parsing read Dir as
    # the pixel and sent every leaf to pixel 0 -- live, 2026-09-22
    published = "Norder,Dir,Npix,num_rows\n2,0,0,3251099\n2,0,1,3193928\n3,0,200,1833268\n"
    assert nc.parse_partition_info(published) == [(2, 0), (2, 1), (3, 200)]
    with pytest.raises(ValueError, match="more than once"):
        nc.parse_partition_info("Norder,Dir,Npix\n2,0,0\n2,0,0\n")
    with pytest.raises(ValueError, match="lacks Norder/Npix"):
        nc.parse_partition_info("order,pixel\n2,0\n")
    assert nc.read_properties("# c\nobs_collection=X_1\nhats_nrows=5\n") == {
        "obs_collection": "X_1", "hats_nrows": "5"}


def test_leaf_path_and_hp6_layout():
    assert nc.leaf_path(2, 186) == "dataset/Norder=2/Dir=0/Npix=186/part0.snappy.parquet"
    assert nc.leaf_path(9, 123456) == "dataset/Norder=9/Dir=120000/Npix=123456/part0.snappy.parquet"
    assert nc.hp6_file(7) == "objectdir_hp6/hp6=7/part.parquet"
    assert nc.HP6_SHIFT == 46                      # order 29 -> order 6


def test_normalise_ptype_and_split_by_hp6():
    table = _leaf_table(3, 200, n=500)
    fixed = nc.normalise_ptype(table)
    assert "" not in fixed["ptype"].to_pylist()
    assert fixed["ptype"].null_count == table["ptype"].to_pylist().count("")
    parts = list(nc.split_by_hp6(fixed))
    assert sum(t.num_rows for _, t in parts) == 500
    for pixel, rows in parts:
        hp29 = rows["_healpix_29"].to_numpy()
        assert np.all(hp29 >> 46 == pixel)          # only its own pixel
        assert np.all(np.diff(hp29) >= 0)           # sorted inside the file
        assert 200 << 6 <= pixel < 201 << 6         # inside the order-3 parent
    assert [p for p, _ in parts] == sorted(p for p, _ in parts)


# ---------------------------------------------------------------------------
# mirror + verify over HTTP into a local store
# ---------------------------------------------------------------------------

def test_mirror_streams_and_verifies(served, collection, tmp_path):
    dest = tmp_path / "store"
    counts = nc.mirror(served, str(dest), jobs=2)
    assert counts == {"skipped": 0, "mirrored": 2, "failed": 0}
    hats = dest / nc.HATS_SUBDIR
    for rel in ("hats.properties", "partition_info.csv", "schema.txt", "md5sums.txt"):
        assert (hats / rel).read_bytes() == (collection / rel).read_bytes()
    for order, npix in LEAVES:
        rel = nc.leaf_path(order, npix)
        assert (hats / rel).read_bytes() == (collection / rel).read_bytes()
        assert (hats / (rel + ".md5")).read_text().strip() == \
            hashlib.md5((collection / rel).read_bytes()).hexdigest()
    assert nc.verify(str(dest)) == []
    # a rerun moves nothing
    assert nc.mirror(served, str(dest), jobs=2) == {"skipped": 2, "mirrored": 0, "failed": 0}


def test_mirror_rejects_md5_mismatch_and_verify_reports_it(served, collection,
                                                          tmp_path, monkeypatch):
    # corrupt the manifest's md5 for one leaf: the stream is rejected,
    # the object is not kept, the run reports the failure
    rel = nc.leaf_path(*LEAVES[0])
    md5s = (collection / "md5sums.txt").read_text().replace(
        hashlib.md5((collection / rel).read_bytes()).hexdigest(), "0" * 32)
    (collection / "md5sums.txt").write_text(md5s)
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)      # no backoff
    dest = tmp_path / "store"
    counts = nc.mirror(served, str(dest), jobs=1)
    assert counts == {"skipped": 0, "mirrored": 1, "failed": 1}
    assert not (dest / nc.HATS_SUBDIR / rel).exists()
    assert nc.verify(str(dest)) == [rel]


def test_s3_store_requires_the_buckets_region(monkeypatch):
    # no default and no silent lookup: an unset or wrong region refuses,
    # naming the bucket's real region (Emily, 2026-09-22: a mis-set region
    # is how a 70 GB repartition would run cross-region)
    monkeypatch.setattr(nc.Store, "_bucket_region", lambda self: "us-west-2")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(RuntimeError, match="not set.*us-west-2"):
        nc.Store("s3://some-bucket/ned")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with pytest.raises(RuntimeError, match="us-east-1 but bucket some-bucket is in us-west-2"):
        nc.Store("s3://some-bucket/ned")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    store = nc.Store("s3://some-bucket/ned")
    assert store.is_s3 and store.bucket == "some-bucket" and store.prefix == "ned"


def test_iter_reader_reassembles_the_stream():
    chunks = iter([b"abc", b"defg", b"h"])
    reader = nc._IterReader(chunks)
    assert reader.read(2) == b"ab"
    assert reader.read(4) == b"cdef"
    assert reader.read() == b"gh"
    assert reader.read(3) == b""


# ---------------------------------------------------------------------------
# repartition + manifest on the mirrored store
# ---------------------------------------------------------------------------

def test_repartition_is_correct_and_idempotent(served, tmp_path):
    dest = tmp_path / "store"
    nc.mirror(served, str(dest), jobs=1)
    counts = nc.repartition(str(dest), jobs=1)
    assert counts == {"done": 2, "skipped": 0, "failed": 0, "rows": 600}
    files = sorted((dest / nc.HP6_SUBDIR).glob("hp6=*/part.parquet"))
    assert files
    total = 0
    for f in files:
        pixel = int(f.parent.name.split("=")[1])
        t = pq.read_table(f)
        assert np.all(t["_healpix_29"].to_numpy() >> 46 == pixel)
        assert "" not in t["ptype"].to_pylist()
        total += t.num_rows
    assert total == 600
    # one done marker per leaf; a rerun skips everything
    markers = list((dest / nc.HP6_SUBDIR / nc.DONE_SUBDIR).glob("*.json"))
    assert len(markers) == 2
    assert nc.repartition(str(dest), jobs=1) == {"done": 0, "skipped": 2,
                                                 "failed": 0, "rows": 600}

    manifest = nc.write_manifest(str(dest))
    assert manifest["release"] == "NED_TEST_1.0"
    assert manifest["hp6"]["complete"] is True
    assert manifest["hp6"]["rows"] == 600
    assert manifest["hp6"]["n_files"] == len(files)
    assert set(manifest["hats"]["md5sums"]) == {nc.leaf_path(o, p) for o, p in LEAVES}
    on_disk = json.loads((dest / nc.MANIFEST_NAME).read_text())
    assert on_disk["hp6"]["pixels"] == manifest["hp6"]["pixels"]


def test_delete_leaves_keeps_one_copy(served, tmp_path):
    dest = tmp_path / "store"
    nc.mirror(served, str(dest), jobs=1)
    hats = dest / nc.HATS_SUBDIR
    leaf_files = [hats / nc.leaf_path(o, p) for o, p in LEAVES]
    assert all(f.exists() for f in leaf_files)

    counts = nc.repartition(str(dest), jobs=1, delete_leaves=True)
    assert counts["done"] == 2 and counts["rows"] == 600
    assert not any(f.exists() for f in leaf_files)              # leaves gone
    for name in nc.COLLECTION_FILES:                            # metadata kept
        assert (hats / name).exists()
    total = sum(pq.read_metadata(f).num_rows
                for f in (dest / nc.HP6_SUBDIR).glob("hp6=*/part.parquet"))
    assert total == 600                                         # rows all present once

    # verify does not complain about the consumed leaves; manifest counts them
    assert nc.verify(str(dest)) == []
    manifest = nc.build_manifest(str(dest))
    assert manifest["hp6"]["source_leaves_deleted"] == 2
    assert manifest["hp6"]["complete"] is True
    # a rerun is still a no-op and does not need the leaves
    assert nc.repartition(str(dest), jobs=1, delete_leaves=True) == {
        "done": 0, "skipped": 2, "failed": 0, "rows": 600}


def test_delete_leaves_finishes_an_interrupted_delete(served, tmp_path):
    # marker written, delete not yet done: the next run removes the leaf
    dest = tmp_path / "store"
    nc.mirror(served, str(dest), jobs=1)
    nc.repartition(str(dest), jobs=1)                          # keeps leaves
    leaf = dest / nc.HATS_SUBDIR / nc.leaf_path(*LEAVES[0])
    assert leaf.exists()
    assert nc.repartition(str(dest), jobs=1, delete_leaves=True)["skipped"] == 2
    assert not leaf.exists()
    assert nc.verify(str(dest)) == []


def test_repartition_subset_and_partial_manifest(served, tmp_path):
    dest = tmp_path / "store"
    nc.mirror(served, str(dest), jobs=1)
    only = [nc.leaf_path(*LEAVES[1])]
    assert nc.repartition(str(dest), jobs=1, leaves=only)["done"] == 1
    manifest = nc.build_manifest(str(dest))
    assert (manifest["hp6"]["leaves_done"], manifest["hp6"]["leaves_total"]) == (1, 2)
    assert manifest["hp6"]["complete"] is False


def test_cli_verify_and_manifest(served, tmp_path, capsys):
    dest = tmp_path / "store"
    assert nc.main(["mirror", "--base-url", served, "--dest", str(dest), "--jobs", "1"]) == 0
    assert nc.main(["verify", "--dest", str(dest)]) == 0
    assert nc.main(["repartition", "--dest", str(dest), "--jobs", "1"]) == 0
    assert nc.main(["manifest", "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "NED_TEST_1.0" in out and "0 missing or mismatched" in out
