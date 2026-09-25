"""Database-backed tests for the `export` stage: a loaded source set read through the real query path.

Runs `test_load.py`'s chain (difference -> register -> load, for real
against PostgreSQL, via `test_statistics.py`'s helper) to get a loaded
source set, then reads it the way `export` does:
:class:`rapidpipe.stages.export.PostgresExportDatabase` over the test's
connection -- ``result_set_states`` and the set-scoped, server-side
``sources`` read (``rapidpipe.db.sources.iter_set_rows``) -- and runs the
stage's own ``main()`` end to end, with hats-import where it is installed
and a stand-in catalog where it is not (db-migrations.yml does not
install it). Covers dev's column list and order, the ``flags = 0``
setting, a registered set of another kind, and an unregistered set.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

import rapidpipe.stages.export as export
from rapidpipe.products.catalogexport import selection_digest
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.support.fakeexport import build_export_input_set
from rapidpipe.stages.contract import ExitCode

from .test_register_l2 import _NoCloseNoCommitConnProxy
from .test_statistics import _loaded_source_set

COLUMNS = tuple(tomllib.loads(
    (Path(export.__file__).resolve().parent.parent / "settings" / "export.toml").read_text()
)["hats"]["columns"])


def _db(conn):
    return export.PostgresExportDatabase(conn)


def test_source_set_states_and_rows_through_the_real_query(conn, tmp_path, monkeypatch):
    run_id, source_set, rows = _loaded_source_set(conn, tmp_path, monkeypatch)
    db = _db(conn)

    states = db.result_set_states([source_set, "01J8Y6QZ3M00000000000NOPE0"], run_id)
    assert set(states) == {source_set}
    assert states[source_set]["kind"] == "source-set"
    assert states[source_set]["complete"] is True
    assert states[source_set]["deletion_state"] == "retained"
    assert export.classify_result_sets((source_set,), states) == ([source_set], [])

    read = list(db.source_rows([source_set], COLUMNS, flags_zero_only=False))
    assert [r[0] for r in read] == [r[0] for r in rows]          # sid order, every row
    by_name = [dict(zip(COLUMNS, r)) for r in read]
    assert [(d["sid"], d["ra"], d["dec"], d["fluxfit"]) for d in by_name] == rows
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM sources WHERE result_set = %s "
                    f"ORDER BY sid", (source_set,))
        assert read == cur.fetchall()

    with conn.cursor() as cur:
        cur.execute("UPDATE sources SET flags = 0 WHERE result_set = %s", (source_set,))
        cur.execute("UPDATE sources SET flags = 4 WHERE sid = %s", (rows[0][0],))
    unflagged = list(db.source_rows([source_set], ("sid", "flags"), flags_zero_only=True))
    assert [r[0] for r in unflagged] == [r[0] for r in rows[1:]]
    assert all(r[1] == 0 for r in unflagged)
    assert len(list(db.source_rows([source_set], ("sid",), flags_zero_only=False))) == len(rows)


def test_unknown_columns_are_refused_before_any_sql(conn):
    with pytest.raises(ValueError, match="not readable"):
        list(_db(conn).source_rows(["x"], ("sid", "ra; drop table sources"),
                                   flags_zero_only=False))


def test_stage_main_exports_the_named_set(conn, tmp_path, monkeypatch):
    run_id, source_set, rows = _loaded_source_set(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(export, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    if importlib.util.find_spec("hats_import") is None:
        def _stand_in(csv_paths, hats, output_path, tmp_dir):
            lines = [p.read_text().splitlines() for p in csv_paths]
            assert lines[0][0] == ",".join(COLUMNS) and len(lines[0]) == len(rows) + 1
            catalog = output_path / hats["catalog_name"]
            (catalog / "dataset" / "Norder=2" / "Dir=0").mkdir(parents=True)
            (catalog / "properties").write_text(f"hats_nrows={len(rows)}\n")
            (catalog / "dataset" / "Norder=2" / "Dir=0" / "Npix=1.parquet").write_bytes(b"PAR1")
            return "stand-in"
        monkeypatch.setattr(export, "build_hats_catalog", _stand_in)

    inputs = tmp_path / "export-inputs"
    build_export_input_set(inputs, result_sets=[source_set])
    outputs = tmp_path / "export-outputs"
    overlay = tmp_path / "export.toml"
    overlay.write_text("[hats]\ndask_processes = false\n")
    rc = export.main(["--run", run_id, "--unit", "4711398", "--attempt", "01J8Y6QZ3M0000000000EXPAT1",
                      "--inputs", str(inputs), "--outputs", str(outputs),
                      "--settings", str(overlay)])
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(outputs / "manifest.json").outputs
    assert entry.kind == "catalog-export"
    assert entry.key["selection"] == selection_digest([source_set])
    assert "result_set" not in entry.key
    assert entry.registration["row_count"] == len(rows)
    assert entry.registration["source_sets"] == [source_set]
    assert entry.registration["partition_count"] >= 1


def test_stage_main_refuses_an_unregistered_set(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    inputs = tmp_path / "inputs"
    build_export_input_set(inputs, result_sets=["01J8Y6QZ3M00000000000NOPE0"])
    rc = export.main(["--run", "01J8Y6QZ3M00000000000XRUN0", "--unit", "4711398",
                      "--attempt", "01J8Y6QZ3M0000000000EXPAT2", "--inputs", str(inputs),
                      "--outputs", str(tmp_path / "outputs")])
    assert rc == int(ExitCode.INPUT_REJECTED)
