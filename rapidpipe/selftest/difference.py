"""The difference stage's :class:`rapidpipe.selftest.runner.StageFixture`.

Prepare/check logic ported from ``tests/fixtures/difference/run_fixture.py``
(the pre-existing ``make stage-difference`` fixture), unchanged in
substance -- this module supplies the same two hooks, reading the
packaged fixture data instead of a copy local to the test tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from rapidpipe.products.diffimage import validate_difference_entry, validate_source_catalog_entry
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakedifftools import build_input_set, cdf_dir

FAKE_TOOLKIT = "rapidpipe.selftest.support.fakedifftools:fake_toolkit"
TOOLKIT_ENV = "RAPIDPIPE_DIFFERENCE_TOOLKIT"
UNIT_ID = "e20260821001234/SCA07"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    build_input_set(inputs, **expected["inputs"])
    overlay = work / "settings.toml"
    text = (fixture_dir("difference") / "settings.toml").read_text()
    if fake:
        # cdf_dir() resolves the same directory build_input_set() itself
        # used for the reference catalog's SExtractor parameter file --
        # a checkout's own cdf/, $RAPID_CFG, or the image's /code/cdf
        # (see that function's docstring). Both must agree, or the fixture
        # and the stage it drives would read different parameter files.
        text += f'\n[paths]\ncfg_path = "{cdf_dir()}"\n'
    overlay.write_text(text)
    return inputs, overlay, {}


def _check_count(checks: Checks, spec, actual: int, label: str) -> None:
    if isinstance(spec, list):
        checks.check(spec[0] <= actual <= spec[1], f"{label}: expected in {spec}, got {actual}")
    else:
        checks.check(actual == spec, f"{label}: expected {spec}, got {actual}")


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    outputs, inputs = context.outputs, context.inputs
    spec = expected[context.tools]
    tolerances = expected["tolerances"]
    l2_registration = (
        json.loads((inputs / "manifest.json").read_text())["outputs"][0]["registration"])

    for entry in manifest.outputs:
        try:
            if entry.kind == "difference-image":
                validate_difference_entry(entry.to_dict())
            elif entry.kind == "source-catalog":
                validate_source_catalog_entry(entry.to_dict())
        except ValueError as exc:
            checks.check(False, f"{entry.kind} {entry.instance}: {exc}")
            continue
        checks.check(True, "")

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
    for catalog_type, counts in d.get("source_counts_close", {}).items():
        for sign, value in counts.items():
            checks.close_count(
                value, registration["source_counts"][catalog_type][sign],
                abs_tol=tolerances["catalog_count_abs"], rel_tol=tolerances["catalog_count_rel"],
                label=f"source_counts {catalog_type} {sign}")

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

    if "execution_notes" in spec:
        record = json.loads((outputs / manifest.execution_record).read_text())
        checks.check(record.get("notes") == spec["execution_notes"],
                     f"execution notes: expected {spec['execution_notes']}, "
                     f"got {record.get('notes')}")


FIXTURE = StageFixture(
    stage="difference",
    module="rapidpipe.stages.difference",
    unit_kind="detector-image",
    unit_id=UNIT_ID,
    fake_toolkit_env={TOOLKIT_ENV: FAKE_TOOLKIT},
    prepare=_prepare,
    check=_check,
)
