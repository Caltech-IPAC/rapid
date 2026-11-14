"""An intake job's exposures row published with the file's own program, and the surveys seed.

The intake stage runs on the unit tests' synthetic SOC L2 file (its PSF
and surveys lookups faked) with ``[intake] program_id`` unset, and its
attempt is published for real: the official ``exposures`` row carries the
file's ``PROGRAM`` and joins its commissioning ``surveys`` row. The seed
holds one negative id per simulation and the twelve commissioning
programs. Run by foundation.yml against database/schema.sql.
"""

from __future__ import annotations

from rapidpipe.foundation import change, create_job, create_run, publish
from tests.unit.test_intake import run_intake, write_asdf
from tests.unit.test_intake import source  # noqa: F401  (a fixture)

FLIGHT = "r0104901001001002001_0001_wfi08_f146"
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
