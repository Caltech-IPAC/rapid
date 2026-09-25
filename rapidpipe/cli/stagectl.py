"""``rapidpipe stage run|list|describe``: the ``stage`` command group.

``rapidpipe stage run <name> [stage argv...]`` is the stage contract's
"Invocation" form: everything after ``<name>`` is forwarded unchanged to
``rapidpipe.stages.<name>.main(argv)``, including ``-h``, so ``rapidpipe
stage run admit -h`` prints admit's own help. The launcher's older form
``rapidpipe stage <name> ...`` (what ``rapidpipe.launch.batch.submit_unit``
puts in every Batch job's command) keeps working byte for byte:
``rapidpipe.cli.main.main`` rewrites it to ``stage run <name> ...`` before
argparse sees it (:func:`rewrite_legacy_argv`).

``list`` prints every stage name, one per line, with its unit kind when
the stage module imports; ``describe <name>`` prints the stage's
``DECLARATION`` as ``key: value`` lines.

Stage modules are imported with ``importlib.import_module`` at call time,
never at the top of this module, for the same reason ``rapidpipe.cli.main``
does: ``rapidpipe.cli`` decides which stage code loads.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any, Sequence

from rapidpipe.stages.contract import STAGE_NAMES, ExitCode

#: The ``stage`` group's own subcommands; anything else after ``stage`` that
#: is not an option is taken as a stage name (the legacy form).
SUBCOMMANDS = ("run", "list", "describe")


def rewrite_legacy_argv(argv: list[str]) -> list[str]:
    """``stage <name> ...`` -> ``stage run <name> ...``; anything else unchanged.

    Applied when ``argv[0] == "stage"`` and ``argv[1]`` is neither one of
    :data:`SUBCOMMANDS` nor an option (``-h``/``--help`` reach the group's
    own help). A known stage name and an unknown word alike are rewritten,
    so an unknown name still gets ``_run_stage_command``'s exit-64 message
    rather than an argparse ``invalid choice``.
    """
    if (len(argv) >= 2 and argv[0] == "stage"
            and argv[1] not in SUBCOMMANDS and not argv[1].startswith("-")):
        return ["stage", "run", *argv[1:]]
    return argv


def add_parsers(top_level: Any) -> argparse.ArgumentParser:
    """Add the ``stage`` group, with ``run``, ``list`` and ``describe``, to
    the top-level subparsers action ``top_level``; return the group parser."""
    stage_parser = top_level.add_parser(
        "stage",
        help="Run, list or describe stages (rapidpipe stage <name> ... is "
             "rapidpipe stage run <name> ...).",
        description="Run one stage in this process, list the stages, or "
                    "describe one. 'rapidpipe stage <name> ...' (the form "
                    "the Batch launcher submits) is the same as "
                    "'rapidpipe stage run <name> ...'.")
    stage_parser.set_defaults(stage_group_parser=stage_parser)
    subparsers = stage_parser.add_subparsers(dest="stage_command", metavar="{run,list,describe}")

    run_parser = subparsers.add_parser(
        "run",
        help="Run one stage: rapidpipe stage run <name> --run ... --unit ... "
             "--attempt ... --inputs ... --outputs ... [--settings ...] [--dry-run]",
        description="Run one stage attempt in this process. Everything after "
                    "<name> is forwarded unchanged to the stage's own "
                    "entrypoint; 'rapidpipe stage run <name> -h' prints the "
                    "stage's own help. 'rapidpipe stage <name> ...' is the "
                    "same command.")
    run_parser.add_argument(
        "name", nargs="?", help=f"Stage name: one of {', '.join(STAGE_NAMES)}.")
    run_parser.add_argument(
        "stage_argv", nargs=argparse.REMAINDER,
        help="Arguments forwarded to the stage's own entrypoint.")

    subparsers.add_parser(
        "list", help="List the stage names, with each stage's unit kind.",
        description="Print every stage name, one per line, with its unit kind "
                    "when the stage is implemented.")

    describe_parser = subparsers.add_parser(
        "describe", help="Print one stage's declaration.",
        description="Print a stage's DECLARATION (unit kind, arguments, "
                    "inputs, outputs, settings, database access, exit codes) "
                    "as key: value lines.")
    describe_parser.add_argument("name", choices=STAGE_NAMES, help="Stage name.")
    return stage_parser


def _import_stage(name: str) -> Any:
    return importlib.import_module(f"rapidpipe.stages.{name.replace('-', '_')}")


def _declaration_or_none(name: str) -> Any:
    try:
        module = _import_stage(name)
    except ModuleNotFoundError:
        return None
    return getattr(module, "DECLARATION", None)


def _format(value: Any) -> str:
    if isinstance(value, (tuple, list)):
        return ", ".join(_format(v) for v in value) if value else "(none)"
    if isinstance(value, ExitCode):
        return f"{int(value)} {value.name}"
    if value is None:
        return "(none)"
    return str(value)


def declaration_lines(declaration: Any) -> list[str]:
    """``key: value`` lines for a :class:`~rapidpipe.stages.contract.StageDeclaration`.

    Nested dicts (``argument_schema``, ``resource_defaults``) are flattened
    to ``outer.inner: value``; tuples are comma-separated; exit codes print
    as ``<number> <NAME>``.
    """
    lines = [
        f"name: {declaration.name}",
        f"unit: {declaration.unit}",
    ]
    for key, value in sorted(declaration.argument_schema.items()):
        lines.append(f"arguments.{key}: {_format(value)}")
    lines += [
        f"inputs: {_format(declaration.consumes)}",
        f"outputs: {_format(declaration.produces)}",
        f"settings: {_format(declaration.settings_schema_path)}",
        f"database_access: {declaration.database_access}",
    ]
    for key, value in sorted(declaration.resource_defaults.items()):
        lines.append(f"resources.{key}: {_format(value)}")
    lines.append(f"exit_codes: {_format(tuple(declaration.supported_exit_codes))}")
    return lines


def _list_command() -> int:
    for name in STAGE_NAMES:
        declaration = _declaration_or_none(name)
        if declaration is None:
            print(f"{name}\t(not implemented)")
        else:
            print(f"{name}\t{declaration.unit}")
    return int(ExitCode.SUCCESS)


def _describe_command(name: str) -> int:
    declaration = _declaration_or_none(name)
    if declaration is None:
        sys.stderr.write(
            f"rapidpipe stage describe: {name!r} is a known stage name but "
            "has no implemented module or DECLARATION\n")
        return int(ExitCode.USAGE)
    for line in declaration_lines(declaration):
        print(line)
    return int(ExitCode.SUCCESS)


def dispatch(args: argparse.Namespace, run_stage: Any) -> int:
    """Run the ``stage`` subcommand ``args`` names.

    ``run_stage(name, argv)`` is ``rapidpipe.cli.main._run_stage_command``,
    passed in rather than imported so this module never imports ``main``
    at load time. No subcommand prints the group help and exits 64,
    keeping the old "a stage name is required" message.
    """
    command = getattr(args, "stage_command", None)
    if command == "run":
        return run_stage(args.name, args.stage_argv)
    if command == "list":
        return _list_command()
    if command == "describe":
        return _describe_command(args.name)
    args.stage_group_parser.print_help(sys.stderr)
    sys.stderr.write(
        "rapidpipe stage: a subcommand or stage name is required; known "
        f"stages: {', '.join(STAGE_NAMES)}\n")
    return int(ExitCode.USAGE)


__all__: Sequence[str] = ("add_parsers", "dispatch", "rewrite_legacy_argv", "declaration_lines")
