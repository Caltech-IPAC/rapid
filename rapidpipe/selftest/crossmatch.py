"""The crossmatch stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic follows :mod:`rapidpipe.selftest.load`'s shape: a
synthetic input manifest and a fake database instead of real PostgreSQL,
the fake's seed and state files alongside the prepared inputs, named by
the environment variables :mod:`rapidpipe.selftest.support.fakecrossmatchdb`
uses (:data:`~rapidpipe.selftest.support.fakecrossmatchdb.SEED_ENV` /
``STATE_ENV``).

The input is an input-set manifest naming two source sets in one child
table, one exposure each, whose MJD order is the reverse of their expid
order, so a sort on the wrong column shows up in which sources become
objects. The expected objects and merges rows are named by source id and
recomputed here (``radec_index`` of the named source's position) rather
than stored as numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.science.spatial import field_neighbours, tessellation_field
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakecrossmatchdb import (
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_crossmatch_input_set,
    source_set_entry,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakecrossmatchdb:fake_database"
DATABASE_ENV = "RAPIDPIPE_CROSSMATCH_DATABASE"


def _source_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for s in spec["sources"]:
        exposure = spec["source_sets"][s["set"]]
        rows.append({"sid": s["sid"], "ra": s["ra"], "dec": s["dec"], "fluxfit": s["fluxfit"],
                     "flags": s["flags"], "field": s["field"], "expid": exposure["expid"],
                     "mjdobs": exposure["mjdobs"], "result_set": s["set"]})
    return rows


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    spec = expected["inputs"]
    inputs = work / "inputs"
    rows = _source_rows(spec)
    entries = [source_set_entry(instance, spec["table"],
                                sum(1 for r in rows if r["result_set"] == instance))
               for instance in spec["source_sets"]]
    build_crossmatch_input_set(inputs, entries)
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("crossmatch") / "settings.toml").read_text())
    seed = work / "db-seed.json"
    seed.write_text(json.dumps({
        "source_sets": {i: {"table": spec["table"], "complete": True}
                        for i in spec["source_sets"]},
        "tables": {spec["table"]: rows},
    }))
    state = work / "db-state.json"
    extra_env = {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed), STATE_ENV: str(state)}
    return inputs, overlay, extra_env


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    spec = expected["expected"]
    inputs = expected["inputs"]
    field = inputs["field"]
    by_sid = {s["sid"]: s for s in inputs["sources"]}

    # The seed itself: the fixture's fields are the tessellation's.
    checks.check(len(field_neighbours(field)) == spec["neighbours"],
                 f"field {field} has {spec['neighbours']} neighbours")
    for s in inputs["sources"]:
        checks.check(tessellation_field(s["ra"], s["dec"]) == s["field"],
                     f"source {s['sid']} field is the tessellation's")

    checks.check(manifest.inputs.products == {}, "inputs.products is empty")
    checks.check(list(manifest.inputs.result_sets) == spec["result_sets_read"],
                 f"inputs.result_sets: expected {spec['result_sets_read']}, "
                 f"got {list(manifest.inputs.result_sets)}")
    checks.check(len(manifest.outputs) == 1, f"one output, got {len(manifest.outputs)}")
    if len(manifest.outputs) != 1:
        return
    entry = manifest.outputs[0]
    checks.check(entry.kind == "association-set" and entry.is_result_set(),
                 "the output is an association-set result set (no members)")
    checks.check(is_valid_ulid(entry.instance), "association-set instance id is a ULID")
    record = json.loads((context.outputs / manifest.execution_record).read_text())
    expected_key = {"field": field, "base": None,
                    "source_sets": sorted(inputs["source_sets"]),
                    "settings_hash": record.get("settings_hash")}
    checks.check(entry.key == expected_key, f"association-set key: got {entry.key}")
    checks.check(entry.registration.get("row_counts") == spec["row_counts"],
                 f"row_counts: expected {spec['row_counts']}, "
                 f"got {entry.registration.get('row_counts')}")
    checks.check(entry.registration.get("astroobjects_table") == f"astroobjects_{field}"
                 and entry.registration.get("merges_table") == f"merges_{field}",
                 "registration names the field's two tables")
    notes = record.get("notes", {})
    checks.check(notes.get("exposures") == spec["exposures"]
                 and notes.get("neighbours") == spec["neighbours"],
                 f"execution notes exposures/neighbours: got {notes}")

    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database was committed")
        return
    state = json.loads(state_path.read_text())
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")
    checks.check(state["locks"] == [field], f"the field was locked once: {state['locks']}")
    checks.check(state["made_tables"] == spec["made_tables"],
                 f"tables made: expected {spec['made_tables']}, got {state['made_tables']}")
    checks.check(state["clustered"] == spec["clustered"],
                 f"clustered: expected {spec['clustered']}, got {state['clustered']}")

    instance = entry.instance
    run_columns = (manifest.run, manifest.attempt, instance)
    objects = state["tables"].get(f"astroobjects_{field}", [])
    merges = state["tables"].get(f"merges_{field}", [])
    checks.check(all((r["run"], r["attempt"], r["result_set"]) == run_columns
                     for r in objects + merges), "every row carries the run columns")

    def aid_of(sid: int) -> int:
        return new_object_id(by_sid[sid]["ra"], by_sid[sid]["dec"])

    expected_aids = sorted(aid_of(sid) for sid in spec["objects_from_sids"])
    checks.check(sorted(r["aid"] for r in objects) == expected_aids,
                 f"objects are sources {spec['objects_from_sids']}'s positions")
    for r in objects:
        source = next((by_sid[sid] for sid in spec["objects_from_sids"]
                       if aid_of(sid) == r["aid"]), None)
        checks.check(source is not None and r["ra0"] == source["ra"]
                     and r["dec0"] == source["dec"],
                     f"object {r['aid']} ra0/dec0 are its first source's position")
    expected_pairs = sorted((aid_of(o), s) for o, s in spec["merges"])
    checks.check(sorted((r["aid"], r["sid"]) for r in merges) == expected_pairs,
                 f"merges pairs: expected {spec['merges']} (object's source, source)")
    registered = state["association_sets"].get(instance, {})
    checks.check(registered.get("row_count") == spec["row_counts"]["merges"]
                 and registered.get("complete")
                 and (registered.get("stage"), registered.get("attempt"))
                 == ("crossmatch", manifest.attempt),
                 f"association set registered complete by this attempt: {registered}")
    checks.check(registered.get("result_sets_read") == spec["result_sets_read"],
                 "registration lists the source sets as inputs")


FIXTURE = StageFixture(
    stage="crossmatch",
    module="rapidpipe.stages.crossmatch",
    unit_kind="field",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
