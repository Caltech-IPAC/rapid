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
    publish_dir,
)

from .fakes3 import FakeS3


def test_publish_dir_local_noop_when_same_path(tmp_path):
    src = tmp_path / "outputs"
    src.mkdir()
    location = Location(kind="local", path=src)
    publish_dir(src, location)  # must not raise


def test_publish_dir_local_different_path_raises(tmp_path):
    src = tmp_path / "outputs"
    src.mkdir()
    other = tmp_path / "elsewhere"
    location = Location(kind="local", path=other)
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
    location = Location(kind="s3", bucket="bucket", prefix="runs/r1")
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
    location = Location(kind="s3", bucket="bucket", prefix="runs/r1")
    publish_dir(src, location, client=fake)
    assert [c for c in fake.calls if c[0] == "upload_file"] == []
