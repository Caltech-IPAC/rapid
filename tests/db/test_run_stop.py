"""Stopping a run and cleaning up after it (``rapidpipe.foundation.stop``), against the
foundation database, a faked Batch client and stand-in storage (a temporary directory for
``file://``, the in-memory bucket for ``s3://``).

Contracts: a stop records who, when and why, once, refuses the jobs never submitted and asks
Batch to end every job it has not ended; a stopped run takes no job on any path, nothing
recorded or sent; a submission in flight when the stop is called is waited for, ended by it,
and its container does no work; cleanup refuses a run not stopped, a run with a job not shown
ended, and an outputs root the run did not write to; its list holds no published or official
product; a product published between the listing and the removal survives; the removal takes
the list and nothing else.
"""

from __future__ import annotations

import contextlib
import json
import threading

import pytest

from rapidpipe import launch
from rapidpipe.foundation import Refused, Stopped, create_job, local, publish, run_job
from rapidpipe.foundation import prefix as prefix_module
from rapidpipe.foundation import stop as run_stop
from tests.db.foundation_helpers import new_run, wait_until_blocked
from tests.db.test_job_local import KEY, failing_stage, trivial_stage
from tests.unit.fakes3 import FakeS3

EXPECTS = [{"product": "exposures", "key": KEY, "binding": None}]


class Batch:
    """A fake Batch client: ``statuses`` is ``{batch id: status}``; a submission gets the next
    ``batch-<n>`` as ``RUNNABLE``, after ``release`` is set; terminations are recorded."""

    def __init__(self, statuses: dict | None = None):
        self.statuses, self.submitted, self.terminated = dict(statuses or {}), [], []
        self.requested, self.release = threading.Event(), threading.Event()
        self.release.set()

    def submit_job(self, **request):
        self.requested.set()
        assert self.release.wait(10)
        self.submitted.append(request)
        self.statuses[f"batch-{len(self.submitted)}"] = "RUNNABLE"
        return {"jobId": f"batch-{len(self.submitted)}"}

    def describe_jobs(self, jobs):
        return {"jobs": [{"jobId": j, "status": self.statuses[j], "statusReason": "as the fake says",
                          "jobName": f"rapid-trivial-{j}", "jobQueue": "arn:aws:batch:r:1:job-queue/q"}
                         for j in jobs if j in self.statuses]}

    def terminate_job(self, jobId, reason):
        self.terminated.append(jobId)


def no_batch():
    raise AssertionError("Batch was asked, and nothing of this run is on Batch")


@pytest.fixture()
def factory(connect):
    """``connect()`` as the foundation takes it: a context manager yielding a connection."""
    return lambda: contextlib.closing(connect())


@pytest.fixture(autouse=True)
def off_ec2(monkeypatch):
    monkeypatch.setattr(local, "instance_type", lambda timeout=1.0, item="instance-type": None)


def on_batch(conn, job: int, batch_id: str, state: str = "running", **entry) -> None:
    """Give ``job`` a Batch submission, as ``launch.submit`` and the container record one."""
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = %s, batch_job_id = %s, attempts = jsonb_build_object(%s::text,"
                    " %s::jsonb) WHERE id = %s", (state, batch_id, batch_id, json.dumps(entry), job))
    conn.commit()


def rows(conn, sql: str, *params) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        found = cur.fetchall()
    conn.commit()
    return found


def test_a_stop_is_recorded_once_refuses_unsubmitted_jobs_and_asks_batch_to_end_what_has_not_ended(connect):
    conn = connect()
    run = new_run(conn)
    unsubmitted, running, queued, died = (create_job(conn, run, "trivial", {}) for _ in range(4))
    on_batch(conn, running, "b-running")
    on_batch(conn, queued, "b-queued", state="launched")
    on_batch(conn, died, "b-died")
    batch = Batch({"b-running": "RUNNING", "b-queued": "RUNNABLE", "b-died": "FAILED"})

    first = run_stop.stop(conn, run, "tester", "wrong settings", lambda: batch)
    assert (first["stopped_by"], first["stopped_why"], first["already"]) == ("tester", "wrong settings", False)
    assert first["refused"] == [unsubmitted]
    assert [(t["job"], t["batch_job"], t["status"]) for t in first["terminated"]] == [
        (running, "b-running", "RUNNING"), (queued, "b-queued", "RUNNABLE")]
    assert batch.terminated == ["b-running", "b-queued"]            # not the one Batch had ended
    (state, refusal), = rows(conn, "SELECT state, refusal FROM jobs WHERE id = %s", unsubmitted)
    assert state == "failed" and refusal.startswith(f"Stopped: run {run} was stopped by tester")

    again = run_stop.stop(conn, run, "someone else", "another reason", lambda: batch)
    assert (again["stopped"], again["stopped_by"], again["stopped_why"], again["already"]) == (
        first["stopped"], "tester", "wrong settings", True)


def test_a_stopped_run_takes_no_job_and_sends_nothing_to_batch(connect, factory, tmp_path, monkeypatch):
    conn = connect()
    run = new_run(conn)
    earlier = create_job(conn, run, "trivial", {})
    with pytest.raises(RuntimeError):                               # ended, and owed a new attempt
        run_job(factory, earlier, tmp_path.as_uri(), failing_stage)
    run_stop.stop(conn, run, "tester", "enough", no_batch)
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    batch, ran = Batch(), []

    with pytest.raises(Stopped, match="was stopped by tester .* \\(enough\\)"):
        create_job(conn, run, "trivial", {})                        # the loop, job local, job submit
    with pytest.raises(Stopped):
        launch.submit(earlier, "trivial", job_definition="d", conn=conn, client=batch)   # job submit --job, a rerun
    with pytest.raises(Stopped):
        run_job(factory, earlier, tmp_path.as_uri(), ran.append)    # job run, a container starting

    assert batch.submitted == [] and ran == []
    assert rows(conn, "SELECT id, state FROM jobs WHERE run = %s", run) == [(earlier, "failed")]
    assert sorted(p.name for p in (tmp_path / str(run) / "trivial" / str(earlier)).iterdir()) == ["1"]


def test_a_submission_in_flight_when_stop_is_called_is_ended_by_it_and_its_container_does_no_work(
        connect, factory, tmp_path, monkeypatch):
    conn = connect()
    run = new_run(conn)
    job = create_job(conn, run, "trivial", {})
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    batch = Batch()
    batch.release.clear()                                           # the request to Batch is in flight
    submitting = threading.Thread(target=launch.submit, args=(job, "trivial"),
                                  kwargs={"job_definition": "d", "conn": connect(), "client": batch})
    submitting.start()
    assert batch.requested.wait(10)

    stopper, result = connect(), {}
    stopping = threading.Thread(
        target=lambda: result.update(run_stop.stop(stopper, run, "tester", "enough", lambda: batch)))
    stopping.start()
    wait_until_blocked(connect(), stopper.info.backend_pid)         # the stop waits for the submission
    batch.release.set()
    submitting.join(10)
    stopping.join(10)

    assert [(t["job"], t["batch_job"]) for t in result["terminated"]] == [(job, "batch-1")]
    assert batch.terminated == ["batch-1"] and result["refused"] == []
    # Batch starts the container before the termination lands: it does not start work.
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "batch-1")
    ran = []
    with pytest.raises(Stopped):
        run_job(factory, job, tmp_path.as_uri(), ran.append)
    assert ran == [] and list(tmp_path.iterdir()) == []
    (state, refusal, outcome), = rows(
        conn, "SELECT state, refusal, attempts -> 'batch-1' ->> 'outcome' FROM jobs WHERE id = %s", job)
    assert (state, outcome) == ("failed", "failed") and refusal.startswith("Stopped: run")


def test_cleanup_refuses_a_run_that_is_not_stopped(connect, factory, tmp_path):
    run = new_run(connect())
    with pytest.raises(Refused, match=f"run {run} is not stopped: 'rapidpipe run stop --run {run}"):
        run_stop.cleanup(factory, run, tmp_path.as_uri(), no_batch, delete=True)


@pytest.mark.parametrize("case, expected", [
    ("active on Batch", "its Batch job b-1 is RUNNING: 'rapidpipe run stop"),
    ("unknown to Batch", "its Batch job b-1 has no recorded end and Batch does not describe it"),
    ("running here", "its row is still 'running' with no Batch job"),
])
def test_cleanup_refuses_a_run_with_a_job_not_shown_ended_naming_it_and_removes_nothing(
        connect, factory, tmp_path, case, expected):
    conn = connect()
    run = new_run(conn)
    failed, other = create_job(conn, run, "trivial", {}), create_job(conn, run, "trivial", {})
    with pytest.raises(RuntimeError):
        run_job(factory, failed, tmp_path.as_uri(), failing_stage)  # an ended job with files to remove
    if case == "running here":
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET state = 'running' WHERE id = %s", (other,))
        conn.commit()
    else:
        on_batch(conn, other, "b-1", state="failed")                # Batch's own retry may follow a failure
    batch = Batch({"b-1": "RUNNING"} if case == "active on Batch" else {})
    with conn.cursor() as cur:                                      # stopped, with the job as the case has it
        cur.execute("UPDATE runs SET stopped = now(), stopped_by = 't', stopped_why = 'w' WHERE id = %s", (run,))
    conn.commit()

    with pytest.raises(Refused) as refusal:
        run_stop.cleanup(factory, run, tmp_path.as_uri(), lambda: batch, delete=True)
    assert f"job {other} (trivial): {expected}" in str(refusal.value)
    assert (tmp_path / str(run) / "trivial" / str(failed) / "1" / "exposures.parquet").is_file()


def stopped_run_with_products(conn, factory, root: str) -> dict:
    """A stopped run under ``root``: an official job, a published job not bound, a published job
    with a failed attempt before it, and a failed job; return their ids. Under ``file://`` each failed
    attempt leaves its two files (the stage writes in place); under ``s3://`` it uploads none."""
    run = new_run(conn, live=True)
    official = create_job(conn, run, "trivial", {}, expects=EXPECTS)
    assert run_job(factory, official, root, trivial_stage)["group"] is not None
    published = create_job(conn, run, "trivial", {})
    run_job(factory, published, root, trivial_stage)
    retried = create_job(conn, run, "trivial", {})
    with pytest.raises(RuntimeError):
        run_job(factory, retried, root, failing_stage)
    run_job(factory, retried, root, trivial_stage)
    failed = create_job(conn, run, "trivial", {})
    with pytest.raises(RuntimeError):
        run_job(factory, failed, root, failing_stage)
    run_stop.stop(conn, run, "tester", "enough", no_batch)
    return {"run": run, "official": official, "published": published, "retried": retried, "failed": failed}


def test_the_list_holds_no_published_or_official_product_and_counts_what_it_does_not_recognise(
        connect, factory, tmp_path):
    ids = stopped_run_with_products(connect(), factory, tmp_path.as_uri())
    run = ids["run"]
    (tmp_path / str(run) / "qa" / "note").mkdir(parents=True)
    (tmp_path / str(run) / "qa" / "note" / "table.md").write_text("evidence someone kept")
    (tmp_path / str(run) / "trivial" / "999999" / "1").mkdir(parents=True)      # no job of this run
    (tmp_path / str(run) / "trivial" / "999999" / "1" / "x.parquet").write_text("another database's")
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    listed = run_stop.cleanup(factory, run, tmp_path.as_uri(), no_batch)

    assert [(a["job"], a["attempt"], a["state"], a["files"]) for a in listed["attempts"]] == [
        (ids["retried"], 1, "published", 2), (ids["failed"], 1, "failed", 2)]
    assert listed["files"] == 4 and "deleted" not in listed
    assert listed["protected"] == {"attempts": 3, "files": 6}       # each a parquet file and its manifest
    assert listed["unrecognised"] == {"qa": 1, "trivial": 1}
    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == before       # a listing removes nothing


def test_a_product_published_between_the_listing_and_the_removal_survives(connect, factory, tmp_path,
                                                                          monkeypatch):
    conn = connect()
    ids = stopped_run_with_products(conn, factory, tmp_path.as_uri())
    run, failed = ids["run"], ids["failed"]
    attempt = tmp_path / str(run) / "trivial" / str(failed) / "1"        # its files and manifest are whole
    listing = run_stop._list

    def list_then_publish(prefix, client=None):
        found = listing(prefix, client)
        assert publish(conn, failed, 1, attempt.as_uri() + "/")["published"]
        return found

    monkeypatch.setattr(run_stop, "_list", list_then_publish)
    result = run_stop.cleanup(factory, run, tmp_path.as_uri(), no_batch, delete=True)

    assert result["kept"] == [{"job": failed, "attempt": 1, "files": 2,
                               "why": "it was published since the listing"}]
    assert sorted(p.name for p in attempt.iterdir()) == ["exposures.parquet", "manifest.json"]
    assert result["deleted"] == 2                                                # the retried job's first attempt
    assert not (tmp_path / str(run) / "trivial" / str(ids["retried"]) / "1").exists()


def test_the_removal_takes_the_list_and_nothing_else(connect, factory, tmp_path, monkeypatch):
    s3 = FakeS3(page_size=3)
    monkeypatch.setattr("rapidpipe.products.storage.s3_client", lambda: s3)
    monkeypatch.setattr(prefix_module, "_cache", tmp_path / "cache")
    root = "s3://products/foundation"
    ids = stopped_run_with_products(connect(), factory, root)
    run = ids["run"]
    left = {f"foundation/{run}/trivial/{ids['failed']}/1/exposures.parquet",       # uploaded, never published
            f"foundation/{run}/trivial/{ids['failed']}/1/manifest.json",
            f"foundation/{run}/trivial/{ids['retried']}/2/exposures.parquet"}       # beside its published attempt 1
    for key in left:
        s3.seed("products", key, b"left behind")
    s3.seed("products", f"foundation/{run}/qa/table.md", b"evidence someone kept")
    s3.seed("products", f"foundation/{run}0/trivial/{ids['failed']}/1/x.parquet", b"another run's")
    before = {key for _, key in s3._objects}

    listed = run_stop.cleanup(factory, run, root, no_batch)
    result = run_stop.cleanup(factory, run, root, no_batch, delete=True)

    assert {a["prefix"] for a in listed["attempts"]} == {f"s3://products/{key.rsplit('/', 1)[0]}/" for key in left}
    assert before - {key for _, key in s3._objects} == left
    assert (result["deleted"], result["kept"], result["failed"]) == (3, [], [])
    assert run_stop.cleanup(factory, run, root, no_batch)["files"] == 0


def test_cleanup_refuses_an_outputs_root_the_runs_published_jobs_did_not_write_to(connect, factory, tmp_path):
    ids = stopped_run_with_products(connect(), factory, (tmp_path / "written").as_uri())
    elsewhere = tmp_path / "elsewhere" / str(ids["run"]) / "trivial" / str(ids["failed"]) / "1"
    elsewhere.mkdir(parents=True)
    (elsewhere / "x.parquet").write_text("not this run's")
    with pytest.raises(Refused, match="is not the root the run wrote to"):
        run_stop.cleanup(factory, ids["run"], (tmp_path / "elsewhere").as_uri(), no_batch, delete=True)
    assert (elsewhere / "x.parquet").is_file()
