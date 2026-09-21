"""The stage contract as code: declaration, exit codes, and the runner.

Every stage exports a :class:`StageDeclaration` and calls :func:`run_stage`
from its ``main(argv)``. This module implements the contract's "Invocation",
"Exit codes" and "Settings" sections; ``rapidpipe.stages.settings`` supplies
the TOML load and merge that "Settings" describes. The manifest ``run_stage``
publishes follows the products page's shape
(``rapidpipe.products.manifest.Manifest``); a stage's ``body`` supplies only
what it alone knows -- its output entries and any result sets it read -- and
``run_stage`` wraps that into the enclosing manifest with the run, unit,
stage and attempt identifiers from argv, an execution record it writes
itself, and the input manifest location from ``--inputs``.

This module may import ``rapidpipe.products`` and ``rapidpipe.stages.settings``,
but never another stage module, ``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Sequence

from rapidpipe.log import stage_log_context
from rapidpipe.products.manifest import (
    Inputs,
    Manifest,
    ManifestError,
    OutputEntry,
    Unit,
)
from rapidpipe.stages.settings import SettingsError, canonical_hash, resolve_settings

#: The stable list of stage names (stage contract, "Declaration"). A
#: StageDeclaration naming anything else is rejected by validate().
STAGE_NAMES = (
    "admit",
    "reference",
    "difference",
    "finalize",
    "register",
    "load",
    "crossmatch",
    "statistics",
    "prune",
    "alerts",
    "photometry",
    "export",
)

#: The four units of work a stage may declare (stage contract, "Declaration").
UNIT_KINDS = ("exposure", "detector-image", "field", "processing-date")

#: The three database access levels a stage may declare.
DB_ACCESS_LEVELS = ("none", "read", "read-write")


class ExitCode(IntEnum):
    """The stage contract's five exit codes and the caller's action.

    Values and meanings are fixed by the contract's "Exit codes" table;
    do not renumber or add to this list without a contract change.
    """

    SUCCESS = 0
    """Success, manifest published. Caller's action: none."""

    USAGE = 64
    """Bad arguments, invalid settings, or missing environment.
    Caller's action: fail, no retry."""

    INPUT_REJECTED = 65
    """A declared input was absent, corrupt or incompatible once its
    storage was reached. Caller's action: fail, no retry."""

    STAGE_ERROR = 70
    """Unclassified stage error; stop for investigation.
    Caller's action: fail, no retry."""

    TRANSIENT_FAILURE = 75
    """A recognised temporary dependency failure; repeating the same work
    may succeed. Caller's action: retry within the limit."""


class StageContractError(Exception):
    """Base class for the four exceptions ``run_stage`` maps to exit codes."""

    exit_code: ExitCode


class UsageError(StageContractError):
    """Bad arguments, invalid settings, or missing environment. Maps to 64."""

    exit_code = ExitCode.USAGE


class InputRejected(StageContractError):
    """A declared input is absent, corrupt or incompatible. Maps to 65."""

    exit_code = ExitCode.INPUT_REJECTED


class StageError(StageContractError):
    """An unclassified stage error the caller should stop and investigate.
    Maps to 70. ``run_stage`` also maps any exception not otherwise listed
    here to this code, per the contract: "The entrypoint maps argument
    errors to 64 and unhandled exceptions to 70."."""

    exit_code = ExitCode.STAGE_ERROR


class TransientFailure(StageContractError):
    """A recognised temporary dependency failure; retry within the limit
    may succeed. Maps to 75."""

    exit_code = ExitCode.TRANSIENT_FAILURE


@dataclass(frozen=True)
class StageDeclaration:
    """What a stage is: its name, unit, schemas, dependencies and limits.

    Importing a declaration performs no I/O (stage contract, "Declaration").
    ``consumes`` and ``produces`` name the product kinds the stage requires
    and writes; ``settings_schema_path`` is the stage's
    ``settings/<name>.toml`` defaults file, or ``None`` if it declares no
    settings. ``supported_exit_codes`` must be a subset of the five defined
    in :class:`ExitCode`, and 0 is always implicitly supported.
    """

    name: str
    unit: str
    argument_schema: dict[str, Any]
    settings_schema_path: str | None
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    database_access: str
    resource_defaults: dict[str, Any] = field(default_factory=dict)
    supported_exit_codes: tuple[ExitCode, ...] = (
        ExitCode.SUCCESS,
        ExitCode.USAGE,
        ExitCode.INPUT_REJECTED,
        ExitCode.STAGE_ERROR,
        ExitCode.TRANSIENT_FAILURE,
    )

    def validate(self) -> None:
        if self.name not in STAGE_NAMES:
            raise ValueError(
                f"unknown stage name {self.name!r}; expected one of {STAGE_NAMES}")
        if self.unit not in UNIT_KINDS:
            raise ValueError(
                f"unknown unit kind {self.unit!r}; expected one of {UNIT_KINDS}")
        if self.database_access not in DB_ACCESS_LEVELS:
            raise ValueError(
                f"unknown database_access {self.database_access!r}; "
                f"expected one of {DB_ACCESS_LEVELS}")
        unknown_codes = set(self.supported_exit_codes) - set(ExitCode)
        if unknown_codes:
            raise ValueError(f"unsupported exit codes declared: {unknown_codes}")


@dataclass
class StageContext:
    """What ``body(context)`` receives: parsed invocation, settings, inputs.

    ``input_manifest`` is the parsed and validated upstream
    :class:`~rapidpipe.products.manifest.Manifest` this attempt reads (the
    ``--inputs`` location's ``manifest.json``); ``body`` reads its
    ``inputs.products`` and ``outputs`` to find what it needs, and never
    reads a fresher copy from disk.
    """

    declaration: StageDeclaration
    run_id: str
    unit_id: str
    attempt_id: str
    inputs_dir: Path
    outputs_dir: Path
    settings: dict[str, Any]
    settings_hash: str
    input_manifest: Manifest
    dry_run: bool
    logger: logging.Logger | logging.LoggerAdapter


@dataclass
class StageResult:
    """What ``body(context)`` returns: its outputs, and what it read.

    ``outputs`` is the list of :class:`~rapidpipe.products.manifest.OutputEntry`
    the stage wrote (file products, database result sets, or both).
    ``products_read`` maps each consumed product kind to the upstream
    instance id the stage actually used (products page, "A complete
    manifest": ``inputs.products``) -- ``body`` selects these from
    ``context.input_manifest``, since only the stage knows which of the
    input manifest's declared products it read. ``result_sets_read`` names
    the database result-set instance ids the stage read, for the stages
    the stage contract names (``crossmatch``, ``statistics``, ``prune``);
    transform stages leave both empty as appropriate. ``body`` assembles
    neither ``run``/``unit``/``stage``/``attempt`` nor the execution record
    or input-manifest reference -- ``run_stage`` supplies those from the
    invocation it already parsed.
    """

    outputs: Sequence[OutputEntry]
    products_read: dict[str, str] = field(default_factory=dict)
    result_sets_read: Sequence[str] = ()


def _build_parser(declaration: StageDeclaration) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"rapidpipe stage {declaration.name}", add_help=True)
    parser.add_argument("--run", required=True, dest="run_id")
    parser.add_argument("--unit", required=True, dest="unit_id")
    parser.add_argument("--attempt", required=True, dest="attempt_id")
    parser.add_argument("--inputs", required=True, dest="inputs")
    parser.add_argument("--outputs", required=True, dest="outputs")
    parser.add_argument("--settings", required=False, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    return parser


def _is_s3(location: str) -> bool:
    return location.startswith("s3://")


def _read_input_manifest(inputs: str) -> Manifest:
    if _is_s3(inputs):
        raise UsageError(
            f"--inputs {inputs!r}: S3 input locations are not implemented "
            "in this stage runner; pass a local directory containing "
            "manifest.json")
    manifest_path = Path(inputs) / "manifest.json"
    if not manifest_path.exists():
        raise InputRejected(f"input manifest not found: {manifest_path}")
    try:
        return Manifest.read(manifest_path)
    except json.JSONDecodeError as exc:
        raise InputRejected(f"{manifest_path}: not valid JSON: {exc}") from exc
    except ManifestError as exc:
        raise InputRejected(f"{manifest_path}: invalid manifest: {exc}") from exc


def _source_revision() -> str | None:
    """``git rev-parse HEAD`` in the current working directory, or ``None``.

    ``None`` covers every way this can fail to produce a revision: git not
    installed, cwd not a repository, or any other non-zero exit -- the
    execution record then simply omits provenance it could not determine,
    per the contract's "source revision, working-copy changes if any,
    image digest when applicable".
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None


def _write_execution_record(
    outputs_dir: Path,
    attempt_id: str,
    settings_hash: str,
) -> str:
    """Write ``exec/<attempt>.json`` under ``outputs_dir``; return its
    manifest-relative path.

    Holds the resolved settings hash, the source revision (``None`` if
    ``git rev-parse HEAD`` does not succeed in the current working
    directory), and the image digest from ``RAPIDPIPE_IMAGE_DIGEST`` if
    set, else ``None`` -- the execution record's minimal content per the
    stage contract's "The manifest": "the source revision, working-copy
    changes if any, image digest when applicable, database schema
    version, and the resolved settings." Schema version and working-copy
    changes are not recorded here: they belong to ``rapidpipe.runs``,
    which this module must not import.
    """
    relative_path = f"exec/{attempt_id}.json"
    record_path = outputs_dir / relative_path
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "settings_hash": settings_hash,
        "source_revision": _source_revision(),
        "image_digest": os.environ.get("RAPIDPIPE_IMAGE_DIGEST"),
    }
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return relative_path


def run_stage(
    declaration: StageDeclaration,
    body: Callable[[StageContext], StageResult],
    argv: Sequence[str],
) -> int:
    """Parse argv under the one invocation form, run ``body``, return an exit code.

    Never raises: every exception is mapped to an :class:`ExitCode` value,
    which is what the caller (``main(argv)``) should pass to ``sys.exit``.
    ``--dry-run`` validates arguments, settings and the input manifest, then
    returns 0 without calling ``body`` or writing anything (contract,
    "Invocation"). Otherwise ``body`` is called and, only after it returns a
    :class:`StageResult`, ``run_stage`` wraps it into the enclosing
    :class:`~rapidpipe.products.manifest.Manifest` -- run, unit and stage
    from the declaration and argv, attempt from ``--attempt``, an
    execution record this function writes to ``exec/<attempt>.json``, and
    ``inputs.manifest`` set to the ``--inputs`` location -- validates it,
    and publishes it to ``--outputs``. If ``body`` raises, no manifest is
    written.
    """
    declaration.validate()
    # Before argv is parsed there is no run/attempt id yet; a plain logger
    # (still under the rapidpipe.* hierarchy, still caught by
    # rapidpipe.log's identity filter once configured) covers that window.
    logger = logging.getLogger(f"rapidpipe.stages.{declaration.name}")

    try:
        parser = _build_parser(declaration)
        try:
            args = parser.parse_args(list(argv))
        except SystemExit as exc:
            # argparse calls sys.exit(2) on a usage error; translate to the
            # contract's usage code instead of letting 2 leak out.
            logger.info(
                "stage=%s exit=%s reason=argparse", declaration.name, ExitCode.USAGE)
            return int(ExitCode.USAGE) if exc.code != 0 else int(ExitCode.SUCCESS)

        # Now that --run and --attempt are known, switch to the identified
        # logger so every remaining line (including the "start" line below)
        # carries them, per the contract's "wire it into run_stage so every
        # stage logs its start, its exit code and its manifest path".
        logger = stage_log_context(declaration.name, args.run_id, args.attempt_id)

        if _is_s3(args.outputs):
            raise UsageError(
                f"--outputs {args.outputs!r}: S3 output locations are not "
                "implemented in this stage runner")
        outputs_dir = Path(args.outputs)

        try:
            settings = resolve_settings(declaration.settings_schema_path, args.settings)
        except (SettingsError, FileNotFoundError, OSError) as exc:
            raise UsageError(str(exc)) from exc
        settings_hash = canonical_hash(settings)

        input_manifest = _read_input_manifest(args.inputs)

        context = StageContext(
            declaration=declaration,
            run_id=args.run_id,
            unit_id=args.unit_id,
            attempt_id=args.attempt_id,
            inputs_dir=Path(args.inputs),
            outputs_dir=outputs_dir,
            settings=settings,
            settings_hash=settings_hash,
            input_manifest=input_manifest,
            dry_run=args.dry_run,
            logger=logger,
        )

        logger.info(
            "stage=%s run=%s unit=%s attempt=%s start dry_run=%s",
            declaration.name, context.run_id, context.unit_id,
            context.attempt_id, context.dry_run)

        if args.dry_run:
            logger.info(
                "stage=%s run=%s unit=%s attempt=%s exit=%s dry-run validated",
                declaration.name, context.run_id, context.unit_id,
                context.attempt_id, int(ExitCode.SUCCESS))
            return int(ExitCode.SUCCESS)

        result = body(context)

        if not isinstance(result, StageResult):
            raise StageError(
                f"stage body returned {type(result).__name__}, "
                "expected a StageResult")

        outputs_dir.mkdir(parents=True, exist_ok=True)
        execution_record_ref = _write_execution_record(
            outputs_dir, args.attempt_id, settings_hash)

        manifest = Manifest(
            run=args.run_id,
            unit=Unit(kind=declaration.unit, id=args.unit_id),
            stage=declaration.name,
            attempt=args.attempt_id,
            execution_record=execution_record_ref,
            inputs=Inputs(
                manifest=str(Path(args.inputs) / "manifest.json"),
                products=dict(result.products_read),
                result_sets=tuple(result.result_sets_read),
            ),
            outputs=tuple(result.outputs),
        )

        manifest_path = outputs_dir / "manifest.json"
        try:
            manifest.write(manifest_path)
        except ManifestError as exc:
            raise StageError(f"completion manifest failed validation: {exc}") from exc

        logger.info(
            "stage=%s run=%s unit=%s attempt=%s exit=%s manifest=%s",
            declaration.name, context.run_id, context.unit_id,
            context.attempt_id, int(ExitCode.SUCCESS), manifest_path)
        return int(ExitCode.SUCCESS)

    except StageContractError as exc:
        logger.error(
            "stage=%s exit=%s error=%s", declaration.name, int(exc.exit_code), exc)
        return int(exc.exit_code)
    except Exception as exc:  # noqa: BLE001 - contract: unhandled -> 70
        logger.error(
            "stage=%s exit=%s error=%s", declaration.name, int(ExitCode.STAGE_ERROR),
            exc, exc_info=True)
        return int(ExitCode.STAGE_ERROR)
