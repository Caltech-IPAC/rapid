-- The Roman observation identifiers on exposures (port-schema-07): dev's
-- migration 20261015 (5ba96e38), its names and types, without its indexes
-- (nothing here queries by them). Nullable with no default, so the table
-- is not rewritten; existing rows stay null (no backfill), and intake
-- writes them from the converted header from this file on. schema.sql
-- carries them for a fresh database; this file brings an existing one to
-- it, applied once.
ALTER TABLE exposures
    ADD COLUMN execution_plan smallint,
    ADD COLUMN pass smallint,
    ADD COLUMN segment smallint,
    ADD COLUMN observation smallint,
    ADD COLUMN visit smallint,
    ADD COLUMN exposure smallint,
    ADD COLUMN visit_id character varying(19),
    ADD COLUMN observation_id character varying(28);
