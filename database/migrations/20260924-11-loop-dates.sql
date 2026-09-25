--------------------------------------------------------------------------------------------------------------------------
-- 20260924-11-loop-dates.sql
--
-- The processing-date loop's own record (supervisor step 7, ruling R7): one row
-- per (schedule, processing date) that `rapidpipe loop run` (rapidpipe/launch/
-- loop.py) has started. `run` is the date's one production run (R4); `state`
-- is open while the loop is walking the date, complete once the run is
-- promoted (or the promotion refused, R6) and finished, failed when a unit
-- failed. `promotion` is the promotions row id when the date's run was
-- promoted (no foreign key: a refusal leaves it NULL and says why in
-- `record`). `record` carries the spec location, release, per-stage units ->
-- selected attempt and scheduler job, the fields, the base association sets
-- bound per field (R5), the alert container, and the promotion or refusal.
-- The previous complete row of a schedule is how the next date finds its base
-- catalog per field. Additive only. Grants to `rapid_rebuild_pipeline` are
-- guarded on the role's existence, as 20260924-08 is, so CI (no such role)
-- applies them as a no-op.
--------------------------------------------------------------------------------------------------------------------------

CREATE TABLE loop_dates (
    schedule        text NOT NULL,
    processing_date date NOT NULL,
    run             rapid_ulid NOT NULL REFERENCES runs (id),
    state           text NOT NULL CHECK (state IN ('open', 'complete', 'failed')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    promotion       rapid_ulid,
    record          jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (schedule, processing_date)
);

CREATE INDEX loop_dates_run_idx ON loop_dates (run);

COMMENT ON TABLE loop_dates IS
    'One row per processing date a scheduled loop (rapidpipe loop run) has started: '
    'its production run, state open/complete/failed, the promotion id, and a JSON '
    'record of what ran (supervisor step 7, ruling R7).';
COMMENT ON COLUMN loop_dates.promotion IS
    'The promotions row for the date''s run; NULL when promotion was refused '
    '(record.promotion says why) or the date is not complete.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT, UPDATE ON loop_dates TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
