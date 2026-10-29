"""
Tests of the RUNPROGRAMIDS selection: parse_program_ids and get_expids_for_obs_datetime_range in
modules/utils/rapid_pipeline_subs.py, and the SQL that the RAPIDDB query methods in
database/modules/utils/rapid_db.py build with and without program_ids (checked with a fake
cursor, so no database is needed).

Run from the repository root:  python -m pytest modules/utils/tests
"""

import re
import sys
import types

import pytest

try:
    import psycopg2  # noqa: F401
except ImportError:
    stub = types.ModuleType("psycopg2")
    stub.DatabaseError = Exception
    stub.ProgrammingError = Exception
    sys.modules["psycopg2"] = stub

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util


SUBQUERY = "in (select expid from Exposures where program_id in (-2,994)) "


class FakeCursor:

    def __init__(self):
        self.queries = []

    def execute(self, query):
        self.queries.append(query)

    def __iter__(self):
        return iter([])

    def fetchone(self):
        return (1,)


class FakeConn:

    def commit(self):
        pass

    def rollback(self):
        pass


def _fake_dbh():
    dbh = object.__new__(db.RAPIDDB)
    dbh.cur = FakeCursor()
    dbh.conn = FakeConn()
    dbh.exit_code = 0
    return dbh


def test_parse_program_ids_unset():
    assert util.parse_program_ids(None) is None
    assert util.parse_program_ids("") is None
    assert util.parse_program_ids("  ") is None


def test_parse_program_ids_values():
    assert util.parse_program_ids("-2") == [-2]
    assert util.parse_program_ids(" 994, 1094 ,994") == [994, 1094]


@pytest.mark.parametrize("bad", ["abc", "1,,2", "1.5", "1,x"])
def test_parse_program_ids_bad(bad):
    with pytest.raises(ValueError):
        util.parse_program_ids(bad)


def test_get_run_program_ids_bad_exits(monkeypatch):
    monkeypatch.setenv("RUNPROGRAMIDS", "abc")
    with pytest.raises(SystemExit) as e:
        util.get_run_program_ids()
    assert e.value.code == 64


def test_program_ids_clause():
    assert db.RAPIDDB.program_ids_clause(None) == ""
    assert db.RAPIDDB.program_ids_clause([-2, 994]) == "and expid " + SUBQUERY


@pytest.mark.parametrize("call", [
    lambda dbh, p: dbh.get_field_fid_nframes_records(5, None, program_ids=p),
    lambda dbh, p: dbh.get_field_fid_nframes_records(5, 3, program_ids=p),
    lambda dbh, p: dbh.get_field_fid_nframes_records_for_mjdobs_range(0.0, 99999.0, 5, None, program_ids=p),
    lambda dbh, p: dbh.get_field_fid_nframes_records_for_mjdobs_range(0.0, 99999.0, 5, 3, program_ids=p),
    lambda dbh, p: dbh.get_l2files_records_for_datetime_range_field_fid("2027-01-01", "2027-01-02", 7, 3,
                                                                       program_ids=p),
    lambda dbh, p: dbh.get_overlapping_l2files("null", 3, [7, 8], 10.0, 20.0, 99999.0, program_ids=p),
])
def test_queries_with_and_without_program_ids(call, monkeypatch):
    monkeypatch.delenv("STARTREFIMMJDOBS", raising=False)

    dbh = _fake_dbh()
    call(dbh, None)
    unrestricted = dbh.cur.queries[-1]
    assert "Exposures" not in unrestricted

    dbh = _fake_dbh()
    call(dbh, [-2, 994])
    restricted = dbh.cur.queries[-1]
    assert SUBQUERY in restricted

    # The restricted query is the unrestricted one with only the qualifier added.
    assert re.sub(r"and \S*expid " + re.escape(SUBQUERY), "", restricted) == unrestricted


def test_get_expids_for_obs_datetime_range_program_ids():

    class FakeDBH:
        exit_code = 0
        program_ids_clause = staticmethod(db.RAPIDDB.program_ids_clause)

        def __init__(self):
            self.queries = []

        def execute_sql_queries(self, queries, debug):
            self.queries.extend(queries)
            return [(5,), (9,)]

    dbh = FakeDBH()
    assert util.get_expids_for_obs_datetime_range(dbh, "2027-01-01 00:00:00", "2027-01-02 00:00:00") == [5, 9]
    assert "Exposures" not in dbh.queries[-1]

    util.get_expids_for_obs_datetime_range(dbh, "2027-01-01 00:00:00", "2027-01-02 00:00:00", program_ids=[-2, 994])
    assert "AND dateobs < '2027-01-02 00:00:00' and expid " + SUBQUERY + "ORDER BY expid;" in dbh.queries[-1]

