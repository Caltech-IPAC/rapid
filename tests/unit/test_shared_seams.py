"""One monkeypatch of a seam the CLI shares with launch reaches every
caller: the CLI calls ``launch.walk``, ``runs.create`` and
``products.storage`` through their module attributes at call time, as
launch does, so a test patches the seam in one place
(stage-contract.md §The package)."""

from __future__ import annotations

import datetime as dt

from rapidpipe.cli import main as cli
from rapidpipe.cli import runctl
from rapidpipe.launch import loop
from rapidpipe.launch import walk as launch_walk
from rapidpipe.launch.walk import RegisterUnitIdError
from rapidpipe.products import storage as products_storage
from rapidpipe.runs import create as runs_create
from rapidpipe.runs import repository
from tests.unit.test_cli_runctl import FULL, fake_conn, world  # noqa: F401 (fixtures)
from tests.unit.test_loop import _Conn, _row
from tests.unit.test_loop_discovery import _spec

PROBE = "s3://probe/refused"


def test_one_patch_of_the_register_resolver_reaches_run_local_run_submit_and_the_walk(
        world, monkeypatch, capsys):
    w = world(FULL)
    walk_resolver = launch_walk.resolve_register_unit_id   # the world's stand-in
    calls = []

    def counting(*, unit_id_arg, inputs_location_arg):
        calls.append(inputs_location_arg)
        if inputs_location_arg == PROBE:
            raise RegisterUnitIdError("probe")
        return walk_resolver(unit_id_arg=unit_id_arg, inputs_location_arg=inputs_location_arg)

    monkeypatch.setattr(launch_walk, "resolve_register_unit_id", counting)
    monkeypatch.setattr(launch_walk, "compose_inputs",
                        lambda conn, **kw: "s3://b/runs/R/inputs/difference/U")

    assert cli.main(["run", "local", "R", "register", "--inputs", PROBE,
                     "--outputs-root", "/tmp/unused"]) == 64
    assert cli.main(["run", "submit", "R", "register", "--inputs", PROBE]) == 64
    assert calls == [PROBE, PROBE]

    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://deliv/U"]) == 0
    assert calls[2:] == ["s3://b/runs/R/admit/U/A1", "s3://b/runs/R/difference/U/A3"]
    assert [s["unit"] for s in w.submits if s["stage"] == "register"] == [
        "admit/U", "difference/U"]
    capsys.readouterr()


def test_one_patch_of_create_run_record_reaches_run_create_and_the_loop(
        fake_conn, monkeypatch, capsys):
    calls = []

    def create(conn, **kwargs):
        calls.append(kwargs["lane"])
        return f"RUN{len(calls)}"

    monkeypatch.setattr(runs_create, "create_run_record", create)
    assert cli.main(["run", "create", "--kind", "scratch", "--purpose", "p",
                     "--stages", "admit"]) == 0
    assert capsys.readouterr().out.strip() == "RUN1"
    spec = _spec()
    assert loop._create_run(_Conn(), spec, dt.date(2027, 10, 1), 1) == "RUN2"
    assert calls == ["local", spec.lane]


def test_one_patch_of_create_only_failed_run_reaches_run_create_and_the_loop(
        fake_conn, monkeypatch, capsys):
    seeds = []

    def refuse(conn, seed, **kwargs):
        seeds.append(seed)
        raise repository.SeedRefused(f"seed run {seed!r} has nothing to re-run")

    monkeypatch.setattr(runs_create, "create_only_failed_run", refuse)
    monkeypatch.setattr(loop, "_update_row", lambda *a, **kw: None)
    assert cli.main(["run", "create", "--seed", "SEED1", "--only-failed"]) == 64
    assert loop.retry_date(_Conn(), _spec(), _row(run="SEED2", state="failed")) == 1
    assert seeds == ["SEED1", "SEED2"]
    capsys.readouterr()


def test_the_cli_s_storage_is_the_one_storage_class():
    # runctl names the class only as an alias; a class-attribute patch
    # through either name is one patch.
    assert runctl._Storage is products_storage.Storage
