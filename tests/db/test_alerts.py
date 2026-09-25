"""Database-backed tests for the `alerts` stage: catalog sets into an Avro container and the outbox.

The chain runs for real against PostgreSQL, as tests/db/test_load.py does:
an l2 image is admitted and registered, the difference stage runs with fake
tools and is registered, `load` loads its synthetic Photutils catalogs into
`sources` (positive row 3 carries flags 4). An association set and a
statistics set are then registered and seeded by hand -- the `crossmatch`
and `statistics` stages are not ported yet -- with one object for positive
row 1 (and its statistics) and a merges row for negative row 1 whose object
is missing (an orphan). `alerts` runs against the rolled-back transaction.

Crossmatch's and statistics' rows live in step 1's standalone per-field
tables `merges_<field>`, `astroobjects_<field>` and
`astroobjectsmeta_<field>` (not children of the prototypes), made here as in
production by `create_field_object_tables(field)` and
`create_astroobjectsmeta_child_table(field)` (20260924-04).

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import io
import json
import shutil

import fastavro
import pytest

import rapidpipe.stages.alerts as alerts
from rapidpipe.db import alerts as alerts_db
from rapidpipe.db import objects as objects_db
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest, register_unit_id
from rapidpipe.runs import repository as repo
from rapidpipe.stages.contract import ExitCode

from .attempt_helpers import succeed_and_select
from .test_load import _registered_difference, _run_load
from .test_register_l2 import _NoCloseNoCommitConnProxy, _run_register
from .test_repository import _make_run, _make_unit

ALERTS_UNIT = "e20260821001234/SCA07"


def _field_tables(cur, field):
    """Make field ``field``'s per-field tables through step 1's functions."""
    cur.execute("SELECT create_field_object_tables(%s)", (field,))
    cur.execute("SELECT create_astroobjectsmeta_child_table(%s)", (field,))


def _register_set(conn, run_id, *, stage, kind, key, row_count, unit_suffix=""):
    unit_id = f"{stage}-field-5321{unit_suffix}"
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


def _seeded_chain(conn, tmp_path, monkeypatch, *, extend_base=False, duplicate_pair=False):
    """Everything up to the alerts invocation: returns (run, diff outputs, sets, sids).

    With ``extend_base``, the association set extends a base set as crossmatch
    leaves them: the object row stays in the base that made it, the new
    detection's merges rows are in the extending set. With ``duplicate_pair``
    too, the kept source's (aid, sid) merges row is also in the base.
    """
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

    base_set = None
    if extend_base:
        base_set, base_attempt = _register_set(
            conn, run_id, stage="crossmatch", kind="association-set",
            key={"field": 5321, "base": None}, row_count=1, unit_suffix="-base")
    association_set, assoc_attempt = _register_set(
        conn, run_id, stage="crossmatch", kind="association-set",
        key={"field": 5321, "base": base_set} if extend_base else {"field": "5321"},
        row_count=2)
    statistics_set, stats_attempt = _register_set(
        conn, run_id, stage="statistics", kind="statistics-set",
        key={"membership": association_set}, row_count=1)
    kept, orphan = sids[(1, True)], sids[(1, False)]
    aid, orphan_aid = 7_000_000_001, 7_000_000_002
    with conn.cursor() as cur:
        _field_tables(cur, 5321)
        cur.execute("INSERT INTO merges_5321 (aid, sid, run, attempt, result_set) VALUES "
                    "(%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
                    (aid, kept["sid"], run_id, assoc_attempt, association_set,
                     orphan_aid, orphan["sid"], run_id, assoc_attempt, association_set))
        if duplicate_pair:
            cur.execute("INSERT INTO merges_5321 (aid, sid, run, attempt, result_set) VALUES "
                        "(%s, %s, %s, %s, %s)",
                        (aid, kept["sid"], run_id, base_attempt, base_set))
        object_set, object_attempt = ((base_set, base_attempt) if extend_base
                                      else (association_set, assoc_attempt))
        cur.execute("INSERT INTO astroobjects_5321 (aid, ra0, dec0, flux0, run, attempt, "
                    "result_set) VALUES (%s, %s, %s, 100.0, %s, %s, %s)",
                    (aid, kept["ra"], kept["dec"], run_id, object_attempt, object_set))
        cur.execute("INSERT INTO astroobjectsmeta_5321 (aid, meanra, stdevra, meandec, stdevdec, "
                    "meanflux, stdevflux, nsources, run, attempt, result_set) VALUES "
                    "(%s, %s, 0.5, %s, 0.25, 100.0, 1.0, 3, %s, %s, %s)",
                    (aid, kept["ra"], kept["dec"], run_id, stats_attempt, statistics_set))
    sets = (source_set, association_set, statistics_set)
    return run_id, diff_outputs, sets, {"kept": kept["sid"], "orphan": orphan["sid"],
                                        "flagged": sids[(3, True)]["sid"], "aid": aid,
                                        "base": base_set}


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


def test_a_pruned_set_of_an_unnamed_association_set_exits_65(conn, tmp_path, monkeypatch):
    """R5 (supervisor step 9): a pruned set's base must be a named association set."""
    run_id, diff_outputs, sets, _ = _seeded_chain(conn, tmp_path, monkeypatch)
    pruned, _ = _register_set(conn, run_id, stage="prune", kind="pruned-set",
                              key={"base": new_ulid(), "settings_hash": "sha256:0"},
                              row_count=0)
    inputs, _ = _input_set(tmp_path, diff_outputs, (sets[0], sets[1], pruned))
    rc, attempt_id, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_outbox WHERE attempt = %s", (attempt_id,))
        assert cur.fetchone()[0] == 0


def test_an_object_in_the_base_set_is_found_for_a_detection_in_the_extending_set(
        conn, tmp_path, monkeypatch):
    run_id, diff_outputs, sets, ids = _seeded_chain(conn, tmp_path, monkeypatch,
                                                    extend_base=True, duplicate_pair=True)
    inputs, difference = _input_set(tmp_path, diff_outputs, sets)
    rc, attempt_id, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs / "manifest.json")
    assert list(manifest.inputs.result_sets) == [*sets, ids["base"]]
    container = next(e for e in manifest.outputs if e.kind == "alert-container")
    raw = (outputs / container.primary).read_bytes()
    alerts_read = list(fastavro.reader(io.BytesIO(raw)))
    assert [a["diaSourceId"] for a in alerts_read] == [ids["kept"]]
    assert alerts_read[0]["diaObject"]["diaObjectId"] == ids["aid"]
    assert alerts_read[0]["diaObject"]["nDiaSources"] == 3
    assert container.registration["dropped_count"] == 2      # flagged, and the orphan
    with conn.cursor() as cur:
        cur.execute("SELECT candidate, object FROM alert_outbox WHERE attempt = %s",
                    (attempt_id,))
        assert cur.fetchall() == [(ids["kept"], ids["aid"])]
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (container.instance,))
        assert ids["base"] in {r[0] for r in cur.fetchall()}


def test_two_fields_two_association_sets_and_their_statistics(conn, tmp_path, monkeypatch):
    """One image over two fields: each field's association and statistics set is named.

    Negative source 1's merges row in the first field's set points at an
    object missing from that set (an orphan there), but the second field's
    set associates it with an object of its own, with its own statistics; the
    object the first field's merges row names exists only in the second set,
    which is not in the first set's lineage, so it is not borrowed.
    """
    run_id, diff_outputs, sets, ids = _seeded_chain(conn, tmp_path, monkeypatch)
    source_set, association_set, statistics_set = sets
    field2_set, field2_attempt = _register_set(
        conn, run_id, stage="crossmatch", kind="association-set",
        key={"field": 5322, "base": None}, row_count=1, unit_suffix="-5322")
    field2_stats, field2_stats_attempt = _register_set(
        conn, run_id, stage="statistics", kind="statistics-set",
        key={"membership": field2_set}, row_count=1, unit_suffix="-5322")
    aid2 = 7_000_000_003
    with conn.cursor() as cur:
        _field_tables(cur, 5322)
        cur.execute("SELECT ra, dec FROM sources WHERE sid = %s", (ids["orphan"],))
        ra, dec = cur.fetchone()
        cur.execute("INSERT INTO merges_5322 (aid, sid, run, attempt, result_set) VALUES "
                    "(%s, %s, %s, %s, %s)",
                    (aid2, ids["orphan"], run_id, field2_attempt, field2_set))
        cur.execute("INSERT INTO astroobjects_5322 (aid, ra0, dec0, flux0, run, attempt, "
                    "result_set) VALUES (%s, %s, %s, 100.0, %s, %s, %s), "
                    "(%s, %s, %s, 100.0, %s, %s, %s)",
                    (aid2, ra, dec, run_id, field2_attempt, field2_set,
                     7_000_000_002, ra, dec, run_id, field2_attempt, field2_set))
        cur.execute("INSERT INTO astroobjectsmeta_5322 (aid, meanra, stdevra, meandec, stdevdec, "
                    "meanflux, stdevflux, nsources, run, attempt, result_set) VALUES "
                    "(%s, %s, 0.5, %s, 0.25, 100.0, 1.0, 5, %s, %s, %s)",
                    (aid2, ra, dec, run_id, field2_stats_attempt, field2_stats))
    named = (source_set, association_set, statistics_set, field2_set, field2_stats)
    inputs, _ = _input_set(tmp_path, diff_outputs, named)
    rc, attempt_id, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)

    manifest = Manifest.read(outputs / "manifest.json")
    container = next(e for e in manifest.outputs if e.kind == "alert-container")
    assert container.registration["association_sets"] == [association_set, field2_set]
    assert container.registration["statistics_sets"] == [statistics_set, field2_stats]
    assert container.registration["association_set"] == association_set
    assert container.registration["statistics_set"] == statistics_set
    raw = (outputs / container.primary).read_bytes()
    by_sid = {a["diaSourceId"]: a for a in fastavro.reader(io.BytesIO(raw))}
    assert sorted(by_sid) == sorted([ids["kept"], ids["orphan"]])
    assert by_sid[ids["kept"]]["diaObject"]["nDiaSources"] == 3
    assert by_sid[ids["orphan"]]["diaObject"]["diaObjectId"] == aid2
    assert by_sid[ids["orphan"]]["diaObject"]["nDiaSources"] == 5
    assert container.registration["dropped_count"] == 1          # only the flagged one
    with conn.cursor() as cur:
        cur.execute("SELECT candidate, object FROM alert_outbox WHERE attempt = %s "
                    "ORDER BY record_ordinal", (attempt_id,))
        assert dict(cur.fetchall()) == {ids["kept"]: ids["aid"], ids["orphan"]: aid2}


def test_the_merges_count_fallback_counts_a_duplicated_pair_once(conn, tmp_path, monkeypatch):
    """No statistics set named: nDiaSources falls back to the distinct sources across the chain.

    The kept source's (aid, sid) pair is in the base and in its extension;
    it is one source, so nDiaSources is 1.
    """
    run_id, diff_outputs, sets, ids = _seeded_chain(conn, tmp_path, monkeypatch,
                                                    extend_base=True, duplicate_pair=True)
    source_set, association_set, _ = sets
    inputs, _ = _input_set(tmp_path, diff_outputs, (source_set, association_set))
    rc, _, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)
    container = next(e for e in Manifest.read(outputs / "manifest.json").outputs
                     if e.kind == "alert-container")
    raw = (outputs / container.primary).read_bytes()
    alerts_read = list(fastavro.reader(io.BytesIO(raw)))
    assert [a["diaSourceId"] for a in alerts_read] == [ids["kept"]]
    assert alerts_read[0]["diaObject"]["nDiaSources"] == 1
    assert alerts_read[0]["diaObject"]["raSigma"] is None


# ----------------------------------------------------------------------
# R5 (supervisor step 9, 2026-09-25): prune binds to alerts
# ----------------------------------------------------------------------


def _pruned_chain(conn, tmp_path, monkeypatch):
    """_seeded_chain with a base holding two more detections of the kept
    source's object -- the flagged source and the orphan source -- and a
    pruned set of the association set that lists (aid, flagged) only."""
    run_id, diff_outputs, sets, ids = _seeded_chain(conn, tmp_path, monkeypatch,
                                                    extend_base=True)
    source_set, association_set, _ = sets
    with conn.cursor() as cur:
        cur.execute("SELECT attempt FROM astroobjects_5321 WHERE result_set = %s",
                    (ids["base"],))
        base_attempt = cur.fetchone()[0]
        cur.execute("INSERT INTO merges_5321 (aid, sid, run, attempt, result_set) VALUES "
                    "(%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
                    (ids["aid"], ids["flagged"], run_id, base_attempt, ids["base"],
                     ids["aid"], ids["orphan"], run_id, base_attempt, ids["base"]))
    pruned, prune_attempt = _register_set(
        conn, run_id, stage="prune", kind="pruned-set",
        key={"base": association_set, "settings_hash": "sha256:" + "0" * 64}, row_count=1)
    with conn.cursor() as cur:
        from rapidpipe.db.objects import insert_pruned_merges
        assert insert_pruned_merges(cur, [(ids["aid"], ids["flagged"])], pruned,
                                    association_set, run_id, prune_attempt) == 1
    return run_id, diff_outputs, sets, ids, pruned


def test_history_leaves_out_the_pairs_a_named_pruned_set_lists(conn, tmp_path, monkeypatch):
    """The pruned set lists (aid, flagged): the kept source's history is the
    orphan source only; the unlisted pair (aid, orphan) stays."""
    run_id, diff_outputs, sets, ids, pruned = _pruned_chain(conn, tmp_path, monkeypatch)
    inputs, _ = _input_set(tmp_path, diff_outputs, (*sets, pruned))
    rc, attempt_id, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs / "manifest.json")
    assert list(manifest.inputs.result_sets) == [*sets, pruned, ids["base"]]
    container = next(e for e in manifest.outputs if e.kind == "alert-container")
    raw = (outputs / container.primary).read_bytes()
    by_sid = {a["diaSourceId"]: a for a in fastavro.reader(io.BytesIO(raw))}
    kept = by_sid[ids["kept"]]
    assert [p["diaSourceId"] for p in kept["prvDiaSources"]] == [ids["orphan"]]
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"]["pruned_sets"] == [pruned]
    with conn.cursor() as cur:
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (container.instance,))
        assert pruned in {r[0] for r in cur.fetchall()}


def test_the_exclusion_applies_only_with_the_pruned_set_named(conn, tmp_path, monkeypatch):
    """db.alerts.history and associations directly: without the pruned set the
    listed pair is history and counted; with it, neither."""
    run_id, _, sets, ids, pruned = _pruned_chain(conn, tmp_path, monkeypatch)
    association_set = sets[1]
    lineages = {association_set: [association_set, ids["base"]]}
    fields = {association_set: 5321}
    objects = [(association_set, ids["aid"])]
    with conn.cursor() as cur:
        readable = [sets[0]]
        unpruned = alerts_db.history(cur, lineages, fields, objects, 0.0, source_sets=readable)
        applied = alerts_db.history(cur, lineages, fields, objects, 0.0, source_sets=readable,
                                    pruned_by_association={association_set: pruned})
        assert {r["sid"] for r in unpruned} == {ids["kept"], ids["flagged"], ids["orphan"]}
        assert {r["sid"] for r in applied} == {ids["kept"], ids["orphan"]}
        # no statistics set: nsources is the merges-count fallback
        counts = {}
        for label, pm in (("unpruned", None), ("applied", {association_set: pruned})):
            rows = alerts_db.associations(cur, lineages, fields, {association_set: None},
                                          [ids["kept"], ids["flagged"]],
                                          source_sets=readable, pruned_by_association=pm)
            counts[label] = {r["sid"]: r["nsources"] for r in rows}
        assert counts["unpruned"] == {ids["kept"]: 3, ids["flagged"]: 3}
        # the flagged pair is excluded: no association, and not counted
        assert counts["applied"] == {ids["kept"]: 2}


def test_a_pair_pruned_under_one_association_set_stays_under_another(
        conn, tmp_path, monkeypatch):
    """Codex 9-1 amendment to R5: the exclusion is per association set. B's
    pruned set lists (aid, flagged); A's pruned set lists nothing. A's
    history and fallback count keep the pair; B's leave it out."""
    run_id, _, sets, ids, _ = _pruned_chain(conn, tmp_path, monkeypatch)
    set_a = sets[1]
    set_b, b_attempt = _register_set(conn, run_id, stage="crossmatch", kind="association-set",
                                     key={"field": 5321, "base": None}, row_count=2,
                                     unit_suffix="-b")
    pruned_a, _ = _register_set(conn, run_id, stage="prune", kind="pruned-set",
                                key={"base": set_a, "settings_hash": "sha256:a"}, row_count=0,
                                unit_suffix="-a")
    pruned_b, prune_b_attempt = _register_set(
        conn, run_id, stage="prune", kind="pruned-set",
        key={"base": set_b, "settings_hash": "sha256:b"}, row_count=1, unit_suffix="-pb")
    from rapidpipe.db.objects import insert_pruned_merges
    with conn.cursor() as cur:
        cur.execute("INSERT INTO merges_5321 (aid, sid, run, attempt, result_set) VALUES "
                    "(%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
                    (ids["aid"], ids["kept"], run_id, b_attempt, set_b,
                     ids["aid"], ids["flagged"], run_id, b_attempt, set_b))
        cur.execute("INSERT INTO astroobjects_5321 (aid, ra0, dec0, flux0, run, attempt, "
                    "result_set) SELECT aid, ra0, dec0, flux0, %s, %s, %s FROM astroobjects_5321 "
                    "WHERE aid = %s AND result_set = %s",
                    (run_id, b_attempt, set_b, ids["aid"], ids["base"]))
        assert insert_pruned_merges(cur, [(ids["aid"], ids["flagged"])], pruned_b, set_b,
                                    run_id, prune_b_attempt) == 1
        lineages = {set_a: [set_a, ids["base"]], set_b: [set_b]}
        fields = {set_a: 5321, set_b: 5321}
        pruned = {set_a: pruned_a, set_b: pruned_b}
        rows = alerts_db.history(cur, lineages, fields, [(set_a, ids["aid"]), (set_b, ids["aid"])],
                                 0.0, source_sets=[sets[0]], pruned_by_association=pruned)
        by_set = {}
        for r in rows:
            by_set.setdefault(r["object_set"], set()).add(r["sid"])
        assert by_set[set_a] == {ids["kept"], ids["flagged"], ids["orphan"]}
        assert by_set[set_b] == {ids["kept"]}
        assoc = alerts_db.associations(cur, lineages, fields, {set_a: None, set_b: None},
                                       [ids["kept"], ids["flagged"]],
                                       source_sets=[sets[0]], pruned_by_association=pruned)
        got = {(r["association_set"], r["sid"]): r["nsources"] for r in assoc}
        assert got == {(set_a, ids["kept"]): 3, (set_a, ids["flagged"]): 3,
                       (set_b, ids["kept"]): 1}


# ----------------------------------------------------------------------
# Supervisor step 9 R2 (Codex 9-2): history reads only readable source sets
# ----------------------------------------------------------------------


def _foreign_history(conn, tmp_path, monkeypatch, *, source_state="selected",
                     named_by_base=True):
    """The loop's shape: the alerts run's association set extends another
    (production) run's association set, whose key names that run's source
    set, which holds an earlier detection of the kept source's object.

    ``source_state``: ``selected`` (a production candidate set from its
    unit's selected attempt: readable), ``unselected`` (its load attempt was
    never selected) or ``scratch`` (its custody is scratch). With
    ``named_by_base`` False the base's key names no source set, so the
    earlier detection is outside the readable list.
    Returns (run, diff outputs, sets, ids) with ids["history"], ids["foreign_sources"],
    ids["foreign_base"].
    """
    run_id, diff_outputs, sets, ids = _seeded_chain(conn, tmp_path, monkeypatch)
    source_set, association_set, _ = sets
    producer = _make_run(conn, kind="production", selected_stages=["load", "crossmatch"],
                         max_attempts=3)
    _make_unit(conn, producer, stage="load", unit_id="load-earlier")
    load_attempt = repo.allocate_attempt(conn, producer, "load", "load-earlier")
    foreign_sources = new_ulid()
    repo.register_manifest(conn, {
        "run": producer, "stage": "load", "attempt": load_attempt,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{"kind": "source-set", "format_version": "1", "instance": foreign_sources,
                     "key": {"difference": new_ulid(), "catalog_type": "photutils"},
                     "primary": None, "members": [], "registration": {}, "row_count": 1}],
    }, registering_attempt_id=load_attempt)
    if source_state != "unselected":
        succeed_and_select(conn, load_attempt)
    if source_state == "scratch":
        with conn.cursor() as cur:
            cur.execute("UPDATE product_instances SET custody = 'scratch' WHERE id = %s",
                        (foreign_sources,))
    base_key = {"field": 5321, "base": None,
                "source_sets": [foreign_sources] if named_by_base else [],
                "settings_hash": "sha256:" + "1" * 64}
    _make_unit(conn, producer, stage="crossmatch", unit_id="5321")
    xm_attempt = repo.allocate_attempt(conn, producer, "crossmatch", "5321")
    foreign_base = new_ulid()
    repo.register_manifest(conn, {
        "run": producer, "stage": "crossmatch", "attempt": xm_attempt,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{"kind": "association-set", "format_version": "1", "instance": foreign_base,
                     "key": base_key, "primary": None, "members": [], "registration": {},
                     "row_count": 1}],
    }, registering_attempt_id=xm_attempt)
    succeed_and_select(conn, xm_attempt)
    with conn.cursor() as cur:
        # The alerts run's association set extends the foreign base.
        cur.execute("UPDATE product_instances SET logical_key = logical_key || %s::jsonb "
                    "WHERE id = %s",
                    (json.dumps({"base": foreign_base, "source_sets": [source_set]}),
                     association_set))
        # An earlier detection of the kept source, a day before, in the foreign source set.
        table, _ = objects_db.source_set_table(cur, source_set, run_id)
        cur.execute(f"CREATE TEMP TABLE earlier ON COMMIT DROP AS SELECT * FROM {table} "
                    "WHERE sid = %s", (ids["kept"],))
        cur.execute("UPDATE earlier SET sid = nextval('sources_sid_seq'), id = id + 100000, "
                    "mjdobs = mjdobs - 1, run = %s, attempt = %s, result_set = %s",
                    (producer, load_attempt, foreign_sources))
        cur.execute(f"INSERT INTO {table} SELECT * FROM earlier RETURNING sid")
        history_sid = cur.fetchone()[0]
        cur.execute("DROP TABLE earlier")
        cur.execute("INSERT INTO merges_5321 (aid, sid, run, attempt, result_set) VALUES "
                    "(%s, %s, %s, %s, %s)",
                    (ids["aid"], history_sid, producer, xm_attempt, foreign_base))
    return run_id, diff_outputs, sets, {**ids, "history": history_sid,
                                        "foreign_sources": foreign_sources,
                                        "foreign_base": foreign_base}


def _prv_sids(outputs):
    manifest = Manifest.read(outputs / "manifest.json")
    container = next(e for e in manifest.outputs if e.kind == "alert-container")
    raw = (outputs / container.primary).read_bytes()
    return {a["diaSourceId"]: {p["diaSourceId"] for p in (a["prvDiaSources"] or [])}
            for a in fastavro.reader(io.BytesIO(raw))}


def test_the_loop_case_reads_history_from_the_earlier_dates_selected_source_set(
        conn, tmp_path, monkeypatch):
    """Date 2's alerts over date 1's selected candidate sets: the earlier detection is history."""
    run_id, diff_outputs, sets, ids = _foreign_history(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        lineages = {sets[1]: alerts_db.association_chain(cur, sets[1], run_id)}
        assert lineages[sets[1]] == [sets[1], ids["foreign_base"]]
        assert alerts_db.readable_source_sets(cur, lineages, sets[0], run_id) == [
            sets[0], ids["foreign_sources"]]
    inputs, _ = _input_set(tmp_path, diff_outputs, sets)
    rc, _, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)
    assert ids["history"] in _prv_sids(outputs)[ids["kept"]]


@pytest.mark.parametrize("source_state", ["unselected", "scratch"])
def test_a_chain_naming_an_unreadable_foreign_source_set_exits_65(
        conn, tmp_path, monkeypatch, source_state):
    """Codex 9-2: a selected base whose key names another run's scratch source
    set, or one from an unselected attempt, is refused before any source is read."""
    run_id, diff_outputs, sets, ids = _foreign_history(conn, tmp_path, monkeypatch,
                                                       source_state=source_state)
    with conn.cursor() as cur:
        lineages = {sets[1]: alerts_db.association_chain(cur, sets[1], run_id)}
        match = "scratch" if source_state == "scratch" else "selected attempt"
        with pytest.raises(ValueError, match=match):
            alerts_db.readable_source_sets(cur, lineages, sets[0], run_id)
    inputs, _ = _input_set(tmp_path, diff_outputs, sets)
    rc, attempt_id, _ = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.INPUT_REJECTED)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_outbox WHERE attempt = %s", (attempt_id,))
        assert cur.fetchone()[0] == 0


def test_a_history_row_outside_the_readable_source_sets_never_appears(
        conn, tmp_path, monkeypatch):
    """A merges pair in the chain whose source's set no chain key names is not history."""
    run_id, diff_outputs, sets, ids = _foreign_history(conn, tmp_path, monkeypatch,
                                                       named_by_base=False)
    association_set = sets[1]
    with conn.cursor() as cur:
        lineages = {association_set: alerts_db.association_chain(cur, association_set, run_id)}
        readable = alerts_db.readable_source_sets(cur, lineages, sets[0], run_id)
        assert readable == [sets[0]]
        fields = {association_set: 5321}
        objects_ = [(association_set, ids["aid"])]
        restricted = alerts_db.history(cur, lineages, fields, objects_, 0.0,
                                       source_sets=readable)
        widened = alerts_db.history(cur, lineages, fields, objects_, 0.0,
                                    source_sets=readable + [ids["foreign_sources"]])
        assert ids["history"] not in {r["sid"] for r in restricted}
        assert ids["history"] in {r["sid"] for r in widened}
        # The merges-count fallback counts only readable sources too.
        counts = {label: {r["sid"]: r["nsources"] for r in alerts_db.associations(
                      cur, lineages, fields, {association_set: None}, [ids["kept"]],
                      source_sets=s)}
                  for label, s in (("restricted", readable),
                                   ("widened", readable + [ids["foreign_sources"]]))}
        assert counts["widened"][ids["kept"]] == counts["restricted"][ids["kept"]] + 1
    inputs, _ = _input_set(tmp_path, diff_outputs, sets)
    rc, _, outputs = _run_alerts(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.SUCCESS)
    assert ids["history"] not in _prv_sids(outputs)[ids["kept"]]
