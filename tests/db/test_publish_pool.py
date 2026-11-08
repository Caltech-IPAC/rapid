"""Publishers through a small transaction pool (publish-pool-wedge, run 177, 2026-11-08).

Run 177's difference jobs published into a few dozen overlapping fields
through SMDC's pgbouncer, transaction pooling, 800 server connections for
the pipeline. Each waited on the fields' advisory locks inside its publish
transaction, so each wait held a server connection; the holder spent ten
minutes in the reconcile's Python (``attach``: 14,343 founded xobjects
against 88,213 founded since the read), the waiters filled the pool, and
every other client of the pipeline's login queued until pgbouncer's
``query_wait_timeout`` closed it.

The contract, against a real pgbouncer with a pool of three in front of
the test database and six publishers into overlapping sets of three
fields, each reconciling against thousands of objects already there: no
server connection ever waits on a field lock, no holder of field locks
leaves its connection idle for seconds, another client of the pool is
served throughout, and every publisher publishes.

Needs ``pgbouncer`` on the path beside the database the ``connect``
fixture names (foundation.yml installs it); fails, never skips, without it.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest

from rapidpipe.foundation import job_show, publish
from tests.db.foundation_helpers import DATASET, SURVEY, new_job, new_run, write_attempt

POOL = 3
PUBLISHERS = 6
FIELDS = 8
SEED = 24_000              # objects already published, spread over the fields
FOUNDED = 2_000            # each publisher's founded objects, matching none
RADIUS = 1e-4              # degrees
SPACING = 3e-4             # degrees between grid positions
WAIT_TIMEOUT = 3           # pgbouncer's query_wait_timeout, seconds
IDLE_HOLD_LIMIT = 2.0      # seconds a lock holder may leave its connection idle


def _grid(k: int, shift: float = 0.0) -> tuple[float, float]:
    return 10.0 + (k % 150) * SPACING + shift, -5.0 + (k // 150) * SPACING + shift


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def pooler(tmp_path):
    """A pgbouncer in transaction mode, a pool of POOL, in front of the test database."""
    binary = shutil.which("pgbouncer")
    if binary is None:
        pytest.fail("pgbouncer is not on the path; the pool test needs one")
    port = _free_port()
    user, password = os.environ["PGUSER"], os.environ.get("PGPASSWORD", "")
    (tmp_path / "users.txt").write_text(f'"{user}" "{password}"\n')
    (tmp_path / "pgbouncer.ini").write_text(f"""[databases]
* = host={os.environ['PGHOST']} port={os.environ.get('PGPORT', '5432')}
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {port}
unix_socket_dir =
auth_type = trust
auth_file = {tmp_path / 'users.txt'}
pool_mode = transaction
default_pool_size = {POOL}
max_client_conn = 100
query_wait_timeout = {WAIT_TIMEOUT}
logfile = {tmp_path / 'pgbouncer.log'}
""")
    process = subprocess.Popen([binary, str(tmp_path / "pgbouncer.ini")])
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if time.monotonic() > deadline or process.poll() is not None:
                process.kill()
                pytest.fail(f"pgbouncer did not listen on {port}")
            time.sleep(0.1)
    opened = []

    def connect():
        conn = psycopg2.connect(host="127.0.0.1", port=port, dbname=os.environ["PGDATABASE"],
                                user=user, password=password)
        opened.append(conn)
        return conn

    yield connect
    for conn in opened:
        conn.close()
    process.terminate()
    process.wait(10)


def _attempt(tmp_path, job, objects, fields):
    root = tmp_path / f"job-{job}"
    write_attempt(root, {"objects": [{**o, "dataset": DATASET, "survey": SURVEY} for o in objects]})
    manifest = root / "manifest.json"
    body = json.loads(manifest.read_text())
    body["catalog"] = {"dataset": DATASET, "survey": SURVEY, "fields": fields, "radius": RADIUS}
    manifest.write_text(json.dumps(body))
    return root.as_uri()


def _watch(observer, run: int, stop: threading.Event, seen: dict) -> None:
    """Sample the server's backends: field-lock waiters, and how long each holder sat idle."""
    idle_since: dict[int, float] = {}
    while not stop.is_set():
        with observer.cursor() as cur:
            cur.execute("SELECT l.pid, l.granted, a.state FROM pg_locks l"
                        " JOIN pg_stat_activity a ON a.pid = l.pid"
                        " WHERE l.locktype = 'advisory' AND l.classid = %s AND l.objsubid = 2",
                        (run,))
            rows = cur.fetchall()
        observer.commit()
        now = time.monotonic()
        waiting = {pid for pid, granted, _ in rows if not granted}
        seen["max_waiting"] = max(seen["max_waiting"], len(waiting))
        idle = {pid for pid, granted, state in rows if granted and state == "idle in transaction"}
        for pid in list(idle_since):
            if pid not in idle:
                del idle_since[pid]
        for pid in idle:
            idle_since.setdefault(pid, now)
            seen["max_idle_hold"] = max(seen["max_idle_hold"], now - idle_since[pid])
        time.sleep(0.05)


def _bystander(conn, stop: threading.Event, seen: dict) -> None:
    """Another client of the pool, served throughout or not."""
    while not stop.is_set():
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.commit()
        except psycopg2.Error as exc:
            seen["bystander_errors"].append(str(exc))
            return
        time.sleep(0.1)


def test_publishers_beyond_the_pool_into_overlapping_fields_never_wait_holding_it(
        connect, pooler, tmp_path, monkeypatch):
    module = importlib.import_module("rapidpipe.foundation.publish")    # the package exports the function
    monkeypatch.setattr(module, "LOCK_RETRY_INITIAL_S", 0.05, raising=False)
    monkeypatch.setattr(module, "LOCK_RETRY_CAP_S", 0.5, raising=False)
    setup = connect()
    run = new_run(setup)
    seed_job = new_job(setup, run)
    seed = [{"aid": k, "field": k % FIELDS, "ra0": _grid(k)[0], "dec0": _grid(k)[1]}
            for k in range(SEED)]
    publish(setup, seed_job, 1, _attempt(tmp_path, seed_job, seed, list(range(FIELDS))))
    jobs = [new_job(setup, run) for _ in range(PUBLISHERS)]
    prefixes = {}
    for i, job in enumerate(jobs):
        fields = [i, i + 1, i + 2]                    # each overlaps its neighbours' sets
        founded = [{"aid": k, "field": i, "ra0": _grid(k, SPACING / 2)[0],
                    "dec0": _grid(k, SPACING / 2)[1]} for k in range(i * FOUNDED, (i + 1) * FOUNDED)]
        prefixes[job] = _attempt(tmp_path, job, founded, fields)

    stop, seen = threading.Event(), {"max_waiting": 0, "max_idle_hold": 0.0, "bystander_errors": []}
    watcher = threading.Thread(target=_watch, args=(connect(), run, stop, seen))
    bystander = threading.Thread(target=_bystander, args=(pooler(), stop, seen))
    watcher.start()
    bystander.start()
    try:
        with ThreadPoolExecutor(PUBLISHERS) as pool:
            futures = {job: pool.submit(publish, pooler(), job, 1, prefixes[job]) for job in jobs}
            outcomes = {}
            for job, future in futures.items():
                try:
                    outcomes[job] = future.result(timeout=120)["published"]
                except psycopg2.Error as exc:
                    outcomes[job] = f"{type(exc).__name__}: {exc}".strip()
    finally:
        stop.set()
        watcher.join(10)
        bystander.join(10)

    assert seen["max_waiting"] == 0, f"{seen['max_waiting']} server connections waited on field locks"
    assert seen["max_idle_hold"] < IDLE_HOLD_LIMIT, \
        f"a field-lock holder left its connection idle {seen['max_idle_hold']:.1f} s"
    assert seen["bystander_errors"] == []
    assert outcomes == {job: True for job in jobs}
    assert all(job_show(setup, job)["state"] == "published" for job in jobs)
