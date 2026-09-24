"""Database-backed tests for 20260924-03/-04/-05 and rapidpipe.db.objects.

The run columns on the `merges`, `astroobjects` and `astroobjectsmeta`
prototypes, the per-field table functions (`dev`'s creation, index and
grant blocks, plus the rebuild's set-scoped UNIQUE constraints and
`run`/`result_set` indexes; CLUSTER and ANALYZE), COPY with de-duplication
into the per-field tables, adoption of a `dev` table made before the run
model, `prunedmerges`, the association chain, the done check and the
source-set lookup. Every test runs inside conftest's rolled-back
transaction.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import io

import psycopg2
import pytest

from rapidpipe.db import objects
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo

from .test_repository import _make_run, _make_unit

FIELD = 999999901   # a field no real per-field table has
STAGES = ["crossmatch", "statistics", "prune"]


def _result_set(conn, kind="association-set", *, run_id=None, stage="crossmatch", key=None,
                rows=0):
    """A (new or given) run, an attempt and a complete result set of ``kind``; returns their ids."""
    if run_id is None:
        run_id = _make_run(conn, kind="scratch", selected_stages=STAGES)
    unit_id = f"{stage}-{new_ulid()}"
    _make_unit(conn, run_id, stage=stage, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    instance = new_ulid()
    repo.register_manifest(conn, {
        "run": run_id, "stage": stage, "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{"kind": kind, "format_version": "1", "instance": instance,
                     "key": key if key is not None else {"field": FIELD, "source_sets": [],
                                                         "settings_hash": new_ulid()},
                     "primary": None, "members": [], "registration": {},
                     "row_count": rows}],
    }, registering_attempt_id=attempt_id)
    return run_id, attempt_id, instance


def _csv(*rows):
    return io.StringIO("".join(",".join(str(v) for v in row) + "\n" for row in rows))


def _indexes(cur, table):
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (table,))
    return {r[0] for r in cur.fetchall()}


def _constraints(cur, table):
    cur.execute("SELECT conname, contype FROM pg_constraint WHERE conrelid = %s::regclass",
                (table,))
    return dict(cur.fetchall())


def test_prototypes_carry_the_run_columns_and_their_check(conn):
    with conn.cursor() as cur:
        for table in ("merges", "astroobjects", "astroobjectsmeta"):
            cur.execute("SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name IN ('run', 'attempt', 'result_set')",
                        (table,))
            assert dict(cur.fetchall()) == {"run": "YES", "attempt": "YES", "result_set": "YES"}
            assert _constraints(cur, table)[f"{table}_run_columns_together"] == "c"


def test_create_makes_devs_field_tables_once(conn):
    names = objects.field_table_names(FIELD)
    a, m = names["astroobjects"], names["merges"]
    with conn.cursor() as cur:
        assert objects.ensure_field_object_tables(cur, FIELD) is True
        assert objects.ensure_field_object_tables(cur, FIELD) is False
        cur.execute("SELECT create_field_object_tables(%s)", (FIELD,))
        assert cur.fetchone()[0] is False

        for table in (a, m):
            cur.execute("SELECT relpersistence, pg_get_userbyid(relowner) FROM pg_class "
                        "WHERE relname = %s", (table,))
            assert cur.fetchone() == ("u", "rapidporole")

        assert _indexes(cur, a) == {f"{a}_aid_idx", f"{a}_radec_idx", f"{a}_result_set_idx",
                                    f"{a}_run_idx", f"{a}_set_aid_key"}
        assert _indexes(cur, m) == {f"{m}_aid_idx", f"{m}_sid_idx", f"{m}_result_set_idx",
                                    f"{m}_run_idx", f"{m}_set_aid_sid_key"}

        # INCLUDING CONSTRAINTS copies the CHECK; dev's PRIMARY KEY (aid) is not copied.
        ca, cm = _constraints(cur, a), _constraints(cur, m)
        assert ca["astroobjects_run_columns_together"] == "c"
        assert cm["merges_run_columns_together"] == "c"
        assert ca[f"{a}_set_aid_key"] == "u" and cm[f"{m}_set_aid_sid_key"] == "u"
        assert "p" not in ca.values() and "p" not in cm.values()

        cur.execute("SELECT pg_get_indexdef(%s::regclass)", (f"{a}_radec_idx",))
        assert "q3c_ang2ipix(ra0, dec0)" in cur.fetchone()[0]


def test_create_makes_devs_astroobjectsmeta_table_once(conn):
    t = objects.field_table_names(FIELD)["astroobjectsmeta"]
    with conn.cursor() as cur:
        assert objects.ensure_astroobjectsmeta_table(cur, FIELD) is True
        assert objects.ensure_astroobjectsmeta_table(cur, FIELD) is False
        cur.execute("SELECT relpersistence, pg_get_userbyid(relowner), reloptions FROM pg_class "
                    "WHERE relname = %s", (t,))
        assert cur.fetchone() == ("u", "rapidporole", ["fillfactor=70"])
        assert _indexes(cur, t) == {f"{t}_nsources_idx", f"{t}_meanradec_idx",
                                    f"{t}_result_set_idx", f"{t}_run_idx", f"{t}_set_aid_key"}
        constraints = _constraints(cur, t)
        assert constraints["astroobjectsmeta_run_columns_together"] == "c"
        assert "p" not in constraints.values()


def test_field_is_validated_in_the_database(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("SELECT create_field_object_tables(%s)", (-1,))


def test_copy_dedupes_within_and_across_copies_and_scopes_keys_by_set(conn):
    run_id, attempt_id, first = _result_set(conn)
    _, attempt2, second = _result_set(conn, run_id=run_id)
    with conn.cursor() as cur:
        objects.ensure_field_object_tables(cur, FIELD)
        a = objects.field_table_names(FIELD)["astroobjects"]
        m = objects.field_table_names(FIELD)["merges"]
        obj = (run_id, attempt_id, first)
        rows = [(10, 269.45, -28.77, 12.5, *obj), (11, 269.46, -28.76, 7.0, *obj),
                (10, 269.45, -28.77, 12.5, *obj)]
        assert objects.copy_astroobjects(cur, FIELD, _csv(*rows)) == 2
        assert objects.copy_astroobjects(cur, FIELD, _csv(*rows)) == 0
        # The same aid under another set is another row (keys are set-scoped).
        assert objects.copy_astroobjects(
            cur, FIELD, _csv((10, 269.45, -28.77, 12.5, run_id, attempt2, second))) == 1
        assert objects.count_result_set_rows(cur, a, first) == 2
        assert objects.count_result_set_rows(cur, a, second) == 1

        merges = [(10, 1, *obj), (10, 2, *obj), (10, 1, *obj)]
        assert objects.copy_merges(cur, FIELD, _csv(*merges)) == 2
        assert objects.copy_merges(cur, FIELD, _csv(*merges)) == 0
        assert objects.count_result_set_rows(cur, m, first) == 2
        cur.execute(f"SELECT aid, sid, run, attempt, result_set FROM {m} ORDER BY sid")
        assert cur.fetchall() == [(10, 1, *obj), (10, 2, *obj)]


def test_copy_refuses_half_attached_rows(conn):
    run_id, attempt_id, instance = _result_set(conn)
    with conn.cursor() as cur:
        objects.ensure_field_object_tables(cur, FIELD)
        with pytest.raises(psycopg2.errors.CheckViolation):
            objects.copy_merges(cur, FIELD, _csv((10, 1, run_id, attempt_id, "\\N")))


def test_copy_astroobjectsmeta(conn):
    run_id, attempt_id, instance = _result_set(conn, "statistics-set", stage="statistics",
                                               key={"membership": new_ulid()})
    with conn.cursor() as cur:
        objects.ensure_astroobjectsmeta_table(cur, FIELD)
        row = (10, 269.45, 0.0001, -28.77, 0.0002, 12.5, 0.5, 3, run_id, attempt_id, instance)
        assert objects.copy_astroobjectsmeta(cur, FIELD, _csv(row, row)) == 1
        t = objects.field_table_names(FIELD)["astroobjectsmeta"]
        cur.execute(f"SELECT {', '.join(objects.ASTROOBJECTSMETA_COLUMNS)} FROM {t}")
        got = cur.fetchone()
        assert got[0] == 10 and got[7] == 3 and got[8:] == (run_id, attempt_id, instance)
        assert got[1] == pytest.approx(269.45) and got[2] == pytest.approx(0.0001)


def test_cluster_field_object_tables(conn):
    run_id, attempt_id, instance = _result_set(conn)
    with conn.cursor() as cur:
        objects.ensure_field_object_tables(cur, FIELD)
        objects.copy_astroobjects(
            cur, FIELD, _csv((10, 269.45, -28.77, 12.5, run_id, attempt_id, instance)))
        objects.cluster_field_object_tables(cur, FIELD)
        a = objects.field_table_names(FIELD)["astroobjects"]
        cur.execute("SELECT indisclustered FROM pg_index WHERE indexrelid = %s::regclass",
                    (f"{a}_radec_idx",))
        assert cur.fetchone()[0] is True


def test_cluster_refuses_a_missing_field(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            objects.cluster_field_object_tables(cur, FIELD + 1)


def test_prunedmerges_records_excluded_pairs_once(conn):
    run_id, attempt_id, base = _result_set(conn)
    _, prune_attempt, pruned = _result_set(conn, "pruned-set", run_id=run_id, stage="prune",
                                           key={"base": base, "settings_hash": "h"})
    with conn.cursor() as cur:
        pairs = [(10, 1), (10, 2), (10, 1)]
        assert objects.insert_pruned_merges(cur, pairs, pruned, base, run_id, prune_attempt) == 2
        assert objects.insert_pruned_merges(cur, pairs, pruned, base, run_id, prune_attempt) == 0
        assert objects.insert_pruned_merges(cur, [], pruned, base, run_id, prune_attempt) == 0
        assert objects.count_result_set_rows(cur, "prunedmerges", pruned) == 2
        cur.execute("SELECT DISTINCT base_set, run, attempt FROM prunedmerges "
                    "WHERE result_set = %s", (pruned,))
        assert cur.fetchall() == [(base, run_id, prune_attempt)]


def test_association_chain_and_set_rows_read_base_plus_delta(conn):
    run_id, a0, first = _result_set(conn, key={"field": FIELD, "base": None, "settings_hash": "h"})
    _, a1, second = _result_set(conn, run_id=run_id,
                                key={"field": FIELD, "base": first, "settings_hash": "h"})
    other_run, a2, unrelated = _result_set(conn)
    with conn.cursor() as cur:
        assert objects.association_chain(cur, second) == [second, first]
        assert objects.association_chain(cur, first) == [first]
        objects.ensure_field_object_tables(cur, FIELD)
        objects.copy_astroobjects(cur, FIELD, _csv(
            (1, 1.0, 1.0, 1.0, "\\N", "\\N", "\\N"),
            (2, 1.0, 1.0, 1.0, run_id, a0, first),
            (3, 1.0, 1.0, 1.0, run_id, a1, second),
            (4, 1.0, 1.0, 1.0, other_run, a2, unrelated)))
        a = objects.field_table_names(FIELD)["astroobjects"]
        sql, params = objects.set_rows_clause("o", objects.association_chain(cur, second))
        cur.execute(f"SELECT aid FROM {a} o WHERE {sql} ORDER BY aid", params)
        assert [r[0] for r in cur.fetchall()] == [2, 3]
        cur.execute("UPDATE product_instances SET deletion_state = 'deleted' WHERE id = %s",
                    (first,))
        with pytest.raises(ValueError, match="not retained"):
            objects.association_chain(cur, second)


def test_find_complete_result_set(conn):
    key = {"field": FIELD, "source_sets": ["S1"], "settings_hash": "h"}
    run_id, _, instance = _result_set(conn, key=key, rows=5)
    with conn.cursor() as cur:
        assert objects.find_complete_result_set(cur, "association-set", run_id, key) == (instance, 5)
        assert objects.find_complete_result_set(cur, "pruned-set", run_id, key) is None
        assert objects.find_complete_result_set(
            cur, "association-set", run_id, {**key, "settings_hash": "other"}) is None


def test_current_association_sets(conn):
    _, _, scratch = _result_set(conn)
    _, _, current = _result_set(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'current' WHERE id = %s", (current,))
        assert objects.current_association_sets(cur, FIELD) == [current]
        assert objects.current_association_sets(cur, FIELD + 1) == []


def test_a_dev_table_without_run_columns_is_adopted_in_place(conn):
    names = objects.field_table_names(FIELD)
    a, m, t = names["astroobjects"], names["merges"], names["astroobjectsmeta"]
    run_id, attempt_id, instance = _result_set(conn)
    with conn.cursor() as cur:
        # dev's per-field tables, made before 20260924-03: dev's columns only.
        cur.execute(f"CREATE TABLE {a} (aid bigint NOT NULL, ra0 double precision NOT NULL, "
                    f"dec0 double precision NOT NULL, flux0 real NOT NULL)")
        cur.execute(f"CREATE INDEX {a}_radec_idx ON {a} (q3c_ang2ipix(ra0, dec0))")
        cur.execute(f"CREATE TABLE {m} (aid bigint NOT NULL, sid bigint NOT NULL)")
        cur.execute(f"CREATE TABLE {t} (aid bigint NOT NULL, meanra double precision NOT NULL, "
                    f"stdevra real NOT NULL, meandec double precision NOT NULL, "
                    f"stdevdec real NOT NULL, meanflux real NOT NULL, stdevflux real NOT NULL, "
                    f"nsources smallint NOT NULL)")
        for table in (a, m, t):
            cur.execute(f"ALTER TABLE {table} OWNER TO rapidporole")
        cur.execute(f"INSERT INTO {a} VALUES (1, 1.0, 1.0, 1.0)")

        assert objects.ensure_field_object_tables(cur, FIELD) is False
        assert objects.ensure_astroobjectsmeta_table(cur, FIELD) is False
        for table, prefix, key in ((a, "astroobjects", "_set_aid_key"),
                                   (m, "merges", "_set_aid_sid_key"),
                                   (t, "astroobjectsmeta", "_set_aid_key")):
            constraints = _constraints(cur, table)
            assert constraints[f"{prefix}_run_columns_together"] == "c"
            assert constraints[f"{table}{key}"] == "u"
            assert {f"{table}_run_idx", f"{table}_result_set_idx"} <= _indexes(cur, table)

        # Adoption is idempotent, and the function path agrees.
        cur.execute("SELECT create_field_object_tables(%s)", (FIELD,))
        assert cur.fetchone()[0] is False
        assert objects.copy_astroobjects(
            cur, FIELD, _csv((1, 1.0, 1.0, 1.0, run_id, attempt_id, instance),
                             (1, 1.0, 1.0, 1.0, run_id, attempt_id, instance))) == 1
        cur.execute(f"SELECT aid, run FROM {a} ORDER BY run NULLS FIRST")
        assert cur.fetchall() == [(1, None), (1, run_id)]
        with pytest.raises(psycopg2.errors.CheckViolation):
            objects.copy_merges(cur, FIELD, _csv((1, 5, run_id, attempt_id, "\\N")))


def test_source_set_table_refuses_what_is_not_a_complete_source_set(conn):
    _, _, association = _result_set(conn)
    with conn.cursor() as cur:
        with pytest.raises(ValueError, match="no source-set"):
            objects.source_set_table(cur, association)
        with pytest.raises(ValueError, match="no source-set"):
            objects.source_set_table(cur, new_ulid())
    run_id = _make_run(conn, kind="scratch", selected_stages=["load"])
    _, _, source_set = _result_set(conn, "source-set", run_id=run_id, stage="load",
                                   key={"difference": new_ulid(), "catalog_type": "photutils"})
    with conn.cursor() as cur:
        with pytest.raises(ValueError, match="no diffimages"):
            objects.source_set_table(cur, source_set)
        cur.execute("UPDATE result_sets SET complete = false WHERE instance = %s", (source_set,))
        with pytest.raises(ValueError, match="not complete"):
            objects.source_set_table(cur, source_set)


def test_source_set_table_follows_a_loaded_set_to_its_child_table(conn, tmp_path, monkeypatch):
    from rapidpipe.products.manifest import Manifest
    from rapidpipe.stages.contract import ExitCode

    from .test_load import _registered_difference, _run_load

    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, _, outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(outputs / "manifest.json").outputs
    with conn.cursor() as cur:
        assert objects.source_set_table(cur, entry.instance) == (
            entry.registration["table"], entry.registration["row_count"])
