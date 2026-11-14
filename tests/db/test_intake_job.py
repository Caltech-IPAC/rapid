"""An intake job's exposures row published with the file's own program, the surveys seed, and the expid refusal.

The intake stage runs on the unit tests' synthetic SOC L2 file (its PSF
and surveys lookups faked) with ``[intake] program_id`` unset, and its
attempt is published for real: the official ``exposures`` row carries the
file's ``PROGRAM`` and joins its commissioning ``surveys`` row. The seed
holds one negative id per simulation and the twelve commissioning
programs. A file whose stem packs to the ``expid`` of another program,
plan or segment's official exposure is refused, exit 65, naming both
files. Run by foundation.yml against database/schema.sql.
"""

from __future__ import annotations

import pytest

from rapidpipe.exitcodes import CommandExit
from rapidpipe.foundation import change, create_job, create_run, publish
from tests.unit.test_intake import run_intake, write_asdf
from tests.unit.test_intake import source  # noqa: F401  (a fixture)

FLIGHT = "r0104901001001002001_0001_wfi08_f146"
#: Execution plan 02 and segment 002 of FLIGHT's pass, observation, visit and exposure: the same expid.
FLIGHT_PLAN_2 = "r0104902001002002001_0001_wfi08_f146"
#: The commissioning programs, category COM.
COMMISSIONING = {1006, 1007, 1008, 1018, 1020, 1043, 1049, 1053, 1054, 1055, 1062, 1083}


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_the_surveys_seed_holds_one_id_per_simulation_and_the_commissioning_programs(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT program_id FROM surveys WHERE category = 'SIM' ORDER BY 1")
        assert cur.fetchall() == [(-4,), (-3,), (-2,), (-1,)]
        cur.execute("SELECT program_id FROM surveys WHERE category = 'COM'")
        assert {r[0] for r in cur.fetchall()} == COMMISSIONING


def test_an_intake_job_publishes_the_files_own_program(connect, tmp_path, source):  # noqa: F811
    write_asdf(source / f"{FLIGHT}_cal.asdf", FLIGHT)
    prefix = run_intake(tmp_path, source, FLIGHT, program_id="", surveys=(1049,))

    conn = connect()
    run = create_run(conn, "tester", "test", "abc123", {}, "S", "r00342")
    change(conn, [{"open": {"survey": "S", "dataset": "r00342", "run": run}}], "tester", "live")
    expid = 1 * 10**6 + 2 * 10**4 + 1 * 10**2 + 1  # pass 1, observation 2, visit 1, exposure 1
    job = create_job(conn, run, "intake", {}, expects=[
        {"product": "exposures", "key": {"dataset": "r00342", "expid": expid}, "binding": None}])
    result = publish(conn, job, 1, prefix.as_uri())

    assert result["refusal"] is None
    assert _rows(conn, "SELECT e.program_id, s.category FROM exposures_official e"
                       " JOIN surveys s USING (program_id) WHERE e.job = %s", (job,)) == [(1049, "COM")]


def test_a_file_whose_expid_is_another_plans_official_exposure_is_refused_65_naming_both(
        connect, tmp_path, source):  # noqa: F811
    write_asdf(source / f"{FLIGHT}_cal.asdf", FLIGHT)
    write_asdf(source / f"{FLIGHT_PLAN_2}_cal.asdf", FLIGHT_PLAN_2)
    conn = connect()
    run = create_run(conn, "tester", "test", "abc123", {}, "S", "r00342")
    change(conn, [{"open": {"survey": "S", "dataset": "r00342", "run": run}}], "tester", "live")
    expid = 1 * 10**6 + 2 * 10**4 + 1 * 10**2 + 1
    first = run_intake(tmp_path / "first", source, FLIGHT, program_id="", surveys=(1049,))
    job = create_job(conn, run, "intake", {}, expects=[
        {"product": "exposures", "key": {"dataset": "r00342", "expid": expid}, "binding": None}])
    assert publish(conn, job, 1, first.as_uri())["refusal"] is None

    def official(statement, params):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            row = cur.fetchone()
        conn.commit()
        return row

    # The exposure's own file again passes; the other plan's file of the same expid does not.
    run_intake(tmp_path / "again", source, FLIGHT, program_id="", surveys=(1049,), exposures=official)
    with pytest.raises(CommandExit) as refused:
        run_intake(tmp_path / "second", source, FLIGHT_PLAN_2, program_id="", surveys=(1049,),
                   exposures=official)
    assert refused.value.code == 65
    assert FLIGHT_PLAN_2 in str(refused.value) and f"{FLIGHT}_cal.fits.gz" in str(refused.value)
