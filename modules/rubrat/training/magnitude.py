"""Canonical HLTDS magnitude handling and evaluation-only policy."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np


def _finite(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if np.isfinite(result) else float("nan")


def corrected_ab_magnitude(metadata: dict[str, Any]) -> float:
    """Return the unambiguous AB magnitude associated with a real row.

    New datasets carry ``truth_mag_ab``. Legacy inputs may carry instrumental
    ``truth_mag_instrumental`` and ``truth_zpt``; those are combined exactly
    once. Injection catalogs may supply an already calibrated ``mag_ab``.
    """
    direct = _finite(metadata.get("truth_mag_ab"))
    if np.isfinite(direct):
        return direct
    instrumental = _finite(metadata.get("truth_mag_instrumental"))
    zpt = _finite(metadata.get("truth_zpt"))
    if np.isfinite(instrumental) and np.isfinite(zpt):
        return instrumental + zpt
    for key in ("mag_ab_corrected", "mag_ab", "injection_mag_ab", "catalog_magnitude"):
        value = _finite(metadata.get(key))
        if np.isfinite(value):
            return value
    return float("nan")


def apply_hltds_magnitude_policy(
    labels: Iterable[int],
    metadata: Iterable[dict[str, Any]],
    *,
    limit_ab: float = 26.0,
) -> tuple[np.ndarray, dict[str, int]]:
    """Select all bogus rows and real rows with corrected AB magnitude <= limit."""
    y = np.asarray(list(labels), dtype=np.int64).reshape(-1)
    rows = list(metadata)
    if len(rows) != len(y):
        raise ValueError(f"metadata length {len(rows)} does not match labels length {len(y)}")
    mags = np.asarray([corrected_ab_magnitude(row) for row in rows], dtype=np.float64)
    real = y == 1
    included_real = real & np.isfinite(mags) & (mags <= float(limit_ab))
    mask = ~real | included_real
    summary = {
        "n_total": int(len(y)),
        "n_bogus_retained": int((y == 0).sum()),
        "n_real_total": int(real.sum()),
        "n_real_retained": int(included_real.sum()),
        "n_real_excluded_faint_or_missing": int((real & ~included_real).sum()),
    }
    return mask, summary
