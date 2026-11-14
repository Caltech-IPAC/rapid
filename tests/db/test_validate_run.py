"""scripts/validate_run.py's lineage and catalog checks of a set declared empty.

Contracts: an official binding of a published job whose manifest lists its
product in ``empty_ok`` and that holds no rows of it (``[difference]
skip_loading_sources_records``) is not an orphan, and the lineage summary
counts it under its product; a binding with no rows and no such declaration
still fails lineage, naming the binding; a set listed in ``empty_ok`` that
holds rows resolves to them and is not counted as empty; the catalog
summary counts the official ``sources`` sets declared empty. Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from rapidpipe.foundation import change, create_run, publish
from rapidpipe.foundation.prefix import local_path
from tests.db.foundation_helpers import DATASET, SURVEY, new_job, write_attempt
from tests.db.test_children import NOV4, source_rows

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("validate_run", SCRIPTS / "validate_run.py")
validate_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate_run)

ASSOCIATE = {"match_radius": 1.0, "source_flags": 0, "min_snr": 5.0}


def _validate(monkeypatch, conn):
    """validate_run's queries on the test's connection."""
    monkeypatch.setattr(validate_run._local, "conn", conn, raising=False)
    return validate_run


def _live_run(conn) -> int:
    run = create_run(conn, "tester", "test", "abc123", {"associate": ASSOCIATE}, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    return run


def _sources_set(conn, tmp_path, run: int, rows: list[dict]) -> int:
    """A published, bound sources set whose manifest declares sources ``empty_ok``."""
    key = {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 1}
    job = new_job(conn, run, expects=[{"product": "sources", "key": key, "binding": None}])
    prefix = write_attempt(tmp_path / f"job-{job}", {"sources": rows})
    manifest = local_path(prefix) / "manifest.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "empty_ok": ["sources"]}))
    assert publish(conn, job, 1, prefix)["group"] is not None
    return job


def _binding(conn, job: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM official WHERE job = %s AND product = 'sources'", (job,))
        (binding,) = cur.fetchone()
    conn.commit()
    return binding


def test_a_set_declared_empty_passes_lineage_counted_under_its_product(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    run = _live_run(conn)
    _sources_set(conn, tmp_path, run, [])

    assert _validate(monkeypatch, conn).check_lineage(run)
    line = capsys.readouterr().out
    assert "0 bindings resolve to published rows" in line and "not resolved: 1 sources" in line


def test_a_set_with_no_rows_and_no_declaration_fails_lineage_naming_its_binding(connect, tmp_path, monkeypatch,
                                                                                capsys):
    conn = connect()
    run = _live_run(conn)
    job = _sources_set(conn, tmp_path, run, [])
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET manifest = manifest - 'empty_ok' WHERE id = %s", (job,))
    conn.commit()

    assert not _validate(monkeypatch, conn).check_lineage(run)
    assert f"binding {_binding(conn, job)}: sources" in capsys.readouterr().out


def test_a_set_declared_empty_ok_that_holds_rows_is_not_counted_as_empty(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    run = _live_run(conn)
    _sources_set(conn, tmp_path, run, source_rows(NOV4 + 0.7, 8))

    assert _validate(monkeypatch, conn).check_lineage(run)
    line = capsys.readouterr().out
    assert "1 bindings resolve to published rows" in line and "not resolved: none" in line


def test_the_catalog_summary_counts_the_sources_sets_declared_empty(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    run = _live_run(conn)
    _sources_set(conn, tmp_path, run, [])

    assert _validate(monkeypatch, conn).check_catalog(run)
    assert "1 official sources sets declared empty" in capsys.readouterr().out
