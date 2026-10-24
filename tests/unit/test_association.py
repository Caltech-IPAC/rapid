"""Association in the difference job and reconcile at publish, without a database.

Contracts (the plan's amendment of 2026-10-24 with the field rule, runbook
part 12 step 1): an epoch's accepted sources match every object of the
catalog as read within the radius, each membership carrying its source's
offsets from that object's founding position and its flux; a source that
matches none founds an object in its own field, whichever field the image
is centred on, whose job is the difference job; a source across a field
boundary joins the neighbouring field's object and founds nothing. The
job's manifest names the fields the publisher locks (each field that
gained an object or a membership and its neighbours), and the publisher
takes those locks in ascending field order and reconciles against the
objects founded since the read in all of them. At publish, a founded
object within the radius of one founded since the read is not inserted,
its memberships go to that object rebased to its origin, and the rebased
sums equal a direct aggregation of the member coordinates about the
surviving origin; a founded object near nothing is kept, and so are
memberships onto objects already read. Two jobs founding either side of a
boundary within the radius, interleaved read, read, publish, publish, end
with one object.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import rapidpipe.stages.difference as difference
from rapidpipe.foundation.catalog import attach, between, fields, lock
from rapidpipe.products.spatial import ra_offset, tessellation_field
from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.science.crossmatch.memory import associate_epoch
from rapidpipe.science.spatial import field_center, field_neighbours

R = 1.0 / 3600            # one arcsecond, degrees
JOB = 50


def src(sid, ra, dec, field=7, flux=100.0):
    return {"sid": sid, "expid": 1, "mjdobs": 60000.0, "field": field, "ra": ra, "dec": dec,
            "fluxfit": flux}


def test_sources_join_every_object_within_the_radius_and_unmatched_ones_found_in_their_field():
    objects = {11: (40, 268.0, -29.0), 12: (41, 268.0 + 0.5 * R, -29.0)}
    sources = [src(1, 268.0 + 0.25 * R, -29.0, flux=10.0),       # within both
               src(2, 268.1, -29.0, flux=20.0),                  # within none: founds in 7
               src(3, 268.2, -29.0, field=8)]                    # within none: founds in 8
    epoch = associate_epoch(objects, sources, job=JOB, radius=R)

    a2, a3 = new_object_id(268.1, -29.0), new_object_id(268.2, -29.0)
    assert epoch.founded == sorted([{"aid": a2, "field": 7, "ra0": 268.1, "dec0": -29.0},
                                    {"aid": a3, "field": 8, "ra0": 268.2, "dec0": -29.0}],
                                   key=lambda o: o["aid"])
    by = {(m["aid"], m["sid"]): m for m in epoch.memberships}
    assert set(by) == {(11, 1), (12, 1), (a2, 2), (a3, 3)}
    assert by[(11, 1)]["object_job"] == 40 and by[(12, 1)]["object_job"] == 41
    assert by[(a2, 2)]["object_job"] == JOB and (by[(a2, 2)]["dra"], by[(a2, 2)]["ddec"]) == (0.0, 0.0)
    assert by[(12, 1)]["dra"] == pytest.approx(-0.25 * R) and by[(11, 1)]["flux"] == 10.0
    assert {m["source_job"] for m in epoch.memberships} == {JOB}
    assert epoch.counts["founded"] == 2 and epoch.counts["memberships"] == 4


def test_a_source_across_the_boundary_joins_the_neighbour_fields_object_and_founds_nothing():
    objects = {11: (40, 10.0, 5.0)}                               # founded in field 7
    epoch = associate_epoch(objects, [src(1, 10.0 + 0.3 * R, 5.0, field=8)], job=JOB, radius=R)
    assert epoch.founded == []
    assert [(m["object_job"], m["aid"], m["sid"]) for m in epoch.memberships] == [(40, 11, 1)]


class Recorder:
    def __init__(self, rows=()):
        self.calls, self.rows = [], list(rows)

    def execute(self, statement, params=()):
        self.calls.append((statement, params))

    def fetchall(self):
        return self.rows


def test_the_publisher_locks_each_field_once_in_ascending_order_in_one_statement():
    cur = Recorder()
    lock(cur, 3, [9, 4, 7, 4])
    ((statement, params),) = cur.calls
    assert statement.count("pg_advisory_xact_lock") == 3
    assert params == [3, 4, 3, 7, 3, 9]


def test_the_reconcile_reads_every_locked_field_and_an_old_manifest_its_one_field():
    cur = Recorder()
    between(cur, JOB, 3, {"dataset": "D", "survey": "S", "fields": [8, 7], "radius": R}, [40, 41])
    ((statement, params),) = cur.calls
    assert "o.field = ANY(%s)" in statement and params == (3, "D", "S", [7, 8], JOB, [40, 41])
    assert fields({"dataset": "D", "survey": "S", "field": 7, "radius": R}) == [7]


def _sums(rows):
    n = len(rows)
    return (n, sum(r["dra"] for r in rows), sum(r["dra"] ** 2 for r in rows),
            sum(r["ddec"] for r in rows), sum(r["ddec"] ** 2 for r in rows))


def test_a_duplicate_founder_attaches_to_the_object_founded_since_the_read_with_rebased_sums():
    """Two epochs in flight saw no object at a star: the other published first, at a point
    0.3 arcseconds off; this job's founded object goes to it, and its members' sums are the
    direct aggregation of their coordinates about the surviving origin."""
    other = (44, 777, 10.0 + 0.3 * R, 5.0 - 0.1 * R)              # (job, aid, ra0, dec0)
    sources = [src(1, 10.0, 5.0),                                  # a later exposure joins it
               {**src(2, 10.0 + 0.4 * R, 5.0 + 0.3 * R), "expid": 2, "mjdobs": 60000.1}]
    epoch = associate_epoch({}, sources, job=JOB, radius=R)
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
    sources = [src(1, 10.0, 5.0), src(3, 30.0, 5.0),
               {**src(2, 10.0 - 0.6 * R, 5.0), "expid": 2, "mjdobs": 60000.1}]  # 1.5" from the other
    epoch = associate_epoch({}, sources, job=JOB, radius=R)
    kept, members, counts = attach(JOB, epoch.founded, epoch.memberships, [other], R)

    assert [o["aid"] for o in kept] == [new_object_id(30.0, 5.0)]
    assert sorted((m["aid"], m["sid"]) for m in members) == sorted(
        [(777, 1), (new_object_id(30.0, 5.0), 3)])
    assert counts["reconciled"] == 1 and counts["attached_memberships"] == 1


def test_memberships_onto_objects_already_read_are_untouched():
    read = {11: (40, 10.0, 5.0)}
    epoch = associate_epoch(read, [src(1, 10.0, 5.0 + 0.1 * R)], job=JOB, radius=R)
    assert epoch.founded == []
    kept, members, counts = attach(JOB, [], epoch.memberships, [(44, 777, 10.0, 5.0)], R)
    assert members == epoch.memberships and counts["reconciled"] == 0
    assert math.isclose(members[0]["ddec"], 0.1 * R)


def _boundary(a: int, b: int) -> tuple[float, float, float, float]:
    """A point on the boundary between neighbouring fields ``a`` and ``b``, and the unit step from a to b."""
    (ra_a, dec_a), (ra_b, dec_b) = field_center(a), field_center(b)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tessellation_field(ra_a + mid * (ra_b - ra_a), dec_a + mid * (dec_b - dec_a)) == a:
            lo = mid
        else:
            hi = mid
    dra, ddec = ra_b - ra_a, dec_b - dec_a
    norm = math.hypot(dra * math.cos(math.radians(dec_a)), ddec)
    return ra_a + lo * dra, dec_a + lo * ddec, dra / norm, ddec / norm


def _job(job: int, tmp_path, catalog_rows: list[tuple]):
    """A difference job's context for :func:`difference.associate`, reading ``catalog_rows``."""
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            self.params = params

        def fetchall(self):
            wanted = set(self.params[3])
            return [r for r in catalog_rows if r[2] in wanted]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    path = tmp_path / f"job-{job}"
    path.mkdir()
    return SimpleNamespace(job=job, run=3, dataset="D", survey="S", path=path,
                           settings={"associate": {"match_radius": R, "source_flags": 0,
                                                   "min_snr": 5.0}},
                           connect=lambda: Connection())


def _sources(sid, ra, dec):
    return pa.Table.from_pylist([{"sid": sid, "expid": 1, "mjdobs": 60000.0,
                                  "field": tessellation_field(ra, dec), "ra": ra, "dec": dec,
                                  "fluxfit": 100.0, "fluxerr": 1.0, "flags": 0}])


def test_two_jobs_founding_either_side_of_a_boundary_share_a_lock_and_end_with_one_object(tmp_path):
    """Astra's case: jobs A and B each see an empty catalog and found one object, 0.4 arcseconds
    apart on opposite sides of a field boundary; A publishes, then B. B's locked fields include
    A's object's field, so they serialise, and B's reconcile reads A's object and attaches."""
    field_a = 4662268
    field_b = field_neighbours(field_a)[0]
    ra, dec, ura, udec = _boundary(field_a, field_b)
    pa_, pb = (ra - 0.2 * R * ura, dec - 0.2 * R * udec), (ra + 0.2 * R * ura, dec + 0.2 * R * udec)
    assert tessellation_field(*pa_) == field_a and tessellation_field(*pb) == field_b

    table: list[tuple] = []                                    # (job, aid, field, ra0, dec0)
    jobs = {}
    for name, job, (r, d) in (("A", 61, pa_), ("B", 62, pb)):  # both read before either publishes
        context = _job(job, tmp_path, list(table))
        reads, catalog, counts = difference.associate(context, _sources(1, r, d), field_a)
        assert counts["founded"] == 1 and reads == []
        jobs[name] = (job, catalog, context.path)
    assert field_a in jobs["B"][1]["fields"] and field_b in jobs["A"][1]["fields"]
    assert set(jobs["A"][1]["fields"]) & set(jobs["B"][1]["fields"])

    for name in ("A", "B"):                                    # publish in turn, as the locks order
        job, catalog, path = jobs[name]
        founded = pq.read_table(path / "objects.parquet").to_pylist()
        memberships = pq.read_table(path / "memberships.parquet").to_pylist()
        others = [(j, aid, ra0, dec0) for j, aid, field, ra0, dec0 in table
                  if field in catalog["fields"] and j != job]
        kept, members, _ = attach(job, founded, memberships, others, R)
        table += [(job, o["aid"], o["field"], o["ra0"], o["dec0"]) for o in kept]
        jobs[name] = members

    assert len(table) == 1 and table[0][0] == 61
    assert {(m["object_job"], m["aid"]) for m in jobs["B"]} == {(61, table[0][1])}
