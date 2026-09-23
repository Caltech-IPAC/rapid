#!/usr/bin/env python3
"""The load stage's fixture: prepare, run, check. ``make stage-load``.

Stage contract, "Local execution": "``make stage-<name>`` prepares an
isolated fixture, runs the stage without account credentials, and checks
its products and manifest; provenance fields are validated for shape, not
compared with fixed IDs or paths."

1. Prepare: a new directory (``--workdir``, else a fresh temporary one)
   gets a difference attempt's output location
   (``tests/unit/fakeloaddb.build_load_input_set``): a completion manifest
   naming one ZOGY difference instance and its SExtractor and Photutils
   catalogs, the Photutils ones written from ``expected.json``'s rows in
   `dev`'s format; the settings overlay ``settings.toml``; and the database
   seed -- the difference instance's `diffimages`/`l2files` values.
2. Run: ``python -m rapidpipe.stages.load`` as a subprocess with fresh run
   and attempt ids, the invocation Batch uses. The database is the fake
   (``RAPIDPIPE_LOAD_DATABASE=tests.unit.fakeloaddb:fake_database``), which
   records the child tables made, the COPY rows and the source set, and
   writes them to ``db-state.json`` on commit.
3. Check: the exit code; the manifest (identities, provenance shape, one
   ``source-set`` result set with its key and counts); the rows COPY
   received against ``expected.json``, in order, with exact values; and the
   spatial columns recomputed independently (healpy, and the tessellation
   looked up row by row as `dev` looks it up).

Exit 0 when every check passes, 1 otherwise. The same stage against
PostgreSQL (the child-table functions, COPY, the result-set row) is
``tests/db/test_load.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT))

import healpy as hp  # noqa: E402

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm  # noqa: E402
from rapidpipe.db.ids import is_valid_ulid, new_ulid  # noqa: E402
from rapidpipe.products.manifest import Manifest  # noqa: E402
from tests.unit.fakeloaddb import (  # noqa: E402
    DIFFERENCE_INSTANCE,
    SEED_ENV,
    STATE_ENV,
    UNIT_ID,
    build_load_input_set,
    finder_row,
    main_row,
)

FAKE_DATABASE = "tests.unit.fakeloaddb:fake_database"


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, condition: bool, label: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(label)


def _catalogs(spec: dict[str, Any]) -> dict[str, tuple[list[dict], list[dict]]]:
    out = {}
    for sign, cat in spec.items():
        main = [main_row(i, float(x), float(y), ra, dec) for i, x, y, ra, dec in cat["main"]]
        out[sign] = (main, [finder_row(i) for i in cat["finder_ids"]])
    return out


def _prepare(work: Path, expected: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    inputs = work / "inputs"
    build_load_input_set(inputs, _catalogs(expected["inputs"]["catalogs"]))
    overlay = work / "settings.toml"
    overlay.write_text((FIXTURE_DIR / "settings.toml").read_text())
    seed = work / "db-seed.json"
    seed.write_text(json.dumps({"differences": {
        DIFFERENCE_INSTANCE: expected["inputs"]["difference_row"]}}))
    return inputs, work / "outputs", overlay, seed


def _run(python: str, inputs: Path, outputs: Path, overlay: Path, seed: Path, state: Path,
         run_id: str, attempt_id: str) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env["RAPIDPIPE_LOAD_DATABASE"] = FAKE_DATABASE
    env[SEED_ENV] = str(seed)
    env[STATE_ENV] = str(state)
    argv = [python, "-m", "rapidpipe.stages.load",
            "--run", run_id, "--unit", UNIT_ID, "--attempt", attempt_id,
            "--inputs", str(inputs), "--outputs", str(outputs), "--settings", str(overlay)]
    return subprocess.run(argv, env=env, cwd=str(REPO_ROOT)).returncode


def _check_manifest(checks: Checks, outputs: Path, run_id: str, attempt_id: str,
                    spec: dict[str, Any]):
    path = outputs / "manifest.json"
    checks.check(path.exists(), "manifest.json published")
    if not path.exists():
        return None
    manifest = Manifest.read(path)
    checks.check(manifest.stage == "load", "manifest stage is 'load'")
    checks.check(manifest.run == run_id, "manifest run is the invocation's")
    checks.check(manifest.attempt == attempt_id, "manifest attempt is the invocation's")
    checks.check(manifest.unit.kind == "detector-image" and manifest.unit.id == UNIT_ID,
                 "manifest unit is the invocation's")
    record_path = outputs / manifest.execution_record
    checks.check(record_path.exists(), "execution record written")
    if record_path.exists():
        record = json.loads(record_path.read_text())
        checks.check(re.fullmatch(r"[0-9a-f]{64}", str(record.get("settings_hash"))) is not None,
                     "execution record settings_hash is a SHA-256 hex digest")
    checks.check(manifest.inputs.products.get("difference-image") == DIFFERENCE_INSTANCE,
                 "inputs.products names the difference instance")
    checks.check({"source-catalog/positive", "source-catalog/negative"}
                 <= set(manifest.inputs.products), "inputs.products names both catalogs")
    checks.check(len(manifest.outputs) == 1, f"one output, got {len(manifest.outputs)}")
    if not manifest.outputs:
        return manifest
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
    return manifest


def _check_rows(checks: Checks, state: dict[str, Any], manifest: Manifest | None,
                run_id: str, attempt_id: str, spec: dict[str, Any], tolerance: float) -> None:
    checks.check(state["made_tables"] == spec["made_tables"],
                 f"child tables made: expected {spec['made_tables']}, got {state['made_tables']}")
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")
    rows = state["tables"].get(spec["table"], [])
    checks.check([[int(r["id"]), r["isdiffpos"]] for r in rows] == spec["rows_in_order"],
                 f"rows in order: expected {spec['rows_in_order']}, "
                 f"got {[[r['id'], r['isdiffpos']] for r in rows]}")
    instance = manifest.outputs[0].instance if manifest and manifest.outputs else None
    source_set = state["source_sets"].get(instance, {})
    checks.check(source_set.get("row_count") == spec["row_count"] and source_set.get("complete"),
                 f"source set registered complete with {spec['row_count']} rows: {source_set}")
    checks.check((source_set.get("stage"), source_set.get("attempt")) == ("load", attempt_id),
                 "source set registered by this load attempt")

    tessellation = RomanTessellationClosedForm()
    for r in rows:
        label = f"row {r['id']}/{r['isdiffpos']}"
        for column, value in spec["every_row"].items():
            checks.check(r[column] == value, f"{label} {column}: expected {value}, got {r[column]}")
        checks.check((r["run"], r["attempt"], r["result_set"]) == (run_id, attempt_id, instance),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_fixture.py", description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", default=None,
                        help="an empty or new directory to prepare the fixture in "
                             "(default: a fresh temporary directory, kept for inspection)")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    expected = json.loads((FIXTURE_DIR / "expected.json").read_text())
    if args.workdir:
        work = Path(args.workdir)
        work.mkdir(parents=True, exist_ok=True)
        if any(work.iterdir()):
            print(f"stage-load: {work} is not empty", file=sys.stderr)
            return 1
    else:
        work = Path(tempfile.mkdtemp(prefix="stage-load-"))

    inputs, outputs, overlay, seed = _prepare(work, expected)
    state_path = work / "db-state.json"
    run_id, attempt_id = new_ulid(), new_ulid()
    print(f"stage-load: database=fake workdir={work}")
    code = _run(args.python, inputs, outputs, overlay, seed, state_path, run_id, attempt_id)

    spec = expected["expected"]
    checks = Checks()
    checks.check(code == spec["exit_code"], f"exit code: expected {spec['exit_code']}, got {code}")
    manifest = _check_manifest(checks, outputs, run_id, attempt_id, spec) if code == 0 else None
    if code == 0:
        checks.check(state_path.exists(), "the fake database was committed")
        if state_path.exists():
            _check_rows(checks, json.loads(state_path.read_text()), manifest, run_id, attempt_id,
                        spec, expected["tolerances"]["position_deg_abs"])

    for failure in checks.failures:
        print(f"stage-load: FAIL {failure}")
    verdict = "PASS" if not checks.failures else "FAIL"
    print(f"stage-load: {verdict} ({checks.passed} checks passed, "
          f"{len(checks.failures)} failed; outputs in {outputs})")
    return 0 if not checks.failures else 1


if __name__ == "__main__":
    sys.exit(main())
