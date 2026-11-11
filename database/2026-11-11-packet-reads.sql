-- The packet stage's field reads with their filters inside each child's query
-- (packet-field-reads): packet_origin_rows, packet_membership_rows and
-- packet_reuse_rows. ORIGINS_SQL, MEMBERSHIPS_SQL and REUSE_SQL read every
-- row of the triggers' fields through <parent>_in(fields), whose RETURN QUERY
-- holds them until the caller filters, in temp files past work_mem; run 229's
-- packet jobs held up to 313 such reads at once (throughput-record-2,
-- 2026-11-11). Each reader applies its query's own filter inside the child's
-- query, so it returns the rows that query keeps, of every run, and holds
-- nothing else. schema.sql carries them for a fresh database; this file
-- brings an existing one to it, applied once, in one transaction, as
-- postgres. It only adds functions; their EXECUTE reaches the pipeline's
-- roles through PUBLIC, as field_rows' does.

BEGIN;

CREATE FUNCTION packet_origin_rows(parent anyelement, fields integer[], jobs bigint[],
                                   aids bigint[])
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    aid text := CASE WHEN name = 'xobjects' THEN 'xaid' ELSE 'aid' END;
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I'
                                    ' WHERE (job, %I) IN (SELECT * FROM unnest($1, $2))', child, aid)
            USING jobs, aids;
    END LOOP;
END
$$;

CREATE FUNCTION packet_membership_rows(parent anyelement, fields integer[], jobs bigint[],
                                       aids bigint[], own bigint)
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    aid text := CASE WHEN name = 'xmemberships' THEN 'xaid' ELSE 'aid' END;
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I m'
                                    ' WHERE (m.object_job, m.%I) IN (SELECT * FROM unnest($1, $2))'
                                    ' AND (m.job = $3 OR EXISTS (SELECT 1 FROM public.official o'
                                    ' WHERE o.closed_at IS NULL AND o.product = ''sources'''
                                    ' AND o.job = m.job))', child, aid)
            USING jobs, aids, own;
    END LOOP;
END
$$;

CREATE FUNCTION packet_reuse_rows(fields integer[], pids integer[])
RETURNS SETOF forced
LANGUAGE plpgsql STABLE AS $$
DECLARE
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name('forced', f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I f WHERE f.pid = ANY($1)'
                                    ' AND EXISTS (SELECT 1 FROM public.official o'
                                    ' WHERE o.closed_at IS NULL AND o.product = ''packets'''
                                    ' AND o.job = f.job)', child)
            USING pids;
    END LOOP;
END
$$;

COMMIT;
