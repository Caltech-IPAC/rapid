"""The outage probe: three small jobs that meet the Thursday database bracket through pgbouncer.

``rapidpipe outage-probe --window DATE --until TIME`` runs one scenario
through the real client (:func:`rapidpipe.db.connection.connect`, its
shipped retry budget) against whatever endpoint a pipeline job reads,
writing every step as one JSON line to stdout (the job's log stream) and,
when the attempt reaches ``--until``, its timeline as the product. It is
a measurement, not a pipeline stage: it writes no database row and takes
no table lock (``SELECT 1``, ``pg_backend_pid()``, ``pg_sleep``), and it
exists for the two windows of 2026-10-29 and 2026-11-05; the reader that
sets the client's budget from them (queue item outage-probe-read) deletes
it.

Scenarios, chosen by ``--scenario`` or else ``AWS_BATCH_JOB_ARRAY_INDEX``
(the scheduled submission is one array job of size three):

- 0 ``fresh-login``: a new connection every ``--interval`` seconds, each
  ``connect()`` stamped with its result and duration: a login that needs
  pgbouncer's ``auth_query`` while the pool is paused, under its
  ``client_login_timeout`` (60 s) and the client's 10 s connect timeout.
- 1 ``idle-connection``: one connection opened at start, its client port
  and backend pid recorded, exercised (``SELECT pg_backend_pid()``, then
  commit, so it holds no server connection between exercises) every
  ``--interval`` seconds; on an error it is closed and reopened through
  ``connect()`` and exercised again.
- 2 ``in-flight``: back-to-back transactions on one connection, each
  ``now(); pg_sleep(--hold); commit`` with its begin and end stamped, so one
  is open whenever PAUSE lands; on an error the outcome is recorded and the
  connection reopened. ``--hold`` is at most 30 s, well under the bracket's
  drain budget (``db_drain_max_seconds`` 120), so PAUSE never waits past it.

Pass condition, the same for each scenario, read by outage-probe-read from
Batch and the outputs root alone: the job ends SUCCEEDED; its attempts and
each attempt's exit code are Batch's record (a connect the client gives up
on raises ``ConnectionUnavailable`` and exits 75, which Batch retries, three
attempts, exactly as a pipeline job); and exactly one product exists under
the scenario's identity ``<outputs root>/outage-probe/<window>/<index>-<scenario>/``,
at ``<attempt>/timeline.json``, the attempt that wrote it in the key and the
body. No product is a missing write; two are a duplicate; an overwrite
cannot happen, since each attempt writes under its own number. A job whose
timeline shows no disturbance (no error, no step slower than ``--stall``
seconds beyond its own work) ends ``bracket not met``, exit 0: inconclusive,
never a pass.

The bracket (rapid_systems ``op-db-maintenance.sh`` in the Thursday window,
15:00 UTC): the schedule submits at 14:58 UTC, each scenario prepares its
state at once and holds it, exercising at intervals until ``--until``, so
it spans the bracket whenever it lands before then; nothing in the job
detects the bracket's phases. Overlap is proven after the fact: the
reader joins these timestamps against the PAUSE, restart and reopen lines
of that window's ``rapid-maintenance-canary`` CodeBuild log, and reads
which branch the window took (patch: PAUSE then RESUME; reboot: the
pooler stopped and started with the host) from the same record. The
expected offsets and branches are in the pull request that added this
module (rapid PR, outage-probe-prep) and the runbook's part-13 row.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import psycopg2

from rapidpipe.db.connection import connect as db_connect
from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation import prefix

SCENARIOS = ("fresh-login", "idle-connection", "in-flight")
MAX_HOLD_S = 30.0


def _stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds")


def _client_port(conn) -> int | None:
    try:
        with socket.fromfd(conn.fileno(), socket.AF_INET, socket.SOCK_STREAM) as sock:
            return sock.getsockname()[1]
    except (OSError, IndexError, TypeError):
        return None


class _Probe:
    def __init__(self, scenario: str, attempt: int, connect: Callable, clock: Callable[[], float]):
        self.scenario, self.attempt, self.clock = scenario, attempt, clock
        self.connect, self.events = connect, []
        self._cm = self.conn = None

    def log(self, event: str, **fields: Any) -> None:
        record = {"t": _stamp(self.clock()), "scenario": self.scenario, "attempt": self.attempt,
                  "event": event, **fields}
        self.events.append(record)
        print(json.dumps(record), flush=True)

    def open(self, event: str) -> None:
        start = self.clock()
        self._cm = self.connect(application_name=f"rapidpipe-outage-probe-{self.scenario}")
        try:
            self.conn = self._cm.__enter__()
        except Exception as exc:
            self._cm = None
            self.log(event, ok=False, seconds=round(self.clock() - start, 3), error=str(exc).strip())
            raise
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        self.conn.commit()
        self.log(event, ok=True, seconds=round(self.clock() - start, 3),
                 client_port=_client_port(self.conn), backend_pid=pid)

    def close(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
        self._cm = self.conn = None


def _fresh_login(probe: _Probe, hold: float) -> float:
    probe.open("login")
    probe.close()
    return probe.events[-1]["seconds"]


def _idle_exercise(probe: _Probe, hold: float) -> float:
    start = probe.clock()
    try:
        with probe.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        probe.conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
        probe.log("exercise", ok=False, seconds=round(probe.clock() - start, 3), error=str(exc).strip())
        probe.close()
        probe.open("reopen")
        return probe.clock() - start
    probe.log("exercise", ok=True, seconds=round(probe.clock() - start, 3), backend_pid=pid)
    return probe.clock() - start


def _transaction(probe: _Probe, hold: float) -> float:
    begin = probe.clock()
    try:
        with probe.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid(), now()")
            pid, _ = cur.fetchone()
            cur.execute("SELECT pg_sleep(%s)", (hold,))
        probe.conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
        probe.log("transaction", outcome="error", begin=_stamp(begin), end=_stamp(probe.clock()),
                  error=str(exc).strip())
        probe.close()
        probe.open("reopen")
        return probe.clock() - begin - hold
    end = probe.clock()
    probe.log("transaction", outcome="committed", begin=_stamp(begin), end=_stamp(end), backend_pid=pid)
    return end - begin - hold


_STEPS = {"fresh-login": _fresh_login, "idle-connection": _idle_exercise, "in-flight": _transaction}


def run(*, window: str, until: dt.datetime, index: int, outputs_root: str, attempt: int = 1,
        job_id: str | None = None, interval: float = 5.0, hold: float = 3.0, stall: float = 2.0,
        connect: Callable | None = None, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run scenario ``index`` until ``until``, write its product and return the summary.

    The first step always runs, so an attempt that starts after ``until``
    (a Batch retry late in the window) still records one exercise. A
    ``ConnectionUnavailable`` from the client propagates: the command's
    exit-code boundary turns it into 75, as for any pipeline job.
    """
    if not 0 <= index < len(SCENARIOS):
        raise CommandExit(int(ExitCode.USAGE), f"scenario index {index} is not 0, 1 or 2")
    if not 0 < hold <= MAX_HOLD_S:
        raise CommandExit(int(ExitCode.USAGE), f"--hold {hold} is not in (0, {MAX_HOLD_S:g}] seconds")
    scenario = SCENARIOS[index]
    probe = _Probe(scenario, attempt, connect or db_connect, clock)
    probe.log("start", window=window, until=_stamp(until.timestamp()), job_id=job_id,
              interval=interval, hold=hold if scenario == "in-flight" else None)
    if scenario != "fresh-login":
        probe.open("open")
    slowest = 0.0
    errors = 0
    while True:
        n = len(probe.events)
        slowest = max(slowest, _STEPS[scenario](probe, hold))
        errors += sum(1 for e in probe.events[n:] if e.get("ok") is False or e.get("outcome") == "error")
        if clock() >= until.timestamp():
            break
        if scenario != "in-flight":     # its transactions run back to back
            sleep(interval)
    probe.close()
    disturbed = errors > 0 or slowest > stall
    summary = {"window": window, "scenario": scenario, "index": index, "attempt": attempt,
               "job_id": job_id, "steps": sum(1 for e in probe.events if e["event"] in
                                              ("login", "exercise", "transaction")),
               "errors": errors, "slowest_extra_seconds": round(slowest, 3),
               "verdict": "bracket met" if disturbed else "bracket not met"}
    probe.log("summary", **{k: v for k, v in summary.items() if k not in ("window", "scenario", "attempt")})
    _write_product(outputs_root, window, index, scenario, attempt, {**summary, "events": probe.events})
    return summary


def product_prefix(outputs_root: str, window: str, index: int) -> str:
    """The scenario's identity: one per window and index, the same on every Batch attempt."""
    return f"{outputs_root.rstrip('/')}/outage-probe/{window}/{index}-{SCENARIOS[index]}"


def _write_product(outputs_root: str, window: str, index: int, scenario: str, attempt: int,
                   body: dict) -> None:
    target = f"{product_prefix(outputs_root, window, index)}/{attempt}"
    if target.startswith("file://"):
        directory = prefix.local_path(target)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "timeline.json").write_text(json.dumps(body, indent=1) + "\n")
        return
    with tempfile.TemporaryDirectory(prefix="outage-probe-") as tmp:
        (Path(tmp) / "timeline.json").write_text(json.dumps(body, indent=1) + "\n")
        prefix.put(Path(tmp), target)


def command(args) -> int:
    """``outage-probe``: the scenario from ``--scenario`` or the array index; exit 0 at ``--until``."""
    root = os.environ.get("RAPIDPIPE_OUTPUTS_ROOT")
    if not root:
        raise CommandExit(int(ExitCode.USAGE), "RAPIDPIPE_OUTPUTS_ROOT is not set")
    index = args.scenario
    if index is None:
        try:
            index = int(os.environ["AWS_BATCH_JOB_ARRAY_INDEX"])
        except (KeyError, ValueError):
            raise CommandExit(int(ExitCode.USAGE),
                              "no --scenario and no AWS_BATCH_JOB_ARRAY_INDEX") from None
    try:
        dt.date.fromisoformat(args.window)
        until = dt.datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommandExit(int(ExitCode.USAGE), f"--window or --until: {exc}") from exc
    if until.tzinfo is None:
        raise CommandExit(int(ExitCode.USAGE), "--until needs a UTC offset, e.g. 2026-10-29T16:15:00Z")
    run(window=args.window, until=until, index=index, outputs_root=root,
        attempt=int(os.environ.get("AWS_BATCH_JOB_ATTEMPT", "1")), job_id=os.environ.get("AWS_BATCH_JOB_ID"),
        interval=args.interval, hold=args.hold, stall=args.stall)
    return int(ExitCode.SUCCESS)
