"""The statistics stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic in :mod:`rapidpipe.selftest.load`'s shape: a synthetic
``crossmatch``-shaped input manifest and a fake database instead of real
PostgreSQL, with the fake database's seed and state files alongside the
prepared inputs, named by the environment variables
:mod:`rapidpipe.selftest.support.fakestatisticsdb` uses
(:data:`~rapidpipe.selftest.support.fakestatisticsdb.SEED_ENV` /
``STATE_ENV``).

The seed is a two-set chain (the input association set and its base, step
1 ruling R3) over two source sets, plus one association set and source
set outside the chain whose rows must not be read. The expected
statistics are recomputed here, per object, with the ported
``compute_radec_statistics`` from the fixture's own source list, and
compared within ``tolerances.statistics_abs``; the counts are literal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.science.statistics.lightcurve import compute_radec_statistics
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakestatisticsdb import (
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_statistics_input_set,
    seed_from_fixture,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakestatisticsdb:fake_database"
DATABASE_ENV = "RAPIDPIPE_STATISTICS_DATABASE"


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    spec = expected["inputs"]
    inputs = work / "inputs"
    build_statistics_input_set(
        inputs, field=int(spec["field"]), instance=spec["association_set"],
        base=spec["base_set"], source_sets=[spec["source_sets"]["delta"]["instance"]])
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("statistics") / "settings.toml").read_text())
    seed = work / "db-seed.json"
    seed.write_text(json.dumps(seed_from_fixture(spec), indent=2))
    state = work / "db-state.json"
    extra_env = {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed), STATE_ENV: str(state)}
    return inputs, overlay, extra_env


def _expected_statistics(obj: dict[str, Any]) -> dict[str, float]:
    ras = [s[2] for s in obj["sources"]]
    decs = [s[3] for s in obj["sources"]]
    fluxes = [s[4] for s in obj["sources"]]
    meanra, meandec, stdra, stddec, _ = compute_radec_statistics(ras, decs)
    return {"meanra": float(meanra), "stdevra": float(stdra), "meandec": float(meandec),
            "stdevdec": float(stddec), "meanflux": float(np.mean(fluxes)),
            "stdevflux": float(np.std(fluxes))}


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    spec = expected["expected"]
    inputs = expected["inputs"]
    tolerance = expected["tolerances"]["statistics_abs"]

    checks.check(list(manifest.inputs.result_sets) == [inputs["association_set"]],
                 f"inputs.result_sets names the association set: {list(manifest.inputs.result_sets)}")
    checks.check(dict(manifest.inputs.products) == {}, "inputs.products is empty")
    checks.check(len(manifest.outputs) == 1, f"one output, got {len(manifest.outputs)}")
    if not manifest.outputs:
        return
    entry = manifest.outputs[0]
    checks.check(entry.kind == "statistics-set" and entry.is_result_set(),
                 "the output is a statistics-set result set (no members)")
    checks.check(is_valid_ulid(entry.instance), "statistics-set instance id is a ULID")
    checks.check(entry.key == {"membership": inputs["association_set"]},
                 f"statistics-set key: got {entry.key}")
    for name in ("table", "row_count", "objects_in_set"):
        checks.check(entry.registration.get(name) == spec[name],
                     f"registration {name}: expected {spec[name]!r}, "
                     f"got {entry.registration.get(name)!r}")

    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database was committed")
        return
    state = json.loads(state_path.read_text())
    checks.check(state["made_tables"] == spec["made_tables"],
                 f"tables made: expected {spec['made_tables']}, got {state['made_tables']}")
    checks.check(state["commits"] == spec["commits"], f"one commit, got {state['commits']}")
    query = state["queries"][0] if state["queries"] else {}
    checks.check(query.get("chain") == spec["chain"],
                 f"membership read through the chain {spec['chain']}, got {query.get('chain')}")
    checks.check(query.get("source_tables") == spec["source_tables"],
                 f"one SELECT per source set: expected {spec['source_tables']}, "
                 f"got {query.get('source_tables')}")

    registered = state["statistics_sets"].get(entry.instance, {})
    checks.check(registered.get("row_count") == spec["row_count"] and registered.get("complete"),
                 f"statistics set registered complete with {spec['row_count']} rows: {registered}")
    checks.check((registered.get("stage"), registered.get("attempt"))
                 == ("statistics", manifest.attempt), "registered by this statistics attempt")
    checks.check(registered.get("result_sets") == [inputs["association_set"]],
                 "registration's inputs.result_sets names the association set")

    rows = state["tables"].get(spec["table"], [])
    by_aid = {int(r["aid"]): r for r in rows}
    checks.check(len(rows) == spec["row_count"] and len(by_aid) == len(rows),
                 f"{spec['row_count']} rows, one per object: got {len(rows)}")
    checks.check(sorted(by_aid) == sorted(int(a) for a in spec["nsources"]),
                 f"objects: expected {sorted(spec['nsources'])}, got {sorted(by_aid)}")
    for r in rows:
        checks.check((r["run"], r["attempt"], r["result_set"])
                     == (manifest.run, manifest.attempt, entry.instance),
                     f"object {r['aid']} run columns")
    for obj in inputs["objects"]:
        aid = obj["aid"]
        row = by_aid.get(aid)
        if row is None:
            continue
        checks.check(int(row["nsources"]) == spec["nsources"][str(aid)],
                     f"object {aid} nsources: expected {spec['nsources'][str(aid)]}, "
                     f"got {row['nsources']}")
        for column, value in _expected_statistics(obj).items():
            checks.close(value, float(row[column]), abs_=tolerance,
                         label=f"object {aid} {column}")

    wrap = by_aid.get(101)
    if wrap is not None:
        meanra = float(wrap["meanra"])
        checks.check(0.0 <= meanra <= 360.0 and min(meanra, 360.0 - meanra) < 1e-3,
                     f"object 101's mean RA sits at the 0/360 wrap, not mid-sky: {meanra}")
    single = by_aid.get(102)
    if single is not None:
        checks.check(float(single["stdevra"]) == 0.0 and float(single["stdevdec"]) == 0.0
                     and float(single["stdevflux"]) == 0.0,
                     "a single-source object has standard deviations 0.0, not NaN")
        checks.close(10.25, float(single["meanra"]), abs_=tolerance, label="object 102 meanra")
        checks.close(-3.5, float(single["meandec"]), abs_=tolerance, label="object 102 meandec")


FIXTURE = StageFixture(
    stage="statistics",
    module="rapidpipe.stages.statistics",
    unit_kind="field",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
