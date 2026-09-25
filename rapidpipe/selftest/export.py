"""The export stage's :class:`rapidpipe.selftest.runner.StageFixture`.

``export`` is a declared stub (supervisor step 8, 2026-09-24, ruling R9):
``prepare`` writes a synthetic, structurally valid input-set manifest
(:func:`rapidpipe.selftest.support.fakeexport.build_export_input_set`,
naming a source-set, association-set and statistics-set instance -- no
member files, since a database result set has none) and the settings
overlay; the fixture's ``expected.json`` declares ``exit_code`` 69, so
:func:`rapidpipe.selftest.runner.run_fixture` checks only that the stage
exits 69 and publishes no manifest -- ``check`` here is never called.

The stage runs no external tool and opens no database connection in this
build, so the fixture is identical with ``--real-tools`` and without (one
``expected`` spec, as ``finalize``'s).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakeexport import UNIT_ID, build_export_input_set


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    build_export_input_set(inputs, **expected.get("inputs", {}))
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("export") / "settings.toml").read_text())
    return inputs, overlay, {}


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
          context: CheckContext) -> None:
    # Never reached: exit_code is 69, so run_fixture checks "no manifest
    # published" itself and does not call this hook.
    checks.check(False, "export fixture's check hook should not run")


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
