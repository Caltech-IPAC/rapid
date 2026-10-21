"""The foundation schema, database/schema.sql (foundation plan v5, Part 1).

Run by .github/workflows/foundation.yml against a database the schema was
applied to; db-migrations.yml ignores this module, so the migration stream
and the foundation schema never collect each other's tests.
"""

from __future__ import annotations

import os
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
VIEWS = tuple(f"{t}_official" for t in PRODUCT_TABLES) + ("members_official",)


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
