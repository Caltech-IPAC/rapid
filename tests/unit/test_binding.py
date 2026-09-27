"""The one input-binding primitive, ``rapidpipe.runs.binding`` (supervisor
step 2, 2026-09-26).

Three groups: the recorder tests (both composers reach the primitive,
exactly once per consumer), the static bypass guard (neither composer can
admit, bind or write an input set around it), and the primitive's own
order and failure behaviour over a fake connection and storage.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rapidpipe.cli import runctl
from rapidpipe.launch import loop
from rapidpipe.products.manifest import Inputs, Manifest, OutputEntry, Unit
from rapidpipe.runs import binding, inputs, repository
from rapidpipe.runs.inputs import InputsRefused
# Shared fixtures and worlds of the composers' own tests.
from tests.unit.test_cli_runctl import compose_env, fake_conn  # noqa: F401 - fixtures
from tests.unit.test_loop import _Conn, _two_image_world

REPO = Path(__file__).resolve().parents[2]


# ======================================================================
# (a) Recorders: every composition goes through bind_input_set
# ======================================================================

def _recorder(monkeypatch):
    calls: list[dict] = []

    def record(conn, storage, **kw):
        calls.append(kw)
        return binding.BoundInputSet(kw["dest"], None, (), (), False)

    monkeypatch.setattr(binding, "bind_input_set", record)
    return calls


@pytest.mark.parametrize("reuse", [False, True])
def test_compose_inputs_makes_one_call_to_the_primitive(
        compose_env, fake_conn, monkeypatch, capsys, reuse):
    calls = _recorder(monkeypatch)
    dest = str(compose_env["dest"])
    assert runctl.compose_inputs(
        fake_conn, run_id="R", stage="difference", unit_id="U", from_stage="admit",
        template=str(compose_env["template"]), dest=dest, reuse_existing=reuse) == dest
    assert len(calls) == 1
    call = calls[0]
    assert (call["run_id"], call["stage"], call["unit_kind"], call["unit_id"]) == (
        "R", "difference", "detector-image", "U")
    assert call["dest"] == dest
    assert call["reuse_existing"] is reuse
    assert callable(call["compose"])
    assert capsys.readouterr().out.strip() == f"inputs={dest}"


def test_the_loops_three_sites_each_make_one_call_per_consumer(monkeypatch):
    # _two_image_world: two images whose source sets share one maintain unit
    # from two load outputs (so maintain composes), two fields, two images.
    spec, tools, storage, walks, created, updates, units = _two_image_world(monkeypatch)
    calls = _recorder(monkeypatch)
    assert loop.process_date(_Conn(), spec, spec.dates[0], tools, interval=1, timeout=10) == 0
    root = "s3://b/scratch/runs/RUN2/inputs"
    seen = [(c["run_id"], c["stage"], c["unit_kind"], c["unit_id"], c["dest"]) for c in calls]
    assert seen == [
        ("RUN2", "maintain", "detector-date", "20271001/SCA01", f"{root}/maintain/20271001/SCA01"),
        ("RUN2", "crossmatch", "field", "5", f"{root}/crossmatch/5"),
        ("RUN2", "crossmatch", "field", "6", f"{root}/crossmatch/6"),
        ("RUN2", "alerts", "detector-image", units[0], f"{root}/alerts/{units[0]}"),
        ("RUN2", "alerts", "detector-image", units[1], f"{root}/alerts/{units[1]}"),
    ]
    assert all(c.get("reuse_existing", True) is True for c in calls)
    # The returned location is what each consumer walks with.
    walked = {u: i for u, _, i, *_ in walks}
    assert walked["20271001/SCA01"] == [f"{root}/maintain/20271001/SCA01"]
    assert walked[units[1]] == [f"{root}/alerts/{units[1]}"]
    # Nothing was copied or written around the (recording) primitive.
    assert storage.copies == [] and storage.written == {}


# ======================================================================
# (b) The static bypass guard
# ======================================================================

def _calls_named(tree: ast.AST, name: str) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                lines.append(node.lineno)
    return lines


def test_no_composer_admits_binds_or_writes_around_the_primitive():
    """Step 2's done condition: both composers (``run inputs``/``run start``
    and the loop's maintain, crossmatch and alerts sites) bind and write an
    input set only through ``binding.bind_input_set``. A direct
    ``bind_unit_inputs``/``bind_registered_inputs`` call, a private
    registered-instance lookup, or a manifest write outside ``_Storage``
    reintroduces a second path with its own order."""
    runctl_text = (REPO / "rapidpipe/cli/runctl.py").read_text()
    loop_text = (REPO / "rapidpipe/launch/loop.py").read_text()
    for text in (runctl_text, loop_text):
        for forbidden in ("bind_unit_inputs", "bind_registered_inputs",
                          "_registered_instances", "_registered("):
            assert forbidden not in text
        assert "binding.bind_input_set(" in text

    # write_manifest: in runctl only _Storage's own definition; in loop never
    # called (its LoopTools docstring names the interface, so calls are
    # found through the syntax tree, not the text).
    runctl_tree = ast.parse(runctl_text)
    storage_cls = next(n for n in runctl_tree.body
                       if isinstance(n, ast.ClassDef) and n.name == "_Storage")
    inside = range(storage_cls.lineno, storage_cls.end_lineno + 1)
    lines = [i for i, line in enumerate(runctl_text.splitlines(), 1)
             if "write_manifest(" in line]
    assert lines and all(i in inside for i in lines)
    assert _calls_named(ast.parse(loop_text), "write_manifest") == []


# ======================================================================
# (c) The primitive: order, reuse, refusal and failure
# ======================================================================

def _manifest(instances=("I1", "I2"), result_sets=("RS1", "I1")) -> Manifest:
    return Manifest(
        run="R", unit=Unit("detector-image", "U"), stage="input-set", attempt="A",
        execution_record="exec/input-set.json",
        inputs=Inputs(manifest="m", result_sets=tuple(result_sets)),
        outputs=tuple(OutputEntry(kind="l2-image", format_version="1", instance=i,
                                  key={"i": i}) for i in instances))


class _Log:
    def __init__(self):
        self.calls: list[str] = []
        self.binds: list[list[str]] = []


class _FakeConn:
    def __init__(self, log: _Log):
        self.log = log
        self.committed = 0

    def commit(self):
        self.committed += 1
        self.log.calls.append("commit")


class _FakeStorage:
    def __init__(self, log: _Log, existing: Manifest | None = None, fail_writes: int = 0):
        self.log = log
        self.manifests: dict[str, Manifest] = {}
        self.fail_writes = fail_writes
        if existing is not None:
            self.manifests[DEST] = existing

    @staticmethod
    def _key(location) -> str:
        return f"s3://{location.bucket}/{location.prefix}"

    def exists(self, location, relative):
        assert relative == "manifest.json"
        return self._key(location) in self.manifests

    def read_manifest(self, location_text):
        self.log.calls.append("read")
        return self.manifests[location_text]

    def write_manifest(self, manifest, location):
        self.log.calls.append("write")
        if self.fail_writes:
            self.fail_writes -= 1
            raise OSError("disk went away")
        self.manifests[self._key(location)] = manifest


DEST = "s3://b/scratch/runs/R/inputs/difference/U"
REGISTERED = {"I1", "RS1"}


@pytest.fixture()
def log(monkeypatch):
    log = _Log()
    monkeypatch.setattr(repository, "add_unit",
                        lambda conn, run_id, stage, kind, unit_id: log.calls.append("add_unit"))

    def bind(conn, run_id, stage, unit_id, instances):
        log.calls.append("bind")
        log.binds.append(list(instances))

    monkeypatch.setattr(inputs, "bind_unit_inputs", bind)
    monkeypatch.setattr(inputs, "_registered",
                        lambda conn, names: {n for n in names if n in REGISTERED})
    return log


def _bind(conn, storage, compose, **kw):
    return binding.bind_input_set(
        conn, storage, run_id="R", stage="difference", unit_kind="detector-image",
        unit_id="U", dest=DEST, compose=compose, **kw)


class _Compose:
    def __init__(self, manifest: Manifest):
        self.manifest = manifest
        self.calls = 0

    def __call__(self) -> Manifest:
        self.calls += 1
        return self.manifest


def test_composing_admits_binds_commits_then_writes(log):
    conn, storage, compose = _FakeConn(log), _FakeStorage(log), _Compose(_manifest())
    result = _bind(conn, storage, compose)
    assert log.calls == ["add_unit", "bind", "commit", "write"]
    assert compose.calls == 1
    # The id rule: outputs' instances + result_sets, de-duplicated; the
    # registered subset is bound, the rest skipped.
    assert log.binds == [["I1", "RS1"]]
    assert result == binding.BoundInputSet(DEST, compose.manifest, ("I1", "RS1"), ("I2",),
                                           False)
    assert storage.manifests[DEST] is compose.manifest


def test_reuse_rebinds_the_existing_manifest_and_never_rewrites_it(log):
    existing = _manifest(instances=("I1",), result_sets=("RS1", "GONE"))
    conn, storage = _FakeConn(log), _FakeStorage(log, existing=existing)
    compose = _Compose(_manifest())
    result = _bind(conn, storage, compose)
    assert compose.calls == 0
    assert "write" not in log.calls
    assert log.calls == ["add_unit", "read", "bind", "commit"]
    assert log.binds == [[i for i in inputs.manifest_instances(existing) if i in REGISTERED]]
    assert conn.committed == 1
    assert result.reused is True and result.manifest is existing
    assert result.skipped == ("GONE",)


def test_an_existing_manifest_without_reuse_is_refused_before_anything(log):
    conn = _FakeConn(log)
    storage = _FakeStorage(log, existing=_manifest())
    compose = _Compose(_manifest())
    with pytest.raises(binding.InputSetExists,
                       match=f"refusing to overwrite {DEST}/manifest.json"):
        _bind(conn, storage, compose, reuse_existing=False)
    assert log.calls == [] and compose.calls == 0 and conn.committed == 0


def test_a_failed_write_leaves_committed_bindings_and_the_next_call_composes_again(log):
    conn = _FakeConn(log)
    storage = _FakeStorage(log, fail_writes=1)
    compose = _Compose(_manifest())
    with pytest.raises(OSError, match="disk went away"):
        _bind(conn, storage, compose)
    assert conn.committed == 1
    assert log.binds == [["I1", "RS1"]]
    assert storage.manifests == {}

    log.calls.clear()
    result = _bind(conn, storage, compose)
    assert compose.calls == 2
    assert log.calls == ["add_unit", "bind", "commit", "write"]
    assert log.binds == [["I1", "RS1"], ["I1", "RS1"]]
    assert conn.committed == 2
    assert storage.manifests[DEST] is compose.manifest
    assert result.reused is False


def test_admission_refusal_comes_before_composing(log, monkeypatch):
    def refuse(*_a, **_k):
        raise repository.RunDeletingOrDeleted("run 'R' is 'deleting'; it admits no new work")

    monkeypatch.setattr(repository, "add_unit", refuse)
    conn, storage, compose = _FakeConn(log), _FakeStorage(log), _Compose(_manifest())
    with pytest.raises(repository.RunDeletingOrDeleted):
        _bind(conn, storage, compose)
    assert compose.calls == 0
    assert log.calls == [] and conn.committed == 0 and storage.manifests == {}


def test_a_producer_refusal_from_bind_is_inputs_refused_and_nothing_is_written(
        log, monkeypatch):
    def refuse(conn, run_id, stage, unit_id, instances):
        log.calls.append("bind")
        raise repository.ProducerDeletingOrDeleted("input I1's run 'P' is 'deleting'")

    monkeypatch.setattr(inputs, "bind_unit_inputs", refuse)
    conn, storage, compose = _FakeConn(log), _FakeStorage(log), _Compose(_manifest())
    with pytest.raises(InputsRefused, match="is 'deleting'"):
        _bind(conn, storage, compose)
    assert log.calls == ["add_unit", "bind"]
    assert conn.committed == 0 and storage.manifests == {}
