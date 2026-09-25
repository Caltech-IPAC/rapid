"""Stand-ins for the export stage's boundaries: an input-set manifest, a fake database.

- :func:`build_export_input_set` writes the stage's input-set manifest
  (stage ``input-set``, unit ``field``), naming its result sets only by
  instance id in ``inputs.result_sets`` -- a database result set has no
  member files (products page, "Database result sets": "rows, not files").
- :class:`FakeExportDatabase` has
  :class:`rapidpipe.stages.export.PostgresExportDatabase`'s methods over
  in-memory state: registered result sets (kind, completeness, deletion
  state) and ``sources`` rows keyed by ``result_set``. :func:`seed_rows`
  makes the fixture's rows: ~200 sources scattered around one position in
  two named source sets, plus a set that is not named, whose rows must not
  be read. :func:`fake_database` is the factory
  ``RAPIDPIPE_EXPORT_DATABASE`` names for a stage run as a subprocess: it
  reads its seed from the JSON file ``RAPIDPIPE_FAKE_EXPORT_SEED`` names
  and writes what was read to the JSON file ``RAPIDPIPE_FAKE_EXPORT_STATE``
  names when the context exits.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time) so ``rapidpipe selftest --stage
export`` can import it inside the image.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
from pathlib import Path
from typing import Any, Iterator, Sequence

from rapidpipe.db.sources import READABLE_COLUMNS

UNIT_ID = "4711398"
RUN = "01J8Y6QZ3MF1NA1E00000000RN"
ATTEMPT = "01J8Y6QZ3MF1NA1E00000000AT"
SOURCE_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000S1"
SECOND_SOURCE_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000S2"
UNNAMED_SOURCE_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000S9"
ASSOCIATION_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000A1"
STATISTICS_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000T1"

#: The fixture's position (dev's example source record in
#: generateSourceHATSCatalog.py) and scatter, degrees.
CENTER = (268.52830287805165, -29.281683816567625)
SCATTER = 0.01

SEED_ENV = "RAPIDPIPE_FAKE_EXPORT_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_EXPORT_STATE"


def build_export_input_set(
    inputs: Path, *, unit_id: str = UNIT_ID,
    result_sets: Sequence[str] | None = None,
) -> Path:
    """Write a synthetic export input-set manifest under ``inputs``.

    ``result_sets`` overrides the default named sets (two source sets and
    an association set) -- pass ``()`` for "names no result sets", or a
    tuple with a repeat for "names a result set twice". Returns the path.
    """
    named = (list(result_sets) if result_sets is not None
             else [SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE, ASSOCIATION_SET_INSTANCE])
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


def _row(sid: int, result_set: str, rng: random.Random, flags: int) -> dict[str, Any]:
    ra = CENTER[0] + rng.uniform(-SCATTER, SCATTER)
    dec = CENTER[1] + rng.uniform(-SCATTER, SCATTER)
    row: dict[str, Any] = {c: 0 for c in READABLE_COLUMNS}
    row.update({
        "sid": sid, "id": sid % 1000, "pid": 328539, "ra": ra, "dec": dec,
        "xfit": rng.uniform(0, 4088), "yfit": rng.uniform(0, 4088),
        "fluxfit": rng.uniform(-300, 3000), "xerr": 0.07, "yerr": 0.09, "fluxerr": 19.7,
        "npixfit": 238, "qfit": -26.4, "cfit": 0.08, "redchi": 1.0, "flags": flags,
        "sharpness": 0.89, "roundness1": -0.22, "roundness2": -0.12, "npix": 25,
        "peak": 129.6, "isdiffpos": sid % 2 == 0, "field": int(UNIT_ID), "hp6": 28823,
        "hp9": 1844709, "expid": 75740, "fid": 4, "sca": 2, "mjdobs": 61514.91410272289,
        "run": RUN, "attempt": ATTEMPT, "result_set": result_set,
    })
    return row


def seed_rows(counts: dict[str, int], *, flagged_every: int = 10,
              seed: int = 20260924) -> list[dict[str, Any]]:
    """``counts[set]`` rows per source set, sids ascending, every ``flagged_every``-th flagged."""
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    sid = 293228900
    for result_set, count in counts.items():
        for i in range(count):
            sid += 1
            rows.append(_row(sid, result_set, rng, 5 if i % flagged_every == 0 else 0))
    return rows


def seed_from_fixture(spec: dict[str, Any]) -> dict[str, Any]:
    """The fake database's seed from ``expected.json``'s ``inputs``."""
    instances = {name: {"kind": v["kind"], "complete": v.get("complete", True),
                        "deletion_state": v.get("deletion_state", "retained"),
                        "row_count": v.get("rows")}
                 for name, v in spec["result_sets"].items()}
    counts = {name: v["rows"] for name, v in spec["result_sets"].items()
              if v["kind"] == "source-set"}
    return {"instances": instances, "sources": seed_rows(counts)}


class FakeExportDatabase:
    """:class:`rapidpipe.stages.export.PostgresExportDatabase` over in-memory state."""

    def __init__(self, instances: dict[str, dict[str, Any]] | None = None,
                 sources: list[dict[str, Any]] | None = None) -> None:
        self.instances = dict(instances or {})
        self.sources = list(sources or [])
        self.queries: list[dict[str, Any]] = []
        self.rows_read = 0

    def result_set_states(self, instances: list[str]) -> dict[str, dict[str, Any]]:
        return {i: dict(self.instances[i]) for i in instances if i in self.instances}

    def source_rows(self, source_sets: list[str], columns: tuple[str, ...], *,
                    flags_zero_only: bool) -> Iterator[tuple]:
        unknown = [c for c in columns if c not in READABLE_COLUMNS]
        if unknown:
            raise ValueError(f"not readable sources columns: {unknown}")
        self.queries.append({"source_sets": list(source_sets), "columns": list(columns),
                             "flags_zero_only": flags_zero_only})
        wanted = set(source_sets)
        for row in sorted(self.sources, key=lambda r: r["sid"]):
            if row["result_set"] in wanted and (not flags_zero_only or row["flags"] == 0):
                self.rows_read += 1
                yield tuple(row[c] for c in columns)

    def state(self) -> dict[str, Any]:
        return {"queries": self.queries, "rows_read": self.rows_read}


@contextlib.contextmanager
def fake_database():
    """The ``RAPIDPIPE_EXPORT_DATABASE`` factory: seeded from, and saved to, JSON files."""
    seed = json.loads(Path(os.environ[SEED_ENV]).read_text())
    db = FakeExportDatabase(instances=seed.get("instances"), sources=seed.get("sources"))
    try:
        yield db
    finally:
        Path(os.environ[STATE_ENV]).write_text(json.dumps(db.state(), indent=2, sort_keys=True))
