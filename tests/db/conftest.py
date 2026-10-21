"""Fixtures for the database-backed rapidpipe.runs.repository tests.

Separate from tests/unit/ so the plain unit-test job
(.github/workflows/unit-tests.yml) stays database-free: collection of this
directory does no I/O until a fixture is actually used.

Every test gets a fresh connection whose whole body runs inside one
outer transaction that is always rolled back at teardown -- tests never
commit, so one test's rows never leak into the next test's queries. The
repository functions themselves open no transactions of their own (see
rapidpipe.runs.repository's module docstring): they operate on the
connection they are given, so running each test inside one already-open,
never-committed transaction is exactly the isolation psycopg2 gives for
free, no per-test schema reset required.

Failing, not skipping: the ``conn`` and ``connect`` fixtures fail with "PGHOST is not
set; database tests need a database" when ``PGHOST`` is unset. A skip is
never a pass: a suite that silently skips every database test reports
green while proving nothing (foundation plan, Part 5 item 1).
"""

from __future__ import annotations

import os

import pytest

PG_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


def _pg_configured() -> bool:
    return bool(os.environ.get("PGHOST"))


def _connect():
    if not _pg_configured():
        pytest.fail("PGHOST is not set; database tests need a database")
    import psycopg2

    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )


@pytest.fixture()
def conn():
    connection = _connect()
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture()
def connect():
    """A factory of connections to the foundation database (``schema.sql``).

    For tests of the foundation's operations, which commit: the fixture
    empties runs, jobs, job_inputs, official and, by cascade, every product
    table before the test and again after it, so each test starts from no
    rows and leaves none for the rolled-back ``conn`` tests. Every
    connection it hands out is closed at teardown.
    """
    opened = []

    def factory():
        connection = _connect()
        opened.append(connection)
        return connection

    def empty():
        connection = _connect()
        try:
            with connection, connection.cursor() as cur:
                cur.execute("TRUNCATE runs, jobs, job_inputs, official CASCADE")
        finally:
            connection.close()

    empty()
    try:
        yield factory
    finally:
        for connection in opened:
            connection.close()
        empty()
