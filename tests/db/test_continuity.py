"""The pipeline through a database closure, a lost connection and a killed submitter (continuity design).

Contracts, each against a real PostgreSQL whose backend is ended from a
second connection (``pg_terminate_backend``) or by a test hook:

- a statement cut inside a rollback handler raises the cut's
  ``OperationalError``, which exits 75, not the rollback's ``InterfaceError``;
- a cut during a stage's read fails the attempt with 75, and the next attempt
  computes again and publishes;
- a cut during publication, a commit whose acknowledgement is lost, and a cut
  during the end's record are each published again on a new connection: one
  computation, one published result, the end recorded;
- a loop killed after Batch accepted a first submission leaves no row, the
  next pass plans the key again, and a container for the orphan exits 64
  without computing; one killed after Batch accepted a rerun leaves the row
  ``failed`` with its old Batch id, and the next pass reruns it; no pending
  row is left without a Batch id;
- ``loop --serve`` records the loop and runs no pass, a run has one open
  loop, and ``serve`` closes each loop of its job definition with the verb's
  exit code.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rapidpipe.cli import main as cli
from rapidpipe.db.connection import transaction
from rapidpipe.foundation import create_job, create_run, local

publisher = importlib.import_module("rapidpipe.foundation.publish")   # the package's `publish` is the function

REPO = Path(__file__).resolve().parents[2]
SETTINGS = {"intake": {"source_root": "s3://bucket/l2/", "unsigned": True},
            "reference": {"selection": {"min_frames": 2}},
            "packet": {"gaia_match": False, "ned_match": False, "lvs_match": False, "ss_match": False}}
STEM = "r0034201001001004001_0001_wfi08_f146"


def cut(conn) -> None:
    """End ``conn``'s backend from a second connection and wait until it has gone."""
    with contextlib.closing(psycopg2.connect("")) as other, other.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s, 5000)", (conn.get_backend_pid(),))
        other.commit()


def cut_once(monkeypatch, module, name: str, when=lambda *a: True) -> None:
    """Make ``module.name``'s first call that ``when`` accepts end its connection's backend first."""
    real, done = getattr(module, name), []

    def cutting(conn_or_cur, *args, **kwargs):
        if not done and when(*args):
            done.append(True)
            cut(getattr(conn_or_cur, "connection", conn_or_cur))
        return real(conn_or_cur, *args, **kwargs)

    monkeypatch.setattr(module, name, cutting)


def counting_stage(calls: list):
    def stage(job) -> None:
        calls.append(job.attempt)
        pq.write_table(pa.Table.from_pylist([{"dataset": job.dataset, "expid": 7,
                                              "dateobs": "2027-10-07T07:18:18", "exptime": 66.4}]),
                       job.path / "exposures.parquet")
        job.write_manifest([("exposures", "exposures.parquet")])
    return stage


def new_run(conn) -> int:
    return create_run(conn, "tester", "continuity", "abc123", SETTINGS, "S", "D")


def one(conn, sql: str, params=()) -> tuple:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.commit()
    return row


@pytest.mark.parametrize("operation", ["transaction", "create_job"])
def test_a_statement_cut_inside_a_rollback_handler_raises_the_cut_and_exits_75(connect, operation):
    run = new_run(connect())
    with pytest.raises(psycopg2.Error) as raised:
        if operation == "transaction":
            with transaction() as conn:
                cut(conn)
                conn.cursor().execute("SELECT 1")
        else:
            conn = connect()
            cut(conn)
            create_job(conn, run, "trivial", {})
    assert isinstance(raised.value, psycopg2.OperationalError)
    assert cli.exit_code(raised.value) == 75


def test_a_cut_during_a_stage_read_exits_75_and_the_next_attempt_computes_again(
        connect, tmp_path, monkeypatch):
    conn = connect()
    job = create_job(conn, new_run(conn), "trivial", {})
    calls, cuts = [], []

    def reading_stage(context) -> None:
        with context.connect() as read:
            if not cuts:
                cuts.append(True)
                cut(read)
            read.cursor().execute("SELECT 1")
        counting_stage(calls)(context)

    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", tmp_path.as_uri())
    monkeypatch.setattr(cli, "_stage_run", lambda stage: reading_stage)
    assert cli.main(["job", "run", "--job", str(job)]) == 75
    assert one(conn, "SELECT state, exit_code FROM jobs WHERE id = %s", (job,)) == ("failed", 75)
    assert cli.main(["job", "run", "--job", str(job)]) == 0
    assert calls == [2]
    assert one(conn, "SELECT state, published_attempt FROM jobs WHERE id = %s", (job,)) == ("published", 2)


@pytest.mark.parametrize("boundary", ["publication", "acknowledgement", "end record"])
def test_a_lost_connection_while_publishing_publishes_once_without_computing_again(
        connect, tmp_path, monkeypatch, caplog, boundary):
    conn = connect()
    job = create_job(conn, new_run(conn), "trivial", {})
    if boundary == "publication":
        cut_once(monkeypatch, publisher, "_insert")
    elif boundary == "acknowledgement":
        monkeypatch.setenv(publisher.TEST_LOSE_ACK, "1")
    else:
        cut_once(monkeypatch, local, "_record", lambda job_id, assignments, *a: "exit_code = 0" in assignments)
    calls = []
    factory = lambda: contextlib.closing(connect())  # noqa: E731
    result = local.run_job(factory, job, tmp_path.as_uri(), counting_stage(calls), retry_on=cli.LOST_CONNECTION)
    assert result["attempt"] == 1 and calls == [1]
    assert one(conn, "SELECT count(*) FROM exposures WHERE job = %s", (job,)) == (1,)
    assert one(conn, "SELECT state, published_attempt, exit_code, ended IS NOT NULL FROM jobs WHERE id = %s",
               (job,)) == ("published", 1, 0, True)
    retries = [r.getMessage() for r in caplog.records if "publish retry" in r.getMessage()]
    assert len(retries) == 1 and retries[0].startswith(f"job {job}: publish retry 1 after ")


#: A loop pass in a process of its own (a kill ends it): S3 lists :data:`STEM`, Batch accepts every
#: submission and logs it to BATCH_LOG, and describes the Batch ids BATCH_FAILED names as a dead host.
DRIVER = f"""
import json, os, sys
from rapidpipe import launch
from rapidpipe.cli import main as cli

class S3:
    def list_objects_v2(self, **kw):
        return {{"Contents": [{{"Key": "l2/{STEM}_cal.asdf"}}]}} if kw["StartAfter"] < "l2/{STEM}" else {{}}
    def get_paginator(self, name):
        return self
    def paginate(self, **kw):
        return [{{"Contents": [{{"Key": "l2/{STEM}_cal.asdf"}}]}}]

class Batch:
    def submit_job(self, **request):
        with open(os.environ["BATCH_LOG"], "a") as log:
            log.write(json.dumps(request) + "\\n")
        return {{"jobId": request["jobName"] + "-" + str(sum(1 for _ in open(os.environ["BATCH_LOG"])))}}
    def describe_jobs(self, jobs):
        dead = os.environ.get("BATCH_FAILED", "").split(",")
        return {{"jobs": [{{"jobId": j, "status": "FAILED", "statusReason": "Host EC2 (instance i-1) terminated.",
                          "jobName": "rapid-psf", "jobQueue": "q"}} for j in jobs if j in dead]}}

cli.storage.s3_client = lambda unsigned=False: S3()
launch.batch_client = lambda: Batch()
sys.exit(cli.main(sys.argv[1:]))
"""


def loop_pass(run: int, log: Path, kill_at: str | None = None, failed: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "BATCH_LOG": str(log), "BATCH_FAILED": failed, "PYTHONPATH": str(REPO),
           "RAPIDPIPE_BATCH_JOB_QUEUE": "q", "RAPIDPIPE_OUTPUTS_ROOT": "s3://products/foundation"}
    env.pop("RAPIDPIPE_TEST_KILL_AT", None)
    if kill_at:
        env["RAPIDPIPE_TEST_KILL_AT"] = kill_at
    return subprocess.run([sys.executable, "-c", DRIVER, "loop", "--run", str(run), "--filters", "F146",
                           "--scas", "8", "--pointings", "all", "--job-definition", "jd", "--once"],
                          env=env, capture_output=True, text=True, cwd=REPO)


def submitted(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def stranded(conn, run: int) -> int:
    return one(conn, "SELECT count(*) FROM jobs WHERE run = %s AND state IN ('launched', 'running')"
                     " AND batch_job_id IS NULL", (run,))[0]


def test_a_kill_after_batch_accepts_a_first_submission_leaves_no_row_and_its_container_exits_64(
        connect, tmp_path, monkeypatch):
    conn = connect()
    run, log = new_run(conn), tmp_path / "batch.log"
    assert loop_pass(run, log, kill_at="submit-accepted").returncode == 137
    orphan = int(submitted(log)[0]["containerOverrides"]["command"][-1])
    assert one(conn, "SELECT count(*) FROM jobs WHERE run = %s", (run,)) == (0,)
    assert loop_pass(run, log).returncode == 0
    planned = one(conn, "SELECT stage, scope ->> 'file', batch_job_id IS NOT NULL FROM jobs WHERE run = %s", (run,))
    assert planned == ("psf", STEM, True) and stranded(conn, run) == 0
    calls = []
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", tmp_path.as_uri())
    monkeypatch.setattr(cli, "_stage_run", lambda stage: counting_stage(calls))
    assert cli.main(["job", "run", "--job", str(orphan)]) == 64 and calls == []


def test_a_kill_after_batch_accepts_a_rerun_leaves_the_row_failed_with_its_old_id_and_the_next_pass_reruns(
        connect, tmp_path):
    conn = connect()
    run, log = new_run(conn), tmp_path / "batch.log"
    job = create_job(conn, run, "psf", {"dataset": "D", "file": STEM})
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'running', batch_job_id = 'b-old' WHERE id = %s", (job,))
    conn.commit()
    assert loop_pass(run, log, kill_at="rerun-accepted", failed="b-old").returncode == 137
    assert one(conn, "SELECT state, batch_job_id FROM jobs WHERE id = %s", (job,)) == ("failed", "b-old")
    assert loop_pass(run, log, failed="b-old").returncode == 0
    reruns = [r["jobName"] for r in submitted(log)]
    assert reruns == [f"rapid-psf-{job}-rerun"] * 2
    state, batch_id = one(conn, "SELECT state, batch_job_id FROM jobs WHERE id = %s", (job,))
    assert state == "launched" and batch_id.startswith(reruns[1]) and batch_id != "b-old"
    assert stranded(conn, run) == 0


class Stop(Exception):
    """Ends ``serve`` at its first sleep."""


def test_loop_serve_records_one_open_loop_a_run_and_serve_closes_each_with_the_verbs_code(
        connect, monkeypatch, capsys):
    conn = connect()
    first, drained, other = new_run(conn), new_run(conn), new_run(conn)
    monkeypatch.setattr(cli.storage, "s3_client", lambda unsigned=False: type(
        "Empty", (), {"list_objects_v2": lambda self, **kw: {}})())
    monkeypatch.setattr(cli.launch, "batch_client", lambda: type(
        "Idle", (), {"describe_jobs": lambda self, jobs: {"jobs": []}})())
    served = ["--filters", "F146", "--scas", "8", "--pointings", "all", "--serve"]
    for run, definition in ((first, "jd"), (drained, "jd"), (other, "elsewhere")):
        assert cli.main(["loop", "--run", str(run), "--job-definition", definition, *served]) == 0
    assert cli.main(["loop", "--run", str(first), "--job-definition", "jd", *served]) == 64
    assert one(conn, "SELECT count(*) FROM jobs") == (0,)              # --serve runs no pass
    with conn.cursor() as cur:                                         # as if its first pass had run
        cur.execute("UPDATE loops SET passed_at = now() WHERE run = %s", (drained,))
    conn.commit()

    def stop(seconds):
        raise Stop

    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(Stop):
        cli._serve(cli._build_parser().parse_args(["serve", "--job-definition", "jd", "--interval", "1"]))
    closed = {run: one(conn, "SELECT exit_code FROM loops WHERE run = %s", (run,))[0]
              for run in (first, drained, other)}
    assert closed == {first: 64, drained: 0, other: None}
