"""``job local`` and ``job run``: run a job's stage in this process, then publish it.

Contracts: ``job local`` records a job, runs a trivial stage and publishes
it, and the live run's job binds the key it expects; ``job run`` on a job
whose attempt 1 wrote files but did not publish publishes attempt 2, once;
``job run`` on a published job stops before touching a file; a stage that
stops with a named reason leaves its code and reason on the failed job's
row, any other exception 70 and no reason; a job run
under a job definition records its image digest and instance type and reports
its peak memory.
"""

from __future__ import annotations

import contextlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rapidpipe import launch
from rapidpipe.cli import main as cli
from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation import create_job, local, run_job
from tests.db.foundation_helpers import DATASET, new_run

KEY = {"dataset": DATASET, "expid": 7}


def trivial_stage(job) -> None:
    """Write one exposure row and its manifest under the attempt's prefix."""
    pq.write_table(pa.Table.from_pylist([{"dataset": job.dataset, "expid": 7,
                                          "dateobs": "2027-10-07T07:18:18", "exptime": 66.4}]),
                   job.path / "exposures.parquet")
    job.write_manifest([("exposures", "exposures.parquet")])


def failing_stage(job) -> None:
    trivial_stage(job)
    raise RuntimeError("interrupted after writing its files")


def exposures_rows(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM exposures WHERE job = %s", (job_id,))
        count = cur.fetchone()[0]
        cur.execute("SELECT state, published_attempt FROM jobs WHERE id = %s", (job_id,))
        state = cur.fetchone()
    conn.commit()
    return count, state


def test_job_local_publishes_and_binds_under_a_live_run(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    run = new_run(conn, live=True)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", tmp_path.as_uri())
    monkeypatch.setattr(cli, "_stage_run", lambda stage: trivial_stage)
    code = cli.main(["job", "local", "--run", str(run), "--stage", "trivial", "--scope", "{}",
                     "--expects", json.dumps([{"product": "exposures", "key": KEY, "binding": None}])])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["published"] and result["group"] is not None
    assert (tmp_path / str(run) / "trivial" / str(result["job"]) / "1" / "manifest.json").is_file()
    with conn.cursor() as cur:
        cur.execute("SELECT job, run FROM exposures_official WHERE expid = 7")
        assert cur.fetchall() == [(result["job"], run)]
    conn.commit()


def test_job_run_publishes_attempt_two_once_after_an_unpublished_attempt_one(connect, tmp_path):
    conn = connect()
    run = new_run(conn, live=True)
    job = create_job(conn, run, "trivial", {}, expects=[{"product": "exposures", "key": KEY,
                                                         "binding": None}])
    factory = lambda: contextlib.closing(connect())  # noqa: E731
    with pytest.raises(RuntimeError):
        run_job(factory, job, tmp_path.as_uri(), failing_stage)
    assert exposures_rows(conn, job) == (0, ("failed", None))
    result = run_job(factory, job, tmp_path.as_uri(), trivial_stage)
    assert (result["attempt"], result["published"]) == (2, True)
    assert exposures_rows(conn, job) == (1, ("published", 2))
    assert (tmp_path / str(run) / "trivial" / str(job) / "1" / "exposures.parquet").is_file()


def test_a_failed_job_submitted_again_is_launched_until_its_container_starts(connect, tmp_path,
                                                                              monkeypatch):
    class Batch:
        def submit_job(self, **request):
            return {"jobId": "batch-2"}

    conn = connect()
    job = create_job(conn, new_run(conn, live=True), "trivial", {})
    with pytest.raises(RuntimeError):
        run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), failing_stage)
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    launch.submit(job, "difference", job_definition="definition", conn=conn, client=Batch())
    with conn.cursor() as cur:
        cur.execute("SELECT state, batch_job_id FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone() == ("launched", "batch-2")
    conn.commit()


@pytest.mark.parametrize("raised, recorded", [
    (CommandExit(int(ExitCode.INPUT_REJECTED), "refpsf: 7 stars, 10 needed"),
     (int(ExitCode.INPUT_REJECTED), "refpsf: 7 stars, 10 needed")),
    (RuntimeError("unexpected"), (int(ExitCode.STAGE_ERROR), None)),
])
def test_a_failed_job_row_records_a_named_reason_and_its_code(connect, tmp_path, raised, recorded):
    conn = connect()
    job = create_job(conn, new_run(conn, live=True), "trivial", {})

    def stopping_stage(job):
        raise raised

    with pytest.raises(type(raised)):
        run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), stopping_stage)
    with conn.cursor() as cur:
        cur.execute("SELECT state, exit_code, refusal FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone() == ("failed", *recorded)
    conn.commit()


def test_job_run_on_a_published_job_stops_before_touching_a_file(connect, tmp_path):
    conn = connect()
    run = new_run(conn, live=True)
    job = create_job(conn, run, "trivial", {})
    factory = lambda: contextlib.closing(connect())  # noqa: E731
    run_job(factory, job, tmp_path.as_uri(), trivial_stage)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    def must_not_run(job):
        raise AssertionError("the stage ran for a published job")

    result = run_job(factory, job, tmp_path.as_uri(), must_not_run)
    assert (result["published"], result["attempt"]) == (False, 1)
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert exposures_rows(conn, job) == (1, ("published", 1))


def test_job_run_records_the_image_digest_and_instance_type_and_reports_peak_memory(
        connect, tmp_path, monkeypatch):
    conn = connect()
    run = new_run(conn)
    job = create_job(conn, run, "trivial", {})
    monkeypatch.setenv("RAPID_IMAGE_DIGEST", "sha256:" + "ab" * 32)
    monkeypatch.setattr(local, "instance_type", lambda: "m6a.4xlarge")
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), trivial_stage)
    assert result["published"] and result["peak_rss_kib"]["self"] > 0
    with conn.cursor() as cur:
        cur.execute("SELECT image_digest, instance_type FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone() == ("sha256:" + "ab" * 32, "m6a.4xlarge")
    conn.commit()
