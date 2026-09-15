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

2026-09-15: the same default lived on every layer BELOW the bridge --
`AdmissionRepository.add_manifest_entry` / `admit_l2file`, the identity
helpers `normalized_checksum` / `l2file_payload` / `l2file_identity`, and the
bridge's own `enumerate_source` (as `algorithm`). The repository is reachable
without the bridge (`backfill_g0001_admission.py` calls it directly), so a
caller there would have met the identical silent mislabel. Those defaults are
gone too, and the signature and call-site checks below cover the whole chain.

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

from database.sims.admission_bridge import (enumerate_source,
                                            record_l2file_admission)
from pipeline.repositories import admission_identity
from pipeline.repositories.admission import AdmissionRepository

REPO_ROOT = Path(__file__).resolve().parents[3]

SKIP_DIR_NAMES = {".venv", ".git", "rapid_pipeline.egg-info"}

#: Every function on the admission path that takes an algorithm, with the
#: name it takes it under. A default on ANY of these is a place a caller can
#: lean on without saying what it computed.
ALGORITHM_PARAMETERS = {
    "record_l2file_admission": (record_l2file_admission, "checksum_algorithm"),
    "enumerate_source": (enumerate_source, "algorithm"),
    "AdmissionRepository.add_manifest_entry": (
        AdmissionRepository.add_manifest_entry, "checksum_algorithm"),
    "AdmissionRepository.admit_l2file": (
        AdmissionRepository.admit_l2file, "checksum_algorithm"),
    "normalized_checksum": (admission_identity.normalized_checksum,
                            "algorithm"),
    "l2file_payload": (admission_identity.l2file_payload,
                       "checksum_algorithm"),
    "l2file_identity": (admission_identity.l2file_identity,
                        "checksum_algorithm"),
}

#: The callables the repository-wide sweep checks, keyed by the call name as
#: it appears in source, with the keyword each must be passed. The identity
#: helpers are not swept: `normalized_checksum` takes its algorithm
#: positionally and its only callers are the repository and its own tests.
SWEPT_CALLS = {
    "record_l2file_admission": "checksum_algorithm",
    "admit_l2file": "checksum_algorithm",
    "add_manifest_entry": "checksum_algorithm",
    "enumerate_source": "algorithm",
}


@pytest.mark.parametrize("name", sorted(ALGORITHM_PARAMETERS))
def test_checksum_algorithm_is_a_required_keyword(name):
    """No default: a caller that omits it must fail at the call site, not
    with a mismatched-digest-length error from inside identity
    normalization."""
    function, parameter = ALGORITHM_PARAMETERS[name]
    params = inspect.signature(function).parameters
    assert parameter in params, f"{name} no longer takes {parameter}"
    assert params[parameter].default is inspect.Parameter.empty, (
        f"{name} defaults {parameter} to {params[parameter].default!r}; "
        f"the algorithm must be stated by the caller")


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
    """Calls in `path` to any name in `SWEPT_CALLS` that pass neither the
    required keyword nor a `**kwargs` splat that could carry it."""
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
        if name not in SWEPT_CALLS:
            continue
        keyword = SWEPT_CALLS[name]
        if any(kw.arg in (keyword, None) for kw in node.keywords):
            continue
        offenders.append(f"{node.lineno}:{name}")
    return offenders


def test_every_call_site_passes_checksum_algorithm():
    violations = {}
    for path in _iter_repo_python_files():
        # The functions' own definitions contain no calls to themselves.
        offenders = _offending_calls(path)
        if offenders:
            violations[str(path.relative_to(REPO_ROOT))] = offenders

    assert not violations, (
        "an admission-path function called without its algorithm keyword "
        "-- the default that let this happen silently is gone, so every "
        "call site must say what algorithm it computed:\n"
        + "\n".join(f"  {file}:{','.join(lines)}"
                     for file, lines in sorted(violations.items()))
    )
