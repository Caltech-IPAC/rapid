"""The release tool's parts without a cut: tag arithmetic, hook result
parsing, the command-line forms, the execution record's release, and a
released run's Batch submission -- all database-free."""

from __future__ import annotations

import contextlib
import json

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.launch import batch as launch_batch
from rapidpipe.release import __main__ as release_main
from rapidpipe.release import core
from rapidpipe.release.hooks import HookFailed, parse_hook_result
from rapidpipe.runs import repository
from rapidpipe.stages import contract
from tests.unit.fakebatch import FakeBatch
from tests.unit.releaserepo import DIGEST, IMAGE_REF, git, isolate_git, make_release_repo


# ======================================================================
# next_tag
# ======================================================================

@pytest.fixture()
def repo(tmp_path, monkeypatch):
    isolate_git(monkeypatch, tmp_path)
    return make_release_repo(tmp_path)


def test_next_tag_is_one_with_no_tags(repo):
    assert core.next_tag(repo) == "rebuild-v0.1"


def test_next_tag_is_one_more_than_the_greatest_remote_or_local(repo):
    for name in ("rebuild-v0.1", "rebuild-v0.3", "rebuild-v0.x", "other-v0.9"):
        git(repo, "tag", "-a", name, "-m", "{}")
    git(repo, "push", "-q", "origin", "--tags")
    for name in ("rebuild-v0.1", "rebuild-v0.3", "rebuild-v0.x", "other-v0.9"):
        git(repo, "tag", "-d", name)
    assert core.next_tag(repo) == "rebuild-v0.4"  # from the remote alone
    git(repo, "tag", "rebuild-v0.10")
    assert core.next_tag(repo) == "rebuild-v0.11"  # a local tag counts too


def test_schema_version_is_the_greatest_dated_migration(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert core.schema_version_at(repo, head) == "20260924-08-releases.sql"
    assert core.migration_files(repo, head) == [
        "20260921-01-baseline.sql", "20260924-08-releases.sql"]


# ======================================================================
# hook results
# ======================================================================

@pytest.mark.parametrize("name, line", [
    ("migrate", '{"schema_version": "20260924-08-releases.sql", "applied": ["a.sql"]}'),
    ("build", json.dumps({"image_digest": DIGEST, "image_ref": IMAGE_REF, "image_tag": "t"})),
    ("deploy", '{"deployments": {"rapid-rebuild": "rapid-rebuild:7"}}'),
    ("pins", '{"rows": 0}'),
    ("inspect", '{"deployments": {"a": "a:1"}, "digests": {"a": "x"}}'),
])
def test_valid_hook_results_parse(name, line):
    assert parse_hook_result(name, line) == json.loads(line)


@pytest.mark.parametrize("name, line, message", [
    ("migrate", "done", "not JSON"),
    ("migrate", "[1]", "not a JSON object"),
    ("migrate", '{"schema_version": "x"}', "applied"),
    ("build", '{"image_digest": "sha256:abc", "image_ref": "r@sha256:abc"}', "64 hex"),
    ("build", json.dumps({"image_digest": DIGEST, "image_ref": "r:latest"}), "does not end"),
    ("deploy", '{"deployments": {}}', "at least one"),
    ("deploy", '{"deployments": {"rapid-rebuild": "rapid-rebuild"}}', "name:revision"),
    ("pins", '{"rows": "2"}', "rows"),
    ("pins", '{"rows": true}', "rows"),
])
def test_invalid_hook_results_fail(name, line, message):
    with pytest.raises(HookFailed, match=message):
        parse_hook_result(name, line)


# ======================================================================
# command-line forms
# ======================================================================

@pytest.mark.parametrize("argv", [
    ["--help"], ["cut", "--help"], ["show", "--help"], ["list", "--help"], ["verify", "--help"],
])
def test_every_subcommand_prints_usage(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        release_main.main(argv)
    assert excinfo.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_both_forms_are_one_dry_run_code_path(repo, capsys, monkeypatch):
    def _no_db(**_kw):
        raise AssertionError("a dry run opens no database connection")

    monkeypatch.setattr(release_main, "connect", _no_db)
    argv = ["cut", "--dry-run", "--repo", str(repo), "--hooks-dir", "/nonexistent"]
    assert release_main.main(argv) == 0
    direct = capsys.readouterr().out
    assert cli.main(["release", *argv]) == 0
    assert capsys.readouterr().out == direct
    assert "rebuild-v0.1" in direct
    assert git(repo, "tag", "-l") == ""


def test_usage_refusals_exit_2(repo, monkeypatch, capsys):
    monkeypatch.setattr(release_main, "connect", lambda **_kw: contextlib.nullcontext(None))
    (repo / "database" / "migrations" / "README.md").write_text("dirty\n")
    assert release_main.main(["cut", "--dry-run", "--repo", str(repo)]) == 2
    assert "uncommitted" in capsys.readouterr().err
    assert release_main.main([]) == 2


def test_list_and_show_print_the_rows(monkeypatch, capsys):
    from tests.unit.fakereleasedb import FakeReleaseDB

    db = FakeReleaseDB()
    with db.cursor() as cur:
        cur.execute("INSERT INTO releases", (
            "rebuild-v0.1", "a" * 40, "20260924-08-releases.sql", "migrated", "ops",
            None, "{}"))
        cur.execute("UPDATE releases SET", (
            "complete", DIGEST, IMAGE_REF, "{}", True, "rebuild-v0.1"))
        cur.execute("INSERT INTO release_deployments", (
            "rebuild-v0.1", "rapid-rebuild", "rapid-rebuild:7", "ops"))
    db.commit()
    monkeypatch.setattr(release_main, "connect", lambda **_kw: contextlib.nullcontext(db))

    assert release_main.main(["list"]) == 0
    assert capsys.readouterr().out.split("\t")[:4] == [
        "rebuild-v0.1", "complete", "a" * 12, DIGEST[:19]]
    assert release_main.main(["show", "rebuild-v0.1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["deployments"][0]["job_definition"] == "rapid-rebuild:7"
    assert release_main.main(["show", "rebuild-v0.9"]) == 1


# ======================================================================
# the execution record carries the release (R7)
# ======================================================================

@pytest.mark.parametrize("env, expected", [
    ({}, None),
    ({"RAPID_RELEASE_IDENTITY": "unreleased"}, None),
    ({"RAPID_RELEASE_IDENTITY": "rebuild-v0.3"}, "rebuild-v0.3"),
    ({"RAPIDPIPE_RELEASE": "rebuild-v0.4", "RAPID_RELEASE_IDENTITY": "rebuild-v0.3"},
     "rebuild-v0.4"),
])
def test_execution_record_release(tmp_path, monkeypatch, env, expected):
    for name in ("RAPIDPIPE_RELEASE", "RAPID_RELEASE_IDENTITY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    relative = contract._write_execution_record(tmp_path, "ATT1", "hash")
    assert json.loads((tmp_path / relative).read_text())["release"] == expected


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("unreleased", None), ("rebuild-v0.2", "rebuild-v0.2")])
def test_repository_records_unreleased_as_null(value, expected):
    assert repository._release_identity(value) == expected


# ======================================================================
# a released run submits to its release's job definition revision (R7)
# ======================================================================

class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._result = self._rows.pop(0)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _Conn:
    def __init__(self, *results):
        self._results = list(results)

    def cursor(self):
        return _Cursor(self._results)

    def commit(self):
        pass

    def rollback(self):
        pass


_DEPLOYMENTS = [("rapid-rebuild", "rapid-rebuild:7"),
                ("rapid-rebuild-production", "rapid-rebuild-production:5")]


@pytest.mark.parametrize("kind, expected", [
    ("scratch", "rapid-rebuild:7"), ("production", "rapid-rebuild-production:5")])
def test_release_job_definition_maps_consumer_by_kind(kind, expected):
    conn = _Conn([(kind, "rebuild-v0.1")], _DEPLOYMENTS)
    assert launch_batch._release_job_definition(conn, "RUN") == ("rebuild-v0.1", expected)


def test_release_job_definition_is_none_without_a_release():
    assert launch_batch._release_job_definition(_Conn([("scratch", None)]), "RUN") is None


def test_release_job_definition_refuses_an_ambiguous_mapping():
    conn = _Conn([("production", "rebuild-v0.1")],
                 [("a-production", "a:1"), ("b-production", "b:1")])
    with pytest.raises(launch_batch.ReleaseDefinitionRefused, match="2 job definition"):
        launch_batch._release_job_definition(conn, "RUN")


def _patch_submit(monkeypatch, release):
    calls = {}
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue1")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://bucket/root")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "unversioned-def")
    monkeypatch.setattr(launch_batch, "_release_job_definition", lambda conn, run_id: release)
    monkeypatch.setattr(launch_batch, "_run_kind", lambda conn, run_id: "scratch")
    monkeypatch.setattr(launch_batch, "add_unit",
                        lambda *a: calls.setdefault("add_unit", a))
    monkeypatch.setattr(launch_batch, "allocate_attempt", lambda *a, **k: "ATT1")
    monkeypatch.setattr(launch_batch, "record_scheduler_job", lambda *a, **k: None)
    monkeypatch.setattr(launch_batch, "record_attempt_locations", lambda *a, **k: None)
    return calls


def test_released_run_submits_to_the_active_revision(monkeypatch):
    _patch_submit(monkeypatch, ("rebuild-v0.1", "rapid-rebuild:7"))
    fake = FakeBatch()
    fake.job_definitions["rapid-rebuild:7"] = "ACTIVE"
    launch_batch.submit_unit(
        _Conn(), run_id="RUN", stage="admit", unit_kind="detector-image", unit_id="u",
        inputs_location="s3://in/x", client=fake)
    assert fake.calls[0] == ("describe_job_definitions", ("rapid-rebuild:7",))
    assert fake.submitted[0]["jobDefinition"] == "rapid-rebuild:7"


@pytest.mark.parametrize("status", ["INACTIVE", None])
def test_released_run_refuses_a_revision_that_is_not_active(monkeypatch, status):
    calls = _patch_submit(monkeypatch, ("rebuild-v0.1", "rapid-rebuild:7"))
    fake = FakeBatch()
    if status:
        fake.job_definitions["rapid-rebuild:7"] = status
    with pytest.raises(launch_batch.ReleaseDefinitionRefused, match="rebuild-v0.1"):
        launch_batch.submit_unit(
            _Conn(), run_id="RUN", stage="admit", unit_kind="detector-image", unit_id="u",
            inputs_location="s3://in/x", client=fake)
    assert fake.submitted == []
    assert "add_unit" not in calls


def test_released_run_refuses_a_contradicting_explicit_definition(monkeypatch):
    _patch_submit(monkeypatch, ("rebuild-v0.1", "rapid-rebuild:7"))
    with pytest.raises(launch_batch.ReleaseDefinitionRefused, match="not 'other:1'"):
        launch_batch.submit_unit(
            _Conn(), run_id="RUN", stage="admit", unit_kind="detector-image", unit_id="u",
            inputs_location="s3://in/x", job_definition="other:1", client=FakeBatch())


def test_run_submit_exits_1_on_a_release_refusal(monkeypatch, capsys):
    def _refuse(*_a, **_k):
        raise launch_batch.ReleaseDefinitionRefused("release rebuild-v0.1 ... not ACTIVE")

    monkeypatch.setattr(cli, "connect", lambda **_kw: contextlib.nullcontext(_Conn()))
    monkeypatch.setattr(launch_batch, "submit_unit", _refuse)
    rc = cli.main(["run", "submit", "RUN", "admit", "--unit", "u", "--inputs", "s3://in/x"])
    assert rc == 1
    assert "rebuild-v0.1" in capsys.readouterr().err


# ======================================================================
# rapidpipe run create --release (R7)
# ======================================================================

def test_run_create_release_fills_revision_and_digest(monkeypatch, capsys):
    seen = {}

    def _create_run(conn, **kwargs):
        seen.update(kwargs)
        return "RUN01"

    conn = _Conn([("20260924-08-releases.sql",)], [("complete", "c" * 40, DIGEST)])
    monkeypatch.setattr(cli, "connect", lambda **_kw: contextlib.nullcontext(conn))
    monkeypatch.setattr(repository, "create_run", _create_run)
    monkeypatch.setattr(cli, "_source_revision_or_unknown",
                        lambda: pytest.fail("a released run reads no git"))
    rc = cli.main(["run", "create", "--kind", "production", "--purpose", "p",
                   "--stages", "admit", "--release", "rebuild-v0.1"])
    assert rc == 0
    assert (seen["release"], seen["code_revision"], seen["image_digest"]) == (
        "rebuild-v0.1", "c" * 40, DIGEST)


@pytest.mark.parametrize("row, state", [(None, "absent"), (("built", "c" * 40, DIGEST), "built")])
def test_run_create_refuses_a_release_that_is_not_complete(monkeypatch, capsys, row, state):
    conn = _Conn([("20260924-08-releases.sql",)], [row] if row else [])
    monkeypatch.setattr(cli, "connect", lambda **_kw: contextlib.nullcontext(conn))
    monkeypatch.setattr(repository, "create_run", lambda *a, **k: pytest.fail("no run"))
    rc = cli.main(["run", "create", "--kind", "scratch", "--purpose", "p",
                   "--stages", "admit", "--release", "rebuild-v0.1"])
    assert rc == 2
    assert f"is {state}, not complete" in capsys.readouterr().err
