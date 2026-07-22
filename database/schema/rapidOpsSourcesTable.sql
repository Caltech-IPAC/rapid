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
    aid bigint NOT NULL,                 -- AstroObjects FK; the partition key
    id integer NOT NULL,                       -- Non-unique id column in photutils psf-fit catalog file in S3 bucket
    pid integer NOT NULL,                      -- DiffImages primary key
    isdiffpos boolean NOT NULL DEFAULT TRUE,   -- t = positive difference, f = negative difference
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
    redchi real NOT NULL DEFAULT 0.0           -- Reduced chi2
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
    PRIMARY KEY (aid, sid)                     -- partition key must be in the PK
) PARTITION BY HASH (aid) ;

-- Sources table must be owned by rapidporole for inheritance.
-- ALTER TABLE sources OWNER TO rapidadminrole;
ALTER TABLE sources OWNER TO rapidporole;

-- Create 128 partitions based on hash on aid
-- May need to increase the number of paritions if we don't do real/bogus filtering first
DO $$
BEGIN
    FOR r IN 0..127 LOOP
        EXECUTE format(
            'CREATE TABLE sources_p%s PARTITION OF sources '
            'FOR VALUES WITH (MODULUS 128, REMAINDER %s)', r, r);
        EXECUTE format('ALTER TABLE sources_p%s OWNER TO rapidporole', r);
    END LOOP;
END $$;

SET default_tablespace = pipeline_indx_01;
CREATE INDEX sources_aid_mjd_idx ON sources (aid, mjdobs);   -- light-curve retrieval
CREATE INDEX sources_mjdobs_brin ON sources USING brin (mjdobs);  -- date-range scans


CREATE SEQUENCE sources_sid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 100; --allow for parallel workers to not conflict (creates sid gaps but sid only needs to be unique)

ALTER SEQUENCE sources_sid_seq OWNER TO rapidadminrole;

ALTER TABLE sources ALTER COLUMN sid SET DEFAULT nextval('sources_sid_seq'::regclass);

SET default_tablespace = pipeline_indx_01;

ALTER TABLE sources ADD CONSTRAINT sourcespk UNIQUE (aid, pid, id); --prevents loading the same source to the same aid twice

ALTER TABLE sources ADD CONSTRAINT sources_pid_fk FOREIGN KEY (pid) REFERENCES diffimages(pid);

CREATE INDEX sources_pid_idx ON sources (pid);
CREATE INDEX sources_expid_idx ON sources (expid);
CREATE INDEX sources_sca_idx ON sources (sca);
CREATE INDEX sources_field_idx ON sources (field);
CREATE INDEX sources_flags_idx ON sources (flags);

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
--- AstroObjects table is clustered by q3c index but not partitioned
-----------------------------
-- TABLE: AstroObjects
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE astroobjects (
    aid bigint NOT NULL,
    ra0 double precision NOT NULL,              -- RA corresponding to initial sky position
    dec0 double precision NOT NULL,             -- Dec corresponding to initial sky position
    flux0 real NOT NULL,                        -- Flux of initial sky position
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

ALTER TABLE ONLY astroobjects ADD CONSTRAINT astroobjects_pkey PRIMARY KEY (aid);

CREATE INDEX astroobjects_field_idx ON astroobjects (field);

CREATE INDEX astroobjects_radec_idx ON astroobjects (q3c_ang2ipix(ra0, dec0));

CLUSTER astroobjects_radec_idx ON astroobjects;

ANALYZE astroobjects;

------------------------------------------------------------
-- Matching all sources catalog by position to astroobjects catalog,
-- using a Q3C-library function:
-- E.g.,
-- SELECT a.aid,b.sid
-- FROM astroobjects AS a, sources_20250811 AS b
-- WHERE q3c_join(a.ra0, a.dec0, b.ra, b.dec, 0.000277778)
-- This query returns ALL pairs within the search cone, not just the nearest neighbors.
-- The results of this source matching can be stored in a parquet file.

-- Cone-searching query (used to build a light curve for a specified sky position ra_, dec_):
-- E.g.,
-- SELECT aid, ra0, dec0, flux0, cast(q3c_dist(ra0, dec0, ra_, dec_) * 3600.0 as real) as dist
-- FROM astroobjects
-- WHERE q3c_radial_query(ra0, dec0, ra_, dec_, radius_)
-- ORDER by dist;
------------------------------------------------------------


-----------------------------
-- TABLE: AstroObjectsMeta
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE astroobjectsmeta (
    aid bigint NOT NULL,
    meanra double precision NOT NULL,           -- Mean RA
    cos_sum double precision NOT NULL,          -- sum(cos(RA)) used for circular mean to avoid 0/360 average issue
    sin_sum double preciscion NOT NULL,         -- sum(sin(RA))
    stdevra real NOT NULL,                      -- Standard deviation of RA
    meandec double precision NOT NULL,          -- Mean Dec
    stdevdec real NOT NULL,                     -- Standard deviation of Dec
    meanflux real NOT NULL,                     -- Mean flux
    fluxsum2 real NOT NULL,                     -- sum(flux^2)
    stdevflux real NOT NULL,                    -- Standard deviation of flux
    mjdmin double precision NOT NULL,
    mjdmax double precision NOT NULL,
    nsources smallint NOT NULL                  -- Total number of sources (all filters)
);

ALTER TABLE astroobjectsmeta OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY astroobjectsmeta ADD CONSTRAINT astroobjectsmeta_pkey PRIMARY KEY (aid);

CREATE INDEX astroobjectsmeta_nsources_idx ON astroobjectsmeta (nsources);

