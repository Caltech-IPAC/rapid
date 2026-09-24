--------------------------------------------------------------------------------------------------------------------------
-- 20260924-06-units-detector-date.sql
--
-- Adds the unit kind `detector-date` to `units.unit_kind`'s CHECK. The `maintain`
-- stage's unit is one (observation date, detector) pair, unit id
-- `<yyyymmdd>/SCA<nn>` (step 1 ruling R2, 2026-09-24). No kind among the four in
-- 20260921-02-run-model.sql fits it, so the stage contract's list gains a fifth.
-- Without this migration the launcher cannot add a `maintain` unit.
--
-- The CHECK is an inline column constraint in 20260921-02, so PostgreSQL named it
-- `units_unit_kind_check` (<table>_<column>_check). It is dropped and re-added
-- under the same name with the one extra value, in one statement, so no moment
-- exists without a check. Existing rows all satisfy the wider check. Additive in
-- effect: every value the old check allowed is still allowed.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE units
    DROP CONSTRAINT units_unit_kind_check,
    ADD CONSTRAINT units_unit_kind_check
        CHECK (unit_kind IN ('exposure', 'detector-image', 'field', 'processing-date', 'detector-date'));
