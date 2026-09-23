--------------------------------------------------------------------------------------------------------------------------
-- 20260923-05-sources-child-table-functions.sql
--
-- The `sources_<yyyymmdd>_<sca>` child tables, made and finished exactly as
-- `dev`'s pipeline/loadPSFCatIntoDBSourcesTable.py makes and finishes them,
-- as two functions the rebuild's `load` stage calls through its service login.
--
-- Why functions rather than the loader's own SQL: `dev` runs its DDL as a login
-- holding `rapidporole`, which owns `sources` ("Sources table must be owned by
-- rapidporole for inheritance", rapidOpsSourcesTable.sql) -- `INHERIT sources`
-- needs ownership of the parent, and `CLUSTER` ownership of the child. The
-- rebuild's service login `rapid_rebuild_pipeline` holds table grants only
-- (20260923-01/-03), and making it a member of `rapidporole` would be a
-- cluster-wide grant, reaching every database the cluster serves. Instead
-- these two functions run as their owner, `rapidporole` (SECURITY DEFINER), take
-- only a date and an SCA, build the table name themselves, and run `dev`'s
-- statements unchanged. EXECUTE is granted to the service login in
-- 20260923-06, guarded on the role.
--
-- `create_sources_child_table(obs_date, sca)`: `dev`'s creation block (L788-797)
-- and its index block (L890-899) together, since in `dev` the index block runs
-- for exactly the tables the same run created. Returns true when it made the
-- table, false when it already existed. A transaction-scoped advisory lock on
-- the table name serialises two loads racing to make the same table; the
-- second sees the first's table once the first commits. Differences from
-- `dev`, both forced:
--   - `SET default_tablespace = pipeline_data_01` / `pipeline_indx_01` are
--     omitted, as the baseline omits them (20260921-01's header: no
--     tablespace exists in a database this stream builds).
--   - The grants block (`dev` L931-936, run in `dev` after loading) is run
--     here at creation, adding the rebuild's two roles when they exist
--     (`rapid_rebuild_pipeline`: the rows it loads, `rapid_read`: SELECT, as
--     20260923-01 grants them everywhere else). Default privileges do not
--     reach a table `rapidporole` creates, so the child needs its own grants.
--
-- `cluster_sources_child_table(obs_date, sca)`: `dev`'s CLUSTER and ANALYZE
-- (L928-929). `dev` runs them once per processing date after every load; the
-- rebuild's `load` runs per detector image, so the stage calls this only when
-- its `[child_tables] cluster_and_analyze` setting is on (off by default).
--------------------------------------------------------------------------------------------------------------------------

CREATE FUNCTION sources_child_table_name(obs_date_ text, sca_ integer)
    RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF obs_date_ IS NULL OR obs_date_ !~ '^[0-9]{8}$' THEN
        RAISE EXCEPTION 'sources child table: observation date must be yyyymmdd, got %', obs_date_;
    END IF;
    IF sca_ IS NULL OR sca_ < 1 OR sca_ > 99 THEN
        RAISE EXCEPTION 'sources child table: sca must be 1..99, got %', sca_;
    END IF;
    RETURN format('sources_%s_%s', obs_date_, sca_);
END
$$;

CREATE FUNCTION create_sources_child_table(obs_date_ text, sca_ integer)
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

    EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidreadrole', t);
    EXECUTE format('GRANT SELECT ON TABLE %I TO GROUP rapidreadrole', t);
    EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidadminrole', t);
    EXECUTE format('GRANT ALL ON TABLE %I TO GROUP rapidadminrole', t);
    EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidporole', t);
    EXECUTE format('GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE %I TO rapidporole', t);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO rapid_rebuild_pipeline', t);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        EXECUTE format('GRANT SELECT ON TABLE %I TO rapid_read', t);
    END IF;
    RETURN true;
END
$$;

CREATE FUNCTION cluster_sources_child_table(obs_date_ text, sca_ integer)
    RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp AS $$
DECLARE
    t text := sources_child_table_name(obs_date_, sca_);
BEGIN
    IF to_regclass('public.' || t) IS NULL THEN
        RAISE EXCEPTION 'sources child table % does not exist', t;
    END IF;
    EXECUTE format('CLUSTER %I USING %I', t, t || '_radec_idx');
    EXECUTE format('ANALYZE %I', t);
END
$$;

ALTER FUNCTION create_sources_child_table(text, integer) OWNER TO rapidporole;
ALTER FUNCTION cluster_sources_child_table(text, integer) OWNER TO rapidporole;
REVOKE ALL ON FUNCTION create_sources_child_table(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION cluster_sources_child_table(text, integer) FROM PUBLIC;
