"""Database-backed tests for the `statistics` stage: an association set's objects into `astroobjectsmeta`.

Runs `test_load.py`'s chain helpers (difference -> register -> load, for
real against PostgreSQL) to get a loaded source set, then fabricates what
`crossmatch` would leave: an `association-set` instance registered through
`register_manifest` (so its `product_instances`, `result_sets` and
dependency rows exist) and its `merges_<field>`/`astroobjects_<field>`
rows written through `rapidpipe.db.objects`. `statistics` then runs through
its own `main()` against a `crossmatch`-shaped manifest. Covers the rows and
their values, the statistics-set instance and its `result_sets` and
dependency rows, reuse on a second attempt, a second set beside the first
with the done check off, the base-plus-delta chain, and a set outside the
chain left unread.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import io

import pytest

import rapidpipe.stages.statistics as statistics
from rapidpipe.db import objects
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.science.statistics.lightcurve import object_statistics
from rapidpipe.selftest.support.fakestatisticsdb import build_statistics_input_set
from rapidpipe.stages.contract import ExitCode

from .test_load import _registered_difference, _run_load
from .test_register_l2 import _NoCloseNoCommitConnProxy

FIELD = 999999903   # a field no real per-field table has
TABLE = f"astroobjectsmeta_{FIELD}"


def _loaded_source_set(conn, tmp_path, monkeypatch):
    """A real run with one loaded source set; returns (run, source set, [(sid, ra, dec, fluxfit)])."""
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, _, outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(outputs / "manifest.json").outputs
    with conn.cursor() as cur:
        cur.execute(f"SELECT sid, ra, dec, fluxfit FROM {entry.registration['table']} "
                    f"WHERE result_set = %s ORDER BY sid", (entry.instance,))
        rows = cur.fetchall()
    assert len(rows) == 3
    return run_id, entry.instance, rows


def _csv(*rows):
    return io.StringIO("".join(",".join(str(v) for v in row) + "\n" for row in rows))


def _association_set(conn, run_id, *, source_sets, base=None, pairs=()):
    """What crossmatch leaves: a registered association set and its rows; returns its instance."""
    unit_id = f"xm-{new_ulid()}"
    repo.add_unit(conn, run_id, "crossmatch", "field", unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "crossmatch", unit_id)
    instance = new_ulid()
    key = {"field": FIELD, "base": base, "source_sets": sorted(source_sets),
           "settings_hash": "0" * 64}
    repo.register_manifest(conn, {
        "run": run_id, "stage": "crossmatch", "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": sorted(source_sets) + ([base] if base else [])},
        "outputs": [{"kind": "association-set", "format_version": "1", "instance": instance,
                     "key": key, "primary": None, "members": [],
                     "registration": {"astroobjects_table": f"astroobjects_{FIELD}",
                                      "merges_table": f"merges_{FIELD}"},
                     "row_count": len(pairs)}],
    }, registering_attempt_id=attempt_id)
    run_columns = (run_id, attempt_id, instance)
    with conn.cursor() as cur:
        objects.ensure_field_object_tables(cur, FIELD)
        objects.copy_merges(cur, FIELD, _csv(*[(aid, sid, *run_columns) for aid, sid in pairs]))
        objects.copy_astroobjects(cur, FIELD, _csv(
            *[(aid, 1.0, 1.0, 1.0, *run_columns) for aid in sorted({a for a, _ in pairs})]))
    return instance


def _run_statistics(conn, monkeypatch, tmp_path, run_id, association, *, name="statistics",
                    base=None, source_sets=(), overlay=""):
    monkeypatch.setattr(statistics, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    repo.add_unit(conn, run_id, "statistics", "field", str(FIELD))
    attempt_id = repo.allocate_attempt(conn, run_id, "statistics", str(FIELD))
    inputs = tmp_path / f"{name}-inputs"
    build_statistics_input_set(inputs, field=FIELD, instance=association, base=base,
                               source_sets=source_sets)
    settings = tmp_path / f"{name}.toml"
    settings.write_text(overlay)
    outputs = tmp_path / f"{name}-outputs"
    rc = statistics.main(["--run", run_id, "--unit", str(FIELD), "--attempt", attempt_id,
                          "--inputs", str(inputs), "--outputs", str(outputs),
                          "--settings", str(settings)])
    return rc, attempt_id, outputs


def _rows(conn, instance):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(objects.ASTROOBJECTSMETA_COLUMNS)} FROM {TABLE} "
                    f"WHERE result_set = %s ORDER BY aid", (instance,))
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def _assert_statistics(row, sources):
    expected = object_statistics(row["aid"], [s[1] for s in sources], [s[2] for s in sources],
                                 [s[3] for s in sources])
    assert row["nsources"] == expected.nsources
    # meanra, meandec are double precision; the other four are dev's `real`.
    assert row["meanra"] == pytest.approx(float(expected.meanra), abs=1e-9)
    assert row["meandec"] == pytest.approx(float(expected.meandec), abs=1e-9)
    for column in ("stdevra", "stdevdec", "meanflux", "stdevflux"):
        assert row[column] == pytest.approx(float(getattr(expected, column)), rel=1e-6, abs=1e-6)


def test_statistics_writes_one_row_per_object_and_one_complete_set(conn, tmp_path, monkeypatch):
    run_id, source_set, sources = _loaded_source_set(conn, tmp_path, monkeypatch)
    (s1, s2, s3) = sources
    association = _association_set(conn, run_id, source_sets=[source_set],
                                   pairs=[(1, s1[0]), (1, s2[0]), (2, s3[0])])
    # A set outside the chain, over the same sources: never read.
    _association_set(conn, run_id, source_sets=[source_set], pairs=[(9, s1[0])])

    rc, attempt_id, outputs = _run_statistics(conn, monkeypatch, tmp_path, run_id, association,
                                              source_sets=[source_set])
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs / "manifest.json")
    assert manifest.inputs.result_sets == (association,)
    (entry,) = manifest.outputs
    assert entry.kind == "statistics-set"
    assert entry.key == {"membership": association}
    assert entry.registration == {"table": TABLE, "row_count": 2, "objects_in_set": 2}

    rows = _rows(conn, entry.instance)
    assert [r["aid"] for r in rows] == [1, 2]
    for r in rows:
        assert (r["run"], r["attempt"], r["result_set"]) == (run_id, attempt_id, entry.instance)
    _assert_statistics(rows[0], [s1, s2])
    _assert_statistics(rows[1], [s3])
    assert (rows[1]["stdevra"], rows[1]["stdevdec"], rows[1]["stdevflux"]) == (0.0, 0.0, 0.0)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pi.kind, pi.producing_stage, pi.producing_attempt, pi.logical_key, "
            "rs.complete, rs.row_count FROM product_instances pi "
            "JOIN result_sets rs ON rs.instance = pi.id WHERE pi.id = %s", (entry.instance,))
        assert cur.fetchone() == ("statistics-set", "statistics", attempt_id,
                                  {"membership": association}, True, 2)
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        assert [r[0] for r in cur.fetchall()] == [association]
        cur.execute("SELECT relpersistence FROM pg_class WHERE relname = %s", (TABLE,))
        assert cur.fetchone() == ("u",)

    # A second attempt on the same membership reuses the set: no second row per object.
    rc, _, again = _run_statistics(conn, monkeypatch, tmp_path, run_id, association,
                                   name="again", source_sets=[source_set])
    assert rc == int(ExitCode.SUCCESS)
    assert Manifest.read(again / "manifest.json").outputs[0].instance == entry.instance
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*), count(DISTINCT (result_set, aid)) FROM {TABLE}")
        assert cur.fetchone() == (2, 2)


def test_done_check_off_writes_a_second_set_beside_the_first(conn, tmp_path, monkeypatch):
    run_id, source_set, (s1, s2, s3) = _loaded_source_set(conn, tmp_path, monkeypatch)
    association = _association_set(conn, run_id, source_sets=[source_set],
                                   pairs=[(1, s1[0]), (2, s2[0]), (2, s3[0])])
    off = "[statistics]\ndone_check = false\n"
    rc, _, first = _run_statistics(conn, monkeypatch, tmp_path, run_id, association,
                                   name="first", source_sets=[source_set], overlay=off)
    assert rc == int(ExitCode.SUCCESS)
    rc, _, second = _run_statistics(conn, monkeypatch, tmp_path, run_id, association,
                                    name="second", source_sets=[source_set], overlay=off)
    assert rc == int(ExitCode.SUCCESS)
    one = Manifest.read(first / "manifest.json").outputs[0].instance
    two = Manifest.read(second / "manifest.json").outputs[0].instance
    assert one != two
    # Keys are set-scoped: the same objects under two sets are two rows each.
    assert [r["aid"] for r in _rows(conn, one)] == [r["aid"] for r in _rows(conn, two)] == [1, 2]
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TABLE}")
        assert cur.fetchone()[0] == 4


def test_membership_is_base_plus_delta(conn, tmp_path, monkeypatch):
    run_id, source_set, (s1, s2, s3) = _loaded_source_set(conn, tmp_path, monkeypatch)
    base = _association_set(conn, run_id, source_sets=[source_set],
                            pairs=[(1, s1[0]), (2, s2[0])])
    # The delta links a further source to object 1 and makes object 3.
    delta = _association_set(conn, run_id, source_sets=[], base=base,
                             pairs=[(1, s3[0]), (3, s3[0])])

    rc, _, outputs = _run_statistics(conn, monkeypatch, tmp_path, run_id, delta, base=base)
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs / "manifest.json")
    (entry,) = manifest.outputs
    assert entry.registration == {"table": TABLE, "row_count": 3, "objects_in_set": 3}
    rows = _rows(conn, entry.instance)
    assert [(r["aid"], r["nsources"]) for r in rows] == [(1, 2), (2, 1), (3, 1)]
    _assert_statistics(rows[0], [s1, s3])
    record = (outputs / manifest.execution_record).read_text()
    assert base in record and delta in record
