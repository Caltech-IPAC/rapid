"""Run one stage locally as a subprocess, over ``rapidpipe.runs.repository``.

Implements the runs page's "Identifiers" ("the local runner and the Batch
wrapper need no database to allocate" attempt ids -- ``allocate_attempt``
already does that) and "Storage layout" (``runs/<run-id>/<stage>/<unit-id>/
<attempt-id>/``) over a plain local directory tree, and the stage
contract's "Invocation" form for the subprocess command line.

This module may import ``rapidpipe.products`` and ``rapidpipe.db``, never
a stage module, ``rapidpipe.launch`` or ``rapidpipe.cli`` (the package's
fixed dependency direction; see ``tests/unit/test_dependency_direction.py``).
It never imports ``rapidpipe.stages.contract`` or any stage's
``DECLARATION`` -- the caller (``rapidpipe.cli``, which may import
anything) resolves the stage name to a module and passes down only the
plain strings this module needs: the stage name and the unit kind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from rapidpipe.products.manifest import Manifest, ManifestError
from rapidpipe.runs.repository import (
    add_unit,
    allocate_attempt,
    attempt_output_location,
    record_attempt_result,
    select_attempt,
)

#: Exit codes the stage contract assigns a disposition without needing the
#: exit code table imported from ``rapidpipe.stages.contract`` -- that
#: module lives in the forbidden ``stages`` subpackage from here, so the
#: three codes this function cares about are repeated as plain ints, with
#: the contract section they come from named in the docstring below.
_TRANSIENT_FAILURE_CODE = 75


@dataclass(frozen=True)
class LocalAttempt:
    """The outcome of one local stage execution.

    ``manifest_path`` is the path to the attempt's ``manifest.json`` when
    the disposition is ``succeeded``, else ``None``. ``selected`` is true
    only when this attempt's result was accepted as its unit's selection.
    """

    attempt_id: str
    output_location: str
    exit_code: int
    disposition: str
    manifest_path: Path | None
    selected: bool


def disposition_for(exit_code: int, manifest_ok: bool) -> str:
    """Map a stage's exit code and manifest validity to a disposition.

    Pure and unit-testable without a database. Rules (runs page,
    "Attempts": "Success requires a valid completion manifest and complete
    declared outputs; exit zero alone is not success"; stage contract,
    "Exit codes"):

    - exit 0 with a valid manifest: ``succeeded``.
    - exit 0 without a valid manifest: ``failed`` -- exit zero alone is
      not success.
    - a negative exit code (the subprocess was killed by a signal, per
      ``subprocess.Popen.returncode``'s convention): ``killed``.
    - exit 75 (``TRANSIENT_FAILURE``): ``transient``.
    - exit 64, 65, 70, or any other non-negative code: ``failed``.
    """
    if exit_code < 0:
        return "killed"
    if exit_code == 0:
        return "succeeded" if manifest_ok else "failed"
    if exit_code == _TRANSIENT_FAILURE_CODE:
        return "transient"
    return "failed"


def _read_manifest_if_valid(output_location: Path) -> Manifest | None:
    manifest_path = output_location / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return Manifest.read(manifest_path)
    except (ManifestError, OSError, ValueError):
        return None


def _read_execution_record(output_location: Path, attempt_id: str) -> dict[str, Any]:
    record_path = output_location / "exec" / f"{attempt_id}.json"
    if not record_path.exists():
        return {}
    try:
        return json.loads(record_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _source_revision_or_unknown() -> str:
    """``git rev-parse HEAD`` in the current working directory, else the
    literal ``"unknown"``.

    Used only to fill ``execution_records.source_revision`` (NOT NULL) for
    an attempt whose stage never reached ``run_stage``'s own
    ``exec/<attempt>.json`` write -- a usage or input-rejected failure
    raises before that point (``rapidpipe.stages.contract.run_stage``), so
    there is no stage-written execution record to read at all for that
    attempt. A stage that DID write one already has a real source
    revision in it (``rapidpipe.stages.contract._source_revision``, the
    same git lookup), so this fallback only ever fires for a record this
    module itself has to construct from nothing.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    revision = result.stdout.strip()
    return revision or "unknown"


def _run_schema_version(conn, run_id: str) -> str | None:
    """The run's own recorded ``schema_version`` (``runs.schema_version``).

    The stage contract's own execution record (``exec/<attempt>.json``,
    written by ``rapidpipe.stages.contract._write_execution_record``)
    deliberately omits schema version: "Schema version and working-copy
    changes are not recorded here: they belong to ``rapidpipe.runs``,
    which this module must not import." ``rapidpipe.runs.repository.
    execution_records.schema_version`` is NOT NULL, so this function's
    result fills that gap before ``record_attempt_result`` is called.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT schema_version FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return row[0] if row else None


def run_stage_locally(
    conn,
    *,
    run_id: str,
    stage: str,
    unit_kind: str,
    unit_id: str,
    inputs: str,
    outputs_root: str,
    settings: str | None = None,
    python: str = sys.executable,
    env: Mapping[str, str] | None = None,
) -> LocalAttempt:
    """Run one stage attempt as a subprocess against a real database.

    Steps, each its own short transaction on ``conn`` (the repository
    functions open no transaction of their own; this function commits
    after each one, per ``rapidpipe.runs.repository``'s module docstring)
    -- except steps 6 and 7, which share one commit so a success is never
    recorded without also being selected:

    1. :func:`~rapidpipe.runs.repository.add_unit` -- a no-op if the unit
       already exists (its own ``ON CONFLICT DO NOTHING``).
    2. :func:`~rapidpipe.runs.repository.allocate_attempt` -- enforces the
       run fence and the attempt allowance; its exceptions propagate
       uncommitted (the caller's transaction, if any wraps this call, sees
       nothing partial).
    3. An exclusive output directory at ``<outputs_root>/runs/<run_id>/
       <stage>/<unit_id>/<attempt_id>`` (runs page, "Storage layout"; a
       ``/`` inside ``unit_id`` such as ``e20260821001234/SCA07`` simply
       nests one more level, exactly as a plain filesystem path). Refuses
       if it already exists, since the location must be exclusive to this
       attempt.
    4. Runs the stage as a subprocess, per the stage contract's
       "Invocation" form, with ``os.environ`` overlaid by ``env``.
       Nothing is captured -- the stage's own logging reaches the
       terminal directly.
    5. :func:`disposition_for` decides the outcome from the exit code and
       whether a valid manifest was published.
    6. Reads ``exec/<attempt_id>.json`` if present (else ``{}``), fills in
       whatever ``execution_records`` requires but the stage's own record
       omits (schema version always; source revision and settings hash
       only when the stage never wrote a record at all), and calls
       :func:`~rapidpipe.runs.repository.record_attempt_result`.
    7. If ``succeeded``, calls
       :func:`~rapidpipe.runs.repository.select_attempt`.

    Raises whatever ``add_unit`` or ``allocate_attempt`` raises (a
    :class:`~rapidpipe.runs.repository.RunModelError` subclass) before any
    subprocess runs. Never swallows a subprocess launch failure (e.g. the
    interpreter not found): that propagates as an ``OSError``.
    """
    add_unit(conn, run_id, stage, unit_kind, unit_id)
    conn.commit()

    attempt_id = allocate_attempt(conn, run_id, stage, unit_id, outputs_root=str(outputs_root))
    conn.commit()

    output_location = Path(attempt_output_location(
        str(outputs_root), run_id, stage, unit_id, attempt_id))
    if output_location.exists():
        raise FileExistsError(
            f"output location already exists, but must be exclusive to "
            f"attempt {attempt_id!r}: {output_location}")
    output_location.mkdir(parents=True)

    argv = [
        python, "-m", f"rapidpipe.stages.{stage}",
        "--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
        "--inputs", inputs, "--outputs", str(output_location),
    ]
    if settings is not None:
        argv += ["--settings", settings]

    subprocess_env = dict(os.environ)
    if env:
        subprocess_env.update(env)

    result = subprocess.run(argv, env=subprocess_env)
    exit_code = result.returncode

    manifest = _read_manifest_if_valid(output_location)
    disposition = disposition_for(exit_code, manifest is not None)
    manifest_path = (output_location / "manifest.json") if manifest is not None else None

    execution_record = _read_execution_record(output_location, attempt_id)
    # execution_records has NOT NULL columns for source_revision,
    # schema_version and settings_hash. A stage that raised before
    # run_stage wrote exec/<attempt>.json (a usage or input-rejected
    # failure, stage contract "Invocation") leaves execution_record empty,
    # so this module supplies its own fallbacks rather than let the insert
    # violate those constraints. A record the stage DID write can still
    # hold explicit null for source_revision (git unavailable) or, in
    # principle, the other two fields, so a plain setdefault (which does
    # nothing when the key is present with None) is not enough -- check
    # the value, not just the key's presence.
    if execution_record.get("schema_version") is None:
        execution_record["schema_version"] = _run_schema_version(conn, run_id)
    if execution_record.get("source_revision") is None:
        execution_record["source_revision"] = _source_revision_or_unknown()
    if execution_record.get("settings_hash") is None:
        execution_record["settings_hash"] = "unknown"
    # record_attempt_result and, when it succeeded, select_attempt run in
    # the same transaction as one commit -- not two -- so a process death
    # between them cannot leave the attempt 'succeeded' with its unit
    # neither selected nor terminal (rapidpipe.launch.batch.reconcile has
    # the same fix, and a repair pass for exactly this kind of pre-
    # existing damage; supervisor step 3, 2026-09-24, WP-F).
    record_attempt_result(
        conn, attempt_id, exit_code, disposition, str(output_location),
        execution_record, scheduler_job_id=None)

    selected = False
    if disposition == "succeeded":
        select_attempt(conn, attempt_id)
        selected = True
    conn.commit()

    return LocalAttempt(
        attempt_id=attempt_id,
        output_location=str(output_location),
        exit_code=exit_code,
        disposition=disposition,
        manifest_path=manifest_path,
        selected=selected,
    )
