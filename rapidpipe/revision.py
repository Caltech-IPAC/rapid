"""The source revision of the current git checkout.

Shared by ``rapidpipe.cli.main``, ``rapidpipe.runs.local`` and
``rapidpipe.stages.contract``, which each recorded a run's or an
execution record's source revision with their own copy of this lookup
before this module existed. Each caller applies its own fallback for
when git yields nothing (a plain ``"unknown"`` string, or, in the
stage contract, the ``RAPID_SOURCE_REVISION`` environment variable a
Batch container's image bakes in); this module is git only.
"""

from __future__ import annotations

import subprocess


def git_revision() -> str | None:
    """``git rev-parse HEAD`` in the current working directory, or
    ``None`` if git is unavailable, the cwd is not a repository, or any
    other non-zero exit or empty output."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None
