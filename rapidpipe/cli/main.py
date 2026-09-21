"""The ``rapidpipe`` command-line tool.

``rapidpipe stage <name> ...`` imports ``rapidpipe.stages.<name>`` and calls
its ``main(argv)``, per the stage contract's "Invocation" section: "``rapidpipe
stage <name>`` calls that same entrypoint." The other operations the
specification's "Tools" section describes -- create a run, rerun part of a
run, watch progress, cancel and restart, list and compare runs, promote a
candidate, delete scratch -- are listed here as placeholders only; each
exits 64 until it is built on ``rapidpipe.runs`` and ``rapidpipe.launch``.

Plain argparse; no third-party CLI library.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Sequence

from rapidpipe import __version__
from rapidpipe.stages.contract import STAGE_NAMES, ExitCode

#: Subcommands named in the specification's "Tools" section that are not
#: yet implemented. Each exits 64 if invoked, and is listed in --help as
#: "not yet implemented".
NOT_YET_IMPLEMENTED = ("run", "promote", "delete")


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.command == "stage":
        return _run_stage_command(args.name, args.stage_argv)

    if args.command in NOT_YET_IMPLEMENTED:
        sys.stderr.write(f"rapidpipe {args.command}: not yet implemented\n")
        return int(ExitCode.USAGE)

    parser.print_help()
    return int(ExitCode.SUCCESS) if args.command is None else int(ExitCode.USAGE)


if __name__ == "__main__":
    sys.exit(main())
