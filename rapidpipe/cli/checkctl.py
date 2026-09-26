"""``rapidpipe check list|run|show``: candidate checks (supervisor step 6,
2026-09-24, R6).

- ``list`` -- the registered checks and the shipped check policies (no
  database);
- ``run <run> [--check-policy P] [--instance I] [--check NAME@V] [--param k=v]...
  [--who W]`` -- run every applicable check of the policy (``--check-policy``,
  an alias ``--policy`` is also accepted, > the run's ``check_policy_ref`` >
  ``rebuild-trial@1``) over the run's
  candidates (instances from selected attempts, any custody), or the one
  instance / the one check; record one ``checks`` row per result; print
  one line per result. ``--param`` overrides the policy's params for the
  named ``--check`` only; such a row does not count for promotion under
  the policy (plan-review amendment A2). Exit 0 when every result passed,
  1 when any failed, 64 on a usage error;
- ``show <run> [--instance I]`` -- the recorded results, newest first, each
  line prefixed ``id=<check row id> at=<happened_at>``.

A result line is ``instance=<id> kind=<kind> key=<compact json>
check=<name>@<v> required=<true|false> outcome=<passed|failed> <summary>``.

Checks run launcher-side against the database (``rapidpipe.cli.main.
connect``, looked up at call time as ``runctl`` does); no image change.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.stages.contract import ExitCode


def add_parser(subparsers: Any) -> None:
    check_parser = subparsers.add_parser(
        "check", help="List, run and show candidate checks.",
        description="List registered checks and shipped check policies, run a "
                    "policy's checks over a run's candidates, show recorded results.")
    check_parser.set_defaults(check_group_parser=check_parser)
    check_subparsers = check_parser.add_subparsers(dest="check_command")

    check_subparsers.add_parser(
        "list", help="List registered checks and shipped check policies.",
        description="List registered checks and shipped check policies.")

    run_parser = check_subparsers.add_parser(
        "run", help="Run a policy's checks over a run's candidates and record them.",
        description="Run every applicable check of the policy over the run's "
                    "candidate instances (selected attempts), record each result, "
                    "and print one line per result. Exit 0 all passed, 1 any failed.")
    run_parser.add_argument("run_id")
    run_parser.add_argument(
        "--check-policy", "--policy", dest="policy", default=None, metavar="NAME@VERSION",
        help="Check policy (default: the run's check policy, else rebuild-trial@1); "
             "the same policy reference `run promote --check-policy` takes. "
             "--policy is an accepted alias.")
    run_parser.add_argument(
        "--instance", default=None, metavar="INSTANCE_ID",
        help="Check only this candidate instance of the run.")
    run_parser.add_argument(
        "--check", default=None, metavar="NAME@VERSION", dest="check_ref",
        help="Run only this check of the policy.")
    run_parser.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE",
        help="Override one of --check's policy params (JSON value, else a string); "
             "repeatable. The result does not count for promotion under the policy.")
    run_parser.add_argument(
        "--who", default=None, help="Recorded in each result's detail (default: the current user).")

    show_parser = check_subparsers.add_parser(
        "show", help="Show a run's recorded check results, newest first.",
        description="Show a run's recorded check results, newest first.")
    show_parser.add_argument("run_id")
    show_parser.add_argument(
        "--instance", default=None, metavar="INSTANCE_ID",
        help="Show only this instance's results.")


def _usage(message: str) -> int:
    sys.stderr.write(f"rapidpipe check: {message}\n")
    return int(ExitCode.USAGE)


def _list_command() -> int:
    from rapidpipe.checks.policy import load_policy, shipped_policies
    from rapidpipe.checks.registry import registered_checks

    for registered in registered_checks():
        print(f"check={registered.ref} kind={registered.kind} "
              f"params={','.join(registered.params)} {registered.description}".rstrip())
    for ref in shipped_policies():
        policy = load_policy(ref)
        checks = ",".join(
            f"{c.ref}({'required' if c.required else 'advisory'})" for c in policy.checks)
        print(f"policy={policy.ref} approval={policy.approval} "
              f"approved_by={policy.approved_by or '-'} "
              f"auto_promote={'true' if policy.auto_promote else 'false'} checks={checks}")
    return int(ExitCode.SUCCESS)


def _parse_params(values: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for value in values:
        key, sep, raw = value.partition("=")
        if not sep or not key:
            raise ValueError(f"--param expects KEY=VALUE, got {value!r}")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def _connect(command: str):
    from rapidpipe.cli import main as _main

    return _main.connect(application_name=f"rapidpipe-check-{command}")


def _require_run(conn, run_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM runs WHERE id = %s", (run_id,))
        if cur.fetchone() is None:
            raise LookupError(f"run {run_id} does not exist")


def _with_connection(command: str, body) -> int:
    from rapidpipe.checks.registry import CheckError
    from rapidpipe.checks.runner import CheckUsageError

    try:
        cm = _connect(command)
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe check {command}: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe check {command}: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)
    with cm as conn:
        try:
            return body(conn)
        except (CheckError, CheckUsageError, LookupError) as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe check {command}: {exc}\n")
            return int(ExitCode.USAGE)
        except BaseException:
            conn.rollback()
            raise


def _run_command(args: argparse.Namespace) -> int:
    from rapidpipe.checks.runner import resolve_run_policy, run_policy_checks

    try:
        overrides = _parse_params(args.param)
    except ValueError as exc:
        return _usage(f"run: {exc}")
    if overrides and args.check_ref is None:
        return _usage("run: --param needs --check: overrides apply to one named check")
    who = args.who or getpass.getuser()

    def body(conn) -> int:
        _require_run(conn, args.run_id)
        policy = resolve_run_policy(conn, args.run_id, args.policy)
        results = run_policy_checks(
            conn, args.run_id, policy, instance=args.instance, check=args.check_ref,
            param_overrides=overrides or None, who=who)
        conn.commit()
        if not results:
            print(f"no applicable checks for run {args.run_id} under policy {policy.ref}")
        for result in results:
            print(result.line())
        return 1 if any(r.outcome != "passed" for r in results) else int(ExitCode.SUCCESS)

    return _with_connection("run", body)


def _show_command(args: argparse.Namespace) -> int:
    from rapidpipe.checks.runner import recorded_checks

    def body(conn) -> int:
        _require_run(conn, args.run_id)
        rows = recorded_checks(conn, args.run_id, instance=args.instance)
        for row in rows:
            at = row.happened_at.isoformat() if row.happened_at else "-"
            print(f"id={row.id} at={at} {row.line()}")
        return int(ExitCode.SUCCESS)

    return _with_connection("show", body)


def dispatch(args: argparse.Namespace) -> int:
    if args.check_command == "list":
        return _list_command()
    if args.check_command == "run":
        return _run_command(args)
    if args.check_command == "show":
        return _show_command(args)
    args.check_group_parser.print_help(sys.stderr)
    sys.stderr.write("rapidpipe check: a subcommand is required: list, run, show\n")
    return int(ExitCode.USAGE)
