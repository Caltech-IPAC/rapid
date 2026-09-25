"""A psycopg2-shaped fake connection for ``rapidpipe.release.core``.

Understands exactly the statements ``core`` issues (schema_migrations,
releases and release_deployments reads and writes), with transaction
semantics: writes go to a pending copy that ``commit`` publishes and
``rollback`` discards. Every commit that changed a release appends
``db:commit:<tag>:<state>`` to ``events`` -- and, when ``log_path`` is
given, to that file, so a test can interleave database checkpoints with
the hook scripts' own log lines and assert the order of a cut.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class _Info:
    def __init__(self, dbname: str):
        self.dbname = dbname


class FakeReleaseDB:
    def __init__(self, *, dbname: str = "rapid_rebuild", log_path: Path | None = None,
                 releases_table: bool = True):
        self.info = _Info(dbname)
        self.log_path = log_path
        self.events: list[str] = []
        self.committed = {
            "schema_migrations": {},  # filename -> sha256
            "releases": {},           # tag -> dict
            "deployments": {},        # (tag, consumer) -> dict
            "releases_table": releases_table,
        }
        self._pending = copy.deepcopy(self.committed)
        self.commits = 0
        self.rollbacks = 0

    # -- psycopg2 connection surface ----------------------------------
    def cursor(self):
        return _Cursor(self)

    def commit(self):
        before = {tag: r["state"] for tag, r in self.committed["releases"].items()}
        self.committed = copy.deepcopy(self._pending)
        self.commits += 1
        for tag, row in self.committed["releases"].items():
            if before.get(tag) != row["state"] or tag not in before:
                self._event(f"db:commit:{tag}:{row['state']}")

    def rollback(self):
        self._pending = copy.deepcopy(self.committed)
        self.rollbacks += 1

    # -- test helpers -------------------------------------------------
    def apply(self, name: str, sha256: str) -> None:
        """Record a migration as applied (committed), as the applier does."""
        self.committed["schema_migrations"][name] = sha256
        self._pending["schema_migrations"][name] = sha256

    def release(self, tag: str) -> dict[str, Any] | None:
        return self.committed["releases"].get(tag)

    def deployments(self, tag: str) -> dict[str, str]:
        return {c: d["job_definition"] for (t, c), d in self.committed["deployments"].items()
                if t == tag}

    def _event(self, text: str) -> None:
        self.events.append(text)
        if self.log_path is not None:
            with open(self.log_path, "a") as fh:
                fh.write(text + "\n")


class _Cursor:
    def __init__(self, db: FakeReleaseDB):
        self.db = db
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def execute(self, sql: str, params: tuple = ()):
        state = self.db._pending
        text = " ".join(sql.split())
        if text.startswith("SELECT filename, sha256 FROM schema_migrations"):
            self._rows = list(state["schema_migrations"].items())
        elif text.startswith("SELECT to_regclass('releases')"):
            self._rows = [(state["releases_table"],)]
        elif text.startswith("SELECT tag, source_revision") and "WHERE tag = %s" in text:
            row = state["releases"].get(params[0])
            self._rows = [self._release_tuple(row)] if row else []
        elif text.startswith("SELECT tag, source_revision") and "ORDER BY" in text:
            rows = sorted(state["releases"].values(), key=lambda r: (r["cut_at"], r["tag"]))
            self._rows = [self._release_tuple(r) for r in rows]
        elif text.startswith("SELECT consumer, job_definition, deployed_at, deployed_by"):
            self._rows = sorted(
                (d["consumer"], d["job_definition"], d["deployed_at"], d["deployed_by"])
                for (tag, _c), d in state["deployments"].items() if tag == params[0])
        elif text.startswith("INSERT INTO releases"):
            tag, revision, schema_version, rstate, cut_by, cut_at, notes = params
            if tag in state["releases"]:
                raise RuntimeError(f"duplicate key releases.tag {tag}")
            state["releases"][tag] = {
                "tag": tag, "source_revision": revision, "schema_version": schema_version,
                "image_digest": None, "image_ref": None, "state": rstate, "cut_by": cut_by,
                "cut_at": cut_at, "completed_at": None, "notes": json.loads(notes)}
        elif text.startswith("UPDATE releases SET"):
            rstate, digest, ref, notes, complete, tag = params
            row = state["releases"][tag]
            row.update(state=rstate, image_digest=digest, image_ref=ref,
                       notes=json.loads(notes))
            if complete:
                row["completed_at"] = datetime.now(timezone.utc)
        elif text.startswith("INSERT INTO release_deployments"):
            tag, consumer, job_definition, by = params
            state["deployments"][(tag, consumer)] = {
                "consumer": consumer, "job_definition": job_definition,
                "deployed_at": datetime.now(timezone.utc), "deployed_by": by}
        else:
            raise AssertionError(f"FakeReleaseDB does not understand: {text}")

    @staticmethod
    def _release_tuple(row):
        return (row["tag"], row["source_revision"], row["schema_version"],
                row["image_digest"], row["image_ref"], row["state"], row["cut_by"],
                row["cut_at"], row["completed_at"], copy.deepcopy(row["notes"]))
