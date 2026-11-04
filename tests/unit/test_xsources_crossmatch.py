"""rapidpipe.science.crossmatch.xsources: dev's crossMatchXSources on the in-memory matcher."""

from __future__ import annotations

import math

import pytest

from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.science.crossmatch.xsources import accepted, xassociate_epoch

R = 0.00001528                          # degrees, dev's match_radius
JOB = 50


def xsource(xsid, ra, dec, fluxap=10.0, flags=0, isdiffpos=True, expid=1, mjdobs=61348.2, field=7):
    return {"xsid": xsid, "expid": expid, "mjdobs": mjdobs, "field": field, "ra": ra, "dec": dec,
            "fluxap": fluxap, "flags": flags, "isdiffpos": isdiffpos}


def test_only_xsources_with_the_flags_and_a_finite_position_and_flux_are_matched():
    rows = [xsource(1, 10.0, 5.0), xsource(2, 10.1, 5.0, flags=2), xsource(3, 10.2, 5.0, fluxap=math.nan),
            xsource(4, math.nan, 5.0), xsource(5, 10.3, 5.0, isdiffpos=False)]
    assert [r["xsid"] for r in accepted(rows, 0)] == [1, 5]
    assert [r["xsid"] for r in accepted(rows, 2)] == [2]


def test_each_unmatched_xsource_founds_in_its_field_with_its_fluxap_and_both_signs_match_together():
    """A positive detection founds; a negative one at that star in a later exposure joins the same
    xobject, as dev does not partition on isdiffpos (an xobject founded by an exposure is visible
    to the next exposure, not to the rest of its own)."""
    rows = [xsource(1, 10.0, 5.0, fluxap=40.0, field=7),
            xsource(2, 10.0 + 0.3 * R, 5.0, fluxap=-12.0, isdiffpos=False, field=7, expid=2,
                    mjdobs=61349.2),
            xsource(3, 30.0, 5.0, fluxap=5.0, field=8)]
    epoch = xassociate_epoch({}, rows, job=JOB, radius=R)
    assert epoch.founded == [
        {"xaid": new_object_id(10.0, 5.0), "field": 7, "ra0": 10.0, "dec0": 5.0, "flux0": 40.0},
        {"xaid": new_object_id(30.0, 5.0), "field": 8, "ra0": 30.0, "dec0": 5.0, "flux0": 5.0}]
    assert sorted((m["xaid"], m["xsid"], m["flux"]) for m in epoch.memberships) == sorted([
        (new_object_id(10.0, 5.0), 1, 40.0), (new_object_id(10.0, 5.0), 2, -12.0),
        (new_object_id(30.0, 5.0), 3, 5.0)])
    assert {(m["object_job"], m["source_job"]) for m in epoch.memberships} == {(JOB, JOB)}
    (second,) = [m for m in epoch.memberships if m["xsid"] == 2]
    assert second["dra"] == pytest.approx(0.3 * R) and second["ddec"] == 0.0


def test_an_xsource_within_the_radius_of_an_xobject_read_joins_it_and_founds_nothing():
    read = {777: (44, 10.0, 5.0)}
    epoch = xassociate_epoch(read, [xsource(1, 10.0, 5.0 + 0.5 * R, fluxap=3.0)], job=JOB, radius=R)
    assert epoch.founded == []
    assert epoch.memberships == [{"object_job": 44, "xaid": 777, "source_job": JOB, "xsid": 1,
                                  "dra": 0.0, "ddec": pytest.approx(0.5 * R), "flux": 3.0}]
    assert epoch.counts["objects_read"] == 1


def test_ra_offsets_wrap_across_zero():
    """An xsource just below RA 360 joining an xobject just above 0: the offset is small, not ~360."""
    epoch = xassociate_epoch({9: (44, 0.000001, 0.0)}, [xsource(1, 359.999999, 0.0)], job=JOB,
                             radius=R)
    (m,) = epoch.memberships
    assert m["xaid"] == 9 and m["dra"] == pytest.approx(-0.000002)
