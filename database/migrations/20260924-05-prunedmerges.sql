--------------------------------------------------------------------------------------------------------------------------
-- 20260924-05-prunedmerges.sql
--
-- The membership table of the `pruned-set` result set, which the rebuild's
-- `prune` stage writes (step 1 ruling R6). Authority: rapid_docs' products
-- page, "Database result sets": "A pruned set is its base association set
-- minus an explicit list of excluded pairs (object, source) within that
-- base; the base is never mutated."
--
-- One row per EXCLUDED (aid, sid) pair: the pairs `dev`'s
-- pipeline/pruneNotBestMerges.py would delete from `merges_<field>`. `dev`
-- deletes them in place; the rebuild records them instead, under the pruned
-- set's own result-set id (`result_set`), naming the association set it
-- prunes (`base_set`). The pruned set's membership is the base set's
-- `merges_<field>` rows whose (aid, sid) has no row here for the same
-- `result_set`. A new table, not a `dev` one: `dev` has no pruned set.
--
-- Choices:
--   - One table for every field, not a per-field table: the base set names
--     its field through its logical key, and the rows are few (only the
--     excluded pairs).
--   - PRIMARY KEY (result_set, aid, sid): a pair is excluded from a set at
--     most once, and `INSERT ... ON CONFLICT DO NOTHING` makes a retried
--     insert a no-op (step 1 ruling R4's pattern).
--   - `run` and `attempt` are nullable with the all-or-none CHECK, the
--     shape of 20260923-04 and 20260924-03, although every row this stream
--     knows of carries both.
--   - Foreign keys on `result_set`, `base_set`, `run` and `attempt`, as on
--     every other run-model column: the rows reference a registered set, so
--     `prune` registers the set before inserting, as `load` does, all in one
--     transaction.
--   - Indexes on `base_set` (every pruned set of one association set) and
--     `run` (run deletion reads rows by run).
--   - Grants: the `dev` roles as `dev` grants its product tables, and the
--     rebuild's service login and `rapid_read` guarded on the roles'
--     existence as 20260923-01/-03 are, so CI applies them as a no-op.
--------------------------------------------------------------------------------------------------------------------------

CREATE TABLE prunedmerges (
    result_set      rapid_ulid NOT NULL REFERENCES result_sets (instance),
    base_set        rapid_ulid NOT NULL REFERENCES result_sets (instance),
    aid             bigint NOT NULL,
    sid             bigint NOT NULL,
    run             rapid_ulid REFERENCES runs (id),
    attempt         rapid_ulid REFERENCES attempts (id),
    PRIMARY KEY (result_set, aid, sid),
    CONSTRAINT prunedmerges_run_columns_together CHECK ((run IS NULL) = (attempt IS NULL))
);

COMMENT ON TABLE prunedmerges IS
    'The excluded (aid, sid) pairs of each pruned-set result set: the set is '
    'its base association set minus these pairs; the base is never mutated '
    '(products page, "Database result sets").';
COMMENT ON COLUMN prunedmerges.result_set IS
    'The pruned-set result set these exclusions define (result_sets.instance).';
COMMENT ON COLUMN prunedmerges.base_set IS
    'The association-set result set this pruned set is drawn from (result_sets.instance).';
COMMENT ON COLUMN prunedmerges.aid IS 'The excluded pair''s astro-object id (merges_<field>.aid).';
COMMENT ON COLUMN prunedmerges.sid IS 'The excluded pair''s source id (merges_<field>.sid).';
COMMENT ON COLUMN prunedmerges.run IS 'The run whose prune attempt wrote this row.';
COMMENT ON COLUMN prunedmerges.attempt IS 'The prune attempt that wrote this row.';

CREATE INDEX prunedmerges_base_set_idx ON prunedmerges (base_set);
CREATE INDEX prunedmerges_run_idx ON prunedmerges (run);

ALTER TABLE prunedmerges OWNER TO rapidadminrole;
REVOKE ALL ON TABLE prunedmerges FROM rapidreadrole;
GRANT SELECT ON TABLE prunedmerges TO GROUP rapidreadrole;
GRANT INSERT, UPDATE, SELECT, DELETE ON TABLE prunedmerges TO rapidporole;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE prunedmerges TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT SELECT ON TABLE prunedmerges TO rapid_read;
    ELSE
        RAISE NOTICE 'role rapid_read does not exist here; grants skipped';
    END IF;
END
$$;
