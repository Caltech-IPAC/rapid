-- A run can be stopped (`rapidpipe run stop`; rapid_docs tool page): three
-- nullable columns on runs, when, by whom and why, null on every run until
-- it is stopped. schema.sql carries them for a fresh database; this file
-- brings an existing one to it, applied once, in one transaction, as
-- postgres, after 2026-11-20-exposure-ids.sql. It adds columns only, which
-- the pipeline's roles reach through the grants they hold on runs. No row is
-- changed: every existing run stays open. A second application fails at the
-- first statement (the column exists) and changes nothing.

BEGIN;

ALTER TABLE runs
    ADD COLUMN stopped timestamp with time zone,
    ADD COLUMN stopped_by text,
    ADD COLUMN stopped_why text,
    ADD CHECK ((stopped IS NULL) = (stopped_by IS NULL) AND (stopped IS NULL) = (stopped_why IS NULL));

COMMIT;
