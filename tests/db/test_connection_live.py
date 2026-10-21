"""CI-only: opens a real connection through rapidpipe.db.connection.

Fails when PGHOST is unset, like the ``conn`` fixture in conftest.py,
which is not reused here because this test exercises connect() itself,
not a pre-opened connection; see tests/db/conftest.py for why a missing
database is a failure and not a skip.
"""

from __future__ import annotations

import os

import pytest

from rapidpipe.db.connection import connect


def _pg_configured() -> bool:
    return bool(os.environ.get("PGHOST"))


def test_connect_opens_a_real_connection_and_runs_select_1():
    if not _pg_configured():
        pytest.fail("PGHOST is not set; database tests need a database")

    with connect(application_name="rapidpipe-test-connection-live") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            (value,) = cur.fetchone()
    assert value == 1
