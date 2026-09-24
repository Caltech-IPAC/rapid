--------------------------------------------------------------------------------------------------------------------------
-- 20260924-03-alert-outbox.sql
--
-- The alert outbox: one row per alert the `alerts` stage wrote into an Avro
-- object container (products page: "`alerts` writes one record per alert
-- (name, candidate id, first-seen time, position) into the alert outbox
-- alongside the container's byte range"; runs page, "Storage layout": "the
-- outbox row carries the container's location and each alert's byte range").
-- Shape ruled by the lead, 2026-09-24. `dev` writes no alert rows at all
-- (its alerts/ package only SELECTs); this table is new, not an ALTER of a
-- `dev` table, and nothing of `dev`'s is renamed or dropped.
--
-- Columns:
--   - `run`, `attempt`: the alerts attempt that wrote the row.
--   - `instance`: the `alert-container` product instance the alert is in; the
--     container's location is that instance's `primary_location` under its
--     producing attempt's `output_location`.
--   - `result_set`: the `alert-set` result set these rows are (its
--     `result_sets.row_count` is the number of rows).
--   - `alert_name`: NULL. `dev`'s `alertnames`/`computeAlertName` are unused
--     by its alert code; the naming scheme is the lead's to decide.
--   - `candidate`: the triggering `sources.sid`; `object`: its `aid`.
--   - `pid`: the difference image's `diffimages.pid`.
--   - `first_seen_mjd`: the alert's `diaObject.firstDiaSourceMjd`, `dev`'s
--     "first seen" (the earliest of the trigger and its previous detections).
--   - `ra`, `dec`: the trigger's position, with `alertnames`' CHECKs.
--   - `record_index`: the alert's 0-based position in the container.
--   - `block_offset`, `block_length`: the byte range of the Avro block that
--     holds exactly this record. The stage flushes one block per record, so
--     the range is addressable (it decodes on its own given the container's
--     header); nullable for a writer that cannot address a record.
--   - `schema_version`: the Avro schema, e.g. '00.04'.
--   - `published_at`, `topic`, `publication_ref`: NULL until something
--     publishes the row. Publication is designed in and off.
--
-- No FKs to `sources` or `diffimages`: `sources` is an inheritance parent
-- whose rows live in child tables (an FK to it would never match), and the
-- run-model FKs already pin the row to its producing attempt.
--------------------------------------------------------------------------------------------------------------------------

CREATE TABLE alert_outbox (
    id rapid_ulid PRIMARY KEY,
    run rapid_ulid NOT NULL REFERENCES runs (id),
    attempt rapid_ulid NOT NULL REFERENCES attempts (id),
    instance rapid_ulid NOT NULL REFERENCES product_instances (id),
    result_set rapid_ulid NOT NULL REFERENCES result_sets (instance),
    alert_name text NULL,
    candidate bigint NOT NULL,
    object bigint NULL,
    pid integer NOT NULL,
    first_seen_mjd double precision,
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    record_index integer NOT NULL,
    block_offset bigint NULL,
    block_length bigint NULL,
    schema_version text NOT NULL,
    written_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz NULL,
    topic text NULL,
    publication_ref text NULL,
    CONSTRAINT alert_outbox_ra_check CHECK ((ra >= 0.0) AND (ra < 360.0)),
    CONSTRAINT alert_outbox_dec_check CHECK ((dec >= -90.0) AND (dec <= 90.0)),
    CONSTRAINT alert_outbox_record_index_check CHECK (record_index >= 0),
    CONSTRAINT alert_outbox_block_range_check
        CHECK ((block_offset IS NULL) = (block_length IS NULL)
               AND (block_offset IS NULL OR (block_offset >= 0 AND block_length > 0))),
    CONSTRAINT alert_outbox_instance_candidate_key UNIQUE (instance, candidate)
);

CREATE INDEX alert_outbox_run_idx ON alert_outbox USING btree (run);
CREATE INDEX alert_outbox_instance_idx ON alert_outbox USING btree (instance);
CREATE INDEX alert_outbox_result_set_idx ON alert_outbox USING btree (result_set);
CREATE INDEX alert_outbox_object_idx ON alert_outbox USING btree (object);

COMMENT ON TABLE alert_outbox IS
    'One row per alert the alerts stage wrote into an Avro object container. '
    'Publication is designed in and off: the row is the outbox, and '
    'published_at/topic/publication_ref stay NULL until a publisher exists.';
COMMENT ON COLUMN alert_outbox.alert_name IS
    'NULL: the alert naming scheme is not decided (dev''s alertnames/computeAlertName are unused).';
COMMENT ON COLUMN alert_outbox.candidate IS 'The triggering sources.sid.';
COMMENT ON COLUMN alert_outbox.object IS 'The triggering source''s astroobjects aid in the association set.';
COMMENT ON COLUMN alert_outbox.record_index IS '0-based position of the alert in the container.';
COMMENT ON COLUMN alert_outbox.block_offset IS
    'Byte offset of the Avro block holding exactly this record; with block_length, readable '
    'on its own given the container header.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT, UPDATE ON alert_outbox TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT SELECT ON alert_outbox TO rapid_read;
    ELSE
        RAISE NOTICE 'role rapid_read does not exist here; grants skipped';
    END IF;
END
$$;
