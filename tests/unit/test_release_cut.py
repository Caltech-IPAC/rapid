"""``rapidpipe.release.core.cut`` end to end, against a temporary git repo
with a bare origin, stub hook scripts and :class:`FakeReleaseDB`.

The hooks and the fake database append to one log file, so the order of a
cut -- tag, migrate, row, build, deploy, pins -- is asserted from what
actually happened, not from what ``cut`` says it did.
"""

from __future__ import annotations

import pytest

from rapidpipe.release import core
from rapidpipe.release.hooks import HookFailed, ReleaseRefused, ReleaseUsage
from tests.unit.fakereleasedb import FakeReleaseDB
from tests.unit.releaserepo import (
    DIGEST,
    IMAGE_REF,
    git,
    isolate_git,
    log_lines,
    make_release_repo,
    migration_shas,
    write_hooks,
)


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    isolate_git(monkeypatch, tmp_path)
    repo = make_release_repo(tmp_path)
    log = tmp_path / "order.log"
    hooks = write_hooks(tmp_path / "hooks", log)
    db = FakeReleaseDB(log_path=log)
    for name, sha in migration_shas(repo).items():
        db.apply(name, sha)
    return repo, hooks, log, db


def _cut(db, repo, hooks, **kw):
    lines: list[str] = []
    result = core.cut(db, repo=repo, hooks_dir=hooks, by="tester", out=lines.append, **kw)
    return result, lines


def _tag_object(repo, tag):
    return git(repo, "rev-parse", f"refs/tags/{tag}")


def test_full_cut_runs_in_order_and_records_every_state(setup):
    repo, hooks, log, db = setup
    head = git(repo, "rev-parse", "HEAD")

    release, lines = _cut(db, repo, hooks)

    assert release.tag == "rebuild-v0.1"
    assert release.state == "complete"
    assert log_lines(log) == [
        f"hook:migrate:tag={head}:digest=unset",
        "db:commit:rebuild-v0.1:migrated",
        f"hook:build:tag={head}:digest=unset",
        "db:commit:rebuild-v0.1:built",
        f"hook:deploy:tag={head}:digest={DIGEST}",
        "db:commit:rebuild-v0.1:deployed",
        f"hook:pins:tag={head}:digest={DIGEST}",
        "db:commit:rebuild-v0.1:complete",
    ]
    row = db.release("rebuild-v0.1")
    assert row["source_revision"] == head
    assert row["schema_version"] == "20260924-08-releases.sql"
    assert (row["image_digest"], row["image_ref"]) == (DIGEST, IMAGE_REF)
    assert row["completed_at"] is not None
    assert row["notes"]["database_target"] == "rapid_rebuild"
    assert row["notes"]["skipped"] == []
    assert row["notes"]["pins_rows"] == 2
    assert db.deployments("rebuild-v0.1") == {
        "rapid-rebuild": "rapid-rebuild:7",
        "rapid-rebuild-production": "rapid-rebuild-production:5"}
    # The tag is annotated, pushed, and its message is the initial manifest.
    assert git(repo, "cat-file", "-t", "refs/tags/rebuild-v0.1") == "tag"
    assert "rebuild-v0.1" in git(repo, "ls-remote", "--tags", "origin")
    manifest = core._tag_manifest(repo, "rebuild-v0.1")
    assert set(manifest) == {"tag", "source_revision", "schema_version", "cut_by", "cut_at"}
    assert manifest["cut_by"] == "tester"
    # Hook stdout is streamed to the operator.
    assert "progress line from deploy" in lines


def test_resume_after_a_failing_deploy_reenters_at_deploy_without_retagging(setup):
    repo, hooks, log, db = setup
    (hooks / "fail-deploy").write_text("")

    with pytest.raises(HookFailed, match="deploy exited 3"):
        _cut(db, repo, hooks)
    assert db.release("rebuild-v0.1")["state"] == "built"
    assert db.deployments("rebuild-v0.1") == {}
    tag_object = _tag_object(repo, "rebuild-v0.1")

    (hooks / "fail-deploy").unlink()
    log.write_text("")
    release, lines = _cut(db, repo, hooks, resume="rebuild-v0.1")

    assert release.state == "complete"
    assert [line.split(":tag=")[0] for line in log_lines(log)] == [
        "hook:deploy", "db:commit:rebuild-v0.1:deployed",
        "hook:pins", "db:commit:rebuild-v0.1:complete"]
    assert _tag_object(repo, "rebuild-v0.1") == tag_object
    assert git(repo, "tag", "-l").split() == ["rebuild-v0.1"]
    assert "release rebuild-v0.1: resuming at deploy" in lines


def test_migration_mismatch_fails_before_the_row_is_written(setup):
    repo, hooks, log, db = setup
    db.apply("20260924-08-releases.sql", "0" * 64)
    with pytest.raises(ReleaseRefused, match="different sha256"):
        _cut(db, repo, hooks)
    assert db.release("rebuild-v0.1") is None
    assert [line.split(":")[1] for line in log_lines(log)] == ["migrate"]


def test_missing_migration_fails_before_the_row_is_written(setup, tmp_path):
    repo, hooks, log, _db = setup
    db = FakeReleaseDB(log_path=log)
    db.apply("20260921-01-baseline.sql", migration_shas(repo)["20260921-01-baseline.sql"])
    with pytest.raises(ReleaseRefused, match="20260924-08-releases.sql is not in"):
        _cut(db, repo, hooks)
    assert db.release("rebuild-v0.1") is None


def test_a_garbled_hook_result_fails_the_cut_at_that_state(setup):
    repo, hooks, _log, db = setup
    (hooks / "garble-build").write_text("")
    with pytest.raises(HookFailed, match="not JSON"):
        _cut(db, repo, hooks)
    assert db.release("rebuild-v0.1")["state"] == "migrated"


def test_skip_records_the_skipped_hook_and_runs_nothing_for_it(setup):
    repo, hooks, log, db = setup
    release, _ = _cut(db, repo, hooks, skip=["pins"])
    assert release.state == "complete"
    assert db.release("rebuild-v0.1")["notes"]["skipped"] == ["pins"]
    assert not any(line.startswith("hook:pins") for line in log_lines(log))


def test_dry_run_touches_nothing(setup):
    repo, hooks, log, _db = setup
    lines: list[str] = []
    plan = core.cut(None, repo=repo, hooks_dir="/nonexistent", dry_run=True, by="t",
                    out=lines.append)
    assert isinstance(plan, core.Plan)
    assert plan.tag == "rebuild-v0.1"
    assert plan.source_revision == git(repo, "rev-parse", "origin/rebuild")
    assert plan.missing_hooks == ["migrate", "build", "deploy", "pins"]
    assert any(line.startswith("tag:") and "rebuild-v0.1" in line for line in lines)
    assert git(repo, "tag", "-l") == ""
    assert git(repo, "ls-remote", "--tags", "origin") == ""
    assert log_lines(log) == []


def test_dirty_tree_is_refused_as_usage(setup):
    repo, hooks, _log, db = setup
    (repo / "database" / "migrations" / "README.md").write_text("changed\n")
    with pytest.raises(ReleaseUsage, match="uncommitted"):
        _cut(db, repo, hooks)


def test_an_existing_tag_is_refused_unless_resumed(setup):
    repo, hooks, _log, db = setup
    _cut(db, repo, hooks)
    with pytest.raises(ReleaseUsage, match="already exists"):
        _cut(db, repo, hooks, tag="rebuild-v0.1")
    release, lines = _cut(db, repo, hooks, resume="rebuild-v0.1")
    assert release.state == "complete"
    assert "release rebuild-v0.1 is already complete; nothing to do" in lines


def test_a_ref_off_the_rebuild_line_is_refused(setup):
    repo, hooks, _log, db = setup
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "x.txt").write_text("x\n")
    git(repo, "add", "x.txt")
    git(repo, "commit", "-q", "-m", "side")
    with pytest.raises(ReleaseUsage, match="not an ancestor"):
        _cut(db, repo, hooks, ref="side")


def test_missing_hooks_are_refused_before_tagging(setup, tmp_path):
    repo, _hooks, _log, db = setup
    with pytest.raises(ReleaseUsage, match="not found or not executable"):
        _cut(db, repo, tmp_path / "empty-hooks")
    assert git(repo, "tag", "-l") == ""


def test_resume_refuses_a_changed_hooks_revision_unless_forced(setup, tmp_path):
    repo, hooks, log, db = setup
    git(hooks, "init", "-q")
    git(hooks, "add", "-A")
    git(hooks, "commit", "-q", "-m", "hooks v1")
    (hooks / "fail-build").write_text("")
    with pytest.raises(HookFailed):
        _cut(db, repo, hooks)
    recorded = db.release("rebuild-v0.1")["notes"]["hooks_dir_revision"]
    assert recorded == git(hooks, "rev-parse", "HEAD")

    (hooks / "fail-build").unlink()
    (hooks / "README").write_text("v2\n")
    git(hooks, "add", "README")
    git(hooks, "commit", "-q", "-m", "hooks v2")
    with pytest.raises(ReleaseRefused, match="hooks_dir revision"):
        _cut(db, repo, hooks, resume="rebuild-v0.1")
    release, _ = _cut(db, repo, hooks, resume="rebuild-v0.1", force_resume=True)
    assert release.state == "complete"
    assert release.notes["forced_resumes"][0]["hooks_dir_revision"] == git(
        hooks, "rev-parse", "HEAD")


def test_resume_refuses_a_different_database_target(setup):
    repo, hooks, _log, db = setup
    (hooks / "fail-pins").write_text("")
    with pytest.raises(HookFailed):
        _cut(db, repo, hooks)
    (hooks / "fail-pins").unlink()
    db.info.dbname = "rapid"
    with pytest.raises(ReleaseRefused, match="database target"):
        _cut(db, repo, hooks, resume="rebuild-v0.1")


def test_resume_without_a_row_reenters_at_migrate(setup):
    """A cut that failed in migrate has a pushed tag and no row."""
    repo, hooks, log, db = setup
    (hooks / "fail-migrate").write_text("")
    with pytest.raises(HookFailed):
        _cut(db, repo, hooks)
    assert db.release("rebuild-v0.1") is None
    (hooks / "fail-migrate").unlink()
    log.write_text("")
    release, _ = _cut(db, repo, hooks, resume="rebuild-v0.1")
    assert release.state == "complete"
    assert log_lines(log)[0].startswith("hook:migrate")
    assert db.release("rebuild-v0.1")["cut_by"] == "tester"


def test_resume_refuses_a_ref_that_is_not_the_tags_commit(setup):
    repo, hooks, _log, db = setup
    (hooks / "fail-build").write_text("")
    with pytest.raises(HookFailed):
        _cut(db, repo, hooks)
    git(repo, "checkout", "-q", "-b", "later")
    (repo / "later.txt").write_text("later\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "later")
    with pytest.raises(ReleaseRefused, match="not at --ref"):
        _cut(db, repo, hooks, resume="rebuild-v0.1", ref="later")


def test_resume_of_an_unknown_tag_is_usage(setup):
    repo, hooks, _log, db = setup
    with pytest.raises(ReleaseUsage, match="no such tag"):
        _cut(db, repo, hooks, resume="rebuild-v0.9")


def test_second_cut_takes_the_next_tag(setup):
    repo, hooks, _log, db = setup
    _cut(db, repo, hooks)
    release, _ = _cut(db, repo, hooks)
    assert release.tag == "rebuild-v0.2"


def test_verify_passes_then_reports_a_moved_remote_tag_and_live_drift(setup, tmp_path):
    repo, hooks, log, db = setup
    _cut(db, repo, hooks)
    write_hooks(hooks, log, names=("inspect",))
    assert core.verify(db, "rebuild-v0.1", repo, hooks, out=lambda _l: None) == []

    write_hooks(hooks, log, names=("inspect",),
                results={"inspect": '{"deployments": {"rapid-rebuild": "rapid-rebuild:8"}}'})
    db.apply("20260924-08-releases.sql", "f" * 64)
    problems = core.verify(db, "rebuild-v0.1", repo, hooks, out=lambda _l: None)
    assert any("different sha256" in p for p in problems)
    assert any("live job definitions" in p for p in problems)
