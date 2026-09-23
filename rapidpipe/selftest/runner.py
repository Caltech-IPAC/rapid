"""Run one stage's packaged fixture and check it: shared by ``make
stage-<name>`` and ``rapidpipe selftest``.

Both the difference and load stages' fixtures share one shape: prepare a
synthetic input set and a settings overlay from ``expected.json`` and
``settings.toml`` in a fixture directory, run the stage as a subprocess
with fresh run and attempt ids (the same invocation Batch uses), and
check its exit code, manifest and products against ``expected.json``'s
values. This module holds that shape once, as :class:`FixtureRunner`, and
:mod:`rapidpipe.selftest.difference` / :mod:`rapidpipe.selftest.load`
supply each stage's own prepare/check details.

The fixture *data* (``expected.json``, ``settings.toml``) is packaged
under ``rapidpipe/selftest/fixtures/<stage>/`` so it ships inside the
pipeline image, which excludes ``tests/`` at build time
(``containers/rapid-pipeline/build.sh``). ``tests/fixtures/<stage>/`` is
the thin, non-packaged reference: its ``run_fixture.py`` (``make
stage-<name>``) reads the same packaged copy this module reads, so
nothing is duplicated between the two.

A stage's own products (``difference-image``, ``source-set``, ...) are
checked only when ``--output-location`` is a local path: that is the only
case this module can read the manifest and its members back to verify
them. Against an S3 ``--output-location`` the stage publishes for real
(``rapidpipe.products.storage``, the same as any Batch job) but this
module cannot cheaply re-read S3 objects without a network round trip and
credentials no other part of ``selftest`` needs, so the check there
reduces to the stage's own exit code -- documented in the printed report,
not silently assumed.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rapidpipe.db.ids import is_valid_ulid, new_ulid
from rapidpipe.products.manifest import Manifest, hash_file
from rapidpipe.products.storage import parse_location

#: The two stages a fixture exists for today (stage contract, "Local
#: execution"; ``tests/fixtures/<stage>/``).
STAGE_NAMES = ("difference", "load")

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"


class Checks:
    """Records pass/fail labels; ``run_fixture.py``'s own collector, shared."""

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


@dataclass
class FixtureResult:
    """What one fixture run produced, for the caller to report or check further."""

    stage: str
    tools: str
    exit_code: int
    checks: Checks
    work_dir: Path
    output_location: str
    output_is_local: bool
    manifest: Manifest | None = None


@dataclass
class CheckContext:
    """Everything a stage's ``check`` hook needs beyond the manifest.

    ``work_dir`` is the fixture's own prepared directory (inputs, the
    settings overlay, and any stage-specific side files a ``prepare`` hook
    wrote there, such as load's ``db-state.json``) -- not the same as
    ``outputs`` when ``--output-location`` overrides where the stage
    itself publishes.
    """

    inputs: Path
    outputs: Path
    work_dir: Path
    tools: str


@dataclass
class StageFixture:
    """One stage's fixture-specific prepare/run/check hooks.

    ``prepare`` writes the synthetic input set and settings overlay into
    ``work_dir`` and returns ``(inputs, overlay, module_env)``: the
    ``--inputs`` argument, the ``--settings`` argument, and any extra
    environment variables the subprocess needs (the fake toolkit/database
    hook, seed/state file paths). ``module`` is the stage's own module
    path (``rapidpipe.stages.difference``). ``check`` runs the
    stage-specific product checks against a successful, locally-readable
    run; it is skipped when the output location is not local.
    """

    stage: str
    module: str
    unit_kind: str
    unit_id: str
    fake_toolkit_env: dict[str, str]
    prepare: Callable[[Path, dict[str, Any], bool], tuple[Path, Path, dict[str, str]]]
    check: Callable[[Checks, Manifest, dict[str, Any], CheckContext], None]
    #: The key into ``expected.json`` holding this stage's exit code and
    #: product spec. Difference's fixture has one per tool set (``fake``/
    #: ``real``, looked up by ``tools``); load's fixture -- no tools to
    #: fake, only a database -- has one fixed key, ``"expected"``.
    spec_key: Callable[[str], str] = lambda tools: tools


def fixture_dir(stage: str) -> Path:
    if stage not in STAGE_NAMES:
        raise ValueError(f"no such stage fixture: {stage!r}; known: {', '.join(STAGE_NAMES)}")
    return FIXTURES_ROOT / stage


def load_expected(stage: str) -> dict[str, Any]:
    return json.loads((fixture_dir(stage) / "expected.json").read_text())


def _check_manifest_shape(checks: Checks, outputs: Path, run_id: str, attempt_id: str,
                          stage: str, unit_kind: str, unit_id: str) -> Manifest | None:
    path = outputs / "manifest.json"
    checks.check(path.exists(), "manifest.json published")
    if not path.exists():
        return None
    manifest = Manifest.read(path)
    checks.check(manifest.stage == stage, f"manifest stage is {stage!r}")
    checks.check(manifest.run == run_id, "manifest run is the invocation's")
    checks.check(manifest.attempt == attempt_id, "manifest attempt is the invocation's")
    checks.check(manifest.unit.kind == unit_kind and manifest.unit.id == unit_id,
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
    return manifest


def run_stage_subprocess(python: str, module: str, inputs: Path, outputs_location: str,
                          overlay: Path, run_id: str, attempt_id: str, unit_id: str,
                          extra_env: dict[str, str], repo_root: Path) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), env.get("PYTHONPATH")]))
    env.update(extra_env)
    argv = [python, "-m", module,
            "--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
            "--inputs", str(inputs), "--outputs", outputs_location, "--settings", str(overlay)]
    return subprocess.run(argv, env=env, cwd=str(repo_root)).returncode


def run_fixture(fx: StageFixture, *, tools: str, python: str, repo_root: Path,
                work_dir: Path | None, output_location: str | None) -> FixtureResult:
    """Prepare, run, and (when local) check one stage's packaged fixture.

    ``tools`` is ``"fake"`` (the default; every external tool replaced by
    the packaged stand-ins) or ``"real"`` (the pipeline image's own
    tools). ``work_dir`` holds the prepared inputs and, when
    ``output_location`` is not given, the outputs too; it is created if
    missing and must be empty. ``output_location`` overrides where the
    stage publishes (``--outputs``): a local path or an ``s3://`` prefix;
    defaults to ``<work_dir>/outputs``.
    """
    expected = load_expected(fx.stage)
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix=f"selftest-{fx.stage}-"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        if any(work_dir.iterdir()):
            raise FileExistsError(f"--work-dir {work_dir} is not empty")

    inputs, overlay, extra_env = fx.prepare(work_dir, expected, tools == "fake")
    if tools == "fake":
        extra_env = {**fx.fake_toolkit_env, **extra_env}

    output_is_local = True
    if output_location is None:
        outputs_path = work_dir / "outputs"
        output_location_str = str(outputs_path)
    else:
        output_location_str = output_location
        location = parse_location(output_location)
        output_is_local = not location.is_s3()
        outputs_path = location.path if output_is_local else None

    run_id, attempt_id = new_ulid(), new_ulid()
    exit_code = run_stage_subprocess(
        python, fx.module, inputs, output_location_str, overlay, run_id, attempt_id,
        fx.unit_id, extra_env, repo_root)

    spec = expected[fx.spec_key(tools)]
    checks = Checks()
    checks.check(exit_code == spec["exit_code"],
                 f"exit code: expected {spec['exit_code']}, got {exit_code}")

    manifest = None
    if exit_code == 0 and output_is_local:
        manifest = _check_manifest_shape(
            checks, outputs_path, run_id, attempt_id, fx.stage, fx.unit_kind, fx.unit_id)
        if manifest is not None:
            context = CheckContext(inputs=inputs, outputs=outputs_path, work_dir=work_dir, tools=tools)
            fx.check(checks, manifest, expected, context)
    elif exit_code == 0:
        # Not a check -- see module docstring: S3 output isn't re-read to
        # verify products, so all that's known is that the stage exited 0.
        print(f"selftest: {fx.stage}: output published to {output_location_str} "
              "(S3); product checks skipped -- only local output locations are "
              "re-read for verification", file=sys.stderr)

    return FixtureResult(
        stage=fx.stage, tools=tools, exit_code=exit_code, checks=checks, work_dir=work_dir,
        output_location=output_location_str, output_is_local=output_is_local, manifest=manifest)
