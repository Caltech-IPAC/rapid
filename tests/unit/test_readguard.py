"""The stage read guard's own logic, with no database: a stubbed cursor.

``rapidpipe.runs.readguard.assert_inputs_readable`` and its call inside ``run_stage``. The
database-backed proof on all three invocation paths is
``tests/cli/test_readguard.py``.
"""

from __future__ import annotations

import contextlib
import importlib
from pathlib import Path

import psycopg2
import pytest

import rapidpipe.products.storage as storage_module
from rapidpipe.db import connection as db_connection
from rapidpipe.products.manifest import Inputs, Manifest, Member, OutputEntry, Unit
from rapidpipe.runs import readguard
from rapidpipe.stages.contract import (
    ExitCode,
    StageDeclaration,
    StageResult,
    run_stage,
)

from .fakes3 import FakeS3

pytestmark = pytest.mark.readguard

RUN = "01RUNCONSUMER0000000000000"
OTHER = "01RUNPRODUCER0000000000000"
SHA = "sha256:" + "1" * 64


def _instance(kind="l2-image", run=OTHER, custody="candidate", deletion_state="retained",
              result_set=False, complete=True, selected=True):
    return dict(kind=kind, run=run, custody=custody, deletion_state=deletion_state,
                result_set=result_set, complete=complete, selected=selected)


def _row(sql, found):
    """``found`` in the column shape of whichever instance query ``sql`` is:
    the guard's own description, or ``rapidpipe.db.objects``'
    ``_READABLE_SQL`` (the shared rule)."""
    if "logical_key" in sql:
        complete = found["complete"] if found["result_set"] else None
        return (found["kind"], found["run"], found["custody"], found["deletion_state"],
                complete, None, "{}", found["selected"])
    return (found["kind"], found["run"], found["custody"], found["deletion_state"],
            found["result_set"], found["complete"], found["selected"])


class FakeCursor:
    """Answers the guard's two queries from ``instances`` and ``members``."""

    def __init__(self, instances, members, log):
        self.instances = instances
        self.members = members  # [(instance, path, primary_location, sha256)]
        self.log = log
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(" ".join(sql.split())[:40])
        if "FROM product_members" in sql:
            sha, path, primary = params
            self._rows = sorted({(i,) for i, p, loc, s in self.members
                                 if s == sha and (p == path or loc == primary)})
        elif "FROM product_instances pi" in sql:
            found = self.instances.get(params[0])
            self._rows = [_row(sql, found)] if found is not None else []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, instances, members):
        self.log = []
        self.rolled_back = 0
        self.committed = 0
        self._instances = instances
        self._members = members

    def cursor(self):
        return FakeCursor(self._instances, self._members, self.log)

    def rollback(self):
        self.rolled_back += 1

    def commit(self):
        self.committed += 1


def _opener(instances=None, members=(), calls=None):
    conn = FakeConnection(instances or {}, list(members))

    @contextlib.contextmanager
    def _open():
        if calls is not None:
            calls.append(1)
        yield conn

    return _open, conn


def _entry(instance, *, path=None, sha=SHA):
    path = path or f"l2/{instance}.fits"
    return OutputEntry(kind="l2-image", format_version="1", instance=instance,
                       key={"exposure": "e1", "detector": "SCA01"}, primary=path,
                       members=(Member("image", path, 10, sha),))


def _manifest(*entries, result_sets=()):
    return Manifest(run="R", unit=Unit(kind="detector-image", id="U"), stage="input-set",
                    attempt="A", execution_record="exec/A.json",
                    inputs=Inputs(manifest="x", result_sets=tuple(result_sets)),
                    outputs=tuple(entries))


# ----------------------------------------------------------------------
# The guard
# ----------------------------------------------------------------------

def test_no_instance_named_makes_no_connection():
    calls = []
    opener, _conn = _opener(calls=calls)
    readguard.assert_inputs_readable(_manifest(), RUN, connect=opener)
    assert calls == []


def test_another_runs_scratch_file_product_is_refused_naming_kind_custody_and_run():
    opener, conn = _opener({"F": _instance(custody="scratch")})
    with pytest.raises(readguard.InputNotReadable) as err:
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN, connect=opener)
    message = str(err.value)
    assert "input F" in message and "l2-image" in message
    assert "custody scratch" in message and f"owning run {OTHER}" in message
    assert "scratch" in message and "not readable" in message
    assert err.value.exit_code == ExitCode.INPUT_REJECTED
    assert conn.log[0].startswith("SET TRANSACTION READ ONLY")
    assert conn.rolled_back == 1 and conn.committed == 0


def test_own_runs_scratch_is_readable():
    opener, _conn = _opener({"F": _instance(run=RUN, custody="scratch")})
    readguard.assert_inputs_readable(_manifest(_entry("F")), RUN, connect=opener)


@pytest.mark.parametrize("custody", ["candidate", "current"])
def test_another_runs_selected_candidate_or_current_is_readable(custody):
    opener, _conn = _opener({"F": _instance(custody=custody)})
    readguard.assert_inputs_readable(_manifest(_entry("F")), RUN, connect=opener)


def test_a_candidate_from_an_unselected_attempt_is_refused():
    opener, _conn = _opener({"F": _instance(selected=False)})
    with pytest.raises(readguard.InputNotReadable, match="selected attempt"):
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN, connect=opener)


def test_a_deleted_instance_is_refused_even_in_its_own_run():
    opener, _conn = _opener({"F": _instance(run=RUN, deletion_state="deleted")})
    with pytest.raises(readguard.InputNotReadable, match="deleted"):
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN, connect=opener)


def test_an_incomplete_result_set_is_refused():
    opener, _conn = _opener(
        {"S": _instance(kind="source-set", result_set=True, complete=False)})
    with pytest.raises(readguard.InputNotReadable, match="complete"):
        readguard.assert_inputs_readable(_manifest(result_sets=["S"]), RUN, connect=opener)


def test_a_result_set_is_judged_by_the_same_rule():
    opener, _conn = _opener(
        {"S": _instance(kind="source-set", result_set=True, custody="scratch")})
    with pytest.raises(readguard.InputNotReadable, match="source-set"):
        readguard.assert_inputs_readable(_manifest(result_sets=["S"]), RUN, connect=opener)


def test_an_unregistered_id_is_readable():
    opener, _conn = _opener({})
    readguard.assert_inputs_readable(
        _manifest(_entry("NEW"), result_sets=["NEWSET"]), RUN, connect=opener)


def test_the_first_refused_instance_is_named():
    opener, _conn = _opener({"A": _instance(), "B": _instance(custody="scratch"),
                             "C": _instance(selected=False)})
    with pytest.raises(readguard.InputNotReadable) as err:
        readguard.assert_inputs_readable(
            _manifest(_entry("A"), _entry("B"), _entry("C")), RUN, connect=opener)
    assert str(err.value).startswith("input B ")


def test_a_fresh_id_over_another_runs_scratch_files_is_refused():
    opener, _conn = _opener(
        {"F": _instance(custody="scratch")},
        members=[("F", "l2/F.fits", "l2/F.fits", SHA)])
    with pytest.raises(readguard.InputNotReadable) as err:
        readguard.assert_inputs_readable(
            _manifest(_entry("NEW", path="l2/F.fits")), RUN, connect=opener)
    assert "member l2/F.fits is a file of registered instance F" in str(err.value)


def test_a_fresh_id_matched_through_primary_location_is_refused():
    opener, _conn = _opener(
        {"F": _instance(custody="scratch")},
        members=[("F", "some/other.fits", "l2/F.fits", SHA)])
    with pytest.raises(readguard.InputNotReadable):
        readguard.assert_inputs_readable(
            _manifest(_entry("NEW", path="l2/F.fits")), RUN, connect=opener)


def test_same_path_with_different_bytes_does_not_match():
    opener, _conn = _opener(
        {"F": _instance(custody="scratch")},
        members=[("F", "l2/F.fits", "l2/F.fits", "sha256:" + "2" * 64)])
    readguard.assert_inputs_readable(
        _manifest(_entry("NEW", path="l2/F.fits")), RUN, connect=opener)


def test_bytes_matching_a_readable_instance_as_well_are_readable():
    opener, _conn = _opener(
        {"F": _instance(custody="scratch"), "G": _instance(custody="candidate")},
        members=[("F", "l2/x.fits", "l2/x.fits", SHA), ("G", "l2/x.fits", "l2/x.fits", SHA)])
    readguard.assert_inputs_readable(
        _manifest(_entry("NEW", path="l2/x.fits")), RUN, connect=opener)


def test_a_readable_member_does_not_authorise_a_forbidden_one():
    other_sha = "sha256:" + "3" * 64
    entry = OutputEntry(
        kind="l2-image", format_version="1", instance="NEW",
        key={"exposure": "e1", "detector": "SCA01"}, primary="l2/ok.fits",
        members=(Member("image", "l2/ok.fits", 10, SHA),
                 Member("mask", "l2/bad.fits", 10, other_sha)))
    opener, _conn = _opener(
        {"G": _instance(custody="candidate"), "F": _instance(custody="scratch")},
        members=[("G", "l2/ok.fits", "l2/ok.fits", SHA),
                 ("F", "l2/bad.fits", "l2/bad.fits", other_sha)])
    with pytest.raises(readguard.InputNotReadable) as err:
        readguard.assert_inputs_readable(_manifest(entry), RUN, connect=opener)
    assert "member l2/bad.fits is a file of registered instance F" in str(err.value)


@pytest.mark.parametrize("raised, expected, code", [
    (db_connection.ConnectionConfigError("missing PGHOST"),
     readguard.ReadGuardNotConfigured, ExitCode.USAGE),
    (db_connection.ConnectionUnavailable("refused"),
     readguard.ReadGuardUnavailable, ExitCode.TRANSIENT_FAILURE),
    (psycopg2.OperationalError("server closed the connection"),
     readguard.ReadGuardUnavailable, ExitCode.TRANSIENT_FAILURE),
])
def test_connection_failures_map_to_64_or_75(raised, expected, code):
    @contextlib.contextmanager
    def _open():
        raise raised
        yield  # pragma: no cover

    with pytest.raises(expected) as err:
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN, connect=_open)
    assert err.value.exit_code == code


def test_the_default_connection_goes_through_the_module_level_indirection(monkeypatch):
    opener, _conn = _opener({"F": _instance(custody="scratch")})
    monkeypatch.setattr(readguard, "connect", opener)
    with pytest.raises(readguard.InputNotReadable):
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN)


EMPTY_REGISTRY = "rapidpipe.selftest.support.fakereadguarddb:empty_registry"


def test_the_selftest_registry_in_a_selftest_run_reads_ids_as_unregistered_and_warns(
        monkeypatch, caplog):
    monkeypatch.setenv(readguard.DATABASE_ENV, EMPTY_REGISTRY)
    monkeypatch.setenv(readguard.SELFTEST_ENV, "1")
    with caplog.at_level("WARNING", logger="rapidpipe.runs.readguard"):
        readguard.assert_inputs_readable(_manifest(_entry("F"), result_sets=["S"]), RUN)
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "read guard: selftest registry in use, no product custody enforced"]


def test_the_selftest_registry_outside_a_selftest_run_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv(readguard.DATABASE_ENV, EMPTY_REGISTRY)
    monkeypatch.delenv(readguard.SELFTEST_ENV, raising=False)
    with pytest.raises(readguard.ReadGuardNotConfigured) as err:
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN)
    assert readguard.DATABASE_ENV in str(err.value)
    assert err.value.exit_code == ExitCode.USAGE


@pytest.mark.parametrize("value", [
    "rapidpipe.no_such_module:factory",
    "tests.unit.test_readguard:_opener",
    "rapidpipe.selftest.support.fakereadguarddb",
    "",
])
def test_any_other_seam_value_is_a_configuration_error_even_in_a_selftest(monkeypatch, value):
    monkeypatch.setenv(readguard.DATABASE_ENV, value)
    monkeypatch.setenv(readguard.SELFTEST_ENV, "1")
    with pytest.raises(readguard.ReadGuardNotConfigured) as err:
        readguard.assert_inputs_readable(_manifest(_entry("F")), RUN)
    assert readguard.DATABASE_ENV in str(err.value)


def test_the_seam_outside_a_selftest_exits_64_from_run_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(readguard.DATABASE_ENV, EMPTY_REGISTRY)
    monkeypatch.delenv(readguard.SELFTEST_ENV, raising=False)
    inputs = _write_inputs(tmp_path / "in", _manifest(_entry("F")))
    rc = run_stage(DECLARATION, lambda context: StageResult(outputs=()),
                   _argv(inputs, tmp_path / "out", "--dry-run"))
    assert rc == 64


# ----------------------------------------------------------------------
# The call inside run_stage
# ----------------------------------------------------------------------

DECLARATION = StageDeclaration(
    name="difference", unit="detector-image",
    settings_schema_path=None, consumes=(), produces=(), database_access="custody")


def _write_inputs(directory: Path, manifest: Manifest) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(manifest.to_json())
    return directory


def _argv(inputs, outputs, *extra):
    return ["--run", RUN, "--unit", "U", "--attempt", "01ATTEMPT00000000000000000",
            "--inputs", str(inputs), "--outputs", str(outputs), *extra]


@pytest.mark.parametrize("raised, code", [
    (readguard.InputNotReadable("input F is not readable"), 65),
    (readguard.ReadGuardNotConfigured("no database"), 64),
    (readguard.ReadGuardUnavailable("unreachable"), 75),
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_run_stage_maps_the_guards_outcomes_and_never_reaches_the_body(
        tmp_path, monkeypatch, raised, code, dry_run):
    seen = []

    def _guard(manifest, run_id, *, connect=None):
        seen.append((manifest.outputs[0].instance, run_id))
        raise raised

    def _body(context):
        raise AssertionError("the body must not run")

    def _validate(context):
        raise AssertionError("validate_inputs must not run")

    monkeypatch.setattr(readguard, "assert_inputs_readable", _guard)
    inputs = _write_inputs(tmp_path / "in", _manifest(_entry("F")))
    extra = ("--dry-run",) if dry_run else ()
    rc = run_stage(DECLARATION, _body, _argv(inputs, tmp_path / "out", *extra),
                   validate_inputs=_validate)
    assert rc == code
    assert seen == [("F", RUN)]
    assert not (tmp_path / "out" / "manifest.json").exists()


def test_run_stage_passes_a_readable_manifest_through_to_the_body(tmp_path, monkeypatch):
    monkeypatch.setattr(readguard, "assert_inputs_readable",
                        lambda manifest, run_id, *, connect=None: None)
    inputs = _write_inputs(tmp_path / "in", _manifest(_entry("F")))
    rc = run_stage(DECLARATION, lambda context: StageResult(outputs=()),
                   _argv(inputs, tmp_path / "out"))
    assert rc == 0


def _seed_s3(fake: FakeS3, prefix: str, manifest: Manifest) -> str:
    fake.seed("bucket", f"{prefix}/manifest.json", manifest.to_json().encode())
    fake.seed("bucket", f"{prefix}/l2/F.fits", b"0123456789")
    return f"s3://bucket/{prefix}"


def test_on_s3_only_the_manifest_is_fetched_before_a_refusal(tmp_path, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))

    def _refuse(manifest, run_id, *, connect=None):
        raise readguard.InputNotReadable("input F is not readable")

    monkeypatch.setattr(readguard, "assert_inputs_readable", _refuse)
    inputs = _seed_s3(fake, "in", _manifest(_entry("F")))
    rc = run_stage(DECLARATION, lambda context: StageResult(outputs=()),
                   _argv(inputs, tmp_path / "out"))
    assert rc == 65
    fetched = [key for op, key in fake.calls if op in ("download_file", "get_object")]
    assert fetched == ["in/manifest.json"]
    assert not any(op == "list_objects_v2" for op, _key in fake.calls)


def test_on_s3_the_members_are_fetched_after_the_guard_passes(tmp_path, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    order = []

    def _pass(manifest, run_id, *, connect=None):
        order.append(("guard", len(fake.calls)))

    monkeypatch.setattr(readguard, "assert_inputs_readable", _pass)
    inputs = _seed_s3(fake, "in", _manifest(_entry("F")))

    def _body(context):
        assert (context.inputs_dir / "l2" / "F.fits").read_bytes() == b"0123456789"
        return StageResult(outputs=())

    rc = run_stage(DECLARATION, _body, _argv(inputs, tmp_path / "out"))
    assert rc == 0
    guard_at = order[0][1]
    before = [key for _op, key in fake.calls[:guard_at]]
    assert before == ["in/manifest.json"]
    assert any(key == "in/l2/F.fits" for _op, key in fake.calls[guard_at:])


def test_on_s3_an_object_under_the_prefix_but_not_in_the_manifest_is_never_fetched(
        tmp_path, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    monkeypatch.setattr(readguard, "assert_inputs_readable",
                        lambda manifest, run_id, *, connect=None: None)
    inputs = _seed_s3(fake, "in", _manifest(_entry("F")))
    fake.seed("bucket", "in/l2/forbidden.fits", b"another run's scratch bytes")

    def _body(context):
        assert not (context.inputs_dir / "l2" / "forbidden.fits").exists()
        return StageResult(outputs=())

    rc = run_stage(DECLARATION, _body, _argv(inputs, tmp_path / "out"))
    assert rc == 0
    touched = [key for _op, key in fake.calls if key.startswith("in/")]
    assert touched == ["in/manifest.json", "in/l2/F.fits"]
    assert not any(op == "list_objects_v2" for op, _key in fake.calls)


def test_on_s3_a_manifest_swapped_after_the_guard_cannot_matter(tmp_path, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    inputs = _seed_s3(fake, "in", _manifest(_entry("F")))
    fake.seed("bucket", "in/l2/G.fits", b"never read")

    def _swap(manifest, run_id, *, connect=None):
        fake.seed("bucket", "in/manifest.json", _manifest(_entry("G")).to_json().encode())

    monkeypatch.setattr(readguard, "assert_inputs_readable", _swap)
    seen = []

    def _body(context):
        seen.append([entry.instance for entry in context.input_manifest.outputs])
        return StageResult(outputs=())

    rc = run_stage(DECLARATION, _body, _argv(inputs, tmp_path / "out"))
    assert rc == 0
    assert seen == [["F"]]
    touched = [key for _op, key in fake.calls if key.startswith("in/")]
    assert touched == ["in/manifest.json", "in/l2/F.fits"]


def test_on_s3_a_member_missing_from_the_prefix_is_rejected_with_65(tmp_path, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage_module, "s3_client", lambda: fake)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    monkeypatch.setattr(readguard, "assert_inputs_readable",
                        lambda manifest, run_id, *, connect=None: None)
    fake.seed("bucket", "in/manifest.json", _manifest(_entry("F")).to_json().encode())
    rc = run_stage(DECLARATION, lambda context: StageResult(outputs=()),
                   _argv("s3://bucket/in", tmp_path / "out"))
    assert rc == 65


# ----------------------------------------------------------------------
# The declaration agrees with the behaviour (stage contract, "Declaration")
# ----------------------------------------------------------------------

def test_a_stage_declaring_none_never_calls_the_guard(tmp_path, monkeypatch):
    def _guard(manifest, run_id, *, connect=None):
        raise AssertionError("a stage declaring database_access='none' must not connect")

    monkeypatch.setattr(readguard, "assert_inputs_readable", _guard)
    monkeypatch.delenv(readguard.DATABASE_ENV, raising=False)
    declaration = StageDeclaration(
        name="difference", unit="detector-image", settings_schema_path=None,
        consumes=(), produces=(), database_access="none")
    inputs = _write_inputs(tmp_path / "in", _manifest(_entry("F")))
    rc = run_stage(declaration, lambda context: StageResult(outputs=()),
                   _argv(inputs, tmp_path / "out"))
    assert rc == 0


@pytest.mark.parametrize("level", ["custody", "read", "read-write"])
def test_every_level_but_none_passes_through_the_guard(tmp_path, monkeypatch, level):
    seen = []
    monkeypatch.setattr(readguard, "assert_inputs_readable",
                        lambda manifest, run_id, *, connect=None: seen.append(run_id))
    declaration = StageDeclaration(
        name="difference", unit="detector-image", settings_schema_path=None,
        consumes=(), produces=(), database_access=level)
    inputs = _write_inputs(tmp_path / "in", _manifest(_entry("F")))
    rc = run_stage(declaration, lambda context: StageResult(outputs=()),
                   _argv(inputs, tmp_path / "out"))
    assert rc == 0
    assert seen == [RUN]


@pytest.mark.parametrize("name", ["admit", "reference", "difference", "finalize"])
def test_the_transform_stages_declare_custody(name):
    module = importlib.import_module(f"rapidpipe.stages.{name}")
    assert module.DECLARATION.database_access == "custody"


def test_no_shipped_stage_skips_the_guard():
    """Every shipped stage reads an input manifest, so none may declare
    ``none``: that level skips the custody check."""
    from rapidpipe.stages.contract import STAGE_NAMES

    for name in STAGE_NAMES:
        module = importlib.import_module(f"rapidpipe.stages.{name}")
        assert module.DECLARATION.database_access != "none", name
