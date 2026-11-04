-- xobjects and xmemberships, dev's XAstroObjects and XMerges (xobjects): the
-- xsources' object catalog, written by the SFFT difference job's cross-match
-- of its xsources and reconciled at publish, split into child tables by field
-- like objects and memberships. schema.sql carries them for a fresh database;
-- this file brings an existing one to it, applied once, in one transaction, as
-- postgres, after 2026-11-04-field-children.sql. It only adds: two empty
-- parents, their readers and views, create_field_child taking them as parents
-- (CREATE OR REPLACE keeps its owner and its EXECUTE grant) and job_fields
-- naming them. The new tables reach the pipeline's roles through the
-- schema's default privileges.

BEGIN;

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

DO $$
BEGIN
    CREATE INDEX xobjects_radec_idx ON xobjects (q3c_ang2ipix(ra0, dec0));
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'q3c is not available (%); xobjects_radec_idx is not created', SQLERRM;
END
$$;

ALTER TABLE job_fields DROP CONSTRAINT job_fields_parent_check;
ALTER TABLE job_fields ADD CONSTRAINT job_fields_parent_check
    CHECK (parent IN ('objects', 'memberships', 'packets', 'forced', 'xobjects', 'xmemberships'));

CREATE OR REPLACE FUNCTION create_field_child(parent text, field integer)
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

CREATE FUNCTION xobjects_in(fields integer[]) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::xobjects, fields, NULL) $$;
CREATE FUNCTION xobjects_of(jobs bigint[]) RETURNS SETOF xobjects
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::xobjects, job_fields_of('xobjects', jobs), jobs) $$;
CREATE FUNCTION xmemberships_in(fields integer[]) RETURNS SETOF xmemberships
LANGUAGE sql STABLE AS $$ SELECT * FROM field_rows(NULL::xmemberships, fields, NULL) $$;
CREATE FUNCTION xmemberships_of(jobs bigint[]) RETURNS SETOF xmemberships
LANGUAGE sql STABLE AS $$
    SELECT * FROM field_rows(NULL::xmemberships, job_fields_of('xmemberships', jobs), jobs) $$;

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

COMMIT;
