-- The object catalog (night-2026-10-24 task 7; the plan's amendment of
-- 2026-10-24: association in the difference job over an append-only
-- per-field catalog, reconcile at publish, the set chain retired). schema.sql
-- carries the new tables and views for a fresh database; this file brings an
-- existing one to it, applied once, in one transaction.
--
-- The chain's tables are renamed, not dropped: their rows (the associate
-- stage's object sets and memberships, runs 1 to 68) stay readable as
-- objects_chain and memberships_chain. The chain's three views go (a view
-- holds no rows). job_inputs keeps its `excludes` kind for those runs' rows.
-- The new tables reach the pipeline's roles through the schema's default
-- privileges (docs/foundation-smdc-step.md, part 3 of the SMDC step).

BEGIN;

DROP VIEW members_official;
DROP VIEW memberships_official;
DROP VIEW objects_official;

ALTER TABLE objects RENAME TO objects_chain;
ALTER TABLE memberships RENAME TO memberships_chain;
ALTER INDEX objects_job_idx RENAME TO objects_chain_job_idx;
ALTER INDEX objects_dataset_survey_field_aid_job_key RENAME TO objects_chain_dataset_survey_field_aid_job_key;
ALTER INDEX memberships_source_idx RENAME TO memberships_chain_source_idx;
ALTER INDEX memberships_job_aid_source_job_sid_key RENAME TO memberships_chain_job_aid_source_job_sid_key;
ALTER INDEX IF EXISTS objects_radec_idx RENAME TO objects_chain_radec_idx;

-- The object catalog, per run and field, append-only (plan amendment
-- 2026-10-24): an object is founded by the difference job whose unmatched
-- source it was; its id is the position hash of that source. The publisher
-- reconciles a job's founded objects against those founded since its read
-- (rapidpipe.foundation.catalog), so no two jobs found one object.
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

-- A difference job's memberships: each of its sources (source_job, sid) in
-- each object (object_job, aid) it matched, with the source's share of the
-- object's sufficient sums: its offsets from the object's founding position
-- (degrees, RA wrapped into [-180, 180)) and its fluxfit
-- (rapidpipe.science.statistics.sums). source_job is the job itself.
CREATE TABLE memberships (
    job bigint NOT NULL REFERENCES jobs (id),        -- the difference job
    object_job bigint NOT NULL REFERENCES jobs (id), -- the object's founding job
    aid bigint NOT NULL,
    source_job bigint NOT NULL REFERENCES jobs (id), -- the source set's job
    sid bigint NOT NULL,
    dra double precision NOT NULL,
    ddec double precision NOT NULL,
    flux double precision NOT NULL,
    UNIQUE (job, object_job, aid, source_job, sid)
);

CREATE INDEX memberships_source_idx ON memberships (source_job, sid);
CREATE INDEX memberships_object_idx ON memberships (object_job, aid);

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

DO $$
BEGIN
    CREATE INDEX objects_radec_idx ON objects (q3c_ang2ipix(ra0, dec0));
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'q3c is not available (%); objects_radec_idx is not created', SQLERRM;
END
$$;

COMMIT;
