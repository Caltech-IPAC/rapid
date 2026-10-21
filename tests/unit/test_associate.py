"""The associate stage's science: the in-memory matcher and the sufficient sums.

Contracts, on synthetic catalogs: a source joins every object within the
radius (many-to-many); an unmatched source founds an object whose id is
its position hash, and coincident founders share it; an object founded by
one exposure is matched by the next and not by its own; a predecessor's
object keeps its id; the neighbour pass adds memberships and no objects,
inside the cone only; the boundary is q3c's strict inequality; the sums
updated member by member give `lightcurve`'s mean and standard deviation,
and removing an excluded member gives the statistics without it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.science.crossmatch.memory import Catalog, Source, associate, within
from rapidpipe.science.statistics.lightcurve import object_statistics
from rapidpipe.science.statistics.sums import Sums

R = 1.528e-05  # degrees, dev's match radius
ARCSEC = 1 / 3600


def src(sid, ra, dec, expid=1, mjd=60000.0, field=1, job=10):
    return Source(source_job=job, sid=sid, expid=expid, mjdobs=mjd, field=field, ra=ra, dec=dec)


def test_a_source_joins_every_object_within_the_radius():
    catalog = Catalog(R, {1: (100.0, 0.0), 2: (100.0 + 0.8 * R, 0.0), 3: (100.0 + 3 * R, 0.0)})
    result = associate(catalog, [src(7, 100.0 + 0.4 * R, 0.0)], [])
    assert sorted(result.memberships) == [(1, 10, 7), (2, 10, 7)]
    assert result.new_objects == {}


def test_an_unmatched_source_founds_an_object_and_coincident_founders_share_it():
    catalog = Catalog(R)
    result = associate(catalog, [src(1, 50.0, 10.0), src(2, 50.0, 10.0, job=11)], [])
    aid = new_object_id(50.0, 10.0)
    assert result.new_objects == {aid: (50.0, 10.0)}
    assert sorted(result.memberships) == [(aid, 10, 1), (aid, 11, 2)]


def test_objects_founded_by_an_exposure_are_seen_by_the_next_not_by_their_own():
    near = 50.0 + 0.5 * R
    same_exposure = associate(Catalog(R), [src(1, 50.0, 10.0), src(2, near, 10.0)], [])
    assert len(same_exposure.new_objects) == 2
    later = associate(Catalog(R), [src(1, 50.0, 10.0, expid=1, mjd=1.0),
                                   src(2, near, 10.0, expid=2, mjd=2.0)], [])
    aid = new_object_id(50.0, 10.0)
    assert later.new_objects == {aid: (50.0, 10.0)}
    assert sorted(later.memberships) == [(aid, 10, 1), (aid, 10, 2)]


def test_exposures_are_matched_in_ascending_mjd_not_in_expid_order():
    near = 50.0 + 0.5 * R
    result = associate(Catalog(R), [src(1, 50.0, 10.0, expid=9, mjd=2.0),
                                    src(2, near, 10.0, expid=3, mjd=1.0)], [])
    assert list(result.new_objects) == [new_object_id(near, 10.0)]


def test_a_predecessor_object_keeps_its_id():
    catalog = Catalog(R, {42: (200.0, -30.0)})
    result = associate(catalog, [src(5, 200.0, -30.0 + 0.5 * R)], [])
    assert result.memberships == [(42, 10, 5)] and result.new_objects == {}


def test_the_neighbour_pass_adds_memberships_only_and_inside_the_cone():
    catalog = Catalog(R, {1: (10.0, 0.0)})
    inside = src(1, 10.0, 0.5 * R, field=2)
    lonely = src(2, 11.0, 0.0, field=2)
    result = associate(catalog, [], [inside, lonely], cone=(10.0, 0.0, 1.0))
    assert result.memberships == [(1, 10, 1)] and result.new_objects == {}
    assert result.pass2_memberships == 1
    assert associate(Catalog(R, {1: (10.0, 0.0)}), [], [inside], cone=(20.0, 0.0, 1.0)).memberships == []


def test_within_is_strict_at_the_radius():
    assert within(0.0, 0.0, 0.0, 0.999 * R, R)
    assert not within(0.0, 0.0, 0.0, 1.001 * R, R)


def test_the_grid_finds_matches_across_ra_zero_and_near_the_pole():
    catalog = Catalog(R, {1: (359.9999999, 0.0), 2: (123.0, 89.9999999)})
    assert catalog.matches(0.0000001, 0.0) == [1]
    assert catalog.matches(303.0, 89.9999999) == [2]


def members(rng, n, ra=267.96, dec=-28.73):
    return (ra + rng.normal(0, 3e-6, n), dec + rng.normal(0, 3e-6, n),
            rng.normal(1000.0, 50.0, n))


def test_the_sums_give_lightcurves_statistics():
    rng = np.random.default_rng(1)
    ras, decs, fluxes = members(rng, 7)
    sums = Sums(ras[0], decs[0])
    for ra, dec, flux in zip(ras, decs, fluxes):
        sums.add(ra, dec, flux)
    got, want = sums.statistics(), object_statistics(1, ras, decs, fluxes)
    assert sums.nsources == want.nsources
    assert got["meanra"] == pytest.approx(want.meanra, abs=1e-12)
    assert got["meandec"] == pytest.approx(want.meandec, abs=1e-12)
    assert got["stdevdec"] == pytest.approx(want.stdevdec, rel=1e-9)
    assert got["stdevra"] == pytest.approx(want.stdevra, rel=1e-6)
    assert got["meanflux"] == pytest.approx(want.meanflux, rel=1e-12)
    assert got["stdevflux"] == pytest.approx(want.stdevflux, rel=1e-9)


def test_subtracting_an_excluded_member_gives_the_statistics_without_it():
    rng = np.random.default_rng(2)
    ras, decs, fluxes = members(rng, 5)
    sums = Sums(ras[0], decs[0])
    for ra, dec, flux in zip(ras, decs, fluxes):
        sums.add(ra, dec, flux)
    sums.add(ras[2], decs[2], fluxes[2], sign=-1)
    keep = [0, 1, 3, 4]
    want = object_statistics(1, ras[keep], decs[keep], fluxes[keep])
    got = sums.statistics()
    assert sums.nsources == 4
    assert got["meanflux"] == pytest.approx(want.meanflux, rel=1e-12)
    assert got["stdevflux"] == pytest.approx(want.stdevflux, rel=1e-9)
    assert got["meandec"] == pytest.approx(want.meandec, abs=1e-12)


def test_an_object_with_no_members_left_has_undefined_statistics():
    sums = Sums(1.0, 2.0)
    sums.add(1.0, 2.0, 5.0)
    sums.add(1.0, 2.0, 5.0, sign=-1)
    assert sums.nsources == 0 and math.isnan(sums.statistics()["meanra"])
