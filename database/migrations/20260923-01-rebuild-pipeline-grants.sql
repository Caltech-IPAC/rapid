--------------------------------------------------------------------------------------------------------------------------
-- 20260923-01-rebuild-pipeline-grants.sql
--
-- Table grants for the rebuild pipeline's service login `rapid_rebuild_pipeline`
-- (created cluster-wide by rapid_systems db-migrations/147, NOLOGIN there; its
-- password comes from rapid-db-config, never from a migration). The rebuild's
-- `register` stage and the `rapidpipe run` launcher write the run-model tables
-- (20260921-02) and the l2/difference tables directly, so this role needs
-- table-level INSERT/SELECT/UPDATE here -- in the rebuild's own TRIAL database,
-- which this migration stream builds from empty (README). No DELETE: `run
-- delete` is an operator action, not the service login's.
--
-- Guarded on the role's existence so CI (which applies this stream to a fresh
-- PostgreSQL with no such role) applies it as a no-op, and a deployment that
-- creates the role later re-runs these grants by applying a later migration.
-- `rapid_read` (rapid_systems 010/082: the people-facing SELECT group) gets
-- SELECT everywhere for the same reason: operators verify a run's rows through
-- their own login. Additive only; nothing revoked, renamed or dropped.
--------------------------------------------------------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT USAGE ON SCHEMA public TO rapid_rebuild_pipeline;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO rapid_rebuild_pipeline;
        GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO rapid_rebuild_pipeline;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO rapid_rebuild_pipeline;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT USAGE ON SCHEMA public TO rapid_read;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO rapid_read;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO rapid_read;
    ELSE
        RAISE NOTICE 'role rapid_read does not exist here; grants skipped';
    END IF;
END
$$;
