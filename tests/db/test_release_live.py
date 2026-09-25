"""Releases against a live PostgreSQL with the migration stream applied:
the 20260924-08 schema, ``cut``/``show``/``verify`` end to end, the
released-image promotion rule (R8), the execution record's release column
and a released run's Batch submission (R7).

Skips cleanly if PGHOST is unset (see conftest.py). ``cut`` commits at
every checkpoint by design, so the end-to-end test uses a fresh random tag
and digest; CI's database is created fresh per job. Everything else runs
inside the conftest's never-committed transaction.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import psycopg2
import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.launch import batch as launch_batch
from rapidpipe.release import core
from rapidpipe.runs import repository as repo
from tests.unit.fakebatch import FakeBatch
from tests.unit.releaserepo import git, isolate_git, log_lines, write_hooks

from .test_repository import _make_run, _make_unit, _register_simple_instance

REPO_ROOT = Path(__file__).resolve().parents[2]


def _digest() -> str:
    return "sha256:" + "".join(random.choice("0123456789abcdef") for _ in range(64))


def _tag() -> str:
    return f"rebuild-v0.{random.randrange(10**6, 10**9)}"


def _insert_release(conn, tag, digest, state="complete", deployments=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, image_digest, "
            "state, cut_by) VALUES (%s, %s, %s, %s, %s, 'test')",
            (tag, "a" * 40, "20260924-08-releases.sql", digest, state))
        for consumer, job_definition in (deployments or {}).items():
            cur.execute(
                "INSERT INTO release_deployments (release, consumer, job_definition, "
                "deployed_by) VALUES (%s, %s, %s, 'test')", (tag, consumer, job_definition))


def _candidate_with_record(conn, run_id, execution_record):
    """A registered candidate from a selected attempt with this record."""
    unit_id = new_ulid()
    _make_unit(conn, run_id, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "difference", unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location="runs/x", scheduler_job_id="j",
        execution_record={"source_revision": "a" * 40, "schema_version": "1",
                          "settings_hash": "h", **execution_record})
    repo.select_attempt(conn, attempt_id)
    instance = _register_simple_instance(
        conn, run_id, "difference", attempt_id, logical_key={"k": new_ulid()})
    return instance, attempt_id


# ======================================================================
# schema
# ======================================================================

@pytest.mark.parametrize("column, value", [
    ("source_revision", "not-a-sha"), ("image_digest", "sha256:short"), ("state", "cutting")])
def test_releases_checks_refuse_bad_values(conn, column, value):
    values = {"tag": _tag(), "source_revision": "a" * 40, "schema_version": "x",
              "image_digest": None, "state": "migrated", "cut_by": "t", column: value}
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO releases (tag, source_revision, schema_version, image_digest, "
                "state, cut_by) VALUES (%(tag)s, %(source_revision)s, %(schema_version)s, "
                "%(image_digest)s, %(state)s, %(cut_by)s)", values)
        cur.execute("ROLLBACK TO SAVEPOINT s")


def test_create_run_records_its_release_and_refuses_an_unknown_one(conn):
    tag = _tag()
    _insert_release(conn, tag, _digest())
    run_id = _make_run(conn, release=tag)
    with conn.cursor() as cur:
        cur.execute("SELECT release FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] == tag
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            _make_run(conn, release="rebuild-v0.0")
        cur.execute("ROLLBACK TO SAVEPOINT s")


@pytest.mark.parametrize("value, stored", [
    ("rebuild-v0.7", "rebuild-v0.7"), ("unreleased", None), (None, None)])
def test_record_attempt_result_writes_the_release(conn, value, stored):
    run_id = _make_run(conn)
    _instance, attempt_id = _candidate_with_record(conn, run_id, {"release": value})
    with conn.cursor() as cur:
        cur.execute("SELECT release FROM execution_records WHERE attempt = %s", (attempt_id,))
        assert cur.fetchone()[0] == stored


# ======================================================================
# promotion eligibility: the released-image rule (R8)
# ======================================================================

def test_promotion_accepts_the_image_of_a_complete_release(conn):
    tag, digest = _tag(), _digest()
    _insert_release(conn, tag, digest)
    run_id = _make_run(conn)
    instance, _ = _candidate_with_record(
        conn, run_id, {"image_digest": digest, "release": tag})
    promotion_id = repo.promote_run(conn, run_id, "ops", "released")
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        assert cur.fetchone()[0] == "current"
        cur.execute("SELECT request_context FROM promotions WHERE id = %s", (promotion_id,))
        assert cur.fetchone()[0] == {"run": run_id}


@pytest.mark.parametrize("case", ["unknown digest", "no digest", "other release", "not complete"])
def test_promotion_refuses_an_unreleased_image_naming_the_attempt(conn, case):
    tag, digest = _tag(), _digest()
    _insert_release(conn, tag, digest, state="deployed" if case == "not complete" else "complete")
    other = _tag()
    _insert_release(conn, other, _digest())
    record = {
        "unknown digest": {"image_digest": _digest()},
        "no digest": {},
        "other release": {"image_digest": digest, "release": other},
        "not complete": {"image_digest": digest},
    }[case]
    run_id = _make_run(conn)
    _instance, attempt_id = _candidate_with_record(conn, run_id, record)
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT s")
        with pytest.raises(repo.PromotionRefused, match=attempt_id):
            repo.promote_run(conn, run_id, "ops", "unreleased")
        cur.execute("ROLLBACK TO SAVEPOINT s")


def test_allow_unreleased_promotes_and_records_the_exception(conn):
    run_id = _make_run(conn)
    instance, attempt_id = _candidate_with_record(conn, run_id, {"image_digest": _digest()})
    promotion_id = repo.promote_run(conn, run_id, "ops", "trial", allow_unreleased=True)
    with conn.cursor() as cur:
        cur.execute("SELECT request_context FROM promotions WHERE id = %s", (promotion_id,))
        assert cur.fetchone()[0] == {
            "run": run_id, "allow_unreleased": True, "attempts": [attempt_id]}
    # A rollback restores the prior selection without re-checking releases.
    repo.rollback_promotion(conn, promotion_id, "ops", "undo")
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        assert cur.fetchone()[0] == "candidate"


# ======================================================================
# a released run submits to its release's revision (R7)
# ======================================================================

@pytest.mark.parametrize("kind, expected", [
    ("scratch", "rapid-rebuild:7"), ("production", "rapid-rebuild-production:5")])
def test_released_run_submits_its_release_revision(conn, monkeypatch, kind, expected):
    tag = _tag()
    _insert_release(conn, tag, _digest(), deployments={
        "rapid-rebuild": "rapid-rebuild:7",
        "rapid-rebuild-production": "rapid-rebuild-production:5"})
    run_id = _make_run(conn, kind=kind, release=tag)
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "q")
    monkeypatch.setenv(f"RAPIDPIPE_OUTPUTS_ROOT_{kind.upper()}", "/tmp/release-outputs")
    monkeypatch.setenv(f"RAPIDPIPE_BATCH_JOB_DEFINITION_{kind.upper()}", "unversioned")
    fake = FakeBatch()
    fake.job_definitions[expected] = "ACTIVE"
    launch_batch.submit_unit(
        _NoCommit(conn), run_id=run_id, stage="difference", unit_kind="detector-image",
        unit_id="e1/SCA01", inputs_location="/tmp/in", client=fake)
    assert fake.submitted[0]["jobDefinition"] == expected


class _NoCommit:
    """The conftest connection, with commit made a no-op so the test's
    rows stay inside its never-committed transaction."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ======================================================================
# cut / show / verify end to end
# ======================================================================

def test_cut_show_and_verify_against_the_live_schema(conn, tmp_path, monkeypatch):
    """The tagged tree's migrations are this checkout's own files, so the
    database CI just migrated has every one of them at its sha256."""
    isolate_git(monkeypatch, tmp_path)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "rebuild", str(origin))
    work = tmp_path / "rapid"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-B", "rebuild")
    shutil.copytree(REPO_ROOT / "database" / "migrations", work / "database" / "migrations")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "migrations")
    git(work, "push", "-q", "origin", "rebuild")
    git(work, "fetch", "-q", "origin")
    log = tmp_path / "order.log"
    hooks = write_hooks(tmp_path / "hooks", log)
    write_hooks(hooks, log, names=("inspect",))
    tag = _tag()

    release = core.cut(conn, repo=work, tag=tag, hooks_dir=hooks, by="ci",
                       out=lambda _line: None)

    assert release.state == "complete"
    assert [line.split(":")[1] for line in log_lines(log)] == [
        "migrate", "build", "deploy", "pins"]
    shown = core.show(conn, tag)
    assert shown.source_revision == git(work, "rev-parse", "HEAD")
    assert shown.schema_version == max(
        p.name for p in (REPO_ROOT / "database" / "migrations").glob("2*.sql"))
    assert {d.consumer: d.job_definition for d in shown.deployments} == {
        "rapid-rebuild": "rapid-rebuild:7",
        "rapid-rebuild-production": "rapid-rebuild-production:5"}
    assert tag in [r.tag for r in core.list_releases(conn)]
    assert core.verify(conn, tag, work, hooks, out=lambda _line: None) == []

    # Clean up the committed rows so reruns against a kept database stay tidy.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM release_deployments WHERE release = %s", (tag,))
        cur.execute("DELETE FROM releases WHERE tag = %s", (tag,))
    conn.commit()

