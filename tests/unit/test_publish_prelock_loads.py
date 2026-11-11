"""A publish loads its sources and xsources before it takes its field locks, once, whatever the tries.

The two loads were 58.0 % of run 234's field-lock hold (publish-lock-metrics) for tables whose rows
go to date-and-SCA children that no field lock protects; a try that finds a lock held rolls back
and waits with no pool connection (publish-pool-wedge), so it must not have loaded them first.
"""

from __future__ import annotations

import hashlib
import importlib
import json

# The package re-exports the function ``publish`` under the module's name.
publisher = importlib.import_module("rapidpipe.foundation.publish")


def _connection(events: list[str], committed: list[str]):
    pending: list[str] = []

    class Cursor:
        row = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            if "FOR UPDATE" in statement:
                self.row = ("launched", 1, None, None)
            elif statement.startswith("SELECT state FROM jobs"):
                self.row = ("launched",)
            elif "create_catalog_child" in statement:
                self.row = (f"{params[0]}_child",)

        def copy_expert(self, statement, buffer):
            table = "xsources" if "xsources" in repr(statement) else "sources"
            events.append(f"load {table}")
            pending.extend(f"{table} {line}" for line in buffer.read().splitlines())

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


def test_a_publish_whose_first_try_finds_a_lock_held_loads_sources_and_xsources_once_before_locking(
        tmp_path, monkeypatch):
    outputs = []
    for table in ("sources", "xsources"):
        path = tmp_path / f"{table}.parquet"
        path.write_bytes(table.encode())
        outputs.append({"table": table, "path": path.name,
                        "checksum": "sha256:" + hashlib.sha256(table.encode()).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"outputs": outputs}))
    monkeypatch.setattr(publisher, "_rows", lambda path: [{"mjdobs": 61348.7, "sca": 3, "rb": 0.5}])
    events: list[str] = []
    holder = iter([True])  # another publisher holds a field's lock at the first look, then not

    def held(cur, run, fields):
        events.append("look")
        return next(holder, False)

    def try_lock(cur, run, fields):
        events.append("lock")
        return not held(cur, run, fields)

    monkeypatch.setattr(publisher.object_catalog, "held", held, raising=False)
    monkeypatch.setattr(publisher.object_catalog, "try_lock", try_lock)
    monkeypatch.setattr(publisher, "lock_wait", lambda tries: 0.0)
    committed: list[str] = []

    result = publisher.publish(_connection(events, committed), 1, 1, tmp_path.as_uri() + "/")

    loads = [e for e in events if e.startswith("load")]
    assert sorted(loads) == ["load sources", "load xsources"]
    assert sorted(committed) == ["sources 61348.7,3,0.5,1", "xsources 61348.7,3,0.5,1"]
    assert max(events.index(e) for e in loads) < events.index("lock")
    metrics = result["metrics"]
    assert metrics["publish_lock_tries"] == 2
    assert {"publish_load_sources_s", "publish_load_xsources_s"} <= metrics.keys()
    assert not {"publish_hold_load_sources_s", "publish_hold_load_xsources_s"} & metrics.keys()
