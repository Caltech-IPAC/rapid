--------------------------------------------------------------------------------------------------------------------------
-- 20260924-05-objects-grants.sql
--
-- EXECUTE on 20260924-03's three per-field table functions for the rebuild's
-- service login `rapid_rebuild_pipeline`, whose `crossmatch` and `statistics`
-- stages call them. Guarded on the role's existence, as 20260923-06 is, so CI
-- (a fresh PostgreSQL with no such role) applies it as a no-op. Additive only.
--------------------------------------------------------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT EXECUTE ON FUNCTION create_field_object_tables(integer) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION create_astroobjectsmeta_child_table(integer) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION cluster_field_object_tables(integer) TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
