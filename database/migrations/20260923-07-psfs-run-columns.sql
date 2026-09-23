--------------------------------------------------------------------------------------------------------------------------
-- 20260923-07-psfs-run-columns.sql
--
-- Attaches the run model (20260921-02-run-model.sql) to the `dev` `psfs` table,
-- for `register`'s handling of the `psf` kind (products page, "File products":
-- `psf`, made by `admit`, today's table `psfs`). The pattern of
-- 20260921-03-difference-run-columns.sql: `run`, `attempt`, `instance`,
-- nullable (rows `dev`'s add_psf/update_psf write carry none), all three NULL
-- or all set, `instance` unique, `run`/`attempt` indexed. Nothing renamed or
-- dropped.
--
-- The logical key `psfspk UNIQUE (fid, sca, version)` is kept as it is, not
-- widened by `run`: `register` allocates `version` through `dev`'s own `addPSF`
-- (the next number for (fid, sca) across the table), so two runs never hold
-- the same (fid, sca, version) and the constraint keeps its meaning.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE psfs
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN instance rapid_ulid REFERENCES product_instances (id);

COMMENT ON COLUMN psfs.run IS
    'The run whose register attempt recorded this row, NULL for rows written '
    'before the run model (products page, "Schema").';
COMMENT ON COLUMN psfs.attempt IS
    'The register attempt that wrote this row, NULL for rows written before '
    'the run model.';
COMMENT ON COLUMN psfs.instance IS
    'The product_instances row of this psf, NULL for rows written before the '
    'run model.';

ALTER TABLE psfs
    ADD CONSTRAINT psfs_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (instance IS NULL));

ALTER TABLE psfs
    ADD CONSTRAINT psfs_instance_uq UNIQUE (instance);

CREATE INDEX psfs_run_idx ON psfs (run);
CREATE INDEX psfs_attempt_idx ON psfs (attempt);
