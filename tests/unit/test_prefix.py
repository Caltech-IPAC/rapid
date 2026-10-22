"""``rapidpipe.foundation.prefix``: files under a ``file://`` or ``s3://`` URL.

Contracts: ``get`` gives a ``file://`` URL's own path and downloads an
``s3://`` object once per process, raising ``FileNotFoundError`` for an
absent one; ``put`` leaves a ``file://`` prefix alone and uploads a
directory to an ``s3://`` prefix with ``manifest.json`` last; ``attempts``
names the attempt directories present under an ``s3://`` job prefix; the
download cache is made under ``TMPDIR`` and removed at process exit.
"""

from __future__ import annotations

import pytest

from rapidpipe.foundation import prefix

from .fakes3 import FakeS3


@pytest.fixture()
def s3(monkeypatch, tmp_path):
    fake = FakeS3(page_size=2)
    monkeypatch.setattr("rapidpipe.products.storage.s3_client", lambda: fake)
    monkeypatch.setattr(prefix, "_cache", tmp_path / "cache")
    return fake


def test_get_of_a_file_url_is_its_own_path(tmp_path):
    (tmp_path / "a.fits").write_bytes(b"x")
    assert prefix.get((tmp_path / "a.fits").as_uri()) == tmp_path / "a.fits"


def test_get_downloads_an_s3_object_once(s3):
    s3.seed("bucket", "run/1/a.parquet", b"rows")
    first = prefix.get("s3://bucket/run/1/a.parquet")
    again = prefix.get("s3://bucket/run/1/a.parquet")
    assert first == again and first.read_bytes() == b"rows"
    assert [c for c in s3.calls if c[0] == "download_file"] == [("download_file", "run/1/a.parquet")]


def test_get_of_an_absent_s3_object_is_file_not_found(s3):
    with pytest.raises(FileNotFoundError):
        prefix.get("s3://bucket/run/1/missing.parquet")


def test_put_to_a_file_prefix_moves_nothing_and_refuses_another_directory(tmp_path):
    prefix.put(tmp_path, tmp_path.as_uri() + "/")
    with pytest.raises(ValueError):
        prefix.put(tmp_path / "elsewhere", tmp_path.as_uri() + "/")


def test_put_uploads_every_file_with_the_manifest_last(s3, tmp_path):
    work = tmp_path / "work"
    (work / "packets").mkdir(parents=True)
    for name in ("manifest.json", "packets.parquet", "packets/1-8-8.avro"):
        (work / name).write_bytes(name.encode())
    prefix.put(work, "s3://bucket/root/3/packet/9/1/")
    uploads = [key for op, key in s3.calls if op == "upload_file"]
    assert sorted(uploads) == ["root/3/packet/9/1/manifest.json", "root/3/packet/9/1/packets.parquet",
                               "root/3/packet/9/1/packets/1-8-8.avro"]
    assert uploads[-1] == "root/3/packet/9/1/manifest.json"


def test_attempts_names_the_attempt_directories_under_a_job_prefix(s3):
    for key in ("root/3/packet/9/1/manifest.json", "root/3/packet/9/1/a.parquet",
                "root/3/packet/9/2/a.parquet", "root/3/packet/90/1/a.parquet"):
        s3.seed("bucket", key, b"")
    assert prefix.attempts("s3://bucket/root/3/packet/9") == {1, 2}
    assert prefix.attempts("s3://bucket/root/3/packet/8") == set()


def test_the_download_cache_is_made_under_tmpdir_and_removed_at_exit(monkeypatch, tmp_path):
    fake = FakeS3(page_size=2)
    fake.seed("bucket", "run/1/a.parquet", b"rows")
    monkeypatch.setattr("rapidpipe.products.storage.s3_client", lambda: fake)
    monkeypatch.setattr(prefix, "_cache", None)
    monkeypatch.setattr(prefix.tempfile, "tempdir", str(tmp_path))
    registered = []
    monkeypatch.setattr(prefix.atexit, "register", lambda *call, **kw: registered.append((call, kw)))
    path = prefix.get("s3://bucket/run/1/a.parquet")
    cache = prefix._cache
    assert cache.parent == tmp_path and path.is_relative_to(cache)
    (remove, *args), kwargs = registered[0]
    remove(*args, **kwargs)
    assert not cache.exists()
