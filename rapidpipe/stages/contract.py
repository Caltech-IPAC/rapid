"""The stage contract as code: declaration, exit codes, and the runner.

Every stage exports a :class:`StageDeclaration` and calls :func:`run_stage`
from its ``main(argv)``. This module implements the contract's "Invocation",
"Exit codes" and "Settings" sections; ``rapidpipe.stages.settings`` supplies
the TOML load and merge that "Settings" describes. ``--settings`` accepts a
local path or an ``s3://bucket/key`` location -- a Batch job has no laptop
path to give it -- fetched here (``rapidpipe.stages.settings`` stays
stdlib-only, so the fetch cannot live there) before the file is handed to
``resolve_settings``; a bad or unreachable ``s3://`` overlay is a usage
error (exit 64), the same as a bad local ``--settings`` path, not the
``InputRejected``/``TransientFailure`` family ``--inputs``/``--outputs`` S3
errors map to -- the overlay is an argument, not a declared input. The
manifest ``run_stage`` publishes follows the products page's shape
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
import shutil
import subprocess
import tempfile
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
from rapidpipe.products.storage import (
    Location,
    LocationError,
    fetch_object,
    fetch_prefix,
    join,
    parse_location,
    publish_dir,
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

    ``inputs_dir``/``outputs_dir`` are always local directories: when
    ``--inputs``/``--outputs`` name an S3 prefix, ``run_stage`` fetches (or
    stages) it under a temporary work directory first, and these two
    fields point there. ``inputs_location``/``outputs_location`` carry the
    original ``--inputs``/``--outputs`` argument strings unchanged, for a
    stage that must record the location rather than the local path it was
    resolved to (e.g. ``register``'s ``output_location``).
    """

    declaration: StageDeclaration
    run_id: str
    unit_id: str
    attempt_id: str
    inputs_dir: Path
    outputs_dir: Path
    inputs_location: str
    outputs_location: str
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
    transform stages leave both empty as appropriate. ``execution_notes``
    is anything the stage must record about how the attempt went that is
    not an output (the difference stage notes a non-fatal SFFT failure
    here); ``run_stage`` writes it into the execution record under
    ``notes``, and writes no ``notes`` key when it is empty. ``body`` assembles
    neither ``run``/``unit``/``stage``/``attempt`` nor the execution record
    or input-manifest reference -- ``run_stage`` supplies those from the
    invocation it already parsed.
    """

    outputs: Sequence[OutputEntry]
    products_read: dict[str, str] = field(default_factory=dict)
    result_sets_read: Sequence[str] = ()
    execution_notes: dict[str, Any] = field(default_factory=dict)


def _build_parser(declaration: StageDeclaration) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"rapidpipe stage {declaration.name}", add_help=True)
    parser.add_argument("--run", required=True, dest="run_id")
    parser.add_argument("--unit", required=True, dest="unit_id")
    parser.add_argument("--attempt", required=True, dest="attempt_id")
    parser.add_argument("--inputs", required=True, dest="inputs")
    parser.add_argument("--outputs", required=True, dest="outputs")
    parser.add_argument(
        "--settings", required=False, default=None,
        help="Path to a TOML settings overlay: a local path, or an "
             "s3://bucket/key location (fetched before use, including "
             "under --dry-run).")
    parser.add_argument("--dry-run", action="store_true", default=False)
    return parser


def _is_s3(location: str) -> bool:
    return location.startswith("s3://")


#: Recognised as network-shaped: a fetch or publish is retryable (maps to
#: TransientFailure) when the exception's class name ends with one of
#: these, checked by name (suffix, not exact match, so a test's stand-in
#: class such as ``FakeEndpointConnectionError`` is recognised the same
#: way as ``botocore.exceptions.EndpointConnectionError``) rather than by
#: ``isinstance``, since this module never imports botocore.
_TRANSIENT_EXCEPTION_NAMES = (
    "EndpointConnectionError",
    "ConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ThrottlingException",
    "RequestTimeout",
    "RequestTimeoutException",
)


#: A ClientError-shaped exception's ``response["Error"]["Code"]`` values
#: that mean "the object is not there" -- a missing input manifest, mapped
#: to InputRejected rather than the generic StageError other client errors
#: get.
_NOT_FOUND_ERROR_CODES = ("404", "NoSuchKey")


def _client_error_code(exc: BaseException) -> str | None:
    """The ``Error.Code`` of a ClientError-shaped exception, or ``None``.

    Matches by shape (a ``response`` attribute holding that structure), not
    by ``isinstance``, so a test's stand-in exception is recognised the
    same way as a real ``botocore.exceptions.ClientError`` without this
    module importing botocore.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _map_storage_error(exc: BaseException) -> StageContractError:
    """Map an S3 fetch/publish failure to the contract's exit codes.

    A missing object (a ClientError-shaped exception with a "not found"
    error code) is :class:`InputRejected`. A ``botocore``/``boto3``
    exception (or ``ImportError`` for missing boto3) whose class name
    looks network-shaped -- a connection failure, timeout, or throttling --
    is a :class:`TransientFailure` (retrying the same work may succeed);
    any other storage-layer or boto3 failure is a :class:`StageError`.
    """
    if isinstance(exc, ImportError):
        return StageError(f"boto3 is required for an S3 location: {exc}")

    code = _client_error_code(exc)
    if code in _NOT_FOUND_ERROR_CODES:
        return InputRejected(str(exc))

    class_name = type(exc).__name__
    if any(class_name.endswith(name) for name in _TRANSIENT_EXCEPTION_NAMES):
        return TransientFailure(str(exc))
    return StageError(str(exc))


def _read_input_manifest_from(inputs_dir: Path, inputs_arg: str) -> Manifest:
    manifest_path = inputs_dir / "manifest.json"
    if not manifest_path.exists():
        raise InputRejected(f"input manifest not found: {manifest_path}")
    try:
        return Manifest.read(manifest_path)
    except json.JSONDecodeError as exc:
        raise InputRejected(f"{manifest_path}: not valid JSON: {exc}") from exc
    except ManifestError as exc:
        raise InputRejected(f"{manifest_path}: invalid manifest: {exc}") from exc


def _work_root() -> Path:
    """The base directory for a stage attempt's temporary work directory.

    ``RAPIDPIPE_WORK`` if set, else the system temp directory (stage
    contract: locations and settings come from the environment).
    """
    configured = os.environ.get("RAPIDPIPE_WORK")
    return Path(configured) if configured else Path(tempfile.gettempdir())


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
    notes: dict[str, Any] | None = None,
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
    if notes:
        record["notes"] = notes
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

    work_dir: Path | None = None
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

        try:
            inputs_location = parse_location(args.inputs)
            outputs_location = parse_location(args.outputs)
            settings_location = (
                parse_location(args.settings) if args.settings is not None else None)
        except LocationError as exc:
            raise UsageError(str(exc)) from exc

        settings_is_s3 = settings_location is not None and settings_location.is_s3()

        needs_work_dir = (
            inputs_location.is_s3() or outputs_location.is_s3() or settings_is_s3)
        if needs_work_dir:
            _work_root().mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(
                prefix=f"rapidpipe-{declaration.name}-{args.attempt_id}-",
                dir=str(_work_root())))

        # The settings overlay is fetched (if it names an s3:// location)
        # and resolved before --dry-run's early return, since --dry-run
        # validates settings too. A missing/unreadable overlay -- local or
        # S3 -- is a usage error (exit 64): the overlay is an argument, not
        # a declared input, so this does not go through
        # _map_storage_error's InputRejected/TransientFailure mapping.
        settings_overlay_path = args.settings
        if settings_is_s3:
            assert work_dir is not None
            assert settings_location is not None
            try:
                settings_overlay_path = fetch_object(
                    settings_location, "",
                    work_dir / "settings-overlay.toml")
            except LocationError as exc:
                raise UsageError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise UsageError(
                    f"could not fetch --settings overlay {args.settings!r}: {exc}"
                ) from exc

        try:
            settings = resolve_settings(
                declaration.settings_schema_path, settings_overlay_path)
        except (SettingsError, FileNotFoundError, OSError) as exc:
            raise UsageError(str(exc)) from exc
        settings_hash = canonical_hash(settings)

        if inputs_location.is_s3():
            assert work_dir is not None
            inputs_dir = work_dir / "inputs"
        else:
            assert inputs_location.path is not None
            inputs_dir = inputs_location.path

        if outputs_location.is_s3():
            assert work_dir is not None
            outputs_dir = work_dir / "outputs"
        else:
            assert outputs_location.path is not None
            outputs_dir = outputs_location.path

        if inputs_location.is_s3():
            inputs_dir.mkdir(parents=True, exist_ok=True)
            try:
                if args.dry_run:
                    # Validate without fetching the whole prefix: one object.
                    fetch_object(
                        inputs_location, "manifest.json",
                        inputs_dir / "manifest.json")
                else:
                    fetch_prefix(inputs_location, inputs_dir)
            except LocationError as exc:
                raise InputRejected(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise _map_storage_error(exc) from exc

        input_manifest = _read_input_manifest_from(inputs_dir, args.inputs)

        context = StageContext(
            declaration=declaration,
            run_id=args.run_id,
            unit_id=args.unit_id,
            attempt_id=args.attempt_id,
            inputs_dir=inputs_dir,
            outputs_dir=outputs_dir,
            inputs_location=args.inputs,
            outputs_location=args.outputs,
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
            outputs_dir, args.attempt_id, settings_hash, dict(result.execution_notes))

        manifest = Manifest(
            run=args.run_id,
            unit=Unit(kind=declaration.unit, id=args.unit_id),
            stage=declaration.name,
            attempt=args.attempt_id,
            execution_record=execution_record_ref,
            inputs=Inputs(
                manifest=join(inputs_location, "manifest.json"),
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

        if outputs_location.is_s3():
            try:
                publish_dir(outputs_dir, outputs_location)
            except Exception as exc:  # noqa: BLE001
                raise _map_storage_error(exc) from exc
            published_manifest_ref = join(outputs_location, "manifest.json")
        else:
            published_manifest_ref = str(manifest_path)

        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir = None

        logger.info(
            "stage=%s run=%s unit=%s attempt=%s exit=%s manifest=%s",
            declaration.name, context.run_id, context.unit_id,
            context.attempt_id, int(ExitCode.SUCCESS), published_manifest_ref)
        return int(ExitCode.SUCCESS)

    except StageContractError as exc:
        logger.error(
            "stage=%s exit=%s error=%s", declaration.name, int(exc.exit_code), exc)
        if work_dir is not None:
            logger.info(
                "stage=%s attempt failed; work directory kept at %s",
                declaration.name, work_dir)
        return int(exc.exit_code)
    except Exception as exc:  # noqa: BLE001 - contract: unhandled -> 70
        logger.error(
            "stage=%s exit=%s error=%s", declaration.name, int(ExitCode.STAGE_ERROR),
            exc, exc_info=True)
        if work_dir is not None:
            logger.info(
                "stage=%s attempt failed; work directory kept at %s",
                declaration.name, work_dir)
        return int(ExitCode.STAGE_ERROR)
