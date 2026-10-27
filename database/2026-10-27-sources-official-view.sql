-- sources_official shows every sources column (sources-official-view).
-- schema.sql's view selects t.*, which Postgres expands when the view is
-- created, so the columns later added to sources by ALTER TABLE (sumrat,
-- nneg, nbad by 2026-10-24-pixel-columns.sql; rb, rblabel by
-- 2026-10-27-real-bogus.sql) never reached SMDC's view. A replace may only
-- append columns, so this keeps the live definition's columns, binding and
-- run in place and adds the five after them; owner and grants stay. A
-- fresh schema.sql database already has them, before binding and run,
-- because its * resolves at its own creation; consumers select by name.
-- Applied once.
CREATE OR REPLACE VIEW sources_official AS
SELECT t.job, t.sid, t.dataset, t.expid, t.sca, t.fid, t.pid, t.id,
       t.isdiffpos, t.field, t.hp6, t.hp9, t.mjdobs, t.ra, t."dec",
       t.xfit, t.yfit, t.xerr, t.yerr, t.fluxfit, t.fluxerr, t.flags,
       t.qfit, t.cfit, t.redchi, t.npixfit, t.sharpness, t.roundness1,
       t.roundness2, t.peak, o.id AS binding, j.run,
       t.sumrat, t.nneg, t.nbad, t.rb, t.rblabel
FROM sources t
JOIN official o ON o.closed_at IS NULL AND o.product = 'sources' AND o.job = t.job
JOIN jobs j ON j.id = t.job;
