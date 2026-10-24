-- A job's Batch submissions (night-2026-10-24 task 8, Spot as a probe): one
-- entry per Batch job keyed by its id, written by the submitter, the job and
-- the loop's reconcile (rapidpipe.foundation.attempts), and job_metrics gains
-- the per-job numbers read from them and from the row's own times. schema.sql
-- carries both for a fresh database; this file brings an existing one to
-- them, applied once.
ALTER TABLE jobs ADD COLUMN attempts jsonb DEFAULT '{}'::jsonb NOT NULL;

COMMENT ON COLUMN jobs.attempts IS
    'One entry per Batch submission, keyed by Batch job id: queue, submitted, why (the submitter); started, instance_type, availability_zone, batch_attempt, ended, outcome (the job); stopped, status, status_reason, exit_code, hedged (the loop''s reconcile). ISO 8601 UTC times.';

CREATE OR REPLACE VIEW job_metrics AS
SELECT j.run, j.id AS job, j.stage, r.dataset, r.survey, j.state, j.scope,
       (j.scope ->> 'expid')::integer AS expid,
       (j.scope ->> 'sca')::smallint AS sca,
       (j.scope ->> 'fid')::smallint AS fid,
       (j.scope ->> 'field')::integer AS field,
       j.scope ->> 'file' AS file,
       m.metric,
       m.value
FROM jobs j
JOIN runs r ON r.id = j.run
CROSS JOIN LATERAL (
    SELECT e.key AS metric, (e.value #>> '{}')::double precision AS value
    FROM jsonb_each(j.manifest -> 'metrics') e
    WHERE jsonb_typeof(e.value) = 'number'
    UNION ALL
    SELECT 'container_seconds', extract(epoch FROM j.ended - j.started)::double precision
    WHERE j.state = 'published' AND j.started IS NOT NULL AND j.ended IS NOT NULL
    UNION ALL
    SELECT s.metric, s.value
    FROM (SELECT count(*) AS n,
                 count(*) FILTER (WHERE a.value ->> 'status_reason' LIKE 'Host EC2%') AS reclaims,
                 count(*) FILTER (WHERE a.value ? 'hedged') AS hedges,
                 coalesce(sum(extract(epoch FROM
                     coalesce(a.value ->> 'ended', a.value ->> 'stopped', a.value ->> 'hedged')::timestamptz
                     - (a.value ->> 'started')::timestamptz))
                     FILTER (WHERE a.value ? 'started'
                             AND coalesce(a.value ->> 'outcome', '') <> 'published'
                             AND coalesce(a.value ->> 'ended', a.value ->> 'stopped', a.value ->> 'hedged') IS NOT NULL),
                     0) AS lost
          FROM jsonb_each(j.attempts) a) t
    CROSS JOIN LATERAL (VALUES ('batch_submissions', t.n::double precision),
                               ('reclaims', t.reclaims::double precision),
                               ('hedges', t.hedges::double precision),
                               ('lost_seconds', t.lost::double precision)) s (metric, value)
    WHERE t.n > 0
) m;

COMMENT ON VIEW job_metrics IS
    'One row per job and metric: its manifest''s metrics (the stage that computed it, its units and denominator are in that stage''s module docstring: intake: masked_fraction; reference: rapidpipe.science.reference.measure.quality_metrics; difference: rapidpipe.stages.difference and rapidpipe.science.difference.metrics; packet: rapidpipe.stages.packet), container_seconds (a published job''s started to ended, its publishing attempt), and from jobs.attempts batch_submissions, reclaims (status reason Host EC2), hedges and lost_seconds (started to end of every submission not published). A metric a job could not compute is absent, never zero. The scope columns are null where the stage''s scope lacks the key.';
