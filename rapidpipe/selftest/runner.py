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
always checked against a local copy of the outputs, whatever
``--output-location`` names. When it is an ``s3://`` prefix, the stage is
still run with a local ``--outputs`` directory (under ``work_dir``) so
this module can read the manifest and its members back the same way a
local run would; only once every check has run against that local copy is
the directory uploaded to the requested S3 location with
:func:`rapidpipe.products.storage.publish_dir` -- the same upload path
``rapidpipe.stages.contract`` itself uses for a real S3 ``--outputs``, so
the object layout and manifest-last upload order are identical to what a
Batch job produces. This means the check count is the same locally and on
Batch (``FixtureResult.checks``), never silently reduced to just the exit
code -- and the printed report says where the outputs ended up.
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
from rapidpipe.products.storage import parse_location, publish_dir

#: The stages a fixture exists for today (stage contract, "Local
#: execution"; ``tests/fixtures/<stage>/``).
STAGE_NAMES = ("difference", "load", "maintain", "crossmatch")

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

    def close_count(self, expected: int | None, actual: int | None, *, abs_tol: int,
                    rel_tol: float, label: str) -> None:
        """A catalog row count, allowed to drift by up to ``max(abs_tol,
        rel_tol * expected)`` either way -- SExtractor and photutils give a
        few more or fewer sources run to run on identical pixels. ``None``
        means no detections (a catalog the real tools skipped), checked for
        equality since there is no meaningful tolerance around "none".
        """
        if expected is None or actual is None:
            self.check(expected == actual, f"{label}: expected {expected}, got {actual}")
            return
        tol = max(abs_tol, rel_tol * expected)
        self.check(abs(actual - expected) <= tol,
                   f"{label}: expected {expected} (+/- {tol:.3g}), got {actual}")


@dataclass
class FixtureResult:
    """What one fixture run produced, for the caller to report or check further.

    ``output_location`` is always where the checks in ``checks`` ran
    against -- a local directory, whether or not ``--output-location``
    named one (see :func:`run_fixture`). ``uploaded_to`` is set only when
    ``--output-location`` named an ``s3://`` prefix: the local outputs
    were, after every check passed or failed, uploaded there too.
    """

    stage: str
    tools: str
    exit_code: int
    checks: Checks
    work_dir: Path
    output_location: str
    output_is_local: bool
    manifest: Manifest | None = None
    uploaded_to: str | None = None


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
    """Prepare, run, and check one stage's packaged fixture.

    ``tools`` is ``"fake"`` (the default; every external tool replaced by
    the packaged stand-ins) or ``"real"`` (the pipeline image's own
    tools). ``work_dir`` holds the prepared inputs and the outputs; it is
    created if missing and must be empty.

    The stage is always run with a *local* ``--outputs`` directory
    (``work_dir / "outputs"``), so its manifest and products can be read
    back and checked the same way regardless of ``output_location`` --
    this is what makes the check count identical locally and on Batch.
    When ``output_location`` names an ``s3://`` prefix, the local outputs
    are uploaded there with :func:`~rapidpipe.products.storage.publish_dir`
    after every check has run (so a check failure is still reported, and
    still uploaded for inspection). When it names a local path instead,
    that path *is* the outputs directory (no separate upload: the runner
    prepares the stage's outputs directly there, matching
    ``rapidpipe.products.storage.publish_dir``'s own same-directory no-op
    for a local destination). ``output_location`` defaults to
    ``<work_dir>/outputs`` when not given.
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

    upload_location: str | None = None
    if output_location is None:
        outputs_path = work_dir / "outputs"
    else:
        location = parse_location(output_location)
        if location.is_s3():
            upload_location = output_location
            outputs_path = work_dir / "outputs"
        else:
            assert location.path is not None
            outputs_path = location.path
    local_output_location_str = str(outputs_path)

    run_id, attempt_id = new_ulid(), new_ulid()
    exit_code = run_stage_subprocess(
        python, fx.module, inputs, local_output_location_str, overlay, run_id, attempt_id,
        fx.unit_id, extra_env, repo_root)

    spec = expected[fx.spec_key(tools)]
    checks = Checks()
    checks.check(exit_code == spec["exit_code"],
                 f"exit code: expected {spec['exit_code']}, got {exit_code}")

    manifest = None
    if exit_code == 0:
        manifest = _check_manifest_shape(
            checks, outputs_path, run_id, attempt_id, fx.stage, fx.unit_kind, fx.unit_id)
        if manifest is not None:
            context = CheckContext(inputs=inputs, outputs=outputs_path, work_dir=work_dir, tools=tools)
            fx.check(checks, manifest, expected, context)

    if upload_location is not None and outputs_path.exists():
        upload_target = parse_location(upload_location)
        publish_dir(outputs_path, upload_target)
        print(f"selftest: {fx.stage}: outputs uploaded to {upload_location}", file=sys.stderr)

    return FixtureResult(
        stage=fx.stage, tools=tools, exit_code=exit_code, checks=checks, work_dir=work_dir,
        output_location=local_output_location_str, output_is_local=True, manifest=manifest,
        uploaded_to=upload_location)
