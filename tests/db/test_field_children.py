"""The field children of objects, memberships, packets and forced: made, loaded by COPY and read at publish.

``database/schema.sql``'s ``create_field_child`` makes ``<parent>_<field>``,
inheriting the parent with a ``CHECK`` on its field; ``rapidpipe.foundation.publish``
files each row under its object's field, loads each child by ``COPY`` and
records the job's fields in ``job_fields``; ``<parent>_in(fields)``,
``<parent>_in(fields, run)``, ``<parent>_of(jobs)`` and ``objects_official_in(fields)`` read the children
only (rapid_docs decisions.md, field-children). Run by foundation.yml against
database/schema.sql.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg2.extensions
from psycopg2 import errors, sql

from rapidpipe.foundation import publish
from rapidpipe.foundation.children import FIELD_PARENTS, field_child_name
from tests.db.foundation_helpers import (
    DATASET, SURVEY, new_job, new_run, wait_until_blocked, write_attempt,
)


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _children(conn, parent: str) -> list[str]:
    return [r[0] for r in _rows(conn, "SELECT inhrelid::regclass::text FROM pg_inherits"
                                      " WHERE inhparent = %s::regclass ORDER BY 1", (parent,))]


def _object(aid: int, field: int) -> dict:
    return {"aid": aid, "dataset": DATASET, "survey": SURVEY, "field": field,
            "ra0": 10.0 + aid, "dec0": -5.0}


def _member(job: int, aid: int, field: int, sid: int) -> dict:
    return {"object_job": job, "aid": aid, "field": field, "source_job": job, "sid": sid,
            "dra": 1e-6 * sid, "ddec": 0.0, "flux": 100.0 + sid}


def _packet(source_job: int, aid: int, field: int, sid: int) -> dict:
    return {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 1, "object_job": source_job, "aid": aid,
            "field": field, "source_job": source_job, "sid": sid, "trigger_mjd": 61348.2,
            "band": "F184", "schema_version": "00.07", "filename": "p.avro",
            "byte_offset": 100 * sid, "byte_length": 100}


def _forced(aid: int, field: int, forced_id: int) -> dict:
    return {"forced_id": forced_id, "aid": aid, "field": field, "rid": 1, "pid": 1, "expid": 1,
            "sca": 8, "fid": 1, "band": "F184", "mjdobs": 61348.2, "ra": 10.0, "dec": -5.0,
            "x": 1.5, "y": 2.5, "psf_flux": float("nan"), "psf_fluxerr": None,
            "science_flux": 1.0 / 3.0, "science_fluxerr": 1.0, "flags": 0, "time_proc": 0.0}


def _xobject(xaid: int, field: int) -> dict:
    return {"xaid": xaid, "dataset": DATASET, "survey": SURVEY, "field": field,
            "ra0": 20.0 + xaid, "dec0": -5.0, "flux0": 7.5}


def _xmember(job: int, xaid: int, field: int, xsid: int) -> dict:
    return {"object_job": job, "xaid": xaid, "field": field, "source_job": job, "xsid": xsid,
            "dra": -1e-6 * xsid, "ddec": 0.0, "flux": 50.0 + xsid}


def _publish_two_fields(conn, tmp_path) -> tuple[int, int, int]:
    """A difference job founding objects and xobjects in fields 7 and 9 and a packet job carrying
    both fields' objects; returns (run, difference job, packet job)."""
    run = new_run(conn)
    diff = new_job(conn, run)
    publish(conn, diff, 1, write_attempt(tmp_path / f"job-{diff}", {
        "objects": [_object(1, 7), _object(2, 7), _object(3, 9)],
        "memberships": [_member(diff, 1, 7, 1), _member(diff, 2, 7, 2), _member(diff, 3, 9, 3)],
        "xobjects": [_xobject(1, 7), _xobject(4, 9)],
        "xmemberships": [_xmember(diff, 1, 7, 1), _xmember(diff, 4, 9, 2), _xmember(diff, 4, 9, 3)]}))
    pkt = new_job(conn, run)
    publish(conn, pkt, 1, write_attempt(tmp_path / f"job-{pkt}", {
        "packets": [_packet(diff, 1, 7, 1), _packet(diff, 3, 9, 3)],
        "forced": [_forced(1, 7, 11), _forced(3, 9, 33)]}))
    return run, diff, pkt


def test_each_row_lands_in_its_objects_field_child_and_the_parents_hold_none(connect, tmp_path):
    conn = connect()
    _, diff, pkt = _publish_two_fields(conn, tmp_path)
    for parent in FIELD_PARENTS:
        assert _children(conn, parent) == [f"{parent}_7", f"{parent}_9"]
        assert _rows(conn, f"SELECT count(*) FROM ONLY {parent}") == [(0,)]
        assert _rows(conn, f"SELECT DISTINCT tableoid::regclass::text, field FROM {parent}"
                           " ORDER BY 1") == [(f"{parent}_7", 7), (f"{parent}_9", 9)]
    assert _rows(conn, "SELECT job, parent, field FROM job_fields ORDER BY 1, 2, 3") == [
        (diff, "memberships", 7), (diff, "memberships", 9), (diff, "objects", 7), (diff, "objects", 9),
        (diff, "xmemberships", 7), (diff, "xmemberships", 9), (diff, "xobjects", 7),
        (diff, "xobjects", 9), (pkt, "forced", 7), (pkt, "forced", 9), (pkt, "packets", 7), (pkt, "packets", 9)]
    # COPY keeps a NaN, a null and every digit of a float.
    assert _rows(conn, "SELECT psf_flux = 'NaN', psf_fluxerr, science_flux FROM forced_7") == [
        (True, None, 1.0 / 3.0)]


def test_the_parent_refuses_a_row_and_a_child_refuses_another_field(connect):
    conn = connect()
    run = new_run(conn)
    job = new_job(conn, run)
    with conn.cursor() as cur:
        cur.execute("SELECT create_field_child('objects', 4)")
        row = (job, 1, DATASET, SURVEY, 5, 10.0, -5.0)
        with pytest.raises(errors.CheckViolation, match="objects_holds_no_rows"):
            cur.execute("INSERT INTO objects (job, aid, dataset, survey, field, ra0, dec0)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)", row)
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT create_field_child('objects', 4)")
        with pytest.raises(errors.CheckViolation, match="objects_4_field"):
            cur.execute("INSERT INTO objects_4 (job, aid, dataset, survey, field, ra0, dec0)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)", row)
    conn.rollback()


def test_the_function_makes_field_children_of_the_four_tables_only(connect):
    conn = connect()
    with conn.cursor() as cur:
        with pytest.raises(errors.RaiseException, match="not a table with field children"):
            cur.execute("SELECT create_field_child('sources', 1)")
    conn.rollback()
    with conn.cursor() as cur:
        with pytest.raises(errors.RaiseException, match="not a tessellation field"):
            cur.execute("SELECT create_field_child('objects', -1)")
    conn.rollback()


@pytest.mark.parametrize("parent", FIELD_PARENTS)
def test_the_python_child_name_is_the_functions(connect, parent):
    conn = connect()
    assert _rows(conn, "SELECT field_child_name(%s, 123456)", (parent,)) == [
        (field_child_name(parent, 123456),)]


def test_two_callers_making_one_child_both_get_it(connect):
    """The second waits on the first's advisory lock, then finds the child made."""
    first, second, observer = connect(), connect(), connect()
    with first.cursor() as cur:
        cur.execute("SELECT create_field_child('memberships', 12)")
        made = cur.fetchone()[0]
    with second.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    second.commit()

    def call():
        with second.cursor() as cur:
            cur.execute("SELECT create_field_child('memberships', 12)")
            name = cur.fetchone()[0]
        second.commit()
        return name

    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(call)
        wait_until_blocked(observer, pid)
        first.commit()
        assert waiting.result(timeout=10) == made == "memberships_12"
    assert _children(observer, "memberships") == ["memberships_12"]


def test_a_membership_attached_to_an_object_in_a_field_with_no_memberships_child_gets_one(
        connect, tmp_path):
    """Job A's object in field 1 was published with no membership; job B founded at the same
    star in field 2, and the reconcile attaches B's membership to A's object: its field, 1, has
    no memberships child until the loading transaction makes one."""
    conn = connect()
    run = new_run(conn)
    a, b = new_job(conn, run), new_job(conn, run)
    publish(conn, a, 1, write_attempt(tmp_path / f"job-{a}", {"objects": [_object(7, 1)]}))
    assert _children(conn, "memberships") == []
    root = tmp_path / f"job-{b}"
    write_attempt(root, {"objects": [{**_object(8, 2), "ra0": 17.0 + 0.5e-4}],
                         "memberships": [_member(b, 8, 2, 1)]})
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["catalog"] = {"dataset": DATASET, "survey": SURVEY, "fields": [1, 2], "radius": 1e-4}
    (root / "manifest.json").write_text(json.dumps(manifest))

    result = publish(conn, b, 1, root.as_uri())

    assert result["published"]
    assert _rows(conn, "SELECT job, aid, field FROM objects") == [(a, 7, 1)]
    assert _children(conn, "memberships") == ["memberships_1", "memberships_2"]
    assert _rows(conn, "SELECT tableoid::regclass::text, object_job, aid, field FROM memberships") == [
        ("memberships_1", a, 7, 1)]
    assert _rows(conn, "SELECT parent, field FROM job_fields WHERE job = %s ORDER BY 1", (b,)) == [
        ("memberships", 1)]


def test_the_readers_give_the_rows_the_parents_give(connect, tmp_path):
    conn = connect()
    run, diff, pkt = _publish_two_fields(conn, tmp_path)
    other = new_job(conn, run)                                # a job with no rows in any field
    for parent in FIELD_PARENTS:                              # as row text: NaN equals itself
        everything = _rows(conn, f"SELECT t::text FROM {parent} t ORDER BY 1")
        in_fields = _rows(conn, f"SELECT t::text FROM {parent}_in(ARRAY[7, 9, 8]) t ORDER BY 1")
        of_jobs = _rows(conn, f"SELECT t::text FROM {parent}_of(%s::bigint[]) t ORDER BY 1",
                        ([diff, pkt, other],))
        assert everything and in_fields == everything and of_jobs == everything, parent
        assert _rows(conn, f"SELECT t::text FROM {parent}_in(ARRAY[9]) t ORDER BY 1") == _rows(
            conn, f"SELECT t::text FROM {parent} t WHERE field = 9 ORDER BY 1"), parent
        for absent in ("ARRAY[8]::integer[]", "'{}'::integer[]"):
            assert _rows(conn, f"SELECT count(*) FROM {parent}_in({absent})") == [(0,)]
        assert _rows(conn, f"SELECT count(*) FROM {parent}_of(ARRAY[%s]::bigint[])", (other,)) == [(0,)]
    # objects_official_in is objects_official's rows of those fields: the sums of the official
    # memberships. The difference job's source set is bound here so its objects are official.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO official (product, key, job, opened_group, opened_by, opened_why)"
                    " VALUES ('sources', '{\"expid\": 1}', %s, 1, 't', 'test')", (diff,))
    conn.commit()
    view = _rows(conn, "SELECT * FROM objects_official ORDER BY job, aid")
    assert len(view) == 3
    assert _rows(conn, "SELECT * FROM objects_official_in(ARRAY[7, 9]) ORDER BY job, aid") == view
    assert _rows(conn, "SELECT * FROM objects_official_in(ARRAY[9]) ORDER BY job, aid") == [
        v for v in view if v[4] == 9]
    xview = _rows(conn, "SELECT * FROM xobjects_official ORDER BY job, xaid")
    assert len(xview) == 2 and [v[9] for v in xview] == [1, 2]   # nsources: xaid 1 one, xaid 4 two
    assert _rows(conn, "SELECT * FROM xobjects_official_in(ARRAY[7, 9]) ORDER BY job, xaid") == xview


def test_the_run_readers_give_the_run_s_rows_of_the_fields_only(connect, tmp_path):
    conn = connect()
    jobs = {}
    for run in (new_run(conn), new_run(conn)):                # two runs founding in fields 7 and 9
        jobs[run] = new_job(conn, run)
        publish(conn, jobs[run], 1, write_attempt(tmp_path / f"job-{jobs[run]}", {
            "objects": [_object(1, 7), _object(3, 9)],
            "memberships": [_member(jobs[run], 1, 7, 1), _member(jobs[run], 3, 9, 3)],
            "xobjects": [_xobject(1, 7), _xobject(4, 9)],
            "xmemberships": [_xmember(jobs[run], 1, 7, 1), _xmember(jobs[run], 4, 9, 2)]}))
    for parent in ("objects", "xobjects"):
        for run, job in jobs.items():
            assert _rows(conn, f"SELECT t::text FROM {parent}_in(ARRAY[7, 9, 8], %s) t ORDER BY 1",
                         (run,)) == _rows(conn, f"SELECT t::text FROM {parent} t WHERE job = %s"
                                                " ORDER BY 1", (job,)), parent
            assert _rows(conn, f"SELECT t::text FROM {parent}_in(ARRAY[9], %s) t ORDER BY 1", (run,)) == _rows(
                conn, f"SELECT t::text FROM {parent} t WHERE job = %s AND field = 9 ORDER BY 1",
                (job,)), parent
        assert _rows(conn, f"SELECT count(*) FROM {parent}_in(ARRAY[7, 9], %s)", (max(jobs) + 1,)) == [(0,)]


def test_a_child_carries_the_parents_indexes(connect, tmp_path):
    conn = connect()
    _publish_two_fields(conn, tmp_path)
    for parent in FIELD_PARENTS:
        want = {d.replace(f"ON public.{parent} ", "ON public.CHILD ").split(" USING ", 1)[1]
                for (d,) in _rows(conn, "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
                                  (parent,))}
        got = {d.split(" USING ", 1)[1]
               for (d,) in _rows(conn, "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
                                 (f"{parent}_7",))}
        assert want and got == want, parent


def test_the_readers_scan_no_parent(connect, tmp_path):
    """The parents' scan counters stand still while every reader runs (a parent read scans it)."""
    conn, observer = connect(), connect()
    run, diff, pkt = _publish_two_fields(conn, tmp_path)

    def scans() -> dict[str, int]:
        _rows(observer, "SELECT pg_stat_clear_snapshot()")
        return dict(_rows(observer, "SELECT relname, coalesce(seq_scan, 0) + coalesce(idx_scan, 0)"
                                    " FROM pg_stat_user_tables WHERE relname = ANY(%s)",
                          (list(FIELD_PARENTS),)))

    before = scans()
    with conn.cursor() as cur:
        for parent in FIELD_PARENTS:
            cur.execute(f"SELECT count(*) FROM {parent}_in(ARRAY[7, 9])")
            assert cur.fetchone()[0] > 0
            cur.execute(f"SELECT count(*) FROM {parent}_of(%s::bigint[])", ([diff, pkt],))
            assert cur.fetchone()[0] > 0
        for parent in ("objects", "xobjects"):
            cur.execute(f"SELECT count(*) FROM {parent}_in(ARRAY[7, 9], %s)", (run,))
            assert cur.fetchone()[0] > 0
        cur.execute("SELECT count(*) FROM objects_official_in(ARRAY[7, 9])")
        cur.execute("SELECT count(*) FROM xobjects_official_in(ARRAY[7, 9])")
        cur.execute("SELECT pg_stat_force_next_flush()")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")                               # an idle backend flushes its counters
    conn.commit()
    assert scans() == before
    # The probe sees a parent read: the same counters move when a parent is queried.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM objects")
        cur.execute("SELECT pg_stat_force_next_flush()")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    conn.commit()
    assert scans()["objects"] > before["objects"]


class _Recording(psycopg2.extensions.cursor):
    """A cursor that records every statement it executes and every COPY it sends, as text."""

    log: list[tuple[str, str]] = []

    def execute(self, query, vars=None):
        text = query.as_string(self) if isinstance(query, sql.Composable) else query
        type(self).log.append(("execute", text.decode() if isinstance(text, bytes) else text))
        return super().execute(query, vars)

    def copy_expert(self, statement, file, size=8192):
        text = statement.as_string(self) if isinstance(statement, sql.Composable) else statement
        type(self).log.append(("copy", text))
        return super().copy_expert(statement, file, size)


def test_the_publisher_loads_the_field_children_by_copy_never_insert(connect, tmp_path):
    """Each of the four tables reaches its children by one COPY per child, named; no INSERT of a
    row of them is sent, to a child or a parent (dev's loaders COPY, pipeline/loadSECatIntoDB*)."""
    conn = connect()
    conn.cursor_factory = _Recording
    _Recording.log = []
    _publish_two_fields(conn, tmp_path)
    copies = sorted(text.split()[1].strip('"') for kind, text in _Recording.log if kind == "copy")
    assert copies == sorted(f"{p}_{f}" for p in FIELD_PARENTS for f in (7, 9))
    inserts = [text for kind, text in _Recording.log if kind == "execute"
               and any(f"INSERT INTO {p}" in text.replace('"', "") for p in FIELD_PARENTS)]
    assert inserts == []


def test_the_readers_need_no_inheritance(connect, tmp_path):
    """With every field child detached from its parent (ALTER TABLE ... NO INHERIT), the readers
    return the rows they returned before: they query the fields' children by name, not the parent."""
    conn = connect()
    _, diff, pkt = _publish_two_fields(conn, tmp_path)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO official (product, key, job, opened_group, opened_by, opened_why)"
                    " VALUES ('sources', '{\"expid\": 1}', %s, 1, 't', 'test')", (diff,))
    conn.commit()
    readers = [*(f"SELECT t::text FROM {p}_in(ARRAY[7, 9]) t ORDER BY 1" for p in FIELD_PARENTS),
               *(f"SELECT t::text FROM {p}_of(ARRAY[{diff}, {pkt}]::bigint[]) t ORDER BY 1"
                 for p in FIELD_PARENTS),
               "SELECT t::text FROM objects_official_in(ARRAY[7, 9]) t ORDER BY 1",
               "SELECT t::text FROM xobjects_official_in(ARRAY[7, 9]) t ORDER BY 1"]
    before = [_rows(conn, q) for q in readers]
    assert all(before)
    children = [(p, f"{p}_{f}") for p in FIELD_PARENTS for f in (7, 9)]
    try:
        with conn.cursor() as cur:
            for parent, child in children:
                cur.execute(f"ALTER TABLE {child} NO INHERIT {parent}")
        conn.commit()
        for parent in FIELD_PARENTS:                          # inheritance is gone: the parents are empty
            assert _rows(conn, f"SELECT count(*) FROM {parent}") == [(0,)]
        assert [_rows(conn, q) for q in readers] == before
    finally:                                                  # inherited again, so the fixture drops them
        with conn.cursor() as cur:
            for parent, child in children:
                cur.execute("SELECT 1 FROM pg_inherits WHERE inhrelid = %s::regclass", (child,))
                if cur.fetchone() is None:
                    cur.execute(f"ALTER TABLE {child} INHERIT {parent}")
        conn.commit()
