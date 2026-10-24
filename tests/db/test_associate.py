"""The associate stage through ``run_job`` against the schema.

Contracts: under a live run, an epoch's associate job binds the field's
object set by itself, and the next epoch's job, expecting that binding,
moves it; ``members_official`` returns both epochs' memberships through
the chain; the second set carries the first's objects forward with sums
grown by the second epoch's members; a job told at launch that a source
set is excluded drops its members from the sums and the members view
drops them, while their rows stay. A source whose only flag is the
informational bright footprint is accepted; any other bit rejects it.
"""

from __future__ import annotations

import contextlib

from rapidpipe.foundation import create_job, create_run, change, publish, run_job
from rapidpipe.products.diffimage import SOURCE_FLAG_BRIGHT_FOOTPRINT
from rapidpipe.stages import associate
from tests.db.foundation_helpers import write_attempt

SURVEY, DATASET, FIELD = "S", "D", 4658155
OBJECT_KEY = {"dataset": DATASET, "survey": SURVEY, "field": FIELD}
SETTINGS = {"associate": {"match_radius": 1.528e-05, "source_flags": 0, "min_snr": 5.0}}
R = 1.528e-05


def source_row(expid, sid, ra, dec, flux=1000.0, flags=0, field=FIELD):
    return {"sid": sid, "dataset": DATASET, "expid": expid, "sca": 8, "fid": 8, "pid": expid,
            "id": sid, "field": field, "hp6": 0, "hp9": 0, "mjdobs": 60000.0 + expid,
            "ra": ra, "dec": dec, "xfit": 10.0, "yfit": 10.0, "xerr": 0.1, "yerr": 0.1,
            "fluxfit": flux, "fluxerr": 10.0, "flags": flags, "qfit": 0.0, "cfit": 0.0,
            "npixfit": 25, "sharpness": 0.0, "roundness1": 0.0, "roundness2": 0.0, "peak": 1.0}


def source_set(conn, run, root, expid, rows):
    """Publish a source set of ``rows`` for exposure ``expid``; return its job."""
    key = {"dataset": DATASET, "expid": expid, "sca": 8, "fid": 8}
    job = create_job(conn, run, "import", {}, expects=[{"product": "sources", "key": key,
                                                        "binding": None}])
    publish(conn, job, 1, write_attempt(root / f"sources-{job}", {"sources": rows}))
    return job


def object_binding(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, job FROM official WHERE closed_at IS NULL AND product = 'objects'")
        row = cur.fetchone()
    conn.commit()
    return row


def query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_two_epochs_move_the_binding_and_an_exclusion_drops_members(connect, tmp_path):
    conn = connect()
    run = create_run(conn, "tester", "associate", "abc123", SETTINGS, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    factory = lambda: contextlib.closing(connect())  # noqa: E731
    root = tmp_path.as_uri()

    def associate_job(sets, predecessor=None, excludes=()):
        inputs = [{"product": "sources", "key": {}, "source_job": s} for s in sets]
        if predecessor is not None:
            inputs.append({"product": "objects", "key": OBJECT_KEY, "source_job": predecessor})
        bound = object_binding(conn)
        job = create_job(conn, run, "associate", {"field": FIELD}, inputs=inputs,
                         expects=[{"product": "objects", "key": OBJECT_KEY,
                                   "binding": bound[0] if bound else None}],
                         excludes=[{"key": {}, "source_job": x} for x in excludes])
        result = run_job(factory, job, root, associate.run)
        assert result["published"] and result["refusal"] is None
        return job

    epoch1 = source_set(conn, run, tmp_path, 1, [
        source_row(1, 1, 100.0, 0.0, flux=1000.0),
        source_row(1, 2, 100.1, 0.0),
        source_row(1, 3, 100.2, 0.0, flags=4),          # flagged: not accepted
        source_row(1, 4, 100.3, 0.0, flux=20.0),        # S/N 2: not accepted
        source_row(1, 5, 100.4, 0.0, flags=4 | SOURCE_FLAG_BRIGHT_FOOTPRINT)])  # not accepted
    first = associate_job([epoch1])
    assert object_binding(conn)[1] == first
    assert query(conn, "SELECT count(*) FROM objects_official")[0][0] == 2

    epoch2 = source_set(conn, run, tmp_path, 2, [
        source_row(2, 1, 100.0 + 0.5 * R, 0.0, flux=1200.0),
        source_row(2, 2, 101.0, 0.0, flags=SOURCE_FLAG_BRIGHT_FOOTPRINT)])  # accepted
    second = associate_job([epoch2], predecessor=first)
    assert object_binding(conn)[1] == second
    members = query(conn, "SELECT job, source_job, sid FROM members_official ORDER BY source_job, sid")
    assert members == [(first, epoch1, 1), (first, epoch1, 2), (second, epoch2, 1), (second, epoch2, 2)]
    carried = query(conn, "SELECT nsources, sumflux, sumflux2 FROM objects_official"
                          " WHERE ra0 = 100.0 AND dec0 = 0.0")
    assert carried == [(2, 2200.0, 1000.0 ** 2 + 1200.0 ** 2)]
    assert query(conn, "SELECT count(*) FROM objects_official")[0][0] == 3

    epoch3 = source_set(conn, run, tmp_path, 3, [source_row(3, 1, 102.0, 0.0)])
    third = associate_job([epoch3], predecessor=second, excludes=[epoch1])
    assert object_binding(conn)[1] == third
    assert query(conn, "SELECT nsources, sumflux FROM objects_official"
                       " WHERE ra0 = 100.0 AND dec0 = 0.0") == [(1, 1200.0)]
    assert query(conn, "SELECT nsources FROM objects_official WHERE ra0 = 100.1") == [(0,)]
    assert query(conn, "SELECT count(*) FROM members_official WHERE source_job = %s",
                 (epoch1,)) == [(0,)]
    assert query(conn, "SELECT count(*) FROM memberships WHERE source_job = %s",
                 (epoch1,)) == [(2,)]
