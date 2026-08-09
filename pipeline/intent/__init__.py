"""The intent layer: work_units, unit_events, campaigns.

`design/operations.md` § Workflow schema, ADOPTED as v1 by the integration
review (rapid_plan/decisions.md, composite ruling 13):

    "Two state machines only. A work unit -- (job type, declared input
    scope, operational class, definition version) -- has six states:
    blocked ... ready, submitted, complete, failed, quarantined. ...
    Writers are exclusive per transition class: validation/ingest creates,
    the orchestrator submits and applies retry dispositions, the
    reconciler closes, the mutation API does operator overrides. A
    campaign -- one row per finite-class run, test campaigns the same
    shape under the test class -- runs defined -> active <-> paused ->
    complete | abandoned; progress is never stored, always derived from
    its units."

Module map:

* `writer` -- `WorkUnitWriter`/`CampaignWriter`: the injected-executor
  writers that create and transition work units and campaigns, exactly
  the shape `observability.attempts.AttemptWriter` establishes for
  attempts. This is the ONE place work_units/unit_events/campaigns rows
  are written from Python; nothing else bare-INSERTs or bare-UPDATEs
  these tables.

Schema: migration 036 (`rapid_systems/cloudformation/db-migrations/
036-intent-schema-v1.sql`), LIVE, applied. This module targets that
schema exactly -- six work_unit states, the partial unique index on
(job_type, input_scope) WHERE superseded_by_unit_id IS NULL, and the
writer-vocabulary CHECK on unit_events.writer. No live database is
touched by this module's own tests; every writer here is tested against
an injected fake executor, per this repo's house convention.
"""

from pipeline.intent.writer import (
    CampaignWriter,
    IllegalTransition,
    SupersessionConflict,
    WorkUnitWriter,
)

__all__ = [
    "CampaignWriter",
    "IllegalTransition",
    "SupersessionConflict",
    "WorkUnitWriter",
]
