"""The maintain stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic follows :mod:`rapidpipe.selftest.load`'s shape: a
synthetic input manifest and a fake database instead of real PostgreSQL,
plus the fake database's seed and state files alongside the prepared
inputs, named by the environment variables
:mod:`rapidpipe.selftest.support.fakemaintaindb` uses
(:data:`~rapidpipe.selftest.support.fakemaintaindb.SEED_ENV` /
``STATE_ENV``). ``maintain`` declares no settings, so the overlay this
fixture writes is empty -- there is no packaged ``settings.toml`` to
read, unlike ``difference``'s and ``load``'s fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture
from rapidpipe.selftest.support.fakemaintaindb import (
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_maintain_input_set,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakemaintaindb:fake_database"
DATABASE_ENV = "RAPIDPIPE_MAINTAIN_DATABASE"


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    spec = expected["inputs"]
    build_maintain_input_set(inputs, table=spec["table"], instance=spec["source_set_instance"])
    overlay = work / "settings.toml"
    overlay.write_text("")
    seed = work / "db-seed.json"
    seed.write_text(json.dumps({"existing_tables": spec["existing_tables"]}))
    state = work / "db-state.json"
    extra_env = {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed), STATE_ENV: str(state)}
    return inputs, overlay, extra_env


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    spec = expected["expected"]

    checks.check(manifest.outputs == (), f"no outputs, got {len(manifest.outputs)}")
    checks.check(list(manifest.inputs.result_sets) == spec["result_sets_read"],
                 f"inputs.result_sets: expected {spec['result_sets_read']}, "
                 f"got {list(manifest.inputs.result_sets)}")

    record_path = context.outputs / manifest.execution_record
    if not record_path.exists():
        checks.check(False, "execution record written")
        return
    record = json.loads(record_path.read_text())
    notes = record.get("notes", {})
    checks.check(notes.get("table") == spec["table"],
                 f"execution record notes.table: expected {spec['table']!r}, got {notes.get('table')!r}")
    checks.check(notes.get("clustered") is True, "execution record notes.clustered is true")

    # `_prepare` wrote the fake database's state file to `work_dir` (the
    # `STATE_ENV` path), independent of where `--output-location` sent the
    # stage's own outputs -- as load.py's fixture does.
    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database was committed")
        return
    state = json.loads(state_path.read_text())
    checks.check(state["clustered"] == spec["clustered"],
                 f"clustered: expected {spec['clustered']}, got {state['clustered']}")
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")


FIXTURE = StageFixture(
    stage="maintain",
    module="rapidpipe.stages.maintain",
    unit_kind="detector-date",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
