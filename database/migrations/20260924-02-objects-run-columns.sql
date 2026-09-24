--------------------------------------------------------------------------------------------------------------------------
-- 20260924-02-objects-run-columns.sql
--
-- Attaches the run model (20260921-02-run-model.sql) to the `dev` prototypes
-- `merges`, `astroobjects` and `astroobjectsmeta`, which the rebuild's
-- `crossmatch` and `statistics` stages write through per-field tables made
-- `LIKE` them (20260924-03). Authority: rapid_docs' products page, "Database
-- result sets": an `association-set` is a result set whose rows live in
-- `merges` and `astroobjects`, a `statistics-set` one whose rows live in
-- `astroobjectsmeta`, and "Every row carries the run id, the attempt id that
-- wrote it and its result-set id." The `dev` schema is kept: nothing is
-- renamed or dropped, and `dev`'s columns keep their meaning.
--
-- Choices, in the pattern of 20260923-04-sources-run-columns.sql:
--   - `run`, `attempt` and `result_set` are nullable. Rows with `run IS NULL`
--     are pre-run-model rows (written by `dev`'s crossMatchSources.py and
--     computeStatisticsForAstroObjects.py) and are always current: every run's
--     crossmatch reads them as part of the catalog (step 1 ruling R3,
--     2026-09-24). No existing row is touched.
--   - A CHECK requires the three to be all NULL or all set: a row half
--     attached to the run model is not a state this schema allows.
--   - The prototypes are NOT inheritance parents ("Like-tables are NOT
--     inherited from the prototype table", baseline). A per-field table made
--     after this migration gets the columns and the CHECK from `CREATE TABLE
--     ... (LIKE <prototype> INCLUDING DEFAULTS INCLUDING CONSTRAINTS)`, as
--     `dev` makes them. A per-field table `dev` made before this migration
--     does not gain them: ADD COLUMN on a prototype reaches no copy. A
--     database built by this stream holds no such table. Production `rapid`
--     does; 20260924-03's table functions adopt such a table in place (they
--     add these columns, the CHECK and the rebuild's constraints and indexes)
--     the first time a stage asks for it.
--   - The foreign keys sit on the prototypes only. `LIKE` never copies a
--     foreign key, and the prototypes hold no rows, so they are declarations
--     of meaning, as 20260923-04's are on `sources`.
--   - No index on the new columns: the prototypes hold no rows, and each
--     per-field table indexes `run` and `result_set` when it is made
--     (20260924-03).
--------------------------------------------------------------------------------------------------------------------------

-- merges -----------------------------------------------------------------------------------------------------------------

ALTER TABLE merges
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN result_set rapid_ulid REFERENCES result_sets (instance);

COMMENT ON COLUMN merges.run IS
    'The run whose crossmatch attempt wrote this row, NULL for rows written '
    'before the run model, which are always current (products page, "Database '
    'result sets").';
COMMENT ON COLUMN merges.attempt IS
    'The crossmatch attempt that wrote this row, NULL for rows written before '
    'the run model (products page, "Database result sets").';
COMMENT ON COLUMN merges.result_set IS
    'The association-set result set this row belongs to (result_sets.instance), '
    'NULL for rows written before the run model (products page, "Database '
    'result sets").';

ALTER TABLE merges
    ADD CONSTRAINT merges_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (result_set IS NULL));

-- astroobjects -----------------------------------------------------------------------------------------------------------

ALTER TABLE astroobjects
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN result_set rapid_ulid REFERENCES result_sets (instance);

COMMENT ON COLUMN astroobjects.run IS
    'The run whose crossmatch attempt wrote this row, NULL for rows written '
    'before the run model, which are always current (products page, "Database '
    'result sets").';
COMMENT ON COLUMN astroobjects.attempt IS
    'The crossmatch attempt that wrote this row, NULL for rows written before '
    'the run model (products page, "Database result sets").';
COMMENT ON COLUMN astroobjects.result_set IS
    'The association-set result set this row belongs to (result_sets.instance), '
    'NULL for rows written before the run model (products page, "Database '
    'result sets").';

ALTER TABLE astroobjects
    ADD CONSTRAINT astroobjects_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (result_set IS NULL));

-- astroobjectsmeta -------------------------------------------------------------------------------------------------------

ALTER TABLE astroobjectsmeta
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN result_set rapid_ulid REFERENCES result_sets (instance);

COMMENT ON COLUMN astroobjectsmeta.run IS
    'The run whose statistics attempt wrote this row, NULL for rows written '
    'before the run model, which are always current (products page, "Database '
    'result sets").';
COMMENT ON COLUMN astroobjectsmeta.attempt IS
    'The statistics attempt that wrote this row, NULL for rows written before '
    'the run model (products page, "Database result sets").';
COMMENT ON COLUMN astroobjectsmeta.result_set IS
    'The statistics-set result set this row belongs to (result_sets.instance), '
    'NULL for rows written before the run model (products page, "Database '
    'result sets").';

ALTER TABLE astroobjectsmeta
    ADD CONSTRAINT astroobjectsmeta_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (result_set IS NULL));
