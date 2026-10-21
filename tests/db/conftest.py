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

Failing, not skipping: the ``conn`` fixture fails with "PGHOST is not
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


@pytest.fixture()
def conn():
    if not _pg_configured():
        pytest.fail("PGHOST is not set; database tests need a database")
    import psycopg2

    connection = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
