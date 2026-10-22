"""The publisher (foundation plan v5, Part 1 statement 6).

Run by foundation.yml against database/schema.sql; db-migrations.yml ignores it.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from rapidpipe.foundation import PublishError, apply, job_show, publish
from tests.db.foundation_helpers import (
    DATASET, FIELD_KEY, SURVEY, memberships_rows, new_job, new_run, objects_rows, open_bindings,
    wait_until_blocked, write_attempt,
)


def _count(conn, table, job):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE job = %s", (job,))
        n = cur.fetchone()[0]
    conn.commit()
    return n


def _attempt(tmp_path, conn, job, attempt=1):
    source_set = new_job(conn, new_run(conn))
    return write_attempt(tmp_path / f"job-{job}" / f"attempt-{attempt}", {
        "objects": objects_rows(), "memberships": memberships_rows(source_set)})


def test_publish_loads_its_outputs_once_and_a_retry_stops_even_without_files(connect, tmp_path):
    conn = connect()
    job = new_job(conn, new_run(conn))
    prefix = _attempt(tmp_path, conn, job)

    first = publish(conn, job, 1, prefix)
    retry = publish(conn, job, 1, prefix)
    shutil.rmtree(tmp_path / f"job-{job}")
    gone = publish(conn, job, 2, prefix)

    assert first["published"] and not retry["published"] and not gone["published"]
    assert (_count(conn, "objects", job), _count(conn, "memberships", job)) == (3, 3)
    shown = job_show(conn, job)
    assert (shown["state"], shown["published_attempt"]) == ("published", 1)
    assert shown["manifest"]["prefix"] == prefix


@pytest.mark.parametrize("damage", ["missing", "altered"])
def test_an_attempt_whose_outputs_do_not_verify_publishes_nothing(connect, tmp_path, damage):
    conn = connect()
    job = new_job(conn, new_run(conn))
    prefix = _attempt(tmp_path, conn, job)
    memberships = tmp_path / f"job-{job}" / "attempt-1" / "memberships.parquet"
    if damage == "missing":
        memberships.unlink()
    else:
        memberships.write_bytes(memberships.read_bytes() + b"x")

    with pytest.raises(PublishError):
        publish(conn, job, 1, prefix)

    assert (_count(conn, "objects", job), _count(conn, "memberships", job)) == (0, 0)
    assert job_show(conn, job)["state"] == "launched"


def test_a_live_runs_job_binds_the_keys_it_expects(connect, tmp_path):
    conn = connect()
    job = new_job(conn, new_run(conn, live=True),
                  expects=[{"product": "objects", "key": FIELD_KEY, "binding": None}])

    result = publish(conn, job, 1, _attempt(tmp_path, conn, job))

    assert result["group"] is not None and result["refusal"] is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM objects_official WHERE job = %s", (job,))
        assert cur.fetchone()[0] == 3


def test_a_job_whose_run_is_not_live_is_published_unbound_with_the_refusal_recorded(
        connect, tmp_path):
    conn = connect()
    new_run(conn, live=True)
    job = new_job(conn, new_run(conn),
                  expects=[{"product": "objects", "key": FIELD_KEY, "binding": None}])

    result = publish(conn, job, 1, _attempt(tmp_path, conn, job))

    assert result["published"] and result["group"] is None and result["live"] is False
    assert job_show(conn, job)["refusal"] == result["refusal"] and "not live" in result["refusal"]
    assert _count(conn, "objects", job) == 3
    assert open_bindings(conn, product="objects") == []


def test_publication_racing_a_cutover_publishes_unbound(connect, tmp_path):
    a, b = connect(), connect()
    old_run = new_run(a, live=True)
    bound = new_job(a, old_run, expects=[{"product": "objects", "key": FIELD_KEY, "binding": None}])
    publish(a, bound, 1, _attempt(tmp_path, a, bound))
    binding = open_bindings(a, product="objects")[0][0]
    live = [r for r in open_bindings(a) if r[4] == old_run][0][0]
    late = new_job(a, old_run, expects=[{"product": "objects", "key": FIELD_KEY,
                                         "binding": binding}])
    prefix = _attempt(tmp_path, a, late)
    new_run_id = new_run(a)
    replacement = new_job(a, new_run_id)

    with a.cursor() as cur:
        apply(cur, [
            {"close": binding, "open": {"product": "objects", "key": FIELD_KEY,
                                        "job": replacement}},
            {"close": live, "open": {"survey": SURVEY, "dataset": DATASET, "run": new_run_id}},
        ], "a", "cutover")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(publish, b, late, 1, prefix)
        wait_until_blocked(connect(), b.get_backend_pid())
        a.commit()
        result = pending.result(timeout=10)

    assert result["published"] and result["group"] is None
    assert f"binding {binding} is no longer open" in result["refusal"]
    assert _count(a, "objects", late) == 3
    assert [r[3] for r in open_bindings(a, product="objects")] == [replacement]
