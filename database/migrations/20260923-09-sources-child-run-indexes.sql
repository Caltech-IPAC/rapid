--------------------------------------------------------------------------------------------------------------------------
-- 20260923-09-sources-child-run-indexes.sql
--
-- Ben, 2026-09-23: `create_sources_child_table(obs_date, sca)` (20260923-05)
-- must also index its two new run-model columns (20260923-04's `run` and
-- `result_set`) on every child table it makes, one b-tree index per column,
-- in the naming pattern the function's other eight indexes already use.
-- No UNIQUE constraint: two scratch runs are allowed to coexist over the
-- same logical inputs by design, so a `result_set` (or `run`) value is not
-- unique within a child table. Crossmatch reads a result set by id
-- (`result_set`), and run deletion wants rows by run (`run`) -- both scans,
-- neither an equality lookup that a unique index would also have to serve.
--
-- Applied migrations are never edited (database/migrations/README.md), so
-- this replaces the function wholesale with CREATE OR REPLACE, keeping
-- 20260923-05's body byte-identical apart from the two new CREATE INDEX
-- statements. Owner, SECURITY DEFINER, search_path, grants and CLUSTER
-- behaviour are untouched; `cluster_sources_child_table` is not redefined.
--------------------------------------------------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION create_sources_child_table(obs_date_ text, sca_ integer)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp AS $$
DECLARE
    t text := sources_child_table_name(obs_date_, sca_);
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('rapid.sources-child-table.' || t));
    IF to_regclass('public.' || t) IS NOT NULL THEN
        RETURN false;
    END IF;

    EXECUTE format('CREATE TABLE %I (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS)', t);
    EXECUTE format('ALTER TABLE %I OWNER TO rapidporole', t);
    EXECUTE format('ALTER TABLE %I SET UNLOGGED', t);
    EXECUTE format('ALTER TABLE %I INHERIT sources', t);

    EXECUTE format('CREATE INDEX %I ON %I (pid)', t || '_pid_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (expid)', t || '_expid_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (sca)', t || '_sca_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (field)', t || '_field_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (flags)', t || '_flags_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (mjdobs)', t || '_mjdobs_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (sid)', t || '_sid_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (q3c_ang2ipix(ra, dec))', t || '_radec_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (result_set)', t || '_result_set_idx', t);
    EXECUTE format('CREATE INDEX %I ON %I (run)', t || '_run_idx', t);

    EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidreadrole', t);
    EXECUTE format('GRANT SELECT ON TABLE %I TO GROUP rapidreadrole', t);
    EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidadminrole', t);
    EXECUTE format('GRANT ALL ON TABLE %I TO GROUP rapidadminrole', t);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO rapid_rebuild_pipeline', t);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        EXECUTE format('GRANT SELECT ON TABLE %I TO rapid_read', t);
    END IF;
    RETURN true;
END
$$;

ALTER FUNCTION create_sources_child_table(text, integer) OWNER TO rapidporole;
REVOKE ALL ON FUNCTION create_sources_child_table(text, integer) FROM PUBLIC;
