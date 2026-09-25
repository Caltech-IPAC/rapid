--------------------------------------------------------------------------------------------------------------------------
-- 20260924-10-attempt-inputs-seeded-units.sql
--
-- Recovery: the inputs and settings an attempt ran with, frozen on the
-- attempt, and the link from a re-run's unit back to the unit it re-runs.
-- Authority: supervisor step 6 ruling R7 (2026-09-24) and rapid_docs'
-- runs page ("Attempts", recovery). `rapidpipe.launch.batch.submit_unit`
-- records `attempts.inputs_location` / `settings_location` in the same
-- transaction as the scheduler job id; `run create --seed <run>
-- --only-failed` (rapidpipe.runs.repository.seed_failed_units) creates
-- units with `seeded_from_unit` set; `run start` resolves such a unit's
-- inputs from the seed unit's most recent attempt (ruling R8).
--
-- Additive only: three nullable columns, nothing renamed or dropped.
-- Grants are unaffected: 20260923-01 and 20260923-03 grant on the whole
-- tables, which covers columns added later.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE attempts
    ADD COLUMN inputs_location text,
    ADD COLUMN settings_location text;

ALTER TABLE units
    ADD COLUMN seeded_from_unit rapid_ulid REFERENCES units (id);

CREATE INDEX units_seeded_from_unit_idx ON units (seeded_from_unit);

COMMENT ON COLUMN attempts.inputs_location IS
    'The inputs location (S3 URI) the attempt was submitted with, recorded '
    'by rapidpipe.launch.batch.submit_unit; NULL for attempts submitted '
    'before this column existed (supervisor step 6, R7; 20260924-10).';
COMMENT ON COLUMN attempts.settings_location IS
    'The settings overlay location the attempt was submitted with, NULL '
    'when it ran with the image defaults or predates this column '
    '(supervisor step 6, R7; 20260924-10).';
COMMENT ON COLUMN units.seeded_from_unit IS
    'For a unit created by run create --seed <run> --only-failed, the '
    'seed run''s non-complete unit it re-runs; NULL otherwise '
    '(supervisor step 6, R7; 20260924-10).';
