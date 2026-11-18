"""The surveys seed's calibration, CCS and GAS programs, and the script that adds them to an existing database.

``database/schema.sql`` seeds the 28 programs with their STScI titles;
``database/2026-11-17-survey-programs.sql`` adds those missing from an
older seed and leaves a program_id already present as it is. Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "database" / "2026-11-17-survey-programs.sql"
#: The programs by category, as the seed and the script hold them.
PROGRAMS = {
    "CAL": set(range(1064, 1083)),
    "CCS": {1400, 1410, 1420},
    "GAS": {1430, 2000, 2001, 2002, 2003, 2004},
}
NEW = set().union(*PROGRAMS.values())


def _script(cur):
    cur.execute(SCRIPT.read_text().replace("BEGIN;", "").replace("COMMIT;", ""))


def _by_category(cur):
    cur.execute("SELECT category, program_id FROM surveys WHERE program_id = ANY(%s)", (sorted(NEW),))
    found = {}
    for category, program in cur.fetchall():
        found.setdefault(category, set()).add(program)
    return found


def test_the_seed_holds_the_calibration_ccs_and_gas_programs(conn):
    with conn.cursor() as cur:
        assert _by_category(cur) == PROGRAMS
        cur.execute("SELECT program_title FROM surveys WHERE program_id IN (1078, 2001) ORDER BY 1")
        assert cur.fetchall() == [("Dispersion Solution - Prism",),
                                  ("Roman eXtreme Deep Field (RXDF): transformative science at Roman's limit",)]


def test_the_script_adds_missing_programs_and_leaves_existing_ones(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM surveys WHERE program_id = ANY(%s) AND program_id <> 1064", (sorted(NEW),))
        cur.execute("UPDATE surveys SET program_title = 'Darks', category = 'X' WHERE program_id = 1064")
        cur.execute("SELECT count(*) FROM surveys")
        before = cur.fetchone()[0]
        _script(cur)
        cur.execute("SELECT count(*) FROM surveys")
        assert cur.fetchone()[0] == before + len(NEW) - 1
        cur.execute("SELECT program_title, category FROM surveys WHERE program_id = 1064")
        assert cur.fetchone() == ("Darks", "X")
        _script(cur)
        cur.execute("SELECT count(*) FROM surveys")
        assert cur.fetchone()[0] == before + len(NEW) - 1


def test_the_script_matches_the_seed(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT program_id, program_title, category FROM surveys WHERE program_id = ANY(%s)",
                    (sorted(NEW),))
        seed = set(cur.fetchall())
        cur.execute("DELETE FROM surveys WHERE program_id = ANY(%s)", (sorted(NEW),))
        _script(cur)
        cur.execute("SELECT program_id, program_title, category FROM surveys WHERE program_id = ANY(%s)",
                    (sorted(NEW),))
        assert set(cur.fetchall()) == seed and len(seed) == len(NEW)
