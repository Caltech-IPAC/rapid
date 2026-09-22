"""Database-backed tests for the run/attempt/instance columns added to
diffimages/diffimmeta by 20260921-03-difference-run-columns.sql, and for
diffimmeta.source_counts added by 20260921-04-diffimmeta-source-counts.sql.

Skips cleanly if PGHOST is unset (see conftest.py). Uses the repository
API (test_repository's helpers) to create the run/unit/attempt/instance
chain a rebuild-style row needs, and a local helper to insert the legacy
FK parents (exposures, l2files, refimages) a diffimages row needs,
reusing the baseline's seeded filters/pipelines/scas/swversions rows.
"""

from __future__ import annotations

import json

import psycopg2
import psycopg2.errors
import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo

from .test_repository import _make_run, _make_unit, _succeed_and_select


# ======================================================================
# Legacy FK-parent fixture
# ======================================================================
#
# diffimages needs FK parents in exposures, l2files, refimages, scas,
# filters, pipelines and swversions. The baseline already seeds
# filters (fid 1..8), scas (sca 1..18), pipelines (ppid 15, 12, 17) and
# swversions (one row via its own sequence) -- see
# 20260921-01-baseline.sql's INSERT statements -- so this fixture reuses
# an existing row from each of those rather than inserting new ones, and
# only creates fresh exposures/l2files/refimages rows, which nothing
# seeds.

def _existing_seed_ids(cur):
    cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
    (fid,) = cur.fetchone()
    cur.execute("SELECT sca FROM scas ORDER BY sca LIMIT 1")
    (sca,) = cur.fetchone()
    cur.execute("SELECT ppid FROM pipelines ORDER BY ppid LIMIT 1")
    (ppid,) = cur.fetchone()
    cur.execute("SELECT svid FROM swversions ORDER BY svid LIMIT 1")
    (svid,) = cur.fetchone()
    return fid, sca, ppid, svid


def _make_exposure(cur, fid, field=1000, hp6=1, hp9=1):
    cur.execute(
        """
        INSERT INTO exposures (dateobs, field, hp6, hp9, fid, exptime, mjdobs)
        VALUES (now(), %s, %s, %s, %s, 100.0, 61000.0)
        RETURNING expid
        """,
        (field, hp6, hp9, fid),
    )
    (expid,) = cur.fetchone()
    return expid


def _make_l2file(cur, expid, sca, fid, version=1, vbest=1, field=1000, hp6=1, hp9=1):
    cur.execute(
        """
        INSERT INTO l2files (
            expid, sca, version, vbest, field, hp6, hp9, fid, dateobs,
            mjdobs, exptime, filename, checksum, crval1, crval2, crpix1,
            crpix2, cd11, cd12, cd21, cd22, ctype1, ctype2, cunit1,
            cunit2, equinox, ra, dec
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, now(),
            61000.0, 100.0, %s, '00000000000000000000000000000000',
            10.0, 20.0, 512.0, 512.0, 1.0, 0.0, 0.0, 1.0,
            'RA---TAN', 'DEC--TAN', 'deg', 'deg', 2000.0, 10.0, 20.0
        )
        RETURNING rid
        """,
        (expid, sca, version, vbest, field, hp6, hp9, fid,
         f"l2/{new_ulid()}.fits"),
    )
    (rid,) = cur.fetchone()
    return rid


def _make_refimage(cur, fid, ppid, svid, field=1000, version=1, vbest=1, hp6=1, hp9=1):
    cur.execute(
        """
        INSERT INTO refimages (field, hp6, hp9, fid, ppid, version, vbest, svid)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING rfid
        """,
        (field, hp6, hp9, fid, ppid, version, vbest, svid),
    )
    (rfid,) = cur.fetchone()
    return rfid


def _diffimages_fk_parents(cur, field=1000):
    """Insert the FK-parent chain a diffimages row needs; return a dict
    of the ids diffimages/diffimmeta columns reference."""
    fid, sca, ppid, svid = _existing_seed_ids(cur)
    expid = _make_exposure(cur, fid, field=field)
    rid = _make_l2file(cur, expid, sca, fid, field=field)
    rfid = _make_refimage(cur, fid, ppid, svid, field=field)
    return {
        "rid": rid, "expid": expid, "sca": sca, "ppid": ppid,
        "fid": fid, "rfid": rfid, "svid": svid, "field": field,
    }


_DIFFIMAGE_COLUMNS = (
    "rid, expid, sca, ppid, version, vbest, rfid, field, hp6, hp9, fid, "
    "ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4, "
    "infobitssci, infobitsref, filename, svid"
)


def _insert_diffimage(cur, parents, version=1, vbest=1, run=None, attempt=None, instance=None):
    cur.execute(
        f"""
        INSERT INTO diffimages ({_DIFFIMAGE_COLUMNS}, run, attempt, instance)
        VALUES (
            %(rid)s, %(expid)s, %(sca)s, %(ppid)s, %(version)s, %(vbest)s,
            %(rfid)s, %(field)s, 1, 1, %(fid)s,
            10.0, 20.0, 9.9, 19.9, 10.1, 19.9, 10.1, 20.1, 9.9, 20.1,
            0, 0, %(filename)s, %(svid)s,
            %(run)s, %(attempt)s, %(instance)s
        )
        RETURNING pid
        """,
        {**parents, "version": version, "vbest": vbest,
         "filename": f"diff/{new_ulid()}.fits",
         "run": run, "attempt": attempt, "instance": instance},
    )
    (pid,) = cur.fetchone()
    return pid


def _rebuild_chain(conn):
    """Create a run/unit/attempt/instance chain through the repository
    API, for a rebuild-style diffimages/diffimmeta insert."""
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = _succeed_and_select(conn, run_id, stage, unit_id)
    instance_id = new_ulid()
    with conn.cursor() as cur:
        _register_manifest_for_instance(conn, run_id, stage, attempt_id, instance_id)
    return run_id, attempt_id, instance_id


def _register_manifest_for_instance(conn, run_id, stage, attempt_id, instance_id):
    manifest = {
        "run": run_id,
        "unit": {"kind": "detector-image", "id": "e001/SCA01"},
        "stage": stage,
        "attempt": attempt_id,
        "inputs": {"manifest": "s3://inputs/manifest.json", "products": {}, "result_sets": []},
        "outputs": [
            {
                "kind": "difference-image",
                "format_version": "1",
                "instance": instance_id,
                "key": {"unit": "e001/SCA01"},
                "primary": f"diff/{instance_id}.fits",
                "members": [
                    {"role": "difference", "path": f"diff/{instance_id}.fits",
                     "bytes": 100, "sha256": "sha256:" + "0" * 64},
                ],
            },
        ],
    }
    repo.register_manifest(conn, manifest, registering_attempt_id=attempt_id)


# ======================================================================
# Column and FK existence
# ======================================================================

def test_diffimages_has_run_attempt_instance_columns_with_fks(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'diffimages' AND column_name IN ('run', 'attempt', 'instance')
            """
        )
        columns = {row[0] for row in cur.fetchall()}
        assert columns == {"run", "attempt", "instance"}

        cur.execute(
            """
            SELECT conname, confrelid::regclass::text
            FROM pg_constraint
            WHERE conrelid = 'diffimages'::regclass AND contype = 'f'
              AND conname IN ('diffimages_run_fkey', 'diffimages_attempt_fkey', 'diffimages_instance_fkey')
            """
        )
        fk_targets = dict(cur.fetchall())
    assert fk_targets.get("diffimages_run_fkey") == "runs"
    assert fk_targets.get("diffimages_attempt_fkey") == "attempts"
    assert fk_targets.get("diffimages_instance_fkey") == "product_instances"


def test_diffimmeta_has_run_attempt_instance_columns_with_fks(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'diffimmeta' AND column_name IN ('run', 'attempt', 'instance')
            """
        )
        columns = {row[0] for row in cur.fetchall()}
        assert columns == {"run", "attempt", "instance"}

        cur.execute(
            """
            SELECT conname, confrelid::regclass::text
            FROM pg_constraint
            WHERE conrelid = 'diffimmeta'::regclass AND contype = 'f'
              AND conname IN ('diffimmeta_run_fkey', 'diffimmeta_attempt_fkey', 'diffimmeta_instance_fkey')
            """
        )
        fk_targets = dict(cur.fetchall())
    assert fk_targets.get("diffimmeta_run_fkey") == "runs"
    assert fk_targets.get("diffimmeta_attempt_fkey") == "attempts"
    assert fk_targets.get("diffimmeta_instance_fkey") == "product_instances"


# ======================================================================
# Legacy-style insert: no run columns
# ======================================================================

def test_legacy_insert_with_no_run_columns_succeeds_and_reads_back_null(conn):
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2001)
        pid = _insert_diffimage(cur, parents)
        cur.execute("SELECT run, attempt, instance FROM diffimages WHERE pid = %s", (pid,))
        row = cur.fetchone()
    assert row == (None, None, None)


# ======================================================================
# Rebuild-style insert: all three set
# ======================================================================

def test_rebuild_insert_with_all_three_set_succeeds(conn):
    run_id, attempt_id, instance_id = _rebuild_chain(conn)
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2002)
        pid = _insert_diffimage(
            cur, parents, run=run_id, attempt=attempt_id, instance=instance_id)
        cur.execute("SELECT run, attempt, instance FROM diffimages WHERE pid = %s", (pid,))
        row = cur.fetchone()
    assert row == (run_id, attempt_id, instance_id)


# ======================================================================
# Together-CHECK: partial sets are refused
# ======================================================================

def test_setting_only_some_of_the_three_is_refused_by_check(conn):
    run_id, attempt_id, instance_id = _rebuild_chain(conn)
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2003)
        cur.execute("SAVEPOINT partial_run_columns")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_diffimage(cur, parents, run=run_id)  # attempt/instance NULL
        cur.execute("ROLLBACK TO SAVEPOINT partial_run_columns")


# ======================================================================
# Widened key: two runs may each hold (rid, ppid, version)
# ======================================================================

def test_two_runs_may_each_hold_same_rid_ppid_version(conn):
    run_a, attempt_a, instance_a = _rebuild_chain(conn)
    run_b, attempt_b, instance_b = _rebuild_chain(conn)
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2004)
        pid_a = _insert_diffimage(
            cur, parents, version=7, run=run_a, attempt=attempt_a, instance=instance_a)
        pid_b = _insert_diffimage(
            cur, parents, version=7, run=run_b, attempt=attempt_b, instance=instance_b)
    assert pid_a != pid_b


def test_same_rid_ppid_version_twice_in_one_run_is_refused(conn):
    run_id, attempt_id, instance_id = _rebuild_chain(conn)
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2005)
        _insert_diffimage(
            cur, parents, version=9, run=run_id, attempt=attempt_id, instance=instance_id)

        _, attempt_2, instance_2 = _second_instance_same_run(conn, run_id)
        cur.execute("SAVEPOINT dup_in_run")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_diffimage(
                cur, parents, version=9, run=run_id, attempt=attempt_2, instance=instance_2)
        cur.execute("ROLLBACK TO SAVEPOINT dup_in_run")


def _second_instance_same_run(conn, run_id):
    stage, unit_id = _make_unit(conn, run_id, unit_id=new_ulid())
    attempt_id = _succeed_and_select(conn, run_id, stage, unit_id)
    instance_id = new_ulid()
    _register_manifest_for_instance(conn, run_id, stage, attempt_id, instance_id)
    return unit_id, attempt_id, instance_id


def test_two_legacy_rows_with_same_rid_ppid_version_are_refused(conn):
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2006)
        _insert_diffimage(cur, parents, version=3)
        cur.execute("SAVEPOINT dup_legacy")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_diffimage(cur, parents, version=3)
        cur.execute("ROLLBACK TO SAVEPOINT dup_legacy")


# ======================================================================
# instance UNIQUE on diffimages
# ======================================================================

def test_instance_unique_on_diffimages(conn):
    run_id, attempt_id, instance_id = _rebuild_chain(conn)
    with conn.cursor() as cur:
        parents_a = _diffimages_fk_parents(cur, field=2007)
        _insert_diffimage(
            cur, parents_a, version=1, run=run_id, attempt=attempt_id, instance=instance_id)

        parents_b = _diffimages_fk_parents(cur, field=2008)
        cur.execute("SAVEPOINT dup_instance")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_diffimage(
                cur, parents_b, version=1, run=run_id, attempt=attempt_id, instance=instance_id)
        cur.execute("ROLLBACK TO SAVEPOINT dup_instance")


# ======================================================================
# diffimmeta.source_counts
# ======================================================================

def _insert_diffimmeta(cur, pid, fid, sca, source_counts=None):
    cur.execute(
        """
        INSERT INTO diffimmeta (
            pid, nsexcatsources, scalefacref, dxrmsfin, dyrmsfin,
            dxmedianfin, dymedianfin, field, hp6, hp9, fid, sca,
            source_counts
        ) VALUES (
            %s, 10, 1.0, 0.01, 0.01, 0.0, 0.0, 1000, 1, 1, %s, %s, %s
        )
        """,
        (pid, fid, sca, source_counts),
    )


def test_diffimmeta_source_counts_accepts_object(conn):
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2009)
        pid = _insert_diffimage(cur, parents, version=1)
        _insert_diffimmeta(
            cur, pid, parents["fid"], parents["sca"],
            source_counts=json.dumps({"sextractor": {"positive": 1, "negative": 2}}))
        cur.execute("SELECT source_counts FROM diffimmeta WHERE pid = %s", (pid,))
        (source_counts,) = cur.fetchone()
    assert source_counts == {"sextractor": {"positive": 1, "negative": 2}}


def test_diffimmeta_source_counts_refuses_array(conn):
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2010)
        pid = _insert_diffimage(cur, parents, version=1)
        cur.execute("SAVEPOINT source_counts_array")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_diffimmeta(
                cur, pid, parents["fid"], parents["sca"],
                source_counts=json.dumps([1, 2, 3]))
        cur.execute("ROLLBACK TO SAVEPOINT source_counts_array")


def test_diffimmeta_source_counts_refuses_string(conn):
    with conn.cursor() as cur:
        parents = _diffimages_fk_parents(cur, field=2011)
        pid = _insert_diffimage(cur, parents, version=1)
        cur.execute("SAVEPOINT source_counts_string")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_diffimmeta(
                cur, pid, parents["fid"], parents["sca"],
                source_counts=json.dumps("not an object"))
        cur.execute("ROLLBACK TO SAVEPOINT source_counts_string")


# ======================================================================
# diffimagespk still exists, now spans four columns
# ======================================================================

def test_diffimagespk_constraint_spans_four_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'diffimages'::regclass AND conname = 'diffimagespk'"
        )
        row = cur.fetchone()
    assert row is not None
    definition = row[0]
    assert "rid" in definition
    assert "ppid" in definition
    assert "version" in definition
    assert "run" in definition
    assert "NULLS NOT DISTINCT" in definition
