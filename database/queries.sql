-- queries.sql: the read queries the foundation's official views must answer
-- (foundation plan v5, Part 2): the plan's eight queries, then the team's
-- twelve spatial and status shapes (dev's SQL, re-expressed on the views).
--
-- One statement per block. Each block opens with
--   -- name: <name>          a stable handle (tests/db/test_queries.py runs each)
--   -- returns: <columns>    the result's column names, in order
-- then comment lines saying what it answers and where its shape comes from.
-- Parameters are psycopg2 named placeholders, %(name)s; in psql, substitute
-- them by hand.
--
-- Rules the statements keep:
-- - Scientific keys, never row ids, join two products: an image key is
--   (dataset, expid, sca, fid), a reference's (dataset, field, fid). A
--   difference image's rid and rfid are lineage, not join keys.
-- - A failed execution publishes nothing, so `status` is a filter each
--   query states where dev's did (status > 0), never a view's.
-- - A query "within a named set" reads the base tables through that set's
--   recorded inputs (Part 1 statement 4: inputs named at launch are the
--   authority), not the official views.

--------------------------------------------------------------------------------
-- The plan's eight queries
--------------------------------------------------------------------------------

-- name: current_image_and_source_set
-- returns: rid, l2file, pid, diffimage, source_set, nsources
-- The current L2 file, difference image and source set for a dataset,
-- exposure, SCA and filter (dev: providers.py's resolve_pid and chip sources).
SELECT l.rid, l.filename AS l2file, d.pid, d.filename AS diffimage, o.job AS source_set,
       (SELECT count(*) FROM sources s WHERE s.job = o.job) AS nsources
FROM l2files_official l
LEFT JOIN diffimages_official d
    ON (d.dataset, d.expid, d.sca, d.fid) = (l.dataset, l.expid, l.sca, l.fid)
LEFT JOIN official o
    ON o.closed_at IS NULL AND o.product = 'sources'
    AND o.key = jsonb_build_object('dataset', l.dataset, 'expid', l.expid, 'sca', l.sca, 'fid', l.fid)
WHERE l.dataset = %(dataset)s AND l.expid = %(expid)s AND l.sca = %(sca)s AND l.fid = %(fid)s;

-- name: object_set_and_members
-- returns: object_set, aid, ra0, dec0, nsources, source_job, sid, recorded_by
-- The current object set of a field and its members through its predecessor
-- chain, less excluded source sets (dev: merges_<field> joined to
-- astroobjects_<field>). recorded_by is the set in the chain that matched it.
SELECT o.job AS object_set, o.aid, o.ra0, o.dec0, o.nsources, m.source_job, m.sid,
       m.job AS recorded_by
FROM objects_official o
JOIN members_official m ON m.set_job = o.job AND m.aid = o.aid
WHERE o.dataset = %(dataset)s AND o.survey = %(survey)s AND o.field = %(field)s
ORDER BY o.aid, m.source_job, m.sid;

-- name: object_history_in_named_set
-- returns: kind, mjdobs, flux, fluxerr, job, id
-- An object's detections and forced photometry within a named object set
-- (official or not): detections are the set's chain's memberships less
-- excluded source sets; forced rows are those of packet sets that used a set
-- of that chain, once per forced id. kind is 'detection' (job, id = the
-- source set and sid) or 'forced' (job, id = the packet set and forced_id).
WITH RECURSIVE chain (job) AS (
    SELECT %(object_set)s::bigint
    UNION ALL
    SELECT i.source_job FROM chain c
    JOIN job_inputs i ON i.job = c.job AND i.kind = 'input' AND i.product = 'objects'
    WHERE i.source_job IS NOT NULL
)
SELECT * FROM (
    SELECT 'detection' AS kind, s.mjdobs, s.fluxfit::double precision AS flux,
           s.fluxerr::double precision AS fluxerr, m.source_job AS job, m.sid AS id
    FROM chain c
    JOIN memberships m ON m.job = c.job
    JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
    WHERE m.aid = %(aid)s
      AND NOT EXISTS (SELECT 1 FROM chain x
                      JOIN job_inputs e ON e.job = x.job AND e.kind = 'excludes'
                      WHERE e.source_job = m.source_job)
    UNION ALL
    (SELECT DISTINCT ON (f.forced_id) 'forced', f.mjdobs, f.psf_flux, f.psf_fluxerr, f.job,
            f.forced_id
     FROM forced f
     WHERE f.aid = %(aid)s
       AND EXISTS (SELECT 1 FROM packets p
                   WHERE p.job = f.job AND p.aid = f.aid AND p.object_job IN (SELECT job FROM chain))
     ORDER BY f.forced_id, f.job)
) history
ORDER BY mjdobs, kind, job, id;

-- name: packet_history_by_observation
-- returns: job, sid, aid, object_job, source_job, trigger_mjd, schema_version, binding
-- Every packet ever made for an image key, every version; binding is the
-- open packet binding of its set, or null for a superseded or unbound set.
SELECT p.job, p.sid, p.aid, p.object_job, p.source_job, p.trigger_mjd, p.schema_version,
       o.id AS binding
FROM packets p
LEFT JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = p.job
WHERE p.dataset = %(dataset)s AND p.expid = %(expid)s AND p.sca = %(sca)s AND p.fid = %(fid)s
ORDER BY p.job, p.sid;

-- name: packet_history_by_source
-- returns: job, expid, sca, fid, aid, object_job, trigger_mjd, binding
-- Every packet triggered by one source (source set, sid), every version.
SELECT p.job, p.expid, p.sca, p.fid, p.aid, p.object_job, p.trigger_mjd, o.id AS binding
FROM packets p
LEFT JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = p.job
WHERE p.source_job = %(source_job)s AND p.sid = %(sid)s
ORDER BY p.job;

-- name: packet_history_by_object
-- returns: job, expid, sca, fid, source_job, sid, object_job, trigger_mjd, binding
-- A versioned object's alert history: the packets of object aid made with
-- the named object set or any set of its predecessor chain.
WITH RECURSIVE chain (job) AS (
    SELECT %(object_set)s::bigint
    UNION ALL
    SELECT i.source_job FROM chain c
    JOIN job_inputs i ON i.job = c.job AND i.kind = 'input' AND i.product = 'objects'
    WHERE i.source_job IS NOT NULL
)
SELECT p.job, p.expid, p.sca, p.fid, p.source_job, p.sid, p.object_job, p.trigger_mjd,
       o.id AS binding
FROM packets p
LEFT JOIN official o ON o.closed_at IS NULL AND o.product = 'packets' AND o.job = p.job
WHERE p.aid = %(aid)s AND p.object_job IN (SELECT job FROM chain)
ORDER BY p.trigger_mjd, p.job;

-- name: candidate_against_official
-- returns: source_job, sid, ra, dec, mjdobs, candidate_aid, official_aid
-- A candidate object set (an unbound associate job) against the official
-- set for the same key: every detection of the candidate's input source
-- sets, with the object each assigns it; a null aid is a detection that set
-- left unmatched (one row per pair of assignments when a source joins
-- several objects).
WITH RECURSIVE chain (job) AS (
    SELECT %(candidate)s::bigint
    UNION ALL
    SELECT i.source_job FROM chain c
    JOIN job_inputs i ON i.job = c.job AND i.kind = 'input' AND i.product = 'objects'
    WHERE i.source_job IS NOT NULL
),
candidate_members AS (
    SELECT m.aid, m.source_job, m.sid
    FROM chain c JOIN memberships m ON m.job = c.job
    WHERE NOT EXISTS (SELECT 1 FROM chain x
                      JOIN job_inputs e ON e.job = x.job AND e.kind = 'excludes'
                      WHERE e.source_job = m.source_job)
),
official_set AS (
    SELECT o.job FROM job_inputs e
    JOIN official o ON o.closed_at IS NULL AND o.product = 'objects' AND o.key = e.key
    WHERE e.job = %(candidate)s AND e.kind = 'expects' AND e.product = 'objects'
)
SELECT s.job AS source_job, s.sid, s.ra, s.dec, s.mjdobs, cm.aid AS candidate_aid,
       om.aid AS official_aid
FROM job_inputs i
JOIN sources s ON s.job = i.source_job
LEFT JOIN candidate_members cm ON cm.source_job = s.job AND cm.sid = s.sid
LEFT JOIN members_official om ON om.source_job = s.job AND om.sid = s.sid
    AND om.set_job = (SELECT job FROM official_set)
WHERE i.job = %(candidate)s AND i.kind = 'input' AND i.product = 'sources'
ORDER BY s.job, s.sid, cm.aid, om.aid;

-- name: ancestors_of_packet_set
-- returns: job, stage, run, state, bound
-- Every job a packet set descends from, through recorded inputs, nearest
-- first by job id: its object set and that set's chain, source sets, images,
-- PSF, reference, catalogs. bound: whether the job holds an open binding.
WITH RECURSIVE up (job) AS (
    SELECT i.source_job FROM job_inputs i
    WHERE i.job = %(packet_job)s AND i.kind = 'input' AND i.source_job IS NOT NULL
    UNION
    SELECT i.source_job FROM up u
    JOIN job_inputs i ON i.job = u.job AND i.kind = 'input' AND i.source_job IS NOT NULL
)
SELECT j.id AS job, j.stage, j.run, j.state,
       EXISTS (SELECT 1 FROM official o WHERE o.closed_at IS NULL AND o.job = j.id) AS bound
FROM up u JOIN jobs j ON j.id = u.job
ORDER BY j.id DESC;

-- name: descendants_of_reference
-- returns: job, stage, run, state, bound_products
-- Every job that read a reference image, directly or through what it
-- produced: the work a bad reference taints. bound_products: the products
-- each still holds an open binding for (what a correction must rebind).
WITH RECURSIVE down (job) AS (
    SELECT r.job FROM refimages r WHERE r.rfid = %(rfid)s
    UNION
    SELECT i.job FROM down d
    JOIN job_inputs i ON i.source_job = d.job AND i.kind = 'input'
)
SELECT j.id AS job, j.stage, j.run, j.state,
       array_remove(array_agg(DISTINCT o.product), NULL) AS bound_products
FROM down d
JOIN jobs j ON j.id = d.job
LEFT JOIN official o ON o.closed_at IS NULL AND o.job = j.id
GROUP BY j.id, j.stage, j.run, j.state
ORDER BY j.id;

-- name: failed_and_unbound_jobs
-- returns: job, run, stage, state, exit_code, refusal, was_bound, expects
-- Jobs that failed, and published jobs holding no open binding: refused
-- (refusal says why; was_bound false) or superseded (was_bound true).
-- expects: the bindings the job expected to replace.
SELECT j.id AS job, j.run, j.stage, j.state, j.exit_code, j.refusal,
       EXISTS (SELECT 1 FROM official o WHERE o.job = j.id) AS was_bound,
       (SELECT jsonb_agg(jsonb_build_object('product', e.product, 'key', e.key,
                                            'binding', e.binding))
        FROM job_inputs e WHERE e.job = j.id AND e.kind = 'expects') AS expects
FROM jobs j
WHERE j.state = 'failed'
   OR (j.state = 'published'
       AND NOT EXISTS (SELECT 1 FROM official o WHERE o.closed_at IS NULL AND o.job = j.id))
ORDER BY j.id;

-- name: bindings_around_group
-- returns: side, binding, product, key, job, run, survey, dataset, who, why
-- The bindings a change group closed ('before') and opened ('after').
SELECT 'before' AS side, o.id AS binding, o.product, o.key, o.job, o.run, o.survey, o.dataset,
       o.closed_by AS who, o.closed_why AS why
FROM official o WHERE o.closed_group = %(group)s
UNION ALL
SELECT 'after', o.id, o.product, o.key, o.job, o.run, o.survey, o.dataset, o.opened_by,
       o.opened_why
FROM official o WHERE o.opened_group = %(group)s
ORDER BY side DESC, binding;

--------------------------------------------------------------------------------
-- The team's twelve spatial and status shapes (LEDGER-queries (a), (b))
--------------------------------------------------------------------------------

-- name: diffimages_in_cone
-- returns: pid, expid, sca, fid, filename, status, mjdobs, l2file
-- Difference images whose centre lies within a cone, usable, in one filter
-- and an MJD window (dev: forced_phot.py's image search, R6). q3c index.
SELECT d.pid, d.expid, d.sca, d.fid, d.filename, d.status, l.mjdobs, l.filename AS l2file
FROM diffimages_official d
JOIN l2files_official l ON (l.dataset, l.expid, l.sca, l.fid) = (d.dataset, d.expid, d.sca, d.fid)
WHERE q3c_radial_query(d.ra0, d.dec0, %(ra)s, %(dec)s, %(radius)s)
  AND d.dataset = %(dataset)s AND d.status > 0 AND d.fid = %(fid)s
  AND l.mjdobs BETWEEN %(mjd_min)s AND %(mjd_max)s
ORDER BY l.mjdobs;

-- name: l2files_in_cone_overlapping_field
-- returns: rid, expid, sca, mjdobs, dist, filename
-- L2 files within a cone that overlap a field, usable, in one filter and an
-- MJD window, less one L2 row (dev: rapid_db.py's reference-input search,
-- R14). q3c index and the overlapfields GIN index.
SELECT l.rid, l.expid, l.sca, l.mjdobs, q3c_dist(l.ra0, l.dec0, %(ra)s, %(dec)s) AS dist,
       l.filename
FROM l2files_official l
WHERE q3c_radial_query(l.ra0, l.dec0, %(ra)s, %(dec)s, %(radius)s)
  AND l.overlapfields @> ARRAY[%(field)s]::integer[]
  AND l.dataset = %(dataset)s AND l.status > 0 AND l.fid = %(fid)s
  AND l.mjdobs BETWEEN %(mjd_min)s AND %(mjd_max)s AND l.rid <> %(exclude_rid)s
ORDER BY l.mjdobs;

-- name: diffimages_in_cone_with_reference
-- returns: pid, expid, sca, jd, checksum, rfid, reffile, refchecksum
-- Difference images near a position with the reference of their field and
-- filter, in a JD window (dev: rapid_db.py R16). The reference joins on its
-- scientific key, not the image's rfid. q3c index.
SELECT d.pid, d.expid, d.sca, d.jd, d.checksum, r.rfid, r.filename AS reffile,
       r.checksum AS refchecksum
FROM diffimages_official d
JOIN refimages_official r ON (r.dataset, r.field, r.fid) = (d.dataset, d.field, d.fid)
WHERE q3c_radial_query(d.ra0, d.dec0, %(ra)s, %(dec)s, %(radius)s)
  AND d.dataset = %(dataset)s AND d.jd BETWEEN %(jd_min)s AND %(jd_max)s
ORDER BY d.jd;

-- name: l2files_overlapping_fields
-- returns: rid, dateobs, sca, field, overlapfields
-- Usable L2 files overlapping any of a list of fields (dev:
-- rapid_pipeline_subs.py R19). GIN index on overlapfields.
SELECT l.rid, l.dateobs, l.sca, l.field, l.overlapfields
FROM l2files_official l
WHERE l.overlapfields && %(fields)s::integer[] AND l.dataset = %(dataset)s AND l.status > 0
ORDER BY l.dateobs, l.sca;

-- name: objects_in_cone
-- returns: aid, ra0, dec0, nsources, object_set
-- Current objects within a cone (the association read's spatial shape, R25,
-- now a query rather than the matcher's: the matcher runs in memory).
-- q3c index on object positions.
SELECT o.aid, o.ra0, o.dec0, o.nsources, o.job AS object_set
FROM objects_official o
WHERE q3c_radial_query(o.ra0, o.dec0, %(ra)s, %(dec)s, %(radius)s)
  AND o.dataset = %(dataset)s AND o.survey = %(survey)s
ORDER BY o.aid;

-- name: chip_sources
-- returns: sid, ra, dec, fluxfit, fluxerr, filter, exptime
-- The alertable sources of one chip with band and exposure time (dev:
-- providers.py R2): no rejecting flag, the informational bright-footprint
-- bit 16384 cleared (rapidpipe.products.diffimage.INFORMATIONAL_SOURCE_FLAGS).
SELECT s.sid, s.ra, s.dec, s.fluxfit, s.fluxerr, f.filter, e.exptime
FROM sources_official s
JOIN filters f ON f.fid = s.fid
LEFT JOIN exposures_official e ON (e.dataset, e.expid) = (s.dataset, s.expid)
WHERE s.dataset = %(dataset)s AND s.expid = %(expid)s AND s.sca = %(sca)s AND s.fid = %(fid)s
  AND s.flags & ~16384 = 0
ORDER BY s.sid;

-- name: reference_for_field
-- returns: rfid, filename, infobits, status, nframes, cov5percent
-- The usable reference for a field and filter (dev: rapid_db.py R9).
SELECT r.rfid, r.filename, r.infobits, r.status, r.nframes, r.cov5percent
FROM refimages_official r
WHERE r.dataset = %(dataset)s AND r.field = %(field)s AND r.fid = %(fid)s AND r.status > 0;

-- name: l2files_for_field_in_window
-- returns: rid, expid, sca, mjdobs, dateobs
-- The L2 work list for a field and filter in an MJD window (dev: rapid_db.py
-- R13, the time-window shape).
SELECT l.rid, l.expid, l.sca, l.mjdobs, l.dateobs
FROM l2files_official l
WHERE l.dataset = %(dataset)s AND l.field = %(field)s AND l.fid = %(fid)s
  AND l.mjdobs BETWEEN %(mjd_min)s AND %(mjd_max)s AND l.status > 0
ORDER BY l.mjdobs, l.sca;

-- name: l2files_without_diffimage
-- returns: rid, expid, sca, fid, mjdobs
-- The work list: usable official L2 files with no official difference image,
-- an anti-join of two views on the image key (dev: rapid_db.py R22, which
-- joined on rid).
SELECT l.rid, l.expid, l.sca, l.fid, l.mjdobs
FROM l2files_official l
WHERE l.dataset = %(dataset)s AND l.status > 0
  AND NOT EXISTS (SELECT 1 FROM diffimages_official d
                  WHERE (d.dataset, d.expid, d.sca, d.fid) = (l.dataset, l.expid, l.sca, l.fid))
ORDER BY l.mjdobs, l.sca;

-- name: frame_counts
-- returns: field, fid, nframes
-- Fields and filters with at least a number of official L2 frames in an MJD
-- window (dev: rapid_db.py R17).
SELECT l.field, l.fid, count(*) AS nframes
FROM l2files_official l
WHERE l.dataset = %(dataset)s AND l.mjdobs BETWEEN %(mjd_min)s AND %(mjd_max)s
GROUP BY l.field, l.fid
HAVING count(*) >= %(min_frames)s
ORDER BY l.field, l.fid;

-- name: diffimage_by_pid
-- returns: pid, ra0, dec0, field, mjdobs, filename, binding
-- One difference image by its product id with its L2 row's time, if it is
-- official (dev: providers.py R7, the point-lookup shape).
SELECT d.pid, d.ra0, d.dec0, d.field, l.mjdobs, d.filename, d.binding
FROM diffimages_official d
JOIN l2files_official l ON (l.dataset, l.expid, l.sca, l.fid) = (d.dataset, d.expid, d.sca, d.fid)
WHERE d.pid = %(pid)s;

-- name: l2files_with_usable_reference
-- returns: rid, expid, sca, rfid, nframes, cov5percent
-- Usable L2 files in a field and filter whose reference is usable and deep
-- enough (dev: rapid_db.py R15).
SELECT l.rid, l.expid, l.sca, r.rfid, r.nframes, r.cov5percent
FROM l2files_official l
JOIN refimages_official r ON (r.dataset, r.field, r.fid) = (l.dataset, l.field, l.fid)
WHERE l.dataset = %(dataset)s AND l.field = %(field)s AND l.fid = %(fid)s
  AND l.status > 0 AND r.status > 0
  AND r.nframes >= %(min_nframes)s AND r.cov5percent >= %(min_cov5percent)s
ORDER BY l.mjdobs, l.sca;
