--------------------------------------------------------------------------------------------------------------------------
-- 20261008_sources_xsources_rblabel
--
-- Add the rblabel column to the Sources and XSources parent tables, for the RuBR-AT real/bogus
-- label (1 real, 0 bogus, -1 not scored), to be loaded from the rb_label column of the PhotUtils
-- catalogs and the RB_LABEL column of the SExtractor catalogs.  The RuBR-AT score goes in the
-- existing rb column.
--
-- Run this after 20261006_sources_xsources_catalog_cols.sql, so that rblabel comes after the
-- columns that migration adds, as it does in database/schema/rapidOpsSourcesTable.sql.
--
-- The child tables (sources_<yyyymmdd>_<sca> and xsources_<yyyymmdd>_<sca>) inherit their
-- parent, so ALTER TABLE on the parent adds the column to every existing child as well, and
-- children created afterwards with CREATE TABLE ... (LIKE <parent> ...) get it too.
--
-- The column is nullable with no default, NULL meaning "not computed", so PostgreSQL changes
-- only the catalog and rewrites no table, existing rows get NULL, and the current load scripts,
-- which COPY with an explicit column list, keep working unchanged.
--
-- Apply to an existing database only.  A database built from scratch with
-- database/scripts/buildDatabase.sh already has this column, because
-- database/schema/rapidOpsSourcesTable.sql defines it.  Grants are table-level, so no grants
-- change.  No stored function refers to these tables, so rapidOpsProcs.sql does not need
-- re-running.
--
-- Run it as a PostgreSQL superuser, not $USER or the pipeline's database user.  The
-- migration adds a column to sources and xsources, which are owned by rapidporole, and through
-- them to every child table.  This prints t when connected as a superuser:
--
--     SELECT rolsuper FROM pg_roles WHERE rolname = current_user;
--
-- Run it in one transaction, so that a failure part way through leaves nothing behind:
--
--     psql -h localhost -p 5432 -d rapidopsdb -U <superuser> \
--          --single-transaction -v ON_ERROR_STOP=1 \
--          -f database/schema/migrations/20261008_sources_xsources_rblabel.sql
--
-- Before running, stop the load scripts and cross-matching jobs, and check the number of child
-- tables against the lock limit, as described in 20261006_sources_xsources_catalog_cols.sql:
-- each ALTER TABLE locks its parent and every child until the transaction ends.  If one
-- transaction fails with "ERROR: out of shared memory", nothing has changed; run the file again
-- without --single-transaction, so that each ALTER TABLE commits on its own.
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 8 October 2026
--------------------------------------------------------------------------------------------------------------------------


-----------------------------
-- TABLE: Sources (PhotUtils catalogs)
-----------------------------

ALTER TABLE sources
    ADD COLUMN rblabel smallint;                -- RuBR-AT real/bogus label: 1 real, 0 bogus, -1 not scored (null = not computed)


-----------------------------
-- TABLE: XSources (SExtractor catalogs)
-----------------------------

ALTER TABLE xsources
    ADD COLUMN rblabel smallint;                -- RB_LABEL: RuBR-AT real/bogus label: 1 real, 0 bogus, -1 not scored (null = not computed)
