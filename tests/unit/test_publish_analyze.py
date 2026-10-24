"""The publisher analyses every table it wrote to, ``official`` included (PR 277's finding).

A fake connection answers the publisher's statements; the loader is
replaced so no table is needed. The database behaviour itself is
``tests/db/test_publish.py``'s.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest
from psycopg2 import sql

# The package re-exports the function ``publish`` under the module's name.
publisher = importlib.import_module("rapidpipe.foundation.publish")


def _identifier(statement) -> str:
    """The table name of ``ANALYZE {}`` composed with one identifier."""
    (identifier,) = [part for part in statement.seq if isinstance(part, sql.Identifier)]
    return identifier.strings[0]


def _connection(live: bool, analysed: list[str]):
    class Cursor:
        row = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            if isinstance(statement, sql.Composed):
                analysed.append(_identifier(statement))
                return
            if "FOR UPDATE" in statement:
                self.row = ("launched", 1, None, None)
            elif "FROM job_inputs" in statement:
                self.rows = [("sources", {"expid": 1}, None)]
            elif "SELECT 1 FROM official" in statement:
                self.row = (1,) if live else None
            elif "nextval" in statement:
                self.row = (7,)
            elif "SELECT id FROM official" in statement:
                self.row = None

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    return Connection()


@pytest.mark.parametrize("live", [True, False])
def test_the_publisher_analyses_every_table_it_wrote_to(tmp_path, monkeypatch, live):
    outputs = []
    for table in ("diffimages", "sources"):
        path = tmp_path / f"{table}.parquet"
        path.write_bytes(table.encode())
        outputs.append({"table": table, "path": path.name,
                        "checksum": "sha256:" + hashlib.sha256(table.encode()).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"outputs": outputs}))
    loaded = []
    monkeypatch.setattr(publisher, "_rows", lambda path: [{"row": 1}])  # loaded, so bindable
    monkeypatch.setattr(publisher, "_insert", lambda cur, table, rows, job: loaded.append(table))
    analysed: list[str] = []

    result = publisher.publish(_connection(live, analysed), 1, 1, tmp_path.as_uri() + "/")

    written = set(loaded) | ({"official"} if result["group"] is not None else set())
    assert result["published"] and (result["group"] is not None) == live
    assert sorted(analysed) == sorted(written)
    assert ("official" in analysed) == live
