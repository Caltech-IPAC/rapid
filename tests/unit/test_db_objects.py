"""Tests for rapidpipe.db.objects that need no database: names, SQL shape, COPY arguments.

The functions against a real PostgreSQL (the 20260924-03 per-field table
functions, COPY with de-duplication, `prunedmerges`, the visibility rule)
are in tests/db/test_objects_child_tables.py.
"""

from __future__ import annotations

import datetime
import io

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
    assert cur.executed == [("SELECT to_regclass(%s) IS NOT NULL", ("public.astroobjectsmeta_7",))]
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


def test_source_set_table_follows_key_to_diffimages_and_l2files():
    cur = FakeCursor([("DIFF", True, "retained", 12),
                      (datetime.datetime(2026, 8, 21, 3, 4, 5), 7)])
    assert objects.source_set_table(cur, "SS") == ("sources_20260821_7", 12)
    assert "pi.kind = 'source-set'" in cur.executed[0][0]
    assert cur.executed[0][1] == ("SS",)
    assert cur.executed[1][1] == ("DIFF",)


@pytest.mark.parametrize("rows, match", [
    ([], "no source-set"),
    ([("DIFF", False, "retained", 0)], "not complete"),
    ([("DIFF", True, "deleted", 0)], "not complete"),
    ([(None, True, "retained", 0)], "names no difference"),
    ([("DIFF", True, "retained", 0)], "no diffimages"),
])
def test_source_set_table_refuses(rows, match):
    with pytest.raises(ValueError, match=match):
        objects.source_set_table(FakeCursor(rows), "SS")


def test_catalog_visibility_sql():
    sql, params = objects.catalog_visibility_sql("o", "RUN")
    assert sql == (
        "(o.run IS NULL OR o.run = %s OR o.result_set IN "
        "(SELECT id FROM product_instances WHERE kind = 'association-set' "
        "AND custody = 'current' AND deletion_state = 'retained'))")
    assert params == ("RUN",)
    assert objects.catalog_visibility_sql("a")[1] == ()
    assert sql.count("%s") == 1
    for bad in ("", "o.x", "o; DROP", "1a"):
        with pytest.raises(ValueError):
            objects.catalog_visibility_sql(bad)


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
