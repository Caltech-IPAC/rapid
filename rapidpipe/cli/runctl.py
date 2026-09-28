"""``rapidpipe run start|status|inputs|compare|expire``: run management.

The Batch-level run management the specification's "Tools" section names,
over the one-attempt primitives ``rapidpipe.cli.main`` already has
(``run submit``, ``run reconcile``):

- ``start`` -- walk a run's selected stages for one unit: skip what is
  complete, submit what is not (``rapidpipe.launch.batch.submit_unit``),
  wait for each attempt by polling ``reconcile``, and move on
  (``rapidpipe.launch.walk.start``);
- ``status`` -- reconcile, then one line per unit;
- ``inputs`` -- compose an input set: a template input-set manifest's
  reference entries plus one producer attempt's output entry, copied into
  one prefix (manifest member paths are relative and contained, so an
  input set must live under one prefix; ``rapidpipe.launch.walk.compose_inputs``);
- ``compare`` -- two runs side by side;
- ``expire`` -- ``rapidpipe.runs.cleanup.expire_runs``.

Database connections: every command here opens its connection through
``rapidpipe.cli.main.connect``, looked up at call time
(``from rapidpipe.cli import main as _main; _main.connect(...)``), never
through a name bound at import. A test that monkeypatches
``rapidpipe.cli.main.connect`` therefore covers these commands too, and
this module never imports ``main`` at load time (``main`` imports this
module).

This module parses, prints and maps exit codes; the walk and the
composer are ``rapidpipe.launch.walk``, whose seams (``sleep``, ``now``,
``_reconcile``, ``_run_row``, ``_unit_row``) reach these commands too.
The SQL only these commands read is kept in small module-level functions
(:func:`_status_rows`, :func:`_compare_units`, :func:`_compare_instances`,
:func:`_timings_rows`) so the database-free unit tests can monkeypatch
them.

Exit codes, as for the rest of ``rapidpipe run`` (tool.md §Exit codes):
0 success; 64 a usage error or a refusal (any other ``RunModelError``);
75 transient (an AWS-shaped error, the database unavailable, or
``start``'s ``--timeout``); 1 a unit failed, or a policy refusal
(``POLICY_REFUSALS``). ``status`` also exits 2 (still running,
``ExitCode.INCOMPLETE``) when something is still running.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.launch import walk as launch_walk
from rapidpipe.launch.batch import (
    DependencyIncomplete,
    MissingEnvironmentVariable,
    ReleaseDefinitionRefused,
)
from rapidpipe.launch.walk import RegisterUnitIdError
from rapidpipe.products.manifest import ManifestError
from rapidpipe.products.storage import LocationError, Storage
from rapidpipe.runs.inputs import InputsRefused
from rapidpipe.runs.repository import POLICY_REFUSALS, TERMINAL_UNIT_STATES, RunModelError
from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.stages.contract import STAGE_NAMES

COMMANDS = ("start", "status", "inputs", "compare", "expire", "timings")

_STATUS_STILL_RUNNING = ExitCode.INCOMPLETE


_Exit = CommandExit
_Storage = Storage


# ======================================================================
# Parsers
# ======================================================================

def _iso8601(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from exc


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text!r}")
    return value


def add_parsers(run_subparsers: Any) -> None:
    """Add ``start``, ``status``, ``inputs``, ``compare`` and ``expire``
    beneath ``rapidpipe run``'s subparsers action."""
    start = run_subparsers.add_parser(
        "start",
        help="Run a unit through the run's selected stages on Batch, waiting "
             "for each attempt.",
        description="Walk the run's selected stages in order (or only --stage) "
                    "for one unit: skip a complete stage, submit the next "
                    "attempt of any other, and wait for it by reconciling "
                    "every --interval seconds. A stage's inputs are, in "
                    "order: --inputs <stage>=<loc> (an unprefixed --inputs "
                    "is the first stage's), an input set composed from "
                    "--template <stage>=<loc>, else, for a unit seeded by run "
                    "create --only-failed, the seed attempt's recorded inputs "
                    "(and settings), else the selected output of "
                    "the nearest preceding non-register stage. A unit a "
                    "transient or lost attempt returns to ready gets another "
                    "attempt, within the run's allowance. Exit 0 when every "
                    "stage is complete, 1 when a unit is failed or cancelled, "
                    "75 on --timeout (rerun the printed command to continue).")
    start.add_argument("run_id", help="The run.")
    start.add_argument(
        "--unit", required=True, dest="unit_id",
        help="The unit id (register's own unit id is derived from the manifest "
             "it reads).")
    start.add_argument(
        "--stage", default=None,
        help="Only this stage: its first occurrence in the run's selected "
             "stages that is not yet complete.")
    start.add_argument(
        "--inputs", action="append", default=[], metavar="[STAGE=]LOC",
        help="A stage's input location; unprefixed applies to the first stage "
             "only. Repeatable.")
    start.add_argument(
        "--settings", action="append", default=[], metavar="[STAGE=]LOC",
        help="A stage's settings file; unprefixed applies to the first stage "
             "only. Repeatable.")
    start.add_argument(
        "--template", action="append", default=[], metavar="STAGE=LOC",
        help="Compose STAGE's input set from this template input-set manifest "
             "and the preceding stage's output (see 'run inputs'). Repeatable. "
             "Refused for register, whose inputs are always the producing "
             "stage's output.")
    start.add_argument(
        "--no-wait", action="store_true",
        help="Submit the first stage that needs it, print the command that "
             "continues, and exit 0.")
    start.add_argument(
        "--interval", type=_positive_seconds, default=30.0,
        help="Seconds between reconcile polls (default 30).")
    start.add_argument(
        "--timeout", type=_positive_seconds, default=14400.0,
        help="Seconds to wait for one attempt before exiting 75 (default 14400).")
    start.add_argument(
        "--profile", action="store_true", default=False,
        help="Profile each submitted stage body under cProfile; refused on "
             "a production run (exit 64), since profiles land in the "
             "attempt's own outputs prefix, which for production is the "
             "products bucket.")

    status = run_subparsers.add_parser(
        "status", help="Reconcile a run, then print one line per unit.",
        description="Reconcile the run's unresolved Batch attempts, then print "
                    "stage, unit, state, selected attempt, last attempt, last "
                    "job and its disposition for every unit. Exit 0 when "
                    "every unit is complete, 1 when any failed or was "
                    "cancelled, 2 (still running) when something is still "
                    "running or the run has no units.")
    status.add_argument("run_id", help="The run.")
    status.add_argument(
        "--watch", action="store_true",
        help="Repeat every --interval seconds until every unit is terminal.")
    status.add_argument(
        "--interval", type=_positive_seconds, default=30.0,
        help="Seconds between --watch polls (default 30).")

    inputs = run_subparsers.add_parser(
        "inputs", help="Compose a unit's input set from a template and a producer's output.",
        description="Copy a template input-set manifest's entries and one "
                    "entry of the producer unit's selected output (--kind, "
                    "default l2-image, under l2/) into one prefix, verify the "
                    "copied sizes, write its manifest.json, bind the unit's "
                    "inputs, and print inputs=<dest>. Refuses to overwrite an "
                    "existing <dest>/manifest.json.")
    inputs.add_argument("run_id", help="The run.")
    inputs.add_argument("stage", choices=STAGE_NAMES, help="The stage that will read the input set.")
    inputs.add_argument("--unit", required=True, dest="unit_id", help="The unit id.")
    inputs.add_argument(
        "--from-stage", required=True, dest="from_stage", choices=STAGE_NAMES,
        help="The producing stage whose selected attempt supplies the entry.")
    inputs.add_argument(
        "--template", required=True,
        help="Location (local or s3://) of the template input set's manifest.json.")
    inputs.add_argument(
        "--dest", default=None,
        help="Where to write the input set (default: <scratch outputs root>"
             "/runs/<run>/inputs/<stage>/<unit>, for every run kind: an input "
             "set is a staged working copy, not a product). Must be under "
             "<scratch outputs root>/runs/<run>/inputs/.")
    inputs.add_argument(
        "--kind", default="l2-image",
        help="The producer output entry kind to take (default l2-image).")

    compare = run_subparsers.add_parser(
        "compare", help="Compare two runs: dispositions, products, settings.",
        description="Print, per stage and unit, the two runs' dispositions and "
                    "settings hashes; per product kind and logical key, each "
                    "run's instance ids; the runs' settings overlays; then "
                    "'same' or 'different'. Exit 0 same, 1 different, 64 a "
                    "run not found.")
    compare.add_argument("run_a", help="The first run.")
    compare.add_argument("run_b", help="The second run.")

    expire = run_subparsers.add_parser(
        "expire", help="Delete every expired, unpinned scratch run.",
        description="Delete every scratch run past its expires_at that is not "
                    "pinned, printing one deletion report per run. Honours "
                    "RAPIDPIPE_CLEANUP_ROLE_ARN.")
    expire.add_argument(
        "--now", type=_iso8601, default=None,
        help="Treat this ISO 8601 time as now (default: the database's now()).")

    timings = run_subparsers.add_parser(
        "timings", help="Read-only: queue, execution and orchestration timings per attempt.",
        description="Print one row per attempt of the run: stage, unit, attempt, "
                    "disposition, and the durations Batch's own job timestamps "
                    "(reconcile records them in scheduler_metadata) and the "
                    "attempts table give: queue_s (Batch started - Batch "
                    "created), exec_s (Batch stopped - Batch started), "
                    "fetch_s/body_s (the stage's own execution record's timing, "
                    "when it wrote one; reconcile copies it alongside Batch's "
                    "own timestamps), publish_s (always '-': the stage writes "
                    "its execution record before publishing, so this phase "
                    "never reaches it; only that stage's own final log line "
                    "has it), reconcile_lag_s (this attempt's ended - Batch "
                    "stopped), and over_30m (exec_s > 1800s). An attempt with "
                    "no Batch metadata (an older row, or a local run) prints "
                    "'-' throughout. Then a per-stage summary: count, median, "
                    "p90 and max of exec_s, and how many exceeded 30 minutes. "
                    "Data on stdout only; exits 0, or 64 for an unknown run.")
    timings.add_argument("run_id", help="The run.")
    timings.add_argument(
        "--stage", default=None, choices=STAGE_NAMES,
        help="Only this stage's attempts.")
    timings.add_argument(
        "--json", action="store_true",
        help="One JSON object ({'attempts': [...], 'stages': [...]}) instead "
             "of tab-separated text.")


# ======================================================================
# Shared plumbing
# ======================================================================

def _main_module():
    from rapidpipe.cli import main as _main

    return _main


def _connect(command: str):
    """``rapidpipe.cli.main.connect`` for ``run <command>``, or :class:`_Exit`."""
    try:
        return _main_module().connect(application_name=f"rapidpipe-run-{command}")
    except ConnectionConfigError as exc:
        raise _Exit(int(ExitCode.USAGE), f"database configuration error: {exc}") from exc
    except ConnectionUnavailable as exc:
        raise _Exit(int(ExitCode.TRANSIENT_FAILURE), f"database unavailable: {exc}") from exc


def _with_connection(command: str, body: Callable[[Any], int], *,
                     prog: str = "rapidpipe run",
                     usage_errors: tuple[type[BaseException], ...] = ()) -> int:
    """Connect, run ``body(conn)``, and map exceptions to exit codes.

    ``body`` commits what it wants kept; anything raised rolls back.
    ``prog`` prefixes the messages (``rapidpipe loop`` shares this);
    ``usage_errors`` are further exception types that exit 64; a policy
    refusal exits 1 (tool.md §Exit codes).
    """
    try:
        cm = _connect(command)
    except _Exit as exc:
        sys.stderr.write(f"{prog} {command}: {exc}\n")
        return exc.code

    main = _main_module()
    with cm as conn:
        try:
            return body(conn)
        except _Exit as exc:
            conn.rollback()
            sys.stderr.write(f"{prog} {command}: {exc}\n")
            return exc.code
        except InputsRefused as exc:
            conn.rollback()
            sys.stderr.write(f"{prog} {command}: {exc}\n")
            return int(ExitCode.INPUT_REJECTED)
        except POLICY_REFUSALS as exc:
            conn.rollback()
            sys.stderr.write(f"{prog} {command}: {exc}\n")
            return int(ExitCode.FAILURE)
        except (RunModelError, DependencyIncomplete, MissingEnvironmentVariable,
                RegisterUnitIdError, LocationError, ManifestError, *usage_errors) as exc:
            conn.rollback()
            sys.stderr.write(f"{prog} {command}: {exc}\n")
            return int(ExitCode.USAGE)
        except ReleaseDefinitionRefused as exc:
            conn.rollback()
            sys.stderr.write(f"{prog} {command}: {exc}\n")
            return int(ExitCode.FAILURE)
        except Exception as exc:  # noqa: BLE001 - AWS/botocore-shaped errors
            if main._is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"{prog} {command}: AWS error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            conn.rollback()
            raise


# ======================================================================
# run inputs
# ======================================================================

def _inputs_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        launch_walk.compose_inputs(
            conn, run_id=args.run_id, stage=args.stage, unit_id=args.unit_id,
            from_stage=args.from_stage, template=args.template, dest=args.dest,
            kind=args.kind)
        return int(ExitCode.SUCCESS)

    return _with_connection("inputs", body)


# ======================================================================
# run start
# ======================================================================

def _start_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        try:
            return launch_walk.start(conn, args)
        except _Exit as exc:
            if exc.code == int(ExitCode.TRANSIENT_FAILURE):
                print(f"run={args.run_id} state=timeout", flush=True)
            raise

    return _with_connection("start", body)


# ======================================================================
# run status
# ======================================================================

_STATUS_COLUMNS = ("stage", "unit", "state", "selected_attempt", "last_attempt",
                   "last_job", "disposition")


def _status_rows(conn, run_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.stage, u.unit_id, u.state, u.selected_attempt,
                   a.id, a.scheduler_job_id, a.disposition
            FROM units u
            LEFT JOIN LATERAL (
                SELECT id, scheduler_job_id, disposition
                FROM attempts WHERE unit = u.id
                ORDER BY started DESC, id DESC LIMIT 1) a ON true
            WHERE u.run = %s
            ORDER BY u.created, u.stage, u.unit_id
            """,
            (run_id,))
        return cur.fetchall()


def _status_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        launch_walk.require_run(conn, args.run_id)
        while True:
            launch_walk.reconcile_run(conn, args.run_id)
            rows = _status_rows(conn, args.run_id)
            print("\t".join(_STATUS_COLUMNS))
            for row in rows:
                print("\t".join("-" if v is None else str(v) for v in row))
            sys.stdout.flush()
            states = [row[2] for row in rows]
            all_terminal = bool(rows) and all(s in TERMINAL_UNIT_STATES for s in states)
            if all_terminal or not rows or not args.watch:
                break
            launch_walk.sleep(args.interval)
            print()
        if not rows:
            return _STATUS_STILL_RUNNING
        if any(s in ("failed", "cancelled") for s in states):
            return int(ExitCode.FAILURE)
        if all(s == "complete" for s in states):
            return int(ExitCode.SUCCESS)
        return _STATUS_STILL_RUNNING

    return _with_connection("status", body)


# ======================================================================
# run compare
# ======================================================================

def _compare_units(conn, run_id: str) -> list[tuple[str, str, str | None, str | None]]:
    """(stage, unit, disposition, settings hash) of each unit's selected
    attempt, else its most recent one."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.stage, u.unit_id, a.disposition, er.settings_hash
            FROM units u
            LEFT JOIN attempts a ON a.id = COALESCE(
                u.selected_attempt,
                (SELECT id FROM attempts WHERE unit = u.id
                 ORDER BY started DESC, id DESC LIMIT 1))
            LEFT JOIN execution_records er ON er.attempt = a.id
            WHERE u.run = %s
            """,
            (run_id,))
        return cur.fetchall()


def _compare_instances(conn, run_id: str) -> list[tuple[str, str, str, str | None]]:
    """(kind, logical key as canonical jsonb text, instance id, slot as
    canonical JSON or ``None``) per product instance of the run (the slot:
    products page, "Identity")."""
    from rapidpipe.runs.slots import canonical_json

    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, logical_key::text, id, slot FROM product_instances "
            "WHERE run = %s ORDER BY kind, logical_key::text, id",
            (run_id,))
        return [(kind, key, instance, None if slot is None else canonical_json(slot))
                for kind, key, instance, slot in cur.fetchall()]


def _compare_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        run_a = launch_walk.require_run(conn, args.run_a)
        run_b = launch_walk.require_run(conn, args.run_b)
        different = False

        def show(value: Any) -> str:
            return "-" if value is None else str(value)

        print(f"settings_overlay_ref\t{show(run_a.settings_overlay_ref)}\t"
              f"{show(run_b.settings_overlay_ref)}")
        different |= run_a.settings_overlay_ref != run_b.settings_overlay_ref

        units_a = {(s, u): (d, h) for s, u, d, h in _compare_units(conn, args.run_a)}
        units_b = {(s, u): (d, h) for s, u, d, h in _compare_units(conn, args.run_b)}
        for key in sorted(set(units_a) | set(units_b)):
            a = units_a.get(key, ("(absent)", None))
            b = units_b.get(key, ("(absent)", None))
            print(f"unit\t{key[0]}\t{key[1]}\t{show(a[0])}\t{show(b[0])}\t"
                  f"settings\t{show(a[1])}\t{show(b[1])}")
            different |= a != b

        # Grouped by (kind, logical key) as before; the slot is shown as one
        # more column (the first recorded for the group, "-" when none is),
        # with no change to what counts as different (runs page, "Rules").
        instances_a: dict[tuple[str, str], list[str]] = {}
        instances_b: dict[tuple[str, str], list[str]] = {}
        slots: dict[tuple[str, str], str] = {}
        for rows, into in ((_compare_instances(conn, args.run_a), instances_a),
                           (_compare_instances(conn, args.run_b), instances_b)):
            for kind, key, instance, slot in rows:
                into.setdefault((kind, key), []).append(instance)
                if slot is not None:
                    slots.setdefault((kind, key), slot)
        for key in sorted(set(instances_a) | set(instances_b)):
            a_ids = instances_a.get(key, [])
            b_ids = instances_b.get(key, [])
            print(f"instance\t{key[0]}\t{key[1]}\t{slots.get(key, '-')}\t"
                  f"{','.join(a_ids) or '-'}\t{','.join(b_ids) or '-'}")
            different |= len(a_ids) != len(b_ids)

        print("different" if different else "same")
        return int(ExitCode.FAILURE) if different else int(ExitCode.SUCCESS)

    return _with_connection("compare", body)


# ======================================================================
# run expire
# ======================================================================

def _expire_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs import cleanup

    main = _main_module()
    try:
        # Fresh role credentials per run (they last an hour and do not refresh).
        s3_client_factory = cleanup.cleanup_s3_client_factory()
    except cleanup.CleanupRoleError as exc:
        sys.stderr.write(f"rapidpipe run expire: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    def print_reports(reports: list[Any]) -> None:
        for index, report in enumerate(reports):
            if index:
                print()
            main._print_deletion_report(report)
        refused = sum(1 for r in reports if r.refused)
        print(f"expired={len(reports) - refused} refused={refused}")

    return main._run_model_command(
        "expire",
        lambda conn: cleanup.expire_runs(
            conn, now=args.now, s3_client_factory=s3_client_factory),
        print_result=print_reports)


# ======================================================================
# run timings
# ======================================================================

#: exec_s beyond this many seconds is "over_30m" (both the per-attempt
#: flag and the per-stage summary's count).
_OVER_LONG_SECONDS = 30 * 60

_TIMINGS_COLUMNS = (
    "stage", "unit", "attempt", "disposition", "queue_s", "exec_s",
    "fetch_s", "body_s", "publish_s", "reconcile_lag_s", "over_30m",
)


def _timings_rows(conn, run_id: str, *, stage: str | None = None) -> list[tuple]:
    """One row per attempt of ``run_id`` (optionally restricted to
    ``stage``): its own columns plus ``execution_records.scheduler_metadata``,
    read once here rather than per attempt."""
    query = (
        "SELECT a.stage, u.unit_id, a.id, a.disposition, a.started, a.ended, "
        "er.scheduler_metadata "
        "FROM attempts a "
        "JOIN units u ON u.id = a.unit "
        "LEFT JOIN execution_records er ON er.attempt = a.id "
        "WHERE a.run = %s"
    )
    params: list[Any] = [run_id]
    if stage is not None:
        query += " AND a.stage = %s"
        params.append(stage)
    query += " ORDER BY a.stage, u.unit_id, a.started"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _parse_batch_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass(frozen=True)
class AttemptTiming:
    """One derived row of ``run timings``.

    ``fetch_s``/``body_s`` come from ``scheduler_metadata["stage"]``:
    ``rapidpipe.launch.batch.reconcile`` copies them there from the
    stage's own execution record (``exec/<attempt>.json``'s ``timing``
    key, ``rapidpipe.stages.contract.run_stage``, direction/logging-timing)
    when it fetches one for a SUCCEEDED job with a valid manifest -- no
    second S3 fetch here, and no new column. ``publish_s`` is always
    ``None``: the stage writes its execution record before publishing (the
    same reason that record has no ``"ended"``), so ``publish_s`` never
    reaches even ``scheduler_metadata``; it is only ever in the stage's
    own final log line. An attempt reconcile never resolved this way (an
    older row, a local run, or one that failed before a manifest existed)
    has no ``"stage"`` key either, so ``fetch_s``/``body_s`` fall back to
    ``None`` (printed ``-``) for it too.
    """

    stage: str
    unit: str
    attempt: str
    disposition: str | None
    queue_s: float | None
    exec_s: float | None
    fetch_s: float | None
    body_s: float | None
    publish_s: float | None
    reconcile_lag_s: float | None
    over_30m: bool | None


def _attempt_timing(row: tuple) -> AttemptTiming:
    stage, unit_id, attempt_id, disposition, started, ended, scheduler_metadata = row
    metadata = scheduler_metadata
    if isinstance(metadata, str):  # a driver that does not auto-cast jsonb
        metadata = json.loads(metadata) if metadata else {}
    batch = (metadata or {}).get("batch") or {}
    stage_timing = (metadata or {}).get("stage") or {}

    created_at = _parse_batch_timestamp(batch.get("created_at"))
    started_at = _parse_batch_timestamp(batch.get("started_at"))
    stopped_at = _parse_batch_timestamp(batch.get("stopped_at"))

    queue_s = (
        (started_at - created_at).total_seconds()
        if created_at is not None and started_at is not None else None)
    exec_s = (
        (stopped_at - started_at).total_seconds()
        if started_at is not None and stopped_at is not None else None)
    reconcile_lag_s = (
        (ended - stopped_at).total_seconds()
        if ended is not None and stopped_at is not None else None)

    return AttemptTiming(
        stage=stage, unit=unit_id, attempt=attempt_id, disposition=disposition,
        queue_s=queue_s, exec_s=exec_s,
        fetch_s=stage_timing.get("fetch_s"), body_s=stage_timing.get("body_s"),
        publish_s=None, reconcile_lag_s=reconcile_lag_s,
        over_30m=(None if exec_s is None else exec_s > _OVER_LONG_SECONDS))


def _stage_summary(stage: str, timings: list[AttemptTiming]) -> dict[str, Any]:
    exec_values = sorted(t.exec_s for t in timings if t.exec_s is not None)
    over_30m_count = sum(1 for t in timings if t.over_30m)
    summary: dict[str, Any] = {
        "stage": stage,
        "count": len(exec_values),
        "median_exec_s": None,
        "p90_exec_s": None,
        "max_exec_s": None,
        "over_30m_count": over_30m_count,
    }
    if exec_values:
        summary["median_exec_s"] = statistics.median(exec_values)
        # Nearest-rank, not linear interpolation: round 0.9 * (n - 1) to
        # the nearest sorted index and take that value outright, rather
        # than interpolating between its two neighbours. For [0, 100]
        # this returns 100, not an interpolated 90. Fine for the small
        # per-stage attempt counts this ever runs over.
        index = min(len(exec_values) - 1, int(round(0.9 * (len(exec_values) - 1))))
        summary["p90_exec_s"] = exec_values[index]
        summary["max_exec_s"] = exec_values[-1]
    return summary


def _fmt_timing_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _timings_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        launch_walk.require_run(conn, args.run_id)
        rows = _timings_rows(conn, args.run_id, stage=args.stage)
        timings = [_attempt_timing(row) for row in rows]

        by_stage: dict[str, list[AttemptTiming]] = {}
        for timing in timings:
            by_stage.setdefault(timing.stage, []).append(timing)
        stage_summaries = [_stage_summary(stage, ts) for stage, ts in by_stage.items()]

        if args.json:
            payload = {
                "attempts": [dataclasses.asdict(t) for t in timings],
                "stages": stage_summaries,
            }
            print(json.dumps(payload, sort_keys=True))
            return int(ExitCode.SUCCESS)

        print("\t".join(_TIMINGS_COLUMNS))
        for t in timings:
            print("\t".join(_fmt_timing_value(v) for v in (
                t.stage, t.unit, t.attempt, t.disposition, t.queue_s, t.exec_s,
                t.fetch_s, t.body_s, t.publish_s, t.reconcile_lag_s, t.over_30m)))
        if timings:
            # Nothing to summarise for a run with no attempts at all
            # (--stage matching none of them included): header only.
            print()
            print("\t".join(("stage", "count", "median_exec_s", "p90_exec_s",
                             "max_exec_s", "over_30m_count")))
            for summary in stage_summaries:
                print("\t".join(_fmt_timing_value(summary[key]) for key in (
                    "stage", "count", "median_exec_s", "p90_exec_s",
                    "max_exec_s", "over_30m_count")))
        return int(ExitCode.SUCCESS)

    return _with_connection("timings", body)


# ======================================================================
# Dispatch
# ======================================================================

def dispatch(args: argparse.Namespace) -> int:
    """Run the ``rapidpipe run`` subcommand named by ``args.run_command``,
    one of :data:`COMMANDS`."""
    return {
        "start": _start_command,
        "status": _status_command,
        "inputs": _inputs_command,
        "compare": _compare_command,
        "expire": _expire_command,
        "timings": _timings_command,
    }[args.run_command](args)
