"""CI-only: opens a real connection through rapidpipe.db.connection.

Runs only when PGHOST is set (the ``conn`` fixture's own skip in
conftest.py is not reused here since this test exercises connect()
itself, not a pre-opened connection) -- see tests/rapidpipe_db/conftest.py
for why this directory is separate from tests/rapidpipe/ and skips
rather than being ignored at collection.
"""

from __future__ import annotations

import os

import pytest

from rapidpipe.db.connection import connect


def _pg_configured() -> bool:
    return bool(os.environ.get("PGHOST"))


def test_connect_opens_a_real_connection_and_runs_select_1():
    if not _pg_configured():
        pytest.skip("PGHOST is not set; database tests are skipped")

    with connect(application_name="rapidpipe-test-connection-live") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            (value,) = cur.fetchone()
    assert value == 1
