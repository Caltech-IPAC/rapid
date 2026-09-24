"""The prune stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic follows :mod:`rapidpipe.selftest.load`'s shape: a
synthetic crossmatch-shaped input manifest and a fake database instead of
real PostgreSQL, plus the fake database's seed and state files alongside
the prepared inputs, named by the environment variables
:mod:`rapidpipe.selftest.support.fakeprunedb` uses
(:data:`~rapidpipe.selftest.support.fakeprunedb.SEED_ENV` / ``STATE_ENV``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakeprunedb import (
    ASSOCIATION_INSTANCE,
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_prune_input_set,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakeprunedb:fake_database"
DATABASE_ENV = "RAPIDPIPE_PRUNE_DATABASE"


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    spec = expected["inputs"]
    association = spec["association"]
    build_prune_input_set(inputs, field=spec["field"], association_instance=association["instance"],
                          base=association["base"], source_sets=association["source_sets"])
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("prune") / "settings.toml").read_text())
    seed = work / "db-seed.json"
    seed.write_text(json.dumps({
        "association_sets": {
            association["instance"]: {"base": association["base"],
                                      "source_sets": association["source_sets"]}},
        "source_set_tables": spec["source_set_tables"],
        "sources": spec["sources"],
        "diffimages": spec["diffimages"],
        "merges": spec["merges"],
    }))
    state = work / "db-state.json"
    extra_env = {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed), STATE_ENV: str(state)}
    return inputs, overlay, extra_env


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    spec = expected["expected"]

    checks.check(list(manifest.inputs.result_sets) == [ASSOCIATION_INSTANCE],
                 "inputs.result_sets names the crossmatch association instance")
    checks.check(len(manifest.outputs) == 1, f"one output, got {len(manifest.outputs)}")
    if not manifest.outputs:
        return
    entry = manifest.outputs[0]
    checks.check(entry.kind == "pruned-set" and entry.is_result_set(),
                 "the output is a pruned-set result set (no members)")
    checks.check(is_valid_ulid(entry.instance), "pruned-set instance id is a ULID")
    checks.check(entry.key.get("base") == ASSOCIATION_INSTANCE,
                 f"pruned-set key names the base association set: got {entry.key}")
    for field, value in (("row_count", spec["row_count"]), ("base_row_count", spec["base_row_count"]),
                        ("table", spec["table"]), ("rule", spec["rule"])):
        checks.check(entry.registration.get(field) == value,
                     f"registration {field}: expected {value!r}, got {entry.registration.get(field)!r}")

    # `_prepare` wrote the fake database's state file to `work_dir` (the
    # `STATE_ENV` path), independent of where `--output-location` sent the
    # stage's own outputs -- as load.py's and maintain.py's fixtures do.
    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database was committed")
        return
    state = json.loads(state_path.read_text())
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")
    pruned = state["pruned_sets"].get(entry.instance, {})
    checks.check(pruned.get("row_count") == spec["row_count"] and pruned.get("complete"),
                 f"pruned set registered complete with {spec['row_count']} rows: {pruned}")
    got_pairs = sorted([row["aid"], row["sid"]] for row in state["prunedmerges"]
                       if row["result_set"] == entry.instance)
    checks.check(got_pairs == [list(pair) for pair in spec["excluded_pairs"]],
                 f"excluded pairs: expected {spec['excluded_pairs']}, got {got_pairs}")


FIXTURE = StageFixture(
    stage="prune",
    module="rapidpipe.stages.prune",
    unit_kind="field",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
