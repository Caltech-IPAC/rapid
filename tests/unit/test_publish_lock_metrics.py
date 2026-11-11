"""A publish records its field-lock tries, its wait and its hold split by step in the job's metrics.

The publisher's field-lock wait was 27.8 % of run 232's prompt host-hours and was read only as
an upper bound from log lines (throughput-record-4); its tries, wait and hold become
``job_metrics`` fields: the publish returns them and ``run_job`` adds them to the job's manifest
in its write after the commit that releases the locks.
"""

from __future__ import annotations

import hashlib
import importlib
import json

# The package re-exports the function ``publish`` under the module's name.
publisher = importlib.import_module("rapidpipe.foundation.publish")


def _connection(committed: list[tuple]):
    pending: list[tuple] = []

    class Cursor:
        row = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            pending.append((statement, params))
            if "FOR UPDATE" in statement:
                self.row = ("launched", 1, None, None)
            elif statement.startswith("SELECT state FROM jobs"):
                self.row = ("launched",)

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


def test_one_publish_records_its_tries_wait_and_hold_by_step(tmp_path, monkeypatch):
    path = tmp_path / "diffimages.parquet"
    path.write_bytes(b"d")
    (tmp_path / "manifest.json").write_text(json.dumps({"outputs": [
        {"table": "diffimages", "path": path.name, "checksum": "sha256:" + hashlib.sha256(b"d").hexdigest()}]}))
    monkeypatch.setattr(publisher, "_rows", lambda path: [{"mjdobs": 61348.7, "sca": 8}])
    monkeypatch.setattr(publisher, "_insert", lambda cur, table, rows, job: None)
    held = iter([False, True])  # the first try finds a lock held
    monkeypatch.setattr(publisher.object_catalog, "try_lock", lambda cur, run, fields: next(held))
    monkeypatch.setattr(publisher, "lock_wait", lambda tries: 0.0)
    committed: list[tuple] = []

    metrics = publisher.publish(_connection(committed), 1, 1, tmp_path.as_uri() + "/")["metrics"]

    assert metrics["publish_lock_tries"] == 2 and metrics["publish_lock_fields"] == 0
    assert metrics["publish_lock_wait_s"] >= 0
    steps = {k for k in metrics if k.startswith("publish_hold_") and k != "publish_hold_s"}
    assert steps == {"publish_hold_load_diffimages_s", "publish_hold_record_s",
                     "publish_hold_bind_s", "publish_hold_commit_s"}
    assert sum(metrics[k] for k in steps) <= metrics["publish_hold_s"] + 0.001 * len(steps)
