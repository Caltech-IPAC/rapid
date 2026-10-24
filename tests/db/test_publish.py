"""The publisher (foundation plan v5, Part 1 statement 6; the amendment of 2026-10-24).

Run by foundation.yml against database/schema.sql; db-migrations.yml ignores it.
"""

from __future__ import annotations

import json
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
    assert [r[3] for r in open_bindings(conn, product="objects")] == [job]
    assert _count(conn, "objects", job) == 3


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


def _difference_attempt(tmp_path, job, founded, memberships, reads, radius=1e-4, fields=None):
    """A difference job's catalog outputs, with its read and the catalog key in the manifest.

    ``fields`` names the locked fields (the field rule's manifest); without it the manifest
    names its one field as one written before the rule did.
    """
    root = tmp_path / f"job-{job}" / "attempt-1"
    write_attempt(root, {"objects": [{"field": 1, **o, "dataset": DATASET, "survey": SURVEY}
                                     for o in founded],
                         "memberships": memberships})
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["reads"] = reads
    manifest["catalog"] = ({**FIELD_KEY, "radius": radius} if fields is None else
                           {"dataset": DATASET, "survey": SURVEY, "fields": fields, "radius": radius})
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root.as_uri()


def _member(job, owner, aid, sid, dra=0.0):
    return {"object_job": owner, "aid": aid, "source_job": job, "sid": sid,
            "dra": dra, "ddec": 0.0, "flux": 1.0}


def test_reconcile_attaches_a_founder_to_one_founded_since_the_read_and_records_the_read(
        connect, tmp_path):
    """Jobs A and B of one run both read an empty catalog and founded an object at one star,
    0.18 arcseconds apart; A publishes first; B's object is not inserted and its membership
    goes to A's, rebased; B's read (empty) and A's (empty) are in job_inputs; a job C that read
    A's object publishes its own founder elsewhere untouched."""
    conn = connect()
    run = new_run(conn)
    a, b, c = (new_job(conn, run) for _ in range(3))
    publish(conn, a, 1, _difference_attempt(tmp_path, a, [{"aid": 7, "ra0": 10.0, "dec0": -5.0}],
                                            [_member(a, a, 7, 1)], []))
    d = 0.5e-4
    result = publish(conn, b, 1, _difference_attempt(
        tmp_path, b, [{"aid": 8, "ra0": 10.0 + d, "dec0": -5.0}], [_member(b, b, 8, 1, dra=0.1e-4)], []))
    publish(conn, c, 1, _difference_attempt(
        tmp_path, c, [{"aid": 9, "ra0": 11.0, "dec0": -5.0}], [_member(c, a, 7, 1), _member(c, c, 9, 2)],
        [{"product": "objects", "key": FIELD_KEY, "source_job": a}]))

    assert result["published"]
    assert job_show(conn, b)["manifest"]["reconcile"] == {
        "objects_between": 1, "reconciled": 1, "attached_memberships": 1}
    with conn.cursor() as cur:
        cur.execute("SELECT job, aid FROM objects ORDER BY job")
        assert cur.fetchall() == [(a, 7), (c, 9)]
        cur.execute("SELECT object_job, aid, dra FROM memberships WHERE job = %s", (b,))
        ((owner, aid, dra),) = cur.fetchall()
    conn.commit()
    assert (owner, aid) == (a, 7) and dra == pytest.approx(0.1e-4 + d)
    assert [(i["product"], i["source_job"]) for i in job_show(conn, c)["inputs"]
            if i["kind"] == "input"] == [("objects", a)]
    assert job_show(conn, c)["manifest"]["reconcile"]["reconciled"] == 0


def test_two_founders_either_side_of_a_boundary_serialise_on_a_shared_lock_and_end_as_one_object(
        connect, tmp_path):
    """The field rule: job A founds in field 1 and job B in field 2, 0.18 arcseconds apart, both
    after an empty read, each locking both fields (each touched field and its neighbours). While
    another transaction holds field 1's lock B's publication waits; once it is released B's
    reconcile reads A's object in the neighbouring field and attaches to it."""
    a_conn, b_conn = connect(), connect()
    run = new_run(a_conn)
    a, b = new_job(a_conn, run), new_job(a_conn, run)
    publish(a_conn, a, 1, _difference_attempt(
        tmp_path, a, [{"aid": 7, "field": 1, "ra0": 10.0, "dec0": -5.0}], [_member(a, a, 7, 1)], [],
        fields=[1, 2]))
    prefix = _difference_attempt(
        tmp_path, b, [{"aid": 8, "field": 2, "ra0": 10.0 + 0.5e-4, "dec0": -5.0}],
        [_member(b, b, 8, 1)], [], fields=[2, 1])
    with a_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, 1)", (run,))
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(publish, b_conn, b, 1, prefix)
        wait_until_blocked(connect(), b_conn.get_backend_pid())
        a_conn.commit()
        result = pending.result(timeout=10)

    assert result["published"]
    assert job_show(a_conn, b)["manifest"]["reconcile"]["reconciled"] == 1
    with a_conn.cursor() as cur:
        cur.execute("SELECT job, aid, field FROM objects ORDER BY job")
        assert cur.fetchall() == [(a, 7, 1)]
        cur.execute("SELECT object_job, aid FROM memberships WHERE job = %s", (b,))
        assert cur.fetchall() == [(a, 7)]
    a_conn.commit()

