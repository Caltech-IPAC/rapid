"""The publisher runs no ``ANALYZE`` and nothing after the commit that publishes (publish-analyze).

Each ``ANALYZE`` holds its table's self-conflicting lock to the end of its
transaction, so a post-commit ``ANALYZE`` serialised every publisher behind
the first table they shared (run 215); autovacuum keeps the statistics.

A fake connection answers the publisher's statements and records each with
the commit that ends it; the loader is replaced so no table is needed. The
database behaviour itself is ``tests/db/test_publish.py``'s.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest

# The package re-exports the function ``publish`` under the module's name.
publisher = importlib.import_module("rapidpipe.foundation.publish")


def _connection(live: bool, statements: list[str]):
    class Cursor:
        row = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            statement = statement if isinstance(statement, str) else repr(statement)
            statements.append(statement)
            if "FOR UPDATE" in statement:
                self.row = ("launched", 1, None, None)
            elif statement.startswith("SELECT state FROM jobs"):
                self.row = ("launched",)
            elif "create_catalog_child" in statement:
                self.row = ("sources_20261104_8",)
            elif "FROM job_inputs" in statement:
                self.rows = [("sources", {"expid": 1}, None)]
            elif "SELECT 1 FROM official" in statement:
                self.row = (1,) if live else None
            elif "nextval" in statement:
                self.row = (7,)
            elif "SELECT id FROM official" in statement:
                self.row = None

        def copy_expert(self, statement, stream):
            statements.append(repr(statement))

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            statements.append("COMMIT")

        def rollback(self):
            statements.append("ROLLBACK")

    return Connection()


@pytest.mark.parametrize("live", [True, False])
def test_the_publisher_runs_no_analyze_and_nothing_after_it_publishes(tmp_path, monkeypatch, live):
    outputs = []
    for table in ("diffimages", "sources"):
        path = tmp_path / f"{table}.parquet"
        path.write_bytes(table.encode())
        outputs.append({"table": table, "path": path.name,
                        "checksum": "sha256:" + hashlib.sha256(table.encode()).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"outputs": outputs}))
    # Loaded, so bindable; mjdobs and sca place the sources rows in their child.
    monkeypatch.setattr(publisher, "_rows", lambda path: [{"mjdobs": 61348.7, "sca": 8}])
    monkeypatch.setattr(publisher, "_insert", lambda cur, table, rows, job: None)
    statements: list[str] = []

    result = publisher.publish(_connection(live, statements), 1, 1, tmp_path.as_uri() + "/")

    assert result["published"] and (result["group"] is not None) == live
    assert not [s for s in statements if "ANALYZE" in s.upper()]
    published = max(i for i, s in enumerate(statements) if "state = 'published'" in s)
    assert statements[published:].count("COMMIT") == 1 and statements[-1] == "COMMIT"
