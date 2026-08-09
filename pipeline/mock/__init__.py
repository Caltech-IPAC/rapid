"""The mission-mock harness: schedule-to-generation staging.

`design/operations.md` § Mission mock and test campaigns, ADOPTED:

    "Mock inputs enter through the same validation/ingest writer as real
    inputs: the harness's one new component transforms mission-schedule
    rows into staged generations -- exposure to per-SCA objects, wall
    time to MJD, manifest written last -- under simulated-substrate
    identity fixed at creation. Nothing else writes input rows."

Module map:

* `transformer` -- `stage_generation`: the one new component the design
  names, plus `create_mock_campaign`, which wires a staged generation's
  work units to a campaign row through `pipeline.intent.writer`.

**PROMINENT FINDING, stated here because it governs everything in this
package (integration review ruling 13's task brief, verified against this
repo rather than assumed):** there is no existing S3 "staged inputs"
reader for this package to mirror. The production science gatherer
(`submission.gathering.gather_science_units`) reads EXCLUSIVELY from
database rows (`l2filemeta`/`l2files`, via `rapid_db.RAPIDDB` query
methods) — `pipeline.operator.inputs`'s docstring phrase "the staged
inputs occupy 2027-10-01 to 2027-10-07" refers to rows already present in
those tables (populated by ad hoc scripts under `database/sims/
db_register_*sim_files.py`, which parse real FITS headers and INSERT rows
directly), never to S3 objects a gatherer lists or reads. This package
therefore does NOT reproduce a nonexistent reader; it defines its own v1
staging layout — documented in `transformer`'s module docstring — written
through the injectable `pipeline.runtime.boundaries.ObjectStore` boundary,
with its own key grammar stated and justified there.
"""

from pipeline.mock.transformer import (
    GenerationManifest,
    ScheduleRow,
    StagedSCA,
    create_mock_campaign,
    stage_generation,
)

__all__ = [
    "GenerationManifest",
    "ScheduleRow",
    "StagedSCA",
    "create_mock_campaign",
    "stage_generation",
]
