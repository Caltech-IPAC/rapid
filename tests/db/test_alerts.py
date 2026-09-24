"""Database-backed tests for the `alerts` stage: catalog sets into an Avro container and the outbox.

The chain runs for real against PostgreSQL, as tests/db/test_load.py does:
an l2 image is admitted and registered, the difference stage runs with fake
tools and is registered, `load` loads its synthetic Photutils catalogs into
`sources` (positive row 3 carries flags 4). An association set and a
statistics set are then registered and seeded by hand -- the `crossmatch`
and `statistics` stages are not ported yet -- with one object for positive
row 1 (and its statistics) and a merges row for negative row 1 whose object
is missing (an orphan). `alerts` runs against the rolled-back transaction.

The `run`, `attempt` and `result_set` columns on `merges`, `astroobjects`
and `astroobjectsmeta` are 20260924-03-objects-run-columns.sql's.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import io
import json
import shutil

import fastavro

import rapidpipe.stages.alerts as alerts
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest, register_unit_id
from rapidpipe.runs import repository as repo
from rapidpipe.stages.contract import ExitCode

from .test_load import _registered_difference, _run_load
from .test_register_l2 import _NoCloseNoCommitConnProxy, _run_register
from .test_repository import _make_unit

ALERTS_UNIT = "e20260821001234/SCA07"


def _register_set(conn, run_id, *, stage, kind, key, row_count):
    unit_id = f"{stage}-field-5321"
    _make_unit(conn, run_id, stage=stage, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    instance = new_ulid()
    repo.register_manifest(conn, {
        "run": run_id, "stage": stage, "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{"kind": kind, "format_version": "1", "instance": instance, "key": key,
                     "primary": None, "members": [], "registration": {},
                     "row_count": row_count}],
    }, registering_attempt_id=attempt_id)
    return instance, attempt_id


def _seeded_chain(conn, tmp_path, monkeypatch):
    """Everything up to the alerts invocation: returns (run, diff outputs, sets, sids)."""
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, _, load_outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == 0
    source_set = Manifest.read(load_outputs / "manifest.json").outputs[0].instance
    with conn.cursor() as cur:
        cur.execute("SELECT id, isdiffpos, sid, flags, ra, dec FROM sources "
                    "WHERE result_set = %s", (source_set,))
        sids = {(r[0], r[1]): {"sid": r[2], "flags": r[3], "ra": r[4], "dec": r[5]}
                for r in cur.fetchall()}
    assert sids[(3, True)]["flags"] == 4

    association_set, assoc_attempt = _register_set(
        conn, run_id, stage="crossmatch", kind="association-set", key={"field": "5321"},
        row_count=2)
    statistics_set, stats_attempt = _register_set(
        conn, run_id, stage="statistics", kind="statistics-set",
        key={"membership": association_set}, row_count=1)
    kept, orphan = sids[(1, True)], sids[(1, False)]
    aid, orphan_aid = 7_000_000_001, 7_000_000_002
    with conn.cursor() as cur:
        cur.execute("INSERT INTO merges (aid, sid, run, attempt, result_set) VALUES "
                    "(%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
                    (aid, kept["sid"], run_id, assoc_attempt, association_set,
                     orphan_aid, orphan["sid"], run_id, assoc_attempt, association_set))
        cur.execute("INSERT INTO astroobjects (aid, ra0, dec0, flux0, run, attempt, result_set) "
                    "VALUES (%s, %s, %s, 100.0, %s, %s, %s)",
                    (aid, kept["ra"], kept["dec"], run_id, assoc_attempt, association_set))
        cur.execute("INSERT INTO astroobjectsmeta (aid, meanra, stdevra, meandec, stdevdec, "
                    "meanflux, stdevflux, nsources, run, attempt, result_set) VALUES "
                    "(%s, %s, 0.5, %s, 0.25, 100.0, 1.0, 3, %s, %s, %s)",
                    (aid, kept["ra"], kept["dec"], run_id, stats_attempt, statistics_set))
    sets = (source_set, association_set, statistics_set)
    return run_id, diff_outputs, sets, {"kept": kept["sid"], "orphan": orphan["sid"],
                                        "flagged": sids[(3, True)]["sid"], "aid": aid}


def _input_set(tmp_path, diff_outputs, result_sets):
    """An input-set manifest over the registered difference instance's difference member."""
    diff_manifest = json.loads((diff_outputs / "manifest.json").read_text())
    entry = next(e for e in diff_manifest["outputs"] if e["kind"] == "difference-image")
    member = next(m for m in entry["members"] if m["role"] == "difference")
    inputs = tmp_path / "alerts-inputs"
    (inputs / member["path"]).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(diff_outputs / member["path"], inputs / member["path"])
    entry = {**entry, "members": [member], "primary": member["path"]}
    (inputs / "manifest.json").write_text(json.dumps({
        "schema_version": "1", "run": diff_manifest["run"],
        "unit": {"kind": "detector-image", "id": ALERTS_UNIT}, "stage": "input-set",
        "attempt": diff_manifest["attempt"], "execution_record": "exec/none.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {},
                   "result_sets": list(result_sets)},
        "outputs": [entry],
    }))
    return inputs, entry["instance"]


def _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs, *, attempt_id=None):
    monkeypatch.setattr(alerts, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    if attempt_id is None:
        _make_unit(conn, run_id, stage="alerts", unit_id=ALERTS_UNIT)
        attempt_id = repo.allocate_attempt(conn, run_id, "alerts", ALERTS_UNIT)
    outputs = tmp_path / "alerts-outputs"
    rc = alerts.main(["--run", run_id, "--unit", ALERTS_UNIT, "--attempt", attempt_id,
                      "--inputs", str(inputs), "--outputs", str(outputs)])
    return rc, attempt_id, outputs


def test_alerts_writes_outbox_rows_instances_and_nalertpackets(conn, tmp_path, monkeypatch):
    run_id, diff_outputs, sets, ids = _seeded_chain(conn, tmp_path, monkeypatch)
    inputs, difference = _input_set(tmp_path, diff_outputs, sets)
    rc, attempt_id, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)

    manifest = Manifest.read(outputs / "manifest.json")
    by_kind = {e.kind: e for e in manifest.outputs}
    container, alert_set = by_kind["alert-container"], by_kind["alert-set"]
    raw = (outputs / container.primary).read_bytes()
    alerts_read = list(fastavro.reader(io.BytesIO(raw)))
    assert [a["diaSourceId"] for a in alerts_read] == [ids["kept"]]
    assert alerts_read[0]["diaObject"]["diaObjectId"] == ids["aid"]
    assert alerts_read[0]["diaObject"]["nDiaSources"] == 3
    assert container.registration["dropped_count"] == 2

    with conn.cursor() as cur:
        cur.execute("SELECT run, attempt, instance, result_set, alert_name, candidate, object, "
                    "record_ordinal, record_index, block_offset, block_length, "
                    "time_processed_mjd, schema_version, published_at "
                    "FROM alert_outbox WHERE attempt = %s", (attempt_id,))
        rows = cur.fetchall()
        assert len(rows) == 1
        (run, attempt, instance, result_set, name, candidate, obj, ordinal, index, offset,
         length, time_proc, schema_version, published_at) = rows[0]
        assert (run, attempt, instance, result_set) == (run_id, attempt_id, container.instance,
                                                        alert_set.instance)
        assert (name, candidate, obj, ordinal, index, schema_version, published_at) == (
            None, ids["kept"], ids["aid"], 0, 0, "00.04", None)
        # the one block, read on its own after the header, holds the alert
        block = list(fastavro.reader(io.BytesIO(raw[:offset] + raw[offset:offset + length])))
        assert [a["diaSourceId"] for a in block] == [ids["kept"]]
        assert block[0]["diaSource"]["timeProcessedMjd"] == time_proc

        cur.execute(
            "SELECT pi.kind, pi.producing_stage, pi.producing_attempt, pi.custody, "
            "rs.complete, rs.row_count FROM product_instances pi "
            "LEFT JOIN result_sets rs ON rs.instance = pi.id WHERE pi.producing_attempt = %s "
            "ORDER BY pi.kind", (attempt_id,))
        assert cur.fetchall() == [
            ("alert-container", "alerts", attempt_id, "candidate", None, None),
            ("alert-set", "alerts", attempt_id, "candidate", True, 1)]
        cur.execute("SELECT role FROM product_members WHERE instance = %s ORDER BY role",
                    (container.instance,))
        assert [r[0] for r in cur.fetchall()] == ["container", "summary"]
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (container.instance,))
        assert {r[0] for r in cur.fetchall()} == {difference, *sets}
        cur.execute("SELECT nalertpackets FROM diffimages WHERE instance = %s AND run = %s",
                    (difference, run_id))
        assert cur.fetchone()[0] == 1

    # A rerun of the same attempt after an uncertain commit, its local
    # outputs lost: identical bytes regenerated, the rows reused.
    before = {m.path: (outputs / m.path).read_bytes() for m in container.members}
    for path in before:
        (outputs / path).unlink()
    rc, _, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs,
                                 attempt_id=attempt_id)
    assert rc == int(ExitCode.SUCCESS)
    assert {p: (outputs / p).read_bytes() for p in before} == before
    again = {e.kind: e.instance for e in Manifest.read(outputs / "manifest.json").outputs}
    assert again == {"alert-container": container.instance, "alert-set": alert_set.instance}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_outbox WHERE attempt = %s", (attempt_id,))
        assert cur.fetchone()[0] == 1

    # register replays the alerts manifest as a no-op.
    rc, _ = _run_register(conn, monkeypatch, outputs, run_id=run_id,
                          unit_id=register_unit_id(Manifest.read(outputs / "manifest.json")),
                          tmp_path=tmp_path, name="alerts-register")
    assert rc == int(ExitCode.SUCCESS)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM product_instances WHERE producing_attempt = %s",
                    (attempt_id,))
        assert cur.fetchone()[0] == 2


def test_an_unknown_result_set_kind_exits_65(conn, tmp_path, monkeypatch):
    run_id, diff_outputs, sets, _ = _seeded_chain(conn, tmp_path, monkeypatch)
    pruned, _ = _register_set(conn, run_id, stage="prune", kind="pruned-set",
                              key={"base": sets[1]}, row_count=0)
    inputs, _ = _input_set(tmp_path, diff_outputs, (sets[0], sets[1], pruned))
    rc, attempt_id, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_outbox WHERE attempt = %s", (attempt_id,))
        assert cur.fetchone()[0] == 0
