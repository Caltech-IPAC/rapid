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
-- TABLE: Scas
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE scas (
    sca smallint NOT NULL,                            -- Primary key
    CONSTRAINT scaspk CHECK (((sca >= 1) AND (sca <= 18)))
);

ALTER TABLE scas OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY scas ADD CONSTRAINT scaspk2 UNIQUE (sca);


INSERT into scas (sca) SELECT generate_series(1,18);


-----------------------------
-- TABLE: Exposures
-----------------------------

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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
    a_0_2 double precision,                              -- FITS-header keyword: A_0_2
    a_0_3 double precision,                              -- FITS-header keyword: A_0_3
    a_0_4 double precision,                              -- FITS-header keyword: A_0_4
    a_1_1 double precision,                              -- FITS-header keyword: A_1_1
    a_1_2 double precision,                              -- FITS-header keyword: A_1_2
    a_1_3 double precision,                              -- FITS-header keyword: A_1_3
    a_2_0 double precision,                              -- FITS-header keyword: A_2_0
    a_2_1 double precision,                              -- FITS-header keyword: A_2_1
    a_2_2 double precision,                              -- FITS-header keyword: A_2_2
    a_3_0 double precision,                              -- FITS-header keyword: A_3_0
    a_3_1 double precision,                              -- FITS-header keyword: A_3_1
    a_4_0 double precision,                              -- FITS-header keyword: A_4_0
    b_order smallint,                                    -- FITS-header keyword: B_ORDER
    b_0_2 double precision,                              -- FITS-header keyword: B_0_2
    b_0_3 double precision,                              -- FITS-header keyword: B_0_3
    b_0_4 double precision,                              -- FITS-header keyword: B_0_4
    b_1_1 double precision,                              -- FITS-header keyword: B_1_1
    b_1_2 double precision,                              -- FITS-header keyword: B_1_2
    b_1_3 double precision,                              -- FITS-header keyword: B_1_3
    b_2_0 double precision,                              -- FITS-header keyword: B_2_0
    b_2_1 double precision,                              -- FITS-header keyword: B_2_1
    b_2_2 double precision,                              -- FITS-header keyword: B_2_2
    b_3_0 double precision,                              -- FITS-header keyword: B_3_0
    b_3_1 double precision,                              -- FITS-header keyword: B_3_1
    b_4_0 double precision,                              -- FITS-header keyword: B_4_0
    equinox real NOT NULL,                               -- FITS-header keyword: EQUINOX
    ra double precision NOT NULL,                        -- FITS-header keyword: RA_TARG
    dec double precision NOT NULL,                       -- FITS-header keyword: DEC_TARG
    paobsy real,                                         -- FITS-header keyword: PA_OBSY
    pafpa real,                                          -- FITS-header keyword: PA_FPA
    zptmag real,                                         -- FITS-header keyword: ZPTMAG
    skymean real,                                        -- FITS-header keyword: SKY-MEAN
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
    z double precision NOT NULL,
    hp6 integer NOT NULL,               -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,               -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,
    sca smallint NOT NULL,
    mjdobs double precision NOT NULL
);

ALTER TABLE l2filemeta OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

CREATE TABLE pipelines (
    ppid smallint NOT NULL,
    priority smallint NOT NULL,
    script character varying(255) default 'TBD' NOT NULL,
    descrip character varying(255) NOT NULL
);

ALTER TABLE pipelines OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY pipelines ADD CONSTRAINT pipelinespk UNIQUE (ppid);

ALTER TABLE ONLY pipelines ADD CONSTRAINT pipelinespk2 UNIQUE (priority);


-----------------------------
-- TABLE: SwVersions
-----------------------------

SET default_tablespace = pipeline_data_01;

CREATE TABLE swversions (
    svid smallint NOT NULL,
    cvstag varchar(30) NOT NULL,
    installed timestamp NOT NULL,
    comment varchar(255),
    release varchar(15) NOT NULL
);

ALTER TABLE swversions OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

CREATE TABLE archiveversions (
    avid integer NOT NULL,
    archived timestamp NOT NULL
);

ALTER TABLE archiveversions OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

CREATE TABLE refimimages (
    rfid integer NOT NULL,
    rid integer NOT NULL
);

ALTER TABLE refimimages OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY refimimages ADD CONSTRAINT refimimages_rfid_fk FOREIGN KEY (rfid) REFERENCES refimages(rfid);

ALTER TABLE ONLY refimimages ADD CONSTRAINT refimimages_rid_fk FOREIGN KEY (rid) REFERENCES l2files(rid);

CREATE INDEX refimimages_rid_idx ON refimimages (rid);
CREATE INDEX refimimages_rfid_idx ON refimimages (rfid);


-----------------------------
-- TABLE: SOCProcs
--
-- Tracks periodic, discrete deliveries of exposure data from the SOC.
-----------------------------

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

CREATE TABLE refimmeta (
    rfid integer NOT NULL,                 -- Primary key
    field integer NOT NULL,                -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                  -- Level-6 healpix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                  -- Level-9 healpix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL,                 -- Foreign key from Filters table
    nframes smallint NOT NULL,             -- Number of images in stack
    mjdobsmin double precision NOT NULL,   -- Minimum MJD of input images in stack
    mjdobsmax double precision NOT NULL,   -- Maximum MJD of input images in stack
    npixsat integer NOT NULL,              -- Number of saturated pixels in reference image
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
    nsexcatsources integer NOT NULL        -- Number of sources in reference-image SExtractor catalog
);

ALTER TABLE refimmeta OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

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

SET default_tablespace = pipeline_data_01;

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

SET default_tablespace = pipeline_indx_01;

CREATE index fields_field_idx on fields(field);

CREATE INDEX fields_radec_idx ON fields (q3c_ang2ipix(ra0, dec0));
CLUSTER fields_radec_idx ON fields;
ANALYZE fields;

