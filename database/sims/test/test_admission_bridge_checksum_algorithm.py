"""`record_l2file_admission` must never guess a checksum's algorithm.

Observed live on rapid-admin, 2026-09-09 20:30 UTC: the D6 socsim admission
run (attempt 8, 444 of 444 files) failed every `register_l2file` call with
`AdmissionIdentityError: a sha256 checksum is 64 hex characters; got 32`. The
32-character value is an md5 digest from `db.compute_checksum` (see
`database/modules/utils/rapid_db.py`, `l2files.checksum varchar(32)`), but
`record_l2file_admission` defaulted `checksum_algorithm="sha256"` and all
three production registrars (`db_register_socsim_files.py`,
`db_register_troxel_sim_files.py`, `db_register_rimtimsim_files.py`) omitted
the keyword, so every admission was mislabelled. `backfill_g0001_admission.py`
passes `checksum_algorithm="md5"` explicitly, which is why only that path
worked.

The fix removes the default: `checksum_algorithm` is now a required keyword,
so a caller that omits it fails loudly at the call, not with a confusing
64-vs-32 message from deep inside identity normalization two frames later.

Stub-tier: `admission_bridge` imports only from `pipeline.repositories`,
which needs no psycopg2/boto3/live connection to exercise this signature
check. The second test walks the repository with `ast`, the same shape as
`test_execute_sql_queries_callers.py`, so it needs no imports of the call
sites themselves (several need psycopg2/boto3/healpy at import time).
"""

import ast
import inspect
from pathlib import Path

import pytest

from database.sims.admission_bridge import record_l2file_admission

REPO_ROOT = Path(__file__).resolve().parents[3]

SKIP_DIR_NAMES = {".venv", ".git", "rapid_pipeline.egg-info"}


def test_checksum_algorithm_is_a_required_keyword():
    """No default: a caller that omits it must fail at the call site, not
    with a mismatched-digest-length error from inside identity
    normalization."""
    params = inspect.signature(record_l2file_admission).parameters
    assert "checksum_algorithm" in params
    assert params["checksum_algorithm"].default is inspect.Parameter.empty


def test_calling_without_checksum_algorithm_raises_type_error():
    """The live failure mode, reproduced without a database: omitting the
    keyword must raise immediately, not silently default to sha256."""
    with pytest.raises(TypeError):
        record_l2file_admission(
            dbh=object(), exposure=1, sca=1, source_checksum="d" * 32,
            rid=1, facts={})


#: This file itself calls `record_l2file_admission` without the keyword,
#: deliberately, to prove the TypeError fires -- not a production call site.
THIS_FILE = Path(__file__).resolve()


def _iter_repo_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.resolve() == THIS_FILE:
            continue
        yield path


def _offending_calls(path):
    """Calls in `path` to a function/method named `record_l2file_admission`
    that pass no `checksum_algorithm` keyword argument."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name != "record_l2file_admission":
            continue
        if any(kw.arg == "checksum_algorithm" for kw in node.keywords):
            continue
        offenders.append(node.lineno)
    return offenders


def test_every_call_site_passes_checksum_algorithm():
    violations = {}
    for path in _iter_repo_python_files():
        # The function's own definition contains no call to itself.
        offenders = _offending_calls(path)
        if offenders:
            violations[str(path.relative_to(REPO_ROOT))] = offenders

    assert not violations, (
        "record_l2file_admission() called without a checksum_algorithm "
        "keyword -- the default that let this happen silently is gone, "
        "so every call site must say what algorithm it computed:\n"
        + "\n".join(f"  {file}:{','.join(map(str, lines))}"
                     for file, lines in sorted(violations.items()))
    )
