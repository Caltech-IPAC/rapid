"""Behavioural, black-box tests of ``rapidpipe run start``: argv in, exit
code / stdout / stderr and database state out, against a real
PostgreSQL with Batch and S3 faked (see ``tests/cli/test_run_lifecycle.py``'s
module docstring for the shared registration correction and setup this
module reuses).

``run start`` waits for an attempt by polling ``reconcile`` in a loop
this suite does not otherwise control. Rather than driving FakeBatch
from a side thread, :func:`_auto_complete_reconcile` monkeypatches
``rapidpipe.cli.runctl._reconcile`` to give any attempt of the run that
is still unresolved (and not explicitly excluded) a SUCCEEDED status and
a valid manifest at its own output location just before the real
reconcile runs -- standing in for Batch finishing the job between polls,
so a wait loop resolves on its first poll instead of spinning.
"""

from __future__ import annotations

from rapidpipe.cli import runctl

from .conftest import FAKE_BUCKET
from .test_run_lifecycle import _create_run, _kv, _seed_manifest


def _auto_complete_reconcile(monkeypatch, db, fake_batch, fake_s3, *, skip_jobs=frozenset()):
    original = runctl._reconcile

    def wrapper(conn, run_id):
        with db.cursor() as cur:
            cur.execute(
                "SELECT u.stage, u.unit_id, a.id, a.scheduler_job_id, a.output_location "
                "FROM attempts a JOIN units u ON u.id = a.unit "
                "WHERE a.run = %s AND a.disposition IS NULL "
                "AND a.scheduler_job_id IS NOT NULL",
                (run_id,))
            rows = cur.fetchall()
        for stage, unit_id, attempt_id, job_id, output_location in rows:
            if job_id in skip_jobs:
                continue
            fake_batch.set_status(job_id, "SUCCEEDED")
            _seed_manifest(fake_s3, output_location, run_id=run_id, stage=stage,
                            unit_id=unit_id, attempt_id=attempt_id)
        return original(conn, run_id)

    monkeypatch.setattr(runctl, "_reconcile", wrapper)


# ======================================================================
# --no-wait: submit and print the continue command
# ======================================================================

def test_start_no_wait_submits_admit_and_prints_continue(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,register")
    result = cli("run", "start", run_id, "--unit", "U", "--inputs",
                 "admit=s3://in-bucket/in-prefix", "--no-wait")
    assert result.rc == 0, result.err
    assert "attempt=" in result.out and "job=" in result.out and "outputs=" in result.out
    assert "continue:" in result.out
    assert f"run={run_id} state=submitted" in result.out

    with db.cursor() as cur:
        cur.execute("SELECT stage, state FROM units WHERE run = %s", (run_id,))
        assert cur.fetchall() == [("admit", "running")]


# ======================================================================
# The full walk: admit already in flight, completes, register derives
# its unit id and inputs from admit's manifest, completes too.
# ======================================================================

def test_start_continues_after_no_wait_through_register_to_completion(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,register")
    first = cli("run", "start", run_id, "--unit", "U", "--inputs",
               "admit=s3://in-bucket/in-prefix", "--no-wait")
    assert first.rc == 0, first.err
    attempt_id = _kv(first.out, "attempt")
    job_id = _kv(first.out, "job")
    output_location = _kv(first.out, "outputs")

    # admit's job finishes between the two 'run start' invocations, with
    # a valid manifest at its printed output location.
    fake_batch.set_status(job_id, "SUCCEEDED")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage="admit",
                    unit_id="U", attempt_id=attempt_id)

    monkeypatch.setattr(runctl, "sleep", lambda s: None)
    _auto_complete_reconcile(monkeypatch, db, fake_batch, fake_s3)

    result = cli("run", "start", run_id, "--unit", "U")
    assert result.rc == 0, result.err
    assert "admit U attempt" in result.out and "already in flight" in result.out
    assert f"run={run_id} state=complete" in result.out

    with db.cursor() as cur:
        cur.execute(
            "SELECT stage, state FROM units WHERE run = %s ORDER BY stage", (run_id,))
        rows = dict(cur.fetchall())
    assert rows == {"admit": "complete", "register": "complete"}

    with db.cursor() as cur:
        cur.execute(
            "SELECT unit_id FROM units WHERE run = %s AND stage = 'register'", (run_id,))
        assert cur.fetchone()[0] == "admit/U"


# ======================================================================
# A terminal FAILED job: start exits 1, the unit is failed.
# ======================================================================

def test_start_a_failed_attempt_exits_1(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit")
    first = cli("run", "start", run_id, "--unit", "U", "--inputs", "s3://d", "--no-wait")
    assert first.rc == 0, first.err
    job_id = _kv(first.out, "job")
    fake_batch.set_status(job_id, "FAILED", container_exit_code=1)

    result = cli("run", "start", run_id, "--unit", "U")
    assert result.rc == 1
    assert f"run={run_id} state=failed" in result.out

    with db.cursor() as cur:
        cur.execute("SELECT state FROM units WHERE run = %s AND stage = 'admit'", (run_id,))
        assert cur.fetchone()[0] == "failed"


# ======================================================================
# A transient (exit 75) job returns the unit to ready; start allocates
# another attempt within the same invocation.
# ======================================================================

def test_start_a_transient_exit_returns_to_ready_and_resubmits(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    run_id = _create_run(cli, db, kind="scratch", stages="admit",
                         extra=("--max-attempts", "2"))
    first = cli("run", "start", run_id, "--unit", "U", "--inputs", "s3://d", "--no-wait")
    assert first.rc == 0, first.err
    job_id_1 = _kv(first.out, "job")
    fake_batch.set_status(job_id_1, "FAILED", container_exit_code=75)

    monkeypatch.setattr(runctl, "sleep", lambda s: None)
    _auto_complete_reconcile(monkeypatch, db, fake_batch, fake_s3, skip_jobs=frozenset({job_id_1}))

    result = cli("run", "start", run_id, "--unit", "U", "--inputs", "s3://d")
    assert result.rc == 0, result.err
    assert "is ready again after a transient attempt; allocating another" in result.out
    assert f"run={run_id} state=complete" in result.out

    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT state FROM units WHERE run = %s AND stage = 'admit'", (run_id,))
        assert cur.fetchone()[0] == "complete"


# ======================================================================
# --timeout expiry
# ======================================================================

def test_start_timeout_exits_75_with_a_continue_command(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    run_id = _create_run(cli, db, kind="scratch", stages="admit")
    submitted = cli("run", "start", run_id, "--unit", "U", "--inputs", "s3://d", "--no-wait")
    assert submitted.rc == 0, submitted.err
    job_id = _kv(submitted.out, "job")
    # RUNNING (one of _UNRESOLVED_BATCH_STATUSES): the job never
    # resolves, so every reconcile poll leaves the attempt unresolved.
    fake_batch.set_status(job_id, "RUNNING")

    # A fake clock only sleep() advances, matching real now()/sleep()
    # semantics (test_cli_runctl.py's own World fixture does the same).
    clock = {"t": 0.0}
    monkeypatch.setattr(runctl, "now", lambda: clock["t"])
    monkeypatch.setattr(runctl, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    result = cli("run", "start", run_id, "--unit", "U", "--interval", "30", "--timeout", "60")
    assert result.rc == 75
    assert result.out.splitlines()[-1] == f"run={run_id} state=timeout"
    assert "timed out after 60s" in result.err
    assert f"continue with: rapidpipe run start {run_id} --unit U" in result.err


# ======================================================================
# --stage register before admit is complete: DependencyIncomplete, 64,
# naming the admit unit.
# ======================================================================

def test_start_stage_register_before_admit_is_complete_exits_64(cli, db):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,register")
    result = cli("run", "start", run_id, "--unit", "U", "--stage", "register")
    assert result.rc == 64
    assert f"run={run_id!r}" in result.err
    assert "stage='admit'" in result.err
    assert "unit_id='U'" in result.err
