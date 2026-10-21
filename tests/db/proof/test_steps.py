"""The procedure proof, milestone 0 (foundation plan v5, Part 3, steps 2, 4, 6, 8, 10, 11, 12, 13).

One test per step, each asserting the step's observation as stated, read
back from the database through the official views and the ``packets``
table. Steps 1, 3, 5, 7 and 9 need the real stages and are milestone 3.
Run by foundation.yml against database/schema.sql; db-migrations.yml
ignores this directory.
"""

from __future__ import annotations

import importlib
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from rapidpipe.foundation import Refused, apply, change, job_show, publish, undo
from tests.db.foundation_helpers import wait_until_blocked
from tests.db.proof.fixture import (
    DATASET, EPOCHS, FIELD_KEY, NSOURCES, SCAS, SURVEY, World, image_key,
)


@pytest.fixture()
def world(connect, tmp_path):
    return World(connect(), tmp_path)


def _official_jobs(world, view):
    return {r[0] for r in world.rows(f"SELECT DISTINCT job FROM {view}")}


def _official_table(world):
    return world.rows("SELECT * FROM official ORDER BY id")


def _two_recipes(world):
    """Run 1 live through both epochs; run 2, not live, over the same imports."""
    first = world.run("first recipe", live=True)
    sets = world.import_epochs(first)
    made_first = world.recipe(first, sets)
    second = world.run("second recipe", {"recipe": "second", "radius": 2.0})
    made_second = world.recipe(second, sets)
    return sets, first, made_first, second, made_second


def test_step_2_two_candidates_from_one_predecessor(world):
    run = world.run("recipe", live=True)
    sets = world.import_epochs(run)
    predecessor = world.recipe(run, sets, epochs=(1,))["objects"][1]
    expected = world.binding("objects", FIELD_KEY)
    epoch2 = [sets[(2, s)] for s in SCAS]

    first, second = (world.associate(run, 2, epoch2, predecessor, published=False)
                     for _ in range(2))
    for job in (first, second):
        publish(world.conn, job, 1, world.prefix(job, 1))

    assert _official_jobs(world, "objects_official") == {first}
    assert world.rows("SELECT count(*) FROM objects WHERE job = %s", (second,))[0][0] > 0
    shown = job_show(world.conn, second)
    assert (shown["state"], shown["refusal"]) == (
        "published", f"binding {expected} is no longer open")
    assert {"kind": "input", "product": "objects", "key": FIELD_KEY,
            "source_job": predecessor, "binding": None} in shown["inputs"]
    assert {"kind": "expects", "product": "objects", "key": FIELD_KEY,
            "source_job": None, "binding": expected} in shown["inputs"]
    assert shown["bindings"] == []


def test_step_4_cutover_is_one_group(world):
    sets, first, made_first, second, made_second = _two_recipes(world)
    assert _official_jobs(world, "packets_official") == set(made_first["packets"].values())

    group = change(world.conn, world.cutover(second, made_second), "proof", "cutover")

    opened = world.rows("SELECT product, job, run FROM official WHERE opened_group = %s", (group,))
    closed = world.rows("SELECT product, job, run FROM official WHERE closed_group = %s", (group,))
    assert len(opened) == len(closed) == 1 + len(made_second["packets"]) + 1
    assert world.live()[1] == second
    assert _official_jobs(world, "packets_official") == set(made_second["packets"].values())
    assert _official_jobs(world, "objects_official") == {made_second["objects"][2]}
    for packet_job, object_job, source_job in world.rows(
            "SELECT DISTINCT job, object_job, source_job FROM packets_official"):
        inputs = {(i["product"], i["source_job"])
                  for i in job_show(world.conn, packet_job)["inputs"] if i["kind"] == "input"}
        assert inputs == {("objects", object_job), ("sources", source_job)}
        assert object_job in made_second["objects"].values()


def test_step_6_undo_restores_the_first_recipe(world):
    sets, first, made_first, second, made_second = _two_recipes(world)
    cutover = change(world.conn, world.cutover(second, made_second), "proof", "cutover")
    replaced = {r[0] for r in world.rows(
        "SELECT id FROM official WHERE closed_group = %s", (cutover,))}

    undo(world.conn, cutover, "proof", "undo the cutover")

    assert world.live()[1] == first
    assert _official_jobs(world, "packets_official") == set(made_first["packets"].values())
    assert _official_jobs(world, "objects_official") == {made_first["objects"][2]}
    open_ids = {r[0] for r in world.rows("SELECT id FROM official WHERE closed_at IS NULL")}
    assert not replaced & open_ids
    second_jobs = list(made_second["packets"].values())
    assert world.rows("SELECT count(*) FROM packets WHERE job = ANY(%s)", (second_jobs,))[0][0] \
        == NSOURCES * len(second_jobs)


class Killed(BaseException):
    """The execution was killed."""


def test_step_8_an_interrupted_publication(world, monkeypatch):
    run = world.run("recipe", live=True)
    sets = world.import_epochs(run)
    object_set = world.recipe(run, sets, epochs=(1,))["objects"][1]
    job = world.packets(run, 2, 1, sets[(2, 1)], object_set, published=False)
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
    sets = world.import_epochs(run)
    world.recipe(run, sets, epochs=(1,))
    contested = world.binding("objects", FIELD_KEY)
    bystander = world.binding("diffimages", image_key(1, 1))
    assert bystander < contested
    campaign = world.run("campaign")
    one, other = (world.associate(campaign, 1, [sets[(1, s)] for s in SCAS], None)
                  for _ in range(2))
    a, b = world.conn, connect()

    with a.cursor() as cur:
        apply(cur, [{"close": contested,
                     "open": {"product": "objects", "key": FIELD_KEY, "job": one}}], "a", "first")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(change, b, [
            {"close": bystander},
            {"close": contested, "open": {"product": "objects", "key": FIELD_KEY, "job": other}},
        ], "b", "second")
        wait_until_blocked(connect(), b.get_backend_pid())
        a.commit()
        with pytest.raises(Refused) as refused:
            pending.result(timeout=10)

    assert refused.value.binding == contested
    assert f"binding {contested}" in str(refused.value)
    assert _official_jobs(world, "objects_official") == {one}
    assert world.binding("diffimages", image_key(1, 1)) == bystander
    assert world.rows("SELECT count(*) FROM official WHERE job = %s OR closed_by = 'b'"
                      " OR opened_by = 'b'", (other,))[0][0] == 0


def test_step_11_publication_racing_a_cutover(world, connect):
    sets, first, made_first, second, made_second = _two_recipes(world)
    key = image_key(2, 1)
    expected = world.binding("packets", key)
    late = world.packets(first, 2, 1, sets[(2, 1)], made_first["objects"][2], published=False)
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
    first = world.run("first recipe", live=True)
    sets = world.import_epochs(first)
    made_first = world.recipe(first, sets, epochs=(1,))
    second = world.run("second recipe", {"recipe": "second"})
    made_second = world.recipe(second, sets, epochs=(1,))
    cutover = change(world.conn, world.cutover(second, made_second), "proof", "cutover")
    moved = world.binding("objects", FIELD_KEY)
    progress = world.recipe(second, sets, epochs=(2,), predecessor=made_second["objects"][1])
    assert _official_jobs(world, "objects_official") == {progress["objects"][2]}
    before = _official_table(world)

    with pytest.raises(Refused) as refused:
        undo(world.conn, cutover, "proof", "undo the cutover")

    assert refused.value.binding == moved
    assert _official_table(world) == before

    still = [{"close": world.binding("packets", image_key(1, s)),
              "open": {"product": "packets", "key": image_key(1, s),
                       "job": made_first["packets"][(1, s)]}} for s in SCAS]
    still.append({"close": world.live()[0],
                  "open": {"survey": SURVEY, "dataset": DATASET, "run": first}})
    change(world.conn, still, "proof", "restore the keys still as expected")

    assert {(e, j) for e, j in world.rows("SELECT DISTINCT expid, job FROM packets_official")} == \
        {(1, made_first["packets"][(1, s)]) for s in SCAS} | \
        {(2, progress["packets"][(2, s)]) for s in SCAS}
    assert _official_jobs(world, "objects_official") == {progress["objects"][2]}
    assert world.live()[1] == first


def test_step_13_a_superseded_source_set(world):
    run = world.run("recipe", live=True)
    sets = world.import_epochs(run)
    set_1 = world.recipe(run, sets, epochs=(1,))["objects"][1]
    superseded = sets[(1, 1)]
    replacement = world.import_image(run, 1, 1, flux_scale=2.0)
    assert _official_jobs(world, "sources_official") >= {replacement}
    assert superseded not in _official_jobs(world, "sources_official")

    set_2 = world.associate(run, 2, [sets[(2, s)] for s in SCAS] + [replacement], set_1,
                            excludes=[superseded])

    assert _official_jobs(world, "objects_official") == {set_2}
    members = world.rows("SELECT DISTINCT source_job FROM members_official")
    assert superseded not in {r[0] for r in members}
    sums = world.rows("""
        SELECT o.aid, o.nsources, o.sumflux, count(s.sid), coalesce(sum(s.fluxfit), 0)
        FROM objects_official o
        LEFT JOIN members_official m ON m.set_job = o.job AND m.aid = o.aid
        LEFT JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
        GROUP BY o.aid, o.nsources, o.sumflux""")
    assert len(sums) == NSOURCES * len(SCAS)
    for _, nsources, sumflux, members_count, members_flux in sums:
        assert nsources == members_count == len(EPOCHS)
        assert sumflux == pytest.approx(members_flux)
    assert world.rows("SELECT count(*) FROM sources WHERE job = %s", (superseded,))[0][0] \
        == NSOURCES
    assert world.rows("SELECT count(*) FROM memberships WHERE job = %s AND source_job = %s",
                      (set_1, superseded))[0][0] == NSOURCES
