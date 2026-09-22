--------------------------------------------------------------------------------------------------------------------------
-- 20260921-03-difference-run-columns.sql
--
-- Attaches the run model (20260921-02-run-model.sql) to the `dev`
-- difference-image tables, `diffimages` and `diffimmeta`. Authority:
-- rapid_docs' products page
-- (https://roman-rapid.readthedocs.io/en/latest/system/products.html),
-- "Schema" section: "The `dev` schema's product tables ... are kept with
-- their names and columns. Each gains `run`, `attempt` and `instance` (or
-- `result_set`) columns, and where a uniqueness constraint would block
-- two runs holding the same logical product it is widened to include the
-- run or set. Nothing is renamed and nothing is dropped." and the runs
-- page's "Schema" section: "The additive columns and widened keys on the
-- `dev` product tables land one kind at a time, difference image first,
-- each with the stage that writes it." This migration lands the columns;
-- the `register` stage that writes them lands separately (products page,
-- "Schema"; lead's 2026-09-21 handover ordered the columns first).
--
-- Choices made here, recorded in LEDGER-difference-columns.md:
--   - All three new columns on both tables are nullable. Rows the legacy
--     `addDiffImage`/`registerDiffImMeta` procedures write carry none of
--     them, and legacy rows are left untouched -- nothing here requires
--     backfilling or touching a single existing row.
--   - A CHECK constraint on each table requires the three columns to be
--     all NULL or all set together: a row half-attached to the run model
--     (e.g. a run but no attempt) is not a state this schema allows.
--   - `diffimages.instance` and `diffimmeta.instance` are each UNIQUE
--     (NULLs do not collide, so legacy rows are unaffected): one
--     diffimages/diffimmeta row per product instance.
--   - The legacy `diffimagespk` unique constraint, `UNIQUE (rid, ppid,
--     version)`, would refuse a second run producing the same logical
--     difference image as an earlier run -- exactly the case a rebuild
--     run and a legacy/production run, or two rebuild runs, both need to
--     hold. It is dropped and replaced by an equivalent name and column
--     list plus `run`, using `UNIQUE NULLS NOT DISTINCT` (PostgreSQL 15+;
--     this stream targets 18). A plain `UNIQUE (rid, ppid, version, run)`
--     would silently lift the uniqueness rule off every legacy row,
--     because plain UNIQUE treats every NULL as distinct from every other
--     NULL -- two, or two thousand, legacy rows sharing the same (rid,
--     ppid, version) with `run` NULL would no longer conflict. NULLS NOT
--     DISTINCT keeps NULL among legacy rows' `run` values, so the
--     constraint continues to reject a duplicate (rid, ppid, version)
--     for `run IS NULL` exactly as before, while two distinct non-NULL
--     `run` values may each hold that combination once. The name stays
--     `diffimagespk` so it continues to say "this is diffimages' logical
--     key", persisting the team's existing name for it.
--   - `diffimmeta` gets the same three columns, together-CHECK and
--     instance-unique, plus `run`/`attempt` indexes, even though
--     `diffimmeta` is one-to-one with `diffimages` on `pid` and the
--     values are therefore recoverable by a join. They are carried
--     directly so every row a stage attempt writes says which run,
--     attempt and instance wrote it without a join, and so `run delete`
--     cleanup (runs page, "Deletion": "cleanup idempotently removes the
--     run's object versions and run-scoped science rows") can select
--     run-scoped `diffimmeta` rows by the `run` column directly rather
--     than joining through `diffimages` first.
--------------------------------------------------------------------------------------------------------------------------

-- ======================================================================
-- diffimages: run, attempt, instance
-- ======================================================================

ALTER TABLE diffimages
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN instance rapid_ulid REFERENCES product_instances (id);

COMMENT ON COLUMN diffimages.run IS
    'The run that produced this row, NULL for legacy rows written before '
    'the run model (products page, "Schema").';
COMMENT ON COLUMN diffimages.attempt IS
    'The attempt that produced this row, NULL for legacy rows written '
    'before the run model (products page, "Schema").';
COMMENT ON COLUMN diffimages.instance IS
    'The product_instances row this diffimages row corresponds to, NULL '
    'for legacy rows written before the run model (products page, '
    '"Schema").';

ALTER TABLE diffimages
    ADD CONSTRAINT diffimages_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (instance IS NULL));

ALTER TABLE diffimages
    ADD CONSTRAINT diffimages_instance_uq UNIQUE (instance);

CREATE INDEX diffimages_run_idx ON diffimages (run);
CREATE INDEX diffimages_attempt_idx ON diffimages (attempt);

-- Widen the logical key to include the run. See the header comment for
-- why NULLS NOT DISTINCT, not a plain UNIQUE, is what keeps legacy-row
-- uniqueness intact while letting two runs each hold the same (rid,
-- ppid, version) once.
ALTER TABLE diffimages DROP CONSTRAINT diffimagespk;
ALTER TABLE diffimages
    ADD CONSTRAINT diffimagespk UNIQUE NULLS NOT DISTINCT (rid, ppid, version, run);

-- ======================================================================
-- diffimmeta: run, attempt, instance
-- ======================================================================

ALTER TABLE diffimmeta
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN instance rapid_ulid REFERENCES product_instances (id);

COMMENT ON COLUMN diffimmeta.run IS
    'The run that produced this row, NULL for legacy rows written before '
    'the run model. Redundant with diffimages.run via the pid join, '
    'carried directly so run-delete cleanup can select run-scoped rows '
    'here without a join (runs page, "Deletion").';
COMMENT ON COLUMN diffimmeta.attempt IS
    'The attempt that produced this row, NULL for legacy rows written '
    'before the run model. Redundant with diffimages.attempt via the '
    'pid join, carried directly so every row says which attempt wrote '
    'it without a join.';
COMMENT ON COLUMN diffimmeta.instance IS
    'The product_instances row this diffimmeta row corresponds to, NULL '
    'for legacy rows written before the run model. Redundant with '
    'diffimages.instance via the pid join, carried directly for the '
    'same reason as run and attempt above.';

ALTER TABLE diffimmeta
    ADD CONSTRAINT diffimmeta_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (instance IS NULL));

ALTER TABLE diffimmeta
    ADD CONSTRAINT diffimmeta_instance_uq UNIQUE (instance);

CREATE INDEX diffimmeta_run_idx ON diffimmeta (run);
CREATE INDEX diffimmeta_attempt_idx ON diffimmeta (attempt);
