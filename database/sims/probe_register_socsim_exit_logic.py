"""
Offline probe for the failure-propagation logic of db_register_socsim_files.py.

The registration script opens the tessellation sqlite database and the RAPID
Postgres database at import time, so it cannot be imported without live
infrastructure.  This probe therefore re-states the two rules that the script's
termination block enforces and checks them against the outcome combinations
that the g0001 Batch runs produced, including the one that exited 0 while
writing zero rows.

Run offline, no AWS and no database:

    python3 database/sims/probe_register_socsim_exit_logic.py

Exits 0 if every case behaves as intended, 1 otherwise.
"""

import sys


def termination_exit_code(n_to_register,n_registered,n_failed):

    '''
    The termination rule from db_register_socsim_files.py, main program.
    Kept in sync by hand; see the "Termination" block there.
    '''

    if n_failed > 0:
        return 65

    if n_to_register > 0 and n_registered == 0:
        return 65

    return 0


# (description, n_to_register, n_registered, n_failed, expected exit code)

cases = [
    ("nothing listed, nothing done: nothing to do",            0,    0,    0,  0),
    ("all files registered",                                5166, 5166,    0,  0),
    ("g0001 rev-6: all listed, all downloads failed",       5166,    0, 5166, 65),
    ("zero rows written but no failure counted",            5166,    0,    0, 65),
    ("partial failure: most registered, some failed",       5166, 5000,  166, 65),
    ("single file failed",                                     1,    0,    1, 65),
]

n_bad = 0

for description,n_to_register,n_registered,n_failed,expected in cases:

    actual = termination_exit_code(n_to_register,n_registered,n_failed)

    if actual == expected:
        result = "ok"
    else:
        result = "FAIL"
        n_bad += 1

    print(f"{result:4s}  exit={actual:2d} (expected {expected:2d})  {description}")

print(f"\nn_cases,n_bad = {len(cases)},{n_bad}")

if n_bad > 0:
    print("*** Error: termination rule did not behave as intended")
    sys.exit(1)

sys.exit(0)
