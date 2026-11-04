-- xsources: dev's table of the SFFT difference image's SExtractor sources
-- (xsources). schema.sql carries the table, its no-rows check, its view and
-- the child-table functions for a fresh database; this file brings an
-- existing one to it, applied once, in one transaction, as postgres. It adds a
-- table and a view and touches no row, so it runs beside the pipeline, before
-- the first difference job of an image that writes xsources. The function is
-- replaced in place, which keeps its owner and its EXECUTE grant; the new
-- table and view reach the pipeline's roles through the schema's default
-- privileges.

BEGIN;

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

-- The child-table functions (schema.sql states them in full):
-- catalog_child_name, new; create_catalog_child, replaced (it now takes
-- xsources and names through catalog_child_name, keeping its owner and its
-- EXECUTE grant); source_set_child, sources_of and xsources_of, new, which let a
-- query read a source set's rows from its child without its parent.
-- The name of the child of `parent` holding rows at `mjdobs` on `sca`:
-- <parent>_<yyyymmdd>_<sca>, the UT date of mjdobs (MJD days begin at 0h UT).
CREATE OR REPLACE FUNCTION catalog_child_name(parent text, mjdobs double precision, sca integer)
RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT parent || '_' || to_char(date '1858-11-17' + floor(mjdobs)::integer, 'YYYYMMDD') || '_' || sca
$$;

CREATE OR REPLACE FUNCTION create_catalog_child(parent text, mjdobs double precision, sca integer)
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

-- An official xsource: its source set is official.
CREATE VIEW xsources_official AS
SELECT t.*, o.id AS binding, j.run
FROM xsources t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

COMMIT;
