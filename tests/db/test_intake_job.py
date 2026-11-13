"""An intake job's exposures row published with the file's own program, the surveys seed and the SMDC surveys migration.

The intake stage runs on the unit tests' synthetic SOC L2 file (its PSF
and surveys lookups faked) with ``[intake] program_id`` unset, and its
attempt is published for real: the official ``exposures`` row carries the
file's ``PROGRAM`` and joins its commissioning ``surveys`` row. The seed
holds one negative id per simulation and the twelve commissioning
programs; ``database/2026-11-13-survey-programs.sql`` brings an older seed
to it and moves the HLWAS simulation's exposures from -1 to -4. Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

from pathlib import Path

from rapidpipe.foundation import change, create_job, create_run, publish
from tests.unit.test_intake import run_intake, write_asdf
from tests.unit.test_intake import source  # noqa: F401  (a fixture)

MIGRATION = Path(__file__).resolve().parents[2] / "database" / "2026-11-13-survey-programs.sql"
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
        cur.execute("SELECT program_id, program_title FROM surveys WHERE category = 'SIM' ORDER BY 1")
        assert cur.fetchall() == [(-4, "SOCSIMS HLWAS"), (-3, "RimTimSims"), (-2, "OpenUniverse"),
                                  (-1, "SOCSIMS GBTDS")]
        cur.execute("SELECT program_id FROM surveys WHERE category = 'COM'")
        assert {r[0] for r in cur.fetchall()} == COMMISSIONING
        cur.execute("SELECT program_title FROM surveys WHERE program_id = 1053")
        assert cur.fetchone() == ("High-Extinction Guiding Opportunity Activity",)


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
    assert _rows(conn, "SELECT e.program_id, s.program_title FROM exposures_official e"
                       " JOIN surveys s USING (program_id) WHERE e.job = %s", (job,)) == [
        (1049, "RAPID Implementation")]


def test_the_migration_brings_an_older_seed_to_schema_and_moves_hlwas_exposures_to_minus_4(conn):
    body = MIGRATION.read_text().replace("BEGIN;", "").replace("COMMIT;", "")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM surveys WHERE program_id = -4 OR category = 'COM'")
        cur.execute("UPDATE surveys SET program_title = 'SOCSIMS' WHERE program_id = -1")
        cur.execute("INSERT INTO surveys (program_id, program_title, category)"
                    " VALUES (1053, 'High-Extinction Guiging Opportunity Activity', 'COM')")
        cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset)"
                    " VALUES ('t', 'test', 'abc', '{}', 'S', 'D') RETURNING id")
        cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, 'intake', '{}') RETURNING id",
                    (cur.fetchone()[0],))
        job = cur.fetchone()[0]
        for expid, (dataset, program) in enumerate([("r00342-local", -1), ("r00342-inj1", -1),
                                                    ("r00340-gb1", -1), ("ou-tds1", -2)]):
            cur.execute("INSERT INTO exposures (job, dataset, expid, dateobs, exptime, program_id)"
                        " VALUES (%s, %s, %s, now(), 60, %s)", (job, dataset, expid, program))

        cur.execute(body)

        cur.execute("SELECT program_id, program_title, category FROM surveys"
                    " WHERE program_id < 0 OR program_id = 1053 ORDER BY 1")
        assert cur.fetchall() == [(-4, "SOCSIMS HLWAS", "SIM"), (-3, "RimTimSims", "SIM"),
                                  (-2, "OpenUniverse", "SIM"), (-1, "SOCSIMS GBTDS", "SIM"),
                                  (1053, "High-Extinction Guiding Opportunity Activity", "COM")]
        cur.execute("SELECT program_id FROM surveys WHERE category = 'COM'")
        assert {r[0] for r in cur.fetchall()} == COMMISSIONING
        cur.execute("SELECT dataset, program_id FROM exposures WHERE job = %s ORDER BY expid", (job,))
        assert cur.fetchall() == [("r00342-local", -4), ("r00342-inj1", -4), ("r00340-gb1", -1),
                                  ("ou-tds1", -2)]
