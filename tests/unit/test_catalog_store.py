"""The catalog store: mirroring the NED HATS collection into a store without
local staging, verifying it, re-cutting leaves into order-6 files, describing
the result, and projecting a LINCC-shaped Gaia catalog (`dev`'s
``alerts/test/test_catalog_store.py``, ported 2026-10-29, plus the rebuild's
``gaia-from-table`` and the write-once recovery).

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

from rapidpipe.science.catalogs import store as nc


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


def _serve(directory, ranges=False):
    """`directory` over HTTP on a random local port, optionally honouring Range."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(directory), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = Path(self.translate_path(self.path))
            spec = self.headers.get("Range")
            if not ranges or not spec or not path.is_file():
                return super().do_GET()
            lo, _, hi = spec.removeprefix("bytes=").partition("-")
            size = path.stat().st_size
            lo, hi = int(lo), min(int(hi) if hi else size - 1, size - 1)
            with open(path, "rb") as f:
                f.seek(lo)
                body = f.read(hi - lo + 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


@pytest.fixture()
def served(collection):
    """The collection over HTTP on a random local port; yields its base URL."""
    server, url = _serve(collection)
    try:
        yield url
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
    # the pixel and sent every leaf to pixel 0 (live, 2026-09-22)
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


def test_s3_store_trusts_the_region_when_the_lookup_is_denied(monkeypatch):
    # a job role without GetBucketLocation: the stated region is trusted
    # with a warning (the rebuild's departure); unset still refuses
    monkeypatch.setattr(nc.Store, "_bucket_region", lambda self: None)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        nc.Store("s3://some-bucket/ned")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert nc.Store("s3://some-bucket/ned").is_s3


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
    # summary values, not a 49k-entry pixel list
    assert "pixels" not in manifest["hp6"]
    pixels = sorted(int(f.parent.name.split("=")[1]) for f in files)
    assert (manifest["hp6"]["pixel_min"], manifest["hp6"]["pixel_max"]) == (pixels[0], pixels[-1])
    assert manifest["hp6"]["sky_coverage"] == round(len(files) / 49152, 4)
    per_file = sorted(pq.read_metadata(f).num_rows for f in files)
    assert manifest["hp6"]["rows_per_file_max"] == per_file[-1]
    assert manifest["hp6"]["rows_per_file_median"] == per_file[len(per_file) // 2]
    on_disk = json.loads((dest / nc.MANIFEST_NAME).read_text())
    assert on_disk["hp6"] == manifest["hp6"]


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


def test_cli_parse_failure_exits_64():
    with pytest.raises(SystemExit) as info:
        nc.main(["ingest-gaia"])                   # --dest is required
    assert info.value.code == 64


def test_the_module_is_the_command_line(tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    module = [sys.executable, "-m", "rapidpipe.science.catalogs.store"]
    env = {**os.environ, "PYTHONPATH": str(root)}
    table = tmp_path / "t.parquet"
    pq.write_table(pa.table({"source_id": pa.array([7], pa.int64()), "ra": [1.0], "dec": [2.0]}), table)
    run = subprocess.run([*module, "gaia-from-table", "--dest", str(tmp_path / "g"),
                          "--table", str(table), "--release", "r"],
                         capture_output=True, text=True, cwd=root, env=env)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["rows"] == 1
    assert subprocess.run([*module, "--help"], capture_output=True, cwd=root, env=env).returncode == 0
    assert subprocess.run(module, capture_output=True, cwd=root, env=env).returncode == 64


# ---------------------------------------------------------------------------
# Gaia: a synthetic LINCC-shaped catalog (all columns + extras), projected
# ---------------------------------------------------------------------------

GAIA_LEAVES = [(2, 5), (6, 28823)]


def _gaia_leaf_table(order, npix, n=500, seed=0):
    """Rows inside (order, npix) with every GAIA_COLUMNS column, in the real
    types, plus two columns the projection must drop."""
    rng = np.random.default_rng(seed)
    lo, hi = npix << (2 * (29 - order)), (npix + 1) << (2 * (29 - order))
    f32 = lambda: pa.array(rng.random(n).astype(np.float32))   # noqa: E731
    cols = {
        "_healpix_29": pa.array(np.sort(rng.integers(lo, hi, n, dtype=np.int64))),
        "solution_id": pa.array(np.full(n, 1636148068921376768, dtype=np.int64)),
        "ref_epoch": pa.array(np.full(n, 2016.0)),
        "designation": [f"Gaia DR3 {i}" for i in range(n)],
        "source_id": pa.array(rng.integers(1, 2**62, n, dtype=np.int64)),
        "ra": rng.uniform(0, 360, n), "dec": rng.uniform(-90, 90, n),
        "ra_error": f32(), "dec_error": f32(),
        "pmra": rng.normal(0, 5, n), "pmdec": rng.normal(0, 5, n),
        "pmra_error": f32(), "pmdec_error": f32(),
        "parallax": rng.normal(1, 1, n), "parallax_error": f32(),
        "ruwe": f32(), "astrometric_excess_noise": f32(),
        "ipd_frac_multi_peak": pa.array(rng.integers(0, 100, n).astype(np.int8)),
        "duplicated_source": pa.array(rng.random(n) < 0.1),
        "phot_g_mean_mag": f32(), "phot_bp_mean_mag": f32(), "phot_rp_mean_mag": f32(),
        "bp_rp": f32(), "phot_g_mean_flux_over_error": f32(),
        "phot_variable_flag": pa.array(rng.choice(["NOT_AVAILABLE", "VARIABLE"], n).tolist()),
        "non_single_star": pa.array(rng.integers(0, 4, n).astype(np.int16)),
        "in_qso_candidates": pa.array(rng.random(n) < 0.01),
        "in_galaxy_candidates": pa.array(rng.random(n) < 0.01),
        "classprob_dsc_combmod_star": f32(), "classprob_dsc_combmod_galaxy": f32(),
        "classprob_dsc_combmod_quasar": f32(),
    }
    return pa.table(cols)


@pytest.fixture()
def gaia_collection(tmp_path):
    """A LINCC-shaped Gaia catalog directory: Npix=<p>.parquet leaves with
    several row groups, plus the two metadata files."""
    root = tmp_path / "gaia_src"
    for order, npix in GAIA_LEAVES:
        path = root / nc.gaia_leaf_rel(order, npix)
        path.parent.mkdir(parents=True)
        pq.write_table(_gaia_leaf_table(order, npix, seed=npix), path, row_group_size=200)
    (root / "partition_info.csv").write_text(
        "Norder,Npix\n" + "".join(f"{o},{p}\n" for o, p in GAIA_LEAVES))
    (root / "hats.properties").write_text(
        "#HATS catalog\nobs_collection=gaia_test\nhats_nrows=1000\n"
        "hats_npix_suffix=.parquet\nhats_release_date=2024-09-18\n")
    return root


@pytest.fixture()
def gaia_served(gaia_collection):
    """`gaia_collection` over HTTP with Range support (SimpleHTTPRequestHandler
    has none), which is what the ingest's HTTP reader relies on."""
    server, url = _serve(gaia_collection, ranges=True)
    try:
        yield url
    finally:
        server.shutdown()


def test_http_range_file_reads_like_a_file(gaia_served, gaia_collection):
    rel = nc.gaia_leaf_rel(*GAIA_LEAVES[1])
    raw = (gaia_collection / rel).read_bytes()
    f = nc._HttpRangeFile(gaia_served + rel, nc.requests.Session())
    assert f.size == len(raw)
    f.seek(-8, 2)
    assert f.read(4) == raw[-8:-4] and f.tell() == len(raw) - 4
    f.seek(10)
    assert f.read(100) == raw[10:110]
    assert f.read(0) == b""
    f.seek(0, 2)
    assert f.read(1) == b""


def test_ingest_gaia_projects_resumes_recovers_and_verifies(gaia_served, tmp_path):
    dest = tmp_path / "store"
    info = nc.ingest_gaia(str(dest), gaia_served, jobs=2)
    assert info["counts"] == {"skipped": 0, "done": 2, "recovered": 0, "failed": 0}
    assert info["complete"] and info["rows_match_hats_nrows"] is True
    assert info["rows"] == 1000 and info["leaves_done"] == 2
    assert info["ref_epoch"] == 2016.0                           # from the source's statistics
    for order, npix in GAIA_LEAVES:
        t = pq.read_table(dest / nc.gaia_leaf_rel(order, npix))
        assert t.column_names == list(nc.GAIA_COLUMNS)           # dropped the extras
        assert "ref_epoch" in t.column_names                     # the epoch travels with the rows
        assert t.num_rows == 500
        assert t["phot_g_mean_mag"].type == pa.float32()         # types preserved
        assert t["ipd_frac_multi_peak"].type == pa.int8()
        meta = pq.ParquetFile(dest / nc.gaia_leaf_rel(order, npix)).metadata
        assert meta.row_group(0).column(0).compression == "ZSTD"
        assert meta.num_row_groups == 3                          # row groups kept
    assert (dest / "hats.properties").exists()
    assert (dest / "partition_info.csv").exists()
    # resumable: a leaf without its marker but present is recovered from its
    # footer (a bucket that refuses overwrites cannot take it again), the other skipped
    marker = dest / nc.gaia_done_marker(*GAIA_LEAVES[0])
    before = json.loads(marker.read_text())
    marker.unlink()
    again = nc.ingest_gaia(str(dest), gaia_served, jobs=1)
    assert again["counts"] == {"skipped": 1, "done": 0, "recovered": 1, "failed": 0}
    after = json.loads(marker.read_text())
    assert {k: after[k] for k in ("rows", "row_groups", "bytes")} == \
        {k: before[k] for k in ("rows", "row_groups", "bytes")}
    # a leaf that is gone with its marker is projected again
    marker.unlink()
    (dest / nc.gaia_leaf_rel(*GAIA_LEAVES[0])).unlink()
    assert nc.ingest_gaia(str(dest), gaia_served, jobs=1)["counts"]["done"] == 1
    # a subset run only touches named leaves, and names must exist
    sub = nc.ingest_gaia(str(dest), gaia_served, leaves=[GAIA_LEAVES[1]], force=True)
    assert sub["counts"] == {"skipped": 0, "done": 1, "recovered": 0, "failed": 0}
    with pytest.raises(ValueError, match="not in partition_info"):
        nc.ingest_gaia(str(dest), gaia_served, leaves=[(6, 1)])


def test_ingest_gaia_from_a_local_directory_and_cli(gaia_collection, tmp_path, capsys):
    dest = tmp_path / "store"
    assert nc.main(["ingest-gaia", "--url", str(gaia_collection), "--dest", str(dest),
                    "--leaves", "6/28823", "--jobs", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["leaves_done"] == 1 and out["leaves_total"] == 2 and out["complete"] is False
    assert out["rows_match_hats_nrows"] is None               # unknown until complete
    assert pq.read_table(dest / nc.gaia_leaf_rel(6, 28823)).num_rows == 500
    record = json.loads((dest / nc.GAIA_INFO).read_text())
    assert record["release"] == "gaia_test" and record["columns"] == list(nc.GAIA_COLUMNS)
    assert record["tool"].startswith("rapidpipe.science.catalogs.store")


def test_gaia_from_table_writes_the_layout_the_reader_expects(tmp_path, capsys):
    # a box extract: every column, as the archive query returns it
    rows = _gaia_leaf_table(2, 5, n=40, seed=1).drop_columns(["_healpix_29"])
    rng = np.random.default_rng(2)
    rows = rows.set_column(rows.schema.get_field_index("ra"), "ra",
                           pa.array(rng.uniform(7.0, 11.0, 40)))
    rows = rows.set_column(rows.schema.get_field_index("dec"), "dec",
                           pa.array(rng.uniform(-45.5, -42.8, 40)))
    table = tmp_path / "box.parquet"
    pq.write_table(rows, table)
    dest = tmp_path / "store"
    assert nc.main(["gaia-from-table", "--dest", str(dest), "--table", str(table),
                    "--release", "gaia_dr3_box"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"release": "gaia_dr3_box", "leaves_total": out["leaves_total"],
                   "complete": True, "rows": 40, "ref_epoch": 2016.0}
    leaves = nc.parse_partition_info((dest / "partition_info.csv").read_text())
    assert leaves and all(order == nc.GAIA_TABLE_ORDER for order, _ in leaves)
    total = 0
    for order, npix in leaves:
        t = pq.read_table(dest / nc.gaia_leaf_rel(order, npix))
        assert t.column_names == list(nc.GAIA_COLUMNS)
        key = t["_healpix_29"].to_numpy()
        assert np.all(np.diff(key) >= 0) and np.all(key >> (2 * (29 - order)) == npix)
        total += t.num_rows
    assert total == 40
    info = json.loads((dest / nc.GAIA_INFO).read_text())
    assert info["complete"] and info["rows"] == 40 and info["ref_epoch"] == 2016.0

    # a synthetic catalog: the three required columns only, the rest null;
    # the epoch is the caller's
    few = pa.table({"source_id": pa.array([1, 2], pa.int64()), "ra": [10.0, 10.1], "dec": [-44.0, -44.0]})
    info = nc.gaia_from_table(str(tmp_path / "few"), few, "truth_stars", ref_epoch=2026.5)
    assert info["rows"] == 2 and info["ref_epoch"] == 2026.5
    (leaf,) = nc.parse_partition_info((tmp_path / "few" / "partition_info.csv").read_text())
    t = pq.read_table(tmp_path / "few" / nc.gaia_leaf_rel(*leaf))
    assert t["pmra"].null_count == 2 and t["ref_epoch"].to_pylist() == [2026.5, 2026.5]
    with pytest.raises(ValueError, match="lacks columns"):
        nc.gaia_from_table(str(tmp_path / "bad"), pa.table({"ra": [1.0]}), "x")
