--------------------------------------------------------------------------------------------------------------------------
-- 20260921-05-l2-run-columns.sql
--
-- Attaches the run model (20260921-02-run-model.sql) to the `dev` l2-image
-- tables, `exposures`, `l2files` and `l2filemeta`, and adds the external
-- exposure identifier `admit` needs. Authority: rapid_docs' products page
-- (https://roman-rapid.readthedocs.io/en/latest/system/products.html),
-- "Registration metadata" section's l2-image field list -- "External
-- identifiers (the observatory's exposure id) are stored as delivered and
-- mapped to internal ids at admission" -- and its cross-kind rules; the
-- runs page's "Schema" section: "The additive columns and widened keys on
-- the `dev` product tables land one kind at a time ... each with the stage
-- that writes it," here `register` for the l2 image, following the
-- difference image (20260921-03-difference-run-columns.sql), whose
-- pattern this migration repeats for a second kind.
--
-- Choices made here, recorded in LEDGER-register-stage.md:
--   - `exposures.external_id` is a new nullable `text` column with its own
--     UNIQUE constraint, added independently of the run-columns three
--     below: it is present on every row `register` writes (legacy rows
--     admitted before `register` existed have none), not tied to whether
--     the run model is attached, so it gets no together-CHECK of its own.
--     `exposures.expid` stays the internal id and `exposurespk UNIQUE
--     (dateobs)` is untouched, exactly as migration 03 left `diffimages`'
--     legacy key alone apart from widening it.
--   - `l2files` and `l2filemeta` each gain nullable `run`, `attempt`,
--     `instance`, a together-CHECK, an `instance` UNIQUE and `run`/
--     `attempt` indexes -- the same shape migration 03 gave
--     `diffimages`/`diffimmeta`, for the same reason: legacy rows carry
--     none of them, and a row half-attached to the run model is refused.
--   - `l2filespk`, `UNIQUE (expid, sca, version)`, is dropped and
--     replaced by the same name over `(expid, sca, version, run)` with
--     `UNIQUE NULLS NOT DISTINCT` (PostgreSQL 15+; this stream targets
--     18), exactly migration 03's reasoning for `diffimagespk`: a plain
--     `UNIQUE (expid, sca, version, run)` would treat every legacy row's
--     NULL `run` as distinct from every other NULL, silently lifting
--     uniqueness off the legacy rows entirely. NULLS NOT DISTINCT keeps
--     NULL commensurable among legacy rows (so a duplicate (expid, sca,
--     version) with `run IS NULL` is still refused, as before) while
--     letting two distinct non-NULL `run` values each register that same
--     logical l2 image once -- a rebuild run and a legacy/production run,
--     or two rebuild runs, reprocessing the same delivery.
--   - `l2filemeta` carries `run`/`attempt`/`instance` directly rather than
--     only through its `rid` join to `l2files`, for the same reason
--     migration 03 gave `diffimmeta`: every row a stage attempt writes
--     says which run, attempt and instance wrote it without a join, and
--     `run delete` cleanup (runs page, "Deletion") can select run-scoped
--     `l2filemeta` rows by `run` directly.
--------------------------------------------------------------------------------------------------------------------------

-- ======================================================================
-- exposures: external_id
-- ======================================================================

ALTER TABLE exposures
    ADD COLUMN external_id text;

COMMENT ON COLUMN exposures.external_id IS
    'The observatory''s exposure id, stored as delivered (products page, '
    '"Registration metadata": "External identifiers ... are stored as '
    'delivered and mapped to internal ids at admission"). NULL for '
    'exposures registered before this column existed. expid remains the '
    'internal id; exposurespk UNIQUE (dateobs) is unchanged.';

ALTER TABLE exposures
    ADD CONSTRAINT exposures_external_id_uq UNIQUE (external_id);

-- ======================================================================
-- l2files: run, attempt, instance
-- ======================================================================

ALTER TABLE l2files
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN instance rapid_ulid REFERENCES product_instances (id);

COMMENT ON COLUMN l2files.run IS
    'The run that produced this row, NULL for legacy rows written before '
    'the run model (products page, "Schema").';
COMMENT ON COLUMN l2files.attempt IS
    'The attempt that produced this row, NULL for legacy rows written '
    'before the run model (products page, "Schema").';
COMMENT ON COLUMN l2files.instance IS
    'The product_instances row this l2files row corresponds to, NULL for '
    'legacy rows written before the run model (products page, "Schema").';

ALTER TABLE l2files
    ADD CONSTRAINT l2files_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (instance IS NULL));

ALTER TABLE l2files
    ADD CONSTRAINT l2files_instance_uq UNIQUE (instance);

CREATE INDEX l2files_run_idx ON l2files (run);
CREATE INDEX l2files_attempt_idx ON l2files (attempt);

-- Widen the logical key to include the run. See the header comment for
-- why NULLS NOT DISTINCT, not a plain UNIQUE, is what keeps legacy-row
-- uniqueness intact while letting two runs each hold the same (expid,
-- sca, version) once.
ALTER TABLE l2files DROP CONSTRAINT l2filespk;
ALTER TABLE l2files
    ADD CONSTRAINT l2filespk UNIQUE NULLS NOT DISTINCT (expid, sca, version, run);

-- ======================================================================
-- l2filemeta: run, attempt, instance
-- ======================================================================

ALTER TABLE l2filemeta
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN instance rapid_ulid REFERENCES product_instances (id);

COMMENT ON COLUMN l2filemeta.run IS
    'The run that produced this row, NULL for legacy rows written before '
    'the run model. Redundant with l2files.run via the rid join, carried '
    'directly so run-delete cleanup can select run-scoped rows here '
    'without a join (runs page, "Deletion").';
COMMENT ON COLUMN l2filemeta.attempt IS
    'The attempt that produced this row, NULL for legacy rows written '
    'before the run model. Redundant with l2files.attempt via the rid '
    'join, carried directly so every row says which attempt wrote it '
    'without a join.';
COMMENT ON COLUMN l2filemeta.instance IS
    'The product_instances row this l2filemeta row corresponds to, NULL '
    'for legacy rows written before the run model. Redundant with '
    'l2files.instance via the rid join, carried directly for the same '
    'reason as run and attempt above.';

ALTER TABLE l2filemeta
    ADD CONSTRAINT l2filemeta_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (instance IS NULL));

ALTER TABLE l2filemeta
    ADD CONSTRAINT l2filemeta_instance_uq UNIQUE (instance);

CREATE INDEX l2filemeta_run_idx ON l2filemeta (run);
CREATE INDEX l2filemeta_attempt_idx ON l2filemeta (attempt);
