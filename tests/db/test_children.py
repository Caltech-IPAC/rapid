"""sources' child tables: one per UT observation date and SCA, inheriting it, made and loaded by COPY at publish.

``database/schema.sql``'s ``create_catalog_child`` makes a child that
inherits ``sources``; ``rapidpipe.foundation.publish`` calls it before it
loads a set and then loads each child by ``COPY``. Run by foundation.yml
against database/schema.sql.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg2 import errors

from rapidpipe.foundation import publish
from tests.db.foundation_helpers import (
    DATASET, new_job, new_run, wait_until_blocked, write_attempt,
)

#: MJD 61348 is 2026-11-04; MJD days begin at 0h UT.
NOV4 = 61348.0


def source_rows(mjdobs: float, sca: int, n: int = 3, expid: int = 1) -> list[dict]:
    """``n`` sources of one set, every column of ``sources`` but ``job``."""
    return [{"sid": sid, "dataset": DATASET, "expid": expid, "sca": sca, "fid": 1, "pid": 1,
             "id": sid, "isdiffpos": True, "field": 1, "hp6": 1, "hp9": 1, "mjdobs": mjdobs,
             "ra": 10.0 + sid, "dec": -5.0, "xfit": 1.0, "yfit": 1.0, "xerr": 0.1, "yerr": 0.1,
             "fluxfit": 100.0, "fluxerr": 1.0, "flags": 0, "qfit": 0.0, "cfit": 0.0,
             "redchi": 1.0, "npixfit": 25, "sharpness": 0.5, "roundness1": 0.0,
             "roundness2": 0.0, "peak": 10.0, "sumrat": 0.5, "nneg": 0, "nbad": 0,
             "rb": None, "rblabel": None} for sid in range(1, n + 1)]


def _publish(conn, tmp_path, rows: list[dict], live_run: int | None = None) -> int:
    run = live_run if live_run is not None else new_run(conn)
    key = {"dataset": DATASET, "expid": rows[0]["expid"], "sca": rows[0]["sca"], "fid": 1}
    expects = [{"product": "sources", "key": key, "binding": None}] if live_run else ()
    job = new_job(conn, run, expects=expects)
    publish(conn, job, 1, write_attempt(tmp_path / f"job-{job}", {"sources": rows}))
    return job


def _children(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT inhrelid::regclass::text FROM pg_inherits"
                    " WHERE inhparent = 'sources'::regclass ORDER BY 1")
        children = [r[0] for r in cur.fetchall()]
    conn.commit()
    return children


def _one(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_a_published_set_lands_in_its_date_and_sca_child_and_the_parent_holds_none(connect, tmp_path):
    conn = connect()
    job = _publish(conn, tmp_path, source_rows(NOV4 + 0.7, 8), live_run=new_run(conn, live=True))

    assert _one(conn, "SELECT tableoid::regclass::text, count(*) FROM sources GROUP BY 1") == [
        ("sources_20261104_8", 3)]
    assert _one(conn, "SELECT count(*) FROM ONLY sources") == [(0,)]
    assert _one(conn, "SELECT count(*) FROM sources_official WHERE job = %s", (job,)) == [(3,)]
    assert _one(conn, "SELECT relpersistence FROM pg_class WHERE relname = 'sources_20261104_8'") == [
        ("p",)]                                                  # logged


def test_copy_keeps_nulls_booleans_nan_and_every_digit(connect, tmp_path):
    conn = connect()
    rows = source_rows(NOV4 + 0.123456789012345, 5, n=2)
    rows[0].update(isdiffpos=False, rb=float("nan"), rblabel=-1, ra=123.45678901234567)
    rows[1].update(sumrat=None, nneg=None, nbad=None)
    job = _publish(conn, tmp_path, rows)

    got = _one(conn, "SELECT sid, isdiffpos, rb, rblabel, ra, mjdobs, sumrat, nneg, nbad, job"
                     " FROM sources_20261104_5 ORDER BY sid")
    assert got[0][:4] == (1, False, got[0][2], -1) and math.isnan(got[0][2])
    assert got[0][4] == 123.45678901234567 and got[0][5] == NOV4 + 0.123456789012345
    assert got[1][1:4] == (True, None, None) and got[1][6:9] == (None, None, None)
    assert {r[9] for r in got} == {job}


def test_a_child_is_reused_and_each_new_date_or_sca_gets_its_own(connect, tmp_path):
    conn = connect()
    _publish(conn, tmp_path, source_rows(NOV4 + 0.2, 8))
    _publish(conn, tmp_path, source_rows(NOV4 + 0.9, 8))
    _publish(conn, tmp_path, source_rows(NOV4 + 0.5, 17))
    _publish(conn, tmp_path, source_rows(NOV4 + 1.1, 8))

    assert _children(conn) == ["sources_20261104_17", "sources_20261104_8", "sources_20261105_8"]
    assert _one(conn, "SELECT tableoid::regclass::text, count(*) FROM sources GROUP BY 1 ORDER BY 1") == [
        ("sources_20261104_17", 3), ("sources_20261104_8", 6), ("sources_20261105_8", 3)]


@pytest.mark.parametrize("mjdobs, child", [(NOV4 + 0.99999, "sources_20261104_3"),
                                           (NOV4 + 1.0, "sources_20261105_3")])
def test_the_child_is_named_for_the_ut_date_of_mjdobs(conn, mjdobs, child):
    with conn.cursor() as cur:
        cur.execute("SELECT create_catalog_child('sources', %s, 3)", (mjdobs,))
        assert cur.fetchone()[0] == child


def _job(cur) -> int:
    cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset)"
                " VALUES ('t', 't', 'r', '{}', 'S', 'D') RETURNING id")
    cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, 'difference', '{}')"
                " RETURNING id", (cur.fetchone()[0],))
    return cur.fetchone()[0]


def _insert(cur, table: str, job: int, row: dict) -> None:
    cur.execute(f"INSERT INTO {table} (job, {', '.join(row)})"
                f" VALUES (%s, {', '.join(['%s'] * len(row))})", (job, *row.values()))


def test_the_parent_refuses_a_row_and_a_child_refuses_another_date_or_sca(conn):
    with conn.cursor() as cur:
        job = _job(cur)
        cur.execute("SELECT create_catalog_child('sources', %s, 4)", (NOV4 + 0.5,))
        cur.execute("SAVEPOINT s")
        with pytest.raises(errors.CheckViolation, match="sources_holds_no_rows"):
            _insert(cur, "sources", job, source_rows(NOV4 + 0.5, 4, n=1)[0])
        cur.execute("ROLLBACK TO SAVEPOINT s")
        for mjdobs, sca in ((NOV4 + 0.5, 5), (NOV4 + 1.5, 4)):
            with pytest.raises(errors.CheckViolation, match="sources_20261104_4_date_sca"):
                _insert(cur, "sources_20261104_4", job, source_rows(mjdobs, sca, n=1)[0])
            cur.execute("ROLLBACK TO SAVEPOINT s")
        _insert(cur, "sources_20261104_4", job, source_rows(NOV4 + 0.5, 4, n=1)[0])
        with pytest.raises(errors.UniqueViolation):         # (job, sid) within the child
            _insert(cur, "sources_20261104_4", job, source_rows(NOV4 + 0.6, 4, n=1)[0])


def test_a_query_on_a_date_or_sca_skips_the_other_children(conn):
    with conn.cursor() as cur:
        for mjdobs, sca in ((NOV4 + 0.5, 4), (NOV4 + 0.5, 6), (NOV4 + 1.5, 4)):
            cur.execute("SELECT create_catalog_child('sources', %s, %s)", (mjdobs, sca))
        cur.execute("EXPLAIN SELECT * FROM sources WHERE sca = 6 AND mjdobs >= %s AND mjdobs < %s",
                    (NOV4, NOV4 + 1))
        plan = "\n".join(r[0] for r in cur.fetchall())
    assert "sources_20261104_6" in plan
    assert "sources_20261104_4" not in plan and "sources_20261105_4" not in plan


def test_the_function_makes_children_of_the_catalog_tables_only(conn):
    with conn.cursor() as cur:
        with pytest.raises(errors.RaiseException, match="not a catalog table with children"):
            cur.execute("SELECT create_catalog_child('jobs', %s, 1)", (NOV4,))


def test_two_callers_making_one_child_both_get_it(connect):
    """The second waits on the first's advisory lock, then finds the child made."""
    first, second, observer = connect(), connect(), connect()
    with first.cursor() as cur:
        cur.execute("SELECT create_catalog_child('sources', %s, 12)", (NOV4 + 0.3,))
        made = cur.fetchone()[0]
    with second.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    second.commit()

    def call():
        with second.cursor() as cur:
            cur.execute("SELECT create_catalog_child('sources', %s, 12)", (NOV4 + 0.6,))
            name = cur.fetchone()[0]
        second.commit()
        return name

    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(call)
        wait_until_blocked(observer, pid)
        first.commit()
        assert waiting.result(timeout=10) == made == "sources_20261104_12"
    assert _children(observer) == ["sources_20261104_12"]
