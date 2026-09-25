"""``rapidpipe run start|status|inputs|compare|expire``, ``run create
--seed`` and the cleanup role, with no database: ``connect`` is
monkeypatched on ``rapidpipe.cli.main`` and the SQL ``runctl`` reads is
replaced by a small in-memory stand-in."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.cli import runctl
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products.manifest import (
    Inputs,
    Manifest,
    OutputEntry,
    Unit,
    member_for_file,
)
from rapidpipe.runs import cleanup, repository
from tests.unit.fakes3 import FakeClientError, FakeS3


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_k):
        pass

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def cursor(self):
        return _Cursor()


@pytest.fixture()
def fake_conn(monkeypatch):
    conn = _Conn()

    @contextlib.contextmanager
    def _connect(**_kwargs):
        yield conn

    monkeypatch.setattr(cli, "connect", _connect)
    return conn


# ======================================================================
# An in-memory run: units, attempts, submit/reconcile/resolve
# ======================================================================

class World:
    """A run's units and attempts, and the launcher calls over them.

    ``outcomes[stage]`` lists the dispositions reconcile gives that
    stage's successive attempts (default ``succeeded``); an attempt is
    resolved after ``polls_to_finish`` reconcile calls.
    """

    def __init__(self, stages, *, max_attempts=1, kind="scratch"):
        self.run = runctl.RunRow(kind, list(stages), "open", None)
        self.max_attempts = max_attempts
        self.units: dict[tuple[str, str], dict] = {}
        self.outcomes: dict[str, list[str]] = {}
        self.polls_to_finish = 1
        self.submits: list[dict] = []
        self.reconciles = 0
        self._n = 0

    def unit(self, stage, unit_id):
        return self.units.setdefault(
            (stage, unit_id), {"state": "pending", "selected": None, "attempts": []})

    def complete(self, stage, unit_id):
        """Test setup: a unit already complete, with a selected attempt."""
        u = self.unit(stage, unit_id)
        self._n += 1
        attempt = {"id": f"OLD{self._n}", "disposition": "succeeded", "job": f"job-old{self._n}",
                   "out": f"s3://b/runs/R/{stage}/{unit_id}/OLD{self._n}", "polls": 0}
        u["attempts"].append(attempt)
        u["state"], u["selected"] = "complete", attempt["id"]

    # -- runctl's SQL --------------------------------------------------
    def run_row(self, conn, run_id):
        return self.run if run_id == "R" else None

    def unit_row(self, conn, run_id, stage, unit_id):
        u = self.units.get((stage, unit_id))
        if u is None:
            return None
        last = u["attempts"][-1] if u["attempts"] else {}
        return runctl.UnitRow(u["state"], u["selected"], last.get("id"),
                              last.get("disposition"), last.get("job"), last.get("out"))

    # -- rapidpipe.launch.batch ------------------------------------------
    def submit_unit(self, conn, *, run_id, stage, unit_kind, unit_id, inputs_location,
                    settings_location=None):
        u = self.unit(stage, unit_id)
        if u["state"] in ("complete", "failed", "cancelled"):
            raise repository.UnitTerminal(f"unit {stage}/{unit_id} is {u['state']!r}")
        if len(u["attempts"]) >= self.max_attempts:
            raise repository.AttemptAllowanceExhausted(f"{stage}/{unit_id} allowance used")
        self._n += 1
        attempt = {"id": f"A{self._n}", "disposition": None, "job": f"job-{self._n}",
                   "out": f"s3://b/runs/R/{stage}/{unit_id}/A{self._n}", "polls": 0}
        u["attempts"].append(attempt)
        u["state"] = "running"
        self.submits.append({"stage": stage, "unit": unit_id, "kind": unit_kind,
                             "inputs": inputs_location, "settings": settings_location})
        return launch_batch.BatchSubmission(attempt["id"], attempt["job"], "name", attempt["out"])

    def reconcile(self, conn, *, run_id):
        self.reconciles += 1
        results = []
        for (stage, _unit_id), u in self.units.items():
            for attempt in u["attempts"]:
                if attempt["disposition"] is not None:
                    continue
                attempt["polls"] += 1
                if attempt["polls"] < self.polls_to_finish:
                    results.append(launch_batch.Reconciled(
                        attempt["id"], attempt["job"], "RUNNING", None, False))
                    continue
                queue = self.outcomes.get(stage, [])
                disposition = queue.pop(0) if queue else "succeeded"
                attempt["disposition"] = disposition
                # As record_attempt_result: transient/lost return the unit to
                # ready while attempts remain; failed/killed are terminal.
                if disposition == "succeeded":
                    u["state"], u["selected"] = "complete", attempt["id"]
                elif disposition in ("transient", "lost"):
                    u["state"] = "ready" if len(u["attempts"]) < self.max_attempts else "failed"
                else:
                    u["state"] = "failed"
                results.append(launch_batch.Reconciled(
                    attempt["id"], attempt["job"],
                    "SUCCEEDED" if disposition == "succeeded" else "FAILED",
                    disposition, disposition == "succeeded"))
        return results

    def resolve(self, conn, *, run_id, unit_id, upstream_stage):
        u = self.units.get((upstream_stage, unit_id))
        if u is None or u["state"] != "complete":
            raise launch_batch.DependencyIncomplete(
                f"unit {upstream_stage}/{unit_id} is not complete")
        return next(a["out"] for a in u["attempts"] if a["id"] == u["selected"])


@pytest.fixture()
def world(monkeypatch, fake_conn):
    holder = {}

    def install(stages, **kwargs):
        w = World(stages, **kwargs)
        monkeypatch.setattr(runctl, "_run_row", w.run_row)
        monkeypatch.setattr(runctl, "_unit_row", w.unit_row)
        monkeypatch.setattr(launch_batch, "submit_unit", w.submit_unit)
        monkeypatch.setattr(launch_batch, "reconcile", w.reconcile)
        monkeypatch.setattr(launch_batch, "resolve_inputs_from_stage", w.resolve)
        # register's unit id is "<producing stage>/<unit>"; the stand-in
        # reads it off the producer's output location instead of a manifest.
        monkeypatch.setattr(
            cli, "_resolve_register_unit_id",
            lambda *, unit_id_arg, inputs_location_arg:
                "/".join(inputs_location_arg.split("/runs/R/")[1].split("/")[:-1]))
        clock = {"t": 0.0}
        monkeypatch.setattr(runctl, "now", lambda: clock["t"])
        monkeypatch.setattr(runctl, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
        holder["world"] = w
        return w

    return install


FULL = ["admit", "register", "difference", "register", "load"]


def test_start_walks_every_stage_in_order_with_inputs_by_precedence(world, monkeypatch, capsys):
    w = world(FULL)
    composed = []

    def _compose(conn, **kw):
        composed.append(kw)
        return "s3://b/runs/R/inputs/difference/U"

    monkeypatch.setattr(runctl, "compose_inputs", _compose)
    rc = cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://deliv/U",
                   "--template", "difference=s3://tmpl/ref", "--settings", "s3://s/admit.toml",
                   "--settings", "load=s3://s/load.toml"])
    assert rc == 0
    assert [(s["stage"], s["unit"], s["inputs"], s["settings"]) for s in w.submits] == [
        ("admit", "U", "s3://deliv/U", "s3://s/admit.toml"),
        ("register", "admit/U", "s3://b/runs/R/admit/U/A1", None),
        ("difference", "U", "s3://b/runs/R/inputs/difference/U", None),
        ("register", "difference/U", "s3://b/runs/R/difference/U/A3", None),
        # load after register reads difference's output, not register's.
        ("load", "U", "s3://b/runs/R/difference/U/A3", "s3://s/load.toml"),
    ]
    assert composed == [{"run_id": "R", "stage": "difference", "unit_id": "U",
                         "from_stage": "admit", "template": "s3://tmpl/ref",
                         "reuse_existing": True}]
    out = capsys.readouterr().out.splitlines()
    assert ("stage=admit unit=U attempt=A1 job=job-1 disposition=succeeded "
            "outputs=s3://b/runs/R/admit/U/A1") in out
    assert out[-1] == "run=R state=complete"


def test_start_skips_complete_stages(world, capsys):
    w = world(["admit", "register"])
    w.complete("admit", "U")
    assert cli.main(["run", "start", "R", "--unit", "U"]) == 0
    out = capsys.readouterr().out
    assert "admit U already complete" in out
    assert [s["stage"] for s in w.submits] == ["register"]
    assert w.submits[0]["unit"] == "admit/U"


def test_start_everything_complete_submits_nothing(world, capsys):
    w = world(["admit"])
    w.complete("admit", "U")
    assert cli.main(["run", "start", "R", "--unit", "U"]) == 0
    assert w.submits == []
    assert capsys.readouterr().out.splitlines()[-1] == "run=R state=complete"


def test_start_first_stage_without_inputs_exits_64(world, capsys):
    world(["admit", "register"])
    assert cli.main(["run", "start", "R", "--unit", "U"]) == 64
    assert "give its inputs with --inputs admit=<location>" in capsys.readouterr().err


def test_start_unknown_run_exits_64(world, capsys):
    world(["admit"])
    assert cli.main(["run", "start", "NOPE", "--unit", "U", "--inputs", "x"]) == 64
    assert "no such run: NOPE" in capsys.readouterr().err


def test_start_stage_not_selected_exits_64(world, capsys):
    world(["admit"])
    assert cli.main(["run", "start", "R", "--unit", "U", "--stage", "load"]) == 64
    assert "not one of run R's selected stages" in capsys.readouterr().err


def test_start_failed_attempt_exits_1(world, capsys):
    w = world(["admit", "register"], max_attempts=3)
    w.outcomes["admit"] = ["failed"]
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://deliv/U"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "stage=admit unit=U attempt=A1 job=job-1 disposition=failed" in out[-2]
    assert out[-1] == "run=R state=failed"
    assert [s["stage"] for s in w.submits] == ["admit"]


def test_start_transient_result_gets_another_attempt_in_the_same_invocation(world, capsys):
    w = world(["admit", "register"], max_attempts=3)
    w.outcomes["admit"] = ["transient", "lost"]
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://deliv/U"]) == 0
    assert [(s["stage"], s["unit"], s["inputs"]) for s in w.submits] == [
        ("admit", "U", "s3://deliv/U"), ("admit", "U", "s3://deliv/U"),
        ("admit", "U", "s3://deliv/U"), ("register", "admit/U", "s3://b/runs/R/admit/U/A3")]
    out = capsys.readouterr().out
    assert "admit U is ready again after a transient attempt; allocating another" in out
    assert "admit U is ready again after a lost attempt; allocating another" in out


def test_start_transient_until_the_allowance_is_used_exits_1(world, capsys):
    w = world(["admit"], max_attempts=2)
    w.outcomes["admit"] = ["transient", "transient"]
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d"]) == 1
    assert len(w.submits) == 2
    assert w.unit("admit", "U")["state"] == "failed"


def test_start_retries_share_one_timeout(world, capsys):
    w = world(["admit"], max_attempts=10)
    w.polls_to_finish = 2
    w.outcomes["admit"] = ["transient"] * 9
    rc = cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d",
                   "--interval", "30", "--timeout", "100"])
    # Attempts resolve at t=30, 60, 90 and 120; the fourth is past the
    # 100 s deadline, so no fifth attempt is allocated.
    assert rc == 75
    assert len(w.submits) == 4
    assert "is ready again after a transient attempt; continue with" in capsys.readouterr().err


def test_start_on_a_failed_or_cancelled_unit_exits_1_without_submitting(world, capsys):
    w = world(["admit"], max_attempts=1)
    w.outcomes["admit"] = ["failed"]
    argv = ["run", "start", "R", "--unit", "U", "--inputs", "s3://deliv/U"]
    assert cli.main(argv) == 1
    assert w.unit("admit", "U")["state"] == "failed"
    capsys.readouterr()
    assert cli.main(argv) == 1
    assert capsys.readouterr().out.splitlines() == ["admit U is failed", "run=R state=failed"]
    w.unit("admit", "U")["state"] = "cancelled"
    assert cli.main(argv) == 1
    assert len(w.submits) == 1


def test_start_waits_on_an_attempt_already_in_flight(world, capsys):
    w = world(["admit"])
    w.polls_to_finish = 3
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d", "--no-wait"]) == 0
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d"]) == 0
    assert len(w.submits) == 1
    out = capsys.readouterr().out
    assert "admit U attempt A1 already in flight" in out
    assert "poll stage=admit unit=U attempt=A1 status=RUNNING" in out


def test_start_no_wait_submits_one_stage_and_prints_the_continue_command(world, capsys):
    w = world(["admit", "register"])
    rc = cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://deliv/U",
                   "--no-wait", "--interval", "5"])
    assert rc == 0
    assert [s["stage"] for s in w.submits] == ["admit"]
    assert w.reconciles == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "attempt=A1 job=job-1 outputs=s3://b/runs/R/admit/U/A1"
    assert out[1] == ("continue: rapidpipe run start R --unit U --inputs s3://deliv/U "
                      "--interval 5")
    assert out[-1] == "run=R state=submitted"


def test_start_timeout_exits_75(world, capsys):
    w = world(["admit"])
    w.polls_to_finish = 10**6
    rc = cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d",
                   "--interval", "30", "--timeout", "60"])
    assert rc == 75
    captured = capsys.readouterr()
    assert captured.out.count("poll stage=admit") == 3
    assert captured.out.splitlines()[-1] == "run=R state=timeout"
    assert "timed out after 60s" in captured.err
    assert "continue with: rapidpipe run start R --unit U --inputs s3://d --timeout 60" in captured.err


def test_start_stage_runs_only_its_first_incomplete_occurrence(world):
    w = world(FULL)
    for stage, unit in (("admit", "U"), ("register", "admit/U"), ("difference", "U")):
        w.complete(stage, unit)
    assert cli.main(["run", "start", "R", "--unit", "U", "--stage", "register"]) == 0
    assert [(s["stage"], s["unit"]) for s in w.submits] == [("register", "difference/U")]


def test_start_stage_with_incomplete_producer_is_refused(world, capsys):
    world(FULL)
    assert cli.main(["run", "start", "R", "--unit", "U", "--stage", "difference"]) == 64
    assert "is not complete" in capsys.readouterr().err


def test_start_keyed_inputs_beat_producer_resolution(world):
    w = world(["admit", "difference"])
    w.complete("admit", "U")
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "difference=/local/set"]) == 0
    assert w.submits[0]["inputs"] == "/local/set"


def test_start_duplicate_or_unprefixed_template_is_refused(world, capsys):
    world(["admit", "difference"])
    assert cli.main(["run", "start", "R", "--unit", "U", "--template", "s3://t"]) == 64
    assert "expected <stage>=<location>" in capsys.readouterr().err
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "admit=a",
                     "--inputs", "admit=b"]) == 64
    assert "--inputs given twice for stage 'admit'" in capsys.readouterr().err


def test_start_attempt_running_without_a_scheduler_job_exits_64(world, capsys):
    """Allocation committed but submit/record_scheduler_job failed: reconcile
    never resolves it, so start refuses instead of polling to timeout."""
    w = world(["admit"])
    u = w.unit("admit", "U")
    u["attempts"].append({"id": "A9", "disposition": None, "job": None,
                          "out": "s3://b/runs/R/admit/U/A9", "polls": 0})
    u["state"] = "running"
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d"]) == 64
    err = capsys.readouterr().err
    assert "run R stage admit unit U: attempt A9" in err
    assert "no scheduler job" in err and "resolved by hand" in err
    assert w.submits == [] and w.reconciles == 0


def test_start_template_on_register_is_refused(world, monkeypatch, capsys):
    w = world(["admit", "register"])
    monkeypatch.setattr(runctl, "compose_inputs", lambda *a, **k: pytest.fail("composed"))
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d",
                     "--template", "register=s3://t"]) == 64
    assert "--template is refused for register" in capsys.readouterr().err
    assert w.submits == []


def test_inputs_for_register_is_refused(compose_env, capsys):
    rc = cli.main(["run", "inputs", "R", "register", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"])])
    assert rc == 64
    assert "--template is refused for register" in capsys.readouterr().err
    assert compose_env["calls"]["add_unit"] == []


def test_start_batch_error_exits_75(world, monkeypatch, capsys):
    world(["admit"])

    def _boom(*_a, **_k):
        raise FakeClientError("ThrottlingException", "slow down")

    monkeypatch.setattr(launch_batch, "submit_unit", _boom)
    assert cli.main(["run", "start", "R", "--unit", "U", "--inputs", "s3://d"]) == 75
    assert "AWS error" in capsys.readouterr().err


# ======================================================================
# run status
# ======================================================================

def _status_setup(monkeypatch, rows_sequence, *, reconcile=None):
    monkeypatch.setattr(runctl, "_run_row",
                        lambda conn, run_id: runctl.RunRow("scratch", [], "open", None)
                        if run_id == "R" else None)
    seq = list(rows_sequence)
    monkeypatch.setattr(runctl, "_status_rows",
                        lambda conn, run_id: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(launch_batch, "reconcile", reconcile or (lambda conn, *, run_id: []))
    monkeypatch.setattr(runctl, "sleep", lambda s: None)


ROW_DONE = ("admit", "U", "complete", "A1", "A1", "job-1", "succeeded")
ROW_RUNNING = ("register", "admit/U", "running", None, "A2", "job-2", None)
ROW_FAILED = ("register", "admit/U", "failed", None, "A2", "job-2", "failed")


@pytest.mark.parametrize("rows, code", [
    ([ROW_DONE], 0), ([ROW_DONE, ROW_FAILED], 1), ([ROW_DONE, ROW_RUNNING], 2), ([], 2)])
def test_status_exit_codes(monkeypatch, fake_conn, capsys, rows, code):
    _status_setup(monkeypatch, [rows])
    assert cli.main(["run", "status", "R"]) == code
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "stage\tunit\tstate\tselected_attempt\tlast_attempt\tlast_job\tdisposition"
    assert len(out) == 1 + len(rows)
    if rows:
        assert out[1].split("\t")[0] == rows[0][0]


def test_status_watch_repeats_until_terminal(monkeypatch, fake_conn, capsys):
    _status_setup(monkeypatch, [[ROW_DONE, ROW_RUNNING], [ROW_DONE, ROW_RUNNING], [ROW_DONE]])
    assert cli.main(["run", "status", "R", "--watch", "--interval", "1"]) == 0
    assert capsys.readouterr().out.count("stage\tunit\tstate") == 3


def test_status_unknown_run_and_batch_error(monkeypatch, fake_conn, capsys):
    def _boom(conn, *, run_id):
        raise FakeClientError("AccessDenied", "no")

    _status_setup(monkeypatch, [[ROW_DONE]], reconcile=_boom)
    assert cli.main(["run", "status", "NOPE"]) == 64
    assert cli.main(["run", "status", "R"]) == 75
    assert "AWS error" in capsys.readouterr().err


# ======================================================================
# run compare
# ======================================================================

def _compare_setup(monkeypatch, units, instances, overlays=("o", "o")):
    runs = {"A": runctl.RunRow("scratch", [], "open", overlays[0]),
            "B": runctl.RunRow("scratch", [], "open", overlays[1])}
    monkeypatch.setattr(runctl, "_run_row", lambda conn, run_id: runs.get(run_id))
    monkeypatch.setattr(runctl, "_compare_units", lambda conn, run_id: units[run_id])
    monkeypatch.setattr(runctl, "_compare_instances", lambda conn, run_id: instances[run_id])


def test_compare_same(monkeypatch, fake_conn, capsys):
    _compare_setup(
        monkeypatch,
        {"A": [("admit", "U", "succeeded", "h1")], "B": [("admit", "U", "succeeded", "h1")]},
        {"A": [("l2-image", '{"u": 1}', "IA")], "B": [("l2-image", '{"u": 1}', "IB")]})
    assert cli.main(["run", "compare", "A", "B"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "unit\tadmit\tU\tsucceeded\tsucceeded\tsettings\th1\th1" in out
    assert 'instance\tl2-image\t{"u": 1}\tIA\tIB' in out
    assert out[-1] == "same"


@pytest.mark.parametrize("units_b, instances_b, overlays", [
    ([("admit", "U", "failed", "h1")], [("l2-image", '{"u": 1}', "IB")], ("o", "o")),
    ([("admit", "U", "succeeded", "h2")], [("l2-image", '{"u": 1}', "IB")], ("o", "o")),
    ([("admit", "U", "succeeded", "h1")], [], ("o", "o")),
    ([("admit", "U", "succeeded", "h1")], [("l2-image", '{"u": 1}', "IB")], ("o", "p")),
])
def test_compare_different(monkeypatch, fake_conn, capsys, units_b, instances_b, overlays):
    _compare_setup(
        monkeypatch,
        {"A": [("admit", "U", "succeeded", "h1")], "B": units_b},
        {"A": [("l2-image", '{"u": 1}', "IA")], "B": instances_b}, overlays)
    assert cli.main(["run", "compare", "A", "B"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "different"


def test_compare_run_not_found(monkeypatch, fake_conn, capsys):
    _compare_setup(monkeypatch, {}, {})
    assert cli.main(["run", "compare", "A", "Z"]) == 64
    assert "no such run: Z" in capsys.readouterr().err


# ======================================================================
# run inputs
# ======================================================================

def _entry(kind, instance, base: Path, files: dict[str, bytes], role="image"):
    members = []
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        members.append(member_for_file(role, path, relative_to=base))
    return OutputEntry(kind=kind, format_version="1", instance=instance, key={"k": instance},
                       members=tuple(members), primary=members[0].path)


def _write(manifest: Manifest, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest.write(directory / "manifest.json")


@pytest.fixture()
def compose_env(tmp_path, monkeypatch, fake_conn):
    template = tmp_path / "template"
    producer = tmp_path / "producer"
    _write(Manifest(
        run="REFRUN", unit=Unit("detector-image", "U"), stage="input-set", attempt="REFATT",
        execution_record="exec/input-set.json",
        inputs=Inputs(manifest="input-set"),
        outputs=(
            _entry("l2-image", "OLDL2", template, {"l2/old.fits": b"old"}),
            _entry("reference-image", "REF1", template, {"ref/image.fits": b"reference!"}),
            _entry("psf", "PSF1", template, {"psf/sci.fits": b"psf"}, role="psf"),
        )), template)
    _write(Manifest(
        run="R", unit=Unit("detector-image", "U"), stage="admit", attempt="ADMITATT",
        execution_record="exec/admit.json", inputs=Inputs(manifest="delivery"),
        outputs=(_entry("l2-image", "L2NEW", producer, {"deep/dir/science.fits": b"science"}),),
    ), producer)

    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", str(tmp_path / "outputs"))
    monkeypatch.delenv("RAPIDPIPE_OUTPUTS_ROOT", raising=False)
    calls = {"add_unit": [], "bind": []}
    monkeypatch.setattr(runctl, "_run_row",
                        lambda conn, run_id: runctl.RunRow("scratch", [], "open", None)
                        if run_id == "R" else None)
    monkeypatch.setattr(launch_batch, "resolve_inputs_from_stage",
                        lambda conn, *, run_id, unit_id, upstream_stage: str(producer))
    monkeypatch.setattr(repository, "add_unit",
                        lambda conn, run_id, stage, kind, unit_id:
                            calls["add_unit"].append((run_id, stage, kind, unit_id)))
    monkeypatch.setattr(repository, "bind_unit_inputs",
                        lambda conn, run_id, stage, unit_id, instances:
                            calls["bind"].append((run_id, stage, unit_id, list(instances))))
    monkeypatch.setattr(runctl, "_registered_instances",
                        lambda conn, ids: [i for i in ids if i in ("L2NEW", "REF1")])
    return {"template": template, "producer": producer, "calls": calls, "tmp": tmp_path,
            "dest": tmp_path / "outputs/runs/R/inputs/difference/U"}


def test_inputs_composes_a_local_input_set(compose_env, fake_conn, capsys):
    dest = compose_env["dest"]
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"]), "--dest", str(dest)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == f"inputs={dest}"
    manifest = Manifest.read(dest / "manifest.json")
    assert manifest.run == "R"
    assert manifest.stage == "input-set"
    assert manifest.attempt == "ADMITATT"
    assert manifest.unit == Unit("detector-image", "U")
    assert manifest.execution_record == "exec/input-set.json"
    assert manifest.inputs.manifest == "input-set"
    kinds = [o.kind for o in manifest.outputs]
    assert kinds == ["l2-image", "reference-image", "psf"]
    l2 = manifest.outputs[0]
    assert l2.instance == "L2NEW"
    assert l2.primary == "l2/science.fits"
    assert [m.path for m in l2.members] == ["l2/science.fits"]
    assert (dest / "l2/science.fits").read_bytes() == b"science"
    assert (dest / "ref/image.fits").read_bytes() == b"reference!"
    assert (dest / "psf/sci.fits").read_bytes() == b"psf"
    assert not (dest / "l2/old.fits").exists()
    calls = compose_env["calls"]
    assert calls["add_unit"] == [("R", "difference", "detector-image", "U")]
    assert calls["bind"] == [("R", "difference", "U", ["L2NEW", "REF1"])]
    assert fake_conn.committed == 1


def test_inputs_refuses_to_overwrite_an_existing_manifest(compose_env, capsys):
    dest = compose_env["dest"]
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text("{}")
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"]), "--dest", str(dest)])
    assert rc == 64
    assert "refusing to overwrite" in capsys.readouterr().err


def test_inputs_size_mismatch_exits_1(compose_env, capsys):
    (compose_env["template"] / "ref/image.fits").write_bytes(b"short")
    dest = compose_env["dest"]
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"]), "--dest", str(dest)])
    assert rc == 1
    assert "its manifest says 10" in capsys.readouterr().err
    assert not (dest / "manifest.json").exists()


def test_inputs_missing_template_manifest_exits_64(compose_env, capsys):
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["tmp"] / "nowhere"),
                   "--dest", str(compose_env["dest"])])
    assert rc == 64
    assert "no manifest.json at" in capsys.readouterr().err


def test_inputs_default_dest_is_the_scratch_root_for_every_run_kind(
        compose_env, monkeypatch, capsys):
    monkeypatch.setattr(runctl, "_run_row",
                        lambda conn, run_id: runctl.RunRow("production", [], "open", None))
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", str(compose_env["tmp"] / "products"))
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"])])
    assert rc == 0
    assert (compose_env["dest"] / "manifest.json").exists()
    assert not (compose_env["tmp"] / "products").exists()


@pytest.mark.parametrize("dest", ["elsewhere", "outputs/runs/R/inputs",
                                  "outputs/runs/OTHER/inputs/x", "outputs/runs/R/difference/U"])
def test_inputs_dest_outside_the_run_inputs_root_is_refused(compose_env, capsys, dest):
    target = compose_env["tmp"] / dest
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"]), "--dest", str(target)])
    assert rc == 64
    assert "is not under" in capsys.readouterr().err
    assert compose_env["calls"]["add_unit"] == []
    assert not (target / "manifest.json").exists()


def test_inputs_admission_fence_fires_before_any_copy(compose_env, monkeypatch, capsys):
    def _refuse(*_a, **_k):
        raise repository.RunDeletingOrDeleted("run 'R' is 'deleting'; it admits no new work")

    monkeypatch.setattr(repository, "add_unit", _refuse)
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", str(compose_env["template"])])
    assert rc == 64
    assert "admits no new work" in capsys.readouterr().err
    assert not compose_env["dest"].exists()


def test_inputs_over_s3_copies_server_side(compose_env, monkeypatch, capsys):
    s3 = FakeS3()
    for name in ("template", "producer"):
        base = compose_env[name]
        for path in base.rglob("*"):
            if path.is_file():
                s3.seed("src", f"{name}/{path.relative_to(base).as_posix()}", path.read_bytes())
    monkeypatch.setattr(launch_batch, "resolve_inputs_from_stage",
                        lambda conn, **kw: "s3://src/producer")
    from rapidpipe.products import storage

    monkeypatch.setattr(storage, "s3_client", lambda: s3)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", "s3://dst/root")
    dest = "s3://dst/root/runs/R/inputs/set"
    rc = cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                   "--template", "s3://src/template", "--dest", dest])
    assert rc == 0
    copies = [key for op, key in s3.calls if op == "copy_object"]
    assert sorted(copies) == ["root/runs/R/inputs/set/l2/science.fits",
                             "root/runs/R/inputs/set/psf/sci.fits",
                             "root/runs/R/inputs/set/ref/image.fits"]
    assert [op for op, key in s3.calls if key == "root/runs/R/inputs/set/manifest.json"][-1] == "upload_file"
    written = json.loads(s3._objects[("dst", "root/runs/R/inputs/set/manifest.json")])
    assert written["outputs"][0]["primary"] == "l2/science.fits"

    # A second compose into the same prefix is refused.
    assert cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage", "admit",
                     "--template", "s3://src/template", "--dest", dest]) == 64


def test_inputs_binds_and_commits_before_the_manifest_is_written(
        compose_env, monkeypatch, fake_conn, capsys):
    """A manifest write that fails leaves the bindings committed and no
    manifest, so a retry composes again rather than reusing an unbound set."""
    dest = compose_env["dest"]
    real_write = runctl._Storage.write_manifest
    seen = {}

    def _fail_once(self, manifest, location):
        if not seen:
            seen["bind_at_write"] = list(compose_env["calls"]["bind"])
            seen["committed_at_write"] = fake_conn.committed
            raise OSError("disk went away")
        return real_write(self, manifest, location)

    monkeypatch.setattr(runctl._Storage, "write_manifest", _fail_once)
    kw = dict(run_id="R", stage="difference", unit_id="U", from_stage="admit",
              template=str(compose_env["template"]), dest=str(dest))
    with pytest.raises(OSError):
        runctl.compose_inputs(fake_conn, **kw)
    assert seen["bind_at_write"] == [("R", "difference", "U", ["L2NEW", "REF1"])]
    assert seen["committed_at_write"] == 1
    assert not (dest / "manifest.json").exists()
    assert runctl.compose_inputs(fake_conn, **kw) == str(dest)
    assert (dest / "manifest.json").exists()


def test_start_reuse_of_an_existing_manifest_rebinds_and_commits(
        compose_env, fake_conn, capsys):
    """A manifest left without its bindings (the pre-fix ordering, or a
    rolled-back bind) is re-bound when ``run start`` reuses it."""
    dest = compose_env["dest"]
    assert cli.main(["run", "inputs", "R", "difference", "--unit", "U", "--from-stage",
                     "admit", "--template", str(compose_env["template"]),
                     "--dest", str(dest)]) == 0
    calls = compose_env["calls"]
    calls["add_unit"].clear()
    calls["bind"].clear()          # as if the first bind had been rolled back
    committed = fake_conn.committed
    out = runctl.compose_inputs(
        fake_conn, run_id="R", stage="difference", unit_id="U", from_stage="admit",
        template=str(compose_env["template"]), dest=str(dest), reuse_existing=True)
    assert out == str(dest)
    assert calls["add_unit"] == [("R", "difference", "detector-image", "U")]
    assert calls["bind"] == [("R", "difference", "U", ["L2NEW", "REF1"])]
    assert fake_conn.committed == committed + 1
    assert "(already composed)" in capsys.readouterr().out


# ======================================================================
# run expire, run delete and the cleanup role; run create --seed
# ======================================================================

class _FakeSTS:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.durations = []

    def assume_role(self, *, RoleArn, RoleSessionName, DurationSeconds=None):
        self.calls.append((RoleArn, RoleSessionName))
        self.durations.append(DurationSeconds)
        if self.fail:
            raise FakeClientError("AccessDenied", "not allowed")
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK",
                                "SessionToken": "ST"}}


class _SweepConn(_Conn):
    """expire_runs's candidate listing returns ``rows``."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


@pytest.fixture()
def fake_boto3(monkeypatch):
    made = {"sts": _FakeSTS(), "s3": []}

    def _client(service, **kwargs):
        if service == "sts":
            return made["sts"]
        client = object()
        made["s3"].append((client, kwargs))
        return client

    monkeypatch.setattr(cleanup, "_boto3_client", _client)
    monkeypatch.setattr(cleanup.getpass, "getuser", lambda: "tester")
    return made


def test_cleanup_s3_client_unset_is_none(monkeypatch, fake_boto3):
    monkeypatch.delenv("RAPIDPIPE_CLEANUP_ROLE_ARN", raising=False)
    assert cleanup.cleanup_s3_client() is None
    assert fake_boto3["sts"].calls == []


def test_cleanup_s3_client_assumes_the_role(monkeypatch, fake_boto3):
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn:example:role/rapid-cleanup")
    client = cleanup.cleanup_s3_client()
    assert fake_boto3["sts"].calls == [
        ("arn:example:role/rapid-cleanup", "rapidpipe-cleanup-tester")]
    assert fake_boto3["sts"].durations == [3600]
    assert fake_boto3["s3"] == [(client, {"aws_access_key_id": "AK",
                                          "aws_secret_access_key": "SK",
                                          "aws_session_token": "ST"})]


def test_cleanup_session_name_is_truncated_to_64(monkeypatch, fake_boto3):
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn")
    monkeypatch.setattr(cleanup.getpass, "getuser", lambda: "u" * 100)
    cleanup.cleanup_s3_client()
    assert len(fake_boto3["sts"].calls[0][1]) == 64


def test_expire_prints_one_report_per_run_with_the_cleanup_client(
        monkeypatch, fake_conn, fake_boto3, capsys):
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn")
    seen = {}

    def _expire_runs(conn, *, now=None, s3_client_factory=None, scratch_bucket=None):
        seen.update(now=now, s3_client=s3_client_factory())
        return [cleanup.DeletionReport(run_id="R1", objects_deleted=2),
                cleanup.DeletionReport(run_id="R2", refused="pinned meanwhile")]

    monkeypatch.setattr(cleanup, "expire_runs", _expire_runs)
    assert cli.main(["run", "expire", "--now", "2026-10-01T00:00:00+00:00"]) == 0
    assert seen["now"].isoformat() == "2026-10-01T00:00:00+00:00"
    assert seen["s3_client"] is fake_boto3["s3"][0][0]
    out = capsys.readouterr().out.splitlines()
    assert "run_id: R1" in out and "run_id: R2" in out
    assert "refused: pinned meanwhile" in out
    assert out[-1] == "expired=1 refused=1"
    assert fake_conn.committed == 1


def test_expire_sweep_assumes_the_role_once_per_run(monkeypatch, fake_boto3):
    """Each expired run gets fresh credentials: the fake STS is called once
    per run in a sweep of two, and each run gets its own client."""
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn")
    conn = _SweepConn([("R1", "o"), ("R2", "o")])
    clients = []

    def _delete_run(conn, run_id, requested_by, *, s3_client=None, scratch_bucket=None,
                    expiry=False):
        clients.append(s3_client)
        return cleanup.DeletionReport(run_id=run_id)

    monkeypatch.setattr(cleanup, "delete_run", _delete_run)
    reports = cleanup.expire_runs(
        conn, s3_client_factory=cleanup.cleanup_s3_client_factory())
    assert [r.run_id for r in reports] == ["R1", "R2"]
    assert len(fake_boto3["sts"].calls) == 2
    assert clients == [made for made, _ in fake_boto3["s3"]]
    assert clients[0] is not clients[1]


def test_expire_sweep_records_a_role_failure_after_the_first_run(monkeypatch, fake_boto3):
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn")
    conn = _SweepConn([("R1", "o"), ("R2", "o")])
    monkeypatch.setattr(cleanup, "delete_run",
                        lambda conn, run_id, *a, **k: cleanup.DeletionReport(run_id=run_id))
    factory = cleanup.cleanup_s3_client_factory()
    fake_boto3["sts"].fail = True
    reports = cleanup.expire_runs(conn, s3_client_factory=factory)
    assert reports[0].refused is None
    assert "could not assume the cleanup role" in reports[1].refused


def test_cleanup_s3_client_factory_unset_is_none(monkeypatch, fake_boto3):
    monkeypatch.delenv("RAPIDPIPE_CLEANUP_ROLE_ARN", raising=False)
    assert cleanup.cleanup_s3_client_factory() is None



    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "expire", "--now", "yesterday"])
    assert exc.value.code == 2


def test_expire_and_delete_role_failure_exit_75(monkeypatch, fake_conn, fake_boto3, capsys):
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn")
    fake_boto3["sts"].fail = True
    monkeypatch.setattr(cleanup, "expire_runs", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(cleanup, "delete_run", lambda *a, **k: pytest.fail("must not run"))
    assert cli.main(["run", "expire"]) == 75
    assert cli.main(["run", "delete", "R1"]) == 75
    err = capsys.readouterr().err
    assert "could not assume the cleanup role" in err and "AccessDenied" in err


def test_delete_passes_the_cleanup_client(monkeypatch, fake_conn, fake_boto3):
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn")
    seen = {}

    def _delete_run(conn, run_id, requested_by, *, s3_client=None):
        seen["s3_client"] = s3_client
        return cleanup.DeletionReport(run_id=run_id)

    monkeypatch.setattr(cleanup, "delete_run", _delete_run)
    assert cli.main(["run", "delete", "R1", "--requested-by", "me"]) == 0
    assert seen["s3_client"] is fake_boto3["s3"][0][0]


def test_delete_without_the_role_uses_the_default_client(monkeypatch, fake_conn, fake_boto3):
    monkeypatch.delenv("RAPIDPIPE_CLEANUP_ROLE_ARN", raising=False)
    seen = {}

    def _delete_run(conn, run_id, requested_by, *, s3_client=None):
        seen["s3_client"] = s3_client
        return cleanup.DeletionReport(run_id=run_id)

    monkeypatch.setattr(cleanup, "delete_run", _delete_run)
    assert cli.main(["run", "delete", "R1", "--requested-by", "me"]) == 0
    assert seen == {"s3_client": None}


def test_create_seed_is_passed_through(monkeypatch, fake_conn, capsys):
    seen = {}

    def _create_run(conn, **kwargs):
        seen.update(kwargs)
        return "NEWRUN"

    monkeypatch.setattr(repository, "create_run", _create_run)
    monkeypatch.setattr(cli, "_source_revision_or_unknown", lambda: "rev")
    rc = cli.main(["run", "create", "--kind", "scratch", "--purpose", "p", "--stages", "admit",
                   "--seed", "OLDRUN"])
    assert rc == 0
    assert seen["seed_run"] == "OLDRUN"
    assert capsys.readouterr().out.strip() == "NEWRUN"


def test_create_unknown_seed_exits_64(monkeypatch, fake_conn, capsys):
    def _create_run(conn, **kwargs):
        raise repository.RunNotFound(f"seed_run {kwargs['seed_run']!r} does not exist")

    monkeypatch.setattr(repository, "create_run", _create_run)
    monkeypatch.setattr(cli, "_source_revision_or_unknown", lambda: "rev")
    assert cli.main(["run", "create", "--kind", "scratch", "--purpose", "p", "--stages",
                     "admit", "--seed", "NOPE"]) == 64
    assert "seed_run 'NOPE' does not exist" in capsys.readouterr().err
    assert fake_conn.rolled_back == 1
