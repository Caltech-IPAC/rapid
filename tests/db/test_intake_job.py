"""An intake job's exposures row published with the file's own program, the surveys seed, and the exposure id.

The intake stage runs on the unit tests' synthetic SOC L2 file (its PSF
and surveys lookups faked, its exposure-id lookup real) with ``[intake]
program_id`` unset, and its attempt is published for real: the official
``exposures`` row carries the file's ``PROGRAM`` and joins its
commissioning ``surveys`` row. The seed holds one negative id per
simulation and the twelve commissioning programs. Two stems that pack to
one old ``expid`` (program 1043's two execution plans) are two exposures
with two ids and two official rows. The intake exits 65 when its scope's
``expid`` is not the id ``exposure_ids`` holds for its stem's name in its
dataset (rapid_docs exposure-id). Run by foundation.yml against
database/schema.sql.
"""

from __future__ import annotations

import pytest

from rapidpipe.exitcodes import CommandExit
from rapidpipe.foundation import change, create_job, create_run, loop, publish
from tests.unit.test_intake import run_intake, write_asdf
from tests.unit.test_intake import source  # noqa: F401  (a fixture)

DATASET = "r00342"
FLIGHT = "r0104901001001002001_0001_wfi08_f146"
#: Program 1043's execution plans 01 and 02 of one pass, observation, visit and exposure: one packed expid.
PLAN_1 = "r0104301001001001001_0001_wfi08_f146"
PLAN_2 = "r0104302001001001001_0001_wfi08_f146"
#: The commissioning programs, category COM.
COMMISSIONING = {1006, 1007, 1008, 1018, 1020, 1043, 1049, 1053, 1054, 1055, 1062, 1083}


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _lookup(conn):
    """The intake's exposure-id read, on the real table."""
    def held(dataset, name):
        rows = _rows(conn, "SELECT expid FROM exposure_ids WHERE dataset = %s AND name = %s", (dataset, name))
        return rows[0][0] if rows else None
    return held


def _live_run(conn) -> int:
    run = create_run(conn, "tester", "test", "abc123", {}, "S", DATASET)
    change(conn, [{"open": {"survey": "S", "dataset": DATASET, "run": run}}], "tester", "live")
    return run


def _publish_intake(conn, run, tmp_path, source, stem):  # noqa: F811
    """One intake of ``stem`` under the id the loop would resolve for it, published with its exposures row."""
    name = loop.stem_ids(stem)["name"]
    expid = loop.exposure_ids(conn, DATASET, [name])[name]
    prefix = run_intake(tmp_path / stem, source, stem, program_id="", surveys=(1049, 1043), expid=expid,
                        exposure_ids=_lookup(conn))
    job = create_job(conn, run, "intake", {"dataset": DATASET, "file": stem, "expid": expid}, expects=[
        {"product": "exposures", "key": {"dataset": DATASET, "expid": expid}, "binding": None}])
    assert publish(conn, job, 1, prefix.as_uri())["refusal"] is None
    return job, expid


def test_the_surveys_seed_holds_one_id_per_simulation_and_the_commissioning_programs(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT program_id FROM surveys WHERE category = 'SIM' ORDER BY 1")
        assert cur.fetchall() == [(-4,), (-3,), (-2,), (-1,)]
        cur.execute("SELECT program_id FROM surveys WHERE category = 'COM'")
        assert {r[0] for r in cur.fetchall()} == COMMISSIONING


def test_an_intake_job_publishes_the_files_own_program(connect, tmp_path, source):  # noqa: F811
    write_asdf(source / f"{FLIGHT}_cal.asdf", FLIGHT)
    conn = connect()
    run = _live_run(conn)
    job, expid = _publish_intake(conn, run, tmp_path, source, FLIGHT)
    assert _rows(conn, "SELECT e.expid, e.program_id, s.category FROM exposures_official e"
                       " JOIN surveys s USING (program_id) WHERE e.job = %s", (job,)) == [(expid, 1049, "COM")]


def test_two_stems_that_pack_to_one_old_expid_are_two_exposures_with_two_ids(connect, tmp_path, source):  # noqa: F811
    for stem in (PLAN_1, PLAN_2):
        write_asdf(source / f"{stem}_cal.asdf", stem)
    conn = connect()
    run = _live_run(conn)
    _, first = _publish_intake(conn, run, tmp_path, source, PLAN_1)
    _, second = _publish_intake(conn, run, tmp_path, source, PLAN_2)
    assert first != second
    assert _rows(conn, "SELECT expid, execution_plan FROM exposures_official WHERE dataset = %s ORDER BY expid",
                 (DATASET,)) == [(first, 1), (second, 2)]


def test_the_intake_exits_65_when_its_scopes_expid_is_not_the_id_of_its_stems_name(connect, tmp_path, source):  # noqa: F811
    write_asdf(source / f"{FLIGHT}_cal.asdf", FLIGHT)
    conn = connect()
    name = loop.stem_ids(FLIGHT)["name"]
    held = loop.exposure_ids(conn, DATASET, [name])[name]
    with pytest.raises(CommandExit) as other:
        run_intake(tmp_path / "other", source, FLIGHT, program_id="", surveys=(1049,), expid=held + 1,
                   exposure_ids=_lookup(conn))
    assert other.value.code == 65 and name in str(other.value) and str(held) in str(other.value)
    assert not (tmp_path / "other" / "outputs" / "manifest.json").exists()
    # A name no id was resolved for (another dataset's table has none for it): refused the same way.
    write_asdf(source / f"{PLAN_1}_cal.asdf", PLAN_1)
    with pytest.raises(CommandExit) as none:
        run_intake(tmp_path / "none", source, PLAN_1, program_id="", surveys=(1043,), expid=held,
                   exposure_ids=_lookup(conn))
    assert none.value.code == 65 and "has none" in str(none.value)
