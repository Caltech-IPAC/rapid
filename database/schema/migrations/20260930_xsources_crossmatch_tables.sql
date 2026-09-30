--------------------------------------------------------------------------------------------------------------------------
-- 20260930_xsources_crossmatch_tables
--
-- Add the xmerges, xastroobjects and xastroobjectsmeta prototype tables, the SExtractor-catalog
-- counterparts of merges, astroobjects and astroobjectsmeta.
--
-- crossMatchXSources.py cross-matches the XSources records loaded from the SExtractor catalogs
-- of the SFFT difference images, creating an xastroobjects_<field> and an xmerges_<field>
-- like-table per sky tile.  computeStatisticsForXAstroObjects.py then creates an
-- xastroobjectsmeta_<field> like-table per sky tile and populates it with lightcurve statistics.
-- Each of those scripts builds its like-tables with CREATE TABLE ... (LIKE <prototype> INCLUDING
-- DEFAULTS INCLUDING CONSTRAINTS), so the three prototype tables must exist before the VPO runs
-- any of the XSources post-processing steps.
--
-- No records are ever inserted directly into the prototype tables, and the like-tables do not
-- inherit them, so this migration adds no data and changes no existing table.
--
-- Apply to an existing database only.  A database built from scratch with
-- database/scripts/buildDatabase.sh already has all of this, because
-- database/schema/rapidOpsSourcesTable.sql defines these three tables and
-- database/schema/rapidOpsSourcesTableGrants.sql defines their grants.
--
-- This migration is self-contained: run it by itself, with nothing else.  It only adds new
-- tables, so no stored function changes and rapidOpsProcs.sql does not need re-running.
--
-- Run it in one transaction, so that a failure part way through leaves nothing behind:
--
--     psql ... --single-transaction -v ON_ERROR_STOP=1 -f 20260930_xsources_crossmatch_tables.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 30 September 2026
--------------------------------------------------------------------------------------------------------------------------


-----------------------------
-- TABLE: XMerges
--
-- Associates an XAstroObjects record with an XSources record.  Cross-matching does not
-- partition on isdiffpos, so the XMerges records of one XAstroObject may reference both
-- positive and negative difference-image detections.
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE xmerges (
    xaid bigint NOT NULL,
    xsid bigint NOT NULL
);

ALTER TABLE xmerges OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

CREATE INDEX xmerges_xaid_idx ON xmerges USING btree (xaid);
CREATE INDEX xmerges_xsid_idx ON xmerges USING btree (xsid);


-----------------------------
-- TABLE: XAstroObjects
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE xastroobjects (
    xaid bigint NOT NULL,
    ra0 double precision NOT NULL,              -- RA corresponding to initial sky position
    dec0 double precision NOT NULL,             -- Dec corresponding to initial sky position
    flux0 real NOT NULL                         -- Aperture flux (fluxap) of initial sky position
);

ALTER TABLE xastroobjects OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY xastroobjects ADD CONSTRAINT xastroobjects_pkey PRIMARY KEY (xaid);


-----------------------------
-- TABLE: XAstroObjectsMeta
--
-- Prototype table for the xastroobjectsmeta_<field> like-tables that
-- computeStatisticsForXAstroObjects.py creates, populates and indexes.
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE xastroobjectsmeta (
    xaid bigint NOT NULL,
    meanra double precision NOT NULL,           -- Mean RA
    stdevra real NOT NULL,                      -- Standard deviation of RA
    meandec double precision NOT NULL,          -- Mean Dec
    stdevdec real NOT NULL,                     -- Standard deviation of Dec
    meanflux real NOT NULL,                     -- Mean aperture flux (fluxap)
    stdevflux real NOT NULL,                    -- Standard deviation of aperture flux
    nsources smallint NOT NULL                  -- Total number of xsources (all filters)
);

ALTER TABLE xastroobjectsmeta OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY xastroobjectsmeta ADD CONSTRAINT xastroobjectsmeta_pkey PRIMARY KEY (xaid);

CREATE INDEX xastroobjectsmeta_nsources_idx ON xastroobjectsmeta (nsources);

RESET default_tablespace;


-----------------------------
-- Grants
--
-- Identical to the grants in database/schema/rapidOpsSourcesTableGrants.sql.
--
-- The SELECT grant to rapidporole is what lets the pipeline codes run
-- CREATE TABLE <like-table> (LIKE <prototype> ...), which requires SELECT on the
-- prototype table.
-----------------------------

-- XMerges table

-- rapidreadrole

REVOKE ALL ON TABLE xmerges FROM rapidreadrole;
GRANT SELECT ON TABLE xmerges TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE xmerges FROM rapidadminrole;
GRANT ALL ON TABLE xmerges TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE xmerges FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE xmerges TO rapidporole;


-- XAstroObjects table

-- rapidreadrole

REVOKE ALL ON TABLE xastroobjects FROM rapidreadrole;
GRANT SELECT ON TABLE xastroobjects TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE xastroobjects FROM rapidadminrole;
GRANT ALL ON TABLE xastroobjects TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE xastroobjects FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE xastroobjects TO rapidporole;


-- XAstroObjectsMeta table

-- rapidreadrole

REVOKE ALL ON TABLE xastroobjectsmeta FROM rapidreadrole;
GRANT SELECT ON TABLE xastroobjectsmeta TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE xastroobjectsmeta FROM rapidadminrole;
GRANT ALL ON TABLE xastroobjectsmeta TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE xastroobjectsmeta FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE xastroobjectsmeta TO rapidporole;
