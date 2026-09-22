"""Cheap checks on the container recipe that run without Docker/Podman.

The real proof that the recipe builds and runs is
``.github/workflows/container.yml`` (it needs a container engine, so it
is not duplicated here). These two checks catch the mistakes that would
otherwise only surface in that CI job: a committed default that would
leak a registry or account identifier into the public repository, and a
build script with a syntax error.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERFILE = REPO_ROOT / "containers" / "rapid-pipeline" / "Containerfile"
BUILD_SH = REPO_ROOT / "containers" / "rapid-pipeline" / "build.sh"

#: Matches an ECR-style registry host or a bare 12-digit AWS account id --
#: the two shapes a hard-coded, account-tied base image reference could
#: take (specification, "Repositories": account identifiers are injected
#: at deploy time, never committed).
_ACCOUNT_LIKE = re.compile(r"dkr\.ecr|(?<!\d)\d{12}(?!\d)")


def test_no_default_base_image_names_a_registry():
    text = CONTAINERFILE.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ARG RAPID_BASE_IMAGE"):
            # A bare declaration ("ARG RAPID_BASE_IMAGE") or one with a
            # default ("ARG RAPID_BASE_IMAGE=...") are both fine as long
            # as no default value is an account-tied registry reference.
            assert not _ACCOUNT_LIKE.search(stripped), (
                f"RAPID_BASE_IMAGE must not default to an account-tied "
                f"registry reference: {stripped!r}"
            )
    assert not _ACCOUNT_LIKE.search(text), (
        "Containerfile must not name a registry host or AWS account id "
        "anywhere (specification, \"Repositories\")"
    )


def test_build_sh_syntax():
    result = subprocess.run(
        ["bash", "-n", str(BUILD_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
