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
VIEWS = tuple(f"{t}_official" for t in PRODUCT_TABLES) + ("members_official", "job_metrics")


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
    cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, 'associate', '{}') RETURNING id",
                (run,))
    return run, cur.fetchone()[0]


def _job(cur, run):
    cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, 'associate', '{}') RETURNING id",
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


def test_members_official_follows_the_chain_and_omits_excluded_source_sets(conn):
    """Sets A <- B <- C, C official. A recorded members from source sets S1
    and S2, B from S3, C from S4; C excludes S2. The view returns S1, S3 and
    S4's members and not S2's, which still exist in memberships."""
    with conn.cursor() as cur:
        run, a = _run_and_job(cur)
        b, c = _job(cur, run), _job(cur, run)
        s1, s2, s3, s4 = (_job(cur, run) for _ in range(4))
        key = '{"dataset": "D", "survey": "S", "field": 1}'
        for job, predecessor in ((b, a), (c, b)):
            cur.execute(
                "INSERT INTO job_inputs (job, kind, product, key, source_job)"
                " VALUES (%s, 'input', 'objects', %s, %s)", (job, key, predecessor))
        cur.execute(
            "INSERT INTO job_inputs (job, kind, product, key, source_job)"
            " VALUES (%s, 'excludes', 'sources', %s, %s)",
            (c, '{"dataset": "D", "expid": 1, "sca": 1, "fid": 1}', s2))
        members = [(a, 1, s1, 10), (a, 2, s2, 20), (b, 1, s3, 30), (c, 3, s4, 40)]
        cur.executemany(
            "INSERT INTO memberships (job, aid, source_job, sid) VALUES (%s, %s, %s, %s)", members)
        binding = _open(cur, c, key)

        cur.execute("SELECT job, aid, source_job, sid, set_job, binding, run FROM members_official")
        rows = sorted(cur.fetchall())
        cur.execute("SELECT count(*) FROM memberships WHERE source_job = %s", (s2,))
        (kept,) = cur.fetchone()

    assert rows == sorted((j, aid, s, sid, c, binding, run)
                          for j, aid, s, sid in members if s != s2)
    assert kept == 1


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
