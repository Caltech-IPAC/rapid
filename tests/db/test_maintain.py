"""Database-backed tests for the `maintain` stage: CLUSTER/ANALYZE of a sources child table.

Runs `test_load.py`'s own chain helpers (difference -> register -> load,
for real against PostgreSQL) to get a loaded, unclustered child table,
then runs `maintain` against it and checks the table is clustered on its
radec index -- the same check `test_sources_child_table.py` makes of
`rapidpipe.db.sources.cluster_and_analyze` directly, here exercised
through the stage's own `main()`. Also covers a unit naming a different
detector than the one actually loaded.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import rapidpipe.stages.maintain as maintain
from rapidpipe.db import sources
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.stages.contract import ExitCode

from .test_load import _registered_difference, _run_load
from .test_register_l2 import _NoCloseNoCommitConnProxy


def _run_maintain(conn, monkeypatch, tmp_path, run_id, inputs, *, unit_id, name="maintain"):
    monkeypatch.setattr(maintain, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    repo.add_unit(conn, run_id, "maintain", "detector-date", unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "maintain", unit_id)
    outputs = tmp_path / f"{name}-outputs"
    rc = maintain.main(["--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
                        "--inputs", str(inputs), "--outputs", str(outputs)])
    return rc, attempt_id, outputs


def _is_clustered(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT indisclustered FROM pg_index WHERE indexrelid = %s::regclass",
                    (f"{table}_radec_idx",))
        return bool(cur.fetchone()[0])


def test_maintain_clusters_the_loaded_child_table(conn, tmp_path, monkeypatch):
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, _, load_outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == 0
    (entry,) = Manifest.read(load_outputs / "manifest.json").outputs
    table = entry.registration["table"]
    obs_date, sca = sources._split_table_name(table)
    assert _is_clustered(conn, table) is False

    unit_id = f"{obs_date}/SCA{sca:02d}"
    rc, attempt_id, outputs = _run_maintain(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                            unit_id=unit_id)
    assert rc == int(ExitCode.SUCCESS)

    manifest = Manifest.read(outputs / "manifest.json")
    assert manifest.outputs == ()
    assert manifest.inputs.result_sets == (entry.instance,)
    record_path = outputs / manifest.execution_record
    record = json.loads(record_path.read_text())
    assert record["notes"] == {"table": table, "clustered": True}

    assert _is_clustered(conn, table) is True
    with conn.cursor() as cur:
        cur.execute("SELECT relhasindex FROM pg_class WHERE oid = %s::regclass", (table,))
        assert cur.fetchone()[0] is True


def test_a_unit_naming_a_different_detector_exits_65(conn, tmp_path, monkeypatch):
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, _, load_outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == 0
    (entry,) = Manifest.read(load_outputs / "manifest.json").outputs
    obs_date, sca = sources._split_table_name(entry.registration["table"])

    unit_id = f"{obs_date}/SCA{(sca + 1) % 18 + 1:02d}"
    rc, _, outputs = _run_maintain(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                   unit_id=unit_id, name="mismatch")
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()
    assert _is_clustered(conn, entry.registration["table"]) is False
