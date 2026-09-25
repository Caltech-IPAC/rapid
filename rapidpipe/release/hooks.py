"""The hook contract: running a hook and validating its result.

Hooks are ``rapid_systems``' executables (``rapid`` never names accounts,
hosts or buckets); ``rapidpipe.release.core.cut`` runs them in the order
:data:`HOOKS`, and ``verify`` runs the optional :data:`INSPECT_HOOK`. The
full contract -- environment, result shapes, failure rule -- is in the
``rapidpipe.release`` package docstring; :func:`parse_hook_result`
enforces it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

#: Exit codes (package docstring, "Exit codes").
EXIT_SUCCESS = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_TRANSIENT = 75

#: The four hooks ``cut`` runs, in order, and the optional read-only one
#: ``verify`` runs.
HOOKS = ("migrate", "build", "deploy", "pins")
INSPECT_HOOK = "inspect"

#: Release state after each hook's validated result is recorded. The row
#: does not exist before ``migrate`` (the table ships in the release).
STATE_AFTER = {"migrate": "migrated", "build": "built", "deploy": "deployed", "pins": "complete"}
#: The next hook to run for a row in each state; ``complete`` has none.
NEXT_HOOK = {"migrated": "build", "built": "deploy", "deployed": "pins"}

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
JOB_DEFINITION_RE = re.compile(r"^[A-Za-z0-9_-]+:[0-9]+$")


class ReleaseError(Exception):
    """Base class; ``exit_code`` is what the command-line tool exits with."""

    exit_code = EXIT_REFUSED


class ReleaseUsage(ReleaseError):
    """Bad arguments or a precondition the operator must fix first (exit 2)."""

    exit_code = EXIT_USAGE


class ReleaseRefused(ReleaseError):
    """A check failed, a hook failed, or a resume does not match (exit 1)."""

    exit_code = EXIT_REFUSED


class HookFailed(ReleaseRefused):
    """A hook exited non-zero or its last stdout line was not the JSON
    object its contract requires. The row keeps its last good state."""


# ======================================================================
# hooks
# ======================================================================

def _hook_path(hooks_dir: str | Path, name: str) -> Path:
    return Path(hooks_dir) / name


def hook_available(hooks_dir: str | Path | None, name: str) -> bool:
    if hooks_dir is None:
        return False
    path = _hook_path(hooks_dir, name)
    return path.is_file() and os.access(path, os.X_OK)


def run_hook(
    hooks_dir: str | Path, name: str, env: dict[str, str],
    *, out: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run hook ``name``, stream its stdout, return its last line's JSON.

    Stdout lines are passed to ``out`` (default: this process's stdout) as
    they arrive; stderr is inherited. Raises :class:`HookFailed` on a
    non-zero exit, no output, or a last non-empty line that is not one
    JSON object.
    """
    out = out or (lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()))
    path = _hook_path(hooks_dir, name)
    full_env = {**os.environ, **env}
    try:
        proc = subprocess.Popen(
            [str(path)], env=full_env, stdout=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        raise HookFailed(f"hook {name} ({path}) could not start: {exc}") from exc
    last = ""
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        out(line)
        if line.strip():
            last = line.strip()
    code = proc.wait()
    if code != 0:
        raise HookFailed(f"hook {name} exited {code}")
    return parse_hook_result(name, last)


def parse_hook_result(name: str, line: str) -> dict[str, Any]:
    """Parse and validate one hook's last stdout line against its contract."""
    try:
        result = json.loads(line)
    except json.JSONDecodeError as exc:
        raise HookFailed(
            f"hook {name}: last stdout line is not JSON ({exc}): {line[:200]!r}") from exc
    if not isinstance(result, dict):
        raise HookFailed(f"hook {name}: last stdout line is not a JSON object: {line[:200]!r}")

    if name == "migrate":
        if not isinstance(result.get("schema_version"), str) or not isinstance(
                result.get("applied"), list):
            raise HookFailed(
                "hook migrate must print {\"schema_version\": str, \"applied\": [...]}")
    elif name == "build":
        digest, ref = result.get("image_digest"), result.get("image_ref")
        if not isinstance(digest, str) or not DIGEST_RE.match(digest):
            raise HookFailed(f"hook build: image_digest {digest!r} is not sha256:<64 hex>")
        if not isinstance(ref, str) or not ref.endswith("@" + digest):
            raise HookFailed(f"hook build: image_ref {ref!r} does not end with @{digest}")
    elif name in ("deploy", INSPECT_HOOK):
        deployments = result.get("deployments")
        if not isinstance(deployments, dict) or not deployments:
            raise HookFailed(f"hook {name} must print {{\"deployments\": {{consumer: name:rev}}}} "
                             "with at least one entry")
        for consumer, job_definition in deployments.items():
            if not isinstance(job_definition, str) or not JOB_DEFINITION_RE.match(job_definition):
                raise HookFailed(
                    f"hook {name}: deployment {consumer!r} = {job_definition!r} is not name:revision")
    elif name == "pins":
        rows = result.get("rows")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise HookFailed("hook pins must print {\"rows\": <int>}")
    return result
