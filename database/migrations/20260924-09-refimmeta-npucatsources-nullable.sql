--------------------------------------------------------------------------------------------------------------------------
-- 20260924-09-refimmeta-npucatsources-nullable.sql
--
-- Lets `refimmeta.npucatsources` be NULL. The column counts the sources in a
-- reference image's Photutils PSF-fit catalog; the baseline
-- (20260921-01-baseline.sql, TABLE RefImMeta) declares it NOT NULL because
-- `dev` always builds that catalog. The rebuild's `reference` stage builds
-- it only when `[psfcat] enabled` (off by default: it needs a reference PSF
-- input), so a reference without one has no count to record. NULL says
-- "not measured"; a substituted 0 would say "measured, none found".
-- Authority: supervisor step 8 rulings R6/R7 and the amendment from the
-- Codex plan review (2026-09-24). Written by `register`
-- (rapidpipe/db/refimages.py, through `dev`'s unchanged registerRefImMeta,
-- whose integer parameter already accepts NULL).
--
-- Relaxes a constraint only; every existing row already satisfies it, and
-- nothing is renamed or dropped. Idempotent: dropping NOT NULL from a
-- column that is already nullable is a no-op.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE refimmeta ALTER COLUMN npucatsources DROP NOT NULL;

COMMENT ON COLUMN refimmeta.npucatsources IS
    'Number of sources in the reference image''s Photutils PSF-fit catalog; '
    'NULL when no such catalog was made (20260924-09).';
