"""The foundation schema, database/schema.sql (foundation plan v5, Part 1).

Run by .github/workflows/foundation.yml against a database the schema was
applied to; db-migrations.yml ignores this module, so the migration stream
and the foundation schema never collect each other's tests.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import psycopg2
import pytest
from psycopg2 import errors

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema.sql"

BOOKKEEPING = ("runs", "jobs", "job_inputs", "official")
STATIC = ("filters", "scas", "surveys", "fields")
PRODUCT_TABLES = (
    "exposures", "l2files", "refimages", "refimimages", "refimcatalogs",
    "diffimages", "psfs", "sources", "objects", "memberships", "packets",
    "forced", "external", "catalogs",
)
VIEWS = tuple(f"{t}_official" for t in PRODUCT_TABLES) + ("job_metrics",)


def _admin_connection():
    connection = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname="postgres",
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )
    connection.autocommit = True
    return connection


def test_schema_applies_to_an_empty_database(conn):
    name = f"rapid_schema_apply_{os.getpid()}"
    admin = _admin_connection()
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name}")
            cur.execute(f"CREATE DATABASE {name}")
        result = subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", name, "-f", str(SCHEMA)],
            capture_output=True, text=True, env={**os.environ, "PGDATABASE": name})
        assert result.returncode == 0, result.stderr
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name}")
        admin.close()


def test_the_plans_tables_and_views_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
        views = {r[0] for r in cur.fetchall()}
    assert set(BOOKKEEPING + STATIC + PRODUCT_TABLES) <= tables
    assert set(VIEWS) <= views


def _run_and_job(cur):
    cur.execute(
        "INSERT INTO runs (who, why, revision, settings, survey, dataset)"
        " VALUES ('t', 'test', 'abc', '{}', 'S', 'D') RETURNING id")
    run = cur.fetchone()[0]
    cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, 'difference', '{}') RETURNING id",
                (run,))
    return run, cur.fetchone()[0]


def _job(cur, run):
    cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, 'difference', '{}') RETURNING id",
                (run,))
    return cur.fetchone()[0]


def _open(cur, job, key='{"dataset": "D", "survey": "S", "field": 1}', product="objects"):
    cur.execute(
        "INSERT INTO official (product, key, job, opened_group, opened_by, opened_why)"
        " VALUES (%s, %s, %s, 1, 't', 'test') RETURNING id", (product, key, job))
    return cur.fetchone()[0]


def test_one_open_binding_per_key_and_another_after_it_closes(conn):
    with conn.cursor() as cur:
        run, first = _run_and_job(cur)
        second = _job(cur, run)
        binding = _open(cur, first)
        cur.execute("SAVEPOINT s")
        with pytest.raises(errors.UniqueViolation):
            _open(cur, second)
        cur.execute("ROLLBACK TO SAVEPOINT s")
        cur.execute(
            "UPDATE official SET closed_group = 2, closed_by = 't', closed_at = now(),"
            " closed_why = 'test' WHERE id = %s", (binding,))
        _open(cur, second)
        cur.execute("SELECT job FROM official WHERE product = 'objects' AND closed_at IS NULL")
        assert cur.fetchall() == [(second,)]


def test_a_product_row_without_a_job_is_refused(conn):
    with conn.cursor() as cur:
        with pytest.raises(errors.NotNullViolation) as refused:
            cur.execute(
                "INSERT INTO psfs (job, dataset, fid, sca, filename, checksum)"
                " VALUES (NULL, 'D', 1, 1, 'psf.fits', 'x')")
    assert refused.value.diag.column_name == "job"


def test_the_psf_key_carries_its_kind(conn):
    """A science and a reference PSF of one dataset, filter and SCA are two
    keys, both official at once; a binding whose key lacks the kind shows
    no row; a kind other than the two is refused."""
    with conn.cursor() as cur:
        run, science = _run_and_job(cur)
        reference = _job(cur, run)
        for job, kind in ((science, "science"), (reference, "reference")):
            cur.execute("INSERT INTO psfs (job, dataset, fid, sca, kind, filename, checksum)"
                        " VALUES (%s, 'D', 1, 1, %s, 'psf.fits', 'x')", (job, kind))
            _open(cur, job, f'{{"dataset": "D", "fid": 1, "sca": 1, "kind": "{kind}"}}', "psfs")
        cur.execute("SELECT kind, job FROM psfs_official ORDER BY kind")
        assert cur.fetchall() == [("reference", reference), ("science", science)]

        cur.execute("UPDATE official SET key = key - 'kind' WHERE product = 'psfs' AND job = %s",
                    (reference,))
        cur.execute("SELECT kind FROM psfs_official")
        assert cur.fetchall() == [("science",)]

        with pytest.raises(errors.CheckViolation):
            cur.execute("INSERT INTO psfs (job, dataset, fid, sca, kind, filename, checksum)"
                        " VALUES (%s, 'D', 1, 1, 'difference', 'psf.fits', 'x')", (science,))


def test_the_catalog_views_follow_the_source_sets_bindings(conn):
    """Object A founded by difference job D1, joined by D2 and D3; D1 and D2's source sets
    official, D3's not (superseded). memberships_official is D1's and D2's rows;
    objects_official is A with their two members' sums; an object B whose only member is
    D3's is absent from the view and present in the table."""
    with conn.cursor() as cur:
        run, d1 = _run_and_job(cur)
        d2, d3 = _job(cur, run), _job(cur, run)
        for aid, ra0 in ((1, 10.0), (2, 11.0)):
            cur.execute("INSERT INTO objects (job, aid, dataset, survey, field, ra0, dec0)"
                        " VALUES (%s, %s, 'D', 'S', 1, %s, -5.0)", (d1, aid, ra0))
        members = [(d1, 1, 0.0, 0.0, 10.0), (d2, 1, 2e-6, -1e-6, 12.0), (d3, 1, 4e-6, 0.0, 99.0),
                   (d3, 2, 0.0, 0.0, 5.0)]
        cur.executemany("INSERT INTO memberships (job, object_job, aid, source_job, sid, dra, ddec, flux)"
                        " VALUES (%s, %s, %s, %s, 1, %s, %s, %s)",
                        [(j, d1, aid, j, dra, ddec, flux) for j, aid, dra, ddec, flux in members])
        for epoch, job in ((1, d1), (2, d2)):
            _open(cur, job, f'{{"dataset": "D", "expid": {epoch}, "sca": 1, "fid": 1}}', "sources")

        cur.execute("SELECT DISTINCT job FROM memberships_official ORDER BY job")
        official = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT aid, nsources, sumra, sumra2, sumdec, sumflux, sumflux2, run"
                    " FROM objects_official")
        objects = cur.fetchall()
        cur.execute("SELECT count(*) FROM objects")
        (stored,) = cur.fetchone()

    assert official == [d1, d2]
    ((aid, n, sumra, sumra2, sumdec, sumflux, sumflux2, objects_run),) = objects
    assert (aid, n, objects_run, stored) == (1, 2, run, 2)
    assert (sumra, sumdec, sumflux, sumflux2) == pytest.approx((2e-6, -1e-6, 22.0, 244.0))
    assert sumra2 == pytest.approx(4e-12)


@pytest.mark.skipif(os.environ.get("CI") != "true",
                    reason="q3c is required only in CI; a developer's container may lack it")
def test_q3c_is_present_in_ci(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'q3c'")
        assert cur.fetchone() == (1,)


def test_job_metrics_exposes_each_numeric_metric_with_the_jobs_scope(conn):
    with conn.cursor() as cur:
        run, _ = _run_and_job(cur)
        cur.execute(
            "INSERT INTO jobs (run, stage, scope, manifest) VALUES (%s, 'difference', %s, %s)"
            " RETURNING id",
            (run, '{"expid": 4040101, "sca": 8, "fid": 8}',
             '{"metrics": {"n_sources": 500, "core_fraction": 0.03, "note": "x"}}'))
        job = cur.fetchone()[0]
        cur.execute("SELECT run, stage, dataset, expid, sca, fid, field, metric, value"
                    " FROM job_metrics WHERE job = %s ORDER BY metric", (job,))
        rows = cur.fetchall()
    assert rows == [(run, "difference", "D", 4040101, 8, 8, None, "core_fraction", 0.03),
                    (run, "difference", "D", 4040101, 8, 8, None, "n_sources", 500.0)]


def test_job_metrics_reads_a_jobs_container_time_and_its_submissions(conn):
    attempts = ('{"b-1": {"queue": "spot-q", "started": "2026-10-24T08:00:00+00:00",'
                ' "stopped": "2026-10-24T08:10:00+00:00", "status_reason": "Host EC2 (instance i-1) terminated."},'
                ' "b-2": {"queue": "prompt-q", "started": "2026-10-24T08:11:00+00:00",'
                ' "ended": "2026-10-24T08:12:00+00:00", "outcome": "published"}}')
    with conn.cursor() as cur:
        run, _ = _run_and_job(cur)
        cur.execute(
            "INSERT INTO jobs (run, stage, scope, state, published_attempt, started, ended, attempts)"
            " VALUES (%s, 'intake', '{}', 'published', 2, '2026-10-24T08:11:00Z', '2026-10-24T08:12:00Z', %s)"
            " RETURNING id", (run, attempts))
        job = cur.fetchone()[0]
        cur.execute("SELECT metric, value FROM job_metrics WHERE job = %s ORDER BY metric", (job,))
        rows = dict(cur.fetchall())
    assert rows == {"batch_submissions": 2.0, "container_seconds": 60.0, "hedges": 0.0,
                    "lost_seconds": 600.0, "reclaims": 1.0}


def _official_views(cur) -> dict[str, tuple[str, str]]:
    """Each product table's official view: its bound product and its definition."""
    views = {}
    for table in PRODUCT_TABLES:
        cur.execute("SELECT pg_get_viewdef(%s::regclass)", (f"{table}_official",))
        definition = cur.fetchone()[0]
        views[table] = (re.search(r"o\.product = '(\w+)'", definition).group(1), definition)
    return views


def _columns(cur, relation: str) -> dict[str, bool]:
    """A table's or view's columns, each with whether it is NOT NULL."""
    cur.execute("SELECT attname, attnotnull FROM pg_attribute"
                " WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped", (relation,))
    return dict(cur.fetchall())


def test_every_bound_product_carries_the_dataset_in_its_key_and_its_view(conn):
    """Dataset is in every scientific key (plan Part 1): a table bound under
    its own product name holds ``dataset`` NOT NULL, its view exposes it, and
    a view that matches the binding's key builds the key with it. A set's
    child rows (members, catalogs of a reference, forced and external rows of
    a packet set) are bound through their set's job and carry no key."""
    with conn.cursor() as cur:
        views = _official_views(cur)
        bound = {t: d for t, (product, d) in views.items() if product == t}
        assert bound, "no product table is bound under its own name"
        for table, definition in bound.items():
            assert _columns(cur, table).get("dataset") is True, f"{table}.dataset NOT NULL"
            assert "dataset" in _columns(cur, f"{table}_official"), f"{table}_official.dataset"
            for key in re.findall(r"o\.key = jsonb_build_object\(([^)]*)\)", definition):
                assert re.search(r"'dataset'(::text)?, t\.dataset", key), f"{table} key {key}"
        for table, (product, _) in views.items():
            if product != table:
                assert product in bound, f"{table} is bound through {product}, not a bound product"
