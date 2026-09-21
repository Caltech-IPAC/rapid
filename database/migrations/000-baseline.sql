--------------------------------------------------------------------------------------------------------------------------
-- 000-baseline.sql
--
-- The team's current schema, taken as one idempotent-enough migration so
-- that "apply the migrations directory to an empty database" and "run
-- buildDatabase.sh's schema section" produce the same result. Produced by
-- concatenating the files below, in this order, from database/schema/ as
-- they stood at dev commit c740f3e32bd8166e3ee75e0b4a56e93d4285de02
-- (the commit database/scripts/buildDatabase.sh applies against, per the
-- Repositories section of rapid_docs/system/specification.md). See
-- database/migrations/README.md for how this baseline relates to the
-- migrations that follow it.
--
-- Source files, in the order concatenated (rapidOpsTableSpaces.sql is
-- listed here too but its content is NOT concatenated below -- see
-- "Stripped or adjusted" further down):
--   - rapidOpsRoles.sql
--   - rapidOpsTables.sql
--   - rapidOpsTableGrants.sql
--   - rapidOpsSourcesTable.sql
--   - rapidOpsSourcesTableGrants.sql
--   - rapidOpsProcs.sql
--   - rapidOpsProcGrants.sql
--   - rapidOpsFiltersInserts.sql
--   - rapidOpsPipelinesInserts.sql
--   - rapidOpsSWVersionsInserts.sql
--   - rapidOpsTableSpaces.sql (omitted outright, not concatenated)
--
-- buildDatabase.sh itself only ever runs rapidOpsTables.sql ->
-- rapidOpsTableGrants.sql -> rapidOpsProcs.sql -> rapidOpsProcGrants.sql
-- against a database that already has its roles and tablespaces from an
-- earlier, undocumented step; this baseline adds the roles and (adjusted)
-- tablespace handling ahead of the tables that depend on them, and the
-- sources-family tables and grants that buildDatabase.sh never applies but
-- which are part of the deployed schema.
--
-- Stripped or adjusted from the source files (see the specification's
-- Repositories section: nothing account-specific may be committed to this
-- public repo):
--
--   - rapidOpsTableSpaces.sql: its CREATE TABLESPACE statements bind to
--     the IMSS host's /data/db paths and are OMITTED outright. Every
--     `SET default_tablespace = pipeline_data_01;` /
--     `SET default_tablespace = pipeline_indx_01;` line inside
--     rapidOpsTables.sql and rapidOpsSourcesTable.sql is commented out
--     below rather than left to fail against a plain PostgreSQL instance
--     with no such tablespace; tables land in the database's default
--     tablespace instead. A deployment that wants the two-tablespace
--     layout creates them before applying this baseline and restores
--     these SET lines locally; that is account-specific infrastructure
--     and stays out of this repo.
--   - rapidOpsRoles.sql: the three roles (rapidadminrole, rapidporole,
--     rapidreadrole) are created LOGIN/NOLOGIN as in the source with no
--     passwords (passwords are never committed to git). The GRANT ... TO
--     ubuntu / rapidporuss / apollo lines name specific IMSS host logins
--     and are OMITTED; granting these roles to an actual login is a
--     deployment-time step, not baseline schema.
--   - rapidOpsSourcesTableGrants.sql: the GRANT CREATE ON SCHEMA public /
--     GRANT CREATE ON TABLESPACE ... TO rapidporuss lines name the same
--     IMSS-specific login and are OMITTED; the matching rapidporole
--     grants are kept. The two CREATE ON TABLESPACE grants to rapidporole
--     are also OMITTED since this baseline creates no tablespaces (see
--     above); a deployment using real tablespaces restores them locally.
--   - rapidOpsTimeZone.sql: OMITTED. It is instance configuration (sets
--     the timezone for a database literally named rapidopsdb, not this
--     schema's rapid/rapidopsdb-of-the-day) rather than schema, and
--     belongs with account-specific deployment configuration if kept at
--     all.
--
-- Kept verbatim, including as reference data the pipeline needs to run
-- (per this file's own remit: schema plus reference-data inserts, not
-- instance configuration):
--   - rapidOpsFiltersInserts.sql (8 Roman filters)
--   - rapidOpsPipelinesInserts.sql (3 pipeline definitions)
--   - rapidOpsSWVersionsInserts.sql (1 software-version row)
--
-- Everything else below is the source files' SQL verbatim, so the team
-- recognises it; only the two adjustments named above are made in place.
--------------------------------------------------------------------------------------------------------------------------


-- ======================================================================
-- source: database/schema/rapidOpsRoles.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsRoles.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------

create role rapidadminrole LOGIN SUPERUSER CREATEDB CREATEROLE;
create role rapidporole;
create role rapidreadrole;

-- OMITTED (account-specific login): GRANT rapidadminrole to ubuntu;
-- OMITTED (account-specific login): GRANT rapidporole to ubuntu;
-- OMITTED (account-specific login): GRANT rapidreadrole to ubuntu;

-- OMITTED (account-specific login): GRANT rapidporole to rapidporuss;

-- OMITTED (account-specific login): GRANT rapidreadrole to apollo;

-- Verified apollo inherits the following:
ALTER ROLE rapidreadrole CONNECTION LIMIT -1;

ALTER ROLE rapidporole CONNECTION LIMIT -1;

-- ======================================================================
-- source: database/schema/rapidOpsTables.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsTables
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------


-----------------------------
-- TABLE: Filters
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE filters (
    fid smallint NOT NULL,                               -- FITS-header keyword: FILTERID
    filter character varying(16) NOT NULL                -- FITS-header keyword: FILTER
);

ALTER TABLE filters OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY filters ADD CONSTRAINT filters_pkey PRIMARY KEY (fid);

ALTER TABLE ONLY filters ADD CONSTRAINT filterspk UNIQUE (filter);


-----------------------------
-- TABLE: Scas
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE scas (
    sca smallint NOT NULL,                            -- Primary key
    CONSTRAINT scaspk CHECK (((sca >= 1) AND (sca <= 18)))
);

ALTER TABLE scas OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY scas ADD CONSTRAINT scaspk2 UNIQUE (sca);


INSERT into scas (sca) SELECT generate_series(1,18);


-----------------------------
-- TABLE: Exposures
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE exposures (
    expid integer NOT NULL,                         -- Primary key
    dateobs timestamp without time zone NOT NULL,   -- Header keyword: DATE-OBS
    field integer NOT NULL,                         -- Roman tessellation index for RA_TARG, DEC_TARG
    hp6 integer NOT NULL,                           -- Level-6 healpix index (NESTED) for RA_TARG, DEC_TARG
    hp9 integer NOT NULL,                           -- Level-9 healpix index (NESTED) for RA_TARG, DEC_TARG
    fid smallint NOT NULL,                          -- Foreign key from Filters table
    exptime real NOT NULL,                          -- Header keyword EXPTIME
    mjdobs double precision NOT NULL,               -- Header keyword MJD-OBS
    status smallint DEFAULT 1 NOT NULL,
    infobits integer DEFAULT 0 NOT NULL,
    created timestamp without time zone             -- Timestamp of database record INSERT
        DEFAULT now() NOT NULL
);

ALTER TABLE exposures OWNER TO rapidadminrole;

CREATE SEQUENCE exposures_expid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE exposures_expid_seq OWNER TO rapidadminrole;

ALTER TABLE exposures ALTER COLUMN expid SET DEFAULT nextval('exposures_expid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY exposures ADD CONSTRAINT exposures_pkey PRIMARY KEY (expid);

ALTER TABLE ONLY exposures ADD CONSTRAINT exposurespk UNIQUE (dateobs);

ALTER TABLE ONLY exposures ADD CONSTRAINT exposures_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

CREATE INDEX exposures_fid_idx ON exposures (fid);
CREATE INDEX exposures_field_idx ON exposures (field);
CREATE INDEX exposures_hp6_idx ON exposures (hp6);
CREATE INDEX exposures_hp9_idx ON exposures (hp9);
CREATE INDEX exposures_exptime_idx ON exposures (exptime);
CREATE INDEX exposures_mjdobs_idx ON exposures (mjdobs);
CREATE INDEX exposures_status_idx ON exposures (status);
CREATE INDEX exposures_infobits_idx ON exposures (infobits);
CREATE INDEX exposures_dateobs_idx ON exposures (dateobs);


-----------------------------
-- TABLE: L2Files
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE l2files (
    rid integer NOT NULL,                                -- Primary key
    expid integer NOT NULL,
    sca smallint NOT NULL,                               -- FITS-header keyword: SCA-NUM
    version smallint NOT NULL,
    vbest smallint NOT NULL,
    field integer NOT NULL,                              -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                                -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                                -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,
    dateobs timestamp without time zone NOT NULL,        -- FITS-header keyword: DATE-OBS
    mjdobs double precision NOT NULL,                    -- FITS-header keyword: MJD-OBS
    exptime real NOT NULL,                               -- FITS-header keyword: EXPTIME
    infobits integer DEFAULT 0 NOT NULL,                  -- Bit-wise information flags
    filename character varying(255) NOT NULL,            -- Full path and filename
    checksum character varying(32) NOT NULL,             -- MD5 checksum of entire file
    status smallint DEFAULT 0 NOT NULL,                  -- Set to zero if bad and one if good (verify automatically with
                                                         -- DATASUM and CHECKSUM keywords, or set this manually later, if necessary)
    crval1 double precision NOT NULL,                    -- FITS-header keyword: CRVAL1
    crval2 double precision NOT NULL,                    -- FITS-header keyword: CRVAL2
    crpix1 real NOT NULL,                                -- FITS-header keyword: CRPIX1
    crpix2 real NOT NULL,                                -- FITS-header keyword: CRPIX2
    cd11 double precision NOT NULL,                      -- FITS-header keyword: CD1_1
    cd12 double precision NOT NULL,                      -- FITS-header keyword: CD1_2
    cd21 double precision NOT NULL,                      -- FITS-header keyword: CD2_1
    cd22 double precision NOT NULL,                      -- FITS-header keyword: CD2_1
    ctype1 character varying(16) NOT NULL,               -- FITS-header keyword: CTYPE1
    ctype2 character varying(16) NOT NULL,               -- FITS-header keyword: CTYPE2
    cunit1 character varying(16) NOT NULL,               -- FITS-header keyword: CUNIT1
    cunit2 character varying(16) NOT NULL,               -- FITS-header keyword: CUNIT2
    a_order smallint,                                    -- FITS-header keyword: A_ORDER
    a_0_1 double precision,                              -- FITS-header keyword: A_0_1
    a_0_2 double precision,                              -- FITS-header keyword: A_0_2
    a_0_3 double precision,                              -- FITS-header keyword: A_0_3
    a_0_4 double precision,                              -- FITS-header keyword: A_0_4
    a_0_5 double precision,                              -- FITS-header keyword: A_0_5
    a_1_0 double precision,                              -- FITS-header keyword: A_1_0
    a_1_1 double precision,                              -- FITS-header keyword: A_1_1
    a_1_2 double precision,                              -- FITS-header keyword: A_1_2
    a_1_3 double precision,                              -- FITS-header keyword: A_1_3
    a_1_4 double precision,                              -- FITS-header keyword: A_1_4
    a_2_0 double precision,                              -- FITS-header keyword: A_2_0
    a_2_1 double precision,                              -- FITS-header keyword: A_2_1
    a_2_2 double precision,                              -- FITS-header keyword: A_2_2
    a_2_3 double precision,                              -- FITS-header keyword: A_2_3
    a_3_0 double precision,                              -- FITS-header keyword: A_3_0
    a_3_1 double precision,                              -- FITS-header keyword: A_3_1
    a_3_2 double precision,                              -- FITS-header keyword: A_3_2
    a_4_0 double precision,                              -- FITS-header keyword: A_4_0
    a_4_1 double precision,                              -- FITS-header keyword: A_4_1
    a_5_0 double precision,                              -- FITS-header keyword: A_5_0
    b_order smallint,                                    -- FITS-header keyword: B_ORDER
    b_0_1 double precision,                              -- FITS-header keyword: B_0_1
    b_0_2 double precision,                              -- FITS-header keyword: B_0_2
    b_0_3 double precision,                              -- FITS-header keyword: B_0_3
    b_0_4 double precision,                              -- FITS-header keyword: B_0_4
    b_0_5 double precision,                              -- FITS-header keyword: B_0_5
    b_1_0 double precision,                              -- FITS-header keyword: B_1_0
    b_1_1 double precision,                              -- FITS-header keyword: B_1_1
    b_1_2 double precision,                              -- FITS-header keyword: B_1_2
    b_1_3 double precision,                              -- FITS-header keyword: B_1_3
    b_1_4 double precision,                              -- FITS-header keyword: B_1_4
    b_2_0 double precision,                              -- FITS-header keyword: B_2_0
    b_2_1 double precision,                              -- FITS-header keyword: B_2_1
    b_2_2 double precision,                              -- FITS-header keyword: B_2_2
    b_2_3 double precision,                              -- FITS-header keyword: B_2_3
    b_3_0 double precision,                              -- FITS-header keyword: B_3_0
    b_3_1 double precision,                              -- FITS-header keyword: B_3_1
    b_3_2 double precision,                              -- FITS-header keyword: B_3_2
    b_4_0 double precision,                              -- FITS-header keyword: B_4_0
    b_4_1 double precision,                              -- FITS-header keyword: B_4_1
    b_5_0 double precision,                              -- FITS-header keyword: B_5_0
    equinox real NOT NULL,                               -- FITS-header keyword: EQUINOX
    ra double precision NOT NULL,                        -- FITS-header keyword: RA_TARG
    dec double precision NOT NULL,                       -- FITS-header keyword: DEC_TARG
    paobsy real,                                         -- FITS-header keyword: PA_OBSY
    pafpa real,                                          -- FITS-header keyword: PA_FPA
    zptmag real,                                         -- FITS-header keyword: ZPTMAG
    skymean real,                                        -- FITS-header keyword: SKY-MEAN
    created timestamp without time zone                  -- Timestamp of database record INSERT or last UPDATE
        DEFAULT now() NOT NULL,
    overlapfields integer[] NOT NULL DEFAULT '{}'::integer[],
    CONSTRAINT l2files_vbest_check CHECK ((vbest = ANY (ARRAY[0, 1, 2]))),
    CONSTRAINT l2files_version_check CHECK ((version > 0)),
    CONSTRAINT l2files_ra_check CHECK (((ra >= 0.0) AND (ra < 360.0))),
    CONSTRAINT l2files_dec_check CHECK (((dec >= -90.0) AND (dec <= 90.0)))
);

ALTER TABLE l2files OWNER TO rapidadminrole;

CREATE SEQUENCE l2files_rid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE l2files_rid_seq OWNER TO rapidadminrole;

ALTER TABLE l2files ALTER COLUMN rid SET DEFAULT nextval('l2files_rid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_pkey PRIMARY KEY (rid);

ALTER TABLE ONLY l2files ADD CONSTRAINT l2filespk UNIQUE (expid, sca, version);

ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_expid_fk FOREIGN KEY (expid) REFERENCES exposures(expid);
ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_sca_fk FOREIGN KEY (sca) REFERENCES scas(sca);
ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

CREATE INDEX l2files_rid_idx ON l2files (rid);
CREATE INDEX l2files_sca_idx ON l2files (sca);
CREATE INDEX l2files_field_idx ON l2files (field);
CREATE INDEX l2files_hp6_idx ON l2files (hp6);
CREATE INDEX l2files_hp9_idx ON l2files (hp9);
CREATE INDEX l2files_fid_idx ON l2files (fid);
CREATE INDEX l2files_infobits_idx ON l2files (infobits);
CREATE INDEX l2files_status_idx ON l2files (status);
CREATE INDEX l2files_vbest_idx ON l2files (vbest);
CREATE INDEX l2files_mjdobs_idx ON l2files (mjdobs);
CREATE INDEX l2files_dateobs_idx ON l2files (dateobs);
CREATE INDEX l2files_overlapfields_idx ON l2files USING gin (overlapfields);

-- Q3C indexing will speed up ad-hoc cone searches on (ra, dec).

CREATE INDEX l2files_radec_idx ON l2files (q3c_ang2ipix(ra, dec));
CLUSTER l2files_radec_idx ON l2files;
ANALYZE l2files;


-----------------------------
-- TABLE: L2FileMeta
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE l2filemeta (
    rid integer NOT NULL,
    ra0 double precision NOT NULL,
    dec0 double precision NOT NULL,
    ra1 double precision NOT NULL,
    dec1 double precision NOT NULL,
    ra2 double precision NOT NULL,
    dec2 double precision NOT NULL,
    ra3 double precision NOT NULL,
    dec3 double precision NOT NULL,
    ra4 double precision NOT NULL,
    dec4 double precision NOT NULL,
    x double precision NOT NULL,
    y double precision NOT NULL,
    z double precision NOT NULL,
    hp6 integer NOT NULL,               -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,               -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,
    sca smallint NOT NULL,
    mjdobs double precision NOT NULL
);

ALTER TABLE l2filemeta OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY l2filemeta ADD CONSTRAINT l2filemeta_pkey PRIMARY KEY (rid);

ALTER TABLE ONLY l2filemeta ADD CONSTRAINT l2filemeta_rid_fk FOREIGN KEY (rid) REFERENCES l2files(rid);
ALTER TABLE ONLY l2filemeta ADD CONSTRAINT l2filemeta_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);
ALTER TABLE ONLY l2filemeta ADD CONSTRAINT l2filemeta_sca_fk FOREIGN KEY (sca) REFERENCES scas(sca);

CREATE INDEX l2filemeta_hp6_idx ON l2filemeta (hp6);
CREATE INDEX l2filemeta_hp9_idx ON l2filemeta (hp9);
CREATE INDEX l2filemeta_fid_idx ON l2filemeta (fid);
CREATE INDEX l2filemeta_sca_idx ON l2filemeta (sca);

-- Q3C indexing will speed up ad-hoc cone searches on (ra, dec).

CREATE INDEX l2filemeta_radec_idx ON l2filemeta (q3c_ang2ipix(ra0, dec0));
CLUSTER l2filemeta_radec_idx ON l2filemeta;
ANALYZE l2filemeta;


-----------------------------
-- TABLE: Pipelines
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE pipelines (
    ppid smallint NOT NULL,
    priority smallint NOT NULL,
    script character varying(255) default 'TBD' NOT NULL,
    descrip character varying(255) NOT NULL
);

ALTER TABLE pipelines OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY pipelines ADD CONSTRAINT pipelinespk UNIQUE (ppid);

ALTER TABLE ONLY pipelines ADD CONSTRAINT pipelinespk2 UNIQUE (priority);


-----------------------------
-- TABLE: SwVersions
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE swversions (
    svid smallint NOT NULL,
    cvstag varchar(30) NOT NULL,
    installed timestamp NOT NULL,
    comment varchar(255),
    release varchar(15) NOT NULL
);

ALTER TABLE swversions OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE SEQUENCE swversions_svid_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE swversions_svid_seq OWNER TO rapidadminrole;

ALTER TABLE swversions ALTER COLUMN svid SET DEFAULT nextval('swversions_svid_seq'::regclass);

ALTER TABLE ONLY swversions ADD CONSTRAINT swversions_pkey PRIMARY KEY (svid);


-----------------------------
-- TABLE: ArchiveVersions
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE archiveversions (
    avid integer NOT NULL,
    archived timestamp NOT NULL
);

ALTER TABLE archiveversions OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE SEQUENCE archiveversions_avid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE archiveversions_avid_seq OWNER TO rapidadminrole;

ALTER TABLE archiveversions ALTER COLUMN avid SET DEFAULT nextval('archiveversions_avid_seq'::regclass);

ALTER TABLE ONLY archiveversions ADD CONSTRAINT archiveversions_pkey PRIMARY KEY (avid);


-----------------------------
-- TABLE: RefImages
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE refimages (
    rfid integer NOT NULL,
    field integer NOT NULL,               -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                 -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                 -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,
    ppid smallint NOT NULL,
    version smallint NOT NULL,
    vbest smallint NOT NULL,
    filename character varying(255),
    status smallint DEFAULT 0 NOT NULL,
    checksum character varying(32),
    created timestamp without time zone DEFAULT now() NOT NULL,
    svid smallint NOT NULL,
    avid integer,
    archivestatus smallint DEFAULT 0 NOT NULL,
    infobits integer DEFAULT 0 NOT NULL,
    CONSTRAINT refimages_vbest_check CHECK ((vbest = ANY (ARRAY[0, 1, 2]))),
    CONSTRAINT refimages_version_check CHECK ((version > 0))
);

ALTER TABLE refimages OWNER TO rapidadminrole;

CREATE SEQUENCE refimages_rfid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE refimages_rfid_seq OWNER TO rapidadminrole;

ALTER TABLE refimages ALTER COLUMN rfid SET DEFAULT nextval('refimages_rfid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY refimages ADD CONSTRAINT refimages_pkey PRIMARY KEY (rfid);

ALTER TABLE ONLY refimages ADD CONSTRAINT refimagespk UNIQUE (field, fid, ppid, version);

ALTER TABLE ONLY refimages ADD CONSTRAINT refimages_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);
ALTER TABLE ONLY refimages ADD CONSTRAINT refimages_ppid_fk FOREIGN KEY (ppid) REFERENCES pipelines(ppid);
ALTER TABLE ONLY refimages ADD CONSTRAINT refimages_svid_fk FOREIGN KEY (svid) REFERENCES swversions(svid);
ALTER TABLE ONLY refimages ADD CONSTRAINT refimages_avid_fk FOREIGN KEY (avid) REFERENCES archiveversions(avid);

CREATE INDEX refimages_field_idx ON refimages (field);
CREATE INDEX refimages_hp6_idx ON refimages (hp6);
CREATE INDEX refimages_hp9_idx ON refimages (hp9);
CREATE INDEX refimages_fid_idx ON refimages (fid);
CREATE INDEX refimages_created_idx ON refimages (created);
CREATE INDEX refimages_vbest_idx ON refimages (vbest);
CREATE INDEX refimages_ppid_idx ON refimages (ppid);
CREATE INDEX refimages_avid_idx ON refimages (avid);
CREATE INDEX refimages_archivestatus_idx ON refimages (archivestatus);
CREATE INDEX refimages_status_idx ON refimages (status);


-----------------------------
-- TABLE: DiffImages
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE diffimages (
    pid integer NOT NULL,                          -- Primary key
    rid integer NOT NULL,                          -- Foreign key from L2Files table
    expid integer NOT NULL,                        -- Exposure ID
    sca smallint NOT NULL,                         -- SCA-NUM
    ppid smallint NOT NULL,                        -- Pipeline ID
    version smallint NOT NULL,
    vbest smallint NOT NULL,
    rfid integer NOT NULL,                         -- Foreign key, from RefImages table
    field integer NOT NULL,                        -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                          -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                          -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,                         -- Foreign key from Filters table
    jd double precision,                           -- Julian date of start of image
    ra0 double precision NOT NULL,                 -- Center of image
    dec0 double precision NOT NULL,
    ra1 double precision NOT NULL,                 -- Lower-left corner of image
    dec1 double precision NOT NULL,
    ra2 double precision NOT NULL,                 -- Lower-right corner of image
    dec2 double precision NOT NULL,
    ra3 double precision NOT NULL,                 -- Upper-right corner of image
    dec3 double precision NOT NULL,
    ra4 double precision NOT NULL,                 -- Upper-left corner of image
    dec4 double precision NOT NULL,
    infobitssci integer NOT NULL,                  -- Image InfoBits for input science image
    infobitsref integer NOT NULL,                  -- Image InfoBits for input reference image
    filename text NOT NULL,                        -- Full path and filename of positive difference image
    checksum character varying(32),
    status smallint DEFAULT 0 NOT NULL,            -- Good/bad diff image (1/0) based on several internal image QA indicators
    created timestamp without time zone DEFAULT now() NOT NULL,
    svid smallint NOT NULL,
    avid integer,
    archivestatus smallint DEFAULT 0 NOT NULL,
    nalertpackets integer,                         -- Number of alert packets (avro files) generated
    CONSTRAINT diffimages_vbest_check CHECK ((vbest = ANY (ARRAY[0, 1, 2]))),
    CONSTRAINT diffimages_version_check CHECK ((version > 0)),
    CONSTRAINT diffimages_ra0_check CHECK (((ra0 >= 0.0) AND (ra0 < 360.0))),
    CONSTRAINT diffimages_dec0_check CHECK (((dec0 >= -90.0) AND (dec0 <= 90.0))),
    CONSTRAINT diffimages_ra1_check CHECK (((ra1 >= 0.0) AND (ra1 < 360.0))),
    CONSTRAINT diffimages_dec1_check CHECK (((dec1 >= -90.0) AND (dec1 <= 90.0))),
    CONSTRAINT diffimages_ra2_check CHECK (((ra2 >= 0.0) AND (ra2 < 360.0))),
    CONSTRAINT diffimages_dec2_check CHECK (((dec2 >= -90.0) AND (dec2 <= 90.0))),
    CONSTRAINT diffimages_ra3_check CHECK (((ra3 >= 0.0) AND (ra3 < 360.0))),
    CONSTRAINT diffimages_dec3_check CHECK (((dec3 >= -90.0) AND (dec3 <= 90.0))),
    CONSTRAINT diffimages_ra4_check CHECK (((ra4 >= 0.0) AND (ra4 < 360.0))),
    CONSTRAINT diffimages_dec4_check CHECK (((dec4 >= -90.0) AND (dec4 <= 90.0)))
);

ALTER TABLE diffimages OWNER TO rapidadminrole;

CREATE SEQUENCE diffimages_pid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE diffimages_pid_seq OWNER TO rapidadminrole;

ALTER TABLE diffimages ALTER COLUMN pid SET DEFAULT nextval('diffimages_pid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_pkey PRIMARY KEY (pid);

ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimagespk UNIQUE (rid, ppid, version);

ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_expid_fk FOREIGN KEY (expid) REFERENCES exposures(expid);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_sca_fk FOREIGN KEY (sca) REFERENCES scas(sca);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_ppid_fk FOREIGN KEY (ppid) REFERENCES pipelines(ppid);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_rfid_fk FOREIGN KEY (rfid) REFERENCES refimages(rfid);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_svid_fk FOREIGN KEY (svid) REFERENCES swversions(svid);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_rid_fk FOREIGN KEY (rid) REFERENCES l2files(rid);
ALTER TABLE ONLY diffimages ADD CONSTRAINT diffimages_avid_fk FOREIGN KEY (avid) REFERENCES archiveversions(avid);

CREATE INDEX diffimages_rid_idx ON diffimages(rid);
CREATE INDEX diffimages_expid_idx ON diffimages(expid);
CREATE INDEX diffimages_sca_idx ON diffimages(sca);
CREATE INDEX diffimages_ppid_idx ON diffimages(ppid);
CREATE INDEX diffimages_rfid_idx ON diffimages(rfid);
CREATE INDEX diffimages_field_idx ON diffimages(field);
CREATE INDEX diffimages_hp6_idx ON diffimages (hp6);
CREATE INDEX diffimages_hp9_idx ON diffimages (hp9);
CREATE INDEX diffimages_fid_idx ON diffimages(fid);
CREATE INDEX diffimages_jd_idx ON diffimages(jd);
CREATE INDEX diffimages_status_idx ON diffimages(status);
CREATE INDEX diffimages_created_idx ON diffimages(created);
CREATE INDEX diffimages_infobitssci_idx ON diffimages(infobitssci);
CREATE INDEX diffimages_field_sca_idx ON diffimages(field, sca);
CREATE INDEX diffimages_vbest_idx ON diffimages (vbest);

-- Q3C indexing will speed up ad-hoc cone searches on (ra, dec).

CREATE INDEX diffimages_radec_idx ON diffimages (q3c_ang2ipix(ra0, dec0));
CLUSTER diffimages_radec_idx ON diffimages;
ANALYZE diffimages;


-----------------------------
-- TABLE: AlertNames
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE alertnames (
    alertname char(12) NOT NULL,     -- Primary key
    sca smallint NOT NULL,           -- Readout channel of candidate when alert was first created
    field integer NOT NULL,          -- Roman tessellation index for (ra,dec)
    hp6 integer NOT NULL,            -- Level-6 healpix index (NESTED) for (ra,dec)
    hp9 integer NOT NULL,            -- Level-9 healpix index (NESTED) for (ra,dec)
    ra double precision NOT NULL,    -- Right Ascension
    dec double precision NOT NULL,   -- Declination
    jd double precision NOT NULL,    -- Julian date of initial name usage
    candid bigint NOT NULL,          -- Candidate ID associated with initial name usage
    CONSTRAINT alertnames_ra_check CHECK (((ra >= 0.0) AND (ra < 360.0))),
    CONSTRAINT alertnames_dec_check CHECK (((dec >= -90.0) AND (dec <= 90.0)))
);

ALTER TABLE alertnames OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY alertnames
    ADD CONSTRAINT alertnames_pkey PRIMARY KEY (alertname);

CREATE INDEX alertnames_sca_idx ON alertnames(sca);
CREATE INDEX alertnames_field_idx ON alertnames(field);
CREATE INDEX alertnames_hp6_idx ON alertnames (hp6);
CREATE INDEX alertnames_hp9_idx ON alertnames (hp9);
CREATE INDEX alertnames_jd_idx ON alertnames(jd);
CREATE INDEX alertnames_candid_idx ON alertnames(candid);

-- Q3C indexing will speed up ad-hoc cone searches on (ra, dec).

CREATE INDEX alertnames_radec_idx ON alertnames (q3c_ang2ipix(ra, dec));
CLUSTER alertnames_radec_idx ON alertnames;
ANALYZE alertnames;

CREATE SEQUENCE alertnames_an24id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an24id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an25id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an25id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an26id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an26id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an27id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an27id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an28id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an28id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an29id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an29id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an30id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an30id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an31id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an31id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an32id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an32id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an33id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an33id_seq OWNER TO rapidadminrole;

CREATE SEQUENCE alertnames_an34id_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE alertnames_an34id_seq OWNER TO rapidadminrole;


-----------------------------
-- TABLE: Jobs
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE jobs (
    jid integer NOT NULL,
    rid integer,
    fid smallint,
    ppid smallint NOT NULL,
    expid integer,
    sca smallint,
    field integer,
    machine smallint,
    launched timestamp,
    qwaited interval,
    started timestamp,
    ended timestamp,
    elapsed interval,
    exitcode smallint,
    status smallint DEFAULT 0,
    slurm integer,
    awsbatchjobid varchar(64),
    CONSTRAINT jobs_status_check CHECK (((status >= -1) AND (status <= 1)))
);

ALTER TABLE jobs OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE SEQUENCE jobs_jid_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE jobs_jid_seq OWNER TO rapidadminrole;

ALTER TABLE jobs ALTER COLUMN jid SET DEFAULT nextval('jobs_jid_seq'::regclass);

ALTER TABLE ONLY jobs ADD CONSTRAINT jobs_pkey PRIMARY KEY (jid);

CREATE INDEX jobs_ppid_idx ON jobs (ppid);
CREATE INDEX jobs_expid_idx ON jobs (expid);
CREATE INDEX jobs_field_idx ON jobs (field);
CREATE INDEX jobs_sca_idx ON jobs (sca);
CREATE INDEX jobs_fid_idx ON jobs (fid);
CREATE INDEX jobs_rid_idx ON jobs (rid);
CREATE INDEX jobs_status_idx ON jobs (status);
CREATE INDEX jobs_exitcode_idx ON jobs (exitcode);
CREATE INDEX jobs_machine_idx ON jobs (machine);
CREATE INDEX jobs_started_idx ON jobs (started);


-----------------------------
-- TABLE: RefImCatalogs
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE refimcatalogs (
    rfcatid integer NOT NULL,
    rfid integer NOT NULL,
    ppid smallint NOT NULL,
    cattype smallint NOT NULL,
    field integer NOT NULL,                        -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                          -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                          -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,
    svid smallint NOT NULL,
    filename character varying(255) NOT NULL,
    checksum character varying(32) NOT NULL,
    status smallint DEFAULT 0 NOT NULL,
    created timestamp without time zone NOT NULL,
    archivestatus smallint DEFAULT 0 NOT NULL,
    avid integer
);

ALTER TABLE refimcatalogs OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE SEQUENCE refimcatalogs_rfcatid_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE refimcatalogs_rfcatid_seq OWNER TO rapidadminrole;

ALTER TABLE refimcatalogs ALTER COLUMN rfcatid SET DEFAULT nextval('refimcatalogs_rfcatid_seq'::regclass);

ALTER TABLE ONLY refimcatalogs ADD CONSTRAINT refimcatalogs_pkey PRIMARY KEY (rfcatid);

ALTER TABLE ONLY refimcatalogs ADD CONSTRAINT refimcatalogspk UNIQUE (rfid, ppid, cattype);

ALTER TABLE ONLY refimcatalogs ADD CONSTRAINT refimcatalogs_rfid_fk FOREIGN KEY (rfid) REFERENCES refimages(rfid);

ALTER TABLE ONLY refimcatalogs ADD CONSTRAINT refimcatalogs_ppid_fk FOREIGN KEY (ppid) REFERENCES pipelines(ppid);

ALTER TABLE ONLY refimcatalogs ADD CONSTRAINT refimcatalogs_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

ALTER TABLE ONLY refimcatalogs ADD CONSTRAINT refimcatalogs_avid_fk FOREIGN KEY (avid) REFERENCES archiveversions(avid);

CREATE INDEX refimcatalogs_created_idx ON refimcatalogs (created);
CREATE INDEX refimcatalogs_rfid_idx ON refimcatalogs (rfid);
CREATE INDEX refimcatalogs_ppid_idx ON refimcatalogs (ppid);
CREATE INDEX refimcatalogs_cattype_idx ON refimcatalogs (cattype);
CREATE INDEX refimcatalogs_archivestatus_idx ON refimcatalogs (archivestatus);
CREATE INDEX refimcatalogs_status_idx ON refimcatalogs (status);
CREATE INDEX refimcatalogs_fid_idx ON refimcatalogs (fid);
CREATE INDEX refimcatalogs_field_idx ON refimcatalogs (field);
CREATE INDEX refimcatalogs_hp6_idx ON refimcatalogs (hp6);
CREATE INDEX refimcatalogs_hp9_idx ON refimcatalogs (hp9);
CREATE INDEX refimcatalogs_svid_idx ON refimcatalogs (svid);
CREATE INDEX refimcatalogs_avid_idx ON refimcatalogs (avid);


-----------------------------
-- TABLE: RefImImages
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE refimimages (
    rfid integer NOT NULL,
    rid integer NOT NULL
);

ALTER TABLE refimimages OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY refimimages ADD CONSTRAINT refimimages_rfid_fk FOREIGN KEY (rfid) REFERENCES refimages(rfid);

ALTER TABLE ONLY refimimages ADD CONSTRAINT refimimages_rid_fk FOREIGN KEY (rid) REFERENCES l2files(rid);

CREATE INDEX refimimages_rid_idx ON refimimages (rid);
CREATE INDEX refimimages_rfid_idx ON refimimages (rfid);


-----------------------------
-- TABLE: SOCProcs
--
-- Tracks periodic, discrete deliveries of exposure data from the SOC.
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE socprocs (
    did integer NOT NULL,                                          -- Primary key
    datedeliv timestamp without time zone NOT NULL,
    mjdobsmin double precision NOT NULL,                           -- Minimum MJD of exposure data in this delivery
    mjdobsmax double precision NOT NULL,                           -- Maximum MJD of exposure data in this delivery
    filename character varying(255),
    status smallint DEFAULT 0 NOT NULL,
    checksum character varying(32),
    created timestamp without time zone DEFAULT now() NOT NULL
);

ALTER TABLE socprocs OWNER TO rapidadminrole;

CREATE SEQUENCE socprocs_did_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE socprocs_did_seq OWNER TO rapidadminrole;

ALTER TABLE socprocs ALTER COLUMN did SET DEFAULT nextval('socprocs_did_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY socprocs ADD CONSTRAINT socprocs_pkey PRIMARY KEY (did);

ALTER TABLE ONLY socprocs ADD CONSTRAINT socprocspk UNIQUE (datedeliv);

CREATE INDEX socprocs_datedeliv_idx ON socprocs (datedeliv);
CREATE INDEX socprocs_mjdobsmin_idx ON socprocs (mjdobsmin);
CREATE INDEX socprocs_mjdobsmax_idx ON socprocs (mjdobsmax);
CREATE INDEX socprocs_filename_idx ON socprocs (filename);
CREATE INDEX socprocs_status_idx ON socprocs (status);
CREATE INDEX socprocs_created_idx ON socprocs (created);


-----------------------------
-- TABLE: PSFs
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE psfs (
    psfid integer NOT NULL,                              -- Primary key
    fid smallint NOT NULL,
    sca smallint NOT NULL,                               -- FITS-header keyword: SCA-NUM
    version smallint NOT NULL,
    vbest smallint NOT NULL,
    filename character varying(255) NOT NULL,            -- Full path and filename
    checksum character varying(32) NOT NULL,             -- MD5 checksum of entire file
    status smallint DEFAULT 0 NOT NULL,                  -- Set to zero if bad and one if good (verify automatically with
                                                         -- DATASUM and CHECKSUM keywords, or set this manually later, if necessary)
    created timestamp without time zone                  -- Timestamp of database record INSERT or last UPDATE
        DEFAULT now() NOT NULL,
    CONSTRAINT psfs_vbest_check CHECK ((vbest = ANY (ARRAY[0, 1, 2]))),
    CONSTRAINT psfs_version_check CHECK ((version > 0))
);

ALTER TABLE psfs OWNER TO rapidadminrole;

CREATE SEQUENCE psfs_psfid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE psfs_psfid_seq OWNER TO rapidadminrole;

ALTER TABLE psfs ALTER COLUMN psfid SET DEFAULT nextval('psfs_psfid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY psfs ADD CONSTRAINT psfs_pkey PRIMARY KEY (psfid);

ALTER TABLE ONLY psfs ADD CONSTRAINT psfspk UNIQUE (fid, sca, version);

ALTER TABLE ONLY psfs ADD CONSTRAINT psfs_sca_fk FOREIGN KEY (sca) REFERENCES scas(sca);
ALTER TABLE ONLY psfs ADD CONSTRAINT psfs_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

CREATE INDEX psfs_psfid_idx ON psfs (psfid);
CREATE INDEX psfs_sca_idx ON psfs (sca);
CREATE INDEX psfs_status_idx ON psfs (status);
CREATE INDEX psfs_vbest_idx ON psfs (vbest);


-----------------------------
-- TABLE: DiffImMeta
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE diffimmeta (
    pid integer NOT NULL,
    nsexcatsources integer NOT NULL,         -- Number of records in DiffImage SExtractor catalog.
    scalefacref real NOT NULL,               -- Gain-matching scale factor for reference image.
    dxrmsfin real NOT NULL,                  -- Gain-matching x astrometric uncertainty (final).
    dyrmsfin real NOT NULL,                  -- Gain-matching y astrometric uncertainty (final).
    dxmedianfin real NOT NULL,               -- Gain-matching dx astrometric median offset (final).
    dymedianfin real NOT NULL,               -- Gain-matching dy astrometric median offset (final).
    field integer NOT NULL,                  -- Roman tessellation index for RA_TARG, DEC_TARG
    hp6 integer NOT NULL,                    -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                    -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,
    sca smallint NOT NULL
);

ALTER TABLE diffimmeta OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY diffimmeta ADD CONSTRAINT diffimmeta_pkey PRIMARY KEY (pid);

ALTER TABLE ONLY diffimmeta ADD CONSTRAINT diffimmeta_pid_fk FOREIGN KEY (pid) REFERENCES diffimages(pid);
ALTER TABLE ONLY diffimmeta ADD CONSTRAINT diffimmeta_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);
ALTER TABLE ONLY diffimmeta ADD CONSTRAINT diffimmeta_sca_fk FOREIGN KEY (sca) REFERENCES scas(sca);

CREATE INDEX diffimmeta_field_idx ON diffimmeta (field);
CREATE INDEX diffimmeta_hp6_idx ON diffimmeta (hp6);
CREATE INDEX diffimmeta_hp9_idx ON diffimmeta (hp9);
CREATE INDEX diffimmeta_fid_idx ON diffimmeta (fid);
CREATE INDEX diffimmeta_sca_idx ON diffimmeta (sca);


-----------------------------
-- TABLE: RefImMeta
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE refimmeta (
    rfid integer NOT NULL,                 -- Primary key
    field integer NOT NULL,                -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                  -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                  -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,                 -- Foreign key from Filters table
    nframes smallint NOT NULL,             -- Number of images in stack
    mjdobsmin double precision NOT NULL,   -- Minimum MJD of input images in stack
    mjdobsmax double precision NOT NULL,   -- Maximum MJD of input images in stack
    npixnan integer NOT NULL,              -- Number of NaN pixels in reference image
    clmean real NOT NULL,                  -- Image pixel mean after data clipping
    clstddev real NOT NULL,                -- Image pixel standard deviation after data clipping and reinflating
    clnoutliers integer NOT NULL,          -- Number of image pixels discarded in data clipping
    gmedian real NOT NULL,                 -- Global image pixel median
    datascale real NOT NULL,               -- Global robust image pixel spread = 0.5*(p84-p16)
    gmin real NOT NULL,                    -- Global minimum image pixel value
    gmax real NOT NULL,                    -- Global maximum image pixel value
    cov5percent real NOT NULL,             -- QA metric to measure coverage depth of at least 5
    medncov real NOT NULL,                 -- Median of corresponding depth-of-coverage image
    medpixunc real NOT NULL,               -- Median of corresponding uncertainty image
    fwhmmedpix real NOT NULL,              -- Median of FWHM_IMAGE values in reference-image SExtractor catalog [pixels]
    fwhmminpix real NOT NULL,              -- Minimum of FWHM_IMAGE values in reference-image SExtractor catalog [pixels]
    fwhmmaxpix real NOT NULL,              -- Maximum of FWHM_IMAGE values in reference-image SExtractor catalog [pixels]
    nsxcatsources integer NOT NULL,        -- Number of sources in reference-image SExtractor catalog
    npucatsources integer NOT NULL         -- Number of sources in reference-image PhotUtils catalog
);

ALTER TABLE refimmeta OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY refimmeta ADD CONSTRAINT refimmeta_pkey PRIMARY KEY (rfid);

ALTER TABLE ONLY refimmeta ADD CONSTRAINT refimmeta_rfid_fk FOREIGN KEY (rfid) REFERENCES refimages(rfid);
ALTER TABLE ONLY refimmeta ADD CONSTRAINT refimmeta_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

CREATE INDEX refimmeta_field_idx ON refimmeta (field);
CREATE INDEX refimmeta_hp6_idx ON refimmeta (hp6);
CREATE INDEX refimmeta_hp9_idx ON refimmeta (hp9);
CREATE INDEX refimmeta_fid_idx ON refimmeta (fid);
CREATE INDEX refimmeta_nframes_idx ON refimmeta (nframes);
CREATE INDEX refimmeta_cov5percent_idx ON refimmeta (cov5percent);


-----------------------------
-- TABLE: Fields
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE fields (
    field integer NOT NULL,                -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                  -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                  -- Level-9 healpix index (NESTED) for (ra0,dec0)
    ra1 double precision NOT NULL,         -- Lower-left corner of field
    dec1 double precision NOT NULL,
    ra2 double precision NOT NULL,         -- Lower-right corner of field
    dec2 double precision NOT NULL,
    ra3 double precision NOT NULL,         -- Upper-right corner of field
    dec3 double precision NOT NULL,
    ra4 double precision NOT NULL,         -- Upper-left corner of field
    dec4 double precision NOT NULL,
    ra0 double precision NOT NULL,         -- Center of field
    dec0 double precision NOT NULL,
    CONSTRAINT fields_ra1_check CHECK (((ra1 >= 0.0) AND (ra1 < 360.0))),
    CONSTRAINT fields_dec1_check CHECK (((dec1 >= -90.0) AND (dec1 <= 90.0))),
    CONSTRAINT fields_ra2_check CHECK (((ra2 >= 0.0) AND (ra2 < 360.0))),
    CONSTRAINT fields_dec2_check CHECK (((dec2 >= -90.0) AND (dec2 <= 90.0))),
    CONSTRAINT fields_ra3_check CHECK (((ra3 >= 0.0) AND (ra3 < 360.0))),
    CONSTRAINT fields_dec3_check CHECK (((dec3 >= -90.0) AND (dec3 <= 90.0))),
    CONSTRAINT fields_ra4_check CHECK (((ra4 >= 0.0) AND (ra4 < 360.0))),
    CONSTRAINT fields_dec4_check CHECK (((dec4 >= -90.0) AND (dec4 <= 90.0))),
    CONSTRAINT fields_ra0_check CHECK (((ra0 >= 0.0) AND (ra0 < 360.0))),
    CONSTRAINT fields_dec0_check CHECK (((dec0 >= -90.0) AND (dec0 <= 90.0)))
);

ALTER TABLE fields OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE index fields_field_idx on fields(field);

CREATE INDEX fields_radec_idx ON fields (q3c_ang2ipix(ra0, dec0));
CLUSTER fields_radec_idx ON fields;
ANALYZE fields;


-----------------------------
-- TABLE: ProcReqs
--
-- Keeps track of processing requests.  The request ID is combined with the  processing
-- date to form a unique S3-bucket subdirectory (e.g., 20260831_req12314).  Columns
-- obsstarttime and obsendtime correspond to the following environment variables:
-- export STARTDATETIME="2027-10-07 07:12:00"
-- export ENDDATETIME="2027-10-08 00:00:00"
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE procreqs (
    reqid integer NOT NULL,
    obsstarttime timestamp,
    obsendtime timestamp,
    started timestamp,
    ended timestamp,
    elapsed interval,
    status smallint DEFAULT 0,
    CONSTRAINT procreqs_status_check CHECK (((status >= -1) AND (status <= 1)))
);

ALTER TABLE procreqs OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE SEQUENCE procreqs_reqid_seq
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER TABLE procreqs_reqid_seq OWNER TO rapidadminrole;

ALTER TABLE procreqs ALTER COLUMN reqid SET DEFAULT nextval('procreqs_reqid_seq'::regclass);

ALTER TABLE ONLY procreqs ADD CONSTRAINT procreqs_pkey PRIMARY KEY (reqid);

CREATE INDEX procreqs_obsstarttime_idx ON procreqs (obsstarttime);
CREATE INDEX procreqs_obsendtime_idx ON procreqs (obsendtime);
CREATE INDEX procreqs_status_idx ON procreqs (status);
CREATE INDEX procreqs_started_idx ON procreqs (started);
CREATE INDEX procreqs_ended_idx ON procreqs (ended);

-- ======================================================================
-- source: database/schema/rapidOpsTableGrants.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsTableGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------


-------------------
-- Filters table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE filters FROM rapidreadrole;
GRANT SELECT ON TABLE filters TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE filters FROM rapidadminrole;
GRANT ALL ON TABLE filters TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE filters FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE filters TO rapidporole;


-------------------
-- SCAs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE scas FROM rapidreadrole;
GRANT SELECT ON TABLE scas TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE scas FROM rapidadminrole;
GRANT ALL ON TABLE scas TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE scas FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE scas TO rapidporole;


-------------------
-- Exposures table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE exposures FROM rapidreadrole;
GRANT SELECT ON TABLE exposures TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE exposures_expid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE exposures FROM rapidadminrole;
GRANT ALL ON TABLE exposures TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE exposures_expid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE exposures_expid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE exposures FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE exposures TO rapidporole;

REVOKE ALL ON SEQUENCE exposures_expid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE exposures_expid_seq TO rapidporole;


-------------------
-- L2files table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE l2files FROM rapidreadrole;
GRANT SELECT ON TABLE l2files TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE l2files_rid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE l2files FROM rapidadminrole;
GRANT ALL ON TABLE l2files TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE l2files_rid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE l2files_rid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE l2files FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE l2files TO rapidporole;

REVOKE ALL ON SEQUENCE l2files_rid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE l2files_rid_seq TO rapidporole;


-------------------
-- L2fileMeta table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE l2filemeta FROM rapidreadrole;
GRANT SELECT ON TABLE l2filemeta TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE l2filemeta FROM rapidadminrole;
GRANT ALL ON TABLE l2filemeta TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE l2filemeta FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE l2filemeta TO rapidporole;


-------------------
-- Pipelines table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE pipelines FROM rapidreadrole;
GRANT SELECT ON TABLE pipelines TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE pipelines FROM rapidadminrole;
GRANT ALL ON TABLE pipelines TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE pipelines FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE pipelines TO rapidporole;


-------------------
-- SwVersions table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE swversions FROM rapidreadrole;
GRANT SELECT ON TABLE swversions TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE swversions_svid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE swversions FROM rapidadminrole;
GRANT ALL ON TABLE swversions TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE swversions_svid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE swversions_svid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE swversions FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE swversions TO rapidporole;

REVOKE ALL ON SEQUENCE swversions_svid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE swversions_svid_seq TO rapidporole;


-------------------
-- ArchiveVersions table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE archiveversions FROM rapidreadrole;
GRANT SELECT ON TABLE archiveversions TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE archiveversions_avid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE archiveversions FROM rapidadminrole;
GRANT ALL ON TABLE archiveversions TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE archiveversions_avid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE archiveversions_avid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE archiveversions FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE archiveversions TO rapidporole;

REVOKE ALL ON SEQUENCE archiveversions_avid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE archiveversions_avid_seq TO rapidporole;


-------------------
-- RefImages table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE refimages FROM rapidreadrole;
GRANT SELECT ON TABLE refimages TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE refimages_rfid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE refimages FROM rapidadminrole;
GRANT ALL ON TABLE refimages TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE refimages_rfid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE refimages_rfid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE refimages FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE refimages TO rapidporole;

REVOKE ALL ON SEQUENCE refimages_rfid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE refimages_rfid_seq TO rapidporole;


-------------------
-- DiffImages table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE diffimages FROM rapidreadrole;
GRANT SELECT ON TABLE diffimages TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE diffimages_pid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE diffimages FROM rapidadminrole;
GRANT ALL ON TABLE diffimages TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE diffimages_pid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE diffimages_pid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE diffimages FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE diffimages TO rapidporole;

REVOKE ALL ON SEQUENCE diffimages_pid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE diffimages_pid_seq TO rapidporole;


-------------------
-- AlertNames table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE alertnames FROM rapidreadrole;
GRANT SELECT ON TABLE alertnames TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE alertnames_an24id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an25id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an26id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an27id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an28id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an29id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an30id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an31id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an32id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an33id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an34id_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE alertnames FROM rapidadminrole;
GRANT ALL ON TABLE alertnames TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE alertnames_an24id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an25id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an26id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an27id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an28id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an29id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an30id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an31id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an32id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an33id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an34id_seq FROM rapidadminrole;

GRANT ALL ON SEQUENCE alertnames_an24id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an25id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an26id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an27id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an28id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an29id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an30id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an31id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an32id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an33id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an34id_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE alertnames FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE alertnames TO rapidporole;

REVOKE ALL ON SEQUENCE alertnames_an24id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an25id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an26id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an27id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an28id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an29id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an30id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an31id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an32id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an33id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an34id_seq FROM rapidporole;

GRANT USAGE ON SEQUENCE alertnames_an24id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an25id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an26id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an27id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an28id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an29id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an30id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an31id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an32id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an33id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an34id_seq TO rapidporole;


-------------------
-- Jobs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE jobs FROM rapidreadrole;
GRANT SELECT ON TABLE jobs TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE jobs_jid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE jobs FROM rapidadminrole;
GRANT ALL ON TABLE jobs TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE jobs_jid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE jobs_jid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE jobs FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE jobs TO rapidporole;

REVOKE ALL ON SEQUENCE jobs_jid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE jobs_jid_seq TO rapidporole;


-------------------
-- RefImCatalogs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE refimcatalogs FROM rapidreadrole;
GRANT SELECT ON TABLE refimcatalogs TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE refimcatalogs_rfcatid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE refimcatalogs FROM rapidadminrole;
GRANT ALL ON TABLE refimcatalogs TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE refimcatalogs_rfcatid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE refimcatalogs_rfcatid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE refimcatalogs FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE refimcatalogs TO rapidporole;

REVOKE ALL ON SEQUENCE refimcatalogs_rfcatid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE refimcatalogs_rfcatid_seq TO rapidporole;


-------------------
-- Refimimages table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE refimimages FROM rapidreadrole;
GRANT SELECT ON TABLE refimimages TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE refimimages FROM rapidadminrole;
GRANT ALL ON TABLE refimimages TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE refimimages FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE refimimages TO rapidporole;


-------------------
-- SOCProcs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE socprocs FROM rapidreadrole;
GRANT SELECT ON TABLE socprocs TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE socprocs_did_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE socprocs FROM rapidadminrole;
GRANT ALL ON TABLE socprocs TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE socprocs_did_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE socprocs_did_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE socprocs FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE socprocs TO rapidporole;

REVOKE ALL ON SEQUENCE socprocs_did_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE socprocs_did_seq TO rapidporole;


-------------------
-- PSFs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE psfs FROM rapidreadrole;
GRANT SELECT ON TABLE psfs TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE psfs_psfid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE psfs FROM rapidadminrole;
GRANT ALL ON TABLE psfs TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE psfs_psfid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE psfs_psfid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE psfs FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE psfs TO rapidporole;

REVOKE ALL ON SEQUENCE psfs_psfid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE psfs_psfid_seq TO rapidporole;


-------------------
-- DiffImMeta table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE diffimmeta FROM rapidreadrole;
GRANT SELECT ON TABLE diffimmeta TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE diffimmeta FROM rapidadminrole;
GRANT ALL ON TABLE diffimmeta TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE diffimmeta FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE diffimmeta TO rapidporole;


-------------------
-- RefImMeta table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE refimmeta FROM rapidreadrole;
GRANT SELECT ON TABLE refimmeta TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE refimmeta FROM rapidadminrole;
GRANT ALL ON TABLE refimmeta TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE refimmeta FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE refimmeta TO rapidporole;


-------------------
-- Fields table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE fields FROM rapidreadrole;
GRANT SELECT ON TABLE fields TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE fields FROM rapidadminrole;
GRANT ALL ON TABLE fields TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE fields FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE fields TO rapidporole;


-------------------
-- ProcReqs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE procreqs FROM rapidreadrole;
GRANT SELECT ON TABLE procreqs TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE procreqs_reqid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE procreqs FROM rapidadminrole;
GRANT ALL ON TABLE procreqs TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE procreqs_reqid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE procreqs_reqid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE procreqs FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE procreqs TO rapidporole;

REVOKE ALL ON SEQUENCE procreqs_reqid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE procreqs_reqid_seq TO rapidporole;

-- ======================================================================
-- source: database/schema/rapidOpsSourcesTable.sql
-- ======================================================================

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
-- Parent sources table for creating child tables, one for each combination of observation date and sca.
-- Inheritance, for sources child tables only, is needed because a given source ID looked up in the
-- merges table (see below) cannot be easily traced to the child table in which it is stored.
-- No records are directly inserted into the parent table.
--

-- https://photutils.readthedocs.io/en/stable/api/photutils.psf.PSFPhotometry.html#photutils.psf.PSFPhotometry
-- https://photutils.readthedocs.io/en/stable/api/photutils.detection.DAOStarFinder.html#photutils.detection.DAOStarFinder

-----------------------------
-- TABLE: Sources
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE sources (
    sid bigint NOT NULL,                       -- Database unique primary key
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
    redchi real NOT NULL DEFAULT 0.0,          -- Reduced chi2
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
    mjdobs double precision NOT NULL,          -- MJD OBS of exposure
    rb real                                    -- Null means realbogus not executed
);

-- Sources table must be owned by rapidporole for inheritance.
-- ALTER TABLE sources OWNER TO rapidadminrole;
ALTER TABLE sources OWNER TO rapidporole;

CREATE SEQUENCE sources_sid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE sources_sid_seq OWNER TO rapidadminrole;

ALTER TABLE sources ALTER COLUMN sid SET DEFAULT nextval('sources_sid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY sources ADD CONSTRAINT sources_pkey PRIMARY KEY (sid);

ALTER TABLE ONLY sources ADD CONSTRAINT sourcespk UNIQUE (pid, id, isdiffpos);

ALTER TABLE ONLY sources ADD CONSTRAINT sources_pid_fk FOREIGN KEY (pid) REFERENCES diffimages(pid);

CREATE INDEX sources_pid_idx ON sources (pid);
CREATE INDEX sources_expid_idx ON sources (expid);
CREATE INDEX sources_sca_idx ON sources (sca);
CREATE INDEX sources_field_idx ON sources (field);
CREATE INDEX sources_flags_idx ON sources (flags);
CREATE INDEX sources_mjdobs_idx ON sources (mjdobs);

ALTER TABLE sources SET UNLOGGED;


------------------------------------------------------------
-- A python script will create child tables like the parent sources table.
-- Child-table names will be sources_<observing date: yyyymmdd>_<sca>.
-- The observation date is in UT.
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

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE merges (
    aid bigint NOT NULL,
    sid bigint NOT NULL
);

ALTER TABLE merges OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE INDEX merges_aid_idx ON merges USING btree (aid);
CREATE INDEX merges_sid_idx ON merges USING btree (sid);


-----------------------------
-- TABLE: AstroObjects
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE astroobjects (
    aid bigint NOT NULL,
    ra0 double precision NOT NULL,              -- RA corresponding to initial sky position
    dec0 double precision NOT NULL,             -- Dec corresponding to initial sky position
    flux0 real NOT NULL                         -- Flux of initial sky position
);

ALTER TABLE astroobjects OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY astroobjects ADD CONSTRAINT astroobjects_pkey PRIMARY KEY (aid);


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


-----------------------------
-- TABLE: AstroObjectsMeta
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE astroobjectsmeta (
    aid bigint NOT NULL,
    meanra double precision NOT NULL,           -- Mean RA
    stdevra real NOT NULL,                      -- Standard deviation of RA
    meandec double precision NOT NULL,          -- Mean Dec
    stdevdec real NOT NULL,                     -- Standard deviation of Dec
    meanflux real NOT NULL,                     -- Mean flux
    stdevflux real NOT NULL,                    -- Standard deviation of flux
    nsources smallint NOT NULL                  -- Total number of sources (all filters)
);

ALTER TABLE astroobjectsmeta OWNER TO rapidadminrole;

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY astroobjectsmeta ADD CONSTRAINT astroobjectsmeta_pkey PRIMARY KEY (aid);

CREATE INDEX astroobjectsmeta_nsources_idx ON astroobjectsmeta (nsources);


-----------------------------
-- TABLE: XSources for storing select columns from SExtractor catalogs with other metadata for lightcurve formation.
--
-- Parent sources table for creating child tables, one for each combination of observation date and sca.
--
-- If Scorr or SFFT cconv are used as the detection image, then all the shape parameters
-- are computed from that match-filtered detection image, not the direct diff image.
-- This applies to A/BWIN_IMAGE and FWHM_IMAGE.
-----------------------------

-- SET default_tablespace = pipeline_data_01;  -- OMITTED: no tablespace created in baseline (see header)
CREATE TABLE xsources (
    xsid bigint NOT NULL,                      -- Database unique primary key
    num integer NOT NULL,                      -- Non-unique number column in SExtractor catalog file in S3 bucket
    pid integer NOT NULL,                      -- DiffImages primary key
    isdiffpos boolean NOT NULL DEFAULT TRUE,   -- t = positive difference, f = negative difference
    ra double precision NOT NULL,              -- ALPHAWIN_J2000
    dec double precision NOT NULL,             -- DELTAWIN_J2000
    x real NOT NULL,                           -- XWIN_IMAGE (one-based image-pixel coordinate)
    y real NOT NULL,                           -- YWIN_IMAGE (one-based image-pixel coordinate)
    fluxap real NOT NULL,                      -- FLUX_APER_0 (or simply FLUX_APER)
    fluxap1 real NOT NULL,                     -- FLUX_APER_1
    fluxap2 real NOT NULL,                     -- FLUX_APER_2
    fluxap3 real NOT NULL,                     -- FLUX_APER_3
    fluxap4 real NOT NULL,                     -- FLUX_APER_4
    fluxap5 real NOT NULL,                     -- FLUX_APER_5
    fluxerrap real NOT NULL,                   -- FLUXERR_APER_0 (or simply FLUXERR_APER)
    fluxerrap1 real NOT NULL,                  -- FLUXERR_APER_1
    fluxerrap2 real NOT NULL,                  -- FLUXERR_APER_2
    fluxerrap3 real NOT NULL,                  -- FLUXERR_APER_3
    fluxerrap4 real NOT NULL,                  -- FLUXERR_APER_4
    fluxerrap5 real NOT NULL,                  -- FLUXERR_APER_5
    awinworld real NOT NULL,                   -- AWIN_WORLD
    bwinworld real NOT NULL,                   -- BWIN_WORLD
    awinimage real NOT NULL,                   -- AWIN_IMAGE
    bwinimage real NOT NULL,                   -- BWIN_IMAGE
    fwhmimage real NOT NULL,                   -- FWHM_IMAGE
    classstar real NOT NULL,                   -- CLASS_STAR
    flags smallint NOT NULL,                   -- SExtractor bitwise flags
    field integer NOT NULL,                    -- Roman tessellation index for (ra,dec)
    hp6 integer NOT NULL,                      -- Level-6 healpix index (NESTED) for (ra,dec)
    hp9 integer NOT NULL,                      -- Level-9 healpix index (NESTED) for (ra,dec)
    expid integer NOT NULL,                    -- Exposures primary key
    fid smallint NOT NULL,                     -- Filter ID
    sca smallint NOT NULL,                     -- SCA number (1...18)
    mjdobs double precision NOT NULL,          -- MJD OBS of exposure
    rb real                                    -- Null means realbogus not executed
);

-- XSources table must be owned by rapidporole for inheritance.
-- ALTER TABLE xsources OWNER TO rapidadminrole;
ALTER TABLE xsources OWNER TO rapidporole;

CREATE SEQUENCE xsources_xsid_seq
    START WITH 1
    INCREMENT BY 1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

ALTER SEQUENCE xsources_xsid_seq OWNER TO rapidadminrole;

ALTER TABLE xsources ALTER COLUMN xsid SET DEFAULT nextval('xsources_xsid_seq'::regclass);

-- SET default_tablespace = pipeline_indx_01;  -- OMITTED: no tablespace created in baseline (see header)
ALTER TABLE ONLY xsources ADD CONSTRAINT xsources_pkey PRIMARY KEY (xsid);

ALTER TABLE ONLY xsources ADD CONSTRAINT xsourcespk UNIQUE (pid, num, isdiffpos);

ALTER TABLE ONLY xsources ADD CONSTRAINT xsources_pid_fk FOREIGN KEY (pid) REFERENCES diffimages(pid);

CREATE INDEX xsources_pid_idx ON xsources (pid);
CREATE INDEX xsources_expid_idx ON xsources (expid);
CREATE INDEX xsources_sca_idx ON xsources (sca);
CREATE INDEX xsources_field_idx ON xsources (field);
CREATE INDEX xsources_flags_idx ON xsources (flags);
CREATE INDEX xsources_mjdobs_idx ON xsources (mjdobs);

ALTER TABLE xsources SET UNLOGGED;

-- ======================================================================
-- source: database/schema/rapidOpsSourcesTableGrants.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSourcesTableGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 8 August 2025
--------------------------------------------------------------------------------------------------------------------------


-------------------
-- Allow pipeline software to create sources and astroobjects like-tables:
-------------------

-- OMITTED (account-specific login or no tablespace in baseline): GRANT CREATE ON SCHEMA public TO rapidporuss;
-- OMITTED (account-specific login or no tablespace in baseline): GRANT CREATE ON TABLESPACE pipeline_data_01 TO rapidporuss;
-- OMITTED (account-specific login or no tablespace in baseline): GRANT CREATE ON TABLESPACE pipeline_indx_01 TO rapidporuss;

GRANT USAGE, CREATE ON SCHEMA public TO rapidporole;
-- OMITTED (account-specific login or no tablespace in baseline): GRANT CREATE ON TABLESPACE pipeline_data_01 TO rapidporole;
-- OMITTED (account-specific login or no tablespace in baseline): GRANT CREATE ON TABLESPACE pipeline_indx_01 TO rapidporole;


-------------------
-- Sources table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE sources FROM rapidreadrole;
GRANT SELECT ON TABLE sources TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE sources_sid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE sources FROM rapidadminrole;
GRANT ALL ON TABLE sources TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE sources_sid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE sources_sid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE sources FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE sources TO rapidporole;

REVOKE ALL ON SEQUENCE sources_sid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE sources_sid_seq TO rapidporole;


-------------------
-- AstroObjects table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE astroobjects FROM rapidreadrole;
GRANT SELECT ON TABLE astroobjects TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE astroobjects FROM rapidadminrole;
GRANT ALL ON TABLE astroobjects TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE astroobjects FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE astroobjects TO rapidporole;


-------------------
-- AstroObjectsMeta table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE astroobjectsmeta FROM rapidreadrole;
GRANT SELECT ON TABLE astroobjectsmeta TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE astroobjectsmeta FROM rapidadminrole;
GRANT ALL ON TABLE astroobjectsmeta TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE astroobjectsmeta FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE astroobjectsmeta TO rapidporole;


-------------------
-- Merges table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE merges FROM rapidreadrole;
GRANT SELECT ON TABLE merges TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE merges FROM rapidadminrole;
GRANT ALL ON TABLE merges TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE merges FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE merges TO rapidporole;


-------------------
-- XSources table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE xsources FROM rapidreadrole;
GRANT SELECT ON TABLE xsources TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE xsources_xsid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE xsources FROM rapidadminrole;
GRANT ALL ON TABLE xsources TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE xsources_xsid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE xsources_xsid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE xsources FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE xsources TO rapidporole;

REVOKE ALL ON SEQUENCE xsources_xsid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE xsources_xsid_seq TO rapidporole;

-- ======================================================================
-- source: database/schema/rapidOpsProcs.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsProcs.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 16 April 2024
--------------------------------------------------------------------------------------------------------------------------


-- Insert a new record into the Exposures table or update existing one.
--
create function addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint
)
    returns record as $$

    declare

        r_               record;
        fid_             smallint;
        expid_           integer;
        expid__          integer;

    begin


        begin

            select fid
            into strict fid_
            from Filters
            where filter = filter_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addExposure: Filters record filter=% not found.', filter_;

        end;


        -- Insert or update record.

        expid__ := null;

        select expid
        into expid__
        from Exposures
        where dateobs = dateobs_;

        if (expid__ is null) then

            -- Insert Exposures record.

            begin

                insert into Exposures
                (dateobs,
                 mjdobs,
                 field,
                 hp6,
                 hp9,
                 fid,
                 exptime,
                 status,
                 infobits
                )
                values
                (dateobs_,
                 mjdobs_,
                 field_,
                 hp6_,
                 hp9_,
                 fid_,
                 exptime_,
                 status_,
                 infobits_
                )
                returning expid into strict expid_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in addExposure: Row could not be inserted into Exposures table.';
            end;

        else

            -- Update record in Exposures table.

            expid_ := expid__;

            update Exposures
            set dateobs = dateobs_,
                mjdobs = mjdobs_,
                field = field_,
                hp6 = hp6_,
                hp9 = hp9_,
                fid = fid_,
                exptime = exptime_,
                status = status_,
                infobits = infobits_,
                created = now()
            where expid = expid_;

        end if;

        select expid_, fid_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Insert a new record into the L2Files table.
--
create function addL2File (
    expid_                integer,
    sca_                  smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    dateobs_              timestamp without time zone,
    mjdobs_               double precision,
    exptime_              real,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint,
    crval1_               double precision,
    crval2_               double precision,
    crpix1_               real,
    crpix2_               real,
    cd11_                 double precision,
    cd12_                 double precision,
    cd21_                 double precision,
    cd22_                 double precision,
    ctype1_               character varying(16),
    ctype2_               character varying(16),
    cunit1_               character varying(16),
    cunit2_               character varying(16),
    a_order_              smallint,
    a_0_2_                double precision,
    a_0_3_                double precision,
    a_0_4_                double precision,
    a_1_1_                double precision,
    a_1_2_                double precision,
    a_1_3_                double precision,
    a_2_0_                double precision,
    a_2_1_                double precision,
    a_2_2_                double precision,
    a_3_0_                double precision,
    a_3_1_                double precision,
    a_4_0_                double precision,
    b_order_              smallint,
    b_0_2_                double precision,
    b_0_3_                double precision,
    b_0_4_                double precision,
    b_1_1_                double precision,
    b_1_2_                double precision,
    b_1_3_                double precision,
    b_2_0_                double precision,
    b_2_1_                double precision,
    b_2_2_                double precision,
    b_3_0_                double precision,
    b_3_1_                double precision,
    b_4_0_                double precision,
    equinox_              real,
    ra_                   double precision,
    dec_                  double precision,
    paobsy_               real,
    pafpa_                real,
    zptmag_               real,
    skymean_              real,
    overlapfields_        integer[] DEFAULT NULL
)
    returns record as $$

    declare

        r_               record;
        rid_              integer;
        version_          smallint;
        vbest_            smallint;

    begin

        -- Processed images are versioned according to unique (expid, sca) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updateL2File is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from L2Files
        where expid = expid_
        and sca = sca_;

        if not found then
            version_ := 1;
        end if;

        vbest_ := 0;

        -- Insert L2Files record.

        begin

            insert into L2Files
            (expid,sca,version,status,vbest,
             field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
             filename,checksum,crval1,crval2,
             crpix1,crpix2,cd11,cd12,cd21,cd22,ctype1,ctype2,
             cunit1,cunit2,a_order,a_0_2,a_0_3,a_0_4,a_1_1,a_1_2,
             a_1_3,a_2_0,a_2_1,a_2_2,a_3_0,a_3_1,a_4_0,b_order,
             b_0_2,b_0_3,b_0_4,b_1_1,b_1_2,b_1_3,b_2_0,b_2_1,
             b_2_2,b_3_0,b_3_1,b_4_0,equinox,ra,dec,paobsy,pafpa,
             zptmag,skymean,
             overlapfields
            )
            values
            (expid_,sca_,version_,status_,vbest_,
             field_,hp6_,hp9_,fid_,dateobs_,mjdobs_,exptime_,infobits_,
             filename_,checksum_,crval1_,crval2_,
             crpix1_,crpix2_,cd11_,cd12_,cd21_,cd22_,ctype1_,ctype2_,
             cunit1_,cunit2_,a_order_,a_0_2_,a_0_3_,a_0_4_,a_1_1_,a_1_2_,
             a_1_3_,a_2_0_,a_2_1_,a_2_2_,a_3_0_,a_3_1_,a_4_0_,b_order_,
             b_0_2_,b_0_3_,b_0_4_,b_1_1_,b_1_2_,b_1_3_,b_2_0_,b_2_1_,
             b_2_2_,b_3_0_,b_3_1_,b_4_0_,equinox_,ra_,dec_,paobsy_,pafpa_,
             zptmag_,skymean_,
             coalesce(overlapfields_, '{}'::integer[])
            )
            returning rid into strict rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addL2File: L2Files record for expid,sca=%,% not inserted.', expid_,sca_;

        end;

        select rid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Insert a new record into the L2Files table.
-- Overloaded function with additional fifth-order SIP-distortion coefficients.
--
create function addL2File (
    expid_                integer,
    sca_                  smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    dateobs_              timestamp without time zone,
    mjdobs_               double precision,
    exptime_              real,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint,
    crval1_               double precision,
    crval2_               double precision,
    crpix1_               real,
    crpix2_               real,
    cd11_                 double precision,
    cd12_                 double precision,
    cd21_                 double precision,
    cd22_                 double precision,
    ctype1_               character varying(16),
    ctype2_               character varying(16),
    cunit1_               character varying(16),
    cunit2_               character varying(16),
    a_order_              smallint,
    a_0_1_                double precision,
    a_0_2_                double precision,
    a_0_3_                double precision,
    a_0_4_                double precision,
    a_0_5_                double precision,
    a_1_0_                double precision,
    a_1_1_                double precision,
    a_1_2_                double precision,
    a_1_3_                double precision,
    a_1_4_                double precision,
    a_2_0_                double precision,
    a_2_1_                double precision,
    a_2_2_                double precision,
    a_2_3_                double precision,
    a_3_0_                double precision,
    a_3_1_                double precision,
    a_3_2_                double precision,
    a_4_0_                double precision,
    a_4_1_                double precision,
    a_5_0_                double precision,
    b_order_              smallint,
    b_0_1_                double precision,
    b_0_2_                double precision,
    b_0_3_                double precision,
    b_0_4_                double precision,
    b_0_5_                double precision,
    b_1_0_                double precision,
    b_1_1_                double precision,
    b_1_2_                double precision,
    b_1_3_                double precision,
    b_1_4_                double precision,
    b_2_0_                double precision,
    b_2_1_                double precision,
    b_2_2_                double precision,
    b_2_3_                double precision,
    b_3_0_                double precision,
    b_3_1_                double precision,
    b_3_2_                double precision,
    b_4_0_                double precision,
    b_4_1_                double precision,
    b_5_0_                double precision,
    equinox_              real,
    ra_                   double precision,
    dec_                  double precision,
    paobsy_               real,
    pafpa_                real,
    zptmag_               real,
    skymean_              real,
    overlapfields_        integer[] DEFAULT NULL
)
    returns record as $$

    declare

        r_               record;
        rid_              integer;
        version_          smallint;
        vbest_            smallint;

    begin

        -- Processed images are versioned according to unique (expid, sca) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updateL2File is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from L2Files
        where expid = expid_
        and sca = sca_;

        if not found then
            version_ := 1;
        end if;

        vbest_ := 0;

        -- Insert L2Files record.

        begin

            insert into L2Files
            (expid,sca,version,status,vbest,
             field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
             filename,checksum,crval1,crval2,
             crpix1,crpix2,cd11,cd12,cd21,cd22,ctype1,ctype2,
             cunit1,cunit2,a_order,a_0_1,a_0_2,a_0_3,a_0_4,a_0_5,a_1_0,a_1_1,a_1_2,
             a_1_3,a_1_4,a_2_0,a_2_1,a_2_2,a_2_3,a_3_0,a_3_1,a_3_2,a_4_0,a_4_1,a_5_0,b_order,
             b_0_1,b_0_2,b_0_3,b_0_4,b_0_5,b_1_0,b_1_1,b_1_2,b_1_3,b_1_4,b_2_0,b_2_1,
             b_2_2,b_2_3,b_3_0,b_3_1,b_3_2,b_4_0,b_4_1,b_5_0,equinox,ra,dec,paobsy,pafpa,
             zptmag,skymean,
             overlapfields
            )
            values
            (expid_,sca_,version_,status_,vbest_,
             field_,hp6_,hp9_,fid_,dateobs_,mjdobs_,exptime_,infobits_,
             filename_,checksum_,crval1_,crval2_,
             crpix1_,crpix2_,cd11_,cd12_,cd21_,cd22_,ctype1_,ctype2_,
             cunit1_,cunit2_,a_order_,a_0_1_,a_0_2_,a_0_3_,a_0_4_,a_0_5_,a_1_0_,a_1_1_,a_1_2_,
             a_1_3_,a_1_4_,a_2_0_,a_2_1_,a_2_2_,a_2_3_,a_3_0_,a_3_1_,a_3_2_,a_4_0_,a_4_1_,a_5_0_,b_order_,
             b_0_1_,b_0_2_,b_0_3_,b_0_4_,b_0_5_,b_1_0_,b_1_1_,b_1_2_,b_1_3_,b_1_4_,b_2_0_,b_2_1_,
             b_2_2_,b_2_3_,b_3_0_,b_3_1_,b_3_2_,b_4_0_,b_4_1_,b_5_0_,equinox_,ra_,dec_,paobsy_,pafpa_,
             zptmag_,skymean_,
             coalesce(overlapfields_, '{}'::integer[])
            )
            returning rid into strict rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addL2File: L2Files record for expid,sca=%,% not inserted.', expid_,sca_;

        end;

        select rid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a L2Files record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updateL2File (
    rid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
)

    returns void as $$

    declare

        rid__            integer;
        currentVBest_    smallint;
        expid_           integer;
        sca_             smallint;
        vBest_           smallint;
        bestIs2_         boolean;
        count_           integer;

    begin

        bestIs2_ := 'f';

        -- First, get the expid, sca for the L2 image.

        begin

            select expid, sca
            into strict expid_, sca_
            from l2files
            where rid = rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateL2File: L2Files record not found for rid=%', rid_;

        end;

        -- If this isn't the first L2Files record
        -- for the exposure and sca, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new L2Files record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the L2Files record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from L2Files
        where expid = expid_
        and sca = sca_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updateL2File: More than one L2Files record with vBest>0 returned.';
            end if;

            select rid, vBest
            into rid__, currentVBest_
            from L2Files
            where expid = expid_
            and sca = sca_
            and vBest in (1, 2);

            if (currentvBest_ = 1) then -- vBest is not locked
                update L2Files
                set vBest = 0
                where rid = rid__;

                if not found then
                    raise exception '*** Error in updateL2File: Cannot update L2Files record.';
                end if;
            else
                bestIs2_ := 't';
            end if;

        end if;

        if bestIs2_ = 't' then
            vBest_ := 0;
        else
            vBest_ := 1;
        end if;

        update L2Files
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where rid = rid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updateL2File: Cannot update L2Files record for rid=%', rid_;

    end;

$$ language plpgsql;


-- Insert a new record into or update an existing record in the L2FileMeta table.
--
create function registerL2FileMeta (
    rid_                 integer,
    fid_                 smallint,
    sca_                 smallint,
    ra0_                 double precision,
    dec0_                double precision,
    ra1_                 double precision,
    dec1_                double precision,
    ra2_                 double precision,
    dec2_                double precision,
    ra3_                 double precision,
    dec3_                double precision,
    ra4_                 double precision,
    dec4_                double precision,
    x_                   double precision,
    y_                   double precision,
    z_                   double precision,
    hp6_                 integer,
    hp9_                 integer,
    mjdobs_              double precision
)
    returns void as $$

    declare

        rid__    integer;

    begin


        -- Insert or update record, as appropriate.

        select rid
        into rid__
        from L2FileMeta
        where rid = rid_;

        if not found then


            -- Insert L2FileMeta record.

            begin

                insert into L2FileMeta
                (rid,
                 fid,
                 sca,
                 ra0,
                 dec0,
                 ra1,
                 dec1,
                 ra2,
                 dec2,
                 ra3,
                 dec3,
                 ra4,
                 dec4,
                 x,
                 y,
                 z,
                 hp6,
                 hp9,
                 mjdobs
                )
                values
                (rid_,
                 fid_,
                 sca_,
                 ra0_,
                 dec0_,
                 ra1_,
                 dec1_,
                 ra2_,
                 dec2_,
                 ra3_,
                 dec3_,
                 ra4_,
                 dec4_,
                 x_,
                 y_,
                 z_,
                 hp6_,
                 hp9_,
                 mjdobs_
                );
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerL2FileMeta: L2FileMeta record for rid=% not inserted.', rid_;

            end;

        else


            -- Update L2FileMeta record.

            update L2FileMeta
            set fid = fid_,
                sca = sca_,
                ra0 = ra0_,
                dec0 = dec0_,
                ra1 = ra1_,
                dec1 = dec1_,
                ra2 = ra2_,
                dec2 = dec2_,
                ra3 = ra3_,
                dec3 = dec3_,
                ra4 = ra4_,
                dec4 = dec4_,
                x = x_,
                y = y_,
                z = z_,
                hp6 = hp6_,
                hp9 = hp9_,
                mjdobs = mjdobs_
            where rid = rid_;

        end if;

    end;

$$ language plpgsql;


-- Insert a new record into the DiffImages table.
--
create function addDiffImage (
    rid_                  integer,
    ppid_                 smallint,
    rfid_                 integer,
    infobitssci_          integer,
    infobitsref_          integer,
    ra0_                  double precision,
    dec0_                 double precision,
    ra1_                  double precision,
    dec1_                 double precision,
    ra2_                  double precision,
    dec2_                 double precision,
    ra3_                  double precision,
    dec3_                 double precision,
    ra4_                  double precision,
    dec4_                 double precision,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
)
    returns record as $$

    declare

        r_                record;
        pid_              integer;
        version_          smallint;
        vbest_            smallint;
        svid_             smallint;
        expid_            integer;
        sca_              smallint;
        field_            integer;
        hp6_              integer;
        hp9_              integer;
        fid_              smallint;
        mjdobs_           double precision;
        jd_               double precision;

    begin

        -- Difference images are versioned according to unique (rid, ppid) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updateDiffImage is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from DiffImages
        where rid = rid_
        and ppid = ppid_;

        if not found then
            version_ := 1;
        end if;

        -- Get foreign-key values and other quantities for table normalization.

        begin

            select expid, sca, field, hp6, hp9, fid, mjdobs
            into strict expid_, sca_, field_, hp6_, hp9_, fid_, mjdobs_
            from L2Files
            where rid = rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addProcImage: RawImages record rid=% not found.', rid_;

        end;

        jd_ := mjdobs_ + 2400000.5;

        -- Get software version number.

        begin

            select svid
            into strict svid_
            from SwVersions
            order by svid desc
            limit 1;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addDiffImage: SwVersions record not found.';

        end;

        vbest_ := 0;

        -- Insert DiffImages record.

        begin

            insert into DiffImages
            (rid, ppid, version, status, vbest, filename, checksum,
             expid, sca, field, hp6, hp9, fid, jd, svid,
             rfid, infobitssci, infobitsref,
             ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4
            )
            values
            (rid_, ppid_, version_, status_, vbest_, filename_, checksum_,
             expid_, sca_, field_, hp6_, hp9_, fid_, jd_, svid_,
             rfid_, infobitssci_, infobitsref_,
             ra0_, dec0_, ra1_, dec1_, ra2_, dec2_, ra3_, dec3_, ra4_, dec4_
            )
            returning pid into strict pid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addDiffImage: DiffImages record for rid,ppid=%,% not inserted.', rid_,ppid_;

        end;

        select pid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a DiffImages record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updateDiffImage (
    pid_         integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
)

    returns void as $$

    declare

        pid__            integer;
        currentVBest_    smallint;
        rid_             integer;
        ppid_            smallint;
        vBest_           smallint;
        bestIs2_         boolean;
        count_           integer;

    begin

        bestIs2_ := 'f';

        -- First, get the rid, ppid for the difference image.

        begin

            select rid, ppid
            into strict rid_, ppid_
            from DiffImages
            where pid = pid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateDiffImage: DiffImages record not found for pid=%', pid_;

        end;

        -- If this isn't the first DiffImages record
        -- for the exposure and sca, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new DiffImages record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the DiffImages record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from DiffImages
        where rid = rid_
        and ppid = ppid_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updateDiffImage: More than one DiffImages record with vBest>0 returned.';
            end if;

            select pid, vBest
            into pid__, currentVBest_
            from DiffImages
            where rid = rid_
            and ppid = ppid_
            and vBest in (1, 2);

            if (currentvBest_ = 1) then -- vBest is not locked
                update DiffImages
                set vBest = 0
                where pid = pid__;

                if not found then
                    raise exception '*** Error in updateDiffImage: Cannot update DiffImages record.';
                end if;
            else
                bestIs2_ := 't';
            end if;

        end if;

        if bestIs2_ = 't' then
            vBest_ := 0;
        else
            vBest_ := 1;
        end if;

        update DiffImages
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where pid = pid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updateDiffImage: Cannot update DiffImages record for pid=%', pid_;

    end;

$$ language plpgsql;


-- Insert a new record into the RefImages table.
--
create function addRefImage (
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    ppid_                 smallint,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
)
    returns record as $$

    declare

        r_                record;
        rfid_             integer;
        version_          smallint;
        vbest_            smallint;
        svid_             smallint;

    begin

        -- Reference images are versioned according to unique (field, fid, ppid) quartets.

        -- Note that the vBest flag is updated when database stored
        -- function updateRefImage is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from RefImages
        where field = field_
        and fid = fid_
        and ppid = ppid_;

        if not found then
            version_ := 1;
        end if;

        -- Get software version number.

        begin

            select svid
            into strict svid_
            from SwVersions
            order by svid desc
            limit 1;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addRefImage: SwVersions record not found.';

        end;

        vbest_ := 0;

        -- Insert RefImages record.

        begin

            insert into RefImages
            (field, hp6, hp9, fid, ppid, version, status, vbest, filename, checksum, infobits, svid)
            values
            (field_, hp6_, hp9_, fid_, ppid_, version_, status_, vbest_, filename_, checksum_, infobits_, svid_)
            returning rfid into strict rfid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addRefImage: RefImages record for field,fid,ppid=%,%,% not inserted.', field_,fid_,ppid_;

        end;

        select rfid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a RefImages record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updateRefImage (
    rfid_        integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
)

    returns void as $$

    declare

        rfid__            integer;
        currentVBest_     smallint;
        field_            integer;
        fid_              smallint;
        ppid_             smallint;
        vBest_            smallint;
        bestIs2_          boolean;
        count_            integer;

    begin

        bestIs2_ := 'f';

        -- First, get the field, fid, ppid for the reference image.

        begin

            select field, fid, ppid
            into strict field_, fid_, ppid_
            from RefImages
            where rfid = rfid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateRefImage: RefImages record not found for rfid=%', rfid_;

        end;

        -- If this isn't the first RefImages record
        -- for the field, filter, and ppid, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new RefImages record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the RefImages record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from RefImages
        where field = field_
        and fid = fid_
        and ppid = ppid_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updateRefImage: More than one RefImages record with vBest>0 returned.';
            end if;

            select rfid, vBest
            into rfid__, currentVBest_
            from RefImages
            where field = field_
            and fid = fid_
            and ppid = ppid_
            and vBest in (1, 2);

            if (currentVBest_ = 1) then -- vBest is not locked
                update RefImages
                set vBest = 0
                where rfid = rfid__;

                if not found then
                    raise exception '*** Error in updateRefImage: Cannot update RefImages record.';
                end if;
            else
                bestIs2_ := 't';
            end if;

        end if;

        if bestIs2_ = 't' then
            vBest_ := 0;
        else
            vBest_ := 1;
        end if;

        update RefImages
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where rfid = rfid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updateRefImage: Cannot update RefImages record for rfid=%', rfid_;

    end;

$$ language plpgsql;


-- Insert a new record into the AlertNames table.
--
create function addAlertName (
    name_   char(12),
    sca_    smallint,
    field_  integer,
    hp6_    integer,
    hp9_    integer,
    ra_     double precision,
    dec_    double precision,
    jd_     double precision,
    candId_ bigint
)
    returns void as $$

    begin


        -- Insert AlertNames record.

        begin

            insert into AlertNames (name, sca, field, hp6, hp9, ra, dec, jd, candId)
            values (name_, sca_, field_, hp6_, hp9_, ra_, dec_, jd_, candId_);
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addAlertName: AlertNames record for name=% not inserted.', name_;

        end;

    end;

$$ language plpgsql;


-- Insert a new record into the AlertNames table.
--
create function computeAlertName (
    yeartwodigits_ smallint
)
    returns char(12) as $$

    declare

        anId_    bigint;
        start_   bigint;
        num_     bigint;
        rem_     bigint;
        res_     varchar(7);
        name_    char(12);
        let_     char(1);
        c        char(1)[];

    begin

        c[1] := 'a';
        c[2] := 'b';
        c[3] := 'c';
        c[4] := 'd';
        c[5] := 'e';
        c[6] := 'f';
        c[7] := 'g';
        c[8] := 'h';
        c[9] := 'i';
        c[10] := 'j';
        c[11] := 'k';
        c[12] := 'l';
        c[13] := 'm';
        c[14] := 'n';
        c[15] := 'o';
        c[16] := 'p';
        c[17] := 'q';
        c[18] := 'r';
        c[19] := 's';
        c[20] := 't';
        c[21] := 'u';
        c[22] := 'v';
        c[23] := 'w';
        c[24] := 'x';
        c[25] := 'y';
        c[26] := 'z';



        -- perl -e '$start=321272407;
        -- @c = qw ( a b c d e f g h i j k l m n o p q r s t u v w x y z );
        -- $num = 0 + $start; $res=""; while ($num > 0) { $num--; $rem=$num % 26;
        -- $let = $c[$rem]; $res = $let . $res; $num = ($num - $rem) / 26; } print "$res\n";'
        -- aaaaaaa

        -- perl -e ' $start=321272407;
        -- @c = qw ( a b c d e f g h i j k l m n o p q r s t u v w x y z );
        -- $num = 8031810175 + $start; $res=""; while ($num > 0) { $num--; $rem=$num % 26;
        -- $let = $c[$rem]; $res = $let + $res . $num = ($num - $rem) / 26; } print "$res\n";'
        -- zzzzzzz

        start_ := 321272407;

        anId_ := - 1;

        if (yeartwodigits_ = 24) then
            select nextval('alertnames_an24id_seq') into anId_;
        elseif (yeartwodigits_ = 25) then
            select nextval('alertnames_an25id_seq') into anId_;
        elseif (yeartwodigits_ = 26) then
            select nextval('alertnames_an26id_seq') into anId_;
        elseif (yeartwodigits_ = 27) then
            select nextval('alertnames_an27id_seq') into anId_;
        elseif (yeartwodigits_ = 28) then
            select nextval('alertnames_an28id_seq') into anId_;
        elseif (yeartwodigits_ = 29) then
            select nextval('alertnames_an29id_seq') into anId_;
        elseif (yeartwodigits_ = 30) then
            select nextval('alertnames_an30id_seq') into anId_;
        elseif (yeartwodigits_ = 31) then
            select nextval('alertnames_an31id_seq') into anId_;
        elseif (yeartwodigits_ = 32) then
            select nextval('alertnames_an32id_seq') into anId_;
        elseif (yeartwodigits_ = 33) then
            select nextval('alertnames_an33id_seq') into anId_;
        elseif (yeartwodigits_ = 34) then
            select nextval('alertnames_an34id_seq') into anId_;
        else
            name_ := 'notsupported';
            return name_;
        end if;

        anId_ := anId_ - 1;               -- Algorithm is for zero-based input index.

        if (anId_ > 8031810175) then
            name_ := 'idoutofrange';
            return name_;
        end if;

        num_ := anId_ + start_;

        res_ := '';

        while (num_ > 0) loop
            num_ := num_ - 1;
            rem_ := num_ % 26;
            let_ := c[rem_ + 1];          -- PosgreSQL arrays are one-based.
            res_ := let_ || res_;         -- Need to reverse characters in string.
            num_ := (num_ - rem_) / 26;
        end loop;

        name_ := 'RAPID' || cast(yeartwodigits_ as char(2)) || cast(res_ as char(7));

        return name_;

    end;

$$ language plpgsql;


-- Load job into Jobs table.
--
create function startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer
)
    returns integer as $$

    declare

        jid_     integer;

    begin

        -- Insert record if not found.

        if (expid_ is not null) then

            select jid
            into jid_
            from Jobs
            where ppid = ppid_
            and rid = rid_;

        else

            select jid
            into jid_
            from Jobs
            where ppid = ppid_
            and fid = fid_
            and field = field_;

        end if;

        if not found then

            -- Insert Jobs record.

            begin

                insert into Jobs
                (ppid,
                 expid,
                 field,
                 sca,
                 fid,
                 rid,
                 machine,
                 slurm,
                 launched)
                values
                (ppid_,
                 expid_,
                 field_,
                 sca_,
                 fid_,
                 rid_,
                 machine_,
                 slurm_,
                 now())
                returning jid into strict jid_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in startJob: Row could not be inserted into Jobs table.';

            end;

        else

            -- Update Jobs record.

            update Jobs
            set machine = machine_,
                slurm = slurm_,
                launched = now(),
                started = null,
                ended = null,
                elapsed = null,
                qwaited = null,
                exitcode = null,
                awsbatchjobid = null,
                status = 0
            where jid = jid_;

        end if;

        return jid_;

    end;

$$ language plpgsql;


-- Registers information about a completed job in the Jobs table.
--
create function endJob (
    jid_             integer,
    exitcode_        smallint,
    awsbatchjobid_   varchar(64)
)
    returns void as $$

    declare

    started_ timestamp;
    ended_   timestamp;
    elapsed_ interval;

    begin

         begin

            select started
            into strict started_
            from Jobs
            where jid = jid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in endJob: Jobs record jid=% not found.', jid_;

        end;

        begin

            ended_ = now();
            elapsed_ := ended_ - started_;

            update Jobs
            set ended = ended_,
                elapsed = elapsed_,
                exitcode = exitcode_,
                awsbatchjobid = awsbatchjobid_,
                status = 1
            where jid = jid_;
            exception --------> Required for turning entire block into a transaction.
                when no_data_found then
                    raise exception
                        '*** Error in endJob: Cannot update Jobs record for jid=%', jid_;
        end;

    end;

$$ language plpgsql;


-- Overloaded version with additional timestamp input arguments.
-- Registers information about a completed job in the Jobs table.
--
create function endJob (
    jid_             integer,
    exitcode_        smallint,
    awsbatchjobid_   varchar(64),
    started_         timestamp,
    ended_           timestamp
)
    returns void as $$

    declare

    launched_ timestamp;
    elapsed_ interval;
    qwaited_ interval;

    begin

         begin

            select launched
            into strict launched_
            from Jobs
            where jid = jid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in endJob: Jobs record jid=% not found.', jid_;

        end;

        begin

            qwaited_ := started_ - launched_;
            elapsed_ := ended_ - started_;

            update Jobs
            set started = started_,
                qwaited = qwaited_,
                ended = ended_,
                elapsed = elapsed_,
                exitcode = exitcode_,
                awsbatchjobid = awsbatchjobid_,
                status = 1
            where jid = jid_;
            exception --------> Required for turning entire block into a transaction.
                when no_data_found then
                    raise exception
                        '*** Error in endJob: Cannot update Jobs record for jid=%', jid_;
        end;

    end;

$$ language plpgsql;


-- Get latest software version.
--
create function getLatestSwVersion (
)
    returns smallint as $$

    declare

        svid_    smallint;

    begin

        begin

            select svid
            into strict svid_
            from SwVersions
            order by svid desc
            limit 1;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in getLatestSwVersion: SwVersions record not found.';

        end;

        return svid_;

    end;

$$ language plpgsql;


-- Insert a new record into or update an existing record in the RefImCatalogs table.
--
create function registerRefImCatalog (
    rfid_     integer,
    ppid_     smallint,
    catType_  smallint,
    field_    integer,
    hp6_      integer,
    hp9_      integer,
    fid_      smallint,
    filename_ varchar(255),
    checksum_ varchar(32),
    status_   smallint
)
    returns record as $$

    declare

        rfcatid_          integer;
        svid_             smallint;
        r_                record;

    begin


        -- Get latest software version number.

        select getLatestSwVersion into svid_ from getLatestSwVersion();


        -- Insert or update record, as appropriate.

        select rfcatid
        into rfcatid_
        from RefImCatalogs
        where rfid = rfid_
        and ppid = ppid_
        and catType = catType_;

        if not found then


            -- Insert RefImCatalogs record.

            begin

                insert into RefImCatalogs
                (rfid, ppid, catType, field, hp6, hp9, fid,
                 svid, filename, checksum, status, created)
                values
                (rfid_, ppid_, catType_, field_, hp6_, hp9_, fid_,
                 svid_, filename_, checksum_, status_, now())
                returning rfcatid into strict rfcatid_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerRefImCatalog: RefImCatalogs record for rfid=%, ppid=%, catType=% not inserted.', rfid_, ppid_, catType_;

            end;

        else


            -- Update RefImCatalogs record.

            update RefImCatalogs
            set rfid = rfid_,
                ppid = ppid_,
                catType = catType_,
                field = field_,
                hp6 = hp6_,
                hp9 = hp9_,
                fid = fid_,
                svid = svid_,
                filename = filename_,
                checksum = checksum_,
                status = status_,
                created = now()
            where rfcatid = rfcatid_;

        end if;

        select rfcatid_, svid_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Insert a new record into the RefImImages table, if record is not found.
--
create function registerRefImImage (
    rfid_ integer,
    rid_ integer
)
    returns void as $$

    declare

        rfid__  integer;

    begin


        -- Insert or update record, as appropriate.

        select rfid
        into rfid__
        from RefImImages
        where rfid = rfid_
        and rid = rid_;

        if not found then

            begin

                insert into RefImImages
                (rfid, rid)
                values
                (rfid_, rid_);
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerRefImImages: RefImImages record for rfid=%, rid=% not inserted.', rfid_, rid_;

            end;

        end if;

    end;

$$ language plpgsql;


-- Insert a new record into the SOCProcs table or update existing one.
--
create function addSOCProc (
    datedeliv_            timestamp,
    mjdobsmin_            double precision,
    mjdobsmax_            double precision,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
)
    returns record as $$

    declare

        r_               record;
        did_           integer;
        did__          integer;

    begin


        -- Insert or update record.

        did__ := null;

        select did
        into did__
        from SOCProcs
        where datedeliv = datedeliv_;

        if (did__ is null) then

            -- Insert SOCProcs record.

            begin

                insert into SOCProcs
                (datedeliv,
                 mjdobsmin,
                 mjdobsmax,
                 filename,
                 checksum,
                 status
                )
                values
                (datedeliv_,
                 mjdobsmin_,
                 mjdobsmax_,
                 filename_,
                 checksum_,
                 status_
                )
                returning did into strict did_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in addSOCProc: Row could not be inserted into SOCProcs table.';
            end;

        else

            -- Update record in SOCProcs table.

            did_ := did__;

            update SOCProcs
            set datedeliv = datedeliv_,
                mjdobsmin = mjdobsmin_,
                mjdobsmax = mjdobsmax_,
                filename = filename_,
                checksum = checksum_,
                status = status_,
                created = now()
            where did = did_;

        end if;

        select did_, fid_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Insert a new record into the PSFs table.
--
create function addPSF (
    fid_                  smallint,
    sca_                  smallint,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint

)
    returns record as $$

    declare

        r_                record;
        psfid_            integer;
        version_          smallint;
        vbest_            smallint;

    begin

        -- Processed images are versioned according to unique (fid, sca) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updatePSF is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from PSFs
        where fid = fid_
        and sca = sca_;

        if not found then
            version_ := 1;
        end if;

        vbest_ := 0;

        -- Insert PSFs record.

        begin

            insert into PSFs
            (fid,sca,version,status,vbest,
             filename,checksum
            )
            values
            (fid_,sca_,version_,status_,vbest_,
             filename_,checksum_
            )
            returning psfid into strict psfid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addPSF: PSFs record for fid,sca=%,% not inserted.', fid_,sca_;

        end;

        select psfid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a PSFs record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updatePSF (
    psfid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
)

    returns void as $$

    declare

        psfid__            integer;
        currentVBest_    smallint;
        fid_           integer;
        sca_             smallint;
        vBest_           smallint;
        bestIs2_         boolean;
        count_           integer;

    begin

        bestIs2_ := 'f';

        -- First, get the fid, sca for the L2 image.

        begin

            select fid, sca
            into strict fid_, sca_
            from psfs
            where psfid = psfid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updatePSF: PSFs record not found for psfid=%', psfid_;

        end;

        -- If this isn't the first PSFs record
        -- for the exposure and sca, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new PSFs record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the PSFs record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from PSFs
        where fid = fid_
        and sca = sca_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updatePSF: More than one PSFs record with vBest>0 returned.';
            end if;

            select psfid, vBest
            into psfid__, currentVBest_
            from PSFs
            where fid = fid_
            and sca = sca_
            and vBest in (1, 2);

            if (currentvBest_ = 1) then -- vBest is not locked
                update PSFs
                set vBest = 0
                where psfid = psfid__;

                if not found then
                    raise exception '*** Error in updatePSF: Cannot update PSFs record.';
                end if;
            else
                bestIs2_ := 't';
            end if;

        end if;

        if bestIs2_ = 't' then
            vBest_ := 0;
        else
            vBest_ := 1;
        end if;

        update PSFs
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where psfid = psfid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updatePSF: Cannot update PSFs record for psfid=%', psfid_;

    end;

$$ language plpgsql;


-- Insert a new record into or update an existing record in the DiffImMeta table.
--
create function registerDiffImMeta (
    pid_                 integer,
    fid_                 smallint,
    sca_                 smallint,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    nsexcatsources_      integer,
    scalefacref_         real,
    dxrmsfin_            real,
    dyrmsfin_            real,
    dxmedianfin_         real,
    dymedianfin_         real
)
    returns void as $$

    declare

        pid__    integer;

    begin


        -- Insert or update record, as appropriate.

        select pid
        into pid__
        from DiffImMeta
        where pid = pid_;

        if not found then


            -- Insert DiffImMeta record.

            begin

                insert into DiffImMeta
                (pid,
                 fid,
                 sca,
                 field,
                 hp6,
                 hp9,
                 nsexcatsources,
                 scalefacref,
                 dxrmsfin,
                 dyrmsfin,
                 dxmedianfin,
                 dymedianfin
                )
                values
                (pid_,
                 fid_,
                 sca_,
                 field_,
                 hp6_,
                 hp9_,
                 nsexcatsources_,
                 scalefacref_,
                 dxrmsfin_,
                 dyrmsfin_,
                 dxmedianfin_,
                 dymedianfin_
                );
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerDiffImMeta: DiffImMeta record for pid=% not inserted.', pid_;

            end;

        else


            -- Update DiffImMeta record.

            update DiffImMeta
            set fid = fid_,
                sca = sca_,
                field = field_,
                hp6 = hp6_,
                hp9 = hp9_,
                nsexcatsources = nsexcatsources_,
                scalefacref = scalefacref_,
                dxrmsfin = dxrmsfin_,
                dyrmsfin = dyrmsfin_,
                dxmedianfin = dxmedianfin_,
                dymedianfin = dymedianfin_
            where pid = pid_;

        end if;

    end;

$$ language plpgsql;


-- Insert a new record into or update an existing record in the RefImMeta table.
--
create function registerRefImMeta (
    rfid_                integer,
    fid_                 smallint,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    nframes_             smallint,
    mjdobsmin_           double precision,
    mjdobsmax_           double precision,
    npixnan_             integer,
    clmean_              real,
    clstddev_            real,
    clnoutliers_         integer,
    gmedian_             real,
    datascale_           real,
    gmin_                real,
    gmax_                real,
    cov5percent_         real,
    medncov_             real,
    medpixunc_           real,
    fwhmmedpix_          real,
    fwhmminpix_          real,
    fwhmmaxpix_          real,
    nsxcatsources_       integer,
    npucatsources_       integer
)
    returns void as $$

    declare

        rfid__    integer;

    begin


        -- Insert or update record, as appropriate.

        select rfid
        into rfid__
        from RefImMeta
        where rfid = rfid_;

        if not found then


            -- Insert RefImMeta record.

            begin

                insert into RefImMeta
                (rfid,
                 fid,
                 field,
                 hp6,
                 hp9,
                 nframes,
                 mjdobsmin,
                 mjdobsmax,
                 npixnan,
                 clmean,
                 clstddev,
                 clnoutliers,
                 gmedian,
                 datascale,
                 gmin,
                 gmax,
                 cov5percent,
                 medncov,
                 medpixunc,
                 fwhmmedpix,
                 fwhmminpix,
                 fwhmmaxpix,
                 nsxcatsources,
                 npucatsources
                )
                values
                (rfid_,
                 fid_,
                 field_,
                 hp6_,
                 hp9_,
                 nframes_,
                 mjdobsmin_,
                 mjdobsmax_,
                 npixnan_,
                 clmean_,
                 clstddev_,
                 clnoutliers_,
                 gmedian_,
                 datascale_,
                 gmin_,
                 gmax_,
                 cov5percent_,
                 medncov_,
                 medpixunc_,
                 fwhmmedpix_,
                 fwhmminpix_,
                 fwhmmaxpix_,
                 nsxcatsources_,
                 npucatsources_
                );
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerRefImMeta: RefImMeta record for rfid=% not inserted.', rfid_;

            end;

        else


            -- Update RefImMeta record.

            update RefImMeta
            set fid = fid_,
                field = field_,
                hp6 = hp6_,
                hp9 = hp9_,
                nframes = nframes_,
                mjdobsmin = mjdobsmin_,
                mjdobsmax = mjdobsmax_,
                npixnan = npixnan_,
                clmean = clmean_,
                clstddev = clstddev_,
                clnoutliers = clnoutliers_,
                gmedian = gmedian_,
                datascale = datascale_,
                gmin = gmin_,
                gmax = gmax_,
                cov5percent = cov5percent_,
                medncov = medncov_,
                medpixunc = medpixunc_,
                fwhmmedpix = fwhmmedpix_,
                fwhmminpix = fwhmminpix_,
                fwhmmaxpix = fwhmmaxpix_,
                nsxcatsources = nsxcatsources_,
                npucatsources = npucatsources_
            where rfid = rfid_;

        end if;

    end;

$$ language plpgsql;

-- ======================================================================
-- source: database/schema/rapidOpsProcGrants.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsProcGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 16 April 2024
--------------------------------------------------------------------------------------------------------------------------


grant EXECUTE on FUNCTION addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint
) to rapidporole;


grant EXECUTE on FUNCTION addL2File (
    expid_                integer,
    sca_                  smallint,
    field_                integer,
    hp6_                 integer,
    hp9_                 integer,
    fid_                  smallint,
    dateobs_              timestamp without time zone,
    mjdobs_               double precision,
    exptime_              real,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint,
    crval1_               double precision,
    crval2_               double precision,
    crpix1_               real,
    crpix2_               real,
    cd11_                 double precision,
    cd12_                 double precision,
    cd21_                 double precision,
    cd22_                 double precision,
    ctype1_               character varying(16),
    ctype2_               character varying(16),
    cunit1_               character varying(16),
    cunit2_               character varying(16),
    a_order_              smallint,
    a_0_2_                double precision,
    a_0_3_                double precision,
    a_0_4_                double precision,
    a_1_1_                double precision,
    a_1_2_                double precision,
    a_1_3_                double precision,
    a_2_0_                double precision,
    a_2_1_                double precision,
    a_2_2_                double precision,
    a_3_0_                double precision,
    a_3_1_                double precision,
    a_4_0_                double precision,
    b_order_              smallint,
    b_0_2_                double precision,
    b_0_3_                double precision,
    b_0_4_                double precision,
    b_1_1_                double precision,
    b_1_2_                double precision,
    b_1_3_                double precision,
    b_2_0_                double precision,
    b_2_1_                double precision,
    b_2_2_                double precision,
    b_3_0_                double precision,
    b_3_1_                double precision,
    b_4_0_                double precision,
    equinox_              real,
    ra_                   double precision,
    dec_                  double precision,
    paobsy_               real,
    pafpa_                real,
    zptmag_               real,
    skymean_              real,
    overlapfields_        integer[]
) to rapidporole;


-- Overloaded version with additional fifth-order SIP-distortion coefficient.
-- Insert record in L2Files table.
--
grant EXECUTE on FUNCTION addL2File (
    expid_                integer,
    sca_                  smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    dateobs_              timestamp without time zone,
    mjdobs_               double precision,
    exptime_              real,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint,
    crval1_               double precision,
    crval2_               double precision,
    crpix1_               real,
    crpix2_               real,
    cd11_                 double precision,
    cd12_                 double precision,
    cd21_                 double precision,
    cd22_                 double precision,
    ctype1_               character varying(16),
    ctype2_               character varying(16),
    cunit1_               character varying(16),
    cunit2_               character varying(16),
    a_order_              smallint,
    a_0_1_                double precision,
    a_0_2_                double precision,
    a_0_3_                double precision,
    a_0_4_                double precision,
    a_0_5_                double precision,
    a_1_0_                double precision,
    a_1_1_                double precision,
    a_1_2_                double precision,
    a_1_3_                double precision,
    a_1_4_                double precision,
    a_2_0_                double precision,
    a_2_1_                double precision,
    a_2_2_                double precision,
    a_2_3_                double precision,
    a_3_0_                double precision,
    a_3_1_                double precision,
    a_3_2_                double precision,
    a_4_0_                double precision,
    a_4_1_                double precision,
    a_5_0_                double precision,
    b_order_              smallint,
    b_0_1_                double precision,
    b_0_2_                double precision,
    b_0_3_                double precision,
    b_0_4_                double precision,
    b_0_5_                double precision,
    b_1_0_                double precision,
    b_1_1_                double precision,
    b_1_2_                double precision,
    b_1_3_                double precision,
    b_1_4_                double precision,
    b_2_0_                double precision,
    b_2_1_                double precision,
    b_2_2_                double precision,
    b_2_3_                double precision,
    b_3_0_                double precision,
    b_3_1_                double precision,
    b_3_2_                double precision,
    b_4_0_                double precision,
    b_4_1_                double precision,
    b_5_0_                double precision,
    equinox_              real,
    ra_                   double precision,
    dec_                  double precision,
    paobsy_               real,
    pafpa_                real,
    zptmag_               real,
    skymean_              real,
    overlapfields_        integer[]
) to rapidporole;


grant EXECUTE on FUNCTION updateL2File (
    rid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
) to rapidporole;


grant EXECUTE on FUNCTION registerL2FileMeta (
    rid_                 integer,
    fid_                 smallint,
    sca_                 smallint,
    ra0_                 double precision,
    dec0_                double precision,
    ra1_                 double precision,
    dec1_                double precision,
    ra2_                 double precision,
    dec2_                double precision,
    ra3_                 double precision,
    dec3_                double precision,
    ra4_                 double precision,
    dec4_                double precision,
    x_                   double precision,
    y_                   double precision,
    z_                   double precision,
    hp6_                 integer,
    hp9_                 integer,
    mjdobs_              double precision
) to rapidporole;


grant EXECUTE on FUNCTION addDiffImage (
    rid_                  integer,
    ppid_                 smallint,
    rfid_                 integer,
    infobitssci_          integer,
    infobitsref_          integer,
    ra0_                  double precision,
    dec0_                 double precision,
    ra1_                  double precision,
    dec1_                 double precision,
    ra2_                  double precision,
    dec2_                 double precision,
    ra3_                  double precision,
    dec3_                 double precision,
    ra4_                  double precision,
    dec4_                 double precision,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
) to rapidporole;


grant EXECUTE on FUNCTION updateDiffImage (
    pid_         integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
) to rapidporole;


grant EXECUTE on FUNCTION addRefImage (
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    ppid_                 smallint,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
) to rapidporole;


grant EXECUTE on FUNCTION updateRefImage (
    rfid_        integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
) to rapidporole;


grant EXECUTE on FUNCTION addAlertName (
    name_   char(12),
    sca_    smallint,
    field_  integer,
    hp6_    integer,
    hp9_    integer,
    ra_     double precision,
    dec_    double precision,
    jd_     double precision,
    candId_ bigint
) to rapidporole;


grant EXECUTE on FUNCTION computeAlertName (
    yeartwodigits_ smallint
) to rapidporole;


grant EXECUTE on FUNCTION startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer
) to rapidporole;


grant EXECUTE on FUNCTION endJob (
    jid_       integer,
    exitcode_  smallint,
    awsbatchjobid varchar(64)
) to rapidporole;


-- Overloaded version with additional timestamp input arguments.
-- Registers information about a completed job in the Jobs table.
--
grant EXECUTE on FUNCTION endJob (
    jid_             integer,
    exitcode_        smallint,
    awsbatchjobid_   varchar(64),
    started_         timestamp,
    ended_           timestamp
) to rapidporole;


grant EXECUTE on FUNCTION getLatestSwVersion (
) to rapidporole;


grant EXECUTE on FUNCTION registerRefImCatalog (
    rfid_     integer,
    ppid_     smallint,
    catType_  smallint,
    field_    integer,
    hp6_      integer,
    hp9_      integer,
    fid_      smallint,
    filename_ varchar(255),
    checksum_ varchar(32),
    status_   smallint
) to rapidporole;


grant EXECUTE on FUNCTION registerRefImImage (
    rfid_ integer,
    rid_ integer
) to rapidporole;


grant EXECUTE on FUNCTION addSOCProc (
    datedeliv_            timestamp,
    mjdobsmin_            double precision,
    mjdobsmax_            double precision,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
) to rapidporole;


grant EXECUTE on FUNCTION addPSF (
    fid_                  smallint,
    sca_                  smallint,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint

) to rapidporole;


grant EXECUTE on FUNCTION updatePSF (
    psfid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
) to rapidporole;


grant EXECUTE on FUNCTION registerDiffImMeta (
    pid_                 integer,
    fid_                 smallint,
    sca_                 smallint,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    nsexcatsources_      integer,
    scalefacref_         real,
    dxrmsfin_            real,
    dyrmsfin_            real,
    dxmedianfin_         real,
    dymedianfin_         real
) to rapidporole;


grant EXECUTE on FUNCTION registerRefImMeta (
    rfid_                integer,
    fid_                 smallint,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    nframes_             smallint,
    mjdobsmin_           double precision,
    mjdobsmax_           double precision,
    npixnan_             integer,
    clmean_              real,
    clstddev_            real,
    clnoutliers_         integer,
    gmedian_             real,
    datascale_           real,
    gmin_                real,
    gmax_                real,
    cov5percent_         real,
    medncov_             real,
    medpixunc_           real,
    fwhmmedpix_          real,
    fwhmminpix_          real,
    fwhmmaxpix_          real,
    nsxcatsources_       integer,
    npucatsources_       integer
) to rapidporole;

-- ======================================================================
-- source: database/schema/rapidOpsFiltersInserts.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsFiltersInserts
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------

INSERT INTO filters values (1, 'F184');
INSERT INTO filters values (2, 'H158');
INSERT INTO filters values (3, 'J129');
INSERT INTO filters values (4, 'K213');
INSERT INTO filters values (5, 'R062');
INSERT INTO filters values (6, 'Y106');
INSERT INTO filters values (7, 'Z087');
INSERT INTO filters values (8, 'W146');

-- ======================================================================
-- source: database/schema/rapidOpsPipelinesInserts.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsPipelinesInserts
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 16 September 2024
--------------------------------------------------------------------------------------------------------------------------

INSERT INTO pipelines (ppid,priority,script,descrip) values (15,3,'awsBatchSubmitJobs_launchSingleSciencePipeline.py', 'Science pipeline for input SCA image.');
INSERT INTO pipelines (ppid,priority,script,descrip) values (12,4,'generateReferenceImage.py', 'Standard reference-image pipeline.');
INSERT INTO pipelines (ppid,priority,script,descrip) values (17,5,'awsBatchSubmitJobs_launchSinglePostProcPipeline.py', 'Post-processing pipeline for input SCA image.');

-- ======================================================================
-- source: database/schema/rapidOpsSWVersionsInserts.sql
-- ======================================================================

--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSWVersionsInserts
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 17 September 2024
--------------------------------------------------------------------------------------------------------------------------

-- Store first 7 characters of git hash in cvstag field.

INSERT INTO swversions (cvstag,installed,comment,release) values ('23c83dd',now(),'Development', '0.1');
