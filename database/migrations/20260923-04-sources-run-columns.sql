--------------------------------------------------------------------------------------------------------------------------
-- 20260923-04-sources-run-columns.sql
--
-- Attaches the run model (20260921-02-run-model.sql) to the `dev` `sources`
-- table, which the rebuild's `load` stage writes. Authority: rapid_docs'
-- products page, "Database result sets": a `source-set` is a result set whose
-- rows live in `sources`, made by `load`, and "Every row carries the run id,
-- the attempt id that wrote it and its result-set id." The `dev` schema is
-- kept: nothing is renamed or dropped, and the loader's 28 columns keep their
-- meaning.
--
-- Choices, in the pattern of 20260921-03-difference-run-columns.sql:
--   - `run`, `attempt` and `result_set` are nullable: rows `dev`'s
--     loadPSFCatIntoDBSourcesTable.py writes carry none of them, and no
--     existing row is touched.
--   - A CHECK requires the three to be all NULL or all set: a row half
--     attached to the run model is not a state this schema allows.
--   - `sources` is the inheritance parent of the per-date, per-SCA child
--     tables (`sources_<yyyymmdd>_<sca>`, made at load time). ADD COLUMN and
--     ADD CONSTRAINT ... CHECK recurse to every existing child, so a child
--     made by `dev` before this migration gains the columns and the check
--     too; a child made afterwards gets both from `CREATE TABLE ... (LIKE
--     sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS)`, as `dev` makes them.
--   - The foreign keys sit on the parent only. Inheritance does not carry
--     foreign keys to children, and the parent holds no rows ("No records are
--     directly inserted into the parent table", rapidOpsSourcesTable.sql), so
--     they are declarations of meaning, exactly as `dev`'s own
--     `sources_pid_fk` already is.
--   - No index on the new columns: `dev` indexes each child table when it is
--     made (20260923-05's function reproduces that list), and the parent's
--     indexes are never used. Reading a result set by id is crossmatch's
--     need, not load's; its index lands with crossmatch.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE sources
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN result_set rapid_ulid REFERENCES result_sets (instance);

COMMENT ON COLUMN sources.run IS
    'The run whose load attempt wrote this row, NULL for rows written before '
    'the run model (products page, "Database result sets").';
COMMENT ON COLUMN sources.attempt IS
    'The load attempt that wrote this row, NULL for rows written before the '
    'run model (products page, "Database result sets").';
COMMENT ON COLUMN sources.result_set IS
    'The source-set result set this row belongs to (result_sets.instance), '
    'NULL for rows written before the run model (products page, "Database '
    'result sets").';

ALTER TABLE sources
    ADD CONSTRAINT sources_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (result_set IS NULL));
