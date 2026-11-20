-- The serial exposure id (rapid_docs exposure-id; rapid 622) brought to an
-- existing database, which keeps its rows (Ben, 2026-11-20, replacing the
-- 2026-11-14 ruling that nothing migrates). schema.sql carries the table for
-- a fresh database; this file brings an existing one to it, applied once, in
-- one transaction, as postgres, after 2026-11-17-survey-programs.sql and
-- 2026-11-18-refimmeta.sql. It adds a table, which reaches the pipeline's
-- roles through the schema's default privileges.
--
-- Every exposure already stored keeps the id it has: each name an intake job
-- took in (its scope's `file`, read as rapidpipe.foundation.loop.stem_ids
-- reads it) is recorded under its dataset with the expid of the l2files rows
-- that job wrote. Those packed ids are unique within a dataset and not across
-- datasets, so the key is (dataset, expid) and not expid alone. The counter
-- then starts above the largest expid any table holds, so a new id never
-- equals an id already stored anywhere. An imported row has no name and is
-- not recorded; nothing reads it by name.
--
-- It refuses, naming the rows, and changes nothing when a dataset holds one
-- name under two ids or one id under two names, when an intake's file is a
-- name no reader gives, or when no integer is left above the largest id.

BEGIN;

CREATE TABLE exposure_ids (
    expid integer GENERATED ALWAYS AS IDENTITY,
    dataset text NOT NULL,
    name text NOT NULL,                              -- the producer's name for the exposure (loop.stem_ids)
    created timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (dataset, expid),
    UNIQUE (dataset, name)
);

CREATE TEMPORARY TABLE stored_names ON COMMIT DROP AS
SELECT DISTINCT l.dataset, l.expid, j.scope ->> 'file' AS file,
       CASE
           WHEN j.scope ->> 'file' ~ '^r\d{19}_\d{4}_wfi\d{2}_f\d{3}$'
               THEN substring(j.scope ->> 'file' FROM '^(r\d{19}_\d{4})_')
           WHEN j.scope ->> 'file' ~ '^roman_(tds|was)_[A-Z]\d{3}_\d+_wfi\d{2}$'
               THEN substring(j.scope ->> 'file' FROM '^(roman_(?:tds|was)_[A-Z]\d{3}_\d+)_wfi')
           WHEN j.scope ->> 'file' ~ '^rimtimsim_WFI_F\d{3}_SCA\d{2}_\d{9}_lite$'
               THEN regexp_replace(j.scope ->> 'file', '^rimtimsim_WFI_(F\d{3})_SCA\d{2}_(\d{9})_lite$',
                                   'rimtimsim_WFI_\1_\2')
       END AS name
FROM l2files l JOIN jobs j ON j.id = l.job
WHERE j.stage = 'intake' AND j.scope ? 'file';

DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(format('%s %s', dataset, file), ', ') INTO bad FROM stored_names WHERE name IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'exposure_ids: intake files no reader names: %', bad;
    END IF;
    SELECT string_agg(format('%s %s: expids %s', dataset, name, ids), '; ') INTO bad FROM (
        SELECT dataset, name, string_agg(DISTINCT expid::text, ',') AS ids
        FROM stored_names GROUP BY dataset, name HAVING count(DISTINCT expid) > 1) t;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'exposure_ids: one name stored under two ids: %', bad;
    END IF;
    SELECT string_agg(format('%s %s: names %s', dataset, expid, names), '; ') INTO bad FROM (
        SELECT dataset, expid, string_agg(DISTINCT name, ',') AS names
        FROM stored_names GROUP BY dataset, expid HAVING count(DISTINCT name) > 1) t;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'exposure_ids: one id stored under two names: %', bad;
    END IF;
END $$;

INSERT INTO exposure_ids (expid, dataset, name) OVERRIDING SYSTEM VALUE
SELECT DISTINCT expid, dataset, name FROM stored_names;

-- The largest expid of every table that has the column (an inheritance
-- parent's read covers its children), and the counter one above it.
DO $$
DECLARE
    t record;
    hi bigint := 0;
    m bigint;
BEGIN
    FOR t IN SELECT c.table_name FROM information_schema.columns c
             JOIN information_schema.tables b ON b.table_schema = c.table_schema AND b.table_name = c.table_name
             WHERE c.table_schema = 'public' AND c.column_name = 'expid' AND b.table_type = 'BASE TABLE'
               AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = format('public.%I', c.table_name)::regclass)
    LOOP
        EXECUTE format('SELECT max(expid) FROM %I', t.table_name) INTO m;
        hi := greatest(hi, coalesce(m, 0));
    END LOOP;
    IF hi + 1 > 2147483647 THEN
        RAISE EXCEPTION 'exposure_ids: the largest stored expid is %, which leaves no integer for a new one', hi;
    END IF;
    EXECUTE format('ALTER TABLE exposure_ids ALTER COLUMN expid RESTART WITH %s', hi + 1);
    RAISE NOTICE 'exposure_ids: % names recorded, new ids start at %', (SELECT count(*) FROM exposure_ids), hi + 1;
END $$;

COMMIT;
