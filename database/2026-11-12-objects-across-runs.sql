-- objects and xobjects matched across runs (objects-across-runs): a
-- difference job matches its sources against every run's objects of its
-- dataset in its fields, not its own run's alone, since surveys overlap and an
-- object's members may come from runs of more than one survey. The run-scoped
-- readers objects_in and xobjects_in of (fields, run) and field_run_rows,
-- which 2026-11-10-run-reads.sql added, give way to readers of (fields,
-- dataset) over field_dataset_rows, the dataset's filter inside each child's
-- query. schema.sql carries them for a fresh database; this file brings an
-- existing one to it, applied once, in one transaction, as postgres, after
-- 2026-11-12-objects-without-survey.sql and while no difference job is
-- running: a job of an older image calls the readers this file drops. The
-- publisher's field locks move from (run, field) to (hashtext(dataset),
-- field) in the image, with no database object. Their EXECUTE reaches the
-- pipeline's roles through PUBLIC, as the one-argument readers' does.

BEGIN;

DROP FUNCTION objects_in(integer[], bigint);
DROP FUNCTION xobjects_in(integer[], bigint);
DROP FUNCTION field_run_rows(anyelement, integer[], bigint);

CREATE FUNCTION field_dataset_rows(parent anyelement, fields integer[], dataset text)
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I WHERE dataset = $1', child)
            USING dataset;
    END LOOP;
END
$$;

CREATE FUNCTION objects_in(fields integer[], dataset text) RETURNS SETOF objects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_dataset_rows(NULL::objects, fields, dataset) $$;
CREATE FUNCTION xobjects_in(fields integer[], dataset text) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_dataset_rows(NULL::xobjects, fields, dataset) $$;

COMMIT;
