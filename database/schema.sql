-- schema.sql: the whole RAPID foundation schema, applied once to an empty
-- PostgreSQL 18 database:
--
--   psql -v ON_ERROR_STOP=1 -f database/schema.sql
--
-- Design authority: the foundation plan (PLAN.md v5, 2026-10-20), Part 1.
-- Every published product row names the job that wrote it; the job names
-- its run. Published rows are never changed. `official` says, per product
-- and scientific key, which job's rows are official, and which run is live
-- per survey and dataset; one `<table>_official` view per product table
-- shows the official rows. Nothing here is ever deleted.
--
-- Scientific keys (every key is stored in `official.key` as a jsonb object
-- whose names are the column names below, e.g.
-- {"dataset": "d", "expid": 7, "sca": 3, "fid": 1}):
--
--   exposures                          dataset, expid
--   l2files, diffimages, sources,
--   packets                            dataset, expid, sca, fid
--   refimages                          dataset, field, fid
--   psfs                               dataset, fid, sca, kind
--   objects                            dataset, survey, field
--   catalogs                           name, version
--
-- A set (a reference image, a source set, an object set, a packet set) is
-- identified by its job: its child rows (refimimages, refimcatalogs,
-- sources, memberships, packets, forced, external) carry the set's job and
-- their own id, and their views join through the set's open binding on
-- (product, job).
--
-- Columns are dev's names and types (database/schema/rapidOpsTables.sql and
-- rapidOpsSourcesTable.sql on dev) for the columns the team's queries read,
-- without vbest, version or ppid: a binding replaces vbest, a job replaces
-- version, and a run has one pipeline.

--------------------------------------------------------------------------------
-- Static tables (dev's columns and insert data)
--------------------------------------------------------------------------------

CREATE TABLE filters (
    fid smallint PRIMARY KEY,                        -- FITS-header keyword: FILTERID
    filter character varying(16) NOT NULL UNIQUE     -- FITS-header keyword: FILTER
);

INSERT INTO filters (fid, filter) VALUES
    (1, 'F184'), (2, 'H158'), (3, 'J129'), (4, 'K213'),
    (5, 'R062'), (6, 'Y106'), (7, 'Z087'), (8, 'W146');

CREATE TABLE scas (
    sca smallint PRIMARY KEY CHECK (sca BETWEEN 1 AND 18)
);

INSERT INTO scas (sca) SELECT generate_series(1, 18);

CREATE TABLE surveys (
    program_id integer PRIMARY KEY,                  -- Roman program number, negative for simulations
    program_title character varying(255) NOT NULL,
    category character varying(6),                   -- CCS, GAS, GI, CAL, COM, ENG, ..., SIM
    created timestamp without time zone DEFAULT now() NOT NULL
);

INSERT INTO surveys (program_id, program_title, category) VALUES
    (-1, 'SOCSIMS', 'SIM'),
    (-2, 'OpenUniverse', 'SIM'),
    (-3, 'RimTimSims', 'SIM');

CREATE TABLE fields (
    field integer PRIMARY KEY,                       -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,
    hp9 integer NOT NULL,
    ra1 double precision NOT NULL,                   -- lower-left corner
    dec1 double precision NOT NULL,
    ra2 double precision NOT NULL,                   -- lower-right corner
    dec2 double precision NOT NULL,
    ra3 double precision NOT NULL,                   -- upper-right corner
    dec3 double precision NOT NULL,
    ra4 double precision NOT NULL,                   -- upper-left corner
    dec4 double precision NOT NULL,
    ra0 double precision NOT NULL,                   -- centre
    dec0 double precision NOT NULL
);

--------------------------------------------------------------------------------
-- Bookkeeping: runs, jobs, job_inputs, official
--------------------------------------------------------------------------------

-- A run is a recipe someone launched for a reason, for one survey and dataset.
CREATE TABLE runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    who text NOT NULL,
    created timestamp with time zone DEFAULT now() NOT NULL,
    why text NOT NULL,
    revision text NOT NULL,                          -- code revision
    settings jsonb NOT NULL,                         -- the full settings body
    survey text NOT NULL,
    dataset text NOT NULL
);

-- A job is one execution of one stage under a run.
CREATE TABLE jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run bigint NOT NULL REFERENCES runs (id),
    stage text NOT NULL,
    scope jsonb NOT NULL,
    state text DEFAULT 'launched' NOT NULL
        CHECK (state IN ('launched', 'running', 'published', 'failed')),
    image_digest text,                               -- none for a local checkout
    instance_type text,                              -- the EC2 instance type it ran on; null when unknown
    settings_hash text,
    published_attempt integer,
    manifest jsonb,
    refusal text,                                    -- why the automatic binding was refused, if it was
    launched timestamp with time zone DEFAULT now() NOT NULL,
    started timestamp with time zone,
    ended timestamp with time zone,
    exit_code integer,
    kafka_published_at timestamp with time zone,      -- every packet of its set acknowledged by the topic
    batch_job_id text,                               -- the Batch job of its latest submission; null for a local job
    CHECK ((state = 'published') = (published_attempt IS NOT NULL))
);

CREATE INDEX jobs_run_idx ON jobs (run);
CREATE INDEX jobs_state_idx ON jobs (state);

-- Official bindings: a product binding (product, key, job) or the live
-- binding (survey, dataset, run). Both directions of a change are
-- attributed on the row, so a change group's effect is the rows it opened
-- and the rows it closed.
CREATE SEQUENCE change_groups AS bigint;

CREATE TABLE official (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product text,
    key jsonb,
    job bigint REFERENCES jobs (id),
    run bigint REFERENCES runs (id),
    survey text,
    dataset text,
    opened_group bigint NOT NULL,
    opened_by text NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    opened_why text NOT NULL,
    closed_group bigint,
    closed_by text,
    closed_at timestamp with time zone,
    closed_why text,
    CHECK (
        (job IS NOT NULL AND product IS NOT NULL AND key IS NOT NULL AND run IS NULL)
        OR (run IS NOT NULL AND survey IS NOT NULL AND dataset IS NOT NULL
            AND job IS NULL AND product IS NULL AND key IS NULL)),
    CHECK (
        (closed_at IS NULL AND closed_group IS NULL AND closed_by IS NULL AND closed_why IS NULL)
        OR (closed_at IS NOT NULL AND closed_group IS NOT NULL AND closed_by IS NOT NULL
            AND closed_why IS NOT NULL))
);

CREATE UNIQUE INDEX official_open_product_key ON official (product, key)
    WHERE closed_at IS NULL AND job IS NOT NULL;
CREATE UNIQUE INDEX official_open_live ON official (survey, dataset)
    WHERE closed_at IS NULL AND run IS NOT NULL;
CREATE INDEX official_open_product_job ON official (product, job)
    WHERE closed_at IS NULL;
CREATE INDEX official_opened_group_idx ON official (opened_group);
CREATE INDEX official_closed_group_idx ON official (closed_group);

-- A job's inputs, chosen at launch:
--   input     a set the job reads (product, key, source_job); for an object
--             set's job, its `objects` input is its predecessor
--   expects   per output key, the open binding the job expects to replace
--             (binding), or none (binding null)
--   excludes  a predecessor source set that is no longer official
--             (product 'sources', key, source_job), whose members it drops
CREATE TABLE job_inputs (
    job bigint NOT NULL REFERENCES jobs (id),
    kind text NOT NULL CHECK (kind IN ('input', 'expects', 'excludes')),
    product text NOT NULL,
    key jsonb NOT NULL,
    source_job bigint REFERENCES jobs (id),
    binding bigint REFERENCES official (id)
);

CREATE INDEX job_inputs_job_idx ON job_inputs (job, kind);
CREATE INDEX job_inputs_source_job_idx ON job_inputs (source_job);

--------------------------------------------------------------------------------
-- Scientific tables: every row names its job
--------------------------------------------------------------------------------

CREATE TABLE exposures (
    job bigint NOT NULL REFERENCES jobs (id),
    dataset text NOT NULL,
    expid integer NOT NULL,                          -- the exposure's scientific id, from its producer
    dateobs timestamp without time zone NOT NULL,    -- DATE-OBS
    exptime real NOT NULL,                           -- EXPTIME
    program_id integer,                              -- ASDF meta.observation.program
    UNIQUE (dataset, expid, job)
);

-- l2files with dev's l2filemeta centre and corners folded in.
CREATE TABLE l2files (
    rid integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job bigint NOT NULL REFERENCES jobs (id),
    dataset text NOT NULL,
    expid integer NOT NULL,
    sca smallint NOT NULL REFERENCES scas (sca),     -- SCA-NUM
    fid smallint NOT NULL REFERENCES filters (fid),
    field integer NOT NULL,                          -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,
    hp9 integer NOT NULL,
    dateobs timestamp without time zone NOT NULL,    -- DATE-OBS
    mjdobs double precision NOT NULL,                -- MJD-OBS
    exptime real NOT NULL,                           -- EXPTIME
    infobits integer DEFAULT 0 NOT NULL,
    filename character varying(255) NOT NULL,
    checksum character varying(32) NOT NULL,         -- MD5 of the file
    status smallint DEFAULT 0 NOT NULL,
    crval1 double precision NOT NULL,
    crval2 double precision NOT NULL,
    crpix1 real NOT NULL,
    crpix2 real NOT NULL,
    cd11 double precision NOT NULL,
    cd12 double precision NOT NULL,
    cd21 double precision NOT NULL,
    cd22 double precision NOT NULL,
    ra double precision NOT NULL,                    -- RA_TARG
    dec double precision NOT NULL,                   -- DEC_TARG
    zptmag real,
    limmag real,                                     -- 5-sigma limiting magnitude, computed before publish
    overlapfields integer[] DEFAULT '{}'::integer[] NOT NULL,
    ra0 double precision NOT NULL,                   -- centre
    dec0 double precision NOT NULL,
    ra1 double precision NOT NULL,                   -- corners
    dec1 double precision NOT NULL,
    ra2 double precision NOT NULL,
    dec2 double precision NOT NULL,
    ra3 double precision NOT NULL,
    dec3 double precision NOT NULL,
    ra4 double precision NOT NULL,
    dec4 double precision NOT NULL,
    created timestamp without time zone DEFAULT now() NOT NULL,
    UNIQUE (dataset, expid, sca, fid, job)
);

CREATE INDEX l2files_job_idx ON l2files (job);
CREATE INDEX l2files_field_idx ON l2files (field);
CREATE INDEX l2files_mjdobs_idx ON l2files (mjdobs);
CREATE INDEX l2files_overlapfields_idx ON l2files USING gin (overlapfields);

-- refimages with dev's refimmeta columns the team reads folded in.
CREATE TABLE refimages (
    rfid integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job bigint NOT NULL REFERENCES jobs (id),
    dataset text NOT NULL,
    field integer NOT NULL,
    fid smallint NOT NULL REFERENCES filters (fid),
    filename character varying(255) NOT NULL,
    checksum character varying(32) NOT NULL,
    status smallint DEFAULT 0 NOT NULL,
    infobits integer DEFAULT 0 NOT NULL,
    nframes smallint,                                -- images in the stack
    cov5percent real,                                -- coverage-depth QA metric
    created timestamp without time zone DEFAULT now() NOT NULL,
    UNIQUE (dataset, field, fid, job)
);

CREATE INDEX refimages_job_idx ON refimages (job);

-- Reference membership: which L2 rows built a reference (child of refimages).
CREATE TABLE refimimages (
    job bigint NOT NULL REFERENCES jobs (id),        -- the reference's job
    rfid integer NOT NULL,                           -- the reference row
    rid integer NOT NULL,                            -- an L2 row it was built from
    UNIQUE (job, rfid, rid)
);

-- A reference's mosaic catalogs, keyed by reference and catalog type
-- (child of refimages).
CREATE TABLE refimcatalogs (
    rfcatid integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job bigint NOT NULL REFERENCES jobs (id),        -- the reference's job
    rfid integer NOT NULL,
    cattype smallint NOT NULL,
    filename character varying(255) NOT NULL,
    checksum character varying(32) NOT NULL,
    UNIQUE (job, rfid, cattype)
);

CREATE TABLE diffimages (
    pid integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job bigint NOT NULL REFERENCES jobs (id),
    dataset text NOT NULL,
    expid integer NOT NULL,
    sca smallint NOT NULL REFERENCES scas (sca),
    fid smallint NOT NULL REFERENCES filters (fid),
    rid integer,                                     -- the L2 row it differenced (lineage, not a join key)
    rfid integer,                                    -- the reference row it used (lineage, not a join key)
    field integer NOT NULL,
    jd double precision,                             -- Julian date of start of image
    ra0 double precision NOT NULL,                   -- centre
    dec0 double precision NOT NULL,
    ra1 double precision NOT NULL,                   -- corners
    dec1 double precision NOT NULL,
    ra2 double precision NOT NULL,
    dec2 double precision NOT NULL,
    ra3 double precision NOT NULL,
    dec3 double precision NOT NULL,
    ra4 double precision NOT NULL,
    dec4 double precision NOT NULL,
    infobitssci integer NOT NULL,
    infobitsref integer NOT NULL,
    filename text NOT NULL,
    checksum character varying(32),
    status smallint DEFAULT 0 NOT NULL,
    created timestamp without time zone DEFAULT now() NOT NULL,
    UNIQUE (dataset, expid, sca, fid, job)
);

CREATE INDEX diffimages_job_idx ON diffimages (job);
CREATE INDEX diffimages_field_idx ON diffimages (field);
CREATE INDEX diffimages_jd_idx ON diffimages (jd);

CREATE TABLE psfs (
    psfid integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job bigint NOT NULL REFERENCES jobs (id),
    dataset text NOT NULL,
    fid smallint NOT NULL REFERENCES filters (fid),
    sca smallint NOT NULL REFERENCES scas (sca),
    kind text NOT NULL CHECK (kind IN ('science', 'reference')), -- a science image's PSF or a reference's
    filename character varying(255) NOT NULL,
    checksum character varying(32) NOT NULL,
    status smallint DEFAULT 0 NOT NULL,
    created timestamp without time zone DEFAULT now() NOT NULL,
    UNIQUE (dataset, fid, sca, kind, job)
);

CREATE INDEX psfs_job_idx ON psfs (job);

-- One source set per (dataset, expid, sca, fid) and job; a source is
-- (source set job, sid). The columns the alert provider's Source reads.
CREATE TABLE sources (
    job bigint NOT NULL REFERENCES jobs (id),        -- the source set's job
    sid bigint NOT NULL,                             -- the source's id within its set
    dataset text NOT NULL,
    expid integer NOT NULL,
    sca smallint NOT NULL,
    fid smallint NOT NULL,
    pid integer NOT NULL,                            -- the difference-image row it was found on
    id integer NOT NULL,                             -- photutils catalog id
    isdiffpos boolean DEFAULT true NOT NULL,
    field integer NOT NULL,
    hp6 integer NOT NULL,
    hp9 integer NOT NULL,
    mjdobs double precision NOT NULL,
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    xfit real NOT NULL,
    yfit real NOT NULL,
    xerr real NOT NULL,
    yerr real NOT NULL,
    fluxfit real NOT NULL,
    fluxerr real NOT NULL,
    flags smallint NOT NULL,
    qfit real NOT NULL,
    cfit real NOT NULL,
    redchi real DEFAULT 0.0 NOT NULL,
    npixfit smallint NOT NULL,
    sharpness real NOT NULL,
    roundness1 real NOT NULL,
    roundness2 real NOT NULL,
    peak real NOT NULL,
    UNIQUE (job, sid)
);

-- An object set: every object of one field, with sufficient sums.
-- Object ids are position hashes of the founding source.
CREATE TABLE objects (
    job bigint NOT NULL REFERENCES jobs (id),        -- the object set's job
    aid bigint NOT NULL,
    dataset text NOT NULL,
    survey text NOT NULL,
    field integer NOT NULL,
    ra0 double precision NOT NULL,                   -- founding position
    dec0 double precision NOT NULL,
    nsources integer NOT NULL,                       -- count
    sumra double precision NOT NULL,
    sumra2 double precision NOT NULL,
    sumdec double precision NOT NULL,
    sumdec2 double precision NOT NULL,
    sumflux double precision NOT NULL,
    sumflux2 double precision NOT NULL,
    UNIQUE (dataset, survey, field, aid, job)
);

CREATE INDEX objects_job_idx ON objects (job, aid);

-- An object set's new memberships (this epoch's matches); many-to-many.
CREATE TABLE memberships (
    job bigint NOT NULL REFERENCES jobs (id),        -- the object set's job
    aid bigint NOT NULL,
    source_job bigint NOT NULL REFERENCES jobs (id), -- the source set's job
    sid bigint NOT NULL,
    UNIQUE (job, aid, source_job, sid)
);

CREATE INDEX memberships_source_idx ON memberships (source_job, sid);

-- The packet index: one row per (packet set, source); the Avro bytes are at
-- (filename, byte_offset, byte_length).
CREATE TABLE packets (
    job bigint NOT NULL REFERENCES jobs (id),        -- the packet set's job
    dataset text NOT NULL,                           -- packet set key
    expid integer NOT NULL,
    sca smallint NOT NULL,
    fid smallint NOT NULL,
    object_job bigint NOT NULL REFERENCES jobs (id), -- the object set
    aid bigint NOT NULL,
    source_job bigint NOT NULL REFERENCES jobs (id), -- the source set
    sid bigint NOT NULL,
    trigger_mjd double precision NOT NULL,
    band character varying(16) NOT NULL,
    schema_version text NOT NULL,
    filename text NOT NULL,
    byte_offset bigint NOT NULL,
    byte_length bigint NOT NULL,
    UNIQUE (job, source_job, sid)
);

CREATE INDEX packets_object_idx ON packets (object_job, aid);

-- Forced photometry rows of a packet set (dev's FORCED_TABLE_DTYPE).
CREATE TABLE forced (
    job bigint NOT NULL REFERENCES jobs (id),        -- the packet set's job
    forced_id bigint NOT NULL,                       -- hash of image, object and position
    aid bigint NOT NULL,
    rid integer NOT NULL,                            -- the L2 row measured
    pid integer NOT NULL,                            -- the difference-image row measured
    expid integer NOT NULL,
    sca smallint NOT NULL,
    fid smallint NOT NULL,
    band character varying(16) NOT NULL,
    mjdobs double precision NOT NULL,
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    x real NOT NULL,
    y real NOT NULL,
    psf_flux double precision,
    psf_fluxerr double precision,
    science_flux double precision,
    science_fluxerr double precision,
    flags integer NOT NULL,
    time_proc double precision NOT NULL,
    UNIQUE (job, forced_id)
);

CREATE INDEX forced_aid_idx ON forced (aid);

-- Reference catalogs as products: Gaia, NED, NED-LVS, solar-system predictions.
CREATE TABLE catalogs (
    job bigint NOT NULL REFERENCES jobs (id),
    name text NOT NULL,
    version text NOT NULL,
    location text NOT NULL,
    UNIQUE (name, version, job)
);

-- Gaia match rows of a packet set, each naming the catalog job matched against.
CREATE TABLE external (
    job bigint NOT NULL REFERENCES jobs (id),        -- the packet set's job
    source_job bigint NOT NULL REFERENCES jobs (id),
    sid bigint NOT NULL,
    catalog_job bigint NOT NULL REFERENCES jobs (id),
    catid text NOT NULL,                             -- the catalog's own source id
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    sep double precision NOT NULL,
    pa double precision NOT NULL,
    UNIQUE (job, source_job, sid, catalog_job, catid)
);

--------------------------------------------------------------------------------
-- Official views: plain joins of rows to open bindings, every column plus
-- the binding id and the run.
--------------------------------------------------------------------------------

CREATE VIEW exposures_official AS
SELECT t.*, o.id AS binding, j.run
FROM exposures t
JOIN official o ON o.closed_at IS NULL AND o.product = 'exposures' AND o.job = t.job
    AND o.key = jsonb_build_object('dataset', t.dataset, 'expid', t.expid)
JOIN jobs j ON j.id = t.job;

CREATE VIEW l2files_official AS
SELECT t.*, o.id AS binding, j.run
FROM l2files t
JOIN official o ON o.closed_at IS NULL AND o.product = 'l2files' AND o.job = t.job
    AND o.key = jsonb_build_object('dataset', t.dataset, 'expid', t.expid, 'sca', t.sca, 'fid', t.fid)
JOIN jobs j ON j.id = t.job;

CREATE VIEW refimages_official AS
SELECT t.*, o.id AS binding, j.run
FROM refimages t
JOIN official o ON o.closed_at IS NULL AND o.product = 'refimages' AND o.job = t.job
    AND o.key = jsonb_build_object('dataset', t.dataset, 'field', t.field, 'fid', t.fid)
JOIN jobs j ON j.id = t.job;

CREATE VIEW refimimages_official AS
SELECT t.*, o.id AS binding, j.run
FROM refimimages t
JOIN official o ON o.closed_at IS NULL AND o.product = 'refimages' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW refimcatalogs_official AS
SELECT t.*, o.id AS binding, j.run
FROM refimcatalogs t
JOIN official o ON o.closed_at IS NULL AND o.product = 'refimages' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW diffimages_official AS
SELECT t.*, o.id AS binding, j.run
FROM diffimages t
JOIN official o ON o.closed_at IS NULL AND o.product = 'diffimages' AND o.job = t.job
    AND o.key = jsonb_build_object('dataset', t.dataset, 'expid', t.expid, 'sca', t.sca, 'fid', t.fid)
JOIN jobs j ON j.id = t.job;

CREATE VIEW psfs_official AS
SELECT t.*, o.id AS binding, j.run
FROM psfs t
JOIN official o ON o.closed_at IS NULL AND o.product = 'psfs' AND o.job = t.job
    AND o.key = jsonb_build_object('dataset', t.dataset, 'fid', t.fid, 'sca', t.sca, 'kind', t.kind)
JOIN jobs j ON j.id = t.job;

CREATE VIEW sources_official AS
SELECT t.*, o.id AS binding, j.run
FROM sources t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW objects_official AS
SELECT t.*, o.id AS binding, j.run
FROM objects t
JOIN official o ON o.closed_at IS NULL AND o.product = 'objects' AND o.job = t.job
    AND o.key = jsonb_build_object('dataset', t.dataset, 'survey', t.survey, 'field', t.field)
JOIN jobs j ON j.id = t.job;

CREATE VIEW memberships_official AS
SELECT t.*, o.id AS binding, j.run
FROM memberships t
JOIN official o ON o.closed_at IS NULL AND o.product = 'objects' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW packets_official AS
SELECT t.*, o.id AS binding, j.run
FROM packets t
JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW forced_official AS
SELECT t.*, o.id AS binding, j.run
FROM forced t
JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW external_official AS
SELECT t.*, o.id AS binding, j.run
FROM external t
JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

CREATE VIEW catalogs_official AS
SELECT t.*, o.id AS binding, j.run
FROM catalogs t
JOIN official o ON o.closed_at IS NULL AND o.product = 'catalogs' AND o.job = t.job
    AND o.key = jsonb_build_object('name', t.name, 'version', t.version)
JOIN jobs j ON j.id = t.job;

-- The official object set's full membership: the memberships of every set
-- in its predecessor chain (each object set job's `objects` input), except
-- those whose source set any set in the chain excludes. `set_job` is the
-- official set the row belongs to; `job` is the set that recorded it.
CREATE VIEW members_official AS
WITH RECURSIVE chain (set_job, binding, job) AS (
    SELECT o.job, o.id, o.job
    FROM official o
    WHERE o.closed_at IS NULL AND o.product = 'objects'
    UNION ALL
    SELECT c.set_job, c.binding, i.source_job
    FROM chain c
    JOIN job_inputs i ON i.job = c.job AND i.kind = 'input' AND i.product = 'objects'
    WHERE i.source_job IS NOT NULL
)
SELECT m.*, c.set_job, c.binding, j.run
FROM chain c
JOIN memberships m ON m.job = c.job
JOIN jobs j ON j.id = c.set_job
WHERE NOT EXISTS (
    SELECT 1
    FROM chain x
    JOIN job_inputs e ON e.job = x.job AND e.kind = 'excludes'
    WHERE x.set_job = c.set_job AND e.source_job = m.source_job
);

--------------------------------------------------------------------------------
-- Q3C: the extension and its indexes on image centres and object positions.
-- A developer's container may lack q3c; then this block says so and the
-- schema is complete without them.
--------------------------------------------------------------------------------

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS q3c;
    CREATE INDEX l2files_radec_idx ON l2files (q3c_ang2ipix(ra0, dec0));
    CREATE INDEX diffimages_radec_idx ON diffimages (q3c_ang2ipix(ra0, dec0));
    CREATE INDEX objects_radec_idx ON objects (q3c_ang2ipix(ra0, dec0));
    CREATE INDEX fields_radec_idx ON fields (q3c_ang2ipix(ra0, dec0));
EXCEPTION WHEN feature_not_supported OR undefined_file THEN
    RAISE NOTICE 'q3c is not available (%); the q3c indexes are not created', SQLERRM;
END
$$;
