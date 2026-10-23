"""The foundation verbs: each one's exit code and output on one success
and one refusal, against the foundation database (``database/schema.sql``).

Each verb is run in-process through ``rapidpipe.cli.main.main``, which
connects from the ``PG*`` environment like any invocation; a refusal is a
non-zero exit per ``rapidpipe.exitcodes`` with the reason on stderr.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.cli.main import main
from rapidpipe.exitcodes import ExitCode
from tests.db.foundation_helpers import DATASET, FIELD_KEY, SURVEY, new_job, new_run, open_bindings

WHO_WHY = ["--who", "tester", "--why", "verb test"]


@pytest.fixture()
def conn(connect):
    return connect()


def _run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _group_file(tmp_path, items):
    path = tmp_path / "group.json"
    path.write_text(json.dumps(items))
    return str(path)


def test_run_create_prints_the_new_runs_id(capsys, conn, tmp_path):
    settings = tmp_path / "settings.toml"
    settings.write_text("[associate]\nradius = 1.0\n")
    code, out, err = _run(capsys, "run", "create", *WHO_WHY, "--revision", "abc123",
                          "--settings", str(settings), "--survey", SURVEY, "--dataset", DATASET)
    assert (code, err) == (ExitCode.SUCCESS, "")
    with conn.cursor() as cur:
        cur.execute("SELECT who, revision, settings, survey, dataset FROM runs WHERE id = %s",
                    (int(out),))
        assert cur.fetchone() == ("tester", "abc123", {"associate": {"radius": 1.0}}, SURVEY, DATASET)


def test_run_create_refuses_an_unreadable_settings_file(capsys, conn, tmp_path):
    code, out, err = _run(capsys, "run", "create", *WHO_WHY, "--revision", "abc123",
                          "--settings", str(tmp_path / "absent.toml"),
                          "--survey", SURVEY, "--dataset", DATASET)
    assert (code, out) == (ExitCode.USAGE, "")
    assert "absent.toml" in err
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM runs")
        assert cur.fetchone() == (0,)


def test_run_show_prints_the_run_with_its_live_binding_and_jobs(capsys, conn):
    run = new_run(conn, live=True)
    job = new_job(conn, run)
    code, out, err = _run(capsys, "run", "show", str(run))
    assert (code, err) == (ExitCode.SUCCESS, "")
    shown = json.loads(out)
    assert (shown["id"], shown["survey"], shown["live"] is not None) == (run, SURVEY, True)
    assert [j["id"] for j in shown["jobs"]] == [job]


def test_run_show_refuses_a_run_that_does_not_exist(capsys, conn):
    code, out, err = _run(capsys, "run", "show", "999999")
    assert (code, out) == (ExitCode.USAGE, "")
    assert "run 999999 does not exist" in err


def test_job_show_prints_the_job_with_its_inputs(capsys, conn):
    run = new_run(conn)
    job = new_job(conn, run, expects=[{"product": "objects", "key": FIELD_KEY, "binding": None}])
    code, out, err = _run(capsys, "job", "show", str(job))
    assert (code, err) == (ExitCode.SUCCESS, "")
    shown = json.loads(out)
    assert (shown["id"], shown["run"], shown["state"]) == (job, run, "launched")
    assert [(i["kind"], i["product"]) for i in shown["inputs"]] == [("expects", "objects")]


def test_job_show_refuses_a_job_that_does_not_exist(capsys, conn):
    code, out, err = _run(capsys, "job", "show", "999999")
    assert (code, out) == (ExitCode.USAGE, "")
    assert "job 999999 does not exist" in err


def test_official_change_applies_the_group_and_prints_its_id(capsys, conn, tmp_path):
    job = new_job(conn, new_run(conn))
    group = _group_file(tmp_path, [{"open": {"product": "objects", "key": FIELD_KEY, "job": job}}])
    code, out, err = _run(capsys, "official", "change", "--group", group, *WHO_WHY)
    assert (code, err) == (ExitCode.SUCCESS, "")
    with conn.cursor() as cur:
        cur.execute("SELECT job, opened_by, opened_why FROM official WHERE opened_group = %s",
                    (int(out),))
        assert cur.fetchall() == [(job, "tester", "verb test")]


def test_official_change_refuses_a_closed_binding_naming_it(capsys, conn, tmp_path):
    job = new_job(conn, new_run(conn))
    opened = _group_file(tmp_path, [{"open": {"product": "objects", "key": FIELD_KEY, "job": job}}])
    assert _run(capsys, "official", "change", "--group", opened, *WHO_WHY)[0] == ExitCode.SUCCESS
    (binding, *_), = open_bindings(conn)
    close = _group_file(tmp_path, [{"close": binding}])
    assert _run(capsys, "official", "change", "--group", close, *WHO_WHY)[0] == ExitCode.SUCCESS
    code, out, err = _run(capsys, "official", "change", "--group", close, *WHO_WHY)
    assert (code, out) == (ExitCode.FAILURE, "")
    assert f"refused: binding {binding} is no longer open" in err


def test_official_undo_reverses_a_group_and_prints_the_new_groups_id(capsys, conn, tmp_path):
    job = new_job(conn, new_run(conn))
    group = _group_file(tmp_path, [{"open": {"product": "objects", "key": FIELD_KEY, "job": job}}])
    _, opened, _ = _run(capsys, "official", "change", "--group", group, *WHO_WHY)
    code, out, err = _run(capsys, "official", "undo", opened.strip(), *WHO_WHY)
    assert (code, err) == (ExitCode.SUCCESS, "")
    assert int(out) != int(opened)
    assert open_bindings(conn) == []


def test_official_undo_refuses_when_a_binding_has_since_moved(capsys, conn, tmp_path):
    run = new_run(conn)
    first, second = new_job(conn, run), new_job(conn, run)
    group = _group_file(tmp_path, [{"open": {"product": "objects", "key": FIELD_KEY, "job": first}}])
    _, opened, _ = _run(capsys, "official", "change", "--group", group, *WHO_WHY)
    (binding, *_), = open_bindings(conn)
    moved = _group_file(tmp_path, [{"close": binding,
                                    "open": {"product": "objects", "key": FIELD_KEY, "job": second}}])
    assert _run(capsys, "official", "change", "--group", moved, *WHO_WHY)[0] == ExitCode.SUCCESS
    code, out, err = _run(capsys, "official", "undo", opened.strip(), *WHO_WHY)
    assert (code, out) == (ExitCode.FAILURE, "")
    assert f"refused: binding {binding} is no longer open" in err


class _Batch:
    def __init__(self):
        self.submitted = []

    def submit_job(self, **request):
        self.submitted.append(request)
        return {"jobId": "batch-1"}


def test_job_submit_records_the_job_and_submits_job_run_for_it(capsys, conn, monkeypatch):
    from rapidpipe import launch
    from rapidpipe.cli import main as cli

    batch = _Batch()
    monkeypatch.setattr(launch, "batch_client", lambda: batch)
    monkeypatch.setattr(cli, "_stage_run", lambda stage: None)
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    run = new_run(conn, live=True)
    code, out, err = _run(capsys, "job", "submit", "--run", str(run), "--stage", "difference",
                          "--scope", "{}", "--job-definition", "rapid-rebuild:30")
    assert (code, err) == (ExitCode.SUCCESS, "")
    printed = json.loads(out)
    assert printed["batch_job"] == "batch-1"
    assert batch.submitted[0]["containerOverrides"]["command"] == ["job", "run", "--job",
                                                                    str(printed["job"])]
    with conn.cursor() as cur:
        cur.execute("SELECT run, stage, state, batch_job_id FROM jobs WHERE id = %s", (printed["job"],))
        assert cur.fetchone() == (run, "difference", "launched", "batch-1")


def test_job_submit_refuses_without_a_queue_naming_the_recorded_job(capsys, conn, monkeypatch):
    from rapidpipe.cli import main as cli

    monkeypatch.setattr(cli, "_stage_run", lambda stage: None)
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_QUEUE", raising=False)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    run = new_run(conn, live=True)
    code, out, err = _run(capsys, "job", "submit", "--run", str(run), "--stage", "difference",
                          "--scope", "{}", "--job-definition", "rapid-rebuild")
    assert code == ExitCode.USAGE and out == ""
    assert "recorded, not submitted" in err and "RAPIDPIPE_BATCH_JOB_QUEUE" in err


def test_job_submit_with_job_submits_a_new_attempt_of_the_existing_job(capsys, conn, monkeypatch):
    from rapidpipe import launch

    batch = _Batch()
    monkeypatch.setattr(launch, "batch_client", lambda: batch)
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    job = new_job(conn, new_run(conn, live=True))
    code, out, err = _run(capsys, "job", "submit", "--job", str(job), "--job-definition", "rapid-rebuild:37")
    assert (code, err) == (ExitCode.SUCCESS, "")
    assert json.loads(out) == {"job": job, "batch_job": "batch-1"}
    assert batch.submitted[0]["jobName"].endswith(f"-associate-{job}")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(batch_job_id) FROM jobs")
        assert cur.fetchone() == (1, "batch-1")
