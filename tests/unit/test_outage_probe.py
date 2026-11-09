"""Unit tests for rapidpipe.cli.outage_probe: a faked connection and clock, no database."""

from __future__ import annotations

import contextlib
import datetime as dt
import json

import psycopg2
import pytest

from rapidpipe.cli import main as cli
from rapidpipe.cli import outage_probe
from rapidpipe.db.connection import ConnectionUnavailable

START = dt.datetime(2026, 10, 29, 14, 58, tzinfo=dt.timezone.utc).timestamp()


class _Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        failure = self.conn.failures.pop(0) if self.conn.failures else None
        if failure:
            raise failure
        if "pg_sleep" in sql:
            self.conn.clock.sleep(params[0])

    def fetchone(self):
        return (4242, None)


class _Conn:
    def __init__(self, clock, failures):
        self.clock, self.failures = clock, failures

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def fileno(self):
        return -1


def _connect(clock, failures=None, refuse_after=None):
    """A stand-in for ``connect``: each call opens one fake connection; the ``refuse_after``-th raises."""
    opened = []

    @contextlib.contextmanager
    def connect(**kwargs):
        if refuse_after is not None and len(opened) >= refuse_after:
            raise ConnectionUnavailable("could not connect after 5 attempt(s)")
        opened.append(kwargs)
        yield _Conn(clock, failures if failures is not None else [])

    connect.opened = opened
    return connect


def _run(tmp_path, index, connect, clock, minutes=1, attempt=1):
    until = dt.datetime.fromtimestamp(START + 60 * minutes, dt.timezone.utc)
    return outage_probe.run(window="2026-10-29", until=until, index=index, outputs_root=tmp_path.as_uri(),
                            attempt=attempt, interval=5, hold=3, connect=connect, clock=clock,
                            sleep=clock.sleep)


def test_each_attempt_writes_one_product_under_the_scenario_identity_keyed_by_attempt(tmp_path):
    clock = _Clock()
    _run(tmp_path, 1, _connect(clock), clock, attempt=2)
    products = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json"))
    assert products == ["outage-probe/2026-10-29/1-idle-connection/2/timeline.json"]
    body = json.loads((tmp_path / products[0]).read_text())
    assert body["attempt"] == 2 and body["scenario"] == "idle-connection"


def test_an_undisturbed_scenario_is_bracket_not_met(tmp_path):
    clock = _Clock()
    assert _run(tmp_path, 2, _connect(clock), clock)["verdict"] == "bracket not met"


def test_the_idle_connection_reopens_after_an_error_and_records_both(tmp_path):
    clock = _Clock()
    connect = _connect(clock, failures=[None, None, psycopg2.OperationalError("server closed the connection")])
    summary = _run(tmp_path, 1, connect, clock)
    events = [e["event"] for e in json.loads(next(tmp_path.rglob("*.json")).read_text())["events"]]
    assert summary["verdict"] == "bracket met" and summary["errors"] == 1
    assert len(connect.opened) == 2 and "reopen" in events


def test_a_connect_the_client_gives_up_on_exits_75_with_no_product(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(outage_probe, "db_connect", _connect(clock, refuse_after=0))
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", tmp_path.as_uri())
    code = cli.main(["outage-probe", "--window", "2026-10-29", "--until", "2026-10-29T14:59:00Z",
                     "--scenario", "0"])
    assert code == 75 and not list(tmp_path.rglob("*.json"))


def test_a_transaction_hold_near_the_drain_budget_is_refused(tmp_path):
    clock = _Clock()
    with pytest.raises(outage_probe.CommandExit):
        outage_probe.run(window="2026-10-29", until=dt.datetime.now(dt.timezone.utc), index=2,
                         outputs_root=tmp_path.as_uri(), hold=120, connect=_connect(clock))
