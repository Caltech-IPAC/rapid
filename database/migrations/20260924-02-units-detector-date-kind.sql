--------------------------------------------------------------------------------------------------------------------------
-- 20260924-02-units-detector-date-kind.sql
--
-- Adds 'detector-date' to `units.unit_kind`'s CHECK list for the new
-- `maintain` stage (supervisor step 1, ruling R2, 2026-09-24): unit id
-- `<yyyymmdd>/SCA<nn>`, the (observation date, detector) a run's `load`
-- units for that date and detector share. None of the contract's other
-- four unit kinds fits `maintain`'s unit (`rapidpipe.stages.contract.UNIT_KINDS`,
-- `rapidpipe.products.manifest.UNIT_KINDS`, both amended in the same pull
-- request), so this is a fifth kind, not a substitution.
--
-- Applied migrations are never edited (database/migrations/README.md);
-- 20260921-02-run-model.sql's inline CHECK is dropped by Postgres's
-- default constraint name for a single-column CHECK (`<table>_<column>_check`)
-- and re-added with the fifth value. Nothing else about `units` changes.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE units DROP CONSTRAINT units_unit_kind_check;
ALTER TABLE units ADD CONSTRAINT units_unit_kind_check
    CHECK (unit_kind IN ('exposure', 'detector-image', 'field', 'processing-date', 'detector-date'));
