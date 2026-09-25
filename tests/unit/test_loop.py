"""Unit tests of ``rapidpipe.launch.loop``: the spec, unit-id derivation,
base-set selection, the record, exit codes. Database-free: the module's
SQL helpers and the CLI's tools are replaced."""

from __future__ import annotations

import datetime as dt
import importlib
import json

import pytest

from rapidpipe.launch import loop
from rapidpipe.products.manifest import Inputs, Manifest, OutputEntry, Unit
from rapidpipe.runs import repository
from rapidpipe.stages.contract import STAGE_NAMES
from tests.unit.fakes3 import FakeS3

SPEC = """
[loop]
schedule = "control-loop"
release = "rebuild-v0.3"
kind = "production"
owner = "rusholme"
lane = "prompt"
check_policy = "rebuild-trial@1"
max_attempts = 3

[[dates]]
processing_date = 2027-10-01
[[dates.detector_images]]
delivery = "s3://b/control/delivery/r0034001002001001001-sca01"
admit_settings = "s3://b/settings/admit-socsim.toml"
difference_template = "s3://b/control/step3/P1/inputs"
difference_settings = "s3://b/settings/difference-gain1-imgnoise.toml"

[[dates]]
processing_date = 2027-10-02
[[dates.detector_images]]
delivery = "s3://b/control/delivery/r0034001002001001001-sca01"
difference_template = "s3://b/control/step3/P1/inputs"
unit = "custom/SCA01"
"""


# ======================================================================
# The spec
# ======================================================================

def test_parse_spec_reads_every_field():
    spec = loop.parse_spec(SPEC, "s3://b/loop.toml")
    assert (spec.schedule, spec.release, spec.kind, spec.owner, spec.lane, spec.check_policy,
            spec.max_attempts, spec.profile, spec.location) == (
        "control-loop", "rebuild-v0.3", "production", "rusholme", "prompt",
        "rebuild-trial@1", 3, "batch", "s3://b/loop.toml")
    assert [d.processing_date for d in spec.dates] == [dt.date(2027, 10, 1), dt.date(2027, 10, 2)]
    first, second = spec.dates[0].detector_images[0], spec.dates[1].detector_images[0]
    assert first.unit == "r0034001002001001001/SCA01"
    assert first.admit_settings.endswith("admit-socsim.toml")
    assert (second.unit, second.admit_settings, second.difference_settings) == (
        "custom/SCA01", None, None)


def test_parse_spec_check_policy_is_optional():
    spec = loop.parse_spec(SPEC.replace('check_policy = "rebuild-trial@1"\n', ""), "x")
    assert spec.check_policy is None


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s.replace("[loop]", "[other]"), "no [loop] table"),
    (lambda s: s.replace('kind = "production"', 'kind = "scratch"'), "kind must be"),
    (lambda s: s.replace("max_attempts = 3", "max_attempts = 0"), "max_attempts"),
    (lambda s: s.replace("max_attempts = 3", "max_attempts = true"), "max_attempts"),
    (lambda s: s.replace('release = "rebuild-v0.3"\n', ""), "'release'"),
    (lambda s: s.replace("2027-10-02", "2027-10-01"), "appears twice"),
    (lambda s: s.replace("processing_date = 2027-10-02", 'processing_date = "2027-10-02"'),
     "TOML date"),
    (lambda s: s.replace("processing_date = 2027-10-02", "processing_date = 2027-10-02T00:00:00"),
     "TOML date"),
    (lambda s: s.replace('delivery = "s3://b/control/delivery/r0034001002001001001-sca01"\n'
                         'difference_template', "difference_template"), "'delivery'"),
    (lambda s: s.split("[[dates]]")[0], "no [[dates]]"),
    (lambda s: s + "[[dates.detector_images]]\ndelivery = \"s3://b/d/x-sca01\"\n"
                   "difference_template = \"t\"\nunit = \"custom/SCA01\"\n", "share a unit id"),
    (lambda s: s + "=", "not valid TOML"),
])
def test_parse_spec_refuses_a_malformed_spec(mutate, message):
    with pytest.raises(loop.LoopSpecError, match=message.replace("[", r"\[")):
        loop.parse_spec(mutate(SPEC), "x")


def test_read_spec_text_from_s3_and_local(tmp_path):
    s3 = FakeS3()
    s3.seed("b", "control/loop/control-loop.toml", SPEC.encode())
    assert loop.load_spec("s3://b/control/loop/control-loop.toml", s3_client=s3).schedule == \
        "control-loop"
    path = tmp_path / "spec.toml"
    path.write_text(SPEC)
    assert loop.load_spec(str(path)).release == "rebuild-v0.3"
    with pytest.raises(loop.LoopSpecError, match="no spec at"):
        loop.load_spec("s3://b/missing.toml", s3_client=s3)
    with pytest.raises(loop.LoopSpecError, match="cannot read spec"):
        loop.load_spec(str(tmp_path / "missing.toml"))


# ======================================================================
# Unit ids
# ======================================================================

@pytest.mark.parametrize("delivery, unit", [
    ("s3://b/delivery/r0034001002001001001-sca01", "r0034001002001001001/SCA01"),
    ("s3://b/delivery/r0034001002001001001-SCA17/", "r0034001002001001001/SCA17"),
    ("s3://b/delivery/r0034001002001001001_sca03", "r0034001002001001001/SCA03"),
    ("/local/deliveries/plain", "plain"),
])
def test_detector_unit_id(delivery, unit):
    assert loop.detector_unit_id(delivery) == unit


@pytest.mark.parametrize("table, unit", [
    ("sources_20271001_1", "20271001/SCA01"),
    ("sources_20271001_17", "20271001/SCA17"),
    ("sources_20271001_01", "20271001/SCA01"),
])
def test_maintain_unit_id(table, unit):
    assert loop.maintain_unit_id(table) == unit


@pytest.mark.parametrize("table", ["", "sources_2027101_01", "diffimages", "sources_20271001_x",
                                   "sources_20271001_01; DROP TABLE runs"])
def test_maintain_unit_id_refuses_other_names(table):
    with pytest.raises(loop.LoopError):
        loop.maintain_unit_id(table)


def test_source_set_fields_refuses_a_bad_table_before_any_sql():
    with pytest.raises(loop.LoopError):
        loop.source_set_fields(object(), "runs", "I")


def test_selected_stages_and_positions_match_the_stage_names():
    assert set(loop.SELECTED_STAGES) <= set(STAGE_NAMES)
    # A1: the raw difference is never registered; register follows finalize.
    assert loop.SELECTED_STAGES == (
        "admit", "register", "difference", "finalize", "register", "load", "maintain",
        "crossmatch", "statistics", "prune", "alerts")
    assert [loop.SELECTED_STAGES[p] for p in loop.IMAGE_CHAIN] == [
        "admit", "register", "difference", "finalize", "register", "load"]
    assert [loop.SELECTED_STAGES[p] for p in (loop.MAINTAIN, loop.CROSSMATCH, loop.STATISTICS,
                                              loop.PRUNE, loop.ALERTS)] == [
        "maintain", "crossmatch", "statistics", "prune", "alerts"]


@pytest.mark.parametrize("stage", sorted(loop.UNIT_KINDS))
def test_unit_kinds_match_the_stage_declarations(stage):
    declaration = importlib.import_module(f"rapidpipe.stages.{stage}").DECLARATION
    assert loop.UNIT_KINDS[stage] == declaration.unit


# ======================================================================
# Base-set selection
# ======================================================================

def _entry(kind, instance, key=None, **registration):
    return OutputEntry(kind=kind, format_version="1", instance=instance,
                       key=key or {"x": 1}, registration=registration)


def _manifest(outputs, unit=Unit(kind="field", id="5")):
    return Manifest(run="R0", unit=unit, stage="crossmatch", attempt="A0",
                    execution_record="exec/A0.json", inputs=Inputs(manifest="m"),
                    outputs=tuple(outputs))


class _Storage:
    def __init__(self, manifests=None):
        self.manifests = dict(manifests or {})
        self.written: dict[str, Manifest] = {}
        self.copies: list[tuple[str, str]] = []

    def read_manifest(self, location):
        if location in self.written:
            return self.written[location]
        return self.manifests[location]

    def exists(self, location, relative):
        return f"s3://{location.bucket}/{location.prefix}" in self.written

    def write_manifest(self, manifest, location):
        self.written[f"s3://{location.bucket}/{location.prefix}"] = manifest

    def copy(self, src, src_rel, dst, dst_rel):
        self.copies.append((f"{src.prefix}/{src_rel}", f"{dst.prefix}/{dst_rel}"))

    def size(self, location, relative):
        return 4


def _row(date="2027-10-01", run="RUN1", state="complete", record=None):
    return loop.LoopRow("control-loop", dt.date.fromisoformat(date), run, state, None, None,
                        None, record or {})


def test_base_entry_is_none_on_the_first_date(monkeypatch):
    monkeypatch.setattr(loop, "base_instance", lambda *a: pytest.fail("no lookup"))
    assert loop.base_entry(object(), _Storage(), None, 5) is None


def test_base_entry_is_none_when_the_previous_run_did_not_cross_match_the_field(monkeypatch):
    monkeypatch.setattr(loop, "base_instance", lambda conn, run, field: None)
    assert loop.base_entry(object(), _Storage(), _row(), 5) is None


def test_base_entry_is_the_previous_runs_selected_association_set(monkeypatch):
    seen = {}

    def base_instance(conn, run, field):
        seen["args"] = (run, field)
        return "AS1", "s3://b/runs/RUN1/crossmatch/5/A1"

    monkeypatch.setattr(loop, "base_instance", base_instance)
    entry = _entry("association-set", "AS1", {"field": 5, "base": None})
    storage = _Storage({"s3://b/runs/RUN1/crossmatch/5/A1": _manifest(
        [_entry("association-set", "OTHER", {"field": 5}), entry])})
    assert loop.base_entry(object(), storage, _row(run="RUN1"), 5) == entry
    assert seen["args"] == ("RUN1", "5")


def test_base_entry_refuses_a_set_for_another_field(monkeypatch):
    monkeypatch.setattr(loop, "base_instance", lambda *a: ("AS1", "s3://b/loc"))
    storage = _Storage({"s3://b/loc": _manifest([_entry("association-set", "AS1",
                                                        {"field": 6})])})
    with pytest.raises(loop.LoopError, match="not 5"):
        loop.base_entry(object(), storage, _row(), 5)


def test_crossmatch_inputs_carry_source_sets_then_the_base():
    sources = [_entry("source-set", "S1", table="sources_20271001_01")]
    base = _entry("association-set", "AS1", {"field": 5})
    manifest = loop.crossmatch_inputs("RUN2", 5, sources, base)
    assert [o.instance for o in manifest.outputs] == ["S1", "AS1"]
    assert manifest.inputs.result_sets == ("S1", "AS1")
    assert (manifest.unit, manifest.stage) == (Unit(kind="field", id="5"), "input-set")
    manifest.validate()
    assert loop.crossmatch_inputs("RUN1", 5, sources, None).inputs.result_sets == ("S1",)


# ======================================================================
# One date, database-free: the record's shape
# ======================================================================

class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _world(monkeypatch, *, previous=None, walk_rc=None):
    spec = loop.parse_spec(SPEC, "s3://b/loop.toml")
    img = spec.dates[0].detector_images[0]
    source = _entry("source-set", "S1", {"difference": "DI1"}, table="sources_20271001_01")
    diff = OutputEntry(kind="difference-image", format_version="1", instance="DI1",
                       key={"u": 1}, members=(), registration={})
    refcat = _entry("reference-catalog", "RC1")
    outputs = {
        "load": [source], "finalize": [diff], "crossmatch": [_entry("association-set", "AS2",
                                                                     {"field": 5})],
        "statistics": [_entry("statistics-set", "ST2")], "alerts": [_entry("alert-container",
                                                                          "AC2")]}
    manifests = {f"s3://b/out/{stage}": _manifest(o) for stage, o in outputs.items()}
    manifests[img.difference_template] = _manifest([refcat])
    storage = _Storage(manifests)
    walks: list[tuple[str, list[int], list[str]]] = []

    def walk(conn, *, run_id, unit_id, positions, inputs, settings, templates, interval,
             timeout, continue_hint):
        walks.append((unit_id, positions, inputs, settings, templates))
        assert continue_hint.startswith("rapidpipe loop run --spec s3://b/loop.toml --date ")
        return walk_rc(positions) if walk_rc else 0

    created = {}

    def create_run(conn, **kwargs):
        created.update(kwargs)
        return "RUN2"

    updates = {}
    monkeypatch.setattr(loop, "loop_row", lambda conn, s, d: None)
    monkeypatch.setattr(loop, "_insert_row", lambda conn, s, d, run, record: None)
    monkeypatch.setattr(loop, "_update_row",
                        lambda conn, s, d, **kw: updates.update(kw, record=json.loads(
                            json.dumps(kw["record"], default=str))))
    monkeypatch.setattr(loop, "selected_output",
                        lambda conn, run, stage, unit: f"s3://b/out/{stage}")
    monkeypatch.setattr(loop, "source_set_fields", lambda conn, table, instance: [5])
    monkeypatch.setattr(loop, "previous_complete_rows",
                        lambda conn, s, d: [] if previous is None else [previous])
    monkeypatch.setattr(loop, "base_entry", lambda conn, st, prev, f: (
        None if prev is None else _entry("association-set", "AS1", {"field": f})))
    monkeypatch.setattr(loop, "run_promotion", lambda conn, run: None)
    monkeypatch.setattr(loop, "run_state", lambda conn, run: "open")
    monkeypatch.setattr(loop, "jobless_attempts", lambda conn, run: [])
    monkeypatch.setattr(loop, "unit_records", lambda conn, run: [
        {"stage": "admit", "unit": img.unit, "state": "complete", "attempt": "A", "job": "j"}])
    monkeypatch.setattr(loop, "_registered", lambda conn, ids: [])
    monkeypatch.setattr(repository, "add_unit", lambda *a, **k: None)
    monkeypatch.setattr(repository, "bind_unit_inputs", lambda *a, **k: None)
    monkeypatch.setattr(repository, "finish_run", lambda conn, run: None)
    monkeypatch.setattr(loop, "_promote", lambda conn, run, spec, date: (
        "P1", "P1", "check policy rebuild-trial@1", []))
    tools = loop.LoopTools(walk=walk, create_run=create_run, storage=storage,
                           inputs_root=lambda run: f"s3://b/scratch/runs/{run}/inputs",
                           out=lambda line: None)
    return spec, tools, storage, walks, created, updates


def test_process_date_walks_the_chain_and_records_the_date(monkeypatch):
    spec, tools, storage, walks, created, updates = _world(
        monkeypatch, previous=_row(run="RUN1"))
    rc = loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10)
    assert rc == 0
    assert created["purpose"] == "processing date 2027-10-01 (schedule control-loop)"
    assert (created["release"], created["check_policy_ref"], created["input_selection_ref"],
            created["max_attempts"], created["kind"]) == (
        "rebuild-v0.3", "rebuild-trial@1", "s3://b/loop.toml", 3, "production")
    assert created["stages"] == list(loop.SELECTED_STAGES)

    unit = "r0034001002001001001/SCA01"
    assert [(u, p) for u, p, *_ in walks] == [
        (unit, loop.IMAGE_CHAIN), ("20271001/SCA01", [6]), ("5", [7]), ("5", [8]),
        ("5", [9]), (unit, [10])]
    chain = walks[0]
    assert chain[2] == ["s3://b/control/delivery/r0034001002001001001-sca01"]
    assert chain[3] == ["s3://b/settings/admit-socsim.toml",
                        "difference=s3://b/settings/difference-gain1-imgnoise.toml"]
    assert chain[4] == ["difference=s3://b/control/step3/P1/inputs"]
    assert walks[1][2] == ["s3://b/out/load"]
    assert walks[3][2] == walks[4][2] == ["s3://b/out/crossmatch"]

    xm = storage.written["s3://b/scratch/runs/RUN2/inputs/crossmatch/5"]
    assert [o.instance for o in xm.outputs] == ["S1", "AS1"]
    alerts = storage.written[f"s3://b/scratch/runs/RUN2/inputs/alerts/{unit}"]
    assert [o.kind for o in alerts.outputs] == ["difference-image", "reference-catalog"]
    assert alerts.inputs.result_sets == ("S1", "AS2", "ST2")

    assert updates["state"] == "complete" and updates["promotion"] == "P1"
    record = updates["record"]
    assert record == {
        "spec": "s3://b/loop.toml", "release": "rebuild-v0.3", "run": "RUN2",
        "units": [{"stage": "admit", "unit": unit, "state": "complete", "attempt": "A",
                   "job": "j"}],
        "fields": [5], "base_sets": {"5": "AS1"},
        "bases": {"5": {"run": "RUN1", "processing_date": "2027-10-01", "base_promoted": False}},
        "association_sets": {"5": "AS2"},
        "statistics_sets": {"5": "ST2"},
        "alerts": {unit: {"instance": "AC2", "location": "s3://b/out/alerts"}},
        "promotion": "P1", "promotion_gate": "check policy rebuild-trial@1", "checks": []}


def test_process_date_a_failed_unit_fails_the_date(monkeypatch):
    spec, tools, storage, walks, created, updates = _world(
        monkeypatch, walk_rc=lambda positions: 1 if positions == [loop.STATISTICS] else 0)
    rc = loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10)
    assert rc == 1
    assert updates["state"] == "failed" and updates["promotion"] is None
    assert "statistics 5 did not complete" in updates["record"]["failure"]
    assert [p for _, p, *_ in walks][-1] == [loop.STATISTICS]  # prune, alerts never walked


def test_process_date_a_timeout_leaves_the_row_open_and_exits_75(monkeypatch):
    class Timeout(Exception):
        code = 75

    spec, tools, storage, walks, created, updates = _world(monkeypatch)

    def walk(*a, **k):
        raise Timeout("timed out")

    tools.walk = walk
    with pytest.raises(loop._Stop) as stop:
        loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10)
    assert stop.value.code == 75
    assert updates == {}


# ======================================================================
# Exit codes of the loop
# ======================================================================

def _loop_world(monkeypatch, rows, results):
    spec = loop.parse_spec(SPEC, "x")
    calls = []
    monkeypatch.setattr(loop, "loop_row", lambda conn, s, d: rows.get(str(d)))

    def process(conn, spec, day, tools, *, interval, timeout):
        calls.append(str(day.processing_date))
        result = results[str(day.processing_date)]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(loop, "process_date", process)
    monkeypatch.setattr(loop, "try_lock", lambda conn, schedule: True)
    monkeypatch.setattr(loop, "unlock", lambda conn, schedule: None)
    reopened = []
    monkeypatch.setattr(loop, "reopen_row", lambda conn, s, d: reopened.append(str(d)))
    calls_reopened = reopened
    tools = loop.LoopTools(walk=None, create_run=None, storage=None, inputs_root=None,
                           out=lambda line: None)
    tools.reopened = calls_reopened
    return spec, tools, calls


def test_run_loop_exits_0_when_every_date_completes(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {}, {"2027-10-01": 0, "2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools) == 0
    assert calls == ["2027-10-01", "2027-10-02"]


def test_run_loop_skips_complete_and_resumes_open(monkeypatch):
    spec, tools, calls = _loop_world(
        monkeypatch, {"2027-10-01": _row(), "2027-10-02": _row("2027-10-02", state="open")},
        {"2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools) == 0
    assert calls == ["2027-10-02"]


def test_run_loop_stops_at_the_first_failed_date(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {}, {"2027-10-01": 1, "2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools) == 1
    assert calls == ["2027-10-01"]


def test_run_loop_a_failed_row_stops_before_later_dates(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {"2027-10-01": _row(state="failed")},
                                     {"2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools) == 1
    assert calls == []


def test_run_loop_exits_75_on_a_timeout(monkeypatch):
    spec, tools, calls = _loop_world(
        monkeypatch, {}, {"2027-10-01": loop._Stop(75, "timed out"), "2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools) == 75
    assert calls == ["2027-10-01"]


def test_run_loop_date_filter(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {}, {"2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools, dates=[dt.date(2027, 10, 2)]) == 0
    assert calls == ["2027-10-02"]
    with pytest.raises(loop.LoopSpecError, match="2027-10-09"):
        loop.run_loop(object(), spec, tools, dates=[dt.date(2027, 10, 9)])


def test_run_loop_dry_run_only_plans(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {}, {})
    monkeypatch.setattr(loop, "previous_complete_rows", lambda conn, s, d: [])
    lines = []
    tools.out = lines.append
    assert loop.run_loop(object(), spec, tools, dry_run=True) == 0
    assert calls == []
    assert lines[0].startswith("date=2027-10-01 action=create run=- "
                               "units=r0034001002001001001/SCA01 base=none (first date)")


# ======================================================================
# Promotion
# ======================================================================

class _Recorded:
    def __init__(self, outcome):
        self.id, self.check_name, self.version = "C1", "difference-image-statistics", "1"
        self.instance, self.required, self.outcome = "DI1", True, outcome


def _gate(monkeypatch, *, promote, recorded=("passed",)):
    from rapidpipe.checks import runner

    seen = {}

    class Policy:
        ref = "rebuild-trial@1"

    def resolve(conn, run_id, explicit=None):
        seen["explicit"] = explicit
        return Policy()

    def run_checks(conn, run_id, policy, *, who=None, **_):
        seen["who"] = who
        return [_Recorded(o) for o in recorded]

    monkeypatch.setattr(runner, "resolve_run_policy", resolve)
    monkeypatch.setattr(runner, "run_policy_checks", run_checks)
    monkeypatch.setattr(repository, "promote_run", promote)
    monkeypatch.setattr(loop, "run_promotion", lambda conn, run: None)
    return seen, Policy


def test_promote_runs_the_policys_checks_then_promotes_under_it(monkeypatch):
    got = {}

    def promote_run(conn, run_id, who, reason, *, kinds=None, check_policy=None,
                    allow_unreleased=False):
        got.update(who=who, reason=reason, policy=check_policy.ref)
        return "P1"

    seen, _ = _gate(monkeypatch, promote=promote_run)
    conn = _Conn()
    promotion, text, gate, checks = loop._promote(conn, "R", loop.parse_spec(SPEC, "x"),
                                                  dt.date(2027, 10, 1))
    assert (promotion, text, gate) == ("P1", "P1", "check policy rebuild-trial@1")
    assert checks == [{"id": "C1", "check": "difference-image-statistics@1",
                       "instance": "DI1", "required": True, "outcome": "passed"}]
    assert seen == {"explicit": "rebuild-trial@1", "who": "scheduler"}
    assert got == {"who": "scheduler", "reason": "processing date 2027-10-01",
                   "policy": "rebuild-trial@1"}


def test_promote_records_a_refusal_as_a_science_outcome(monkeypatch):
    def refuse(conn, run_id, who, reason, *, kinds=None, check_policy=None,
               allow_unreleased=False):
        raise repository.PromotionRefused("required check difference-image-statistics failed")

    _gate(monkeypatch, promote=refuse, recorded=("failed",))
    conn = _Conn()
    promotion, text, gate, checks = loop._promote(conn, "R", loop.parse_spec(SPEC, "x"),
                                                  dt.date(2027, 10, 1))
    assert promotion is None and text.startswith("refused: required check")
    assert checks[0]["outcome"] == "failed"
    assert conn.rollbacks == 1


def test_promote_without_the_gate_is_released_image_only(monkeypatch):
    def promote_run(conn, run_id, who, reason, *, kinds=None, allow_unreleased=False):
        return "P2"

    monkeypatch.setattr(repository, "promote_run", promote_run)
    monkeypatch.setattr(loop, "run_promotion", lambda conn, run: None)
    assert loop._promote(_Conn(), "R", loop.parse_spec(SPEC, "x"), dt.date(2027, 10, 1)) == (
        "P2", "P2", "released-image only", [])


def test_promote_a_missing_run_is_not_a_refusal(monkeypatch):
    def missing(conn, run_id, who, reason, *, kinds=None, check_policy=None,
                allow_unreleased=False):
        raise repository.RunNotFound("no run")

    _gate(monkeypatch, promote=missing)
    with pytest.raises(repository.RunNotFound):
        loop._promote(_Conn(), "R", loop.parse_spec(SPEC, "x"), dt.date(2027, 10, 1))


# ======================================================================
# Amendments A2-A5
# ======================================================================

def test_run_loop_exits_75_when_another_loop_holds_the_schedule(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {}, {"2027-10-01": 0, "2027-10-02": 0})
    monkeypatch.setattr(loop, "try_lock", lambda conn, schedule: False)
    lines = []
    tools.out = lines.append
    assert loop.run_loop(object(), spec, tools) == 75
    assert calls == [] and lines == ["another loop holds schedule control-loop"]


def test_run_loop_retry_failed_reopens_the_row_and_resumes(monkeypatch):
    spec, tools, calls = _loop_world(monkeypatch, {"2027-10-01": _row(state="failed")},
                                     {"2027-10-01": 0, "2027-10-02": 0})
    assert loop.run_loop(object(), spec, tools, retry_failed=True) == 0
    assert tools.reopened == ["2027-10-01"]
    assert calls == ["2027-10-01", "2027-10-02"]


def test_base_for_field_walks_back_to_the_newest_date_that_has_the_field(monkeypatch):
    rows = [_row("2027-10-03", run="RUN3"), _row("2027-10-02", run="RUN2"),
            _row("2027-10-01", run="RUN1")]
    found = {"RUN2": ("AS2", "s3://b/RUN2"), "RUN1": ("AS1", "s3://b/RUN1")}
    monkeypatch.setattr(loop, "base_instance", lambda conn, run, field: found.get(run))
    monkeypatch.setattr(loop, "run_promotion",
                        lambda conn, run: "P1" if run == "RUN1" else None)
    storage = _Storage({f"s3://b/{r}": _manifest([_entry("association-set", i,
                                                         {"field": 5})])
                        for r, (i, _) in found.items()})
    base = loop.base_for_field(object(), storage, rows, 5)
    assert (base.entry.instance, base.run, str(base.processing_date), base.promoted) == (
        "AS2", "RUN2", "2027-10-02", False)
    assert loop.base_for_field(object(), storage, rows[2:], 5).promoted is True
    assert loop.base_for_field(object(), storage, [], 5) is None


def test_process_date_a_jobless_attempt_fails_the_date_with_its_id(monkeypatch):
    class Refusal(Exception):
        code = 64

    spec, tools, storage, walks, created, updates = _world(monkeypatch)

    def walk(*a, **k):
        raise Refusal("attempt A9 is running but has no scheduler job")

    tools.walk = walk
    monkeypatch.setattr(loop, "jobless_attempts", lambda conn, run: ["A9"])
    assert loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10) == 1
    assert updates["state"] == "failed"
    assert updates["record"]["jobless_attempt"] == "A9"


def test_process_date_other_refusals_propagate(monkeypatch):
    class Refusal(Exception):
        code = 64

    spec, tools, *_ = _world(monkeypatch)

    def walk(*a, **k):
        raise Refusal("no such run")

    tools.walk = walk
    with pytest.raises(Refusal):
        loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10)


def test_process_date_resumes_a_finished_run_by_completing_the_row(monkeypatch):
    spec, tools, storage, walks, created, updates = _world(monkeypatch)
    monkeypatch.setattr(loop, "loop_row", lambda conn, s, d: _row(run="RUN2", state="open",
                                                                  record={"run": "RUN2"}))
    monkeypatch.setattr(loop, "run_state", lambda conn, run: "finished")
    monkeypatch.setattr(repository, "finish_run", lambda conn, run: pytest.fail("finished"))
    assert loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10) == 0
    assert walks == [] and created == {}
    assert updates["state"] == "complete" and updates["promotion"] == "P1"


def test_promote_reuses_the_runs_existing_promotion(monkeypatch):
    _gate(monkeypatch, promote=lambda *a, **k: pytest.fail("promoted"))
    monkeypatch.setattr(loop, "run_promotion", lambda conn, run: "P0")
    assert loop._promote(_Conn(), "R", loop.parse_spec(SPEC, "x"), dt.date(2027, 10, 1))[0] \
        == "P0"


def test_alert_inputs_follow_the_alerts_stage_rules():
    s1 = _entry("source-set", "S1", {"difference": "DI1"})
    s2 = _entry("source-set", "S2", {"difference": "OTHER"})
    assert loop.alert_source_set([s1, s2], "DI1") is s1
    with pytest.raises(loop.LoopError, match="exactly one"):
        loop.alert_source_set([s2], "DI1")
    assert loop.alert_result_sets(s1, ["AS1", "AS2"], ["ST1"]) == ["S1", "AS1", "AS2", "ST1"]
    with pytest.raises(loop.LoopError, match="no association"):
        loop.alert_result_sets(s1, [], [])
    with pytest.raises(loop.LoopError, match="more statistics"):
        loop.alert_result_sets(s1, ["AS1"], ["ST1", "ST2"])
