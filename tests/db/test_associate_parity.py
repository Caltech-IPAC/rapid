"""The in-memory matcher agrees with the branch's SQL matcher on field 4658155 (milestone 2).

The fixture is the field's two imported source sets (the 500 brightest
per difference image, simulated positions). Both matchers take the
sources the association accepts (``flags = 0``, ``fluxfit / fluxerr
>= 5``) and run epoch 1 then epoch 2 on one growing catalog, each epoch
as the branch's crossmatch ran it (``git show
bd626950:rapidpipe/stages/crossmatch.py``): pass 1 exposure by exposure in
ascending MJD, ``q3c_join`` all-within-radius against the catalog, the
unmatched sources' new objects inserted after the exposure with ``ON
CONFLICT DO NOTHING``. Contract: the same objects (ids and founding
positions), the same memberships and the same sums. The branch's pass 2
(the neighbouring fields' sources against the catalog, memberships only)
is not replayed: the field rule (runbook part 12 step 1) retired it, and
a source founds in its own field, which is pass 1 on that field.

At dev's radius no epoch-2 source of this field lies within it of an
epoch-1 object (the closest pair is 0.37 arcseconds apart), so the
replay also runs at 5 arcseconds (one cross-epoch match) and 30 (sources
joining several objects), on the field's real geometry.

Needs q3c: asserted under ``CI=true``, skipped with that reason otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from rapidpipe.science.crossmatch.catalog import expid_order, new_object_id
from rapidpipe.science.crossmatch.memory import Catalog, Source, associate
from rapidpipe.science.statistics.sums import Sums

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "associate" / "field-4658155-two-epochs.parquet"
FIELD = 4658155
MIN_SNR = 5.0


def accepted() -> list[dict]:
    rows = pq.read_table(FIXTURE).to_pylist()
    return [r for r in rows if r["flags"] == 0 and r["fluxerr"] > 0
            and r["fluxfit"] / r["fluxerr"] >= MIN_SNR]


def q3c_present(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'q3c'")
        present = cur.fetchone() is not None
    if not present and os.environ.get("CI") == "true":
        pytest.fail("q3c is not installed in CI")
    return present


def sql_matcher(cur, rows, radius):
    cur.execute("CREATE TEMP TABLE src (epoch int, sid bigint, expid int, mjdobs float8,"
                " field int, ra float8, dec float8, fluxfit float8) ON COMMIT DROP")
    cur.executemany("INSERT INTO src VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(r["epoch"], r["sid"], r["expid"], r["mjdobs"], r["field"], r["ra"], r["dec"],
                      r["fluxfit"]) for r in rows])
    cur.execute("CREATE TEMP TABLE cat (aid bigint PRIMARY KEY, ra0 float8, dec0 float8) ON COMMIT DROP")
    cur.execute("CREATE INDEX ON cat (q3c_ang2ipix(ra0, dec0))")
    cur.execute("CREATE TEMP TABLE mem (aid bigint, epoch int, sid bigint, UNIQUE (aid, epoch, sid))"
                " ON COMMIT DROP")
    for epoch in (1, 2):
        cur.execute("SELECT DISTINCT expid, mjdobs FROM src WHERE field = %s AND epoch = %s",
                    (FIELD, epoch))
        for expid in expid_order(cur.fetchall()):
            cur.execute("SELECT a.sid, b.aid FROM src a, cat b"
                        " WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, %s)"
                        " AND a.field = %s AND a.expid = %s AND a.epoch = %s",
                        (radius, FIELD, expid, epoch))
            matched = cur.fetchall()
            cur.execute("SELECT sid, ra, dec FROM src WHERE field = %s AND expid = %s"
                        " AND epoch = %s ORDER BY sid", (FIELD, expid, epoch))
            unmatched = [r for r in cur.fetchall() if r[0] not in {sid for sid, _ in matched}]
            for sid, ra, dec in unmatched:
                cur.execute("INSERT INTO cat VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (new_object_id(ra, dec), ra, dec))
            cur.executemany("INSERT INTO mem VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            [(aid, epoch, sid) for sid, aid in matched]
                            + [(new_object_id(ra, dec), epoch, sid) for sid, ra, dec in unmatched])
    cur.execute("SELECT aid, ra0, dec0 FROM cat")
    objects = {aid: (ra0, dec0) for aid, ra0, dec0 in cur.fetchall()}
    cur.execute("SELECT aid, epoch, sid FROM mem")
    memberships = set(cur.fetchall())
    cur.execute("SELECT m.aid, count(*), sum(s.ra - c.ra0), sum((s.ra - c.ra0) ^ 2),"
                " sum(s.dec - c.dec0), sum((s.dec - c.dec0) ^ 2), sum(s.fluxfit), sum(s.fluxfit ^ 2)"
                " FROM mem m JOIN src s ON s.epoch = m.epoch AND s.sid = m.sid"
                " JOIN cat c ON c.aid = m.aid GROUP BY m.aid")
    sums = {r[0]: r[1:] for r in cur.fetchall()}
    return objects, memberships, sums


def memory_matcher(rows, radius):
    catalog = Catalog(radius)
    memberships = []
    for epoch in (1, 2):
        sources = [Source(source_job=epoch, sid=r["sid"], expid=r["expid"], mjdobs=r["mjdobs"],
                          field=r["field"], ra=r["ra"], dec=r["dec"])
                   for r in rows if r["epoch"] == epoch and r["field"] == FIELD]
        result = associate(catalog, sources)
        memberships += result.memberships
    by_key = {(r["epoch"], r["sid"]): r for r in rows}
    sums = {aid: Sums(ra0, dec0) for aid, (ra0, dec0) in catalog.positions.items()}
    for aid, epoch, sid in memberships:
        r = by_key[(epoch, sid)]
        sums[aid].add(r["ra"], r["dec"], r["fluxfit"])
    return dict(catalog.positions), memberships, sums


@pytest.mark.parametrize("radius", [1.528e-05, 5 / 3600, 30 / 3600], ids=["dev", "5arcsec", "30arcsec"])
def test_the_in_memory_matcher_agrees_with_the_sql_matcher_on_the_field(conn, radius):
    if not q3c_present(conn):
        pytest.skip("q3c is not installed here; the parity test runs in CI")
    rows = accepted()
    with conn.cursor() as cur:
        sql_objects, sql_memberships, sql_sums = sql_matcher(cur, rows, radius)
    objects, memberships, sums = memory_matcher(rows, radius)

    assert objects == sql_objects
    assert len(memberships) == len(set(memberships))
    assert set(memberships) == sql_memberships
    for aid, (n, sra, sra2, sdec, sdec2, sflux, sflux2) in sql_sums.items():
        s = sums[aid]
        assert s.nsources == n
        assert (s.sumra, s.sumra2, s.sumdec, s.sumdec2) == pytest.approx(
            (sra, sra2, sdec, sdec2), rel=1e-9, abs=1e-15)
        assert (s.sumflux, s.sumflux2) == pytest.approx((sflux, sflux2), rel=1e-9)
    if radius >= 5 / 3600:
        assert any(s.nsources > 1 for s in sums.values())
    if radius >= 30 / 3600:
        assert len(memberships) > len({(epoch, sid) for _, epoch, sid in memberships})
