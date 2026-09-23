--------------------------------------------------------------------------------------------------------------------------
-- 20260923-01-refimages-instance.sql
--
-- Attaches the run model (20260921-02-run-model.sql) to `refimages`, so
-- `register` can resolve a difference image's reference instance to its
-- `refimages` row (`diffimages.rfid`). Authority: rapid_docs' products
-- page, difference-image field list: "reference instance | manifest
-- identity | `diffimages.rfid`: that instance's `refimages` row". The
-- baseline `refimages` has no instance column, so without this the
-- lookup has nothing to match.
--
-- Additive only, as 20260921-03-difference-run-columns.sql did for
-- `diffimages` and 20260921-05-l2-run-columns.sql for `l2files`: three
-- nullable columns, all NULL or all set together, `instance` unique.
-- Legacy rows (every reference `dev` registered) keep NULLs; `register`
-- resolves those by the legacy rfid the difference manifest carries.
--
-- The logical key `refimagespk UNIQUE (field, fid, ppid, version)` is not
-- widened here: nothing in the rebuild writes `refimages` yet (reference
-- building is not ported), and the key is widened with the stage that
-- first needs two runs to hold one reference.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE refimages
    ADD COLUMN run rapid_ulid REFERENCES runs (id),
    ADD COLUMN attempt rapid_ulid REFERENCES attempts (id),
    ADD COLUMN instance rapid_ulid REFERENCES product_instances (id);

COMMENT ON COLUMN refimages.run IS
    'The run that produced this row, NULL for legacy rows written before '
    'the run model (products page, "Schema").';
COMMENT ON COLUMN refimages.attempt IS
    'The attempt that produced this row, NULL for legacy rows written '
    'before the run model (products page, "Schema").';
COMMENT ON COLUMN refimages.instance IS
    'The product_instances row this refimages row corresponds to, NULL '
    'for legacy rows written before the run model (products page, '
    '"Schema"). register resolves diffimages.rfid through it.';

ALTER TABLE refimages
    ADD CONSTRAINT refimages_run_columns_together
    CHECK ((run IS NULL) = (attempt IS NULL) AND (run IS NULL) = (instance IS NULL));

ALTER TABLE refimages
    ADD CONSTRAINT refimages_instance_uq UNIQUE (instance);

CREATE INDEX refimages_run_idx ON refimages (run);
CREATE INDEX refimages_attempt_idx ON refimages (attempt);
