-- objects and xobjects without survey (objects-without-survey): surveys
-- overlap, and an object's or xobject's members may come from more than one,
-- so neither names a survey; its run records the survey it was run for.
-- schema.sql carries the tables without the column for a fresh database; this
-- file brings an existing one to it, applied once, in one transaction, as
-- postgres, while no difference job is running: a job of an older image
-- writes the column and fails to load after this file, and a job of a newer
-- image omits it and fails to load before.
--
-- The column goes from each parent and from each of its field children (a
-- child made LIKE its parent keeps its own copy, and field_rows returns a
-- child's SELECT * as the parent's row type). Dropping it drops the unique
-- constraints and the field indexes that name it, which are made again
-- without it under the names a fresh schema.sql and create_field_child give
-- them. objects_official, xobjects_official and their _in readers name the
-- column through t.*, so they are dropped and made again as schema.sql has
-- them; they reach the pipeline's roles through the schema's default
-- privileges, as when 2026-11-04-xobjects.sql made them. The difference
-- jobs' recorded object reads drop the key's survey, so a read's key is its
-- product's key (scripts/validate_run.py). Nothing binds objects or
-- xobjects; the file refuses if anything does.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM official WHERE product IN ('objects', 'xobjects')) THEN
        RAISE EXCEPTION 'objects-without-survey: official names objects or xobjects; their keys carry survey';
    END IF;
END
$$;

DROP FUNCTION objects_official_in(integer[]);
DROP VIEW objects_official;
DROP FUNCTION xobjects_official_in(integer[]);
DROP VIEW xobjects_official;

DO $$
DECLARE
    parent text;
    ident text;
    child text;
BEGIN
    FOR parent, ident IN VALUES ('objects', 'aid'), ('xobjects', 'xaid') LOOP
        -- A child cannot drop an inherited column; once its parent has, it holds its own copy.
        EXECUTE format('ALTER TABLE public.%I DROP COLUMN survey', parent);
        FOR child IN SELECT c.relname FROM pg_inherits i
                     JOIN pg_class c ON c.oid = i.inhrelid
                     WHERE i.inhparent = ('public.' || parent)::regclass ORDER BY c.relname LOOP
            EXECUTE format('ALTER TABLE public.%I DROP COLUMN IF EXISTS survey', child);
            EXECUTE format('ALTER TABLE public.%I ADD UNIQUE (dataset, field, %I, job)', child, ident);
            EXECUTE format('CREATE INDEX ON public.%I (dataset, field)', child);
        END LOOP;
        EXECUTE format('ALTER TABLE public.%I ADD UNIQUE (dataset, field, %I, job)', parent, ident);
        EXECUTE format('CREATE INDEX %I ON public.%I (dataset, field)', parent || '_field_idx', parent);
    END LOOP;
END
$$;

UPDATE job_inputs SET key = key - 'survey'
WHERE product IN ('objects', 'xobjects') AND key ? 'survey';

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

COMMIT;
