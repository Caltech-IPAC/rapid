"""Tests for rapidpipe.runs.cleanup that need no database.

The S3 half of scratch-run deletion -- listing every object version and
delete marker under a prefix, page by page, and removing them in batches
of at most 1000 -- and the scratch-bucket default. The database half
(fence, science rows, tombstones, resume, expiry) is in
tests/db/test_run_lifecycle.py.
"""

from __future__ import annotations

import pytest

from rapidpipe.runs import cleanup
from tests.unit.fakes3 import FakeVersionedS3


def test_delete_prefix_removes_every_version_and_delete_marker_under_the_prefix():
    s3 = FakeVersionedS3()
    s3.seed("scratch", "p/runs/R/admit/u/A/manifest.json", versions=3)
    s3.seed("scratch", "p/runs/R/admit/u/A/image.fits", versions=1, delete_marker=True)
    s3.seed("scratch", "p/runs/R/admit/u/A2/manifest.json", versions=1)  # sibling prefix
    s3.seed("scratch", "p/runs/OTHER/admit/u/B/manifest.json", versions=2)

    objects, versions = cleanup._delete_prefix(s3, "scratch", "p/runs/R/admit/u/A/")

    assert (objects, versions) == (2, 5)
    assert s3.remaining("scratch", "p/runs/R/admit/u/A/") == []
    assert len(s3.remaining("scratch", "p/runs/R/admit/u/A2/")) == 1
    assert len(s3.remaining("scratch", "p/runs/OTHER/")) == 2
    for name, kwargs in s3.calls:
        if name == "delete_objects":
            assert kwargs["Delete"]["Quiet"] is True


def test_delete_prefix_paginates_listing_and_batches_deletes_at_1000():
    s3 = FakeVersionedS3(page_size=400)
    for i in range(1300):
        s3.seed("scratch", f"p/runs/R/s/u/A/f{i:05d}")

    objects, versions = cleanup._delete_prefix(s3, "scratch", "p/runs/R/s/u/A/")

    assert (objects, versions) == (1300, 1300)
    assert s3.remaining("scratch") == []
    listings = [kw for name, kw in s3.calls if name == "list_object_versions"]
    deletes = [kw for name, kw in s3.calls if name == "delete_objects"]
    assert len(listings) == 4  # 400 + 400 + 400 + 100
    assert [len(kw["Delete"]["Objects"]) for kw in deletes] == [1000, 300]


def test_delete_prefix_raises_cleanup_failed_on_per_object_errors():
    s3 = FakeVersionedS3()
    s3.seed("scratch", "p/runs/R/s/u/A/locked")
    s3.fail_keys.add("p/runs/R/s/u/A/locked")
    with pytest.raises(cleanup.CleanupFailed, match="AccessDenied"):
        cleanup._delete_prefix(s3, "scratch", "p/runs/R/s/u/A/")


def test_default_scratch_bucket_order(monkeypatch):
    for name in ("RAPIDPIPE_SCRATCH_BUCKET", "RAPIDPIPE_OUTPUTS_ROOT_SCRATCH",
                 "RAPIDPIPE_OUTPUTS_ROOT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(cleanup.DeletionRefused):
        cleanup._default_scratch_bucket()

    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://plain-bucket/prefix")
    assert cleanup._default_scratch_bucket() == "plain-bucket"
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", "s3://scratch-bucket/prefix")
    assert cleanup._default_scratch_bucket() == "scratch-bucket"
    monkeypatch.setenv("RAPIDPIPE_SCRATCH_BUCKET", "explicit-bucket")
    assert cleanup._default_scratch_bucket() == "explicit-bucket"


def test_default_scratch_bucket_refuses_a_local_root(monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_SCRATCH_BUCKET", raising=False)
    monkeypatch.delenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", raising=False)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "/tmp/local-root")
    with pytest.raises(cleanup.DeletionRefused):
        cleanup._default_scratch_bucket()


def test_science_tables_are_in_foreign_key_safe_order():
    order = {table: i for i, table in enumerate(cleanup.SCIENCE_TABLES)}
    # (referencing table, referenced table), from 20260921-01-baseline.sql.
    for child, parent in [
        ("sources", "diffimages"), ("diffimmeta", "diffimages"),
        ("diffimages", "l2files"), ("diffimages", "refimages"),
        ("l2filemeta", "l2files"),
    ]:
        assert order[child] < order[parent], (child, parent)


class _AttemptsConn:
    """Stands in for the one query ``_s3_prefixes`` makes: the run's
    ``s3://`` attempt output locations."""

    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        rows = self.rows

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *_a, **_k):
                pass

            def fetchall(self):
                return list(rows)

        return _Cur()


def _scratch_env(monkeypatch, root="s3://scratch/p"):
    monkeypatch.delenv("RAPIDPIPE_SCRATCH_BUCKET", raising=False)
    monkeypatch.delenv("RAPIDPIPE_OUTPUTS_ROOT", raising=False)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", root)


def test_delete_removes_the_run_composed_input_sets_with_it(monkeypatch):
    _scratch_env(monkeypatch)
    s3 = FakeVersionedS3()
    s3.seed("scratch", "p/runs/R/admit/u/A/manifest.json")
    s3.seed("scratch", "p/runs/R/inputs/difference/u/manifest.json", versions=2)
    s3.seed("scratch", "p/runs/R/inputs/difference/u/l2/science.fits")
    s3.seed("scratch", "p/runs/OTHER/inputs/difference/u/manifest.json")

    conn = _AttemptsConn([("A", "s3://scratch/p/runs/R/admit/u/A")])
    prefixes = cleanup._s3_prefixes(conn, "R", None)
    assert prefixes == [("scratch", "p/runs/R/inputs/"), ("scratch", "p/runs/R/admit/u/A/")]
    for bucket, prefix in prefixes:
        cleanup._delete_prefix(s3, bucket, prefix)

    assert s3.remaining("scratch", "p/runs/R/") == []
    assert len(s3.remaining("scratch", "p/runs/OTHER/")) == 1


def test_inputs_prefix_is_included_for_a_run_with_no_s3_attempts(monkeypatch):
    _scratch_env(monkeypatch, root="s3://scratch")
    assert cleanup._s3_prefixes(_AttemptsConn([]), "R", None) == [("scratch", "runs/R/inputs/")]


def test_inputs_prefix_skipped_without_an_s3_scratch_root_or_in_another_bucket(monkeypatch):
    for name in ("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", "RAPIDPIPE_OUTPUTS_ROOT"):
        monkeypatch.delenv(name, raising=False)
    assert cleanup._s3_prefixes(_AttemptsConn([]), "R", None) == []
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", "/tmp/local-root")
    assert cleanup._s3_prefixes(_AttemptsConn([]), "R", "scratch") == []
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", "s3://elsewhere/p")
    assert cleanup._s3_prefixes(_AttemptsConn([]), "R", "scratch") == []
