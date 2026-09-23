--------------------------------------------------------------------------------------------------------------------------
-- 20260923-03-rebuild-pipeline-delete.sql
--
-- DELETE for `rapid_rebuild_pipeline` on the public tables, completing 20260923-01. The
-- launcher's `run delete` (rapidpipe.runs) and the db test suite's per-test cleanup both
-- delete rows as the service login; measured 2026-09-22 on the trial database: 2 of 66
-- tests/db failed with "permission denied for table l2filemeta" on cleanup. Guarded on the
-- role's existence for the same reason as 20260923-01. Additive only.
--------------------------------------------------------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT DELETE ON ALL TABLES IN SCHEMA public TO rapid_rebuild_pipeline;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT DELETE ON TABLES TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grant skipped';
    END IF;
END
$$;
