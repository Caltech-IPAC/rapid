-- Additive: a nullable note on why reconcile could not resolve an
-- attempt's outcome, without setting a terminal disposition.
--
-- rapid_docs/system/runs.md, "Attempts": disposition is null while
-- queued or running. A launcher-side failure to fetch the attempt's
-- manifest or execution record from S3 (e.g. an AccessDenied on the
-- launcher's own role) is neither of those, and is not evidence the
-- job itself failed -- the Batch job can have SUCCEEDED and written its
-- products. Recording it as a terminal 'failed' disposition would
-- consume one of the unit's limited attempts for a launcher-side
-- problem the job never had a chance to cause. reconcile now leaves
-- such an attempt's disposition NULL (unresolved, so a later reconcile
-- retries it once the launcher-side problem is fixed) and records why
-- here, so the reason is not silently lost.
ALTER TABLE attempts
    ADD COLUMN reconcile_note text;

COMMENT ON COLUMN attempts.reconcile_note IS
    'Set by rapidpipe.launch.batch.reconcile when it could not determine '
    'an attempt''s outcome (e.g. a launcher-side S3 fetch error on an '
    'otherwise-SUCCEEDED Batch job) and left disposition NULL for a later '
    'reconcile to retry, rather than recording a terminal disposition. '
    'Holds the exception class, its message, and the S3 key involved. '
    'Cleared (set back to NULL) if a later reconcile records a real '
    'disposition for the attempt.';
