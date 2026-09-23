#!/usr/bin/env python3
"""The difference stage's fixture: prepare, run, check. ``make stage-difference``.

Stage contract, "Local execution": "``make stage-<name>`` prepares an
isolated fixture, runs the stage without account credentials, and checks
its products and manifest; provenance fields are validated for shape, not
compared with fixed IDs or paths."

1. Prepare: a new directory (``--workdir``, else a fresh temporary one)
   gets the synthetic input set (``tests/unit/fakedifftools.build_input_set``,
   with the parameters in ``expected.json``'s ``inputs``) and a settings
   overlay (``settings.toml`` here, plus the repository's own ``cdf``
   directory as ``[paths] cfg_path`` when the tools are faked).
2. Run: ``python -m rapidpipe.stages.difference`` as a subprocess, with
   fresh run and attempt ids -- the same invocation Batch uses. With
   ``--tools fake`` (the default) ``RAPIDPIPE_DIFFERENCE_TOOLKIT`` selects
   the fakes; with ``--tools real`` the stage runs SExtractor, SWarp,
   bkgest, ZOGY, SFFT, photutils and the SIP-to-PV converter for real,
   which needs the pipeline image.
3. Check: the exit code; the manifest (schema, identities, provenance
   shape, every member's size and SHA-256 against the file on disk, each
   entry against the products page's rules); and ``expected.json``'s
   values for the chosen tools, within the tolerances documented there.

Exit 0 when every check passes, 1 otherwise. The database is not touched:
the stage declares no database access, so the fixture needs no seed data
(``register``'s handling of this manifest is tested in
``tests/db/test_register_difference.py``).
"""

from __future__ import annotations

import argparse
import json
import math
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

import numpy as np  # noqa: E402
from astropy.io import fits  # noqa: E402

from rapidpipe.db.ids import is_valid_ulid, new_ulid  # noqa: E402
from rapidpipe.products.diffimage import (  # noqa: E402
    validate_difference_entry,
    validate_source_catalog_entry,
)
from rapidpipe.products.manifest import Manifest, hash_file  # noqa: E402
from tests.unit.fakedifftools import build_input_set  # noqa: E402

FAKE_TOOLKIT = "tests.unit.fakedifftools:fake_toolkit"
UNIT_ID = "e20260821001234/SCA07"


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, condition: bool, label: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(label)

    def close(self, expected: float, actual: float, *, rel: float = 0.0, abs_: float = 0.0,
              label: str) -> None:
        ok = actual is not None and math.isclose(actual, expected, rel_tol=rel, abs_tol=abs_)
        self.check(ok, f"{label}: expected {expected} (rel {rel}, abs {abs_}), got {actual}")


def _prepare(work: Path, expected: dict[str, Any], tools: str) -> tuple[Path, Path, Path]:
    inputs = work / "inputs"
    build_input_set(inputs, **expected["inputs"])
    overlay = work / "settings.toml"
    text = (FIXTURE_DIR / "settings.toml").read_text()
    if tools == "fake":
        text += f'\n[paths]\ncfg_path = "{REPO_ROOT / "cdf"}"\n'
    overlay.write_text(text)
    return inputs, work / "outputs", overlay


def _run(python: str, inputs: Path, outputs: Path, overlay: Path, tools: str,
         run_id: str, attempt_id: str) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    if tools == "fake":
        env["RAPIDPIPE_DIFFERENCE_TOOLKIT"] = FAKE_TOOLKIT
    else:
        env.pop("RAPIDPIPE_DIFFERENCE_TOOLKIT", None)
    argv = [python, "-m", "rapidpipe.stages.difference",
            "--run", run_id, "--unit", UNIT_ID, "--attempt", attempt_id,
            "--inputs", str(inputs), "--outputs", str(outputs), "--settings", str(overlay)]
    return subprocess.run(argv, env=env, cwd=str(REPO_ROOT)).returncode


def _check_manifest(checks: Checks, outputs: Path, run_id: str, attempt_id: str) -> Manifest | None:
    path = outputs / "manifest.json"
    checks.check(path.exists(), "manifest.json published")
    if not path.exists():
        return None
    manifest = Manifest.read(path)
    checks.check(manifest.stage == "difference", "manifest stage is 'difference'")
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
        checks.check("source_revision" in record and "image_digest" in record,
                     "execution record carries source_revision and image_digest")
    for entry in manifest.outputs:
        checks.check(is_valid_ulid(entry.instance), f"{entry.kind} instance id is a ULID")
        for member in entry.members:
            file_path = outputs / member.path
            if not file_path.exists():
                checks.check(False, f"member {member.path} exists")
                continue
            size, sha = hash_file(file_path)
            checks.check(size == member.bytes and f"sha256:{sha}" == member.sha256,
                         f"member {member.path} size and SHA-256 match the file")
        try:
            if entry.kind == "difference-image":
                validate_difference_entry(entry.to_dict())
            elif entry.kind == "source-catalog":
                validate_source_catalog_entry(entry.to_dict())
            checks.check(True, "")
        except ValueError as exc:
            checks.check(False, f"{entry.kind} {entry.instance}: {exc}")
    return manifest


def _check_count(checks: Checks, spec, actual: int, label: str) -> None:
    if isinstance(spec, list):
        checks.check(spec[0] <= actual <= spec[1], f"{label}: expected in {spec}, got {actual}")
    else:
        checks.check(actual == spec, f"{label}: expected {spec}, got {actual}")


def _check_expected(checks: Checks, manifest: Manifest, outputs: Path, spec: dict[str, Any],
                    l2_registration: dict[str, Any], tolerances: dict[str, float]) -> None:
    for kind, count in spec["entries"].items():
        _check_count(checks, count, sum(1 for e in manifest.outputs if e.kind == kind),
                     f"{kind} entries")
    diffs = [e for e in manifest.outputs if e.kind == "difference-image"]
    if not diffs:
        return
    entry = diffs[0]
    d = spec["difference_image"]
    checks.check(entry.key["differencer"] == d["differencer"], "differencer")
    checks.check(sorted(m.role for m in entry.members) == sorted(d["roles"]), "bundle roles")
    registration = entry.registration

    position_tol = tolerances["position_deg_abs"]
    checks.close(l2_registration["centre"]["ra"], registration["centre"]["ra"],
                 abs_=position_tol, label="centre ra = the l2 instance's")
    checks.close(l2_registration["centre"]["dec"], registration["centre"]["dec"],
                 abs_=position_tol, label="centre dec = the l2 instance's")
    for i, (ra, dec) in enumerate(l2_registration["corners"]):
        checks.close(ra, registration["corners"][i][0], abs_=position_tol, label=f"corner {i} ra")
        checks.close(dec, registration["corners"][i][1], abs_=position_tol, label=f"corner {i} dec")

    for field, value in d.get("registration_exact", {}).items():
        checks.check(registration[field] == value,
                     f"registration {field}: expected {value!r}, got {registration[field]!r}")
    for field, value in d.get("registration_close", {}).items():
        checks.close(value, registration[field], rel=tolerances["scalar_rel"],
                     abs_=tolerances["scalar_abs"], label=f"registration {field}")
    for key, value in d.get("residual_close", {}).items():
        checks.close(value, registration["registration_residual"][key],
                     abs_=tolerances["residual_pixels_abs"], label=f"registration_residual {key}")

    pixels = d.get("pixels")
    if pixels:
        member = next(m for m in entry.members if m.role == pixels["member"])
        data = np.array(fits.getdata(outputs / member.path), dtype=np.float64)
        checks.check(list(data.shape) == pixels["shape"], f"{pixels['member']} shape")
        checks.check(int(np.isnan(data).sum()) == pixels["nan_count"],
                     f"{pixels['member']} NaN count: expected {pixels['nan_count']}, "
                     f"got {int(np.isnan(data).sum())}")
        finite = data[np.isfinite(data)]
        for stat, value in pixels.get("finite", {}).items():
            actual = float(getattr(np, stat)(finite))
            checks.close(value, actual, rel=tolerances["pixel_stat_rel"],
                         abs_=tolerances["pixel_stat_abs"], label=f"{pixels['member']} {stat}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_fixture.py", description=__doc__.splitlines()[0])
    parser.add_argument("--tools", choices=("fake", "real"), default="fake")
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
            print(f"stage-difference: {work} is not empty", file=sys.stderr)
            return 1
    else:
        work = Path(tempfile.mkdtemp(prefix="stage-difference-"))

    inputs, outputs, overlay = _prepare(work, expected, args.tools)
    run_id, attempt_id = new_ulid(), new_ulid()
    print(f"stage-difference: tools={args.tools} workdir={work}")
    code = _run(args.python, inputs, outputs, overlay, args.tools, run_id, attempt_id)

    spec = expected[args.tools]
    checks = Checks()
    checks.check(code == spec["exit_code"], f"exit code: expected {spec['exit_code']}, got {code}")
    manifest = _check_manifest(checks, outputs, run_id, attempt_id) if code == 0 else None
    if manifest is not None:
        l2_registration = json.loads((inputs / "manifest.json").read_text())["outputs"][0]["registration"]
        _check_expected(checks, manifest, outputs, spec, l2_registration, expected["tolerances"])
        if "execution_notes" in spec:
            record = json.loads((outputs / manifest.execution_record).read_text())
            checks.check(record.get("notes") == spec["execution_notes"],
                         f"execution notes: expected {spec['execution_notes']}, "
                         f"got {record.get('notes')}")

    for failure in checks.failures:
        print(f"stage-difference: FAIL {failure}")
    verdict = "PASS" if not checks.failures else "FAIL"
    print(f"stage-difference: {verdict} ({checks.passed} checks passed, "
          f"{len(checks.failures)} failed; tools={args.tools}; outputs in {outputs})")
    return 0 if not checks.failures else 1


if __name__ == "__main__":
    sys.exit(main())
