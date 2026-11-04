"""Readers of sources and xsources read child tables, and read the same rows the parents give.

``sources_of`` and ``xsources_of`` (``database/schema.sql``) and the named queries and
operator scripts rewritten on them (rapid_docs decisions.md, children-reads) are checked
against readings of the parent tables on the same data; test code may read a parent.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

from rapidpipe.foundation import publish
from tests.db.foundation_helpers import DATASET, new_job, new_run, write_attempt
from tests.db.proof.fixture import World
from tests.db.test_children import NOV4, source_rows, xsource_rows
from tests.db.test_queries import load_queries, world  # noqa: F401 (the fixture)

REPO = Path(__file__).resolve().parents[2]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_sources_of_and_xsources_of_read_the_sets_rows_from_their_children(connect, tmp_path):
    conn = connect()
    run = new_run(conn)
    jobs = []
    for n, (mjdobs, sca, with_l2) in enumerate(((NOV4 + 0.2, 8, True), (NOV4 + 1.3, 3, False))):
        job = new_job(conn, run)
        key = {"dataset": DATASET, "expid": n + 1, "sca": sca, "fid": 1}
        diff = {"pid": 500 + n, **key, "field": 1, "jd": mjdobs + 2400000.5, "infobitssci": 0,
                "infobitsref": 0, "status": 1, "filename": "d.fits",
                **{f"{c}{i}": 1.0 for i in range(5) for c in ("ra", "dec")}}
        tables = {"diffimages": [diff], "sources": source_rows(mjdobs, sca, expid=n + 1),
                  "xsources": xsource_rows(mjdobs, sca)}
        if with_l2:                                   # the lineage the stages write; the other
            diff["rid"] = 9000 + n                    # set has none and falls back to its jd
            tables["l2files"] = [{"rid": 9000 + n, **key, "field": 1, "hp6": 1, "hp9": 1,
                                  "dateobs": "2026-11-04T04:48:00", "mjdobs": mjdobs, "exptime": 60.0,
                                  "filename": "l2.fits", "checksum": "0" * 32, "crval1": 1.0,
                                  "crval2": 1.0, "crpix1": 1.0, "crpix2": 1.0, "cd11": 1.0, "cd12": 0.0,
                                  "cd21": 0.0, "cd22": 1.0, "ra": 1.0, "dec": 1.0,
                                  **{f"{c}{i}": 1.0 for i in range(5) for c in ("ra", "dec")}}]
        publish(conn, job, 1, write_attempt(tmp_path / f"job-{job}", tables))
        jobs.append(job)
    empty = new_job(conn, run)                        # a set with no rows: no child, no rows

    for table in ("sources", "xsources"):
        key = "sid" if table == "sources" else "xsid"
        want = _rows(conn, f"SELECT * FROM {table} WHERE job = ANY(%s) ORDER BY job, {key}",
                     (jobs + [empty],))
        got = _rows(conn, f"SELECT * FROM {table}_of(%s::bigint[]) ORDER BY job, {key}",
                    (jobs + [empty],))
        assert got == want and len(got) == (6 if table == "sources" else 4)
        assert _rows(conn, f"SELECT count(*) FROM {table}_of(%s::bigint[])", ([jobs[1]],)) == [
            (3 if table == "sources" else 2,)]
        assert _rows(conn, f"SELECT count(*) FROM {table}_of(%s::bigint[])", ([empty],)) == [(0,)]
    assert _rows(conn, "SELECT source_set_child('sources', %s), source_set_child('xsources', %s)",
                 (jobs[0], jobs[1])) == [("sources_20261104_8", "xsources_20261105_3")]


#: The three named queries as they read the parents before the change (rapid 523), the reference.
QUERIES_ON_PARENTS = {
    "current_image_and_source_set": """
SELECT l.rid, l.filename AS l2file, d.pid, d.filename AS diffimage, o.job AS source_set,
       (SELECT count(*) FROM sources s WHERE s.job = o.job) AS nsources
FROM l2files_official l
LEFT JOIN diffimages_official d
    ON (d.dataset, d.expid, d.sca, d.fid) = (l.dataset, l.expid, l.sca, l.fid)
LEFT JOIN official o
    ON o.closed_at IS NULL AND o.product = 'sources'
    AND o.key = jsonb_build_object('dataset', l.dataset, 'expid', l.expid, 'sca', l.sca, 'fid', l.fid)
WHERE l.dataset = %(dataset)s AND l.expid = %(expid)s AND l.sca = %(sca)s AND l.fid = %(fid)s""",
    "object_history": """
SELECT * FROM (
    SELECT 'detection' AS kind, s.mjdobs, s.fluxfit::double precision AS flux,
           s.fluxerr::double precision AS fluxerr, m.source_job AS job, m.sid AS id
    FROM memberships_official m
    JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
    WHERE m.object_job = %(object_job)s AND m.aid = %(aid)s
    UNION ALL
    (SELECT DISTINCT ON (f.forced_id) 'forced', f.mjdobs, f.psf_flux, f.psf_fluxerr, f.job,
            f.forced_id
     FROM forced f
     WHERE f.aid = %(aid)s
       AND EXISTS (SELECT 1 FROM packets p WHERE p.job = f.job AND p.aid = f.aid
                   AND p.object_job = %(object_job)s)
     ORDER BY f.forced_id, f.job)
) history
ORDER BY mjdobs, kind, job, id""",
    "chip_sources": """
SELECT s.sid, s.ra, s.dec, s.fluxfit, s.fluxerr, f.filter, e.exptime
FROM sources_official s
JOIN filters f ON f.fid = s.fid
LEFT JOIN exposures_official e ON (e.dataset, e.expid) = (s.dataset, s.expid)
WHERE s.dataset = %(dataset)s AND s.expid = %(expid)s AND s.sca = %(sca)s AND s.fid = %(fid)s
  AND s.flags & ~16384 = 0
ORDER BY s.sid""",
}


def test_the_named_queries_give_the_rows_they_gave_on_the_parents(world):
    w, params = world
    queries = load_queries()
    for name, reference in QUERIES_ON_PARENTS.items():
        assert "_of(" in queries[name][1]
        with w.conn.cursor() as cur:
            cur.execute(reference, params)
            want = cur.fetchall()
            cur.execute(queries[name][1], params)
            got = cur.fetchall()
        w.conn.commit()
        assert want, f"{name} returned no rows to compare"
        assert got == want, name


def _proof_run(connect, tmp_path, settings) -> tuple[World, int, dict]:
    w = World(connect(), tmp_path / "world")
    run = w.run("children-reads", settings=settings, live=True)
    return w, run, w.recipe(run, epochs=(1, 2))


def test_validate_runs_checks_pass_and_read_the_parents_rows(connect, tmp_path):
    w, run, made = _proof_run(connect, tmp_path, {
        "associate": {"match_radius": 1.0e-4, "source_flags": 0, "min_snr": 0.0}})
    twice = w.difference(run, 1, 1, expects=None)
    first = made["differences"][(1, 1)]
    validate = _script("validate_run")
    validate._s3 = None
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        verdicts = {"lineage": validate.check_lineage(run), "catalog": validate.check_catalog(run),
                    "counts": validate.check_counts(run), "tables": validate.check_tables(run),
                    "twice": validate.check_twice(first, twice)}
    assert verdicts == dict.fromkeys(verdicts, True), out.getvalue()
    pairs = [tuple(r) for r in w.rows("SELECT DISTINCT object_job, aid FROM memberships WHERE job = %s",
                                      (first,))]
    reference: dict = {}
    for object_job, aid, source_job, sid, mjdobs in w.rows(
            "SELECT DISTINCT m.object_job, m.aid, m.source_job, m.sid, s.mjdobs FROM memberships m"
            " JOIN sources s ON s.job = m.source_job AND s.sid = m.sid JOIN job_inputs i"
            " ON i.job = %s AND i.kind = 'input' AND i.product = 'sources' AND i.source_job = m.job"
            " WHERE (m.object_job, m.aid) IN (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))",
            (made["packets"][(1, 1)], [p[0] for p in pairs], [p[1] for p in pairs])):
        reference.setdefault((object_job, aid), {})[(source_job, sid)] = mjdobs
    assert reference and dict(validate.members_by_aid(made["packets"][(1, 1)], pairs)) == reference


def test_completeness_reads_the_run_s_official_sources(connect, tmp_path):
    w, run, _ = _proof_run(connect, tmp_path, {"intake": {}})
    got = _script("completeness").load(w.conn, run)["sources"]
    want = w.rows("SELECT s.job, s.sid FROM sources_official s WHERE s.run = %s ORDER BY 1, 2", (run,))
    assert len(want) and sorted(zip(got["job"], got["sid"])) == [tuple(r) for r in want]
