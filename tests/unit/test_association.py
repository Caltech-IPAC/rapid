"""Association in the difference job and reconcile at publish, without a database.

Contracts (the plan's amendment of 2026-10-24): an epoch's accepted sources
match every object of the catalog as read within the radius, each
membership carrying its source's offsets from that object's founding
position and its flux; a field source that matches none founds an object
whose job is the difference job; a neighbouring field's source only joins.
At publish, a founded object within the radius of one founded since the
read is not inserted, its memberships go to that object rebased to its
origin, and the rebased sums equal a direct aggregation of the member
coordinates about the surviving origin; a founded object near nothing is
kept, and so are memberships onto objects already read.
"""

from __future__ import annotations

import math

import pytest

from rapidpipe.foundation.catalog import attach
from rapidpipe.products.spatial import ra_offset
from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.science.crossmatch.memory import associate_epoch

R = 1.0 / 3600            # one arcsecond, degrees
JOB = 50


def src(sid, ra, dec, field=7, flux=100.0):
    return {"sid": sid, "expid": 1, "mjdobs": 60000.0, "field": field, "ra": ra, "dec": dec,
            "fluxfit": flux}


def test_sources_join_every_object_within_the_radius_and_unmatched_ones_found():
    objects = {11: (40, 268.0, -29.0), 12: (41, 268.0 + 0.5 * R, -29.0)}
    sources = [src(1, 268.0 + 0.25 * R, -29.0, flux=10.0),       # within both
               src(2, 268.1, -29.0, flux=20.0),                  # within none: founds
               src(3, 268.0, -29.0 + 0.2 * R, field=8)]          # a neighbour's: joins only
    epoch = associate_epoch(objects, sources, job=JOB, field=7, cone=None, radius=R)

    aid = new_object_id(268.1, -29.0)
    assert epoch.founded == [{"aid": aid, "ra0": 268.1, "dec0": -29.0}]
    by = {(m["aid"], m["sid"]): m for m in epoch.memberships}
    assert set(by) == {(11, 1), (12, 1), (aid, 2), (11, 3), (12, 3)}
    assert by[(11, 1)]["object_job"] == 40 and by[(12, 1)]["object_job"] == 41
    assert by[(aid, 2)]["object_job"] == JOB and (by[(aid, 2)]["dra"], by[(aid, 2)]["ddec"]) == (0.0, 0.0)
    assert by[(12, 1)]["dra"] == pytest.approx(-0.25 * R)
    assert by[(11, 3)]["ddec"] == pytest.approx(0.2 * R) and by[(11, 1)]["flux"] == 10.0
    assert {m["source_job"] for m in epoch.memberships} == {JOB}
    assert epoch.counts["founded"] == 1 and epoch.counts["pass2_memberships"] == 2


def _sums(rows):
    n = len(rows)
    return (n, sum(r["dra"] for r in rows), sum(r["dra"] ** 2 for r in rows),
            sum(r["ddec"] for r in rows), sum(r["ddec"] ** 2 for r in rows))


def test_a_duplicate_founder_attaches_to_the_object_founded_since_the_read_with_rebased_sums():
    """Two epochs in flight saw no object at a star: the other published first, at a point
    0.3 arcseconds off; this job's founded object goes to it, and its members' sums are the
    direct aggregation of their coordinates about the surviving origin."""
    other = (44, 777, 10.0 + 0.3 * R, 5.0 - 0.1 * R)              # (job, aid, ra0, dec0)
    sources = [src(1, 10.0, 5.0), src(2, 10.0 + 0.4 * R, 5.0 + 0.3 * R, field=8)]
    epoch = associate_epoch({}, sources, job=JOB, field=7, cone=None, radius=R)
    (founded,) = epoch.founded
    assert len(epoch.memberships) == 2

    kept, members, counts = attach(JOB, epoch.founded, epoch.memberships, [other], R)

    assert kept == [] and counts == {"objects_between": 1, "reconciled": 1, "attached_memberships": 2}
    assert {(m["object_job"], m["aid"]) for m in members} == {(44, 777)}
    direct = [{"dra": ra_offset(s["ra"], other[2]), "ddec": s["dec"] - other[3]} for s in sources]
    for got, want in zip(_sums(members), _sums(direct)):
        assert got == pytest.approx(want, rel=1e-9, abs=1e-18)
    # The formula, for the record: S' = S + n d, Q' = Q + 2 d S + n d² per axis.
    n, s_ra, q_ra, *_ = _sums(epoch.memberships)
    d = ra_offset(founded["ra0"], other[2])
    assert _sums(members)[1] == pytest.approx(s_ra + n * d)
    assert _sums(members)[2] == pytest.approx(q_ra + 2 * d * s_ra + n * d * d)


def test_a_member_outside_the_surviving_objects_radius_is_dropped_and_far_founders_are_kept():
    other = (44, 777, 10.0 + 0.9 * R, 5.0)
    sources = [src(1, 10.0, 5.0), src(2, 10.0 - 0.6 * R, 5.0, field=8),   # 1.5" from the other
               src(3, 30.0, 5.0)]
    epoch = associate_epoch({}, sources, job=JOB, field=7, cone=None, radius=R)
    kept, members, counts = attach(JOB, epoch.founded, epoch.memberships, [other], R)

    assert [o["aid"] for o in kept] == [new_object_id(30.0, 5.0)]
    assert sorted((m["aid"], m["sid"]) for m in members) == sorted(
        [(777, 1), (new_object_id(30.0, 5.0), 3)])
    assert counts["reconciled"] == 1 and counts["attached_memberships"] == 1


def test_memberships_onto_objects_already_read_are_untouched():
    read = {11: (40, 10.0, 5.0)}
    epoch = associate_epoch(read, [src(1, 10.0, 5.0 + 0.1 * R)], job=JOB, field=7, cone=None, radius=R)
    assert epoch.founded == []
    kept, members, counts = attach(JOB, [], epoch.memberships, [(44, 777, 10.0, 5.0)], R)
    assert members == epoch.memberships and counts["reconciled"] == 0
    assert math.isclose(members[0]["ddec"], 0.1 * R)
