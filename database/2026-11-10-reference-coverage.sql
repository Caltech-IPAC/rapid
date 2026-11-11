-- The reference's coverage selection (port/comm-refimage-coverage-selection,
-- commissioning d586136b, ebd95254, debbc6e4, 702d0cad): a job's new state
-- 'declined', a stage that ended normally and published nothing (a reference
-- whose coverage map is short of [reference.selection] min_coverage_fraction),
-- and refimages' covfrac and covdepth, the coverage its publisher records
-- from the reference manifest, null unless the reference was selected by
-- coverage. The columns are nullable with no default, so the table is not
-- rewritten. schema.sql carries both for a fresh database; this file brings
-- an existing one to it, applied once, in one transaction.

BEGIN;

ALTER TABLE jobs DROP CONSTRAINT jobs_state_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_state_check
    CHECK (state IN ('launched', 'running', 'published', 'failed', 'declined'));

ALTER TABLE refimages
    ADD COLUMN covfrac real,
    ADD COLUMN covdepth real;

COMMIT;
