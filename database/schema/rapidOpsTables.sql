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

SET default_tablespace = pipeline_data_01;

CREATE TABLE filters (
    fid smallint NOT NULL,                               -- FITS-header keyword: FILTERID
    filter character varying(16) NOT NULL                -- FITS-header keyword: FILTER
);

ALTER TABLE filters OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY filters ADD CONSTRAINT filters_pkey PRIMARY KEY (fid);

ALTER TABLE ONLY filters ADD CONSTRAINT filterspk UNIQUE (filter);


-----------------------------
-- TABLE: Chips
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE chips (
    chipid smallint NOT NULL,                            -- Primary key
    CONSTRAINT chipspk CHECK (((chipid >= 1) AND (chipid <= 18)))
);

ALTER TABLE chips OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY chips ADD CONSTRAINT chipspk2 UNIQUE (chipid);


INSERT into chips (chipid) SELECT generate_series(1,18);


-----------------------------
-- TABLE: Exposures
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE exposures (
    expid integer NOT NULL,                         -- Primary key
    dateobs timestamp without time zone NOT NULL,   -- Header keyword: DATE-OBS
    field integer NOT NULL,                         -- level-6 healpix index (NESTED) from RA_TARG, DEC_TARG
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

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY exposures ADD CONSTRAINT exposures_pkey PRIMARY KEY (expid);

ALTER TABLE ONLY exposures ADD CONSTRAINT exposurespk UNIQUE (dateobs);

ALTER TABLE ONLY exposures ADD CONSTRAINT exposures_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

CREATE INDEX exposures_fid_idx ON exposures (fid);
CREATE INDEX exposures_field_idx ON exposures (field);
CREATE INDEX exposures_exptime_idx ON exposures (exptime);
CREATE INDEX exposures_mjdobs_idx ON exposures (mjdobs);
CREATE INDEX exposureps_status_idx ON exposures (status);
CREATE INDEX exposures_infobits_idx ON exposures (infobits);


-----------------------------
-- TABLE: L2Files
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE l2files (
    rid integer NOT NULL,                                -- Primary key
    expid integer NOT NULL,
    chipid smallint NOT NULL,                            -- FITS-header keyword: SCA-NUM
    version smallint NOT NULL,
    vbest smallint NOT NULL,
    field integer NOT NULL,
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
    a_order smallint NOT NULL,                           -- FITS-header keyword: A_ORDER
    a_0_2 double precision NOT NULL,                     -- FITS-header keyword: A_0_2
    a_0_3 double precision NOT NULL,                     -- FITS-header keyword: A_0_3
    a_0_4 double precision NOT NULL,                     -- FITS-header keyword: A_0_4
    a_1_1 double precision NOT NULL,                     -- FITS-header keyword: A_1_1
    a_1_2 double precision NOT NULL,                     -- FITS-header keyword: A_1_2
    a_1_3 double precision NOT NULL,                     -- FITS-header keyword: A_1_3
    a_2_0 double precision NOT NULL,                     -- FITS-header keyword: A_2_0
    a_2_1 double precision NOT NULL,                     -- FITS-header keyword: A_2_1
    a_2_2 double precision NOT NULL,                     -- FITS-header keyword: A_2_2
    a_3_0 double precision NOT NULL,                     -- FITS-header keyword: A_3_0
    a_3_1 double precision NOT NULL,                     -- FITS-header keyword: A_3_1
    a_4_0 double precision NOT NULL,                     -- FITS-header keyword: A_4_0
    b_order smallint NOT NULL,                           -- FITS-header keyword: B_ORDER
    b_0_2 double precision NOT NULL,                     -- FITS-header keyword: B_0_2
    b_0_3 double precision NOT NULL,                     -- FITS-header keyword: B_0_3
    b_0_4 double precision NOT NULL,                     -- FITS-header keyword: B_0_4
    b_1_1 double precision NOT NULL,                     -- FITS-header keyword: B_1_1
    b_1_2 double precision NOT NULL,                     -- FITS-header keyword: B_1_2
    b_1_3 double precision NOT NULL,                     -- FITS-header keyword: B_1_3
    b_2_0 double precision NOT NULL,                     -- FITS-header keyword: B_2_0
    b_2_1 double precision NOT NULL,                     -- FITS-header keyword: B_2_1
    b_2_2 double precision NOT NULL,                     -- FITS-header keyword: B_2_2
    b_3_0 double precision NOT NULL,                     -- FITS-header keyword: B_3_0
    b_3_1 double precision NOT NULL,                     -- FITS-header keyword: B_3_1
    b_4_0 double precision NOT NULL,                     -- FITS-header keyword: B_4_0
    equinox real NOT NULL,                               -- FITS-header keyword: EQUINOX
    ra double precision NOT NULL,                        -- FITS-header keyword: RA_TARG
    dec double precision NOT NULL,                       -- FITS-header keyword: DEC_TARG
    paobsy real NOT NULL,                                -- FITS-header keyword: PA_OBSY
    pafpa real NOT NULL,                                 -- FITS-header keyword: PA_FPA
    zptmag real NOT NULL,                                -- FITS-header keyword: ZPTMAG
    skymean real NOT NULL,                               -- FITS-header keyword: SKY-MEAN
    created timestamp without time zone                  -- Timestamp of database record INSERT or last UPDATE
        DEFAULT now() NOT NULL,
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

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_pkey PRIMARY KEY (rid);

ALTER TABLE ONLY l2files ADD CONSTRAINT l2filespk UNIQUE (expid, chipid, version);

ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_expid_fk FOREIGN KEY (expid) REFERENCES exposures(expid);
ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_chipid_fk FOREIGN KEY (chipid) REFERENCES chips(chipid);
ALTER TABLE ONLY l2files ADD CONSTRAINT l2files_fid_fk FOREIGN KEY (fid) REFERENCES filters(fid);

CREATE INDEX l2files_rid_idx ON l2files (rid);
CREATE INDEX l2files_field_idx ON l2files (field);
CREATE INDEX l2files_fid_idx ON l2files (fid);
CREATE INDEX l2files_infobits_idx ON l2files (infobits);
CREATE INDEX l2files_status_idx ON l2files (status);
CREATE INDEX l2files_vbest_idx ON l2files (vbest);
CREATE INDEX l2files_mjdobs_idx ON l2files (mjdobs);

-- Q3C indexing will speed up ad-hoc cone searches on (ra, dec).

CREATE INDEX l2files_radec_idx ON l2files (q3c_ang2ipix(ra, dec));
CLUSTER l2files_radec_idx ON l2files;
ANALYZE l2files;




-----------------------------
-- TABLE: L2FileMeta
-----------------------------

SET default_tablespace = pipeline_data_01;

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
    z double precision NOT NULL
);

ALTER TABLE l2filemeta OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY l2filemeta ADD CONSTRAINT l2filemeta_pkey PRIMARY KEY (rid);

ALTER TABLE ONLY l2filemeta ADD CONSTRAINT l2filemeta_rid_fk FOREIGN KEY (rid) REFERENCES l2files(rid);

CREATE INDEX l2filemeta_radec_idx ON l2filemeta (q3c_ang2ipix(ra0, dec0));
CLUSTER l2filemeta_radec_idx ON l2filemeta;
ANALYZE l2filemeta;
