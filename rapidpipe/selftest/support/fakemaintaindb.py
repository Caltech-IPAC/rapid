"""Stand-ins for the maintain stage's boundaries: a load-shaped input manifest, a fake database.

- :func:`build_maintain_input_set` writes a completion manifest shaped
  like ``load``'s own (stage ``load``, unit kind ``detector-image``,
  a single ``source-set`` output entry) -- the simplest of the two input
  shapes ``maintain`` accepts (the other being a stage input-set manifest
  naming several); ``maintain`` itself does not require any particular
  ``stage`` or ``unit.kind`` on its input manifest, only that every entry
  it reads is a ``source-set`` for its own unit's table.
- :class:`FakeMaintainDatabase` has
  :class:`rapidpipe.stages.maintain.PostgresMaintainDatabase`'s methods
  over in-memory state: which child tables exist, and which table(s) got
  CLUSTERed and ANALYZEd. :func:`fake_database` is the factory
  ``RAPIDPIPE_MAINTAIN_DATABASE`` names for a stage run as a subprocess
  (``make stage-maintain``): it reads its seed from the JSON file
  ``RAPIDPIPE_FAKE_MAINTAIN_SEED`` names and writes its state, on commit,
  to the JSON file ``RAPIDPIPE_FAKE_MAINTAIN_STATE`` names.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time -- ``containers/rapid-pipeline/
build.sh``) so ``rapidpipe selftest --stage maintain`` can import it
inside the image. ``tests/unit/fakemaintaindb.py`` re-exports this module
for the test suite.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from rapidpipe.db import sources as _sources

UNIT_ID = "20260821/SCA07"
OBS_DATE = "20260821"
SCA = 7
TABLE = "sources_20260821_7"

RUN_ID = "01J8Y6QZ3M00000000000MRUN"
ATTEMPT_ID = "01J8Y6QZ3M0000000000MATT0"
SOURCE_SET_INSTANCE = "01J8Y6QZ3M0000000000MSS01"


def build_maintain_input_set(
    inputs: Path,
    *,
    table: str = TABLE,
    instance: str = SOURCE_SET_INSTANCE,
    key: dict[str, Any] | None = None,
    row_count: int = 4,
) -> Path:
    """Write a `load`-shaped completion manifest naming one `source-set` entry.

    ``key`` defaults to a `photutils` key for a difference instance this
    fixture invents; it is never read by ``maintain``, only
    ``registration.table``.
    """
    manifest = {
        "schema_version": "1", "run": RUN_ID,
        "unit": {"kind": "detector-image", "id": "e20260821001234/SCA07"},
        "stage": "load", "attempt": ATTEMPT_ID,
        "execution_record": f"exec/{ATTEMPT_ID}.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {}, "result_sets": []},
        "outputs": [{
            "kind": "source-set", "format_version": "1", "instance": instance,
            "key": key or {"difference": "01J8Y6QZ3M00000000000000D1", "catalog_type": "photutils"},
            "primary": None, "members": [],
            "registration": {"row_count": row_count, "table": table,
                             "rows_by_sign": {"positive": row_count, "negative": 0}},
        }],
    }
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


class FakeMaintainDatabase:
    """In-memory :class:`~rapidpipe.stages.maintain.PostgresMaintainDatabase`."""

    def __init__(self, existing_tables: list[str] | None = None) -> None:
        self.existing_tables: set[str] = set(existing_tables or [])
        self.clustered: list[str] = []
        self.commits = 0
        self.on_commit = None

    def child_table_exists(self, table: str) -> bool:
        return table in self.existing_tables

    def cluster_and_analyze(self, obs_date: str, sca: int) -> None:
        self.clustered.append(_sources.child_table_name(obs_date, sca))

    def commit(self) -> None:
        self.commits += 1
        if self.on_commit is not None:
            self.on_commit(self)

    def state(self) -> dict[str, Any]:
        return {"existing_tables": sorted(self.existing_tables),
                "clustered": list(self.clustered), "commits": self.commits}


SEED_ENV = "RAPIDPIPE_FAKE_MAINTAIN_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_MAINTAIN_STATE"


def fake_database():
    """The ``RAPIDPIPE_MAINTAIN_DATABASE`` factory: seeded from, and saved to, JSON files."""
    seed = json.loads(Path(os.environ[SEED_ENV]).read_text())
    db = FakeMaintainDatabase(existing_tables=seed.get("existing_tables"))
    state_path = Path(os.environ[STATE_ENV])

    def save(database: FakeMaintainDatabase) -> None:
        state_path.write_text(json.dumps(database.state(), indent=2, sort_keys=True))

    db.on_commit = save
    return contextlib.nullcontext(db)
