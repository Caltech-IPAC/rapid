"""The load stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic ported from ``tests/fixtures/load/run_fixture.py``
(the pre-existing ``make stage-load`` fixture), unchanged in substance --
this module supplies the same two hooks, reading the packaged fixture
data instead of a copy local to the test tree.

The load stage's fixture also needs a fake database and its seed/state
files, unlike difference's fixture (no database access declared): those
live in ``work_dir`` alongside the prepared inputs, named by the same
environment variables ``tests/fixtures/load/run_fixture.py`` uses
(:data:`rapidpipe.selftest.support.fakeloaddb.SEED_ENV` /
``STATE_ENV``), so ``--work-dir`` inspection after a run looks the same
either way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import healpy as hp

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakeloaddb import (
    DIFFERENCE_INSTANCE,
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_load_input_set,
    finder_row,
    main_row,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakeloaddb:fake_database"
DATABASE_ENV = "RAPIDPIPE_LOAD_DATABASE"


def _catalogs(spec: dict[str, Any]) -> dict[str, tuple[list[dict], list[dict]]]:
    out = {}
    for sign, cat in spec.items():
        main = [main_row(i, float(x), float(y), ra, dec) for i, x, y, ra, dec in cat["main"]]
        out[sign] = (main, [finder_row(i) for i in cat["finder_ids"]])
    return out


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    build_load_input_set(inputs, _catalogs(expected["inputs"]["catalogs"]))
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("load") / "settings.toml").read_text())
    seed = work / "db-seed.json"
    seed.write_text(json.dumps({"differences": {
        DIFFERENCE_INSTANCE: expected["inputs"]["difference_row"]}}))
    state = work / "db-state.json"
    extra_env = {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed), STATE_ENV: str(state)}
    return inputs, overlay, extra_env


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    spec = expected["expected"]
    tolerance = expected["tolerances"]["position_deg_abs"]

    checks.check(manifest.inputs.products.get("difference-image") == DIFFERENCE_INSTANCE,
                 "inputs.products names the difference instance")
    checks.check({"source-catalog/positive", "source-catalog/negative"}
                 <= set(manifest.inputs.products), "inputs.products names both catalogs")
    checks.check(len(manifest.outputs) == 1, f"one output, got {len(manifest.outputs)}")
    if not manifest.outputs:
        return
    entry = manifest.outputs[0]
    checks.check(entry.kind == "source-set" and entry.is_result_set(),
                 "the output is a source-set result set (no members)")
    checks.check(is_valid_ulid(entry.instance), "source-set instance id is a ULID")
    checks.check(entry.key == {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"},
                 f"source-set key: got {entry.key}")
    for field, value in (("row_count", spec["row_count"]), ("table", spec["table"]),
                         ("rows_by_sign", spec["rows_by_sign"])):
        checks.check(entry.registration.get(field) == value,
                     f"registration {field}: expected {value!r}, got {entry.registration.get(field)!r}")

    # `_prepare` wrote the fake database's state file to `work_dir` (the
    # `STATE_ENV` path), independent of where `--output-location` sent the
    # stage's own outputs.
    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database was committed")
        return
    state = json.loads(state_path.read_text())
    checks.check(state["made_tables"] == spec["made_tables"],
                 f"child tables made: expected {spec['made_tables']}, got {state['made_tables']}")
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")
    rows = state["tables"].get(spec["table"], [])
    checks.check([[int(r["id"]), r["isdiffpos"]] for r in rows] == spec["rows_in_order"],
                 f"rows in order: expected {spec['rows_in_order']}, "
                 f"got {[[r['id'], r['isdiffpos']] for r in rows]}")
    instance = manifest.outputs[0].instance if manifest.outputs else None
    source_set = state["source_sets"].get(instance, {})
    checks.check(source_set.get("row_count") == spec["row_count"] and source_set.get("complete"),
                 f"source set registered complete with {spec['row_count']} rows: {source_set}")
    checks.check((source_set.get("stage"), source_set.get("attempt")) == ("load", manifest.attempt),
                 "source set registered by this load attempt")

    tessellation = RomanTessellationClosedForm()
    for r in rows:
        label = f"row {r['id']}/{r['isdiffpos']}"
        for column, value in spec["every_row"].items():
            checks.check(r[column] == value, f"{label} {column}: expected {value}, got {r[column]}")
        checks.check((r["run"], r["attempt"], r["result_set"]) == (manifest.run, manifest.attempt, instance),
                     f"{label} run columns")
        ra, dec = float(r["ra"]), float(r["dec"])
        checks.check(int(r["hp6"]) == hp.ang2pix(64, ra, dec, nest=True, lonlat=True),
                     f"{label} hp6 recomputed")
        checks.check(int(r["hp9"]) == hp.ang2pix(512, ra, dec, nest=True, lonlat=True),
                     f"{label} hp9 recomputed")
        checks.check(int(r["field"]) == tessellation.get_rtid(ra, dec),
                     f"{label} field recomputed row by row")
        xfit, yfit = float(r["xfit"]), float(r["yfit"])
        checks.check(-0.5 <= xfit <= 64.5 and -0.5 <= yfit <= 64.5, f"{label} fit position in bounds")

    by_key = {(r["id"], r["isdiffpos"]): r for r in rows}
    for exact in spec["exact"]:
        row = by_key.get((exact["id"], exact["isdiffpos"]))
        checks.check(row is not None, f"row {exact['id']}/{exact['isdiffpos']} loaded")
        if row is None:
            continue
        for column, value in exact.items():
            checks.check(row[column] == value,
                         f"row {exact['id']}/{exact['isdiffpos']} {column}: "
                         f"expected {value}, got {row[column]}")
    catalogs = {1: (269.45, -28.77), 5: (269.46, -28.78)}
    for (id_, sign), row in by_key.items():
        if sign == "true" and int(id_) in catalogs:
            ra, dec = catalogs[int(id_)]
            checks.check(abs(float(row["ra"]) - ra) <= tolerance
                         and abs(float(row["dec"]) - dec) <= tolerance,
                         f"row {id_}/{sign} ra, dec within {tolerance} deg")


FIXTURE = StageFixture(
    stage="load",
    module="rapidpipe.stages.load",
    unit_kind="detector-image",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
