"""`seal_admission_run` must run before the per-file admission phase, not after.

Observed live 2026-09-09 (D6 attempt 10, 890 of 890 files): every
`record_l2file_admission` call failed with `psycopg2.DatabaseError:
admission cites manifest 38 which is not sealed; a manifest is sealed
only once every entry is durable, so citing an unsealed one would
record an admission against a source that may still be partial (rule
20)`. The BEFORE INSERT triggers `admission_l2files_manifest_sealed`
and `admission_exposures_manifest_sealed` (rapid_systems migration
051) refuse any admission that cites a manifest whose `sealed_at IS
NULL`.

`db_register_socsim_files.py`, `db_register_rimtimsim_files.py` and
`db_register_troxel_sim_files.py` each sealed the manifest LAST -- after
the per-file admission loop -- on the theory that sealing should wait
until every file is known to have been admitted. That can never work
against 051's triggers: the manifest has to be sealed before the first
admission that cites it, not after. `backfill_g0001_admission.py`
documents the correct order in its own docstring: "THE ORDER IS
ENUMERATE, SEAL, THEN ADMIT -- and it is not the order the repository's
method list suggests."

Each of these three scripts runs top-level registration code at import
time (see `test_db_register_socsim_files_env.py`'s docstring), so they
cannot be imported directly in a test process. This test instead parses
each script's source with `ast` and asserts, purely structurally, that
the `seal_admission_run` call appears before the first call to whatever
performs per-file admission in that script (`run_single_core_job` /
`execute_parallel_processes` for the socsim script, which parallelizes;
the per-file `for` loop that follows the enumeration commit for the
other two, which do not).
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SIMS_DIR = REPO_ROOT / "database" / "sims"


def _call_name(node):
    """The dotted-or-bare name a Call node invokes, or None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _first_call_line(tree, names):
    """Line number of the first call to any of `names` anywhere in `tree`, or None."""
    best = None
    for node in ast.walk(tree):
        if _call_name(node) in names:
            if best is None or node.lineno < best:
                best = node.lineno
    return best


def _registration_body(tree):
    """The statements that drive one registration run, as an ast.Module.

    Either the `if __name__ == '__main__':` block's body directly
    (`db_register_socsim_files.py` inlines everything there), or -- when
    that block is just a call to a `register_files()`-shaped function
    (`db_register_rimtimsim_files.py`, `db_register_troxel_sim_files.py`)
    -- that function's own body.
    """
    main_if = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"
                    and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value == "__main__"):
                main_if = node
                break
    if main_if is None:
        raise AssertionError("no `if __name__ == '__main__':` block found")

    # If the block is a thin dispatch to a single top-level function, use
    # that function's body instead.
    calls = [n for n in main_if.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    if len(main_if.body) == 1 and calls:
        called_name = _call_name(calls[0].value)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == called_name:
                return node.body

    return main_if.body


def _assert_seal_precedes(script_name, admission_call_names):
    path = SIMS_DIR / script_name
    tree = ast.parse(path.read_text(), filename=str(path))
    body = _registration_body(tree)
    module = ast.Module(body=body, type_ignores=[])

    seal_line = _first_call_line(module, {"seal_admission_run"})
    admit_line = _first_call_line(module, admission_call_names)

    assert seal_line is not None, (
        f"{script_name}: no call to seal_admission_run found in the "
        "registration run")
    assert admit_line is not None, (
        f"{script_name}: no per-file admission call "
        f"({admission_call_names}) found in the registration run")
    assert seal_line < admit_line, (
        f"{script_name}: seal_admission_run (line {seal_line}) must run "
        f"before the per-file admission phase (line {admit_line}) -- "
        "051's BEFORE INSERT triggers refuse any admission that cites an "
        "unsealed manifest, so sealing after admission can never succeed."
    )


def test_socsim_seals_before_admitting():
    _assert_seal_precedes(
        "db_register_socsim_files.py",
        {"run_single_core_job", "execute_parallel_processes"})


def test_rimtimsim_seals_before_admitting():
    _assert_seal_precedes(
        "db_register_rimtimsim_files.py",
        {"register_exposure", "register_l2file"})


def test_troxel_seals_before_admitting():
    _assert_seal_precedes(
        "db_register_troxel_sim_files.py",
        {"register_exposure", "register_l2file"})
