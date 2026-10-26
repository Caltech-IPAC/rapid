-- The jobs' quality numbers (PR 350): one row per job and
-- metric, read from `jobs.manifest -> 'metrics'`, so a query per run and
-- stage answers without reading files. schema.sql carries the view for a
-- fresh database; this file brings an existing one to it, applied once.
CREATE VIEW job_metrics AS
SELECT j.run, j.id AS job, j.stage, r.dataset, r.survey, j.state, j.scope,
       (j.scope ->> 'expid')::integer AS expid,
       (j.scope ->> 'sca')::smallint AS sca,
       (j.scope ->> 'fid')::smallint AS fid,
       (j.scope ->> 'field')::integer AS field,
       j.scope ->> 'file' AS file,
       m.key AS metric,
       (m.value #>> '{}')::double precision AS value
FROM jobs j
JOIN runs r ON r.id = j.run
CROSS JOIN LATERAL jsonb_each(j.manifest -> 'metrics') m
WHERE jsonb_typeof(m.value) = 'number';

COMMENT ON VIEW job_metrics IS
    'One row per job and metric of its manifest''s metrics: the stage that computed it, its units and denominator are in that stage''s module docstring (intake: masked_fraction; reference: rapidpipe.science.reference.measure.quality_metrics; difference: rapidpipe.stages.difference and rapidpipe.science.difference.metrics; packet: rapidpipe.stages.packet). A metric a job could not compute is absent, never zero. The scope columns are null where the stage''s scope lacks the key.';
