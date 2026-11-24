"""``datasets``: a run names a registered dataset, so a misspelt name records nothing.

Contracts (rapid_docs decisions, datasets-table, Russ Laher 2026-11-24):
``schema.sql`` registers ``flight``; ``create_dataset`` registers a name once
and refuses it a second time, changing nothing; ``list_datasets`` prints them
by name; ``create_run`` under a name not registered raises ``NotFound``,
naming the registered datasets, and records no run, and the table's foreign
key refuses such a run however it is written; ``database/2026-11-24-datasets.sql``
registers every dataset an existing database's runs name before it adds the
key, and a second application fails and changes nothing. Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest

from rapidpipe.foundation import NotFound, Refused, create_dataset, create_run, list_datasets

DATABASE = Path(__file__).resolve().parents[2] / "database"


def test_schema_registers_flight_and_a_dataset_is_registered_once(connect):
    conn = connect()
    assert [d["name"] for d in list_datasets(conn)] == ["flight"]
    assert create_dataset(conn, "flight-trial-1", "a trial on flight data") == "flight-trial-1"
    with pytest.raises(Refused, match="'flight-trial-1' is already registered"):
        create_dataset(conn, "flight-trial-1", "a second description")
    rows = list_datasets(conn)
    assert [(d["name"], d["description"]) for d in rows] == [
        ("flight", "Flight, every program; the first, program 1049"),
        ("flight-trial-1", "a trial on flight data")]
    assert set(rows[0]) == {"name", "description", "created"}


def test_a_run_under_a_name_not_registered_is_refused_naming_the_registered_and_records_nothing(connect):
    conn = connect()
    create_dataset(conn, "r00342-local", "HLWAS r00342 on a laptop")
    with pytest.raises(NotFound, match=r"dataset 'r00342-locl' is not registered .*"
                                       r"the registered datasets: flight, r00342-local"):
        create_run(conn, "tester", "a typo", "abc123", {}, "HLWAS", "r00342-locl")
    run = create_run(conn, "tester", "the name", "abc123", {}, "HLWAS", "r00342-local")
    with conn.cursor() as cur:
        cur.execute("SELECT id, dataset FROM runs")
        assert cur.fetchall() == [(run, "r00342-local")]
    conn.commit()


def test_the_foreign_key_refuses_a_run_however_it_is_written(conn):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset)"
                        " VALUES ('t', 't', 'r', '{}', 'S', 'not-registered')")


def _connect(name: str):
    return psycopg2.connect(host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
                            dbname=name, user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))


def _admin():
    admin = _connect("postgres")
    admin.autocommit = True
    return admin


def _psql(name: str, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", name, "-f", str(path)],
                          capture_output=True, text=True, env={**os.environ, "PGDATABASE": name})


def test_the_dated_file_registers_every_dataset_runs_name_then_adds_the_key(conn):
    """On a database of the shape before the table (schema.sql less it), with runs of two datasets."""
    name = f"rapid_datasets_file_{os.getpid()}"
    admin = _admin()
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name}")
            cur.execute(f"CREATE DATABASE {name}")
        assert _psql(name, DATABASE / "schema.sql").returncode == 0
        before = _connect(name)
        with before, before.cursor() as cur:
            cur.execute("ALTER TABLE runs DROP CONSTRAINT runs_dataset_fkey")
            cur.execute("DROP TABLE datasets")
            cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset) VALUES"
                        " ('t', 't', 'r', '{}', 'HLWAS', 'r00342-local'),"
                        " ('t', 't', 'r', '{}', 'GBTDS', 'window-smoke'),"
                        " ('t', 't', 'r', '{}', 'HLWAS', 'window-smoke')")
        before.close()
        applied = _psql(name, DATABASE / "2026-11-24-datasets.sql")
        assert applied.returncode == 0, applied.stderr
        again = _psql(name, DATABASE / "2026-11-24-datasets.sql")
        assert again.returncode != 0 and 'relation "datasets" already exists' in again.stderr
        after = _connect(name)
        try:
            with after.cursor() as cur:
                cur.execute("SELECT name, description FROM datasets ORDER BY name")
                assert cur.fetchall() == [
                    ("flight", "Flight, every program; the first, program 1049"),
                    ("r00342-local", "Registered by 2026-11-24-datasets.sql from its runs 1 to 1"),
                    ("window-smoke", "Registered by 2026-11-24-datasets.sql from its runs 2 to 3")]
                with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                    cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset)"
                                " VALUES ('t', 't', 'r', '{}', 'S', 'typo')")
        finally:
            after.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()
