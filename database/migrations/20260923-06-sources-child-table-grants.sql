--------------------------------------------------------------------------------------------------------------------------
-- 20260923-06-sources-child-table-grants.sql
--
-- EXECUTE on 20260923-05's two child-table functions for the rebuild's service
-- login `rapid_rebuild_pipeline`, whose `load` stage calls them. Guarded on the
-- role's existence, as 20260923-01 and -03 are, so CI (a fresh PostgreSQL with
-- no such role) applies it as a no-op. Additive only.
--------------------------------------------------------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT EXECUTE ON FUNCTION create_sources_child_table(text, integer) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION cluster_sources_child_table(text, integer) TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
