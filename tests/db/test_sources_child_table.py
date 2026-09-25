"""Database-backed tests for 20260923-04/-05/-09 and rapidpipe.db.sources.

The `sources` run columns and their together-check, the child-table
functions (`dev`'s creation, index and grant blocks, plus 20260923-09's
`result_set`/`run` indexes; CLUSTER and ANALYZE), COPY into a child with
`dev`'s separator and null string, and the done check. Every test runs
inside conftest's rolled-back transaction.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import io

import psycopg2
import pytest

from rapidpipe.db import sources
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo

from .test_repository import _make_run, _make_unit

OBS_DATE = "20991231"   # a date no real child table has
SCA = 17


def _source_set(conn, rows=0):
    """A run, a load attempt and a complete source-set instance; returns their ids."""
    run_id = _make_run(conn, selected_stages=["load"])
    _make_unit(conn, run_id, stage="load", unit_id="e1/SCA17")
    attempt_id = repo.allocate_attempt(conn, run_id, "load", "e1/SCA17")
    instance = new_ulid()
    key = {"difference": new_ulid(), "catalog_type": "photutils"}
    repo.register_manifest(conn, {
        "run": run_id, "stage": "load", "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{"kind": "source-set", "format_version": "1", "instance": instance,
                     "key": key, "primary": None, "members": [], "registration": {},
                     "row_count": rows}],
    }, registering_attempt_id=attempt_id)
    return run_id, attempt_id, instance, key


def _csv_row(run_id, attempt_id, result_set, *, source_id=1, isdiffpos="true"):
    values = [source_id, 269.45, -28.77, 10.5, 11.25, 123.4, 0.01, 0.02, 1.5,
              25, 0.1, 0.01, 1.2, 0, 0.5, 0.01, 0.02, 25, 9.5,
              7, isdiffpos, 5321, 1234, 56789, 1001, 3, SCA, 60000.5,
              run_id, attempt_id, result_set]
    return ",".join(str(v) for v in values) + "\n"


def test_create_makes_devs_child_table_once(conn):
    with conn.cursor() as cur:
        assert sources.ensure_child_table(cur, OBS_DATE, SCA) is True
        assert sources.ensure_child_table(cur, OBS_DATE, SCA) is False
        cur.execute("SELECT create_sources_child_table(%s, %s)", (OBS_DATE, SCA))
        assert cur.fetchone()[0] is False

        table = sources.child_table_name(OBS_DATE, SCA)
        cur.execute(
            "SELECT c.relpersistence, pg_get_userbyid(c.relowner), p.relname "
            "FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE c.relname = %s", (table,))
        assert cur.fetchone() == ("u", "rapidporole", "sources")

        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (table,))
        indexes = {r[0] for r in cur.fetchall()}
        assert indexes == {f"{table}_{c}_idx" for c in
                           ("pid", "expid", "sca", "field", "flags", "mjdobs", "sid", "radec",
                            "result_set", "run")}

        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,))
        assert {"run", "attempt", "result_set"} <= {r[0] for r in cur.fetchall()}


def test_child_table_name_is_validated_in_the_database(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("SELECT create_sources_child_table(%s, %s)", ("2099-12-31", SCA))


def test_copy_loads_rows_with_their_run_columns(conn):
    run_id, attempt_id, instance, key = _source_set(conn, rows=2)
    with conn.cursor() as cur:
        sources.ensure_child_table(cur, OBS_DATE, SCA)
        table = sources.child_table_name(OBS_DATE, SCA)
        text = (_csv_row(run_id, attempt_id, instance, source_id=1)
                + _csv_row(run_id, attempt_id, instance, source_id=1, isdiffpos="false"))
        sources.copy_sources(cur, table, io.StringIO(text))
        assert sources.count_result_set_rows(cur, table, instance) == 2
        cur.execute(
            f"SELECT id, isdiffpos, pid, field, hp6, hp9, mjdobs, run, attempt, result_set, rb "
            f"FROM {table} ORDER BY isdiffpos DESC")
        rows = cur.fetchall()
        assert rows[0] == (1, True, 7, 5321, 1234, 56789, 60000.5, run_id, attempt_id, instance, None)
        assert rows[1][1] is False
        # The parent sees the child's rows (dev's reason for inheritance).
        cur.execute("SELECT count(*) FROM sources WHERE result_set = %s", (instance,))
        assert cur.fetchone()[0] == 2
        assert sources.find_complete_source_set(cur, run_id, key, attempt_id) == (instance, 2)


def test_run_columns_are_all_or_nothing_on_a_child(conn):
    run_id, attempt_id, instance, _ = _source_set(conn)
    with conn.cursor() as cur:
        sources.ensure_child_table(cur, OBS_DATE, SCA)
        table = sources.child_table_name(OBS_DATE, SCA)
        half = _csv_row(run_id, attempt_id, instance).rsplit(",", 1)[0] + ",\\N\n"
        with pytest.raises(psycopg2.errors.CheckViolation):
            sources.copy_sources(cur, table, io.StringIO(half))


def test_cluster_and_analyze(conn):
    run_id, attempt_id, instance, _ = _source_set(conn, rows=1)
    with conn.cursor() as cur:
        sources.ensure_child_table(cur, OBS_DATE, SCA)
        sources.copy_sources(cur, sources.child_table_name(OBS_DATE, SCA),
                             io.StringIO(_csv_row(run_id, attempt_id, instance)))
        sources.cluster_and_analyze(cur, OBS_DATE, SCA)
        cur.execute("SELECT indisclustered FROM pg_index WHERE indexrelid = %s::regclass",
                    (f"{sources.child_table_name(OBS_DATE, SCA)}_radec_idx",))
        assert cur.fetchone()[0] is True
