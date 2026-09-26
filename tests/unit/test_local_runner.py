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

import json
from pathlib import Path

import pytest

import rapidpipe.cli.main as cli_main
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.products.manifest import Inputs, Manifest, Unit, register_unit_id
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
    (69, True, "failed"),
    (69, False, "failed"),
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

    def _fake_allocate_attempt(conn, run_id, stage, unit_id, *, outputs_root=None):
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
    monkeypatch.setattr(local_module, "_source_revision_or_unknown", lambda: "abc123")
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


def test_run_stage_locally_env_none_value_removes_an_inherited_key(tmp_path, monkeypatch):
    """A ``None`` value in ``env`` removes that key from the subprocess
    environment instead of setting it, so a caller can strip something
    this process itself inherited (a production run stripping an ambient
    ``RAPIDPIPE_PROFILE=1``), not only add or overwrite a key."""
    from rapidpipe.runs import local as local_module

    monkeypatch.setenv("RAPIDPIPE_PROFILE", "1")

    class _FakeConn:
        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(local_module, "add_unit", lambda *a, **k: None)
    monkeypatch.setattr(local_module, "allocate_attempt", lambda *a, **k: "ATTEMPT01")
    monkeypatch.setattr(local_module, "record_attempt_result", lambda *a, **k: None)
    monkeypatch.setattr(local_module, "select_attempt", lambda *a, **k: None)
    monkeypatch.setattr(local_module, "_run_schema_version", lambda conn, run_id: "1")
    monkeypatch.setattr(local_module, "_source_revision_or_unknown", lambda: "abc123")
    monkeypatch.setattr(
        local_module, "_read_manifest_if_valid", lambda output_location: object())

    captured = {}

    class _FakeCompletedProcess:
        returncode = 0

    def _fake_run(argv, env):
        captured["env"] = env
        return _FakeCompletedProcess()

    monkeypatch.setattr(local_module.subprocess, "run", _fake_run)

    local_module.run_stage_locally(
        _FakeConn(),
        run_id="RUN01",
        stage="admit",
        unit_kind="detector-image",
        unit_id="e1/SCA07",
        inputs=str(tmp_path / "in"),
        outputs_root=str(tmp_path / "root"),
        env={"RAPIDPIPE_PROFILE": None},
    )

    assert "RAPIDPIPE_PROFILE" not in captured["env"]


def test_run_stage_locally_fills_none_valued_execution_record_fields(tmp_path, monkeypatch):
    """A stage-written exec/<attempt>.json can hold the NOT NULL columns
    present but explicitly null (e.g. a Batch container where git isn't
    available), not merely absent. dict.setdefault is a no-op when the
    key already exists, so run_stage_locally must check the value, not
    just the key, before falling back -- this is the failure the fix
    addresses (NotNullViolation on execution_records.source_revision).
    """
    from rapidpipe.runs import local as local_module

    calls = {}

    class _FakeConn:
        def commit(self):
            pass

        def rollback(self):
            pass

    def _fake_record_attempt_result(conn, attempt_id, exit_code, disposition,
                                     output_location, execution_record,
                                     scheduler_job_id):
        calls["execution_record"] = execution_record

    class _FakeCompletedProcess:
        returncode = 0

    def _fake_run(argv, env):
        outputs_dir = Path(argv[argv.index("--outputs") + 1])
        (outputs_dir / "manifest.json").write_text(
            '{"schema_version": "1", "run": "r1", '
            '"unit": {"kind": "detector-image", "id": "e1/SCA07"}, '
            '"stage": "admit", "attempt": "ATTEMPT01", '
            '"execution_record": "exec/ATTEMPT01.json", '
            '"inputs": {"manifest": "x", "products": {}, "result_sets": []}, '
            '"outputs": []}')
        exec_dir = outputs_dir / "exec"
        exec_dir.mkdir(parents=True, exist_ok=True)
        (exec_dir / "ATTEMPT01.json").write_text(
            '{"image_digest": null, "settings_hash": "6725abc", '
            '"source_revision": null}')
        return _FakeCompletedProcess()

    monkeypatch.setattr(local_module, "add_unit", lambda *a, **k: None)
    monkeypatch.setattr(local_module, "allocate_attempt", lambda *a, **k: "ATTEMPT01")
    monkeypatch.setattr(local_module, "record_attempt_result", _fake_record_attempt_result)
    monkeypatch.setattr(local_module, "select_attempt", lambda conn, attempt_id: None)
    monkeypatch.setattr(local_module, "_run_schema_version", lambda conn, run_id: "2")
    monkeypatch.setattr(local_module, "_source_revision_or_unknown", lambda: "abc123")
    monkeypatch.setattr(local_module.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        local_module, "_read_manifest_if_valid",
        lambda output_location: object())

    outputs_root = tmp_path / "root"
    local_module.run_stage_locally(
        _FakeConn(),
        run_id="RUN01",
        stage="admit",
        unit_kind="detector-image",
        unit_id="unit1",
        inputs=str(tmp_path / "in"),
        outputs_root=str(outputs_root),
    )

    assert calls["execution_record"] == {
        "image_digest": None,
        "settings_hash": "6725abc",
        "source_revision": "abc123",
        "schema_version": "2",
    }


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


def test_invalid_log_level_env_var_exits_64_not_an_uncaught_valueerror(monkeypatch, capsys):
    # configure_root() runs before argparse sees argv at all, so an
    # invalid RAPIDPIPE_LOG_LEVEL used to raise an uncaught ValueError
    # (surfacing as Python's own exit 1) instead of the contract's usage
    # error (direction review, 2026-09-26).
    monkeypatch.setenv("RAPIDPIPE_LOG_LEVEL", "NOT-A-LEVEL")
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    rc = cli_main.main(["run", "list"])
    assert rc == int(ExitCode.USAGE)
    err = capsys.readouterr().err
    assert "RAPIDPIPE_LOG_LEVEL" in err
    assert "NOT-A-LEVEL" in err


def test_invalid_log_level_env_var_exits_64_even_for_version(monkeypatch, capsys):
    # The check runs before argparse's own --version handling (a
    # SystemExit(0)), so a malformed level still wins: usage error, not
    # the version string, and not an uncaught exception either.
    monkeypatch.setenv("RAPIDPIPE_LOG_LEVEL", "NOT-A-LEVEL")
    rc = cli_main.main(["--version"])
    assert rc == int(ExitCode.USAGE)
    assert "RAPIDPIPE_LOG_LEVEL" in capsys.readouterr().err


def test_connection_unavailable_exits_75(monkeypatch):
    def _raise_unavailable(*args, **kwargs):
        raise ConnectionUnavailable("no route to host")

    monkeypatch.setattr(cli_main, "connect", _raise_unavailable)
    rc = cli_main.main(["run", "list"])
    assert rc == int(ExitCode.TRANSIENT_FAILURE)


# ======================================================================
# register's unit id: derived from the manifest it reads, never chosen
# (Ben, 2026-09-23: "a register unit is identified by what it registers";
# id = <producing stage>/<producing unit id>).
# ======================================================================

def _manifest_json(*, stage: str, unit_id: str, run: str = "r0", attempt: str = "a0") -> str:
    manifest = Manifest(
        run=run,
        unit=Unit(kind="detector-image", id=unit_id),
        stage=stage,
        attempt=attempt,
        execution_record=f"exec/{attempt}.json",
        inputs=Inputs(manifest="s3://bucket/root/manifest.json"),
    )
    return manifest.to_json()


def test_register_unit_id_after_admit():
    manifest = Manifest(
        run="r0", unit=Unit(kind="detector-image", id="r0034001002001001001/SCA01"),
        stage="admit", attempt="a0", execution_record="exec/a0.json",
        inputs=Inputs(manifest="s3://bucket/root/manifest.json"))
    assert register_unit_id(manifest) == "admit/r0034001002001001001/SCA01"


def test_register_unit_id_after_difference_is_distinct_from_after_admit():
    admit_manifest = Manifest(
        run="r0", unit=Unit(kind="detector-image", id="r0034001002001001001/SCA01"),
        stage="admit", attempt="a0", execution_record="exec/a0.json",
        inputs=Inputs(manifest="s3://bucket/root/manifest.json"))
    difference_manifest = Manifest(
        run="r0", unit=Unit(kind="detector-image", id="r0034001002001001001/SCA01"),
        stage="difference", attempt="a1", execution_record="exec/a1.json",
        inputs=Inputs(manifest="s3://bucket/root/manifest.json"))

    admit_register_id = register_unit_id(admit_manifest)
    difference_register_id = register_unit_id(difference_manifest)

    assert admit_register_id == "admit/r0034001002001001001/SCA01"
    assert difference_register_id == "difference/r0034001002001001001/SCA01"
    assert admit_register_id != difference_register_id


def test_resolve_register_unit_id_reads_a_local_manifest(tmp_path):
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(
        _manifest_json(stage="admit", unit_id="e1/SCA07"))

    unit_id = cli_main._resolve_register_unit_id(
        unit_id_arg=None, inputs_location_arg=str(inputs))

    assert unit_id == "admit/e1/SCA07"


def test_resolve_register_unit_id_refuses_an_explicit_unit(tmp_path):
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(
        _manifest_json(stage="admit", unit_id="e1/SCA07"))

    with pytest.raises(cli_main.RegisterUnitIdError):
        cli_main._resolve_register_unit_id(
            unit_id_arg="hand-picked", inputs_location_arg=str(inputs))


def test_resolve_register_unit_id_refuses_a_missing_manifest(tmp_path):
    inputs = tmp_path / "empty"
    inputs.mkdir()

    with pytest.raises(cli_main.RegisterUnitIdError):
        cli_main._resolve_register_unit_id(
            unit_id_arg=None, inputs_location_arg=str(inputs))


def test_resolve_register_unit_id_refuses_invalid_json(tmp_path):
    inputs = tmp_path / "bad"
    inputs.mkdir()
    (inputs / "manifest.json").write_text("not json")

    with pytest.raises(cli_main.RegisterUnitIdError):
        cli_main._resolve_register_unit_id(
            unit_id_arg=None, inputs_location_arg=str(inputs))


def test_run_local_register_rejects_an_explicit_unit_without_connecting(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    rc = cli_main.main([
        "run", "local", "r1", "register",
        "--unit", "hand-picked", "--inputs", str(tmp_path), "--outputs-root", "/tmp/out",
    ])
    assert rc == int(ExitCode.USAGE)


def test_run_local_register_derives_unit_id_from_the_manifest(tmp_path, monkeypatch):
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(
        _manifest_json(stage="admit", unit_id="e1/SCA07"))

    seen = {}

    class _FakeConnCtx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    def _fake_connect(*a, **k):
        return _FakeConnCtx()

    def _fake_run_stage_locally(conn, *, run_id, stage, unit_kind, unit_id, inputs,
                                 outputs_root, settings, python, env=None):
        seen["unit_id"] = unit_id
        return LocalAttempt(
            attempt_id="a1", output_location=str(tmp_path / "out"), exit_code=0,
            disposition="succeeded", manifest_path=None, selected=True)

    monkeypatch.setattr(cli_main, "connect", _fake_connect)
    monkeypatch.setattr(cli_main.launch_batch, "_run_kind", lambda conn, run_id: "scratch")
    monkeypatch.setattr(cli_main, "run_stage_locally", _fake_run_stage_locally)

    rc = cli_main.main([
        "run", "local", "r1", "register",
        "--inputs", str(inputs), "--outputs-root", str(tmp_path / "outputs-root"),
    ])

    assert rc == int(ExitCode.SUCCESS)
    assert seen["unit_id"] == "admit/e1/SCA07"


def test_run_local_profile_sets_the_subprocess_environment(tmp_path, monkeypatch):
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(_manifest_json(stage="admit", unit_id="e1/SCA07"))

    seen = {}

    class _FakeConn:
        def rollback(self):
            pass

    class _FakeConnCtx:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    def _fake_run_stage_locally(conn, *, run_id, stage, unit_kind, unit_id, inputs,
                                 outputs_root, settings, python, env=None):
        seen["env"] = env
        return LocalAttempt(
            attempt_id="a1", output_location=str(tmp_path / "out"), exit_code=0,
            disposition="succeeded", manifest_path=None, selected=True)

    monkeypatch.setattr(cli_main, "connect", lambda *a, **k: _FakeConnCtx())
    monkeypatch.setattr(cli_main.launch_batch, "_run_kind", lambda conn, run_id: "scratch")
    monkeypatch.setattr(cli_main, "run_stage_locally", _fake_run_stage_locally)

    rc = cli_main.main([
        "run", "local", "r1", "register",
        "--inputs", str(inputs), "--outputs-root", str(tmp_path / "outputs-root"),
        "--profile",
    ])

    assert rc == int(ExitCode.SUCCESS)
    assert seen["env"] == {"RAPIDPIPE_PROFILE": "1"}


def test_run_local_production_run_strips_an_inherited_profile_env_var(tmp_path, monkeypatch):
    # No --profile given, but the calling shell's own environment already
    # carries RAPIDPIPE_PROFILE=1 (local.py's subprocess_env starts from
    # dict(os.environ), so it would otherwise inherit this unchanged); a
    # production run must still never profile (direction review,
    # 2026-09-26): main.py now strips the key for every production run,
    # not only when --profile is passed and refused outright.
    monkeypatch.setenv("RAPIDPIPE_PROFILE", "1")
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(_manifest_json(stage="admit", unit_id="e1/SCA07"))

    seen = {}

    class _FakeConn:
        def rollback(self):
            pass

    class _FakeConnCtx:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    def _fake_run_stage_locally(conn, *, run_id, stage, unit_kind, unit_id, inputs,
                                 outputs_root, settings, python, env=None):
        seen["env"] = env
        return LocalAttempt(
            attempt_id="a1", output_location=str(tmp_path / "out"), exit_code=0,
            disposition="succeeded", manifest_path=None, selected=True)

    monkeypatch.setattr(cli_main, "connect", lambda *a, **k: _FakeConnCtx())
    monkeypatch.setattr(cli_main.launch_batch, "_run_kind", lambda conn, run_id: "production")
    monkeypatch.setattr(cli_main, "run_stage_locally", _fake_run_stage_locally)

    rc = cli_main.main([
        "run", "local", "r1", "register",
        "--inputs", str(inputs), "--outputs-root", str(tmp_path / "outputs-root"),
    ])

    assert rc == int(ExitCode.SUCCESS)
    assert seen["env"] == {"RAPIDPIPE_PROFILE": None}


def test_run_local_scratch_run_without_profile_passes_no_env_override(tmp_path, monkeypatch):
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(_manifest_json(stage="admit", unit_id="e1/SCA07"))

    seen = {}

    class _FakeConn:
        def rollback(self):
            pass

    class _FakeConnCtx:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    def _fake_run_stage_locally(conn, *, run_id, stage, unit_kind, unit_id, inputs,
                                 outputs_root, settings, python, env=None):
        seen["env"] = env
        return LocalAttempt(
            attempt_id="a1", output_location=str(tmp_path / "out"), exit_code=0,
            disposition="succeeded", manifest_path=None, selected=True)

    monkeypatch.setattr(cli_main, "connect", lambda *a, **k: _FakeConnCtx())
    monkeypatch.setattr(cli_main.launch_batch, "_run_kind", lambda conn, run_id: "scratch")
    monkeypatch.setattr(cli_main, "run_stage_locally", _fake_run_stage_locally)

    rc = cli_main.main([
        "run", "local", "r1", "register",
        "--inputs", str(inputs), "--outputs-root", str(tmp_path / "outputs-root"),
    ])

    assert rc == int(ExitCode.SUCCESS)
    assert seen["env"] is None


def test_run_submit_profile_not_allowed_maps_to_exit_64(monkeypatch):
    from rapidpipe.launch.batch import ProfileNotAllowed

    class _FakeConn:
        def rollback(self):
            pass

    class _FakeConnCtx:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    def _fake_submit_unit(conn, **kwargs):
        assert kwargs["profile"] is True
        raise ProfileNotAllowed("--profile is refused for a production run (r1)")

    monkeypatch.setattr(cli_main, "connect", lambda *a, **k: _FakeConnCtx())
    monkeypatch.setattr(cli_main.launch_batch, "submit_unit", _fake_submit_unit)

    rc = cli_main.main([
        "run", "submit", "r1", "admit",
        "--unit", "u1", "--inputs", "s3://in/pre", "--profile",
    ])

    assert rc == int(ExitCode.USAGE)


def test_run_local_profile_refused_for_a_production_run(tmp_path, monkeypatch):
    inputs = tmp_path / "admit-outputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(_manifest_json(stage="admit", unit_id="e1/SCA07"))

    class _FakeConn:
        def rollback(self):
            pass

    class _FakeConnCtx:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli_main, "connect", lambda *a, **k: _FakeConnCtx())
    monkeypatch.setattr(cli_main.launch_batch, "_run_kind", lambda conn, run_id: "production")
    monkeypatch.setattr(cli_main, "run_stage_locally", _raise_if_called)

    rc = cli_main.main([
        "run", "local", "r1", "register",
        "--inputs", str(inputs), "--outputs-root", str(tmp_path / "outputs-root"),
        "--profile",
    ])

    assert rc == int(ExitCode.USAGE)


def test_run_local_non_register_stage_still_requires_unit(monkeypatch):
    monkeypatch.setattr(cli_main, "connect", _raise_if_called)
    rc = cli_main.main([
        "run", "local", "r1", "admit",
        "--inputs", "/tmp/in", "--outputs-root", "/tmp/out",
    ])
    assert rc == int(ExitCode.USAGE)
