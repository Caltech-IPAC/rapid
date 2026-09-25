"""The export stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic in :mod:`rapidpipe.selftest.statistics`'s shape: a
synthetic input-set manifest naming two source sets and an association
set, and a fake database instead of real PostgreSQL
(:mod:`rapidpipe.selftest.support.fakeexport`), with its seed and state
files alongside the prepared inputs. hats-import runs for real either side
of ``--real-tools`` (it is pure Python, in the pipeline image's
environment), so there is one ``expected`` spec.

The checks: one ``catalog-export`` entry that passes
:func:`rapidpipe.products.catalogexport.validate_catalog_export_entry`,
its key, row count, source sets and partition count; the catalog's own
``properties`` file agreeing on the row count; and the fake database's
record of what was read -- only the named source sets, never the unnamed
one or the association set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.catalogexport import selection_digest, validate_catalog_export_entry
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakeexport import (
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_export_input_set,
    seed_from_fixture,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakeexport:fake_database"
DATABASE_ENV = "RAPIDPIPE_EXPORT_DATABASE"


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    spec = expected["inputs"]
    inputs = work / "inputs"
    build_export_input_set(inputs, result_sets=spec["named"])
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("export") / "settings.toml").read_text())
    seed = work / "db-seed.json"
    seed.write_text(json.dumps(seed_from_fixture(spec)))
    state = work / "db-state.json"
    return inputs, overlay, {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed),
                             STATE_ENV: str(state)}


def _properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    spec = expected["expected"]
    named = expected["inputs"]["named"]

    checks.check(list(manifest.inputs.result_sets) == named,
                 f"inputs.result_sets names every named set: {list(manifest.inputs.result_sets)}")
    checks.check(len(manifest.outputs) == 1, f"one output, got {len(manifest.outputs)}")
    if not manifest.outputs:
        return
    entry = manifest.outputs[0]
    checks.check(entry.kind == "catalog-export" and not entry.is_result_set(),
                 "the output is a catalog-export file product")
    checks.check(is_valid_ulid(entry.instance), "catalog-export instance id is a ULID")
    try:
        validate_catalog_export_entry(entry.to_dict())
        checks.check(True, "catalog-export entry validates")
    except ValueError as exc:
        checks.check(False, f"catalog-export entry validates: {exc}")

    key = entry.key
    checks.check(key.get("field") == int(UNIT_ID) and key.get("export_type") == spec["export_type"]
                 and key.get("selection") == selection_digest(spec["source_sets"]),
                 f"catalog-export key: got {key}")
    reg = entry.registration
    checks.check(reg.get("row_count") == spec["row_count"],
                 f"row_count: expected {spec['row_count']}, got {reg.get('row_count')}")
    checks.check(reg.get("source_sets") == spec["source_sets"],
                 f"source_sets: expected {spec['source_sets']}, got {reg.get('source_sets')}")
    checks.check(isinstance(reg.get("partition_count"), int)
                 and reg["partition_count"] >= spec["min_partition_count"],
                 f"partition_count >= {spec['min_partition_count']}: {reg.get('partition_count')}")
    checks.check(entry.primary == f"{spec['catalog_dir']}/properties"
                 or entry.primary == f"{spec['catalog_dir']}/hats.properties",
                 f"primary is the catalog's properties file: {entry.primary}")

    primary = context.outputs / (entry.primary or "")
    if primary.is_file():
        rows = _properties(primary).get("hats_nrows")
        checks.check(rows == str(spec["row_count"]),
                     f"catalog properties hats_nrows: expected {spec['row_count']}, got {rows}")
    else:
        checks.check(False, f"primary {entry.primary} exists")

    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database recorded its reads")
        return
    state = json.loads(state_path.read_text())
    queries = state["queries"]
    checks.check(len(queries) == 1 and queries[0]["source_sets"] == spec["source_sets"],
                 f"one read, of the named source sets only: {queries}")
    checks.check(state["rows_read"] == spec["row_count"],
                 f"rows read: expected {spec['row_count']}, got {state['rows_read']}")


FIXTURE = StageFixture(
    stage="export",
    module="rapidpipe.stages.export",
    unit_kind="field",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
