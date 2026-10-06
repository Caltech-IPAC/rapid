--------------------------------------------------------------------------------------------------------------------------
-- 20261006_sources_xsources_catalog_cols
--
-- Add the new difference-image catalog columns to the Sources and XSources parent tables.
--
-- Sources gets nneg, nbad and sumrat, which are to be loaded from the PhotUtils catalogs under
-- the same names.  It already has rb.
--
-- XSources gets nneg, nbad, sumrat, sharpness, roundness1, roundness2, fluxfit, snrfit,
-- redchi, npixfit, flagsfit and cfit, which are to be loaded from the SExtractor-catalog
-- columns NNEG, NBAD, SUMRAT, SHARPNESS, ROUNDNESS1, ROUNDNESS2, FLUX_FIT, SNR_FIT,
-- REDUCED_CHI2, N_PIXELS_FIT, FLAGS_FIT and CFIT.  It already has rb.
--
-- The child tables (sources_<yyyymmdd>_<sca> and xsources_<yyyymmdd>_<sca>) inherit their
-- parent, so ALTER TABLE on the parent adds the columns to every existing child as well, and
-- children created afterwards with CREATE TABLE ... (LIKE <parent> ...) get them too.
--
-- The new columns are nullable with no default, NULL meaning "not computed", so:
--   * PostgreSQL changes only the catalog and rewrites no table, however many rows the
--     children hold;
--   * existing rows get NULL;
--   * the current load scripts (loadPSFCatIntoDBSourcesTable.py and
--     loadSECatIntoDBSourcesTable.py), which COPY with an explicit column list, keep working
--     unchanged and leave the new columns NULL until they are modified to load them.
--
-- Apply to an existing database only.  A database built from scratch with
-- database/scripts/buildDatabase.sh already has these columns, because
-- database/schema/rapidOpsSourcesTable.sql defines them, in the same order, so that both
-- routes give the same column order.  Grants are table-level, so no grants change.  No stored
-- function refers to these tables, so rapidOpsProcs.sql does not need re-running.
--
-- Run it in one transaction, so that a failure part way through leaves nothing behind:
--
--     psql ... --single-transaction -v ON_ERROR_STOP=1 -f 20261006_sources_xsources_catalog_cols.sql
--
-- Before running:
--
--   * Make sure no load script or cross-matching job is running.  Each ALTER TABLE takes an
--     ACCESS EXCLUSIVE lock on the parent and on every child until the transaction ends, and
--     a loader that created a child from the old parent and then ran ALTER TABLE ... INHERIT
--     after this migration would fail, since the child would lack the new columns.
--
--   * Count the child tables:
--
--         SELECT count(*) FROM pg_inherits WHERE inhparent = 'sources'::regclass;
--         SELECT count(*) FROM pg_inherits WHERE inhparent = 'xsources'::regclass;
--
--     Each ALTER TABLE locks the parent and all its children until the transaction ends, so
--     one transaction holds about as many locks as the two counts added together, and too
--     many fail with "ERROR: out of shared memory" (the transaction is then rolled back and
--     nothing is changed).  On a scratch PostgreSQL 17.4 server with default settings
--     (max_locks_per_transaction = 64, max_connections = 100), one transaction succeeded with
--     12000 children in total and failed with 20000.  The limit scales with
--     max_locks_per_transaction * (max_connections + max_prepared_transactions), so check
--     those settings on the operations server (SHOW max_locks_per_transaction; and so on).
--     If the total is too large, run the two ALTER TABLE statements below as separate
--     transactions (each is atomic by itself; with 10000 children per parent, that worked
--     where one transaction failed), or raise max_locks_per_transaction, which needs a
--     server restart.
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 6 October 2026
--------------------------------------------------------------------------------------------------------------------------


-----------------------------
-- TABLE: Sources (PhotUtils catalogs)
-----------------------------

ALTER TABLE sources
    ADD COLUMN nneg smallint,                   -- Number of negative pixels in 5x5 stamp on source (null = not computed)
    ADD COLUMN nbad smallint,                   -- Number of bad pixels in 5x5 stamp on source (null = not computed)
    ADD COLUMN sumrat real;                     -- sum(p)/sum(|p|) of median-filtered 5x5 stamp on source (null = not computed)


-----------------------------
-- TABLE: XSources (SExtractor catalogs)
-----------------------------

ALTER TABLE xsources
    ADD COLUMN nneg smallint,                   -- NNEG: number of negative pixels in 5x5 stamp on source (null = not computed)
    ADD COLUMN nbad smallint,                   -- NBAD: number of bad pixels in 5x5 stamp on source (null = not computed)
    ADD COLUMN sumrat real,                     -- SUMRAT: sum(p)/sum(|p|) of median-filtered 5x5 stamp on source (null = not computed)
    ADD COLUMN sharpness real,                  -- SHARPNESS: PhotUtils DAOStarFinder sharpness (null = not computed)
    ADD COLUMN roundness1 real,                 -- ROUNDNESS1: PhotUtils DAOStarFinder roundness from symmetry (null = not computed)
    ADD COLUMN roundness2 real,                 -- ROUNDNESS2: PhotUtils DAOStarFinder roundness from marginal fits (null = not computed)
    ADD COLUMN fluxfit real,                    -- FLUX_FIT: PhotUtils PSF-fit instrumental flux (null = not computed)
    ADD COLUMN snrfit real,                     -- SNR_FIT: PhotUtils PSF-fit flux / flux error (null = not computed)
    ADD COLUMN redchi real,                     -- REDUCED_CHI2: PhotUtils PSF-fit reduced chi2 (null = not computed)
    ADD COLUMN npixfit smallint,                -- N_PIXELS_FIT: number of unmasked pixels used in PSF fit (null = not computed)
    ADD COLUMN flagsfit smallint,               -- FLAGS_FIT: PhotUtils PSF-fit bitwise flags (null = not computed)
    ADD COLUMN cfit real;                       -- CFIT: PSF-fit residual in central pixel divided by fit flux (null = not computed)
