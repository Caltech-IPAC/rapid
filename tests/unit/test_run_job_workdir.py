"""``run_job``'s local working directory under an ``s3://`` outputs root.

Contracts: the directory is removed once its upload has succeeded; a stage
that raises leaves it where it was, for a person; the uploaded manifest's
``metrics`` carry ``peak_rss_kib`` and the container cgroup's memory
peaks, beside the stage's own; the anon and file peaks are the largest
``memory.stat`` values sampled while the stage ran, and a peak whose file
is absent is absent.
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
    monkeypatch.setattr(local, "CGROUP", tmp_path / "no-cgroup")
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


def write_cgroup(root: Path, anon: int, file: int, peak: int | None = None) -> None:
    root.mkdir(exist_ok=True)
    (root / "memory.stat").write_text(f"anon {anon}\nfile {file}\nkernel 7\n")
    if peak is not None:
        (root / "memory.peak").write_text(f"{peak}\n")


def test_the_manifest_carries_the_largest_cgroup_values_seen_and_memory_peak(workdirs, monkeypatch,
                                                                            tmp_path):
    import json

    root = tmp_path / "cgroup"
    write_cgroup(root, anon=600, file=20)
    monkeypatch.setattr(local, "CGROUP", root)
    uploaded = {}
    monkeypatch.setattr(local, "put", lambda directory, prefix: uploaded.update(
        json.loads((Path(directory) / "manifest.json").read_text())))

    def shrink_anon_grow_file(job):
        write_a_file(job)
        write_cgroup(root, anon=40, file=300, peak=900)
        job.write_manifest([("t", "out.parquet")])

    local.run_job(connect, 5, ROOT, shrink_anon_grow_file)
    assert {k: v for k, v in uploaded["metrics"].items() if k.startswith("cgroup")} == {
        "cgroup_anon_peak_bytes": 600, "cgroup_file_peak_bytes": 300, "cgroup_memory_peak_bytes": 900}


def test_the_sampler_sees_a_peak_that_rises_and_falls_inside_the_stage(tmp_path):
    import time

    write_cgroup(tmp_path, anon=10, file=1)
    with local.CgroupMemory(root=tmp_path, interval=0.01) as memory:
        write_cgroup(tmp_path, anon=700, file=1)
        deadline = time.monotonic() + 5
        while memory.anon != 700 and time.monotonic() < deadline:
            time.sleep(0.01)
        write_cgroup(tmp_path, anon=10, file=1)
    assert memory.peaks()["cgroup_anon_peak_bytes"] == 700


def test_no_cgroup_files_record_no_cgroup_peaks(tmp_path):
    with local.CgroupMemory(root=tmp_path / "absent") as memory:
        pass
    assert memory.peaks() == {}
