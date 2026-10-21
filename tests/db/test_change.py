"""The change operation and undo (foundation plan v5, Part 1 statement 5).

Run by foundation.yml against database/schema.sql; db-migrations.yml ignores it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from rapidpipe.foundation import Refused, apply, change, undo
from tests.db.foundation_helpers import (
    DATASET, FIELD_KEY, SURVEY, new_job, new_run, open_bindings, wait_until_blocked,
)


def _bind(conn, job, close=None):
    group = change(conn, [{"close": close, "open": {"product": "objects", "key": FIELD_KEY,
                                                     "job": job}}], "tester", "bind")
    return open_bindings(conn, product="objects")[0][0], group


def _official(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM official ORDER BY id")
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_a_group_with_a_stale_expected_binding_refuses_and_changes_nothing(connect):
    conn = connect()
    run = new_run(conn)
    first, second, third = (new_job(conn, run) for _ in range(3))
    stale, _ = _bind(conn, first)
    _bind(conn, second, close=stale)
    before = _official(conn)

    with pytest.raises(Refused) as refused:
        change(conn, [{"close": stale, "open": {"product": "objects", "key": FIELD_KEY,
                                                "job": third}}], "tester", "stale")

    assert refused.value.binding == stale
    assert _official(conn) == before


def _cutover(conn):
    """Run one live with K bound to its job; a second run's job; the cutover group between them."""
    old_run = new_run(conn, live=True)
    old_job = new_job(conn, old_run)
    binding, _ = _bind(conn, old_job)
    new_run_id = new_run(conn)
    new_job_id = new_job(conn, new_run_id)
    live = [r for r in open_bindings(conn) if r[4] == old_run][0][0]
    group = change(conn, [
        {"close": binding, "open": {"product": "objects", "key": FIELD_KEY, "job": new_job_id}},
        {"close": live, "open": {"survey": SURVEY, "dataset": DATASET, "run": new_run_id}},
    ], "tester", "cutover")
    return old_run, old_job, binding, new_run_id, new_job_id, group


def test_undo_restores_the_previous_bindings_as_new_rows(connect):
    conn = connect()
    old_run, old_job, binding, _, _, cutover = _cutover(conn)

    undo(conn, cutover, "tester", "back")

    restored = {(r[1], r[3], r[4]) for r in open_bindings(conn)}
    assert restored == {("objects", old_job, None), (None, None, old_run)}
    assert binding not in {r[0] for r in open_bindings(conn)}


def test_undo_refuses_when_one_of_its_bindings_has_since_moved(connect):
    conn = connect()
    _, _, _, new_run_id, new_job_id, cutover = _cutover(conn)
    moved, _ = open_bindings(conn, product="objects")[0][:2]
    _bind(conn, new_job(conn, new_run_id), close=moved)
    before = _official(conn)

    with pytest.raises(Refused) as refused:
        undo(conn, cutover, "tester", "back")

    assert refused.value.binding == moved
    assert _official(conn) == before


def test_two_groups_closing_one_binding_one_commits_and_the_other_refuses(connect):
    a, b = connect(), connect()
    run = new_run(a)
    first, second, third = (new_job(a, run) for _ in range(3))
    binding, _ = _bind(a, first)

    with a.cursor() as cur:
        apply(cur, [{"close": binding, "open": {"product": "objects", "key": FIELD_KEY,
                                                "job": second}}], "a", "first")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(change, b, [{"close": binding, "open": {
            "product": "objects", "key": FIELD_KEY, "job": third}}], "b", "second")
        wait_until_blocked(connect(), b.get_backend_pid())
        a.commit()
        with pytest.raises(Refused) as refused:
            pending.result(timeout=10)

    assert refused.value.binding == binding
    assert [(r[1], r[3]) for r in open_bindings(a)] == [("objects", second)]
    assert third not in {r[3] for r in _official(a)}
