--------------------------------------------------------------------------------------------------------------------------
-- 20260926-01-loop-batches.sql
--
-- Delivery discovery and batches in the processing-date loop (supervisor step 4 of the
-- operations campaign, rulings R4-R6, 2026-09-26). A schedule whose spec names an
-- `inbox` discovers deliveries there (`<inbox>/<YYYY-MM-DD>/<name>/manifest.json`) and
-- forms, per processing date, one batch per firing that found new deliveries for it:
-- `loop_dates` gains `batch` (1, 2, ... per (schedule, processing_date); every existing
-- row keeps batch 1) and `kind` ('batch' today; 'switch' is reserved for the chain
-- switch on operations.md), and its primary key becomes (schedule, processing_date,
-- batch). `loop_deliveries` records every delivery a schedule has classified, one row
-- per location (identity by location: a recorded location is never read again): its
-- identity (the l2-image entry's exposure, detector, version), the primary member's
-- sha256, the delivery's instance and unit, and its state: batched (with the batch it
-- joined), refused (an identical re-delivery), quarantined (malformed, or a checksum
-- conflict) or deferred (a corrected version awaiting a correction run). Additive: no
-- column or table a live release reads is dropped or renamed (the primary key is
-- widened, not removed). Grants to `rapid_rebuild_pipeline` are guarded on the role's
-- existence, as 20260924-11 is, so CI (no such role) applies them as a no-op.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE loop_dates
    ADD COLUMN batch integer NOT NULL DEFAULT 1,
    ADD COLUMN kind text NOT NULL DEFAULT 'batch' CHECK (kind IN ('batch', 'switch'));

ALTER TABLE loop_dates DROP CONSTRAINT loop_dates_pkey;
ALTER TABLE loop_dates ADD PRIMARY KEY (schedule, processing_date, batch);

COMMENT ON COLUMN loop_dates.batch IS
    'The batch of the processing date: 1 for the first firing that found deliveries for '
    'the date (and for a spec-listed date), then 1 + the highest batch of the date.';
COMMENT ON COLUMN loop_dates.kind IS
    'batch (a run over newly discovered or spec-listed deliveries); switch is reserved '
    'for the chain switch (operations.md).';

CREATE TABLE loop_deliveries (
    schedule          text NOT NULL,
    location          text NOT NULL,
    processing_date   date NOT NULL,
    exposure          text,
    detector          text,
    version           text,
    checksum          text,
    delivery_instance text,
    unit              text,
    state             text NOT NULL
                      CHECK (state IN ('batched', 'refused', 'quarantined', 'deferred')),
    reason            text,
    batch             integer,
    discovered_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule, location)
);

CREATE INDEX loop_deliveries_identity_idx
    ON loop_deliveries (schedule, exposure, detector, version);

COMMENT ON TABLE loop_deliveries IS
    'One row per delivery a scheduled loop discovered in its inbox (location = the '
    'delivery prefix admit reads), classified batched, refused, quarantined or deferred '
    '(supervisor step 4, rulings R3-R4, 2026-09-26).';
COMMENT ON COLUMN loop_deliveries.checksum IS
    'The sha256 of the delivery manifest''s primary l2-image member, as the manifest '
    'states it; NULL when the manifest is malformed.';
COMMENT ON COLUMN loop_deliveries.batch IS
    'The loop_dates batch of (schedule, processing_date) the delivery joined; NULL unless '
    'state is batched.';
COMMENT ON COLUMN loop_deliveries.reason IS
    'Why a delivery was not batched: identical re-delivery, checksum conflict, '
    'malformed, or corrected delivery awaits a correction run.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT, UPDATE ON loop_deliveries TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
