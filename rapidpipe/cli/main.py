"""The ``rapidpipe`` command-line tool.

``rapidpipe stage <name> ...`` imports ``rapidpipe.stages.<name>`` and calls
its ``main(argv)``, per the stage contract's "Invocation" section: "``rapidpipe
stage <name>`` calls that same entrypoint." The ``rapidpipe run``
subcommands implemented here:

- ``create``, ``list``, ``show`` -- record a run, list runs, inspect one
  (its units, attempts, expiry, pin and promotions), over
  ``rapidpipe.runs.repository``;
- ``local`` -- run one stage attempt as a local subprocess through
  ``rapidpipe.runs.local``;
- ``submit``, ``reconcile``, ``cancel`` -- one attempt on AWS Batch,
  through ``rapidpipe.launch.batch``;
- ``promote`` -- promote a production run's deliverables
  (``repository.promote_run``); ``rollback`` -- reverse one promotion
  (``repository.rollback_promotion``);
- ``finish`` -- mark a run whose units are all terminal finished
  (``repository.finish_run``);
- ``delete`` -- delete a scratch run and its outputs
  (``rapidpipe.runs.cleanup.delete_run``); ``pin`` / ``unpin`` -- keep a
  scratch run from expiring, or release it (``cleanup.pin_run``).

``rapidpipe release cut|show|list|verify`` is ``python -m
rapidpipe.release``: the ``release`` subparser is built and dispatched by
``rapidpipe.release.__main__``, so both forms are one code path.

The specification's "Tools" section also names Batch-level run
management (rerun part of a run, watch progress, restart), not yet built.

This module resolves a stage's ``DECLARATION`` by
``importlib.import_module(f"rapidpipe.stages.{name}")`` rather than
importing a stage module at the top level, so ``rapidpipe.cli`` -- the one
subpackage allowed to import anything (stage contract, "The package") --
stays the only place that decides which stage code loads, and
``rapidpipe.runs.local`` (which the ``run local`` subcommand calls into)
never has to import a stage itself.

Plain argparse; no third-party CLI library.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from rapidpipe import __version__
from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.db.connection import connect as _default_connect
from rapidpipe.launch.batch import (
    DependencyIncomplete,
    LaunchError,
    MissingEnvironmentVariable,
    ReleaseDefinitionRefused,
)
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products.manifest import Manifest, ManifestError, register_unit_id
from rapidpipe.products.storage import fetch_object, parse_location
from rapidpipe.release import __main__ as release_cli
from rapidpipe.runs.local import run_stage_locally
from rapidpipe.runs.repository import RunModelError
from rapidpipe.selftest import run as run_selftest
from rapidpipe.selftest.runner import STAGE_NAMES as SELFTEST_STAGE_NAMES
from rapidpipe.stages.contract import STAGE_NAMES, ExitCode

#: Recognised as network-shaped, the same rule
#: ``rapidpipe.stages.contract._map_storage_error`` uses: matched by class
#: name suffix, not ``isinstance``, so a test's stand-in exception (or a
#: real ``botocore.exceptions`` one) without this module importing
#: botocore is treated identically. A Batch error of this shape, or any
#: ``ClientError``-shaped exception, exits 75 -- retryable, since the CLI
#: invocation itself can simply be retried.
_BATCH_ERROR_NAMES = (
    "ClientError",
    "EndpointConnectionError",
    "ConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ThrottlingException",
    "RequestTimeout",
    "RequestTimeoutException",
    "BotoCoreError",
)


def _is_batch_error(exc: BaseException) -> bool:
    class_name = type(exc).__name__
    return any(class_name.endswith(name) for name in _BATCH_ERROR_NAMES)

#: Module-level indirection so tests can monkeypatch
#: ``rapidpipe.cli.main.connect`` without reaching into
#: ``rapidpipe.db.connection`` and affecting other callers -- the same
#: pattern ``rapidpipe.stages.register`` uses for the same reason.
connect = _default_connect


class RegisterUnitIdError(ValueError):
    """The register unit id could not be derived from its input manifest."""


def _read_manifest_at(location_arg: str) -> Manifest:
    """Read ``manifest.json`` from a local directory or S3 prefix.

    Same fetch used by a stage's own ``--dry-run`` path
    (``rapidpipe.stages.contract._read_input_manifest_from``, one object,
    not the whole prefix): a local location is read directly; an S3
    location is fetched to a throwaway temp file first, since
    :meth:`~rapidpipe.products.manifest.Manifest.read` only takes a local
    path.
    """
    location = parse_location(location_arg)
    if not location.is_s3():
        manifest_path = Path(location_arg) / "manifest.json"
    else:
        with tempfile.TemporaryDirectory(prefix="rapidpipe-register-unit-") as tmp:
            manifest_path = fetch_object(
                location, "manifest.json", Path(tmp) / "manifest.json")
            return _load_manifest(manifest_path)
    return _load_manifest(manifest_path)


def _load_manifest(manifest_path: Path) -> Manifest:
    try:
        return Manifest.read(manifest_path)
    except FileNotFoundError as exc:
        raise RegisterUnitIdError(f"input manifest not found: {manifest_path}") from exc
    except (json.JSONDecodeError, ManifestError) as exc:
        raise RegisterUnitIdError(f"{manifest_path}: invalid manifest: {exc}") from exc


def _resolve_register_unit_id(*, unit_id_arg: str | None, inputs_location_arg: str) -> str:
    """The ``--unit`` value to use for a `register` invocation.

    register's unit id is always derived from the manifest it reads
    (register_unit_id, ruling: "a register unit is identified by what it
    registers"), never chosen by the caller, so an explicit ``--unit`` for
    register is refused rather than silently overridden.
    """
    if unit_id_arg is not None:
        raise RegisterUnitIdError(
            "--unit is not accepted for register: its unit id is always "
            "derived from the manifest it reads")
    manifest = _read_manifest_at(inputs_location_arg)
    return register_unit_id(manifest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rapidpipe", description="The RAPID pipeline command-line tool.")
    parser.add_argument(
        "--version", action="version", version=f"rapidpipe {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    stage_parser = subparsers.add_parser(
        "stage",
        help="Run one stage: rapidpipe stage <name> --run ... --unit ... "
             "--attempt ... --inputs ... --outputs ... [--settings ...] "
             "[--dry-run]",
        add_help=False,
    )
    stage_parser.add_argument("name", nargs="?", help="Stage name.")
    stage_parser.add_argument(
        "stage_argv", nargs=argparse.REMAINDER,
        help="Arguments forwarded to the stage's own entrypoint.")

    selftest_parser = subparsers.add_parser(
        "selftest",
        help="Run one stage's own packaged fixture and check it: "
             "rapidpipe selftest --stage difference|finalize|load|maintain|crossmatch|alerts|statistics|prune [--real-tools] "
             "[--work-dir DIR] [--output-location s3://... or path]")
    selftest_parser.add_argument(
        "--stage", required=True, choices=SELFTEST_STAGE_NAMES, help="Which stage's fixture to run.")
    selftest_parser.add_argument(
        "--real-tools", action="store_true",
        help="Run the pipeline image's own tools/database instead of the "
             "packaged fakes (the default). Needs the pipeline image, or a "
             "checkout with the same tools on PATH.")
    selftest_parser.add_argument(
        "--work-dir", default=None,
        help="An empty or new directory to prepare the fixture in "
             "(default: a fresh temporary directory).")
    selftest_parser.add_argument(
        "--output-location", default=None,
        help="Where the stage's manifest and products end up: a local "
             "path or an s3:// prefix (default: <work-dir>/outputs). The "
             "stage always runs against a local directory first, and "
             "every check runs against that copy; an s3:// location is "
             "uploaded to only afterwards, so the check count is the "
             "same either way.")
    selftest_parser.add_argument("--python", default=sys.executable)

    run_parser = subparsers.add_parser(
        "run", help="Create, list, inspect, run, promote and delete runs.")
    run_subparsers = run_parser.add_subparsers(dest="run_command")

    create_parser = run_subparsers.add_parser(
        "create", help="Create a run and print its id.")
    create_parser.add_argument(
        "--kind", required=True, choices=("scratch", "production"))
    create_parser.add_argument("--purpose", required=True)
    create_parser.add_argument(
        "--stages", required=True,
        help="Comma-separated list of selected stage names.")
    create_parser.add_argument("--owner", default=None)
    create_parser.add_argument("--lane", default="local")
    create_parser.add_argument("--profile", default="local")
    create_parser.add_argument("--db-target", default=None)
    create_parser.add_argument("--max-attempts", type=int, default=1)
    create_parser.add_argument("--settings-overlay-ref", default=None)
    create_parser.add_argument("--input-selection-ref", default=None)
    create_parser.add_argument(
        "--release", default=None, metavar="TAG",
        help="Create the run from a complete release: its source revision and "
             "image digest are the run's, and Batch submissions use its job "
             "definition revisions.")

    list_parser = run_subparsers.add_parser("list", help="List runs.")
    list_parser.add_argument("--kind", default=None, choices=("scratch", "production"))
    list_parser.add_argument("--owner", default=None)
    list_parser.add_argument("--state", default=None)

    show_parser = run_subparsers.add_parser("show", help="Show one run.")
    show_parser.add_argument("run_id")

    local_parser = run_subparsers.add_parser(
        "local", help="Run one stage attempt locally as a subprocess.")
    local_parser.add_argument("run_id")
    local_parser.add_argument("stage")
    local_parser.add_argument(
        "--unit", required=False, default=None, dest="unit_id",
        help="Required for every stage except register, whose unit id is "
             "always derived from the manifest it reads (--unit is refused "
             "for register).")
    local_parser.add_argument("--inputs", required=True)
    local_parser.add_argument("--outputs-root", required=True)
    local_parser.add_argument("--settings", default=None)
    local_parser.add_argument("--python", default=sys.executable)

    submit_parser = run_subparsers.add_parser(
        "submit", help="Submit one stage attempt to Batch.")
    submit_parser.add_argument("run_id")
    submit_parser.add_argument("stage")
    submit_parser.add_argument(
        "--unit", required=False, default=None, dest="unit_id",
        help="Required for every stage except register, whose unit id is "
             "always derived from the manifest it reads (--unit is refused "
             "for register).")
    inputs_group = submit_parser.add_mutually_exclusive_group(required=True)
    inputs_group.add_argument("--inputs", default=None)
    inputs_group.add_argument("--inputs-from-stage", default=None, dest="inputs_from_stage")
    submit_parser.add_argument("--settings", default=None)

    reconcile_parser = run_subparsers.add_parser(
        "reconcile", help="Reconcile a run's unresolved Batch attempts.")
    reconcile_parser.add_argument("run_id")

    cancel_parser = run_subparsers.add_parser(
        "cancel", help="Terminate an attempt's Batch job.")
    cancel_parser.add_argument("attempt_id")
    cancel_parser.add_argument("--reason", required=True)

    promote_parser = run_subparsers.add_parser(
        "promote", help="Promote a production run's candidates; print the promotion id.")
    promote_parser.add_argument("run_id")
    promote_parser.add_argument("--reason", required=True)
    promote_parser.add_argument(
        "--who", default=None, help="Who is promoting (default: the current user).")
    promote_parser.add_argument(
        "--kinds", default=None,
        help="Comma-separated product kinds to promote (default: every kind).")
    promote_parser.add_argument(
        "--allow-unreleased", action="store_true",
        help="Promote even if a deliverable's attempt ran no complete release's "
             "image; recorded in the promotion's request_context.")

    rollback_parser = run_subparsers.add_parser(
        "rollback", help="Reverse one promotion; print the reversing promotion id.")
    rollback_parser.add_argument("promotion_id")
    rollback_parser.add_argument("--reason", required=True)
    rollback_parser.add_argument(
        "--who", default=None, help="Who is rolling back (default: the current user).")

    delete_parser = run_subparsers.add_parser(
        "delete", help="Delete a scratch run's outputs and science rows.")
    delete_parser.add_argument("run_id")
    delete_parser.add_argument(
        "--requested-by", default=None, dest="requested_by",
        help="The run's owner (default: the current user).")

    finish_parser = run_subparsers.add_parser(
        "finish", help="Mark a run whose units are all terminal as finished.")
    finish_parser.add_argument("run_id")

    pin_parser = run_subparsers.add_parser(
        "pin", help="Pin a run so it never expires.")
    pin_parser.add_argument("run_id")

    unpin_parser = run_subparsers.add_parser(
        "unpin", help="Unpin a run so it expires at its expires_at.")
    unpin_parser.add_argument("run_id")

    release_parser = subparsers.add_parser(
        "release", help="Cut, show, list and verify releases (python -m rapidpipe.release).")
    release_cli.build_parser(release_parser)

    return parser


def _run_stage_command(name: str | None, stage_argv: list[str]) -> int:
    if not name:
        sys.stderr.write(
            "rapidpipe stage: a stage name is required; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)
    if name not in STAGE_NAMES:
        sys.stderr.write(
            f"rapidpipe stage: unknown stage {name!r}; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    module_name = f"rapidpipe.stages.{name.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        sys.stderr.write(
            f"rapidpipe stage: {name!r} is a known stage name but "
            f"{module_name} is not implemented yet; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    stage_main = getattr(module, "main", None)
    if stage_main is None:
        sys.stderr.write(
            f"rapidpipe stage: {module_name} has no main(argv)\n")
        return int(ExitCode.USAGE)

    return stage_main(stage_argv)


def _run_selftest_command(args: argparse.Namespace) -> int:
    """``rapidpipe selftest``: run one stage's own packaged fixture.

    A thin wrapper over :func:`rapidpipe.selftest.run`, which does the
    actual prepare/run/check work; this function only translates argparse
    into that call. Exit codes per the ruling this subcommand implements
    (2026-09-23): 0 on a full pass, 1 on a fixture mismatch, and the
    stage's own exit code on a stage failure.
    """
    return run_selftest(
        stage=args.stage, real_tools=args.real_tools, work_dir=args.work_dir,
        output_location=args.output_location, python=args.python)


def _source_revision_or_unknown() -> str:
    """``git rev-parse HEAD`` in the current working directory, else the
    literal ``"unknown"`` (brief: "else the literal unknown"; distinct from
    the stage contract's execution record, which uses ``None`` for the same
    failure -- this is a run-creation field, not an execution record)."""
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


def _last_applied_schema_version(cur) -> str | None:
    """The filename of the most recently applied migration.

    ``database/apply-migrations.sh`` tracks applied migrations in
    ``schema_migrations(filename, sha256, applied_at)``; filenames sort
    lexicographically in applied order (``YYYYMMDD-NN-short-name.sql``),
    so the greatest filename is the last one applied.
    """
    cur.execute("SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


def _run_create_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import create_run

    owner = args.owner or getpass.getuser()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if not stages:
        sys.stderr.write("rapidpipe run create: --stages must name at least one stage\n")
        return int(ExitCode.USAGE)

    try:
        cm = connect(application_name="rapidpipe-run-create")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run create: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run create: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            with conn.cursor() as cur:
                schema_version = _last_applied_schema_version(cur) or "unknown"
                if args.release is not None:
                    cur.execute(
                        "SELECT state, source_revision, image_digest FROM releases "
                        "WHERE tag = %s", (args.release,))
                    release_row = cur.fetchone()
            if args.release is not None:
                if release_row is None or release_row[0] != "complete":
                    state = "absent" if release_row is None else release_row[0]
                    sys.stderr.write(
                        f"rapidpipe run create: release {args.release} is {state}, "
                        "not complete; refusing\n")
                    conn.rollback()
                    return 2
                _, code_revision, image_digest = release_row
            else:
                code_revision = _source_revision_or_unknown()
                image_digest = os.environ.get("RAPIDPIPE_IMAGE_DIGEST")

            run_id = create_run(
                conn,
                kind=args.kind,
                owner=owner,
                purpose=args.purpose,
                selected_stages=stages,
                code_revision=code_revision,
                image_digest=image_digest,
                schema_version=schema_version,
                settings_overlay_ref=args.settings_overlay_ref,
                input_selection_ref=args.input_selection_ref,
                lane=args.lane,
                resource_profile=args.profile,
                database_target=args.db_target or os.environ.get("PGDATABASE", ""),
                max_attempts_per_unit=args.max_attempts,
                auto_promote=False,
                check_policy_ref=None,
                release=args.release,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    print(run_id)
    return int(ExitCode.SUCCESS)


def _run_list_command(args: argparse.Namespace) -> int:
    try:
        cm = connect(application_name="rapidpipe-run-list")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run list: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run list: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        query = "SELECT id, kind, owner, state, created, purpose FROM runs WHERE 1=1"
        params: list[Any] = []
        if args.kind:
            query += " AND kind = %s"
            params.append(args.kind)
        if args.owner:
            query += " AND owner = %s"
            params.append(args.owner)
        if args.state:
            query += " AND state = %s"
            params.append(args.state)
        query += " ORDER BY created DESC"
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    for run_id, kind, owner, state, created, purpose in rows:
        created_str = created.isoformat(timespec="seconds") if created is not None else ""
        print(f"{run_id}\t{kind}\t{owner}\t{state}\t{created_str}\t{purpose or ''}")
    return int(ExitCode.SUCCESS)


def _run_show_command(args: argparse.Namespace) -> int:
    try:
        cm = connect(application_name="rapidpipe-run-show")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run show: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run show: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, owner, purpose, selected_stages, state, "
                "code_revision, image_digest, schema_version, lane, "
                "resource_profile, database_target, max_attempts_per_unit, "
                "auto_promote, created, expires_at, pinned, finished_at, release "
                "FROM runs WHERE id = %s",
                (args.run_id,),
            )
            run_row = cur.fetchone()
            if run_row is None:
                sys.stderr.write(f"rapidpipe run show: no such run: {args.run_id}\n")
                return 1

            columns = [
                "id", "kind", "owner", "purpose", "selected_stages", "state",
                "code_revision", "image_digest", "schema_version", "lane",
                "resource_profile", "database_target", "max_attempts_per_unit",
                "auto_promote", "created", "expires_at", "pinned", "finished_at",
                "release",
            ]
            for column, value in zip(columns, run_row):
                print(f"{column}: {value}")

            cur.execute(
                "SELECT stage, unit_id, state, selected_attempt "
                "FROM units WHERE run = %s ORDER BY stage, unit_id",
                (args.run_id,),
            )
            print("units:")
            for stage, unit_id, state, selected_attempt in cur.fetchall():
                print(f"  {stage}\t{unit_id}\t{state}\t{selected_attempt}")

            cur.execute(
                "SELECT id, stage, unit, disposition, exit_code, started, ended "
                "FROM attempts WHERE run = %s ORDER BY started",
                (args.run_id,),
            )
            print("attempts:")
            for attempt_id, stage, unit, disposition, exit_code, started, ended in cur.fetchall():
                print(
                    f"  {attempt_id}\t{stage}\t{unit}\t{disposition}\t"
                    f"{exit_code}\t{started}\t{ended}")

            # Promotions that selected or unselected one of this run's
            # instances, with how many of their changes did.
            cur.execute(
                """
                SELECT p.id, p.who, p.happened_at, p.reason, count(*)
                FROM promotions p
                JOIN promotion_changes pc ON pc.promotion = p.id
                WHERE EXISTS (
                    SELECT 1 FROM product_instances pi
                    WHERE pi.run = %s
                      AND pi.id IN (pc.before_instance, pc.after_instance))
                GROUP BY p.id, p.who, p.happened_at, p.reason
                ORDER BY p.happened_at, p.id
                """,
                (args.run_id,),
            )
            promotions = cur.fetchall()
            if promotions:
                print("promotions:")
                for promotion_id, who, happened_at, reason, changes in promotions:
                    print(f"  {promotion_id}\t{who}\t{happened_at}\t{reason}\t{changes}")

    return int(ExitCode.SUCCESS)


def _run_local_command(args: argparse.Namespace) -> int:
    if args.stage not in STAGE_NAMES:
        sys.stderr.write(
            f"rapidpipe run local: unknown stage {args.stage!r}; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    module_name = f"rapidpipe.stages.{args.stage.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        sys.stderr.write(
            f"rapidpipe run local: {args.stage!r} is a known stage name but "
            f"{module_name} is not implemented yet\n")
        return int(ExitCode.USAGE)

    declaration = getattr(module, "DECLARATION", None)
    if declaration is None:
        sys.stderr.write(f"rapidpipe run local: {module_name} has no DECLARATION\n")
        return int(ExitCode.USAGE)

    if args.stage == "register":
        try:
            unit_id = _resolve_register_unit_id(
                unit_id_arg=args.unit_id, inputs_location_arg=args.inputs)
        except RegisterUnitIdError as exc:
            sys.stderr.write(f"rapidpipe run local: {exc}\n")
            return int(ExitCode.USAGE)
    elif args.unit_id is None:
        sys.stderr.write("rapidpipe run local: --unit is required\n")
        return int(ExitCode.USAGE)
    else:
        unit_id = args.unit_id

    try:
        cm = connect(application_name="rapidpipe-run-local")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run local: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run local: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            result = run_stage_locally(
                conn,
                run_id=args.run_id,
                stage=args.stage,
                unit_kind=declaration.unit,
                unit_id=unit_id,
                inputs=args.inputs,
                outputs_root=args.outputs_root,
                settings=args.settings,
                python=args.python,
            )
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run local: {exc}\n")
            return int(ExitCode.USAGE)

    print(
        f"attempt={result.attempt_id} exit={result.exit_code} "
        f"disposition={result.disposition} selected={result.selected} "
        f"outputs={result.output_location}")
    return result.exit_code


def _run_submit_command(args: argparse.Namespace) -> int:
    if args.stage not in STAGE_NAMES:
        sys.stderr.write(
            f"rapidpipe run submit: unknown stage {args.stage!r}; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    module_name = f"rapidpipe.stages.{args.stage.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        sys.stderr.write(
            f"rapidpipe run submit: {args.stage!r} is a known stage name but "
            f"{module_name} is not implemented yet\n")
        return int(ExitCode.USAGE)

    declaration = getattr(module, "DECLARATION", None)
    if declaration is None:
        sys.stderr.write(f"rapidpipe run submit: {module_name} has no DECLARATION\n")
        return int(ExitCode.USAGE)

    # register's own --unit is refused (derived, from the manifest it
    # reads); --inputs-from-stage still names the producing unit's own id
    # (admit's/difference's), needed to look up its output location --
    # carried on --unit for this one case, since register's own unit id
    # does not exist yet to disambiguate it from. Checked before any
    # connection is attempted, matching the plain-stage --unit-required
    # check below (unless the location is --inputs-from-stage, which
    # itself needs a connection to resolve).
    if args.stage == "register":
        if args.inputs_from_stage is None:
            try:
                unit_id = _resolve_register_unit_id(
                    unit_id_arg=args.unit_id, inputs_location_arg=args.inputs)
            except RegisterUnitIdError as exc:
                sys.stderr.write(f"rapidpipe run submit: {exc}\n")
                return int(ExitCode.USAGE)
        elif args.unit_id is None:
            sys.stderr.write(
                "rapidpipe run submit: --unit is required together with "
                "--inputs-from-stage for register: it names the producing "
                "unit to resolve, not register's own unit id\n")
            return int(ExitCode.USAGE)
    elif args.unit_id is None:
        sys.stderr.write("rapidpipe run submit: --unit is required\n")
        return int(ExitCode.USAGE)
    else:
        unit_id = args.unit_id

    try:
        cm = connect(application_name="rapidpipe-run-submit")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run submit: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run submit: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            if args.stage == "register" and args.inputs_from_stage is not None:
                inputs_location = launch_batch.resolve_inputs_from_stage(
                    conn, run_id=args.run_id, unit_id=args.unit_id,
                    upstream_stage=args.inputs_from_stage)
                unit_id = _resolve_register_unit_id(
                    unit_id_arg=None, inputs_location_arg=inputs_location)
            elif args.inputs_from_stage is not None:
                inputs_location = launch_batch.resolve_inputs_from_stage(
                    conn, run_id=args.run_id, unit_id=unit_id,
                    upstream_stage=args.inputs_from_stage)
            else:
                inputs_location = args.inputs

            submission = launch_batch.submit_unit(
                conn,
                run_id=args.run_id,
                stage=args.stage,
                unit_kind=declaration.unit,
                unit_id=unit_id,
                inputs_location=inputs_location,
                settings_location=args.settings,
            )
        except RegisterUnitIdError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except MissingEnvironmentVariable as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return 64
        except DependencyIncomplete as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except ReleaseDefinitionRefused as exc:
            # A permanent refusal, not a retryable one: the run's release
            # names a job definition revision that is not ACTIVE.
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return 1
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - Batch/botocore-shaped errors
            if _is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run submit: Batch error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise

    print(
        f"attempt={submission.attempt_id} job={submission.job_id} "
        f"outputs={submission.output_location}")
    return int(ExitCode.SUCCESS)


def _run_reconcile_command(args: argparse.Namespace) -> int:
    try:
        cm = connect(application_name="rapidpipe-run-reconcile")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run reconcile: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run reconcile: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            results = launch_batch.reconcile(conn, run_id=args.run_id)
        except Exception as exc:  # noqa: BLE001 - Batch/botocore-shaped errors
            if _is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run reconcile: Batch error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise

    for result in results:
        print(
            f"attempt={result.attempt_id} job={result.job_id} "
            f"status={result.batch_status} disposition={result.disposition} "
            f"selected={result.selected}")
    return int(ExitCode.SUCCESS)


def _run_cancel_command(args: argparse.Namespace) -> int:
    try:
        cm = connect(application_name="rapidpipe-run-cancel")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run cancel: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run cancel: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            launch_batch.cancel(conn, attempt_id=args.attempt_id, reason=args.reason)
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run cancel: {exc}\n")
            return int(ExitCode.USAGE)
        except LaunchError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run cancel: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - Batch/botocore-shaped errors
            if _is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run cancel: Batch error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise

    return int(ExitCode.SUCCESS)


def _run_model_command(
    name: str,
    action,
    *,
    print_result=None,
) -> int:
    """Shared shape of the run-model subcommands: connect, call
    ``action(conn)``, commit, print; a refusal (any
    :class:`~rapidpipe.runs.repository.RunModelError`) rolls back, prints
    the exception message and exits 64; an AWS-shaped error (``run
    delete``'s S3 calls) exits 75, retryable -- ``delete`` resumes a
    ``deleting`` run."""
    try:
        cm = connect(application_name=f"rapidpipe-run-{name}")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run {name}: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run {name}: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            result = action(conn)
            conn.commit()
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run {name}: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - AWS/botocore-shaped errors
            conn.rollback()
            if _is_batch_error(exc):
                sys.stderr.write(f"rapidpipe run {name}: AWS error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise
        except BaseException:
            conn.rollback()
            raise

    if print_result is not None:
        print_result(result)
    return int(ExitCode.SUCCESS)


def _run_promote_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import promote_run

    who = args.who or getpass.getuser()
    kinds = None
    if args.kinds is not None:
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        if not kinds:
            sys.stderr.write("rapidpipe run promote: --kinds must name at least one kind\n")
            return int(ExitCode.USAGE)
    return _run_model_command(
        "promote",
        lambda conn: promote_run(conn, args.run_id, who, args.reason, kinds=kinds,
                                 allow_unreleased=args.allow_unreleased),
        print_result=print)


def _run_rollback_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import rollback_promotion

    who = args.who or getpass.getuser()
    return _run_model_command(
        "rollback",
        lambda conn: rollback_promotion(conn, args.promotion_id, who, args.reason),
        print_result=print)


def _print_deletion_report(report) -> None:
    print(f"run_id: {report.run_id}")
    print(f"already_deleted: {report.already_deleted}")
    print(f"objects_deleted: {report.objects_deleted}")
    print(f"versions_deleted: {report.versions_deleted}")
    for table, count in report.rows_deleted.items():
        print(f"rows_deleted.{table}: {count}")
    print(f"instances_marked: {report.instances_marked}")


def _run_delete_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.cleanup import delete_run

    requested_by = args.requested_by or getpass.getuser()
    return _run_model_command(
        "delete",
        lambda conn: delete_run(conn, args.run_id, requested_by),
        print_result=_print_deletion_report)


def _run_finish_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import finish_run

    return _run_model_command("finish", lambda conn: finish_run(conn, args.run_id))


def _run_pin_command(args: argparse.Namespace, pinned: bool) -> int:
    from rapidpipe.runs.cleanup import pin_run

    return _run_model_command(
        "pin" if pinned else "unpin", lambda conn: pin_run(conn, args.run_id, pinned))


def _run_command(args: argparse.Namespace) -> int:
    if args.run_command == "create":
        return _run_create_command(args)
    if args.run_command == "list":
        return _run_list_command(args)
    if args.run_command == "show":
        return _run_show_command(args)
    if args.run_command == "local":
        return _run_local_command(args)
    if args.run_command == "submit":
        return _run_submit_command(args)
    if args.run_command == "reconcile":
        return _run_reconcile_command(args)
    if args.run_command == "cancel":
        return _run_cancel_command(args)
    if args.run_command == "promote":
        return _run_promote_command(args)
    if args.run_command == "rollback":
        return _run_rollback_command(args)
    if args.run_command == "delete":
        return _run_delete_command(args)
    if args.run_command == "finish":
        return _run_finish_command(args)
    if args.run_command == "pin":
        return _run_pin_command(args, True)
    if args.run_command == "unpin":
        return _run_pin_command(args, False)
    sys.stderr.write(
        "rapidpipe run: a subcommand is required: create, list, show, local, "
        "submit, reconcile, cancel, promote, rollback, delete, finish, pin, "
        "unpin\n")
    return int(ExitCode.USAGE)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.command == "stage":
        return _run_stage_command(args.name, args.stage_argv)

    if args.command == "selftest":
        return _run_selftest_command(args)

    if args.command == "run":
        return _run_command(args)

    if args.command == "release":
        return release_cli.dispatch(args)

    parser.print_help()
    return int(ExitCode.SUCCESS) if args.command is None else int(ExitCode.USAGE)


if __name__ == "__main__":
    sys.exit(main())
