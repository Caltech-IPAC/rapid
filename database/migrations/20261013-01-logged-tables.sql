--------------------------------------------------------------------------------------------------------------------------
-- 20261013-01-logged-tables.sql
--
-- Ben, 2026-10-13: every table is LOGGED. On 2026-10-02 rapid-db's
-- postgres was stopped uncleanly; crash recovery reset every UNLOGGED
-- relation to empty, so the sources, merges and astroobjects child tables
-- came back with no rows while their result sets still read complete, and
-- re-running load was a silent no-op. The rows are not cheap to regenerate,
-- so the WAL cost of logging them is accepted.
--
-- Two parts. First, every UNLOGGED table in `public` becomes LOGGED: on a
-- fresh database that is `sources` and `xsources` (the baseline's two
-- `SET UNLOGGED` lines); on an older one it also catches every child table
-- the functions below already made. Their indexes follow their tables.
-- Second, the three child-table functions are replaced without their
-- `SET UNLOGGED` line, bodies otherwise byte-identical to their latest
-- definitions (create_sources_child_table from 20260923-09, the two object
-- functions from 20260924-04). CREATE OR REPLACE keeps owner, grants and
-- SECURITY DEFINER; the ALTER/REVOKE lines restate them as those files do.
-- Applied migrations are never edited (README), hence a new file.
--------------------------------------------------------------------------------------------------------------------------

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relpersistence = 'u'
             ORDER BY c.relname
    LOOP
        EXECUTE format('ALTER TABLE public.%I SET LOGGED', r.relname);
    END LOOP;
END
$$;

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

CREATE OR REPLACE FUNCTION create_field_object_tables(field_ integer)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp AS $$
DECLARE
    a text := object_field_table_name('astroobjects', field_);
    m text := object_field_table_name('merges', field_);
    made boolean := false;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('rapid.object-field-tables.' || field_));

    IF to_regclass('public.' || a) IS NULL THEN
        EXECUTE format('CREATE TABLE %I (LIKE astroobjects INCLUDING DEFAULTS INCLUDING CONSTRAINTS)', a);
        EXECUTE format('ALTER TABLE %I OWNER TO rapidporole', a);

        EXECUTE format('CREATE INDEX %I ON %I (aid)', a || '_aid_idx', a);
        EXECUTE format('CREATE INDEX %I ON %I (q3c_ang2ipix(ra0, dec0))', a || '_radec_idx', a);

        EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidreadrole', a);
        EXECUTE format('GRANT SELECT ON TABLE %I TO GROUP rapidreadrole', a);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidadminrole', a);
        EXECUTE format('GRANT ALL ON TABLE %I TO GROUP rapidadminrole', a);
        made := true;
    END IF;
    PERFORM attach_object_run_model('astroobjects', field_);

    IF to_regclass('public.' || m) IS NULL THEN
        EXECUTE format('CREATE TABLE %I (LIKE merges INCLUDING DEFAULTS INCLUDING CONSTRAINTS)', m);
        EXECUTE format('ALTER TABLE %I OWNER TO rapidporole', m);

        EXECUTE format('CREATE INDEX %I ON %I USING btree (aid)', m || '_aid_idx', m);
        EXECUTE format('CREATE INDEX %I ON %I USING btree (sid)', m || '_sid_idx', m);

        EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidreadrole', m);
        EXECUTE format('GRANT SELECT ON TABLE %I TO GROUP rapidreadrole', m);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidadminrole', m);
        EXECUTE format('GRANT ALL ON TABLE %I TO GROUP rapidadminrole', m);
        made := true;
    END IF;
    PERFORM attach_object_run_model('merges', field_);

    RETURN made;
END
$$;

CREATE OR REPLACE FUNCTION create_astroobjectsmeta_child_table(field_ integer)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp AS $$
DECLARE
    t text := object_field_table_name('astroobjectsmeta', field_);
    made boolean := false;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('rapid.astroobjectsmeta-table.' || t));

    IF to_regclass('public.' || t) IS NULL THEN
        EXECUTE format('CREATE TABLE %I (LIKE astroobjectsmeta INCLUDING DEFAULTS INCLUDING CONSTRAINTS) WITH (fillfactor = 70)', t);
        EXECUTE format('ALTER TABLE %I OWNER TO rapidporole', t);

        EXECUTE format('CREATE INDEX %I ON %I (nsources)', t || '_nsources_idx', t);
        EXECUTE format('CREATE INDEX %I ON %I (q3c_ang2ipix(meanra, meandec))', t || '_meanradec_idx', t);

        EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidreadrole', t);
        EXECUTE format('GRANT SELECT ON TABLE %I TO GROUP rapidreadrole', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM rapidadminrole', t);
        EXECUTE format('GRANT ALL ON TABLE %I TO GROUP rapidadminrole', t);
        made := true;
    END IF;
    PERFORM attach_object_run_model('astroobjectsmeta', field_);

    RETURN made;
END
$$;

ALTER FUNCTION create_sources_child_table(text, integer) OWNER TO rapidporole;
ALTER FUNCTION create_field_object_tables(integer) OWNER TO rapidporole;
ALTER FUNCTION create_astroobjectsmeta_child_table(integer) OWNER TO rapidporole;
REVOKE ALL ON FUNCTION create_sources_child_table(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION create_field_object_tables(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION create_astroobjectsmeta_child_table(integer) FROM PUBLIC;
