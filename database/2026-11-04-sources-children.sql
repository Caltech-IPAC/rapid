-- sources split into child tables by UT observation date and SCA, as dev's
-- are (sources-children). schema.sql carries the parent, its no-rows check and
-- create_catalog_child for a fresh database; this file brings an existing one
-- to it, applied once, in one transaction, as postgres, with nothing running
-- on Batch (the bring-up check of docs/foundation-smdc-step.md, part 1): it
-- rewrites every sources row and holds sources locked until it commits.
--
-- The rows move: the old table is renamed, the new parent and the children
-- its rows fall in are made, each child is filled from the old table, the
-- counts are compared and the old table is dropped only when they agree; any
-- failure rolls the whole file back and leaves sources as it was.
-- sources_official is remade over the new parent with the columns, in the
-- order, of the definition 2026-10-27-sources-official-view.sql left on SMDC.
-- The new tables reach the pipeline's roles through the schema's default
-- privileges; the function's EXECUTE is granted here.

BEGIN;

DROP VIEW sources_official;

ALTER TABLE sources RENAME TO sources_unsplit;
ALTER INDEX IF EXISTS sources_job_sid_key RENAME TO sources_unsplit_job_sid_key;
-- Each child reads its rows through this, not by a scan of the whole table.
CREATE INDEX sources_unsplit_child_idx ON sources_unsplit ((floor(mjdobs)), sca);

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

-- The child of `parent` that holds rows at `mjdobs` on `sca`, made if it does
-- not exist (schema.sql states it in full).
CREATE FUNCTION create_catalog_child(parent text, mjdobs double precision, sca integer)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    day double precision := floor(mjdobs);
    child text := parent || '_' || to_char(date '1858-11-17' + day::integer, 'YYYYMMDD') || '_' || sca;
BEGIN
    IF parent <> ALL (ARRAY['sources']) THEN
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
GRANT EXECUTE ON FUNCTION create_catalog_child(text, double precision, integer) TO rapid_rebuild_pipeline;

DO $$
DECLARE
    d record;
    child text;
BEGIN
    FOR d IN SELECT DISTINCT floor(mjdobs) AS day, sca FROM sources_unsplit ORDER BY 1, 2 LOOP
        child := create_catalog_child('sources', d.day, d.sca);
        EXECUTE format(
            'INSERT INTO public.%I (job, sid, dataset, expid, sca, fid, pid, id, isdiffpos, field,'
            ' hp6, hp9, mjdobs, ra, dec, xfit, yfit, xerr, yerr, fluxfit, fluxerr, flags, qfit,'
            ' cfit, redchi, npixfit, sharpness, roundness1, roundness2, peak, sumrat, nneg, nbad,'
            ' rb, rblabel)'
            ' SELECT job, sid, dataset, expid, sca, fid, pid, id, isdiffpos, field,'
            ' hp6, hp9, mjdobs, ra, dec, xfit, yfit, xerr, yerr, fluxfit, fluxerr, flags, qfit,'
            ' cfit, redchi, npixfit, sharpness, roundness1, roundness2, peak, sumrat, nneg, nbad,'
            ' rb, rblabel'
            ' FROM sources_unsplit WHERE floor(mjdobs) = %s AND sca = %s', child, d.day, d.sca);
    END LOOP;
END
$$;

DO $$
DECLARE
    moved bigint;
    kept bigint;
BEGIN
    SELECT count(*) INTO moved FROM sources;
    SELECT count(*) INTO kept FROM sources_unsplit;
    IF moved <> kept THEN
        RAISE EXCEPTION 'sources-children: % rows copied of %; nothing applied', moved, kept;
    END IF;
    RAISE NOTICE 'sources-children: % rows moved into their children', moved;
END
$$;

DROP TABLE sources_unsplit;

CREATE VIEW sources_official AS
SELECT t.job, t.sid, t.dataset, t.expid, t.sca, t.fid, t.pid, t.id,
       t.isdiffpos, t.field, t.hp6, t.hp9, t.mjdobs, t.ra, t."dec",
       t.xfit, t.yfit, t.xerr, t.yerr, t.fluxfit, t.fluxerr, t.flags,
       t.qfit, t.cfit, t.redchi, t.npixfit, t.sharpness, t.roundness1,
       t.roundness2, t.peak, o.id AS binding, j.run,
       t.sumrat, t.nneg, t.nbad, t.rb, t.rblabel
FROM sources t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;

ANALYZE sources;

COMMIT;
