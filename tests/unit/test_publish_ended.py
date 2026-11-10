"""A published job's row carries ``ended`` from the commit that publishes it.

The job's own ``ended`` write comes after the publisher's ``ANALYZE``
statements, which can wait minutes; a Spot host reclaimed in between left
the row published with ``ended`` null and no ``container_seconds`` (run
188: 30 differences and 88 packets). Here the process dies at the first
``ANALYZE`` and what the publisher committed is read.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest
from psycopg2 import sql

publisher = importlib.import_module("rapidpipe.foundation.publish")


class Reclaimed(BaseException):
    """The host taken away: nothing after it runs."""


def _connection(committed: list[str]):
    pending: list[str] = []

    class Cursor:
        row = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            if isinstance(statement, sql.Composed):
                raise Reclaimed
            pending.append(statement)
            if "FOR UPDATE" in statement:
                self.row = ("launched", 1, None, None)
            elif statement.startswith("SELECT state FROM jobs"):
                self.row = ("launched",)
            elif "SELECT 1 FROM official" in statement:
                self.row = (1,)
            elif "nextval" in statement:
                self.row = (7,)
            elif "SELECT id FROM official" in statement:
                self.row = None

        def fetchone(self):
            return self.row

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            committed.extend(pending)
            pending.clear()

        def rollback(self):
            pending.clear()

    return Connection()


def test_a_job_lost_after_its_publish_commit_keeps_its_ended(tmp_path, monkeypatch):
    path = tmp_path / "diffimages.parquet"
    path.write_bytes(b"d")
    (tmp_path / "manifest.json").write_text(json.dumps({"outputs": [
        {"table": "diffimages", "path": path.name, "checksum": "sha256:" + hashlib.sha256(b"d").hexdigest()}]}))
    monkeypatch.setattr(publisher, "_rows", lambda path: [{"mjdobs": 61348.7, "sca": 8}])
    monkeypatch.setattr(publisher, "_insert", lambda cur, table, rows, job: None)
    committed: list[str] = []

    with pytest.raises(Reclaimed):
        publisher.publish(_connection(committed), 1, 1, tmp_path.as_uri() + "/")

    (published,) = [s for s in committed if "state = 'published'" in s]
    assert "ended =" in published
