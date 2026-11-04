-- objects, memberships, packets and forced split into child tables by field,
-- as dev's astroobjects and merges are (field-children). schema.sql carries
-- the parents with their new field columns, their no-rows checks, job_fields,
-- create_field_child and the readers for a fresh database; this file brings an
-- existing one to it, applied once, in one transaction, as postgres, with
-- nothing running on Batch (the bring-up check of docs/foundation-smdc-step.md,
-- part 1): it rewrites every row of the four tables and holds them locked
-- until it commits. Apply it after 2026-11-04-xsources.sql.
--
-- Each row is filed under its object's field: an object under its own; a
-- membership or a packet under the field of its object (object_job, aid); a
-- forced row under the field of the object its packet set carried it for
-- (its set's packet row of the same aid), or, failing that, the one field its
-- aid has in objects. A row that cannot be filed stops the file with the
-- counts and nothing is applied. The rows move: the old tables are renamed,
-- the new parents and the children their rows fall in are made, each child
-- is filled, job_fields records each job's fields, the counts are compared
-- and the old tables are dropped only when they agree; any failure rolls the
-- whole file back. The four views are remade over the new parents as
-- schema.sql states them. The new tables reach the pipeline's roles through
-- the schema's default privileges; create_field_child's EXECUTE is granted
-- here.

BEGIN;

DROP VIEW memberships_official;
DROP VIEW objects_official;
DROP VIEW packets_official;
DROP VIEW forced_official;

ALTER TABLE objects RENAME TO objects_unsplit;
ALTER TABLE memberships RENAME TO memberships_unsplit;
ALTER TABLE packets RENAME TO packets_unsplit;
ALTER TABLE forced RENAME TO forced_unsplit;

-- The old tables' indexes and constraints (their unique, foreign-key and
-- PostgreSQL 18's named not-null constraints), renamed out of the way of the
-- names the new parents take, so they are named as on a fresh database.
DO $$
DECLARE
    i record;
BEGIN
    FOR i IN SELECT c.relname AS index
             FROM pg_index x
             JOIN pg_class c ON c.oid = x.indexrelid
             JOIN pg_class t ON t.oid = x.indrelid
             WHERE t.relname IN ('objects_unsplit', 'memberships_unsplit', 'packets_unsplit',
                                 'forced_unsplit') LOOP
        EXECUTE format('ALTER INDEX public.%I RENAME TO %I', i.index, 'unsplit_' || i.index);
    END LOOP;
    FOR i IN SELECT t.relname AS tab, k.conname AS con
             FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid
             WHERE t.relname IN ('objects_unsplit', 'memberships_unsplit', 'packets_unsplit',
                                 'forced_unsplit')
               AND k.conname NOT LIKE 'unsplit\_%' LOOP
        EXECUTE format('ALTER TABLE public.%I RENAME CONSTRAINT %I TO %I', i.tab, i.con,
                       'unsplit_' || i.con);
    END LOOP;
END
$$;

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

-- The fields each job's rows of objects, memberships, packets and forced
-- were filed under, written by the publisher with the rows, so a reader
-- finds a job's children without reading a parent.
CREATE TABLE job_fields (
    job bigint NOT NULL REFERENCES jobs (id),
    parent text NOT NULL CHECK (parent IN ('objects', 'memberships', 'packets', 'forced')),
    field integer NOT NULL,
    PRIMARY KEY (job, parent, field)
);

DO $$
BEGIN
    CREATE INDEX objects_radec_idx ON objects (q3c_ang2ipix(ra0, dec0));
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'q3c is not available (%); objects_radec_idx is not created', SQLERRM;
END
$$;

-- The functions, as schema.sql states them.
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
    IF parent <> ALL (ARRAY['objects', 'memberships', 'packets', 'forced']) THEN
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
GRANT EXECUTE ON FUNCTION create_field_child(text, integer) TO rapid_rebuild_pipeline;

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

CREATE FUNCTION job_fields_of(parent text, jobs bigint[])
RETURNS integer[]
LANGUAGE sql STABLE AS $$
    SELECT coalesce(array_agg(DISTINCT f.field ORDER BY f.field), '{}')
    FROM job_fields f WHERE f.parent = job_fields_of.parent AND f.job = ANY(jobs)
$$;

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

-- Each object's field, by (founding job, aid); an object has one.
CREATE TEMPORARY TABLE object_field ON COMMIT DROP AS
    SELECT DISTINCT job, aid, field FROM objects_unsplit;
DO $$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM (SELECT job, aid FROM object_field GROUP BY 1, 2
                                 HAVING count(*) > 1) d;
    IF n > 0 THEN
        RAISE EXCEPTION 'field-children: % objects (job, aid) in more than one field; nothing applied', n;
    END IF;
END
$$;
CREATE UNIQUE INDEX ON object_field (job, aid);

-- The rows with their fields, before they move.
CREATE TEMPORARY TABLE memberships_filed ON COMMIT DROP AS
    SELECT m.job, m.object_job, m.aid, f.field, m.source_job, m.sid, m.dra, m.ddec, m.flux
    FROM memberships_unsplit m LEFT JOIN object_field f ON f.job = m.object_job AND f.aid = m.aid;
CREATE TEMPORARY TABLE packets_filed ON COMMIT DROP AS
    SELECT p.job, p.dataset, p.expid, p.sca, p.fid, p.object_job, p.aid, f.field, p.source_job,
           p.sid, p.trigger_mjd, p.band, p.schema_version, p.filename, p.byte_offset, p.byte_length
    FROM packets_unsplit p LEFT JOIN object_field f ON f.job = p.object_job AND f.aid = p.aid;
CREATE TEMPORARY TABLE forced_filed ON COMMIT DROP AS
    SELECT r.job, r.forced_id, r.aid,
           coalesce((SELECT min(p.field) FROM packets_filed p WHERE p.job = r.job AND p.aid = r.aid),
                    (SELECT min(o.field) FROM object_field o WHERE o.aid = r.aid
                     HAVING count(DISTINCT o.field) = 1)) AS field,
           r.rid, r.pid, r.expid, r.sca, r.fid, r.band, r.mjdobs, r.ra, r.dec, r.x, r.y,
           r.psf_flux, r.psf_fluxerr, r.science_flux, r.science_fluxerr, r.flags, r.time_proc
    FROM forced_unsplit r;

DO $$
DECLARE
    m bigint;
    p bigint;
    f bigint;
    pf bigint;
BEGIN
    SELECT count(*) INTO m FROM memberships_filed WHERE field IS NULL;
    SELECT count(*) INTO p FROM packets_filed WHERE field IS NULL;
    SELECT count(*) INTO f FROM forced_filed WHERE field IS NULL;
    -- A packet set's forced row and its packet row of one aid name one field.
    SELECT count(*) INTO pf FROM (SELECT job, aid FROM packets_filed GROUP BY 1, 2
                                  HAVING count(DISTINCT field) > 1) d;
    IF m + p + f + pf > 0 THEN
        RAISE EXCEPTION 'field-children: % memberships, % packets and % forced rows have no object'
                        ' to name their field, % packet (job, aid) pairs two fields; nothing applied',
                        m, p, f, pf;
    END IF;
END
$$;

CREATE INDEX ON memberships_filed (field);
CREATE INDEX ON packets_filed (field);
CREATE INDEX ON forced_filed (field);

DO $$
DECLARE
    t text;
    f integer;
    child text;
BEGIN
    FOREACH t IN ARRAY ARRAY['objects', 'memberships', 'packets', 'forced'] LOOP
        FOR f IN EXECUTE format('SELECT DISTINCT field FROM %I ORDER BY 1',
                                CASE t WHEN 'objects' THEN 'objects_unsplit' ELSE t || '_filed' END) LOOP
            child := create_field_child(t, f);
            EXECUTE format('INSERT INTO public.%I SELECT * FROM %I WHERE field = %s', child,
                           CASE t WHEN 'objects' THEN 'objects_unsplit' ELSE t || '_filed' END, f);
            EXECUTE format('INSERT INTO job_fields (job, parent, field)'
                           ' SELECT DISTINCT job, %L, %s FROM public.%I', t, f, child);
        END LOOP;
    END LOOP;
END
$$;

DO $$
DECLARE
    t text;
    moved bigint;
    kept bigint;
BEGIN
    FOREACH t IN ARRAY ARRAY['objects', 'memberships', 'packets', 'forced'] LOOP
        EXECUTE format('SELECT count(*) FROM %I', t) INTO moved;
        EXECUTE format('SELECT count(*) FROM %I', t || '_unsplit') INTO kept;
        IF moved <> kept THEN
            RAISE EXCEPTION 'field-children: % % rows copied of %; nothing applied', moved, t, kept;
        END IF;
        RAISE NOTICE 'field-children: % % rows moved into their children', moved, t;
    END LOOP;
END
$$;

DROP TABLE forced_unsplit;
DROP TABLE packets_unsplit;
DROP TABLE memberships_unsplit;
DROP TABLE objects_unsplit;

-- The views, as schema.sql states them.
CREATE VIEW memberships_official AS
SELECT t.*, o.id AS binding, j.run
FROM memberships t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

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

COMMIT;
