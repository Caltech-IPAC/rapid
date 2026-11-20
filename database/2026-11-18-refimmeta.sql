-- dev's RefImMeta, a reference's quality measurements, written by each
-- reference job (rapid_docs decision-refimmeta): the refimmeta table, its
-- indexes and refimmeta_official. schema.sql carries them for a fresh
-- database; this file brings an existing one to it, applied once, in one
-- transaction, as postgres. It only adds a table and a view, which reach the
-- pipeline's roles through the schema's default privileges
-- (docs/foundation-smdc-step.md, part 3 of the SMDC step). No row is
-- backfilled: references published before it have none.

BEGIN;

CREATE TABLE refimmeta (
    rfid integer PRIMARY KEY REFERENCES refimages (rfid),
    job bigint NOT NULL REFERENCES jobs (id),        -- the reference's job
    field integer NOT NULL,                          -- Roman tessellation index for (ra0,dec0)
    hp6 integer NOT NULL,                            -- level-6 HEALPix index (NESTED) for (ra0,dec0)
    hp9 integer NOT NULL,                            -- level-9 HEALPix index (NESTED) for (ra0,dec0)
    fid smallint NOT NULL REFERENCES filters (fid),
    nframes smallint NOT NULL,                       -- images in the stack
    mjdobsmin double precision NOT NULL,             -- minimum MJD of the input images
    mjdobsmax double precision NOT NULL,             -- maximum MJD of the input images
    npixnan integer NOT NULL,                        -- NaN pixels in the reference image
    clmean real NOT NULL,                            -- pixel mean after clipping
    clstddev real NOT NULL,                          -- pixel standard deviation after clipping and reinflating
    clnoutliers integer NOT NULL,                    -- pixels discarded in clipping
    gmedian real NOT NULL,                           -- global pixel median
    datascale real NOT NULL,                         -- global robust spread, 0.5*(p84-p16)
    gmin real NOT NULL,                              -- global minimum pixel value
    gmax real NOT NULL,                              -- global maximum pixel value
    cov5percent real NOT NULL,                       -- coverage-depth QA metric (depth of at least 5)
    medncov real NOT NULL,                           -- median of the depth-of-coverage image
    medpixunc real NOT NULL,                         -- median of the uncertainty image
    fwhmmedpix real NOT NULL,                        -- FWHM_IMAGE of the SExtractor catalog's point sources [pixels]
    fwhmminpix real NOT NULL,                        -- minimum FWHM_IMAGE [pixels]
    fwhmmaxpix real NOT NULL,                        -- maximum FWHM_IMAGE [pixels]
    nsxcatsources integer NOT NULL,                  -- sources in the SExtractor catalog
    npucatsources integer                            -- sources in the Photutils catalog (null: none is made)
);

CREATE INDEX refimmeta_job_idx ON refimmeta (job);
CREATE INDEX refimmeta_field_idx ON refimmeta (field);
CREATE INDEX refimmeta_hp6_idx ON refimmeta (hp6);
CREATE INDEX refimmeta_hp9_idx ON refimmeta (hp9);
CREATE INDEX refimmeta_fid_idx ON refimmeta (fid);
CREATE INDEX refimmeta_nframes_idx ON refimmeta (nframes);
CREATE INDEX refimmeta_cov5percent_idx ON refimmeta (cov5percent);

CREATE VIEW refimmeta_official AS
SELECT t.*, o.id AS binding, j.run
FROM refimmeta t
JOIN official o ON o.closed_at IS NULL AND o.product = 'refimages' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

COMMIT;
