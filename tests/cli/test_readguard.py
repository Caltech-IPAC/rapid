"""The stage read guard on all three invocation paths, against a real PostgreSQL.

The done condition: a stage reading another run's scratch file product
exits 65 on all three paths.

Fixture: a scratch run S registers an ``l2-image`` F (a file product,
members with SHA-256) and a ``source-set`` result set; a production run
P has a unit and an attempt; an input manifest (a local directory holding
``manifest.json``) names F as an output entry and the result set under
``inputs.result_sets``. Then:

1. direct: ``rapidpipe stage run difference --run P ...`` through
   ``rapidpipe.cli.main.main`` exits 65 with the guard's message;
2. local launcher: ``rapidpipe.runs.local.run_stage_locally`` runs
   ``python -m rapidpipe.stages.difference`` as a subprocess with its own
   argv (the suite's in-process stub does not reach it, so the real
   guard runs) and the attempt exits 65;
3. Batch: ``rapidpipe.launch.batch.submit_unit`` submits to a fake Batch,
   and the exact ``containerOverrides.command`` it submitted (the older
   ``stage difference ...`` form ``main`` rewrites) is run through
   ``rapidpipe.cli.main.main`` as the container's ``rapidpipe``
   entrypoint would, with S3 ``--inputs``: exit 65, and only
   ``manifest.json`` was fetched.

No stage body runs: the guard refuses first. The controls use
``--dry-run``, which runs the guard and returns before the body.

Rows are written with the repository functions on the ``db`` fixture's
autocommit connection (the stage opens its own connection, so nothing
can stay in an uncommitted transaction) and deleted at teardown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rapidpipe.cli import main as cli_main
from rapidpipe.db import connection as db_connection
from rapidpipe.db.ids import new_ulid
from rapidpipe.launch import batch as launch_batch
from rapidpipe.runs import readguard
from rapidpipe.runs import repository as repo
from rapidpipe.runs.local import run_stage_locally
from tests.unit.fakebatch import FakeBatch

from .conftest import FAKE_BUCKET

pytestmark = [pytest.mark.readguard, pytest.mark.real_input_manifest]

STAGE = "difference"
SHA = "sha256:" + "5" * 64
F_BYTES = b"not really a FITS file"


# ----------------------------------------------------------------------
# Fixture builders
# ----------------------------------------------------------------------

def _run(db, kind: str, *, max_attempts: int = 2) -> str:
    run_id = repo.create_run(
        db.connection, kind=kind, owner="readguard-test", purpose="readguard",
        selected_stages=["admit", STAGE], code_revision="abc123", image_digest=None,
        schema_version="1", settings_overlay_ref=None, input_selection_ref=None,
        lane="default", resource_profile="default", database_target="rapid",
        max_attempts_per_unit=max_attempts, auto_promote=False, check_policy_ref=None)
    return db.track_run(run_id)


def _attempt(db, run_id: str, stage: str, unit_id: str, *, select: bool = True) -> str:
    repo.add_unit(db.connection, run_id, stage, "detector-image", unit_id)
    attempt_id = repo.allocate_attempt(db.connection, run_id, stage, unit_id)
    repo.record_attempt_result(
        db.connection, attempt_id, exit_code=0, disposition="succeeded",
        output_location=f"runs/{run_id}/{stage}/{unit_id}/{attempt_id}",
        execution_record={"source_revision": "abc123", "schema_version": "1",
                          "settings_hash": "sha256:xyz"},
        scheduler_job_id=None)
    if select:
        repo.select_attempt(db.connection, attempt_id)
    return attempt_id


def _l2_entry(instance: str, *, path: str | None = None) -> dict:
    path = path or f"l2/{instance}.fits"
    return {"kind": "l2-image", "format_version": "1", "instance": instance,
            "key": {"exposure": f"e{instance[-6:]}", "detector": "SCA01"},
            "primary": path,
            "members": [{"role": "image", "path": path, "bytes": len(F_BYTES),
                         "sha256": SHA}]}


def _result_set_entry(instance: str) -> dict:
    return {"kind": "source-set", "format_version": "1", "instance": instance,
            "key": {"difference": new_ulid()}, "primary": None, "members": [],
            "row_count": 0}


def _register(db, run_id: str, stage: str, attempt_id: str, entries: list[dict]) -> None:
    repo.register_manifest(db.connection, {
        "run": run_id, "unit": {"kind": "detector-image", "id": "U"},
        "stage": stage, "attempt": attempt_id,
        "inputs": {"manifest": "x", "products": {}, "result_sets": []},
        "outputs": entries,
    }, registering_attempt_id=attempt_id)


def _producer(db, kind: str, *, select: bool = True) -> tuple[str, str, str]:
    """A run of ``kind`` whose ``admit`` unit registered an l2-image F and a
    source-set; returns ``(run, F, result set)``."""
    run_id = _run(db, kind)
    unit_id = f"p-{new_ulid()}"
    attempt_id = _attempt(db, run_id, "admit", unit_id, select=select)
    if not select:
        # Another attempt of the same unit is the selected one.
        _attempt(db, run_id, "admit", unit_id, select=True)
    f_id, rs_id = new_ulid(), new_ulid()
    _register(db, run_id, "admit", attempt_id, [_l2_entry(f_id), _result_set_entry(rs_id)])
    return run_id, f_id, rs_id


def _input_manifest(*, instances=(), result_sets=(), entries=None) -> dict:
    outputs = entries if entries is not None else [_l2_entry(i) for i in instances]
    return {
        "schema_version": "1", "run": "R", "unit": {"kind": "detector-image", "id": "U"},
        "stage": "input-set", "attempt": new_ulid(), "execution_record": "exec/x.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": list(result_sets)},
        "outputs": outputs,
    }


def _local_inputs(tmp_path: Path, manifest: dict) -> Path:
    directory = tmp_path / f"inputs-{new_ulid()}"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(manifest))
    for entry in manifest["outputs"]:
        for member in entry["members"]:
            path = directory / member["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(F_BYTES)
    return directory


def _direct(cli, run_id, inputs, outputs, *, dry_run=False, unit="U", attempt=None):
    argv = ["stage", "run", STAGE, "--run", run_id, "--unit", unit,
            "--attempt", attempt or new_ulid(), "--inputs", str(inputs),
            "--outputs", str(outputs)]
    if dry_run:
        argv.append("--dry-run")
    return cli(*argv)


@pytest.fixture()
def scratch_f(db):
    """S (scratch) with F and a result set; P (production) with a unit and attempt."""
    s_run, f_id, rs_id = _producer(db, "scratch")
    p_run = _run(db, "production")
    unit_id = f"u-{new_ulid()}"
    p_attempt = _attempt(db, p_run, STAGE, unit_id, select=False)
    return {"S": s_run, "F": f_id, "RS": rs_id, "P": p_run, "unit": unit_id,
            "attempt": p_attempt}


def _refused_f(text: str, fx) -> bool:
    return (f"input {fx['F']}" in text and "l2-image" in text and "custody scratch" in text
            and f"owning run {fx['S']}" in text and "not readable" in text)


# ----------------------------------------------------------------------
# The three paths
# ----------------------------------------------------------------------

def test_path_direct_stage_run_refuses_another_runs_scratch_file_product_with_65(
        cli, db, scratch_f, tmp_path):
    inputs = _local_inputs(tmp_path, _input_manifest(
        instances=[scratch_f["F"]], result_sets=[scratch_f["RS"]]))
    outputs = tmp_path / "out"
    result = _direct(cli, scratch_f["P"], inputs, outputs, unit=scratch_f["unit"],
                     attempt=scratch_f["attempt"])
    assert result.rc == 65, result.err
    assert _refused_f(result.err, scratch_f), result.err
    assert not (outputs / "manifest.json").exists()


def test_path_direct_dry_run_refuses_the_same_way(cli, db, scratch_f, tmp_path):
    inputs = _local_inputs(tmp_path, _input_manifest(instances=[scratch_f["F"]]))
    result = _direct(cli, scratch_f["P"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 65, result.err
    assert _refused_f(result.err, scratch_f), result.err


def test_path_local_launcher_refuses_with_65(db, scratch_f, tmp_path, capfd):
    inputs = _local_inputs(tmp_path, _input_manifest(
        instances=[scratch_f["F"]], result_sets=[scratch_f["RS"]]))
    unit_id = f"l-{new_ulid()}"
    attempt = run_stage_locally(
        db.connection, run_id=scratch_f["P"], stage=STAGE, unit_kind="detector-image",
        unit_id=unit_id, inputs=str(inputs), outputs_root=str(tmp_path / "root"))
    captured = capfd.readouterr()
    assert attempt.exit_code == 65, captured.err
    assert attempt.disposition == "failed" and not attempt.selected
    assert _refused_f(captured.err, scratch_f), captured.err
    with db.cursor() as cur:
        cur.execute("SELECT exit_code, disposition FROM attempts WHERE id = %s",
                    (attempt.attempt_id,))
        assert cur.fetchone() == (65, "failed")


def test_path_batch_command_refuses_with_65_and_fetches_only_the_manifest(
        cli, db, scratch_f, tmp_path, fake_s3, monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "test-queue")
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    manifest = _input_manifest(instances=[scratch_f["F"]], result_sets=[scratch_f["RS"]])
    prefix = f"inputs/{new_ulid()}"
    fake_s3.seed(FAKE_BUCKET, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    member_key = f"{prefix}/l2/{scratch_f['F']}.fits"
    fake_s3.seed(FAKE_BUCKET, member_key, F_BYTES)
    batch = FakeBatch()

    submission = launch_batch.submit_unit(
        db.connection, run_id=scratch_f["P"], stage=STAGE, unit_kind="detector-image",
        unit_id=f"b-{new_ulid()}", inputs_location=f"s3://{FAKE_BUCKET}/{prefix}",
        outputs_root=f"s3://{FAKE_BUCKET}/production", job_definition="test-def",
        client=batch)
    command = batch.submitted[0]["containerOverrides"]["command"]
    assert command[:2] == ["stage", STAGE]
    assert submission.attempt_id in command
    fake_s3.calls.clear()

    result = cli(*command)
    assert result.rc == 65, result.err
    assert _refused_f(result.err, scratch_f), result.err
    fetched = [key for op, key in fake_s3.calls if op in ("download_file", "get_object")]
    assert fetched == [f"{prefix}/manifest.json"]
    assert member_key not in [key for _op, key in fake_s3.calls]


# ----------------------------------------------------------------------
# Controls
# ----------------------------------------------------------------------

def test_the_scratch_result_set_alone_is_refused_too(cli, db, scratch_f, tmp_path):
    inputs = _local_inputs(tmp_path, _input_manifest(result_sets=[scratch_f["RS"]]))
    result = _direct(cli, scratch_f["P"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 65, result.err
    assert f"input {scratch_f['RS']}" in result.err and "source-set" in result.err


def test_f_in_its_own_run_passes_even_as_scratch(cli, db, scratch_f, tmp_path):
    inputs = _local_inputs(tmp_path, _input_manifest(
        instances=[scratch_f["F"]], result_sets=[scratch_f["RS"]]))
    result = _direct(cli, scratch_f["S"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 0, result.err


def test_a_selected_candidate_from_a_production_run_passes(cli, db, tmp_path):
    q_run, f_id, rs_id = _producer(db, "production")
    consumer = _run(db, "production")
    inputs = _local_inputs(tmp_path, _input_manifest(instances=[f_id], result_sets=[rs_id]))
    result = _direct(cli, consumer, inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 0, result.err
    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (f_id,))
        assert cur.fetchone() == ("candidate",)


def test_a_candidate_from_an_unselected_attempt_is_refused_with_65(cli, db, tmp_path):
    q_run, f_id, _rs_id = _producer(db, "production", select=False)
    consumer = _run(db, "production")
    inputs = _local_inputs(tmp_path, _input_manifest(instances=[f_id]))
    result = _direct(cli, consumer, inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 65, result.err
    assert f"input {f_id}" in result.err and "custody candidate" in result.err
    assert "unselected" in result.err or "selected attempt" in result.err


def test_an_unregistered_instance_id_passes(cli, db, tmp_path):
    consumer = _run(db, "production")
    inputs = _local_inputs(tmp_path, _input_manifest(
        instances=[new_ulid()], result_sets=[new_ulid()]))
    result = _direct(cli, consumer, inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 0, result.err


def test_a_fresh_id_over_another_runs_scratch_files_is_refused_with_65(
        cli, db, scratch_f, tmp_path):
    relabelled = _l2_entry(new_ulid(), path=f"l2/{scratch_f['F']}.fits")
    inputs = _local_inputs(tmp_path, _input_manifest(entries=[relabelled]))
    result = _direct(cli, scratch_f["P"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 65, result.err
    assert f"registered instance {scratch_f['F']}" in result.err


def test_a_readable_member_does_not_authorise_another_runs_scratch_member(
        cli, db, scratch_f, tmp_path):
    q_run, g_id, _rs = _producer(db, "production")
    readable = _l2_entry(g_id)
    # The same SHA-256 fixture value for both files: F's and G's members
    # differ only by path, and each path identifies its instance.
    mixed = dict(_l2_entry(new_ulid(), path=f"l2/{g_id}.fits"))
    mixed["members"] = readable["members"] + [
        {"role": "mask", "path": f"l2/{scratch_f['F']}.fits", "bytes": len(F_BYTES),
         "sha256": SHA}]
    inputs = _local_inputs(tmp_path, _input_manifest(entries=[mixed]))
    result = _direct(cli, scratch_f["P"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 65, result.err
    assert f"is a file of registered instance {scratch_f['F']}" in result.err


def test_no_instance_named_makes_no_connection(cli, db, tmp_path, monkeypatch):
    calls = []

    def _connect(*args, **kwargs):
        calls.append(1)
        raise AssertionError("the guard must not connect")

    monkeypatch.setattr(readguard, "connect", _connect)
    consumer = _run(db, "production")
    inputs = _local_inputs(tmp_path, _input_manifest())
    result = _direct(cli, consumer, inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 0, result.err
    assert calls == []


def test_registered_ids_with_the_database_unreachable_exit_75(
        cli, db, scratch_f, tmp_path, monkeypatch):
    real_connect = db_connection.connect
    # Port 1 on the same host refuses at once; one attempt, no backoff.
    monkeypatch.setenv("PGPORT", "1")
    monkeypatch.setattr(readguard, "connect",
                        lambda: real_connect(attempts=1, connect_timeout=2))
    inputs = _local_inputs(tmp_path, _input_manifest(instances=[scratch_f["F"]]))
    result = _direct(cli, scratch_f["P"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 75, result.err
    assert "could not connect to the database" in result.err


def test_registered_ids_with_no_database_configured_exit_64(
        cli, db, scratch_f, tmp_path, monkeypatch):
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD",
                 "RAPID_PARAMETER_PATH", "RAPID_DB_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)
    inputs = _local_inputs(tmp_path, _input_manifest(instances=[scratch_f["F"]]))
    result = _direct(cli, scratch_f["P"], inputs, tmp_path / "out", dry_run=True)
    assert result.rc == 64, result.err
    assert "none is configured" in result.err
