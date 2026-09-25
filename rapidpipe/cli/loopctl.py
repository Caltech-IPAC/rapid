"""``rapidpipe loop run|plan|show``: the scheduled processing-date loop.

The loop itself is ``rapidpipe.launch.loop``; this module parses the
command line and hands the loop the CLI's own code paths
(:class:`rapidpipe.launch.loop.LoopTools`): ``run start``'s walk
(``runctl.walk_unit``), ``run create --release`` (``main.create_run_record``),
``run create --seed <run> --only-failed`` (``main.create_only_failed_run``)
and the storage helper ``run inputs`` uses (``runctl._Storage``), because
``rapidpipe.launch`` may not import ``rapidpipe.cli``.

Exit codes: 0 every processed date complete; 1 a date failed (later dates
not started); 64 a usage error or refusal (a malformed spec, a release that
is not complete, any ``RunModelError``); 75 a timeout, a transient error, or
another loop holding the schedule's advisory lock (rerun the same command to
resume the open date).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from typing import Any

from rapidpipe.cli import runctl
from rapidpipe.launch import loop as launch_loop
from rapidpipe.stages.contract import ExitCode


def _date(text: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from exc


def add_parser(subparsers: Any) -> None:
    """Add ``loop`` with ``run``, ``plan`` and ``show`` to the top-level subparsers."""
    loop_parser = subparsers.add_parser(
        "loop", help="Run, plan and show the scheduled processing-date loop.",
        description="The processing-date loop: one production run per date of a "
                    "loop spec (TOML, local or s3://), walked through the run's "
                    "stages on Batch, promoted and recorded in loop_dates.")
    loop_parser.set_defaults(loop_group_parser=loop_parser)
    sub = loop_parser.add_subparsers(dest="loop_command")

    run = sub.add_parser(
        "run", help="Process every spec date not yet complete, in order.",
        description="For each date of the spec (or each --date) whose loop_dates "
                    "row is absent or open (or failed on a refusal with no failed "
                    "unit, reopened on its run), in spec order: create (or resume) the "
                    "date's production run under the spec's release, walk admit.."
                    "load per detector image, maintain, crossmatch/statistics/prune "
                    "per field (the base catalog is the previous complete date's "
                    "association set for the field), alerts per image, promote, "
                    "finish, and record the date. Exit 0 all complete, 1 a date "
                    "failed, 64 a refusal, 75 a timeout (rerun to resume).")
    run.add_argument("--spec", required=True, help="The loop spec's location.")
    run.add_argument("--date", action="append", type=_date, default=[], dest="dates",
                     metavar="YYYY-MM-DD", help="Only this spec date. Repeatable.")
    run.add_argument("--retry-failed", action="store_true",
                     help="Reopen a failed date on a new run seeded from its run (run "
                          "create --seed <run> --only-failed: the failed units re-run, "
                          "the completed ones inherited) and walk it (default: a failed "
                          "date stops the loop with exit 1).")
    run.add_argument("--dry-run", action="store_true",
                     help="Print the plan (as 'loop plan') and change nothing.")
    run.add_argument("--interval", type=runctl._positive_seconds, default=30.0,
                     help="Seconds between reconcile polls (default 30).")
    run.add_argument("--timeout", type=runctl._positive_seconds, default=14400.0,
                     help="Seconds to wait for one stage's attempts before exiting 75 "
                          "(default 14400).")

    plan = sub.add_parser(
        "plan", help="Print what 'loop run' would do for each spec date.",
        description="Per spec date: whether its run would be created, resumed or "
                    "skipped, its detector-image units, and the previous complete "
                    "date whose association sets would be the fields' bases.")
    plan.add_argument("--spec", required=True, help="The loop spec's location.")

    show = sub.add_parser(
        "show", help="Print a schedule's loop_dates rows.",
        description="One line per processing date of the schedule: state, run, "
                    "promotion, start and end; --json adds each row's record.")
    show.add_argument("schedule")
    show.add_argument("--json", action="store_true", help="Also print each record as JSON.")


def _tools() -> launch_loop.LoopTools:
    main = runctl._main_module()
    return launch_loop.LoopTools(
        walk=runctl.walk_unit, create_run=main.create_run_record,
        storage=runctl._Storage(), inputs_root=runctl.inputs_root,
        create_seeded_run=lambda conn, seed: main.create_only_failed_run(conn, seed)[0])


def _usage_errors() -> tuple[type[BaseException], ...]:
    main = runctl._main_module()
    return (launch_loop.LoopSpecError, launch_loop.LoopError, main.ReleaseNotComplete)


def _run(args: argparse.Namespace) -> int:
    try:
        spec = launch_loop.load_spec(args.spec)
    except launch_loop.LoopSpecError as exc:
        sys.stderr.write(f"rapidpipe loop run: {exc}\n")
        return int(ExitCode.USAGE)
    return runctl._with_connection(
        "run", lambda conn: launch_loop.run_loop(
            conn, spec, _tools(), dates=args.dates, dry_run=args.dry_run,
            interval=args.interval, timeout=args.timeout, retry_failed=args.retry_failed),
        prog="rapidpipe loop", usage_errors=_usage_errors())


def _plan(args: argparse.Namespace) -> int:
    try:
        spec = launch_loop.load_spec(args.spec)
    except launch_loop.LoopSpecError as exc:
        sys.stderr.write(f"rapidpipe loop plan: {exc}\n")
        return int(ExitCode.USAGE)

    def body(conn) -> int:
        launch_loop.plan(conn, spec, _tools())
        conn.rollback()
        return int(ExitCode.SUCCESS)

    return runctl._with_connection("plan", body, prog="rapidpipe loop",
                                   usage_errors=_usage_errors())


def _show(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        code = launch_loop.show(conn, args.schedule, as_json=args.json,
                                out=lambda line: print(line, flush=True))
        conn.rollback()
        return code

    return runctl._with_connection("show", body, prog="rapidpipe loop",
                                   usage_errors=_usage_errors())


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "loop_command", None)
    if command == "run":
        return _run(args)
    if command == "plan":
        return _plan(args)
    if command == "show":
        return _show(args)
    args.loop_group_parser.print_help(sys.stderr)
    sys.stderr.write("rapidpipe loop: a subcommand is required: run, plan, show\n")
    return int(ExitCode.USAGE)
