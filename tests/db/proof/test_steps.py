"""The procedure proof, milestone 0 (foundation plan, Part 3, steps 1, 2, 4, 6, 8, 10, 11, 12, 13).

One test per step, each asserting the step's observation as stated, read
back from the database through the official views and the ``packets``
table. Steps 1, 2 and 13 are the amendment of 2026-10-24's: association
in the difference job over an append-only per-field catalog, two epochs of
one field in flight at once. Steps 3, 5, 7 and 9 need the real stages and
are milestone 3. Run by foundation.yml against database/schema.sql.
"""

from __future__ import annotations

import importlib
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from rapidpipe.foundation import Refused, apply, change, job_show, publish, undo
from rapidpipe.products.spatial import within
from tests.db.foundation_helpers import wait_until_blocked
from tests.db.proof.fixture import (
    DATASET, FIELD_KEY, NSOURCES, RADIUS, SCAS, SURVEY, World, image_key,
)


@pytest.fixture()
def world(connect, tmp_path):
    return World(connect(), tmp_path)


def _official_jobs(world, view):
    return {r[0] for r in world.rows(f"SELECT DISTINCT job FROM {view}")}


def _official_table(world):
    return world.rows("SELECT * FROM official ORDER BY id")


def _two_recipes(world):
    """Run 1 live through both epochs; run 2, not live, its own differences of the same epochs."""
    first = world.run("first recipe", live=True)
    made_first = world.recipe(first)
    second = world.run("second recipe", {"recipe": "second"})
    made_second = world.recipe(second)
    return first, made_first, second, made_second


def _in_flight(world, run, jitter=0.0):
    """Epochs 1 and 2 of SCA 1 differenced at once: both read the empty catalog, 1 publishes first."""
    read = world.catalog(run)
    one = world.difference(run, 1, 1, catalog=read, published=False)
    two = world.difference(run, 2, 1, catalog=read, jitter=jitter, published=False)
    return one, two, publish(world.conn, one, 1, world.prefix(one, 1)), \
        publish(world.conn, two, 1, world.prefix(two, 1))


def _object_stats(world):
    return {(job, aid): (n, sumra, sumdec, sumflux) for job, aid, n, sumra, sumdec, sumflux in world.rows(
        "SELECT job, aid, nsources, sumra, sumdec, sumflux FROM objects_official")}


def test_step_1_two_epochs_of_a_field_in_flight_both_publish_and_bind_their_own_sources(world):
    run = world.run("recipe", live=True)

    one, two, first, second = _in_flight(world, run)

    assert first["group"] is not None and second["group"] is not None
    assert first["refusal"] is None and second["refusal"] is None
    for epoch, job in ((1, one), (2, two)):
        for product in ("diffimages", "sources"):
            assert world.rows("SELECT job FROM official WHERE id = %s",
                              (world.binding(product, image_key(epoch, 1)),)) == [(job,)]
    assert _official_jobs(world, "memberships_official") == {one, two}
    # Every official source is a member, within the radius of its object's position.
    assert world.rows("""
        SELECT count(*) FROM sources_official s WHERE NOT EXISTS (
            SELECT 1 FROM memberships_official m WHERE m.source_job = s.job AND m.sid = s.sid)""") == [(0,)]
    for ra, dec, ra0, dec0 in world.rows(
            "SELECT s.ra, s.dec, o.ra0, o.dec0 FROM memberships_official m"
            " JOIN sources s ON s.job = m.source_job AND s.sid = m.sid"
            " JOIN objects o ON o.job = m.object_job AND o.aid = m.aid"):
        assert within(ra, dec, ra0, dec0, RADIUS)
    # The second job's recorded read is the catalog it saw: empty.
    assert [i for i in job_show(world.conn, two)["inputs"] if i["product"] == "objects"] == []


def test_step_2_the_seconds_founded_objects_attach_where_they_duplicate(world):
    """Epoch 2's stars sit 0.1 arcseconds off epoch 1's; both founded every one; the second to
    publish founds none, its memberships attached with sums rebased to the surviving origins."""
    run = world.run("recipe", live=True)

    one, two, _, second = _in_flight(world, run, jitter=0.1)

    assert world.rows("SELECT count(*) FROM objects WHERE job = %s", (one,)) == [(NSOURCES,)]
    assert world.rows("SELECT count(*) FROM objects WHERE job = %s", (two,)) == [(0,)]
    assert job_show(world.conn, two)["manifest"]["reconcile"] == {
        "objects_between": NSOURCES, "reconciled": NSOURCES, "attached_memberships": NSOURCES}
    # No two objects of different founding jobs within the radius of each other.
    objects = world.rows("SELECT job, ra0, dec0 FROM objects")
    assert not [(a, b) for a in objects for b in objects
                if a[0] < b[0] and within(a[1], a[2], b[1], b[2], RADIUS)]
    # The view's sums are the direct aggregation of the members' coordinates about the origin.
    direct = world.rows("""
        SELECT o.job, o.aid, count(*), sum(s.ra - o.ra0), sum(s.dec - o.dec0), sum(s.fluxfit)
        FROM memberships_official m JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
        JOIN objects o ON o.job = m.object_job AND o.aid = m.aid GROUP BY o.job, o.aid""")
    stats = _object_stats(world)
    assert len(stats) == NSOURCES == len(direct)
    for job, aid, n, sumra, sumdec, sumflux in direct:
        assert stats[(job, aid)][0] == n == 2
        assert stats[(job, aid)][1:] == pytest.approx((sumra, sumdec, sumflux), rel=1e-6, abs=1e-12)
    # The ids are the first publisher's and stay: a third epoch read after both joins them.
    later = world.difference(run, 3, 1)
    assert {r[0] for r in world.rows("SELECT DISTINCT object_job FROM memberships WHERE job = %s",
                                     (later,))} == {one}


def test_step_4_cutover_is_one_group(world):
    first, made_first, second, made_second = _two_recipes(world)
    assert _official_jobs(world, "packets_official") == set(made_first["packets"].values())

    group = change(world.conn, world.cutover(second, made_second), "proof", "cutover")

    opened = world.rows("SELECT product, job, run FROM official WHERE opened_group = %s", (group,))
    closed = world.rows("SELECT product, job, run FROM official WHERE closed_group = %s", (group,))
    assert len(opened) == len(closed) == 2 * len(made_second["differences"]) \
        + len(made_second["packets"]) + 1
    assert world.live()[1] == second
    assert _official_jobs(world, "packets_official") == set(made_second["packets"].values())
    assert _official_jobs(world, "memberships_official") == set(made_second["differences"].values())
    assert {r[0] for r in world.rows("SELECT DISTINCT run FROM objects_official")} == {second}
    for packet_job, object_job, source_job in world.rows(
            "SELECT DISTINCT job, object_job, source_job FROM packets_official"):
        inputs = {(i["product"], i["source_job"])
                  for i in job_show(world.conn, packet_job)["inputs"] if i["kind"] == "input"}
        assert inputs == {("sources", source_job)}
        assert object_job in made_second["differences"].values()


def test_step_6_undo_restores_the_first_recipe(world):
    first, made_first, second, made_second = _two_recipes(world)
    cutover = change(world.conn, world.cutover(second, made_second), "proof", "cutover")
    replaced = {r[0] for r in world.rows(
        "SELECT id FROM official WHERE closed_group = %s", (cutover,))}

    undo(world.conn, cutover, "proof", "undo the cutover")

    assert world.live()[1] == first
    assert _official_jobs(world, "packets_official") == set(made_first["packets"].values())
    assert _official_jobs(world, "memberships_official") == set(made_first["differences"].values())
    open_ids = {r[0] for r in world.rows("SELECT id FROM official WHERE closed_at IS NULL")}
    assert not replaced & open_ids
    second_jobs = list(made_second["packets"].values())
    assert world.rows("SELECT count(*) FROM packets WHERE job = ANY(%s)", (second_jobs,))[0][0] \
        == NSOURCES * len(second_jobs)


class Killed(BaseException):
    """The execution was killed."""


def test_step_8_an_interrupted_publication(world, monkeypatch):
    run = world.run("recipe", live=True)
    source_set = world.difference(run, 2, 1)
    job = world.packets(run, 2, 1, source_set, published=False)
    publisher = importlib.import_module("rapidpipe.foundation.publish")
    loaded_when_killed = []

    def killed(cur, job_id, run_id):
        cur.execute("SELECT count(*) FROM packets WHERE job = %s", (job_id,))
        loaded_when_killed.append(cur.fetchone()[0])
        raise Killed

    def count():
        return world.rows("SELECT (SELECT count(*) FROM packets WHERE job = %(j)s),"
                          " (SELECT count(*) FROM forced WHERE job = %(j)s)", {"j": job})[0]

    with monkeypatch.context() as patch:
        patch.setattr(publisher, "_bind", killed)
        with pytest.raises(Killed):
            publish(world.conn, job, 1, world.prefix(job, 1))
    assert loaded_when_killed == [NSOURCES]
    assert count() == (0, 0)
    assert job_show(world.conn, job)["state"] == "launched"

    shutil.copytree(world.root / f"job-{job}" / "attempt-1", world.root / f"job-{job}" / "attempt-2")
    retry = publish(world.conn, job, 2, world.prefix(job, 2))
    again = publish(world.conn, job, 3, world.prefix(job, 3))

    assert retry["published"] and not again["published"]
    assert count() == (NSOURCES, NSOURCES)
    assert job_show(world.conn, job)["published_attempt"] == 2
    assert world.rows("SELECT job FROM official WHERE id = %s",
                      (world.binding("packets", image_key(2, 1)),)) == [(job,)]


def test_step_10_two_change_groups_at_once(world, connect):
    run = world.run("recipe", live=True)
    world.recipe(run, epochs=(1,))
    key = image_key(1, 1)
    contested = world.binding("sources", key)
    bystander = world.binding("diffimages", key)
    assert bystander < contested
    campaign = world.run("campaign")
    one, other = (world.difference(campaign, 1, 1) for _ in range(2))
    a, b = world.conn, connect()

    with a.cursor() as cur:
        apply(cur, [{"close": contested,
                     "open": {"product": "sources", "key": key, "job": one}}], "a", "first")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(change, b, [
            {"close": bystander},
            {"close": contested, "open": {"product": "sources", "key": key, "job": other}},
        ], "b", "second")
        wait_until_blocked(connect(), b.get_backend_pid())
        a.commit()
        with pytest.raises(Refused) as refused:
            pending.result(timeout=10)

    assert refused.value.binding == contested
    assert f"binding {contested}" in str(refused.value)
    assert one in _official_jobs(world, "memberships_official")
    assert other not in _official_jobs(world, "memberships_official")
    assert world.binding("diffimages", key) == bystander
    assert world.rows("SELECT count(*) FROM official WHERE job = %s OR closed_by = 'b'"
                      " OR opened_by = 'b'", (other,))[0][0] == 0


def test_step_11_publication_racing_a_cutover(world, connect):
    first, made_first, second, made_second = _two_recipes(world)
    key = image_key(2, 1)
    expected = world.binding("packets", key)
    late = world.packets(first, 2, 1, made_first["differences"][(2, 1)], published=False)
    a, b = world.conn, connect()

    with a.cursor() as cur:
        group = apply(cur, world.cutover(second, made_second), "a", "cutover")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(publish, b, late, 1, world.prefix(late, 1))
        wait_until_blocked(connect(), b.get_backend_pid())
        a.commit()
        result = pending.result(timeout=10)

    assert result["published"] and result["group"] is None
    assert result["refusal"] == f"binding {expected} is no longer open"
    assert world.rows("SELECT count(*) FROM packets WHERE job = %s", (late,))[0][0] == NSOURCES
    assert late not in _official_jobs(world, "packets_official")
    packet_bindings = world.rows("SELECT key, job, opened_group FROM official"
                                 " WHERE closed_at IS NULL AND product = 'packets'")
    assert sorted((k["expid"], k["sca"]) for k, _, _ in packet_bindings) == \
        sorted(made_second["packets"])
    assert {(j, g) for _, j, g in packet_bindings} == \
        {(j, group) for j in made_second["packets"].values()}
    assert world.live()[1] == second


def test_step_12_undo_after_progress(world):
    """Progress that moves a binding the cutover opened (epoch 1 of SCA 1 re-differenced under
    the new recipe) refuses the undo; the keys still as expected are restored by a new group."""
    first = world.run("first recipe", live=True)
    made_first = world.recipe(first, epochs=(1,))
    second = world.run("second recipe", {"recipe": "second"})
    made_second = world.recipe(second, epochs=(1,))
    cutover = change(world.conn, world.cutover(second, made_second), "proof", "cutover")
    moved = {world.binding(p, image_key(1, 1)) for p in ("diffimages", "sources")}
    progress = world.difference(second, 1, 1, flux_scale=2.0)
    assert progress in _official_jobs(world, "memberships_official")
    before = _official_table(world)

    with pytest.raises(Refused) as refused:
        undo(world.conn, cutover, "proof", "undo the cutover")

    assert refused.value.binding in moved
    assert _official_table(world) == before

    still = []
    for sca, products in ((1, ("packets",)), (2, ("diffimages", "sources", "packets"))):
        key = image_key(1, sca)
        for product in products:
            job = made_first["packets" if product == "packets" else "differences"][(1, sca)]
            still.append({"close": world.binding(product, key),
                          "open": {"product": product, "key": key, "job": job}})
    still.append({"close": world.live()[0],
                  "open": {"survey": SURVEY, "dataset": DATASET, "run": first}})
    change(world.conn, still, "proof", "restore the keys still as expected")

    assert _official_jobs(world, "packets_official") == set(made_first["packets"].values())
    assert _official_jobs(world, "memberships_official") == {made_first["differences"][(1, 2)], progress}
    assert world.live()[1] == first


def test_step_13_a_superseded_source_set(world):
    """Epoch 1 of SCA 1 re-differenced and rebound: its old memberships leave the view and the
    sums; the replacement's sources join the same objects, whose ids do not change."""
    run = world.run("recipe", live=True)
    made = world.recipe(run)
    superseded = made["differences"][(1, 1)]
    ids_before = {(r[0], r[1]) for r in world.rows("SELECT job, aid FROM objects")}

    replacement = world.difference(run, 1, 1, flux_scale=2.0)

    assert replacement in _official_jobs(world, "sources_official")
    assert superseded not in _official_jobs(world, "sources_official")
    assert superseded not in _official_jobs(world, "memberships_official")
    assert world.rows("SELECT count(*) FROM objects WHERE job = %s", (replacement,)) == [(0,)]
    assert {(r[0], r[1]) for r in world.rows("SELECT job, aid FROM objects")} == ids_before
    sums = world.rows("""
        SELECT o.aid, o.nsources, o.sumflux, count(s.sid), coalesce(sum(s.fluxfit), 0)
        FROM objects_official o
        JOIN memberships_official m ON m.object_job = o.job AND m.aid = o.aid
        JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
        GROUP BY o.aid, o.nsources, o.sumflux""")
    assert len(sums) == NSOURCES * len(SCAS)
    for _, nsources, sumflux, members_count, members_flux in sums:
        assert nsources == members_count == 2
        assert sumflux == pytest.approx(members_flux)
    # The old rows still exist.
    assert world.rows("SELECT count(*) FROM sources WHERE job = %s", (superseded,))[0][0] == NSOURCES
    assert world.rows("SELECT count(*) FROM memberships WHERE job = %s", (superseded,))[0][0] \
        == NSOURCES
