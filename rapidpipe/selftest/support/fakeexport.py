"""A synthetic input-set manifest for the `export` stage's fixture.

``export`` is a declared stub (supervisor step 8, 2026-09-24, ruling R9):
a valid invocation exits 69 without ever opening a database connection.
Its input-set manifest (stage ``input-set``, unit ``field``) names its
result sets only by instance id in ``inputs.result_sets`` -- a database
result set has no member files (products page, "Database result sets":
"rows, not files"), so unlike ``fakephotometry.py`` this fixture writes
no FITS pixels at all, only the manifest.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time) so ``rapidpipe selftest --stage
export`` can import it inside the image.
"""

from __future__ import annotations

import json
from pathlib import Path

UNIT_ID = "4711398"
RUN = "01J8Y6QZ3MF1NA1E00000000RN"
ATTEMPT = "01J8Y6QZ3MF1NA1E00000000AT"
SOURCE_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000S1"
ASSOCIATION_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000A1"
STATISTICS_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000T1"


def build_export_input_set(
    inputs: Path, *, unit_id: str = UNIT_ID,
    result_sets: tuple[str, ...] | None = None,
) -> Path:
    """Write a synthetic export input-set manifest under ``inputs``.

    ``result_sets`` overrides the default three named sets (one of each
    kind ``export`` may draw from) -- pass ``()`` for a bad-input test
    ("names no result sets") or a tuple with a repeat ("names a result set
    twice"). Returns the manifest path.
    """
    named = (list(result_sets) if result_sets is not None
             else [SOURCE_SET_INSTANCE, ASSOCIATION_SET_INSTANCE, STATISTICS_SET_INSTANCE])
    manifest = {
        "schema_version": "1", "run": RUN,
        "unit": {"kind": "field", "id": unit_id},
        "stage": "input-set", "attempt": ATTEMPT,
        "execution_record": "exec/input-set.json",
        "inputs": {"manifest": "composed/manifest.json", "products": {},
                   "result_sets": named},
        "outputs": [],
    }
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path
