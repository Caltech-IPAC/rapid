-- 044-submission-protocol.sql — the durable submission record: PREPARED ->
-- CALLING -> BOUND / UNKNOWN -> FOUND / LOST.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the
-- schema and this file is not applied by `apply-db-migrations.sh` until its
-- owner adopts it. See that directory's README.
--
-- Conformance rule 7 (minimal-viable target § 4): "No transaction spans
-- `SubmitJob`. An ambiguous submission resolves through the durable
-- submission-row protocol (PREPARED -> CALLING -> BOUND / UNKNOWN -> FOUND /
-- LOST); the API call is never repeated for a submission row."
--
-- Four of that rule's five clauses already conform at 820dd40 and are NOT
-- touched by this migration: no transaction spans SubmitJob (submit.py opens
-- none), the call is never repeated (one call site, no retry loop),
-- ExecutionBinding pins the queue/job-definition/image identities, and
-- manifests carry no credentials. The missing clause is the protocol itself.
--
-- WHAT EXISTS TODAY, AND WHY IT IS NOT THE PROTOCOL. An ambiguous
-- `submit_job` outcome leaves pre-created attempt rows with a NULL
-- scheduler_job_id (`pipeline/seams.py`, whose SubmissionFailed docstring
-- correctly refuses to delete them: "a submit_job that times out on the
-- client side may well have been accepted"). Those rows are resolved only by
-- `pipeline/reconciler/horizons.py`'s SUBMISSION_HORIZON_SECONDS — thirty
-- minutes of elapsed time, `beyond_submission_horizon(submitted_at, now,
-- horizon)`, pure arithmetic. That is a TIMEOUT, not a resolution: it never
-- asks Batch whether the job exists, so a job that WAS accepted and is
-- running is classified identically to one that never reached the API. The
-- rule's demand is a positive re-query by deterministic identity, and the
-- state that re-query needs — "we were mid-call when we lost contact" — has
-- nowhere to live.
--
-- WHY A NEW TABLE RATHER THAN COLUMNS ON `attempts`. Three reasons, in
-- increasing order of weight:
--
--   1. `attempts.lifecycle_state` and `attempts.scheduler_state` are both
--      closed CHECK enumerations (011:93-99) carrying, respectively, RAPID's
--      attempt taxonomy and Batch's own job vocabulary. The submission
--      protocol is a third vocabulary about a third thing — the API CALL —
--      and stuffing it into either would make one column mean two kinds of
--      fact, which is exactly the "parallel untyped fact carrier" rule 11
--      forbids.
--
--   2. THE GRAIN IS WRONG. One `submit_job` call submits one ARRAY JOB
--      covering many units — `pipeline/seams.py:submit_units` pre-creates one
--      attempt row per array child and makes ONE call for all of them. The
--      ambiguity is a property of that single call, not of each child: per-
--      attempt columns would record N copies of one fact and leave "was the
--      call made" answerable N different ways. One submission row per call is
--      the honest grain.
--
--   3. RULE 3 ASKS FOR IT DIRECTLY: "Logical work, authorized attempt,
--      submission and observed Batch execution are four distinct records;
--      each level answers one question." `work_units` (036) is the first,
--      `attempts` (011) the second, `attempts.scheduler_*` the fourth. The
--      third has been the missing level all along; this is it.
--
-- THE STATE MACHINE, and what each state means operationally:
--
--   prepared  the row exists, the manifest is published, the attempt rows are
--             pre-created; SubmitJob has NOT been called. A crash here loses
--             nothing — no job can exist.
--   calling   written and COMMITTED immediately before the API call. This is
--             the state the whole table exists for: a row found in `calling`
--             by a later pass means a call was in flight when this process
--             stopped, so a job MAY exist and only Batch can say.
--   bound     the call returned and its scheduler job id is recorded. The
--             happy path ends here.
--   unknown   the call raised ambiguously (timeout, connection reset, an
--             error after the request may have reached Batch). Distinct from
--             `calling` because it records a DECISION — this outcome was
--             judged ambiguous — rather than an interruption.
--   found     a positive re-query by deterministic identity located the job.
--             The scheduler job id is recorded exactly as `bound` would.
--   lost      a negative re-query, past the horizon, established that no job
--             exists. Only now may the work be resubmitted — as a NEW
--             submission row, never by re-calling for this one.
--
-- THE HORIZON SURVIVES AS A BACKSTOP, NOT AS THE TRUTH (brief C1: "The time
-- horizon may remain as a backstop for scheduler-side silence, but it acts on
-- a record that says CALLING/UNKNOWN — the state machine, not the timestamp,
-- is the truth"). `resolution_deadline` carries that: it bounds how long a
-- re-query may keep answering "not yet visible" before the row is declared
-- `lost`. Batch's own eventual consistency is why a single negative answer is
-- not sufficient — a job submitted milliseconds before a describe call can be
-- absent from it.
--
-- THE CALL IS NEVER REPEATED FOR A ROW, and the schema enforces it rather
-- than trusting the code: `submissions_call_once_ck` requires
-- `call_started_at` to be set exactly once (it is NOT NULL from `calling`
-- onward and NULL in `prepared`), and there is no transition back to
-- `prepared` or `calling` in the application's graph. A resubmission after
-- `lost` mints a new row with a new submission_id, so "how many times did we
-- call Batch for this work" is answerable by counting rows.

BEGIN;

CREATE TABLE IF NOT EXISTS public.submissions (
    submission_id        bigint      GENERATED BY DEFAULT AS IDENTITY,

    -- The batch identity this submission covers — `manifest.batch_id`, the
    -- run-scoped identity `pipeline/seams.py` cuts batches under
    -- (`<run_id>-<n>` where a gathering pass exceeds the array ceiling). One
    -- submission row per batch identity per call.
    run_id               text        NOT NULL,
    job_type             text        NOT NULL,

    -- THE DETERMINISTIC IDENTITY THE RE-QUERY USES. `build_submit_kwargs`
    -- names every job `rapid-{manifest.batch_id}` (submission/submit.py), so
    -- the job name is a pure function of the batch identity — which is what
    -- makes a positive re-query possible at all: Batch's ListJobs filters by
    -- jobName, so "does a job for this submission exist" is answerable
    -- without having received a jobId. Stored rather than re-derived so the
    -- re-query asks for exactly the name the call used, even if the naming
    -- convention later changes.
    job_name             text        NOT NULL,
    job_queue            text        NOT NULL,
    job_definition       text        NOT NULL,

    -- The sealed submission's pinned identities (rule 7's fifth clause),
    -- copied from the ExecutionBinding the attempt rows also carry. Recorded
    -- here so a submission row is self-describing: an operator resolving an
    -- UNKNOWN can see exactly what was submitted without joining out.
    manifest_checksum    text        NOT NULL,
    manifest_uri         text        NOT NULL,
    array_size           integer     NOT NULL,

    state                text        NOT NULL,

    -- The scheduler's answer, from the call (`bound`) or from the re-query
    -- (`found`). NULL in every other state.
    scheduler_job_id     text,

    created_at           timestamptz NOT NULL DEFAULT now(),
    -- Set exactly once, immediately before the API call — see
    -- submissions_call_once_ck.
    call_started_at      timestamptz,
    resolved_at          timestamptz,
    -- The backstop the horizon becomes: after this instant a still-negative
    -- re-query may conclude `lost`. Set when the row enters `unknown`.
    resolution_deadline  timestamptz,
    -- Why this row is `unknown` — the exception class/message the ambiguous
    -- call raised, so an operator can tell a client timeout from a throttle
    -- from a credential failure without reading logs.
    ambiguity_detail     text,

    updated_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT submissions_pkey PRIMARY KEY (submission_id),

    CONSTRAINT submissions_state_ck CHECK (state IN (
        'prepared', 'calling', 'bound', 'unknown', 'found', 'lost'
    )),

    -- THE CALL HAPPENS ONCE. `prepared` is the only state in which no call
    -- has been made; every other state is downstream of exactly one call, so
    -- call_started_at is NULL in `prepared` and NOT NULL thereafter. With no
    -- application edge back into `prepared` or `calling`, this is what makes
    -- "the API call is never repeated for a submission row" a property of the
    -- schema rather than a promise in a docstring.
    CONSTRAINT submissions_call_once_ck CHECK (
        (state = 'prepared' AND call_started_at IS NULL)
        OR (state <> 'prepared' AND call_started_at IS NOT NULL)
    ),

    -- A scheduler job id is exactly the two resolved-positive states' to
    -- carry. `unknown` explicitly has none — that is what makes it unknown —
    -- and `lost` has none by definition.
    CONSTRAINT submissions_job_id_ck CHECK (
        (state IN ('bound', 'found') AND scheduler_job_id IS NOT NULL)
        OR (state NOT IN ('bound', 'found') AND scheduler_job_id IS NULL)
    ),

    -- Terminal states carry their resolution instant; open states do not.
    CONSTRAINT submissions_resolved_ck CHECK (
        (state IN ('bound', 'found', 'lost') AND resolved_at IS NOT NULL)
        OR (state IN ('prepared', 'calling', 'unknown') AND resolved_at IS NULL)
    ),

    -- The backstop applies to `unknown` alone: a row in `calling` has not yet
    -- been judged ambiguous, and a resolved row needs no deadline.
    CONSTRAINT submissions_deadline_ck CHECK (
        state <> 'unknown' OR resolution_deadline IS NOT NULL
    )
);

COMMENT ON TABLE public.submissions IS
  'The durable submission record rule 7 requires: one row per SubmitJob call,
   advancing prepared -> calling -> bound on the happy path and calling ->
   unknown -> found/lost when the call''s outcome is ambiguous. The third
   level of rule 3''s identity chain (work_unit -> attempt -> SUBMISSION ->
   batch_execution). Its grain is the CALL, not the attempt: one array
   submission covers many attempt rows, and "was the call made" is one fact
   about all of them. An ambiguous outcome is resolved by positively
   re-querying Batch for job_name — never by repeating submit_job for this
   row, and never by elapsed time alone; the deadline is a backstop bounding
   how long a negative re-query may keep meaning "not yet visible".';

COMMENT ON COLUMN public.submissions.job_name IS
  'The deterministic job name the call used — `rapid-{batch_id}` per
   submission.submit.build_submit_kwargs. This is the identity the ambiguity
   re-query searches Batch by, which is why it is stored rather than
   re-derived: the re-query must ask for the name that WAS used, not the name
   today''s convention would produce.';

COMMENT ON COLUMN public.submissions.call_started_at IS
  'Set once, committed immediately BEFORE the SubmitJob call. A row found in
   `calling` with this set and no scheduler_job_id is the ambiguous case the
   protocol exists for: a call was in flight, so a job may exist, and only
   Batch can say which.';

COMMENT ON COLUMN public.submissions.resolution_deadline IS
  'The backstop, not the truth. After this instant a still-negative re-query
   may conclude `lost`. It exists because Batch is eventually consistent — a
   job submitted moments before a describe call can be absent from it — so a
   single negative answer is not sufficient evidence of absence.';

-- The re-query's own lookup: find open submissions by identity.
CREATE INDEX IF NOT EXISTS submissions_open_idx
    ON public.submissions (state, resolution_deadline)
    WHERE state IN ('prepared', 'calling', 'unknown');

CREATE INDEX IF NOT EXISTS submissions_run_idx
    ON public.submissions (run_id);

CREATE INDEX IF NOT EXISTS submissions_job_name_idx
    ON public.submissions (job_name);

-- ---------------------------------------------------------------------------
-- attempts.submission_id — the link from level 2 to level 3
-- ---------------------------------------------------------------------------
-- NULLABLE, for the same reason 036 made attempts.work_unit_id nullable:
-- every attempt row predating this migration genuinely has no submission
-- record, and a NOT NULL column would either refuse the backfill or invent
-- one. Absent means absence, not a sentinel.
ALTER TABLE public.attempts
    ADD COLUMN IF NOT EXISTS submission_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'attempts_submission_fk'
    ) THEN
        ALTER TABLE public.attempts
            ADD CONSTRAINT attempts_submission_fk
            FOREIGN KEY (submission_id)
            REFERENCES public.submissions (submission_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS attempts_submission_idx
    ON public.attempts (submission_id)
    WHERE submission_id IS NOT NULL;

COMMENT ON COLUMN public.attempts.submission_id IS
  'The submission call that created this attempt row (044). NULLABLE: every
   attempt predating the submission protocol has none, exactly as
   work_unit_id is nullable for attempts predating the intent layer. Many
   attempts share one submission — an array job is one call.';

-- ---------------------------------------------------------------------------
-- Grants, following 012/036's pattern for the tables this stream adds.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON public.submissions TO rapid_pipeline_write;
GRANT SELECT ON public.submissions TO rapid_read, rapid_orchestrator;

-- Named individually per 012's rule (Ben, 2026-08-04): GRANT ... ON ALL
-- SEQUENCES evaluates at execution time over whatever exists, silently
-- widening this migration's blast radius to sequences it never named.
GRANT USAGE, SELECT ON SEQUENCE public.submissions_submission_id_seq
    TO rapid_pipeline_write;
GRANT SELECT ON SEQUENCE public.submissions_submission_id_seq TO rapid_read;

-- schema_migrations is recorded by apply-db-migrations.sh, not by the
-- migration itself.

COMMIT;
