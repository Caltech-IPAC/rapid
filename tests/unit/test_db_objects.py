"""Tests for rapidpipe.db.objects that need no database: names, SQL shape, COPY arguments.

The functions against a real PostgreSQL (the 20260924-04 per-field table
functions, COPY with de-duplication, `prunedmerges`, the visibility rule)
are in tests/db/test_objects_child_tables.py.
"""

from __future__ import annotations

import datetime
import io
import json

import pytest

from rapidpipe.db import objects


class FakeCursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.executed: list[tuple[str, tuple]] = []
        self.copied: list[dict] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def copy_from(self, file, table, sep, null, columns):
        self.copied.append({"text": file.read(), "table": table, "sep": sep, "null": null,
                            "columns": columns})


def test_columns_are_devs_then_the_run_columns():
    assert objects.ASTROOBJECTS_COLUMNS == ("aid", "ra0", "dec0", "flux0",
                                            "run", "attempt", "result_set")
    assert objects.MERGES_COLUMNS == ("aid", "sid", "run", "attempt", "result_set")
    assert objects.ASTROOBJECTSMETA_COLUMNS == (
        "aid", "meanra", "stdevra", "meandec", "stdevdec", "meanflux", "stdevflux", "nsources",
        "run", "attempt", "result_set")
    assert objects.RUN_COLUMNS == ("run", "attempt", "result_set")


def test_field_table_names():
    assert objects.field_table_names(5321) == {
        "astroobjects": "astroobjects_5321", "merges": "merges_5321",
        "astroobjectsmeta": "astroobjectsmeta_5321"}
    assert objects.field_table_names("0")["merges"] == "merges_0"


@pytest.mark.parametrize("field", [-1, "12a", "-3", " 7", None, True, 1.5, "1; DROP TABLE x"])
def test_field_table_names_refuses_anything_but_a_non_negative_integer(field):
    with pytest.raises(ValueError):
        objects.field_table_names(field)


def test_ensure_field_object_tables_skips_the_function_when_both_exist():
    cur = FakeCursor([(True,), (True,)])
    assert objects.ensure_field_object_tables(cur, 7) is False
    assert [p for _, p in cur.executed] == [("public.astroobjects_7",), ("public.merges_7",)]


def test_ensure_field_object_tables_calls_the_function_when_one_is_absent():
    cur = FakeCursor([(True,), (False,), (True,)])
    assert objects.ensure_field_object_tables(cur, 7) is True
    assert cur.executed[-1] == ("SELECT create_field_object_tables(%s)", (7,))


def test_ensure_astroobjectsmeta_table():
    cur = FakeCursor([(True,)])
    assert objects.ensure_astroobjectsmeta_table(cur, 7) is False
    ((sql, params),) = cur.executed
    assert "attname = 'result_set'" in sql and params == ("public.astroobjectsmeta_7",)
    cur = FakeCursor([(False,), (True,)])
    assert objects.ensure_astroobjectsmeta_table(cur, 7) is True
    assert cur.executed[-1] == ("SELECT create_astroobjectsmeta_child_table(%s)", (7,))


def test_cluster_calls_the_function():
    cur = FakeCursor()
    objects.cluster_field_object_tables(cur, 7)
    assert cur.executed == [("SELECT cluster_field_object_tables(%s)", (7,))]


@pytest.mark.parametrize("copy, table, columns", [
    (objects.copy_astroobjects, "astroobjects_7", objects.ASTROOBJECTS_COLUMNS),
    (objects.copy_merges, "merges_7", objects.MERGES_COLUMNS),
    (objects.copy_astroobjectsmeta, "astroobjectsmeta_7", objects.ASTROOBJECTSMETA_COLUMNS),
])
def test_copy_goes_through_a_temp_table_and_on_conflict(copy, table, columns):
    cur = FakeCursor(rowcount=3)
    assert copy(cur, 7, io.StringIO("1,2\n")) == 3
    (copied,) = cur.copied
    temp = f"copy_{table}"
    assert copied["table"] == temp
    assert (copied["sep"], copied["null"]) == (",", "\\N")
    assert copied["columns"] == columns
    sqls = [s for s, _ in cur.executed]
    assert sqls == [
        f"DROP TABLE IF EXISTS pg_temp.{temp}",
        f"CREATE TEMP TABLE {temp} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP",
        f"INSERT INTO {table} SELECT * FROM {temp} ON CONFLICT DO NOTHING",
        f"DROP TABLE {temp}",
    ]


def test_count_result_set_rows_accepts_only_object_tables():
    cur = FakeCursor([(4,)])
    assert objects.count_result_set_rows(cur, "merges_7", "SET") == 4
    assert cur.executed == [("SELECT count(*) FROM merges_7 WHERE result_set = %s", ("SET",))]
    assert objects.count_result_set_rows(FakeCursor([(1,)]), "prunedmerges", "SET") == 1
    for bad in ("sources_20260821_7", "merges", "merges_7; DROP TABLE x"):
        with pytest.raises(ValueError):
            objects.count_result_set_rows(FakeCursor([(0,)]), bad, "SET")


def _readable(kind="source-set", run="RUN", custody="scratch", deletion_state="retained",
              complete=True, row_count=12, key=None, selected=True):
    """One row of ``assert_readable_result_set``'s query."""
    return (kind, run, custody, deletion_state, complete, row_count,
            json.dumps(key if key is not None else {}), selected)


def test_readable_own_run_set_whatever_its_custody_or_attempt():
    state = objects.assert_readable_result_set(
        FakeCursor([_readable(custody="scratch", selected=False, key={"difference": "D"})]),
        "SS", "RUN")
    assert state == {"kind": "source-set", "run": "RUN", "custody": "scratch", "row_count": 12,
                     "key": {"difference": "D"}}


@pytest.mark.parametrize("custody", ["candidate", "current"])
def test_readable_another_runs_selected_production_set(custody):
    cur = FakeCursor([_readable(kind="association-set", run="OTHER", custody=custody)])
    assert objects.assert_readable_result_set(cur, "A", "RUN", kind="association-set")["run"] == "OTHER"
    sql, params = cur.executed[0]
    assert "u.selected_attempt = pi.producing_attempt" in sql and params == ("A",)


@pytest.mark.parametrize("row, kind, match", [
    (None, None, "no result set"),
    (_readable(kind="association-set"), "source-set", "not a source-set"),
    (_readable(complete=False), None, "not complete and retained"),
    (_readable(complete=None), None, "not complete and retained"),
    (_readable(deletion_state="deleted"), None, "not complete and retained"),
    (_readable(run="OTHER", custody="scratch"), None, "scratch result set is not readable"),
    (_readable(run="OTHER", custody="candidate", selected=False), None, "not its unit's selected"),
    (_readable(run="OTHER", custody="current", selected=False), None, "not its unit's selected"),
])
def test_readable_refuses(row, kind, match):
    with pytest.raises(ValueError, match=match):
        objects.assert_readable_result_set(FakeCursor([row] if row else []), "X", "RUN",
                                           kind=kind)


def test_source_set_table_follows_key_to_diffimages_and_l2files():
    cur = FakeCursor([_readable(key={"difference": "DIFF"}),
                      (datetime.datetime(2026, 8, 21, 3, 4, 5), 7)])
    assert objects.source_set_table(cur, "SS", "RUN") == ("sources_20260821_7", 12)
    assert cur.executed[0][1] == ("SS",)
    assert cur.executed[1][1] == ("DIFF",)


@pytest.mark.parametrize("rows, match", [
    ([], "no result set"),
    ([_readable(kind="association-set")], "not a source-set"),
    ([_readable(complete=False)], "not complete"),
    ([_readable(deletion_state="deleted")], "not complete"),
    ([_readable(run="OTHER", custody="scratch", key={"difference": "D"})], "scratch"),
    ([_readable(run="OTHER", custody="candidate", selected=False, key={"difference": "D"})],
     "selected"),
    ([_readable(key={})], "names no difference"),
    ([_readable(key={"difference": "DIFF"})], "no diffimages"),
])
def test_source_set_table_refuses(rows, match):
    with pytest.raises(ValueError, match=match):
        objects.source_set_table(FakeCursor(rows), "SS", "RUN")


def _assoc(base, **kw):
    return _readable(kind="association-set", key={"field": 1, "base": base}, **kw)


def test_association_chain_follows_base_until_null():
    cur = FakeCursor([_assoc("B"), _assoc("C", run="OTHER", custody="current"), _assoc(None)])
    assert objects.association_chain(cur, "A", "RUN") == ["A", "B", "C"]
    assert [p for _, p in cur.executed] == [("A",), ("B",), ("C",)]


@pytest.mark.parametrize("rows, match", [
    ([], "no result set"),
    ([_assoc("B")], "no result set with instance 'B'"),
    ([_readable(kind="source-set")], "not a association-set"),
    ([_assoc(None, deletion_state="deleted")], "not complete and retained"),
    ([_assoc("B"), _assoc("A")], "loops"),
    ([_assoc("B"), _assoc(None, run="OTHER", custody="scratch")], "chain of 'A'.*scratch"),
    ([_assoc(None, run="OTHER", custody="candidate", selected=False)], "selected"),
])
def test_association_chain_refuses(rows, match):
    with pytest.raises(ValueError, match=match):
        objects.association_chain(FakeCursor(rows), "A", "RUN")


def test_set_rows_clause():
    assert objects.set_rows_clause("o", ("A", "B")) == ("o.result_set = ANY(%s)", (["A", "B"],))
    for bad in ("", "o.x", "o; DROP", "1a"):
        with pytest.raises(ValueError):
            objects.set_rows_clause(bad, ["A"])


def test_find_complete_result_set_matches_kind_run_key_and_reusable_attempt():
    cur = FakeCursor([("SET", 7)])
    key = {"field": 5321, "source_sets": ["S"], "settings_hash": "h"}
    assert objects.find_complete_result_set(cur, "association-set", "RUN", key, "ATT") == ("SET", 7)
    sql, params = cur.executed[0]
    assert "pi.kind = %s" in sql and "rs.complete" in sql and "deletion_state = 'retained'" in sql
    assert "JOIN attempts a ON a.id = pi.producing_attempt" in sql
    assert "(a.id = %s OR a.disposition = 'succeeded')" in sql
    assert params == ("association-set", "RUN", json.dumps(key), "ATT")
    assert objects.find_complete_result_set(FakeCursor([]), "pruned-set", "RUN", key, "ATT") is None


def test_current_association_sets():
    cur = FakeCursor([("A",), ("B",)])
    assert objects.current_association_sets(cur, 5321) == ["A", "B"]
    sql, params = cur.executed[0]
    assert "kind = 'association-set'" in sql and "custody = 'current'" in sql
    assert params == ("5321",)


def test_insert_pruned_merges_is_one_statement():
    cur = FakeCursor(rowcount=2)
    assert objects.insert_pruned_merges(cur, [(10, 1), (11, 2)], "P", "B", "R", "T") == 2
    (sql, params), = cur.executed
    assert sql.startswith("INSERT INTO prunedmerges") and "ON CONFLICT DO NOTHING" in sql
    assert params == ("P", "B", "R", "T", [10, 11], [1, 2])
    empty = FakeCursor()
    assert objects.insert_pruned_merges(empty, [], "P", "B", "R", "T") == 0
    assert empty.executed == []
