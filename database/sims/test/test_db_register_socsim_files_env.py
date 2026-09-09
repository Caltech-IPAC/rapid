"""INPUTBUCKET must be required, not silently defaulted.

`db_register_socsim_files.py` used to fall back to a hardcoded bucket
(`socsims-fakesrc-fits-20260709-lite`) when `INPUTBUCKET` was unset,
with only a print line noting the fallback. A forgotten
`-e INPUTBUCKET=...` on an unattended run would then admit files from
that unrelated, stale dataset rather than failing. The fix makes a
missing or empty `INPUTBUCKET` fail closed, in the same shape the same
script already uses for `ROMANTESSELLATIONDBNAME`: print an error and
`exit(64)`.

The module runs registration as top-level code at import time (it opens
the tessellation sqlite database and, eventually, a live Postgres
connection -- see `database/sims/probe_register_socsim_exit_logic.py`'s
docstring for the same fact), so it cannot be imported directly in a
test process. This test instead runs it as a subprocess with
`INPUTBUCKET` unset, `ROMANTESSELLATIONDBNAME` set to a dummy path
(reached only after the bucket check that runs first), and asserts the
process exits 64 with the expected error text on stdout -- never
reaching the sqlite/Postgres code that would need live infrastructure.

No stubbing is required: boto3, healpy, psycopg2 and friends are real
dependencies of this environment's test extra, so the subprocess's own
imports resolve unaided.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "database" / "sims" / "db_register_socsim_files.py"


def _run_with_env(env_overrides):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_unset_inputbucket_fails_closed():
    result = _run_with_env({
        "INPUTBUCKET": None,
        "ROMANTESSELLATIONDBNAME": "/tmp/does-not-need-to-exist.sqlite",
    })
    assert result.returncode == 64, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    assert "Env. var. INPUTBUCKET not set" in result.stdout


def test_empty_inputbucket_fails_closed():
    result = _run_with_env({
        "INPUTBUCKET": "",
        "ROMANTESSELLATIONDBNAME": "/tmp/does-not-need-to-exist.sqlite",
    })
    assert result.returncode == 64, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    assert "Env. var. INPUTBUCKET not set" in result.stdout
