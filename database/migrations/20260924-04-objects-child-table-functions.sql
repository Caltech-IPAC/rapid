--------------------------------------------------------------------------------------------------------------------------
-- 20260924-04-objects-child-table-functions.sql
--
-- The per-field tables `astroobjects_<field>`, `merges_<field>` and
-- `astroobjectsmeta_<field>`, made and finished as `dev`'s
-- pipeline/crossMatchSources.py and pipeline/computeStatisticsForAstroObjects.py
-- make and finish them, as three functions the rebuild's `crossmatch` and
-- `statistics` stages call through their service login. The field is the
-- Roman tessellation tile id (rtid), `dev`'s "field".
--
-- Why functions: as for the `sources` children (20260923-05), `dev` runs this
-- DDL as a login holding `rapidporole`, and CLUSTER needs ownership of the
-- table. The rebuild's service login `rapid_rebuild_pipeline` holds table
-- grants only, so these functions run as their owner, `rapidporole` (SECURITY
-- DEFINER), take only a field number, build the table names themselves with
-- format('%I'), and run `dev`'s statements. EXECUTE is granted to the service
-- login in 20260924-06, guarded on the role.
--
-- `create_field_object_tables(field)`: crossMatchSources.py main()'s creation
-- block and its index-and-grant block, which in `dev` run for exactly the
-- fields the same invocation made, so here they run together. Makes whichever
-- of the two tables is absent; returns true when it made either. A
-- transaction-scoped advisory lock on the field serialises two crossmatch
-- attempts racing to make the same field's tables.
--
-- `create_astroobjectsmeta_child_table(field)`:
-- computeStatisticsForAstroObjects.py main()'s creation block (with its
-- `fillfactor = 70`) and its index block. `dev` drops and recreates this table
-- on every run; the rebuild never drops it: each statistics attempt appends
-- one new `statistics-set` under its own result-set id (step 1 ruling R7).
-- `dev` CLUSTERs it on the position index after loading; the rebuild does not,
-- since the table is appended to rather than rebuilt.
--
-- `cluster_field_object_tables(field)`: crossMatchSources.py main()'s CLUSTER
-- of `astroobjects_<field>` on its position index and ANALYZE of both tables,
-- which `dev` runs between crossmatch's two passes (step 1 ruling R5: it stays
-- inside the `crossmatch` stage).
--
-- Constraints. `CREATE TABLE ... (LIKE p INCLUDING DEFAULTS INCLUDING
-- CONSTRAINTS)` copies NOT NULL constraints (always copied by LIKE), column
-- defaults and CHECK constraints -- here 20260924-03's `*_run_columns_together`.
-- It does not copy PRIMARY KEY, UNIQUE or EXCLUDE constraints (only INCLUDING
-- INDEXES does) and never copies a foreign key (PostgreSQL 18 documentation,
-- CREATE TABLE, "LIKE"). So the prototypes' `astroobjects_pkey` and
-- `astroobjectsmeta_pkey` (PRIMARY KEY (aid)) never reached `dev`'s per-field
-- tables either, which is why `dev` makes `astroobjects_<field>_aid_idx`
-- itself (baseline comment on the prototypes). The rebuild adds set-scoped
-- uniqueness instead (step 1 ruling R4; products page, "Keys are set-scoped"):
-- `UNIQUE (result_set, aid)` on `astroobjects_<field>` and
-- `astroobjectsmeta_<field>`, `UNIQUE (result_set, aid, sid)` on
-- `merges_<field>`. Two scratch runs over the same inputs make the same aids
-- under different sets, so a table-wide key on `aid` would refuse the second.
-- Rows with `result_set IS NULL` (pre-run-model) are not constrained: UNIQUE
-- treats NULLs as distinct. The stages load through a temporary table and
-- `INSERT ... ON CONFLICT DO NOTHING` (rapidpipe/db/objects.py), which folds
-- `dev`'s pruneRedundantMerges (delete duplicate (aid, sid) merges) into the
-- load.
--
-- Differences from `dev`, each forced or ruled, as in 20260923-05:
--   - `SET default_tablespace = pipeline_data_01` / `pipeline_indx_01` are
--     omitted, as the baseline omits them (no tablespace exists in a database
--     this stream builds).
--   - `dev`'s `REVOKE ALL ... FROM rapidporole` and its re-grant of
--     INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES are omitted:
--     `rapidporole` owns the tables, and from PostgreSQL 17 the pair strips
--     the owner's MAINTAIN privilege, after which CLUSTER is refused
--     (measured for the `sources` children in CI on PostgreSQL 18).
--   - `rapid_rebuild_pipeline` (the rows it writes) and `rapid_read` (SELECT)
--     are granted where those roles exist, as 20260923-05 does. Default
--     privileges do not reach a table `rapidporole` creates.
--   - b-tree indexes on `run` and `result_set`, named `<t>_run_idx` and
--     `<t>_result_set_idx` as 20260923-09 names them on the `sources`
--     children: run deletion reads rows by `run`, the next stage reads a set
--     by `result_set`.
--   - The set-scoped UNIQUE constraints above, named `<t>_set_aid_key` and
--     `<t>_set_aid_sid_key`.
--
-- Adopting an existing table. A per-field table `dev` made before
-- 20260924-03 (production `rapid` has them) got nothing from that migration's
-- ALTER of the prototype, because a LIKE copy is not an inheritance child. When
-- `create_field_object_tables` or `create_astroobjectsmeta_child_table` finds
-- such a table (it exists, but has no `run` column), it attaches the run model
-- in place and returns false, since it did not make the table. It adds the
-- three columns, the all-or-none CHECK, the set-scoped UNIQUE, the
-- `run`/`result_set` indexes and the rebuild's grants. `dev`'s rows keep
-- `run IS NULL` (pre-run-model, in no result set). A new table gets the same
-- additions from the same helper, `attach_object_run_model`, so the two paths
-- cannot drift. That helper is callable only by its owner, `rapidporole`, which
-- is who the SECURITY DEFINER functions run as.
--------------------------------------------------------------------------------------------------------------------------

CREATE FUNCTION object_field_table_name(prefix_ text, field_ integer)
    RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF prefix_ IS NULL OR prefix_ NOT IN ('astroobjects', 'merges', 'astroobjectsmeta') THEN
        RAISE EXCEPTION 'per-field table: prefix must be astroobjects, merges or astroobjectsmeta, got %', prefix_;
    END IF;
    IF field_ IS NULL OR field_ < 0 THEN
        RAISE EXCEPTION 'per-field table: field must be a non-negative integer, got %', field_;
    END IF;
    RETURN format('%s_%s', prefix_, field_);
END
$$;

-- The rebuild's additions to one per-field table, idempotent: run columns and
-- their CHECK when absent (an adopted `dev` table), the set-scoped UNIQUE, the
-- `run`/`result_set` indexes, the rebuild's grants.
CREATE FUNCTION attach_object_run_model(prefix_ text, field_ integer)
    RETURNS void
    LANGUAGE plpgsql
    SET search_path = public, pg_temp AS $$
DECLARE
    t text := object_field_table_name(prefix_, field_);
    rel regclass := to_regclass('public.' || object_field_table_name(prefix_, field_));
    key_name text := t || CASE WHEN prefix_ = 'merges' THEN '_set_aid_sid_key' ELSE '_set_aid_key' END;
    key_cols text := CASE WHEN prefix_ = 'merges' THEN 'result_set, aid, sid' ELSE 'result_set, aid' END;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = rel AND attname = 'run' AND NOT attisdropped) THEN
        EXECUTE format('ALTER TABLE %I ADD COLUMN run rapid_ulid, ADD COLUMN attempt rapid_ulid, '
                       'ADD COLUMN result_set rapid_ulid', t);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = rel AND conname = prefix_ || '_run_columns_together') THEN
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK ((run IS NULL) = (attempt IS NULL) '
                       'AND (run IS NULL) = (result_set IS NULL))', t, prefix_ || '_run_columns_together');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = rel AND conname = key_name) THEN
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I UNIQUE (%s)', t, key_name, key_cols);
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (result_set)', t || '_result_set_idx', t);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (run)', t || '_run_idx', t);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO rapid_rebuild_pipeline', t);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        EXECUTE format('GRANT SELECT ON TABLE %I TO rapid_read', t);
    END IF;
END
$$;

CREATE FUNCTION create_field_object_tables(field_ integer)
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
        EXECUTE format('ALTER TABLE %I SET UNLOGGED', a);

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
        EXECUTE format('ALTER TABLE %I SET UNLOGGED', m);

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

CREATE FUNCTION create_astroobjectsmeta_child_table(field_ integer)
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
        EXECUTE format('ALTER TABLE %I SET UNLOGGED', t);

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

CREATE FUNCTION cluster_field_object_tables(field_ integer)
    RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public, pg_temp AS $$
DECLARE
    a text := object_field_table_name('astroobjects', field_);
    m text := object_field_table_name('merges', field_);
BEGIN
    IF to_regclass('public.' || a) IS NULL OR to_regclass('public.' || m) IS NULL THEN
        RAISE EXCEPTION 'per-field tables % and % must both exist', a, m;
    END IF;
    EXECUTE format('CLUSTER %I USING %I', a, a || '_radec_idx');
    EXECUTE format('ANALYZE %I', a);
    EXECUTE format('ANALYZE %I', m);
END
$$;

ALTER FUNCTION attach_object_run_model(text, integer) OWNER TO rapidporole;
ALTER FUNCTION create_field_object_tables(integer) OWNER TO rapidporole;
ALTER FUNCTION create_astroobjectsmeta_child_table(integer) OWNER TO rapidporole;
ALTER FUNCTION cluster_field_object_tables(integer) OWNER TO rapidporole;
REVOKE ALL ON FUNCTION attach_object_run_model(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION create_field_object_tables(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION create_astroobjectsmeta_child_table(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION cluster_field_object_tables(integer) FROM PUBLIC;
