"""Tests for rapidpipe.db.sources that need no database: names, SQL shape, COPY arguments.

The functions against a real PostgreSQL (the 20260923-05 child-table
functions, COPY into a child, the done check) are in
tests/db/test_sources_child_table.py.
"""

from __future__ import annotations

import datetime
import io
import json

import pytest

from rapidpipe.db import sources


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple]] = []
        self.copied: list[dict] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def copy_from(self, file, table, sep, null, columns):
        self.copied.append({"text": file.read(), "table": table, "sep": sep, "null": null,
                            "columns": columns})


def test_columns_are_devs_28_then_the_run_columns():
    assert len(sources.DEV_COLUMNS) == 28
    assert sources.DEV_COLUMNS[:3] == ("id", "ra", "dec")
    assert sources.DEV_COLUMNS[-5:] == ("hp9", "expid", "fid", "sca", "mjdobs")
    assert sources.COLUMNS == sources.DEV_COLUMNS + ("run", "attempt", "result_set")


@pytest.mark.parametrize("dateobs, expected", [
    (datetime.datetime(2026, 8, 21, 3, 4, 5), "20260821"),
    ("2026-08-21 03:04:05.123", "20260821"),
    ("2026-08-21T03:04:05", None),   # dev's split on whitespace: no date alone
])
def test_obs_date_of_is_devs_derivation(dateobs, expected):
    if expected is None:
        with pytest.raises(ValueError):
            sources.obs_date_of(dateobs)
    else:
        assert sources.obs_date_of(dateobs) == expected


def test_child_table_name():
    assert sources.child_table_name("20260821", 7) == "sources_20260821_7"
    with pytest.raises(ValueError):
        sources.child_table_name("2026-08-21", 7)


def test_difference_image_row_reads_diffimages_then_l2files():
    cur = FakeCursor([(42, 9), (1001, 7, 3, 5321, 11, 222, 60000.5, "2026-08-21 03:04:05")])
    row = sources.difference_image_row(cur, "INSTANCE")
    assert row == {"pid": 42, "rid": 9, "expid": 1001, "sca": 7, "fid": 3, "field": 5321,
                   "hp6": 11, "hp9": 222, "mjdobs": 60000.5, "dateobs": "2026-08-21 03:04:05"}
    assert cur.executed[0] == ("SELECT pid, rid FROM diffimages WHERE instance = %s", ("INSTANCE",))
    assert cur.executed[1][1] == (9,)


def test_difference_image_row_refuses_an_unregistered_instance():
    with pytest.raises(ValueError, match="no diffimages row"):
        sources.difference_image_row(FakeCursor([]), "NOPE")
    with pytest.raises(ValueError, match="no l2files row"):
        sources.difference_image_row(FakeCursor([(42, 9)]), "X")


def test_ensure_child_table_skips_the_function_when_the_table_exists():
    cur = FakeCursor([(True,)])
    assert sources.ensure_child_table(cur, "20260821", 7) is False
    assert len(cur.executed) == 1
    assert cur.executed[0][1] == ("public.sources_20260821_7",)


def test_ensure_child_table_calls_the_function_when_absent():
    cur = FakeCursor([(False,), (True,)])
    assert sources.ensure_child_table(cur, "20260821", 7) is True
    assert cur.executed[1] == ("SELECT create_sources_child_table(%s, %s)", ("20260821", 7))


def test_copy_sources_uses_devs_separator_and_null():
    cur = FakeCursor()
    sources.copy_sources(cur, "sources_20260821_7", io.StringIO("1,2\n"))
    (copied,) = cur.copied
    assert copied["table"] == "sources_20260821_7"
    assert (copied["sep"], copied["null"]) == (",", "\\N")
    assert copied["columns"] == sources.COLUMNS


def test_copy_sources_refuses_any_other_table():
    with pytest.raises(ValueError):
        sources.copy_sources(FakeCursor(), "diffimages", io.StringIO(""))


def test_find_complete_source_set_matches_kind_run_key_and_reusable_attempt():
    cur = FakeCursor([("01J8Y6QZ3M0000000000000000", 12)])
    key = {"difference": "D", "catalog_type": "photutils"}
    assert sources.find_complete_source_set(cur, "RUN", key, "ATT") == (
        "01J8Y6QZ3M0000000000000000", 12)
    sql, params = cur.executed[0]
    assert "pi.kind = 'source-set'" in sql and "rs.complete" in sql
    assert "(a.id = %s OR a.disposition = 'succeeded')" in sql
    assert params == ("RUN", json.dumps(key), "ATT")
    assert sources.find_complete_source_set(FakeCursor([]), "RUN", key, "ATT") is None


def test_cluster_and_analyze_calls_the_function():
    cur = FakeCursor()
    sources.cluster_and_analyze(cur, "20260821", 7)
    assert cur.executed == [("SELECT cluster_sources_child_table(%s, %s)", ("20260821", 7))]
