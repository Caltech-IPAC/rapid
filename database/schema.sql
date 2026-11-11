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
--   catalogs                           dataset, name, version
--
-- A set (a reference image, a source set, a packet set) is identified by its
-- job: its child rows (refimimages, refimcatalogs, sources, objects,
-- memberships, packets, forced, external) carry the set's job and their own
-- id, and their views join through the set's open binding on (product, job).
-- The object catalog has no binding of its own (plan amendment 2026-10-24):
-- a difference job's founded objects and memberships ride on its source
-- set's binding.
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
    settings jsonb NOT NULL,                         -- the full settings body, resolved
    layers jsonb,                                    -- its hash and the layers it was resolved from (rapidpipe.foundation.layers); null on earlier runs
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
        CHECK (state IN ('launched', 'running', 'published', 'failed', 'declined')),  -- declined: the stage ended normally and published nothing (a reference short of its coverage), its manifest and reason on the row
    image_digest text,                               -- none for a local checkout
    instance_type text,                              -- the EC2 instance type it ran on; null when unknown
    settings_hash text,
    published_attempt integer,
    manifest jsonb,
    refusal text,                                    -- why the automatic binding was refused, why Batch ended it unrecorded, why the stage failed (a named failure's message, any other exception's type and message), or why it declined
    launched timestamp with time zone DEFAULT now() NOT NULL,
    started timestamp with time zone,
    ended timestamp with time zone,
    exit_code integer,
    kafka_published_at timestamp with time zone,      -- every packet of its set acknowledged by the topic
    kafka_partition integer,                         -- the partition its set's acknowledgements named
    kafka_first_offset bigint,                       -- the first and last offsets they gave; other sets interleave between
    kafka_last_offset bigint,
    batch_job_id text,                               -- the Batch job of its latest submission; null for a local job
    attempts jsonb DEFAULT '{}'::jsonb NOT NULL,     -- one entry per Batch submission, keyed by Batch job id (rapidpipe.foundation.attempts)
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

-- A job's inputs, chosen at launch, or recorded at its read by the publisher
-- from the manifest's `reads` (a difference job's object catalog: one
-- `objects` row per founding job whose objects it read; a packet job's
-- members and forced window):
--   input     a set the job reads (product, key, source_job)
--   expects   per output key, the open binding the job expects to replace
--             (binding), or none (binding null)
-- SMDC also holds `excludes` rows, the retired set chain's (2026-10-24).
CREATE TABLE job_inputs (
    job bigint NOT NULL REFERENCES jobs (id),
    kind text NOT NULL CHECK (kind IN ('input', 'expects')),
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
    program_id integer,                              -- the data source's program (dev's RAPIDDATASOURCE rule)
    -- The Roman observation identifiers, ASDF meta.observation, dev's
    -- migration 20261015 (5ba96e38): null for a source that has none.
    execution_plan smallint,                         -- EXECPLAN
    pass smallint,                                   -- PASS
    segment smallint,                                -- SEGMENT
    observation smallint,                            -- OBSERVTN
    visit smallint,                                  -- VISIT
    exposure smallint,                               -- EXPOSURE
    visit_id character varying(19),                 -- VISIT_ID
    observation_id character varying(28),           -- OBS_ID
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
    covfrac real,                                    -- fraction of its pixels at covdepth or deeper, from its coverage map; null unless selected by coverage
    covdepth real,                                   -- the depth covfrac counts, [reference.selection] min_coverage_depth [frames]
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
-- Split as dev's sources are, by observation date and SCA: the rows live in
-- child tables that inherit this one, one per UT day of mjdobs and SCA,
-- sources_<yyyymmdd>_<sca>, made by create_catalog_child (below) when a set
-- first needs it and loaded by COPY (rapidpipe.foundation.publish). The parent
-- holds no rows: sources_holds_no_rows, which its children do not inherit.
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
    flags smallint NOT NULL,                         -- photutils' fit bits (to 1 << 11); RAPID's 1 << 14 = 16384 the bright
                                                     -- footprint, informational: eligible is flags & ~16384 = 0 (rapidpipe.products.diffimage)
    qfit real NOT NULL,
    cfit real NOT NULL,
    redchi real DEFAULT 0.0 NOT NULL,
    npixfit smallint NOT NULL,
    sharpness real NOT NULL,
    roundness1 real NOT NULL,
    roundness2 real NOT NULL,
    peak real NOT NULL,
    sumrat real,                                     -- dev's pixel columns (rapidpipe.science.difference.columns),
    nneg smallint,                                   -- on a 5 x 5 stamp after a 64 px local background; null where
    nbad smallint,                                   -- unmeasurable (stamp off the image) and on rows made before them
    rb real,                                         -- dev's RuBR-AT score (rapidpipe.science.difference.realbogus), SFFT
    rblabel smallint,                                -- only; label 1 real, 0 bogus, -1 not scored; null: not computed
    UNIQUE (job, sid)
);

ALTER TABLE sources ADD CONSTRAINT sources_holds_no_rows CHECK (false) NO INHERIT;

-- A source set's SExtractor sources: dev's xsources, every source of the SFFT
-- difference image's positive and negative SExtractor catalogs that passes
-- dev's loader cuts, with the extra columns dev computes on them (rapidpipe.
-- science.difference.xsources). Written by the difference job beside its
-- sources, a child of its source set: an xsource is (source set job, xsid), and
-- it is official when its set is (xsources_official). Split as sources is, into
-- child tables that inherit this one, xsources_<yyyymmdd>_<sca>, loaded by COPY;
-- the parent holds no rows.
CREATE TABLE xsources (
    job bigint NOT NULL REFERENCES jobs (id),        -- the source set's job
    xsid bigint NOT NULL,                            -- the xsource's id within its set
    dataset text NOT NULL,
    expid integer NOT NULL,
    sca smallint NOT NULL,
    fid smallint NOT NULL,
    pid integer NOT NULL,                            -- the difference-image row it was found on
    num integer NOT NULL,                            -- NUMBER in its SExtractor catalog (one per sign)
    isdiffpos boolean DEFAULT true NOT NULL,         -- t: the positive catalog, f: the negative
    ra double precision NOT NULL,                    -- ALPHAWIN_J2000
    dec double precision NOT NULL,                   -- DELTAWIN_J2000
    x real NOT NULL,                                 -- XWIN_IMAGE (one-based)
    y real NOT NULL,                                 -- YWIN_IMAGE (one-based)
    fluxap real NOT NULL,                            -- FLUX_APER, FLUX_APER_1 ... FLUX_APER_5
    fluxap1 real NOT NULL,
    fluxap2 real NOT NULL,
    fluxap3 real NOT NULL,
    fluxap4 real NOT NULL,
    fluxap5 real NOT NULL,
    fluxerrap real NOT NULL,                         -- FLUXERR_APER, FLUXERR_APER_1 ... FLUXERR_APER_5
    fluxerrap1 real NOT NULL,
    fluxerrap2 real NOT NULL,
    fluxerrap3 real NOT NULL,
    fluxerrap4 real NOT NULL,
    fluxerrap5 real NOT NULL,
    awinworld real NOT NULL,                         -- AWIN_WORLD
    bwinworld real NOT NULL,                         -- BWIN_WORLD
    awinimage real NOT NULL,                         -- AWIN_IMAGE
    bwinimage real NOT NULL,                         -- BWIN_IMAGE
    fwhmimage real NOT NULL,                         -- FWHM_IMAGE
    classstar real NOT NULL,                         -- CLASS_STAR
    flags smallint NOT NULL,                         -- SExtractor's FLAGS
    field integer NOT NULL,
    hp6 integer NOT NULL,
    hp9 integer NOT NULL,
    mjdobs double precision NOT NULL,
    rb real,                                         -- dev's extra columns; null: not computed
    nneg smallint,
    nbad smallint,
    sumrat real,
    sharpness real,
    roundness1 real,
    roundness2 real,
    fluxfit real,
    snrfit real,
    redchi real,
    npixfit smallint,
    flagsfit smallint,
    cfit real,
    rblabel smallint,
    UNIQUE (job, xsid)
);

ALTER TABLE xsources ADD CONSTRAINT xsources_holds_no_rows CHECK (false) NO INHERIT;

-- The object catalog, per run and field, append-only (plan amendment
-- 2026-10-24): an object is founded by the difference job whose unmatched
-- source it was; its id is the position hash of that source. The publisher
-- reconciles a job's founded objects against those founded since its read
-- (rapidpipe.foundation.catalog), so no two jobs found one object.
--
-- objects, memberships, packets and forced keep their rows in child tables
-- that inherit them, one per field, <parent>_<field>, as dev's astroobjects
-- and merges are split by field; each row is filed under its object's field,
-- so an object's row, memberships, forced photometry and packet rows share
-- one field's children. The parents hold no rows (create_field_child below).
CREATE TABLE objects (
    job bigint NOT NULL REFERENCES jobs (id),        -- the difference job that founded it
    aid bigint NOT NULL,
    dataset text NOT NULL,
    survey text NOT NULL,
    field integer NOT NULL,
    ra0 double precision NOT NULL,                   -- founding position, the origin of its members' offsets
    dec0 double precision NOT NULL,
    UNIQUE (dataset, survey, field, aid, job)
);

CREATE INDEX objects_job_idx ON objects (job, aid);
CREATE INDEX objects_field_idx ON objects (dataset, survey, field);

ALTER TABLE objects ADD CONSTRAINT objects_holds_no_rows CHECK (false) NO INHERIT;

-- A difference job's memberships: each of its sources (source_job, sid) in
-- each object (object_job, aid) it matched, with the source's share of the
-- object's sufficient sums: its offsets from the object's founding position
-- (degrees, RA wrapped into [-180, 180)) and its fluxfit
-- (rapidpipe.science.statistics.sums). source_job is the job itself.
CREATE TABLE memberships (
    job bigint NOT NULL REFERENCES jobs (id),        -- the difference job
    object_job bigint NOT NULL REFERENCES jobs (id), -- the object's founding job
    aid bigint NOT NULL,
    field integer NOT NULL,                          -- the object's field
    source_job bigint NOT NULL REFERENCES jobs (id), -- the source set's job
    sid bigint NOT NULL,
    dra double precision NOT NULL,
    ddec double precision NOT NULL,
    flux double precision NOT NULL,
    UNIQUE (job, object_job, aid, source_job, sid)
);

CREATE INDEX memberships_source_idx ON memberships (source_job, sid);
CREATE INDEX memberships_object_idx ON memberships (object_job, aid);

ALTER TABLE memberships ADD CONSTRAINT memberships_holds_no_rows CHECK (false) NO INHERIT;

-- The xsources' object catalog, dev's XAstroObjects (crossMatchXSources.py):
-- objects and memberships as above, of the xsources of the SFFT difference
-- image (both signs) instead of the sources. An xobject is founded by the
-- difference job whose unmatched xsource it was, with that xsource's
-- position, its aperture flux (flux0, dev's) and its field; the publisher
-- reconciles it as it does an object. Split by field like objects.
CREATE TABLE xobjects (
    job bigint NOT NULL REFERENCES jobs (id),        -- the difference job that founded it
    xaid bigint NOT NULL,
    dataset text NOT NULL,
    survey text NOT NULL,
    field integer NOT NULL,
    ra0 double precision NOT NULL,                   -- founding position, the origin of its members' offsets
    dec0 double precision NOT NULL,
    flux0 real NOT NULL,                             -- the founding xsource's fluxap
    UNIQUE (dataset, survey, field, xaid, job)
);

CREATE INDEX xobjects_job_idx ON xobjects (job, xaid);
CREATE INDEX xobjects_field_idx ON xobjects (dataset, survey, field);

ALTER TABLE xobjects ADD CONSTRAINT xobjects_holds_no_rows CHECK (false) NO INHERIT;

-- A difference job's xmemberships, dev's XMerges: each of its xsources
-- (source_job, xsid) in each xobject (object_job, xaid) it matched, with the
-- xsource's offsets from the xobject's founding position (degrees, RA wrapped
-- into [-180, 180)) and its fluxap, the xobject's sufficient sums.
CREATE TABLE xmemberships (
    job bigint NOT NULL REFERENCES jobs (id),        -- the difference job
    object_job bigint NOT NULL REFERENCES jobs (id), -- the xobject's founding job
    xaid bigint NOT NULL,
    field integer NOT NULL,                          -- the xobject's field
    source_job bigint NOT NULL REFERENCES jobs (id), -- the source set's job, whose xsources these are
    xsid bigint NOT NULL,
    dra double precision NOT NULL,
    ddec double precision NOT NULL,
    flux double precision NOT NULL,
    UNIQUE (job, object_job, xaid, source_job, xsid)
);

CREATE INDEX xmemberships_source_idx ON xmemberships (source_job, xsid);
CREATE INDEX xmemberships_object_idx ON xmemberships (object_job, xaid);

ALTER TABLE xmemberships ADD CONSTRAINT xmemberships_holds_no_rows CHECK (false) NO INHERIT;

-- The packet index: one row per (packet set, source); the Avro bytes are at
-- (filename, byte_offset, byte_length).
CREATE TABLE packets (
    job bigint NOT NULL REFERENCES jobs (id),        -- the packet set's job
    dataset text NOT NULL,                           -- packet set key
    expid integer NOT NULL,
    sca smallint NOT NULL,
    fid smallint NOT NULL,
    object_job bigint NOT NULL REFERENCES jobs (id), -- the object's founding job (an associate job's set before 2026-10-24)
    aid bigint NOT NULL,
    field integer NOT NULL,                          -- the object's field
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

ALTER TABLE packets ADD CONSTRAINT packets_holds_no_rows CHECK (false) NO INHERIT;

-- Forced photometry rows of a packet set (dev's FORCED_TABLE_DTYPE).
CREATE TABLE forced (
    job bigint NOT NULL REFERENCES jobs (id),        -- the packet set's job
    forced_id bigint NOT NULL,                       -- hash of image, object and position
    aid bigint NOT NULL,
    field integer NOT NULL,                          -- the object's field
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

ALTER TABLE forced ADD CONSTRAINT forced_holds_no_rows CHECK (false) NO INHERIT;

-- The fields each job's rows of the field-split tables were filed under,
-- written by the publisher with the rows, so a reader finds a job's children
-- without reading a parent.
CREATE TABLE job_fields (
    job bigint NOT NULL REFERENCES jobs (id),
    parent text NOT NULL CHECK (parent IN ('objects', 'memberships', 'packets', 'forced',
                                           'xobjects', 'xmemberships')),
    field integer NOT NULL,
    PRIMARY KEY (job, parent, field)
);

-- Reference catalogs as products: Gaia, NED, NED-LVS, solar-system predictions.
CREATE TABLE catalogs (
    job bigint NOT NULL REFERENCES jobs (id),
    dataset text NOT NULL,
    name text NOT NULL,
    version text NOT NULL,
    location text NOT NULL,
    UNIQUE (dataset, name, version, job)
);

-- Catalog match rows of a packet set (Gaia, NED, NED-LVS), each naming the catalog job
-- matched against; catid is Gaia's source_id or NED's preferred name.
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
-- Child tables of the catalog tables: one per UT observation date and SCA
--------------------------------------------------------------------------------

-- The child of `parent` that holds rows at `mjdobs` on `sca`, made if it does
-- not exist, as dev's loaders make theirs: a table LIKE the parent with its
-- defaults and its unique index, then INHERIT, which locks the
-- parent SHARE UPDATE EXCLUSIVE (loads and reads go on). Its CHECK holds it
-- to [floor(mjdobs), floor(mjdobs) + 1) and the SCA, so a query on a date or
-- an SCA skips the other children. MJD days begin at 0h UT, so the name's date
-- is the UT observation date. No foreign key to jobs, as dev's children have
-- none: adding one would lock jobs against every publish in flight; the
-- publisher sets job from the job row it holds. An advisory lock per child
-- makes two callers agree. SECURITY DEFINER: the pipeline's role holds table
-- grants only, so the schema's owner makes the tables, and EXECUTE is granted
-- to that role alone (docs/foundation-smdc-step.md).
-- The name of the child of `parent` holding rows at `mjdobs` on `sca`:
-- <parent>_<yyyymmdd>_<sca>, the UT date of mjdobs (MJD days begin at 0h UT).
CREATE FUNCTION catalog_child_name(parent text, mjdobs double precision, sca integer)
RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT parent || '_' || to_char(date '1858-11-17' + floor(mjdobs)::integer, 'YYYYMMDD') || '_' || sca
$$;

CREATE FUNCTION create_catalog_child(parent text, mjdobs double precision, sca integer)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    day double precision := floor(mjdobs);
    child text := catalog_child_name(parent, mjdobs, sca);
BEGIN
    IF parent <> ALL (ARRAY['sources', 'xsources']) THEN
        RAISE EXCEPTION 'create_catalog_child: % is not a catalog table with children', parent;
    END IF;
    IF to_regclass('public.' || quote_ident(child)) IS NOT NULL THEN
        RETURN child;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('create_catalog_child'), hashtext(child));
    -- After the wait, a query of pg_class: its snapshot sees a child another
    -- caller committed meanwhile, where to_regclass's cached lookup may not.
    IF NOT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relname = child) THEN
        EXECUTE format('CREATE TABLE public.%I (LIKE public.%I INCLUDING DEFAULTS INCLUDING INDEXES,'
                       ' CONSTRAINT %I CHECK (mjdobs >= %s AND mjdobs < %s AND sca = %s))',
                       child, parent, child || '_date_sca', day, day + 1, sca);
        EXECUTE format('ALTER TABLE public.%I INHERIT public.%I', child, parent);
    END IF;
    RETURN child;
END
$$;

REVOKE EXECUTE ON FUNCTION create_catalog_child(text, double precision, integer) FROM PUBLIC;

-- Reading the children a task needs, never a parent: a query on sources or
-- xsources (or their _official views) visits every child, and they grow by up
-- to 18 a night. A source set's child is named by its difference image's L2
-- mjdobs and SCA (the job that wrote the set wrote the image), so finding it
-- reads no parent; null when the set has no child of `parent` (no such rows).
-- An image with no L2 row (an imported or synthetic one without its lineage)
-- falls back to its own jd, which the stages set to the same mjdobs + 2400000.5.
CREATE FUNCTION source_set_child(parent text, job bigint)
RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT c.relname::text
    FROM diffimages d
    LEFT JOIN l2files l ON l.rid = d.rid
    JOIN pg_class c
        ON c.relname = catalog_child_name(parent, coalesce(l.mjdobs, d.jd - 2400000.5), d.sca)
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
    WHERE d.job = source_set_child.job
$$;

-- The rows of the source sets `jobs`, read from their children only:
-- FROM sources_of(ARRAY[...]) s where a query would read FROM sources s.
CREATE FUNCTION sources_of(jobs bigint[])
RETURNS SETOF sources
LANGUAGE plpgsql STABLE AS $$
DECLARE
    child text;
BEGIN
    FOR child IN SELECT DISTINCT source_set_child('sources', j) FROM unnest(jobs) j LOOP
        CONTINUE WHEN child IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I WHERE job = ANY($1)', child) USING jobs;
    END LOOP;
END
$$;

-- The xsources of the source sets `jobs`, read from their children only.
CREATE FUNCTION xsources_of(jobs bigint[])
RETURNS SETOF xsources
LANGUAGE plpgsql STABLE AS $$
DECLARE
    child text;
BEGIN
    FOR child IN SELECT DISTINCT source_set_child('xsources', j) FROM unnest(jobs) j LOOP
        CONTINUE WHEN child IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I WHERE job = ANY($1)', child) USING jobs;
    END LOOP;
END
$$;

-- The child tables of objects, memberships, packets, forced, xobjects and
-- xmemberships, one per field, made as the catalog children are: by the publisher, as the schema's
-- owner, LIKE the parent with its defaults and indexes, then INHERIT, with a
-- CHECK holding the child to its field and no foreign key to jobs.
CREATE FUNCTION field_child_name(parent text, field integer)
RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT parent || '_' || field
$$;

CREATE FUNCTION create_field_child(parent text, field integer)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    child text := field_child_name(parent, field);
BEGIN
    IF parent <> ALL (ARRAY['objects', 'memberships', 'packets', 'forced', 'xobjects',
                            'xmemberships']) THEN
        RAISE EXCEPTION 'create_field_child: % is not a table with field children', parent;
    END IF;
    IF field IS NULL OR field < 0 THEN
        RAISE EXCEPTION 'create_field_child: field % is not a tessellation field', field;
    END IF;
    IF to_regclass('public.' || quote_ident(child)) IS NOT NULL THEN
        RETURN child;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('create_field_child'), hashtext(child));
    -- After the wait, a query of pg_class, as in create_catalog_child.
    IF NOT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relname = child) THEN
        EXECUTE format('CREATE TABLE public.%I (LIKE public.%I INCLUDING DEFAULTS INCLUDING INDEXES,'
                       ' CONSTRAINT %I CHECK (field = %s))', child, parent, child || '_field', field);
        EXECUTE format('ALTER TABLE public.%I INHERIT public.%I', child, parent);
    END IF;
    RETURN child;
END
$$;

REVOKE EXECUTE ON FUNCTION create_field_child(text, integer) FROM PUBLIC;

-- Reading the field children a task needs, never a parent. `parent` is a
-- null of the parent's row type (NULL::objects); the rows of the children of
-- `fields` that exist, of the jobs `jobs` when it is not null.
CREATE FUNCTION field_rows(parent anyelement, fields integer[], jobs bigint[])
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        IF jobs IS NULL THEN
            RETURN QUERY EXECUTE format('SELECT * FROM public.%I', child);
        ELSE
            RETURN QUERY EXECUTE format('SELECT * FROM public.%I WHERE job = ANY($1)', child)
                USING jobs;
        END IF;
    END LOOP;
END
$$;

-- field_rows of the run `run`'s jobs only: the run's filter inside each
-- child's query, so a field's rows of other runs are never returned through
-- the function (each RETURN QUERY holds its rows until the caller reads them,
-- in temp files past work_mem; throughput-record, run 228).
CREATE FUNCTION field_run_rows(parent anyelement, fields integer[], run bigint)
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I'
                                    ' WHERE job IN (SELECT id FROM public.jobs WHERE run = $1)', child)
            USING run;
    END LOOP;
END
$$;

-- The packet stage's three field reads, each query's filter inside each
-- child's query, of every run (packet-field-reads, run 229): the objects of
-- the (job, aid) pairs; their memberships of the job's own set `own` or an
-- open sources binding; forced rows on the images `pids` of an open packets
-- binding. Under xobjects and xmemberships the pairs' column is xaid.
CREATE FUNCTION packet_origin_rows(parent anyelement, fields integer[], jobs bigint[],
                                   aids bigint[])
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    aid text := CASE WHEN name = 'xobjects' THEN 'xaid' ELSE 'aid' END;
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I'
                                    ' WHERE (job, %I) IN (SELECT * FROM unnest($1, $2))', child, aid)
            USING jobs, aids;
    END LOOP;
END
$$;

CREATE FUNCTION packet_membership_rows(parent anyelement, fields integer[], jobs bigint[],
                                       aids bigint[], own bigint)
RETURNS SETOF anyelement
LANGUAGE plpgsql STABLE AS $$
DECLARE
    name text := (SELECT t.typname FROM pg_type t WHERE t.oid = pg_typeof(parent));
    aid text := CASE WHEN name = 'xmemberships' THEN 'xaid' ELSE 'aid' END;
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name(name, f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I m'
                                    ' WHERE (m.object_job, m.%I) IN (SELECT * FROM unnest($1, $2))'
                                    ' AND (m.job = $3 OR EXISTS (SELECT 1 FROM public.official o'
                                    ' WHERE o.closed_at IS NULL AND o.product = ''sources'''
                                    ' AND o.job = m.job))', child, aid)
            USING jobs, aids, own;
    END LOOP;
END
$$;

CREATE FUNCTION packet_reuse_rows(fields integer[], pids integer[])
RETURNS SETOF forced
LANGUAGE plpgsql STABLE AS $$
DECLARE
    child text;
BEGIN
    FOR child IN SELECT DISTINCT field_child_name('forced', f) FROM unnest(fields) f
                 WHERE f IS NOT NULL ORDER BY 1 LOOP
        CONTINUE WHEN to_regclass('public.' || quote_ident(child)) IS NULL;
        RETURN QUERY EXECUTE format('SELECT * FROM public.%I f WHERE f.pid = ANY($1)'
                                    ' AND EXISTS (SELECT 1 FROM public.official o'
                                    ' WHERE o.closed_at IS NULL AND o.product = ''packets'''
                                    ' AND o.job = f.job)', child)
            USING pids;
    END LOOP;
END
$$;

-- The fields the jobs `jobs` filed rows of `parent` under (job_fields).
CREATE FUNCTION job_fields_of(parent text, jobs bigint[])
RETURNS integer[]
LANGUAGE sql STABLE AS $$
    SELECT coalesce(array_agg(DISTINCT f.field ORDER BY f.field), '{}')
    FROM job_fields f WHERE f.parent = job_fields_of.parent AND f.job = ANY(jobs)
$$;

-- <parent>_in(fields): every row in those fields' children.
-- <parent>_of(jobs): the rows those jobs wrote, from the children they wrote.
-- <parent>_in(fields, run): the run's rows in those fields' children.
CREATE FUNCTION objects_in(fields integer[]) RETURNS SETOF objects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::objects, fields, NULL) $$;
CREATE FUNCTION objects_of(jobs bigint[]) RETURNS SETOF objects
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::objects, job_fields_of('objects', jobs), jobs) $$;
CREATE FUNCTION memberships_in(fields integer[]) RETURNS SETOF memberships
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::memberships, fields, NULL) $$;
CREATE FUNCTION memberships_of(jobs bigint[]) RETURNS SETOF memberships
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::memberships, job_fields_of('memberships', jobs), jobs) $$;
CREATE FUNCTION packets_in(fields integer[]) RETURNS SETOF packets
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::packets, fields, NULL) $$;
CREATE FUNCTION packets_of(jobs bigint[]) RETURNS SETOF packets
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::packets, job_fields_of('packets', jobs), jobs) $$;
CREATE FUNCTION forced_in(fields integer[]) RETURNS SETOF forced
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::forced, fields, NULL) $$;
CREATE FUNCTION forced_of(jobs bigint[]) RETURNS SETOF forced
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::forced, job_fields_of('forced', jobs), jobs) $$;
CREATE FUNCTION xobjects_in(fields integer[]) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::xobjects, fields, NULL) $$;
CREATE FUNCTION xobjects_of(jobs bigint[]) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::xobjects, job_fields_of('xobjects', jobs), jobs) $$;
CREATE FUNCTION objects_in(fields integer[], run bigint) RETURNS SETOF objects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_run_rows(NULL::objects, fields, run) $$;
CREATE FUNCTION xobjects_in(fields integer[], run bigint) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_run_rows(NULL::xobjects, fields, run) $$;
CREATE FUNCTION xmemberships_in(fields integer[]) RETURNS SETOF xmemberships
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::xmemberships, fields, NULL) $$;
CREATE FUNCTION xmemberships_of(jobs bigint[]) RETURNS SETOF xmemberships
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::xmemberships, job_fields_of('xmemberships', jobs), jobs) $$;

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

-- An official xsource: its source set is official.
CREATE VIEW xsources_official AS
SELECT t.*, o.id AS binding, j.run
FROM xsources t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

-- An official membership: its source set is official. A superseded source
-- set's memberships leave with it.
CREATE VIEW memberships_official AS
SELECT t.*, o.id AS binding, j.run
FROM memberships t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

-- An object with at least one official membership, its statistics the sum
-- of its official memberships' sufficient sums (rapidpipe.science.statistics.sums
-- turns them into means and spreads). An object whose memberships are all
-- superseded is absent here and stays in the catalog its run's difference
-- jobs read, so its id survives a re-difference.
CREATE VIEW objects_official AS
SELECT t.*, j.run, s.nsources, s.sumra, s.sumra2, s.sumdec, s.sumdec2, s.sumflux, s.sumflux2
FROM objects t
JOIN jobs j ON j.id = t.job
CROSS JOIN LATERAL (
    SELECT count(*)::integer AS nsources,
           sum(m.dra) AS sumra, sum(m.dra * m.dra) AS sumra2,
           sum(m.ddec) AS sumdec, sum(m.ddec * m.ddec) AS sumdec2,
           sum(m.flux) AS sumflux, sum(m.flux * m.flux) AS sumflux2
    FROM memberships m
    JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = m.job
    WHERE m.object_job = t.job AND m.aid = t.aid
) s
WHERE s.nsources > 0;

-- objects_official of the fields `fields`, read from their children only:
-- an object's memberships are filed under its field, so its sums are there.
CREATE FUNCTION objects_official_in(fields integer[])
RETURNS SETOF objects_official
LANGUAGE sql STABLE AS $$
    SELECT t.*, j.run, s.nsources, s.sumra, s.sumra2, s.sumdec, s.sumdec2, s.sumflux, s.sumflux2
    FROM objects_in(fields) t
    JOIN jobs j ON j.id = t.job
    JOIN (SELECT m.object_job, m.aid, count(*)::integer AS nsources,
                 sum(m.dra) AS sumra, sum(m.dra * m.dra) AS sumra2,
                 sum(m.ddec) AS sumdec, sum(m.ddec * m.ddec) AS sumdec2,
                 sum(m.flux) AS sumflux, sum(m.flux * m.flux) AS sumflux2
          FROM memberships_in(fields) m
          JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = m.job
          GROUP BY m.object_job, m.aid) s ON s.object_job = t.job AND s.aid = t.aid
$$;

-- An official xmembership: its source set is official (the xsources'
-- binding is the set's). A superseded source set's xmemberships leave with it.
CREATE VIEW xmemberships_official AS
SELECT t.*, o.id AS binding, j.run
FROM xmemberships t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

-- An xobject with at least one official xmembership, its statistics the sums
-- of its official xmemberships' (dev's XAstroObjectsMeta, here from the sums).
CREATE VIEW xobjects_official AS
SELECT t.*, j.run, s.nsources, s.sumra, s.sumra2, s.sumdec, s.sumdec2, s.sumflux, s.sumflux2
FROM xobjects t
JOIN jobs j ON j.id = t.job
CROSS JOIN LATERAL (
    SELECT count(*)::integer AS nsources,
           sum(m.dra) AS sumra, sum(m.dra * m.dra) AS sumra2,
           sum(m.ddec) AS sumdec, sum(m.ddec * m.ddec) AS sumdec2,
           sum(m.flux) AS sumflux, sum(m.flux * m.flux) AS sumflux2
    FROM xmemberships m
    JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = m.job
    WHERE m.object_job = t.job AND m.xaid = t.xaid
) s
WHERE s.nsources > 0;

-- xobjects_official of the fields `fields`, read from their children only.
CREATE FUNCTION xobjects_official_in(fields integer[])
RETURNS SETOF xobjects_official
LANGUAGE sql STABLE AS $$
    SELECT t.*, j.run, s.nsources, s.sumra, s.sumra2, s.sumdec, s.sumdec2, s.sumflux, s.sumflux2
    FROM xobjects_in(fields) t
    JOIN jobs j ON j.id = t.job
    JOIN (SELECT m.object_job, m.xaid, count(*)::integer AS nsources,
                 sum(m.dra) AS sumra, sum(m.dra * m.dra) AS sumra2,
                 sum(m.ddec) AS sumdec, sum(m.ddec * m.ddec) AS sumdec2,
                 sum(m.flux) AS sumflux, sum(m.flux * m.flux) AS sumflux2
          FROM xmemberships_in(fields) m
          JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = m.job
          GROUP BY m.object_job, m.xaid) s ON s.object_job = t.job AND s.xaid = t.xaid
$$;

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
    AND o.key = jsonb_build_object('dataset', t.dataset, 'name', t.name, 'version', t.version)
JOIN jobs j ON j.id = t.job;

-- The jobs' quality numbers, one row per job and metric (PR 350).
CREATE VIEW job_metrics AS
SELECT j.run, j.id AS job, j.stage, r.dataset, r.survey, j.state, j.scope,
       (j.scope ->> 'expid')::integer AS expid,
       (j.scope ->> 'sca')::smallint AS sca,
       (j.scope ->> 'fid')::smallint AS fid,
       (j.scope ->> 'field')::integer AS field,
       j.scope ->> 'file' AS file,
       m.metric,
       m.value
FROM jobs j
JOIN runs r ON r.id = j.run
CROSS JOIN LATERAL (
    SELECT e.key AS metric, (e.value #>> '{}')::double precision AS value
    FROM jsonb_each(j.manifest -> 'metrics') e
    WHERE jsonb_typeof(e.value) = 'number'
    UNION ALL
    SELECT 'container_seconds', extract(epoch FROM j.ended - j.started)::double precision
    WHERE j.state = 'published' AND j.started IS NOT NULL AND j.ended IS NOT NULL
    UNION ALL
    SELECT s.metric, s.value
    FROM (SELECT count(*) AS n,
                 count(*) FILTER (WHERE a.value ->> 'status_reason' LIKE 'Host EC2%') AS reclaims,
                 count(*) FILTER (WHERE a.value ? 'hedged') AS hedges,
                 coalesce(sum(extract(epoch FROM
                     coalesce(a.value ->> 'ended', a.value ->> 'stopped', a.value ->> 'hedged')::timestamptz
                     - (a.value ->> 'started')::timestamptz))
                     FILTER (WHERE a.value ? 'started'
                             AND coalesce(a.value ->> 'outcome', '') <> 'published'
                             AND coalesce(a.value ->> 'ended', a.value ->> 'stopped', a.value ->> 'hedged') IS NOT NULL),
                     0) AS lost
          FROM jsonb_each(j.attempts) a) t
    CROSS JOIN LATERAL (VALUES ('batch_submissions', t.n::double precision),
                               ('reclaims', t.reclaims::double precision),
                               ('hedges', t.hedges::double precision),
                               ('lost_seconds', t.lost::double precision)) s (metric, value)
    WHERE t.n > 0
) m;

COMMENT ON VIEW job_metrics IS
    'One row per job and metric: its manifest''s metrics (the stage that computed it, its units and denominator are in that stage''s module docstring: intake: masked_fraction; reference: rapidpipe.science.reference.measure.quality_metrics; difference: rapidpipe.stages.difference and rapidpipe.science.difference.metrics; packet: rapidpipe.stages.packet), container_seconds (a published job''s started to ended, its publishing attempt), and from jobs.attempts batch_submissions, reclaims (status reason Host EC2), hedges and lost_seconds (started to end of every submission not published). A metric a job could not compute is absent, never zero. The scope columns are null where the stage''s scope lacks the key.';

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
    CREATE INDEX xobjects_radec_idx ON xobjects (q3c_ang2ipix(ra0, dec0));
    CREATE INDEX fields_radec_idx ON fields (q3c_ang2ipix(ra0, dec0));
EXCEPTION WHEN feature_not_supported OR undefined_file THEN
    RAISE NOTICE 'q3c is not available (%); the q3c indexes are not created', SQLERRM;
END
$$;
