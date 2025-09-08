--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSourcesTable.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 8 August 2025
--------------------------------------------------------------------------------------------------------------------------




----------------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------------
-- Parent sources table for creating child tables, one for each combination of processing date and sca.
-- Inheritance, for sources child tables only, is needed because a given source ID looked up in the
-- merges table (see below) cannot be easily traced to the child table in which it is stored.
-- No records are directly inserted into the parent table.
--

-- https://photutils.readthedocs.io/en/stable/api/photutils.psf.PSFPhotometry.html#photutils.psf.PSFPhotometry
-- https://photutils.readthedocs.io/en/stable/api/photutils.detection.DAOStarFinder.html#photutils.detection.DAOStarFinder

-----------------------------
-- TABLE: Sources
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE sources (
    sid bigint NOT NULL,                       -- Database unique primary key
    id integer NOT NULL,                       -- Non-unique id column in photutils psf-fit catalog file in S3 bucket
    pid integer NOT NULL,                      -- DiffImages primary key
    ra double precision NOT NULL,              -- RA corresponding to (xfit,yfit)
    dec double precision NOT NULL,             -- Dec corresponding to (xfit,yfit)
    xfit real NOT NULL,                        -- PSF-fit x position
    yfit real NOT NULL,                        -- PSF-fit y position
    fluxfit real NOT NULL,                     -- PSF-fit instrumental flux
    xerr real NOT NULL,                        -- PSF-fit x error
    yerr real NOT NULL,                        -- PSF-fit y error
    fluxerr real NOT NULL,                     -- PSF-fit instrumental flux error
    npixfit smallint NOT NULL,                 -- Number of unmasked pixels used to fit the source
    qfit real NOT NULL,                        -- Sum of absolute-value fit residuals divided by fit flux
    cfit  real NOT NULL,                       -- Fit residual in initial central pixel value divided by fit flux
    flags smallint NOT NULL,                   -- photutils bitwise flags
    sharpness real NOT NULL,                   -- Object sharpness
    roundness1 real NOT NULL,                  -- Object roundness based on symmetry
    roundness2 real NOT NULL,                  -- Object roundness based on marginal Gaussian fits
    npix smallint NOT NULL,                    -- Total number of pixels in the Gaussian kernel array
    peak real NOT NULL,                        -- Peak pixel value of the object
    field integer NOT NULL,                    -- Roman tessellation index for (ra,dec)
    hp6 integer NOT NULL,                      -- Level-6 healpix index (NESTED) for (ra,dec)
    hp9 integer NOT NULL,                      -- Level-9 healpix index (NESTED) for (ra,dec)
    expid integer NOT NULL,                    -- Exposures primary key
    fid smallint NOT NULL,                     -- Filter ID
    sca smallint NOT NULL,                     -- SCA number (1...18)
    mjdobs double precision NOT NULL           -- MJD OBS of exposure
);


# Sources table must be owned by rapidporole for inheritance.
#ALTER TABLE sources OWNER TO rapidadminrole;
ALTER TABLE sources OWNER TO rapidporole;

CREATE SEQUENCE sources_sid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE sources_sid_seq OWNER TO rapidadminrole;

ALTER TABLE sources ALTER COLUMN sid SET DEFAULT nextval('sources_sid_seq'::regclass);

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY sources ADD CONSTRAINT sources_pkey PRIMARY KEY (sid);

ALTER TABLE ONLY sources ADD CONSTRAINT sourcespk UNIQUE (pid, id);

ALTER TABLE ONLY sources ADD CONSTRAINT sources_pid_fk FOREIGN KEY (pid) REFERENCES diffimages(pid);

CREATE INDEX sources_pid_idx ON sources (pid);
CREATE INDEX sources_expid_idx ON sources (expid);
CREATE INDEX sources_sca_idx ON sources (sca);
CREATE INDEX sources_field_idx ON sources (field);
CREATE INDEX sources_mjdobs_idx ON sources (mjdobs);

ALTER TABLE sources SET UNLOGGED;


------------------------------------------------------------
-- A python script will create child tables like the parent sources table.
-- Child-table names will be sources_<processing date: yyyymmdd>_<sca>.
-- The processing date is in Pacific time.
-- Thus the partitioning scheme for sources is by time and chip number.

-- Below are all the steps to be executed by the Python script for each new child table:

-- SET default_tablespace = pipeline_data_01;
-- CREATE TABLE sources_20250811_18 (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
-- ALTER TABLE sources_20250811_18 SET UNLOGGED;
-- ALTER TABLE sources_20250811_18 INHERIT sources;

-- Data-loading step:
-- Data is loaded into the table here...

-- SET default_tablespace = pipeline_indx_01;
-- CREATE INDEX sources_20250811_18_pid_idx ON sources_20250811_18 (pid);
-- CREATE INDEX sources_20250811_18_expid_idx ON sources_20250811_18 (expid);
-- CREATE INDEX sources_20250811_18_sca_idx ON sources_20250811_18 (sca);
-- CREATE INDEX sources_20250811_18_field_idx ON sources_20250811_18 (field);
-- CREATE INDEX sources_20250811_18_mjdobs_idx ON sources_20250811_18 (mjdobs);

-- The following is not automatically created for the child table just
-- because sid is a primary key in the prototype table.
-- CREATE INDEX sources_20250811_18_sid_idx ON sources_20250811_18 (sid);

-- CREATE INDEX sources_20250811_18_radec_idx ON sources_20250811_18 (q3c_ang2ipix(ra, dec));
-- CLUSTER sources_20250811_18_radec_idx ON sources_20250811_18;
-- ANALYZE sources_20250811_18;

-- ALTER TABLE sources_20250811_18 SET LOGGED;

-- Grants for rapidreadrole
-- REVOKE ALL ON TABLE sources_20250811_18 FROM rapidreadrole;
-- GRANT SELECT ON TABLE sources_20250811_18 TO GROUP rapidreadrole;

-- Grants for rapidadminrole
-- REVOKE ALL ON TABLE sources_20250811_18 FROM rapidadminrole;
-- GRANT ALL ON TABLE sources_20250811_18 TO GROUP rapidadminrole;

-- Grants for rapidporole
-- REVOKE ALL ON TABLE sources_20250811_18 FROM rapidporole;
-- GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE sources_20250811_18 TO rapidporole;

-- Matching all sources by position between catalogs for two different observation times,
-- using a Q3C-library function (executed after 2 child tables are available for cross matching):
-- E.g.,
-- SELECT a.sid,b.sid
-- FROM sources_20250811_18 AS a, sources_2_17 AS b
-- WHERE q3c_join(a.ra, a.dec, b.ra, b.dec, 0.000277778)
-- This query returns ALL pairs within the search cone, not just the nearest neighbors.

-- Cone-searching query (used to build a light curve for a specified sky position ra_, dec_):
-- E.g.,
-- SELECT cast('1' as smallint) as time, id, ra, dec, flux, cast(q3c_dist(ra, dec, ra_, dec_) * 3600.0 as real) as dist
-- FROM Objects_1
-- WHERE q3c_radial_query(ra, dec, ra_, dec_, radius_)
-- ORDER by dist;
------------------------------------------------------------




----------------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------------
-- Prototype merges and astroobjects tables for creating like-tables, one for each sky tile (a.k.a field).
-- Like-tables are NOT inherited from the prototype table
-- (and therefore terminology like "parent" and/or "child" is avoided for these tables).
-- No records are directly inserted into the prototype tables.

-----------------------------
-- TABLE: Merges
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE merges (
    aid bigint NOT NULL,
    sid bigint NOT NULL
);

ALTER TABLE merges OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

CREATE INDEX merges_aid_idx ON merges USING btree (aid);
CREATE INDEX merges_sid_idx ON merges USING btree (sid);


-----------------------------
-- TABLE: AstroObjects
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE astroobjects (
    aid bigint NOT NULL,
    ra0 double precision NOT NULL,              -- RA corresponding to initial sky position
    dec0 double precision NOT NULL,             -- Dec corresponding to initial sky position
    flux0 real NOT NULL,                        -- Flux of initial sky position
    meanra double precision NOT NULL,           -- Mean RA
    stdevra real NOT NULL,                      -- Standard deviation of RA
    meandec double precision NOT NULL,          -- Mean Dec
    stdevdec real NOT NULL,                     -- Standard deviation of Dec
    meanflux real NOT NULL,                     -- Mean flux
    stdevflux real NOT NULL,                    -- Standard deviation of flux
    nsources smallint NOT NULL,                 -- Total number of sources (all filters)
    field integer NOT NULL,                     -- Roman tessellation index for (ra,dec)
    hp6 integer NOT NULL,                       -- Level-6 healpix index (NESTED) for (ra,dec)
    hp9 integer NOT NULL                        -- Level-9 healpix index (NESTED) for (ra,dec)
);

ALTER TABLE astroobjects OWNER TO rapidadminrole;

CREATE SEQUENCE astroobjects_aid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE astroobjects_aid_seq OWNER TO rapidadminrole;

ALTER TABLE astroobjects ALTER COLUMN aid SET DEFAULT nextval('astroobjects_aid_seq'::regclass);

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY astroobjects ADD CONSTRAINT astroobjects_pkey PRIMARY KEY (aid);

CREATE INDEX astroobjects_field_idx ON astroobjects (field);
CREATE INDEX astroobjects_nsources_idx ON astroobjects (nsources);


------------------------------------------------------------
-- A python script will create tables like the merges and astroobjects prototype tables,
-- which is not the same thing as inheriting the respective prototype table.
-- Like-table names will be merges_<field> and astroobjects_<field>.
-- Thus the partitioning scheme for merges and astroobjects is by sky position.

-- Below are all the steps to be executed by the Python script for each new
-- respective like-table:

-- SET default_tablespace = pipeline_data_01;
-- CREATE TABLE merges_1 (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
-- CREATE TABLE astroobjects_1 (LIKE sources INCLUDING DEFAULTS INCLUDING CONSTRAINTS);

-- SET default_tablespace = pipeline_indx_01;
-- CREATE INDEX merges_1_aid_idx ON merges_1 USING btree (aid);
-- CREATE INDEX merges_1_sid_idx ON merges_1 USING btree (sid);
-- CREATE INDEX astroobjects_1_field_idx ON astroobjects_1 (field);
-- CREATE INDEX astroobjects_1_nsources_idx ON astroobjects_1 (nsources);

-- The following is not automatically created for the astroobjects like-table just
-- because aid is a primary key in the astroobjects prototype table.
-- CREATE INDEX astroobjects_1_aid_idx ON astroobjects_1 (aid);

-- ALTER TABLE ONLY astroobjects_1 ADD CONSTRAINT astroobjectspk_1 UNIQUE (ra0, dec0);

-- CREATE INDEX astroobjects_1_radec_idx ON astroobjects_1 (q3c_ang2ipix(ra0, dec0));
-- CLUSTER astroobjects_1_radec_idx ON astroobjects_1;
-- ANALYZE astroobjects_1;

-- Grants for rapidreadrole
-- REVOKE ALL ON TABLE merges_1 FROM rapidreadrole;
-- GRANT SELECT ON TABLE merges_1 TO GROUP rapidreadrole;
-- REVOKE ALL ON TABLE astroobjects_1 FROM rapidreadrole;
-- GRANT SELECT ON TABLE astroobjects_1 TO GROUP rapidreadrole;

-- Grants for rapidadminrole
-- REVOKE ALL ON TABLE merges_1 FROM rapidadminrole;
-- GRANT ALL ON TABLE merges_1 TO GROUP rapidadminrole;
-- REVOKE ALL ON TABLE astroobjects_1 FROM rapidadminrole;
-- GRANT ALL ON TABLE astroobjects_1 TO GROUP rapidadminrole;

-- Grants for rapidporole
-- REVOKE ALL ON TABLE merges_1 FROM rapidporole;
-- GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE merges_1 TO rapidporole;
-- REVOKE ALL ON TABLE astroobjects_1 FROM rapidporole;
-- GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE astroobjects_1 TO rapidporole;

-- Matching all sources catalog by position to astroobjects catalog,
-- using a Q3C-library function:
-- E.g.,
-- SELECT a.aid,b.sid
-- FROM astroobjects_1 AS a, sources_20250811_18 AS b
-- WHERE q3c_join(a.ra0, a.dec0, b.ra, b.dec, 0.000277778)
-- This query returns ALL pairs within the search cone, not just the nearest neighbors.
-- The results of this source matching can be stored in the merges_1 table or parquet file.

-- Cone-searching query (used to build a light curve for a specified sky position ra_, dec_):
-- E.g.,
-- SELECT aid, ra0, dec0, flux0, cast(q3c_dist(ra0, dec0, ra_, dec_) * 3600.0 as real) as dist
-- FROM astroobjects_1
-- WHERE q3c_radial_query(ra0, dec0, ra_, dec_, radius_)
-- ORDER by dist;
------------------------------------------------------------

