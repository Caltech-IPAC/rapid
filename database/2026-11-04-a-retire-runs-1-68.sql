-- Runs 1 to 68's packet and forced rows that name no object, retired
-- (retire-runs-1-68; Ben, 2026-11-07, rapid_docs decisions.md): those runs
-- predate the object catalog (2026-10-24-object-catalog.sql), so their
-- packet rows name the chain's object sets, now objects_chain, and no objects
-- row, and their forced rows carry an aid objects does not hold;
-- 2026-11-04-field-children.sql cannot file either by field. Their sources
-- rows stay (the scorecards read them), and so do objects_chain,
-- memberships_chain, their jobs and their bindings; their forced rows whose aid
-- objects does hold stay and are filed by it. Applied once, in one
-- transaction, as postgres, with nothing running on Batch, first of the
-- 2026-11-04 files (database/README.md).

BEGIN;

DELETE FROM packets p
USING jobs j
WHERE j.id = p.job AND j.run BETWEEN 1 AND 68
  AND NOT EXISTS (SELECT FROM objects o WHERE o.job = p.object_job AND o.aid = p.aid);

DELETE FROM forced r
USING jobs j
WHERE j.id = r.job AND j.run BETWEEN 1 AND 68
  AND NOT EXISTS (SELECT FROM objects o WHERE o.aid = r.aid);

COMMIT;
