-- Run-scoped readers of the field children (run-scoped-reads): objects_in and
-- xobjects_in of (fields, run), the run's filter inside each child's query.
-- The one-argument readers return every run's rows of a field, which
-- field_rows holds until the caller reads them; a field read by six runs'
-- differences spilled to temp files on every association read, and run 228's
-- prompt window stalled at 543 concurrent reads waiting on BuffileWrite
-- (throughput-record, 2026-11-10). schema.sql carries them for a fresh
-- database; this file brings an existing one to it, applied once, in one
-- transaction, as postgres. It only adds functions; their EXECUTE reaches the
-- pipeline's roles through PUBLIC, as the one-argument readers' does.

BEGIN;

CREATE FUNCTION field_run_rows(parent anyelement, fields integer[], run bigint)
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I'
                                    ' WHERE job IN (SELECT id FROM public.jobs WHERE run = $1)', child)
            USING run;
    END LOOP;
END
$$;

CREATE FUNCTION objects_in(fields integer[], run bigint) RETURNS SETOF objects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_run_rows(NULL::objects, fields, run) $$;
CREATE FUNCTION xobjects_in(fields integer[], run bigint) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_run_rows(NULL::xobjects, fields, run) $$;

COMMIT;
