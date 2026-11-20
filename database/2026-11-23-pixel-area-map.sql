-- The pixel area map's CRDS name on l2files (not in dev): the name of the
-- area reference intake finds for a flight file, which the packet stage
-- reads to correct fluxes for pixel area. Nullable with no default, so the
-- table is not rewritten; existing rows stay null (no backfill), and
-- intake writes it from this file on. The l2files_official view on an
-- existing database keeps the columns it was made with and does not show
-- this one; the pipeline reads the column from l2files itself. schema.sql
-- carries the column for a fresh database; this file brings an existing
-- one to it, applied once, in one transaction, as postgres. A second
-- application fails at the statement (the column exists) and changes
-- nothing.

BEGIN;

ALTER TABLE l2files
    ADD COLUMN pixel_area_map text;

COMMIT;
