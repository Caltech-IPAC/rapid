"""Tests for rapidpipe.products.storage: no network, no boto3 required.

Every S3 call goes through a FakeS3 client passed explicitly, so this
module never imports boto3 and runs identically whether or not it is
installed.
"""

from __future__ import annotations

import pytest

from rapidpipe.products.storage import (
    Location,
    LocationError,
    fetch_object,
    fetch_prefix,
    join,
    parse_location,
    publish_dir,
)

from .fakes3 import FakeS3


def test_parse_location_local(tmp_path):
    location = parse_location(str(tmp_path / "some" / "dir"))
    assert location.kind == "local"
    assert location.path == tmp_path / "some" / "dir"
    assert not location.is_s3()


def test_parse_location_s3_bucket_root():
    location = parse_location("s3://my-bucket")
    assert location.kind == "s3"
    assert location.bucket == "my-bucket"
    assert location.prefix == ""
    assert location.is_s3()


def test_parse_location_s3_bucket_root_trailing_slash():
    location = parse_location("s3://my-bucket/")
    assert location.bucket == "my-bucket"
    assert location.prefix == ""


def test_parse_location_s3_with_prefix():
    location = parse_location("s3://my-bucket/runs/r1/admit/")
    assert location.bucket == "my-bucket"
    assert location.prefix == "runs/r1/admit"


@pytest.mark.parametrize("text", ["s3://", "s3:///no-bucket"])
def test_parse_location_malformed_s3_raises(text):
    with pytest.raises(LocationError):
        parse_location(text)


def test_join_local(tmp_path):
    location = parse_location(str(tmp_path / "outputs"))
    assert join(location, "manifest.json") == str(tmp_path / "outputs" / "manifest.json")


def test_join_s3():
    location = parse_location("s3://bucket/runs/r1")
    assert join(location, "manifest.json") == "s3://bucket/runs/r1/manifest.json"


def test_join_s3_bucket_root():
    location = parse_location("s3://bucket")
    assert join(location, "manifest.json") == "s3://bucket/manifest.json"


def test_fetch_prefix_local_returns_path_unchanged(tmp_path):
    src = tmp_path / "inputs"
    src.mkdir()
    location = parse_location(str(src))
    result = fetch_prefix(location, tmp_path / "unused-dest")
    assert result == src
    assert not (tmp_path / "unused-dest").exists()


def test_fetch_prefix_s3_downloads_three_objects_preserving_relative_keys(tmp_path):
    fake = FakeS3()
    fake.seed("bucket", "runs/r1/manifest.json", b'{"a": 1}')
    fake.seed("bucket", "runs/r1/l2/delivered.fits", b"fits-bytes")
    fake.seed("bucket", "runs/r1/exec/a0.json", b'{"b": 2}')

    location = parse_location("s3://bucket/runs/r1")
    dest = tmp_path / "dest"
    result = fetch_prefix(location, dest, client=fake)

    assert result == dest
    assert (dest / "manifest.json").read_bytes() == b'{"a": 1}'
    assert (dest / "l2" / "delivered.fits").read_bytes() == b"fits-bytes"
    assert (dest / "exec" / "a0.json").read_bytes() == b'{"b": 2}'


def test_fetch_prefix_s3_paginates(tmp_path):
    fake = FakeS3(page_size=1)
    fake.seed("bucket", "p/a.txt", b"a")
    fake.seed("bucket", "p/b.txt", b"b")
    fake.seed("bucket", "p/c.txt", b"c")

    location = parse_location("s3://bucket/p")
    dest = tmp_path / "dest"
    fetch_prefix(location, dest, client=fake)

    assert (dest / "a.txt").read_bytes() == b"a"
    assert (dest / "b.txt").read_bytes() == b"b"
    assert (dest / "c.txt").read_bytes() == b"c"
    list_calls = [c for c in fake.calls if c[0] == "list_objects_v2"]
    assert len(list_calls) == 3


def test_fetch_prefix_refuses_non_empty_destination(tmp_path):
    fake = FakeS3()
    fake.seed("bucket", "p/a.txt", b"a")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "existing.txt").write_text("already here")

    location = parse_location("s3://bucket/p")
    with pytest.raises(LocationError):
        fetch_prefix(location, dest, client=fake)


def test_fetch_prefix_s3_missing_prefix_raises(tmp_path):
    fake = FakeS3()
    location = parse_location("s3://bucket/nothing-here")
    with pytest.raises(LocationError):
        fetch_prefix(location, tmp_path / "dest", client=fake)


def test_fetch_object_local_returns_path_unchanged(tmp_path):
    src = tmp_path / "inputs"
    src.mkdir()
    location = parse_location(str(src))
    result = fetch_object(location, "manifest.json", tmp_path / "unused")
    assert result == src / "manifest.json"
    assert not (tmp_path / "unused").exists()


def test_fetch_object_s3_downloads_one_object(tmp_path):
    fake = FakeS3()
    fake.seed("bucket", "p/manifest.json", b'{"ok": true}')
    fake.seed("bucket", "p/l2/delivered.fits", b"should-not-be-fetched")

    location = parse_location("s3://bucket/p")
    dest = tmp_path / "manifest.json"
    result = fetch_object(location, "manifest.json", dest, client=fake)

    assert result == dest
    assert dest.read_bytes() == b'{"ok": true}'
    download_calls = [c for c in fake.calls if c[0] == "download_file"]
    assert download_calls == [("download_file", "p/manifest.json")]


def test_publish_dir_local_noop_when_same_path(tmp_path):
    src = tmp_path / "outputs"
    src.mkdir()
    location = parse_location(str(src))
    publish_dir(src, location)  # must not raise


def test_publish_dir_local_different_path_raises(tmp_path):
    src = tmp_path / "outputs"
    src.mkdir()
    other = tmp_path / "elsewhere"
    location = parse_location(str(other))
    with pytest.raises(LocationError):
        publish_dir(src, location)


def test_publish_dir_s3_uploads_everything_with_manifest_last(tmp_path):
    src = tmp_path / "outputs"
    (src / "l2").mkdir(parents=True)
    (src / "exec").mkdir()
    (src / "manifest.json").write_text('{"a": 1}')
    (src / "l2" / "delivered.fits").write_bytes(b"fits-bytes")
    (src / "exec" / "a0.json").write_text('{"b": 2}')

    fake = FakeS3()
    location = parse_location("s3://bucket/runs/r1")
    publish_dir(src, location, client=fake)

    upload_calls = [c for c in fake.calls if c[0] == "upload_file"]
    assert upload_calls[-1] == ("upload_file", "runs/r1/manifest.json")
    uploaded_keys = {key for _, key in upload_calls}
    assert uploaded_keys == {
        "runs/r1/manifest.json",
        "runs/r1/l2/delivered.fits",
        "runs/r1/exec/a0.json",
    }
    assert fake._objects[("bucket", "runs/r1/manifest.json")] == b'{"a": 1}'


def test_publish_dir_s3_no_files_uploads_nothing(tmp_path):
    src = tmp_path / "outputs"
    src.mkdir()
    fake = FakeS3()
    location = parse_location("s3://bucket/runs/r1")
    publish_dir(src, location, client=fake)
    assert [c for c in fake.calls if c[0] == "upload_file"] == []
