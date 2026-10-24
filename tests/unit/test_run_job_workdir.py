"""``run_job``'s local working directory under an ``s3://`` outputs root.

Contracts: the directory is removed once its upload has succeeded; a stage
that raises leaves it where it was, for a person; the uploaded manifest's
``metrics`` carry ``peak_rss_kib``, beside the stage's own.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from rapidpipe.foundation import local

ROOT = "s3://outputs/foundation"


class Cursor:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=()):
        if "FROM jobs j JOIN runs r" in statement:
            self.rows = [{"state": "launched", "stage": "trivial", "scope": {}, "run": 3,
                          "published_attempt": None, "refusal": None, "settings": {},
                          "survey": "HLWAS", "dataset": "d"}]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def cursor(self, **_):
        return Cursor()

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture()
def workdirs(monkeypatch, tmp_path):
    """The working directories ``run_job`` made, with uploads and publication faked."""
    made: list[Path] = []
    real_mkdtemp = local.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        made.append(Path(real_mkdtemp(dir=tmp_path, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(local.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(local, "attempts", lambda prefix: set())
    monkeypatch.setattr(local, "put", lambda directory, prefix: None)
    monkeypatch.setattr(local, "publish", lambda conn, job, attempt, prefix: {"published": True})
    monkeypatch.setattr(local, "instance_type", lambda: None)
    return made


def connect():
    return contextlib.nullcontext(Connection())


def write_a_file(job):
    (job.path / "out.parquet").write_bytes(b"rows")


def test_the_working_directory_is_removed_after_the_upload(workdirs):
    local.run_job(connect, 5, ROOT, write_a_file)
    (workdir,) = workdirs
    assert workdir.name.startswith("rapidpipe-job-5-1-") and not workdir.exists()


def test_a_failed_stage_leaves_the_working_directory(workdirs):
    def fail(job):
        write_a_file(job)
        raise RuntimeError("the stage failed")

    with pytest.raises(RuntimeError):
        local.run_job(connect, 5, ROOT, fail)
    (workdir,) = workdirs
    assert (workdir / "out.parquet").read_bytes() == b"rows"


def test_the_uploaded_manifest_carries_the_peak_memory_beside_the_stages_metrics(workdirs,
                                                                                monkeypatch):
    import json

    uploaded = {}
    monkeypatch.setattr(local, "put", lambda directory, prefix: uploaded.update(
        json.loads((Path(directory) / "manifest.json").read_text())))
    monkeypatch.setattr(local, "_peak_rss_kib", lambda: {"self": 100, "children": 50})

    def with_metrics(job):
        write_a_file(job)
        job.write_manifest([("t", "out.parquet")], metrics={"n": 3})

    local.run_job(connect, 5, ROOT, with_metrics)
    assert uploaded["metrics"] == {"n": 3, "peak_rss_kib": 150}
