"""The ``rapidpipe`` command-line tool.

``rapidpipe stage <name> ...`` imports ``rapidpipe.stages.<name>`` and calls
its ``main(argv)``, per the stage contract's "Invocation" section: "``rapidpipe
stage <name>`` calls that same entrypoint." ``rapidpipe run create/list/show/
local`` operates on ``rapidpipe.runs.repository`` directly -- create a run,
list and inspect runs, and run one stage attempt locally through
``rapidpipe.runs.local``. The specification's "Tools" section names two
more operations this module does not yet implement, ``promote`` and
``delete``, plus ``run`` for Batch (rerun part of a run, watch progress,
cancel and restart); each remaining placeholder exits 64 until it is
built on ``rapidpipe.launch``.

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
import os
import subprocess
import sys
from typing import Any, Sequence

from rapidpipe import __version__
from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.db.connection import connect as _default_connect
from rapidpipe.runs.local import run_stage_locally
from rapidpipe.runs.repository import RunModelError
from rapidpipe.stages.contract import STAGE_NAMES, ExitCode

#: Subcommands named in the specification's "Tools" section that are not
#: yet implemented. Each exits 64 if invoked, and is listed in --help as
#: "not yet implemented".
NOT_YET_IMPLEMENTED = ("promote", "delete")

#: Module-level indirection so tests can monkeypatch
#: ``rapidpipe.cli.main.connect`` without reaching into
#: ``rapidpipe.db.connection`` and affecting other callers -- the same
#: pattern ``rapidpipe.stages.register`` uses for the same reason.
connect = _default_connect


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

    run_parser = subparsers.add_parser(
        "run", help="Create, list, inspect and locally run runs.")
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
    local_parser.add_argument("--unit", required=True, dest="unit_id")
    local_parser.add_argument("--inputs", required=True)
    local_parser.add_argument("--outputs-root", required=True)
    local_parser.add_argument("--settings", default=None)
    local_parser.add_argument("--python", default=sys.executable)

    for name in NOT_YET_IMPLEMENTED:
        subparsers.add_parser(name, help="Not yet implemented.", add_help=False)

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

            run_id = create_run(
                conn,
                kind=args.kind,
                owner=owner,
                purpose=args.purpose,
                selected_stages=stages,
                code_revision=_source_revision_or_unknown(),
                image_digest=os.environ.get("RAPIDPIPE_IMAGE_DIGEST"),
                schema_version=schema_version,
                settings_overlay_ref=args.settings_overlay_ref,
                input_selection_ref=args.input_selection_ref,
                lane=args.lane,
                resource_profile=args.profile,
                database_target=args.db_target or os.environ.get("PGDATABASE", ""),
                max_attempts_per_unit=args.max_attempts,
                auto_promote=False,
                check_policy_ref=None,
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
                "auto_promote, created "
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
                "auto_promote", "created",
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
                unit_id=args.unit_id,
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


def _run_command(args: argparse.Namespace) -> int:
    if args.run_command == "create":
        return _run_create_command(args)
    if args.run_command == "list":
        return _run_list_command(args)
    if args.run_command == "show":
        return _run_show_command(args)
    if args.run_command == "local":
        return _run_local_command(args)
    sys.stderr.write(
        "rapidpipe run: a subcommand is required: create, list, show, local\n")
    return int(ExitCode.USAGE)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.command == "stage":
        return _run_stage_command(args.name, args.stage_argv)

    if args.command == "run":
        return _run_command(args)

    if args.command in NOT_YET_IMPLEMENTED:
        sys.stderr.write(f"rapidpipe {args.command}: not yet implemented\n")
        return int(ExitCode.USAGE)

    parser.print_help()
    return int(ExitCode.SUCCESS) if args.command is None else int(ExitCode.USAGE)


if __name__ == "__main__":
    sys.exit(main())
