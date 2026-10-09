"""Run and artifact provenance helpers."""

from __future__ import annotations

import importlib.metadata
import platform
import sys
from pathlib import Path
from typing import Any, Sequence

from classification.data_utils import resolve_npz_paths
from rubrat.validation import sha256_file


def artifact_refs(paths: str | Path | Sequence[str | Path]) -> list[dict[str, Any]]:
    return [
        {"path": str(path), "size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in resolve_npz_paths(paths)
    ]


def software_versions() -> dict[str, str]:
    names = ["tensorflow", "keras", "numpy", "scipy", "pandas", "astropy", "pyarrow"]
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def run_context(*, seed: int, train: Sequence[str | Path], val: Sequence[str | Path]) -> dict[str, Any]:
    return {
        "seed": int(seed),
        "python": sys.version,
        "platform": platform.platform(),
        "software_versions": software_versions(),
        "train_artifacts": artifact_refs(train),
        "validation_artifacts": artifact_refs(val),
    }
