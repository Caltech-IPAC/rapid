"""Tests for rapidpipe.runs.local and the rapidpipe.cli.main run subcommand
that need no database.

Database-backed end-to-end behaviour (real stage subprocesses, real
attempts, selection, allowance exhaustion) is in
tests/db/test_local_runner.py, which runs against a real PostgreSQL. This
file exercises only what does not need a connection: disposition_for's
pure rule table, the output-location layout, and the CLI's guarantee that
an unknown stage or bad argument is rejected before any database
connection is attempted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rapidpipe.cli.main as cli_main
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.runs.local import LocalAttempt, disposition_for
from rapidpipe.stages.contract import ExitCode


# ======================================================================
# disposition_for
# ======================================================================

@pytest.mark.parametrize("exit_code,manifest_ok,expected", [
    (0, True, "succeeded"),
    (0, False, "failed"),
    (64, True, "failed"),
    (64, False, "failed"),
    (65, True, "failed"),
    (65, False, "failed"),
    (70, True, "failed"),
    (70, False, "failed"),
    (75, True, "transient"),
    (75, False, "transient"),
    (-1, True, "killed"),
    (-9, False, "killed"),
    (99, True, "failed"),
])
def test_disposition_for(exit_code, manifest_ok, expected):
    assert disposition_for(exit_code, manifest_ok) == expected


# ======================================================================
# Output location layout
# ======================================================================

def test_run_stage_locally_output_location_layout(tmp_path, monkeypatch):
    """The output directory follows the runs page's storage layout:
    <outputs_root>/runs/<run_id>/<stage>/<unit_id>/<attempt_id>, with a
    '/' inside unit_id simply nesting one more level. Exercised here via
    a fake connection and a fake subprocess, so no database or real stage
    is needed -- only the path arithmetic and directory creation are
    under test.
    """
    from rapidpipe.runs import local as local_module

    calls = {}

    class _FakeConn:
        def commit(self):
            pass

        def rollback(self):
            pass

    def _fake_add_unit(conn, run_id, stage, unit_kind, unit_id):
        calls["add_unit"] = (run_id, stage, unit_kind, unit_id)

    def _fake_allocate_attempt(conn, run_id, stage, unit_id):
        return "ATTEMPT01"

    def _fake_record_attempt_result(conn, attempt_id, exit_code, disposition,
                                     output_location, execution_record,
                                     scheduler_job_id):
        calls["record"] = (attempt_id, exit_code, disposition, output_location)

    def _fake_select_attempt(conn, attempt_id):
        calls["selected"] = attempt_id

    class _FakeCompletedProcess:
        returncode = 0

    def _fake_run(argv, env):
        calls["argv"] = argv
        # Write a manifest so disposition is "succeeded".
        outputs_dir = Path(argv[argv.index("--outputs") + 1])
        (outputs_dir / "manifest.json").write_text(
            '{"schema_version": "1", "run": "r1", '
            '"unit": {"kind": "detector-image", "id": "e1/SCA07"}, '
            '"stage": "admit", "attempt": "ATTEMPT01", '
            '"execution_record": "exec/ATTEMPT01.json", '
            '"inputs": {"manifest": "x", "products": {}, "result_sets": []}, '
            '"outputs": []}')
        return _FakeCompletedProcess()

    monkeypatch.setattr(local_module, "add_unit", _fake_add_unit)
    monkeypatch.setattr(local_module, "allocate_attempt", _fake_allocate_attempt)
    monkeypatch.setattr(local_module, "record_attempt_result", _fake_record_attempt_result)
    monkeypatch.setattr(local_module, "select_attempt", _fake_select_attempt)
    monkeypatch.setattr(local_module, "_run_schema_version", lambda conn, run_id: "1")
    monkeypatch.setattr(local_module.subprocess, "run", _fake_run)
    # Manifest.read/validate would reject an empty outputs list for a
    # stage that declares produces=(); admit's own manifest never has
    # zero outputs, but this fake stands in for any stage, so patch
    # Manifest.read to accept it at face value.
    monkeypatch.setattr(
        local_module, "_read_manifest_if_valid",
        lambda output_location: object())

    outputs_root = tmp_path / "root"
    result = local_module.run_stage_locally(
        _FakeConn(),
        run_id="RUN01",
        stage="admit",
        unit_kind="detector-image",
        unit_id="e20260821001234/SCA07",
        inputs=str(tmp_path / "in"),
        outputs_root=str(outputs_root),
    )

    expected_location = (
        outputs_root / "runs" / "RUN01" / "admit"
        / "e20260821001234" / "SCA07" / "ATTEMPT01")
    assert Path(result.output_location) == expected_location
    assert expected_location.is_dir()
    assert result.attempt_id == "ATTEMPT01"
    assert result.disposition == "succeeded"
    assert result.selected is True
    assert calls["selected"] == "ATTEMPT01"
    assert calls["add_unit"] == ("RUN01", "admit", "detector-image", "e20260821001234/SCA07")


def test_run_stage_locally_refuses_existing_output_location(tmp_path, monkeypatch):
    from rapidpipe.runs import local as local_module

    class _FakeConn:
        def commit(self):
            pass

    monkeypatch.setattr(local_module, "add_unit", lambda *a, **k: None)
    monkeypatch.setattr(local_module, "allocate_attempt", lambda *a, **k: "ATTEMPT01")

    outputs_root = tmp_path / "root"
    existing = outputs_root / "runs" / "RUN01" / "admit" / "unit1" / "ATTEMPT01"
    existing.mkdir(parents=True)

    with pytest.raises(FileExistsError):
        local_module.run_stage_locally(
            _FakeConn(),
            run_id="RUN01",
            stage="admit",
            unit_kind="detector-image",
            unit_id="unit1",
            inputs=str(tmp_path / "in"),
            outputs_root=str(outputs_root),
        )


# ======================================================================
# CLI: unknown stage / bad arguments never connect
# ======================================================================

def _raise_if_called(*args, **kwargs):
    raise AssertionError("connect() should not be called")


def test_run_local_unknown_stage_exits_64_without_connecting(monkeypatch):
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    rc = cli_main.main([
        "run", "local", "r1", "not-a-real-stage",
        "--unit", "u1", "--inputs", "/tmp/in", "--outputs-root", "/tmp/out",
    ])
    assert rc == int(ExitCode.USAGE)


def test_run_create_bad_kind_exits_64_before_connecting(monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["run", "create", "--kind", "bogus", "--purpose", "p", "--stages", "admit"])
    # argparse's own choices validation exits 2 before rapidpipe ever gets
    # a chance to run --  still before any connection is attempted.
    assert exc_info.value.code == 2


def test_run_create_no_stages_exits_64_before_connecting(monkeypatch):
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    rc = cli_main.main([
        "run", "create", "--kind", "scratch", "--purpose", "p", "--stages", " , ",
    ])
    assert rc == int(ExitCode.USAGE)


def test_run_with_no_subcommand_exits_64_without_connecting(monkeypatch):
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    rc = cli_main.main(["run"])
    assert rc == int(ExitCode.USAGE)


def test_connection_unavailable_exits_75(monkeypatch):
    def _raise_unavailable(*args, **kwargs):
        raise ConnectionUnavailable("no route to host")

    monkeypatch.setattr(cli_main, "connect", _raise_unavailable)
    rc = cli_main.main(["run", "list"])
    assert rc == int(ExitCode.TRANSIENT_FAILURE)
