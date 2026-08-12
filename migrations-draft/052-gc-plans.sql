-- 052-gc-plans.sql — the recorded, checksummed, two-pass deletion plan rule 21
-- requires, and the per-item intent/outcome protocol that makes its execution
-- crash-recoverable.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the schema
-- and this file is not applied by `apply-db-migrations.sh` until its owner
-- adopts it. See that directory's README. Requires DRAFT 047 (the mutation
-- contract's idempotency/expected-state pair and `derived.record_external_
-- action`) — apply in order.
--
-- Conformance rule 21, verbatim:
--
--     "Object deletion happens only through the two-pass inventory anti-join
--      with a safety horizon exceeding the retry, quarantine and PITR
--      windows, against a recorded plan."
--
-- The GOVERNING DESIGN is `rapid-clean-sheet-destination.md` §4.11, whose
-- seven steps this schema records in order: (1) obtain an S3 inventory; (2)
-- anti-join it against registered artifacts, active manifests, quarantined
-- results and live attempts; (3) apply an age horizon longer than retry,
-- quarantine and PITR windows; (4) record a CHECKSUMMED deletion plan; (5)
-- wait and recompute; (6) delete EXACT OBJECT VERSION IDENTIFIERS in bounded
-- batches; (7) retain the audit result.
--
-- ============================================================================
-- THE SAFETY ARGUMENT, WHICH IS WHY THIS SCHEMA LOOKS PARANOID
-- ============================================================================
--
-- The minimal viable target's §2.9 states the asymmetry this whole design
-- turns on:
--
--     "A database reference to a missing object is a critical integrity
--      failure; an old unregistered object is recoverable garbage."
--
-- Deleting a live object is unrecoverable; failing to delete a dead one costs
-- storage. So EVERY ambiguity resolves toward NOT deleting, and this schema
-- makes retention the default at the row level: an item is deletable only by
-- carrying an explicit `pending` status AND surviving a recomputation AND
-- being on the deletable-class allowlist. Anything unclassifiable is retained
-- and REPORTED — its own counted category — never dropped silently.
--
-- ============================================================================
-- WHY A PLAN IS A LIST AND NOT A COUNT
-- ============================================================================
--
-- §4.11 step 4 says "record a checksummed deletion plan". A row count is not
-- a plan: it cannot be reviewed, it cannot be recomputed against, and it
-- cannot say which object a later failure concerned. So `gc_plans` holds one
-- row per computed candidate SET and `gc_plan_items` one row per candidate
-- OBJECT, each carrying its `VersionId`.
--
-- PLANS ARE IMMUTABLE ONCE COMPUTED, resolved precisely: pass-one candidate
-- rows and the checksum over them are NEVER deleted and NEVER rewritten.
-- Recomputation and execution record their verdicts by setting a STATUS on
-- the existing item, so a dropped candidate remains visible as an
-- `excluded-on-recompute` row rather than vanishing. A plan whose items were
-- deleted to reflect a recomputation would be a plan that lies about what it
-- computed, and the checksum would have to be recomputed to match — at which
-- point it stops being evidence of anything.
--
-- ============================================================================
-- THE ITEM STATUS VOCABULARY, STATED ONCE AND AUTHORITATIVE FOR BOTH PASSES
-- ============================================================================
--
--   pending                — computed as a candidate in pass one, not yet acted on
--   in-flight              — intent to delete is COMMITTED, the S3 call may have run
--   excluded-on-recompute  — reappeared or became referenced by pass two
--   deleted                — the exact recorded version was deleted
--   already-absent         — the object was gone before this run reached it
--   skipped-fenced         — the fence refused, or the current version moved
--   failed                 — the S3 call failed for this object; the run continued
--
-- `in-flight` IS THE CRASH-SAFETY MECHANISM and is why an intent/outcome
-- protocol is needed at all rather than a single `record_external_action`
-- call. That function commits immediately (`operatorctl/contract.py:107`), so
-- one call cannot carry per-object intent, a truthful post-delete outcome AND
-- crash-safe recovery. The precedent's own defect is instructive and is NOT
-- copied: `operatorctl/batch.py:80` records BEFORE the AWS action and its
-- prose claims a later update the code never performs. Here the intent row is
-- committed before the S3 call and the outcome is written after, so a crash
-- between them leaves an `in-flight` item that recovery resolves by
-- RE-CHECKING S3 — never by guessing, and never by assuming the delete
-- happened or did not.
--
-- ============================================================================
-- WHY THE FENCE IS A DATABASE ROW AND NOT AN S3 TAG
-- ============================================================================
--
-- A GC hold expressed as an object tag would be silently dropped: S3's
-- `PutObjectTagging` replaces an object's ENTIRE tag set with no merge, and
-- `pipeline/reconciler/retention.py:219` rewrites the canonical full set on
-- every classification. A hold tag would survive exactly until the next
-- reclassification. The fence therefore lives in `gc_fences`, where both
-- participants can see it transactionally.
--
-- THE FENCE COVERS TERMINAL-RECORD ADVANCEMENT, NOT ONLY REGISTRATION. The
-- watermark comparison (`registered_record_sequence >= terminal_record_
-- sequence`) is a SNAPSHOT: a terminal-record writer can raise the terminal
-- sequence immediately after GC reads it, making registration lag again and
-- need the very object GC then deletes. Exact-version deletion does not help
-- there, because the new registration wants that same version. So a fence is
-- taken over the KEY, and it fails closed: if it cannot be acquired, or the
-- counterpart's participation cannot be verified, the ITEM is skipped and
-- reported while the run continues with the rest.
--
-- ============================================================================
-- WHAT THIS PACKAGE HONESTLY DELIVERS
-- ============================================================================
--
-- With the deletable-class allowlist opt-in and effectively EMPTY (no class
-- has a durable reference surface that makes its absence meaningful today —
-- `artifacts` is not populated on the live registration path), this GC will
-- compute plans, record them, and DELETE LITTLE OR NOTHING. That is the
-- correct and conforming outcome for this package: rule 21 requires that
-- deletion happen ONLY through this mechanism, not that the mechanism reclaim
-- anything in particular. A GC that deletes nothing is a passing outcome; a
-- GC that deletes a live object is a critical integrity failure.
--
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- The plan.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gc_plans (
    plan_id             bigint GENERATED BY DEFAULT AS IDENTITY,
    state               text        NOT NULL DEFAULT 'COMPUTED',

    -- SCOPE IS FIXED BEFORE THE REFERENCE SET AND RECORDED ON EVERY PLAN. An
    -- object outside the declared scope is never a candidate, and a plan that
    -- named a bucket outside the package's declared scope would be a plan
    -- nobody argued for. In this package the declared scope is the products
    -- bucket alone.
    declared_buckets    text[]      NOT NULL,
    declared_prefixes   text[]      NOT NULL,

    -- THE HORIZON AND ITS PROVENANCE. Not a number this code derived: an
    -- external input carrying a comment naming where it came from, with NO
    -- DEFAULT THAT PERMITS DELETION. A plan computed with no horizon deletes
    -- nothing and says why.
    horizon_seconds     bigint,
    horizon_provenance  text,

    -- THE INVENTORY IDENTITY AND ITS TIMESTAMP (§4.11 step 1). A pinned,
    -- timestamped snapshot — never a live ListObjects interleaved with the
    -- anti-join, because an object created during such a listing is neither
    -- reliably present nor reliably absent.
    inventory_id        text        NOT NULL,
    inventory_taken_at  timestamptz NOT NULL,
    inventory_object_count bigint   NOT NULL,
    inventory_complete  boolean     NOT NULL,

    -- The second pinned inventory (§4.11 step 5). NULL until recomputation.
    recompute_inventory_id       text,
    recompute_inventory_taken_at timestamptz,
    recomputed_at                timestamptz,

    -- THE CHECKSUM OVER THE CANDIDATE LIST (§4.11 step 4). Computed once, at
    -- COMPUTED, and NEVER recomputed — recomputation records its verdicts as
    -- item statuses, leaving the list and this value untouched.
    candidate_checksum  text        NOT NULL,
    candidate_count     integer     NOT NULL,
    max_deletions       integer     NOT NULL,

    -- The counted categories. Retention is reported, never silent.
    retained_counts     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    allowlist           text[]      NOT NULL DEFAULT ARRAY[]::text[],

    computed_by         text        NOT NULL,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    reason              text        NOT NULL,
    idempotency_key     text        NOT NULL,
    approved_by         text,
    approved_at         timestamptz,
    executed_at         timestamptz,
    abandoned_reason    text,
    audit_id            bigint,

    CONSTRAINT gc_plans_pkey PRIMARY KEY (plan_id),
    CONSTRAINT gc_plans_idempotency_uq UNIQUE (idempotency_key),
    CONSTRAINT gc_plans_state_ck CHECK (state IN
        ('COMPUTED', 'RECOMPUTED', 'APPROVED', 'EXECUTING', 'COMPLETE',
         'ABANDONED')),
    CONSTRAINT gc_plans_reason_ck CHECK (length(btrim(reason)) > 0),
    CONSTRAINT gc_plans_scope_ck CHECK (cardinality(declared_buckets) > 0),
    -- A BOUND IS MANDATORY AND A PLAN EXCEEDING IT IS REFUSED AT COMPUTATION,
    -- never truncated at execution: silent truncation reads as "covered
    -- everything" when it did not.
    CONSTRAINT gc_plans_bound_ck CHECK (max_deletions > 0),
    CONSTRAINT gc_plans_within_bound_ck
        CHECK (candidate_count <= max_deletions),
    -- A HORIZON WITHOUT PROVENANCE IS A GUESS. Either both or neither.
    CONSTRAINT gc_plans_horizon_ck
        CHECK ((horizon_seconds IS NULL AND horizon_provenance IS NULL)
            OR (horizon_seconds > 0 AND horizon_provenance IS NOT NULL)),
    -- APPROVAL IS A DISTINCT RECORDED ACT with its own actor.
    CONSTRAINT gc_plans_approval_ck
        CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CONSTRAINT gc_plans_checksum_ck CHECK (candidate_checksum LIKE 'sha256:%')
);

CREATE INDEX IF NOT EXISTS gc_plans_state_idx
    ON gc_plans USING btree (state);
CREATE INDEX IF NOT EXISTS gc_plans_computed_at_idx
    ON gc_plans USING btree (computed_at DESC);

COMMENT ON TABLE gc_plans IS
    'One row per computed candidate set (rule 21''s "recorded plan"). '
    'Immutable once computed: the candidate list and its checksum are never '
    'rewritten, and recomputation records its verdicts as item statuses so a '
    'dropped candidate stays visible as an excluded row rather than '
    'vanishing.';
COMMENT ON COLUMN gc_plans.horizon_seconds IS
    'The safety horizon, an EXTERNAL INPUT that fails closed. NULL means no '
    'horizon is configured, and a plan with no horizon deletes nothing and '
    'says why — there is deliberately no default that permits deletion. Must '
    'exceed the pgBackRest PITR retention and every real retry/recovery hold; '
    'the effective value is the MAXIMUM of the configured values, never a '
    'sum.';
COMMENT ON COLUMN gc_plans.horizon_provenance IS
    'Where the horizon came from. A horizon without a stated provenance is a '
    'guess wearing a number, so the CHECK requires both or neither.';
COMMENT ON COLUMN gc_plans.candidate_checksum IS
    'sha256 over the canonical serialization of the pass-one candidate list. '
    'Computed once and NEVER recomputed: a checksum recomputed to match a '
    'changed list is evidence of nothing.';
COMMENT ON COLUMN gc_plans.inventory_complete IS
    'Whether pagination completeness was PROVEN for the inventory. A '
    'truncated listing must be detectable and fatal, never silently short — '
    'a short listing makes objects look absent, and absence is what this '
    'process acts on.';
COMMENT ON COLUMN gc_plans.retained_counts IS
    'Counts by retention category — unattributable, not-allowlisted, '
    'live-attempt, outstanding-registration, referenced, out-of-scope. '
    'Retention is REPORTED, never silent: "absence of a reference is not '
    'evidence of garbage when nothing enumerates that class of object at '
    'all".';
COMMENT ON COLUMN gc_plans.allowlist IS
    'The deletable-class allowlist this plan was computed under. Opt-in and '
    'effectively empty today: a class joins it only when a ratified proposal '
    'names it together with the durable reference surface that makes its '
    'absence meaningful. An empty allowlist means the plan deletes nothing, '
    'which is a conforming outcome.';

-- ---------------------------------------------------------------------------
-- The candidate items — one row per object, carrying its exact VersionId.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gc_plan_items (
    item_id          bigint GENERATED BY DEFAULT AS IDENTITY,
    plan_id          bigint      NOT NULL,
    bucket           text        NOT NULL,
    object_key       text        NOT NULL,

    -- THE EXACT VERSION, AND WHY KEY-ONLY DELETION IS FORBIDDEN. The products
    -- bucket has versioning Enabled (`rapid_systems`
    -- `cloudformation/rapid-product-buckets.yaml:29-30`), so a key-only
    -- delete installs a DELETE MARKER over whatever is current — including a
    -- version written after the plan was computed. Deleting the exact version
    -- the plan recorded means a newly-registered object sharing the key is
    -- untouched.
    version_id       text        NOT NULL,
    object_size      bigint,
    object_modified  timestamptz,

    object_class     text        NOT NULL,
    attributed_attempt_id bigint,
    attributed_prefix     text,

    status           text        NOT NULL DEFAULT 'pending',
    status_reason    text,
    -- The version actually acted on, recorded alongside the outcome. A delete
    -- that found a different current version records what it saw.
    acted_version_id text,
    intent_at        timestamptz,
    outcome_at       timestamptz,

    CONSTRAINT gc_plan_items_pkey PRIMARY KEY (item_id),
    CONSTRAINT gc_plan_items_plan_fk
        FOREIGN KEY (plan_id) REFERENCES gc_plans(plan_id),
    -- ONE ROW PER OBJECT VERSION PER PLAN. Without this a recomputation could
    -- append a second row for the same object and the plan would carry two
    -- verdicts for one thing.
    CONSTRAINT gc_plan_items_object_uq
        UNIQUE (plan_id, bucket, object_key, version_id),
    CONSTRAINT gc_plan_items_status_ck CHECK (status IN
        ('pending', 'in-flight', 'excluded-on-recompute', 'deleted',
         'already-absent', 'skipped-fenced', 'failed')),
    CONSTRAINT gc_plan_items_version_ck CHECK (length(btrim(version_id)) > 0)
);

CREATE INDEX IF NOT EXISTS gc_plan_items_plan_status_idx
    ON gc_plan_items USING btree (plan_id, status);
CREATE INDEX IF NOT EXISTS gc_plan_items_key_idx
    ON gc_plan_items USING btree (bucket, object_key);

COMMENT ON TABLE gc_plan_items IS
    'One row per candidate object, with the exact VersionId the plan '
    'recorded. Rows are never deleted: recomputation and execution set a '
    'status, so an excluded candidate stays visible as evidence of what pass '
    'one computed.';
COMMENT ON COLUMN gc_plan_items.version_id IS
    'The exact object version. Deletion is BY VERSION, never by key alone — '
    'on a versioning-enabled bucket a key-only delete installs a delete '
    'marker over whatever is current, including a version written after '
    'planning.';
COMMENT ON COLUMN gc_plan_items.status IS
    'pending | in-flight | excluded-on-recompute | deleted | already-absent | '
    '| skipped-fenced | failed. One vocabulary, used by both passes. '
    '"in-flight" means intent was committed before the S3 call: a crash there '
    'leaves a recorded in-flight item that recovery resolves by re-checking '
    'S3 rather than by guessing.';
COMMENT ON COLUMN gc_plan_items.attributed_prefix IS
    'The canonical prefix reconstructed from the attempt''s own job type, run '
    'id, work-unit key and attempt id, which must EXACTLY EQUAL the '
    'inventory key''s prefix. Parsing attempt-N out of a key and finding '
    'attempt N is not sufficient — malformed, legacy-layout, mismatched-run, '
    'mismatched-unit and foreign-prefix keys are unattributable and retained.';

-- ---------------------------------------------------------------------------
-- The fence, observed by BOTH registration and GC.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gc_fences (
    fence_id     bigint GENERATED BY DEFAULT AS IDENTITY,
    bucket       text        NOT NULL,
    object_key   text        NOT NULL,
    holder       text        NOT NULL,
    holder_kind  text        NOT NULL,
    acquired_at  timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    CONSTRAINT gc_fences_pkey PRIMARY KEY (fence_id),
    -- ONE HOLDER PER KEY. The exclusion that makes this a fence rather than a
    -- note.
    CONSTRAINT gc_fences_key_uq UNIQUE (bucket, object_key),
    CONSTRAINT gc_fences_kind_ck CHECK (holder_kind IN ('gc', 'registration')),
    CONSTRAINT gc_fences_lease_ck CHECK (expires_at > acquired_at)
);

CREATE INDEX IF NOT EXISTS gc_fences_expiry_idx
    ON gc_fences USING btree (expires_at);

COMMENT ON TABLE gc_fences IS
    'The exclusion protocol both registration and GC observe. A registration '
    'in flight against a candidate key blocks its deletion, and GC holding a '
    'key blocks a registration from binding it. FAILS CLOSED: an item whose '
    'fence cannot be acquired is skipped and reported, and the run continues '
    'with the remaining items. Expressed as a database row rather than an S3 '
    'tag because PutObjectTagging replaces the whole tag set with no merge, '
    'so a hold tag would be dropped by the next retention reclassification '
    '(pipeline/reconciler/retention.py:219).';
COMMENT ON COLUMN gc_fences.expires_at IS
    'A lease, so a crashed holder cannot block a key forever. Expiry is '
    'checked by the acquiring statement rather than by a sweeper: a sweeper '
    'that had not run yet would make an expired fence look live.';

-- ---------------------------------------------------------------------------
-- IMMUTABILITY IS A TRIGGER, NOT GRANTS ALONE (030's and 050's pattern). The
-- table owner and any SECURITY DEFINER function bypass column grants, and a
-- property this load-bearing must not depend on getting a grant map right.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.gc_plan_items_are_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'gc_plan_items rows are never deleted (plan %, item %): a '
            'recomputation records its verdict as a STATUS so an excluded '
            'candidate stays visible as evidence of what pass one computed. '
            'A plan whose items were deleted would be a plan that lies about '
            'what it computed.', OLD.plan_id, OLD.item_id
            USING ERRCODE = 'RA011';
    END IF;

    -- THE IDENTITY OF A CANDIDATE IS FROZEN. Only the outcome columns move.
    IF NEW.bucket IS DISTINCT FROM OLD.bucket
       OR NEW.object_key IS DISTINCT FROM OLD.object_key
       OR NEW.version_id IS DISTINCT FROM OLD.version_id
       OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
       OR NEW.object_class IS DISTINCT FROM OLD.object_class THEN
        RAISE EXCEPTION
            'a computed candidate cannot be rewritten (plan %, item %): the '
            'candidate list is what the plan checksum was computed over, so '
            'changing an item would silently invalidate the plan''s own '
            'evidence', OLD.plan_id, OLD.item_id
            USING ERRCODE = 'RA011';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION derived.gc_plan_items_are_append_only() IS
    'Freezes a candidate''s identity and forbids deletion of plan items. The '
    'trigger backstop rule 21''s "recorded plan" needs — grants alone cannot '
    'provide it, because the owner and SECURITY DEFINER functions bypass '
    'them.';

DROP TRIGGER IF EXISTS gc_plan_items_immutable ON gc_plan_items;
CREATE TRIGGER gc_plan_items_immutable
    BEFORE UPDATE OR DELETE ON gc_plan_items
    FOR EACH ROW EXECUTE FUNCTION derived.gc_plan_items_are_append_only();

-- The plan's own frozen fields, and its legal state transitions.
CREATE OR REPLACE FUNCTION derived.gc_plans_transition_is_legal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    legal_ boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'gc_plans rows are never deleted (plan %); the audit result is '
            'retained (§4.11 step 7)', OLD.plan_id
            USING ERRCODE = 'RA011';
    END IF;

    -- THE CHECKSUM AND THE CANDIDATE LIST'S SHAPE ARE FROZEN. Recomputation
    -- does NOT recompute the checksum; it records item statuses.
    IF NEW.candidate_checksum IS DISTINCT FROM OLD.candidate_checksum
       OR NEW.candidate_count IS DISTINCT FROM OLD.candidate_count
       OR NEW.inventory_id IS DISTINCT FROM OLD.inventory_id
       OR NEW.computed_at IS DISTINCT FROM OLD.computed_at
       OR NEW.declared_buckets IS DISTINCT FROM OLD.declared_buckets THEN
        RAISE EXCEPTION
            'plan % is immutable once computed: its candidate checksum, '
            'count, inventory identity, computation time and declared scope '
            'cannot change. Recomputation records verdicts as item statuses.',
            OLD.plan_id
            USING ERRCODE = 'RA011';
    END IF;

    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;

    legal_ := (OLD.state, NEW.state) IN (
        ('COMPUTED',  'RECOMPUTED'), ('COMPUTED',  'ABANDONED'),
        ('RECOMPUTED','APPROVED'),   ('RECOMPUTED','ABANDONED'),
        ('APPROVED',  'EXECUTING'),  ('APPROVED',  'ABANDONED'),
        ('EXECUTING', 'COMPLETE'),   ('EXECUTING', 'ABANDONED'));

    IF NOT legal_ THEN
        RAISE EXCEPTION
            'illegal GC plan transition % -> % for plan %. The state machine '
            'is COMPUTED -> RECOMPUTED -> APPROVED -> EXECUTING -> COMPLETE, '
            'with ABANDONED reachable from any non-terminal state. In '
            'particular a plan cannot reach EXECUTING without a recorded '
            'recomputation and a recorded approval — that is the two-pass '
            'requirement.', OLD.state, NEW.state, OLD.plan_id
            USING ERRCODE = 'RA011';
    END IF;

    -- APPROVAL MUST BE RECORDED BEFORE EXECUTION, with its own actor.
    IF NEW.state = 'EXECUTING' AND NEW.approved_by IS NULL THEN
        RAISE EXCEPTION
            'plan % cannot execute without a recorded approval actor; '
            'approval is a distinct recorded act', OLD.plan_id
            USING ERRCODE = 'RA011';
    END IF;
    IF NEW.state = 'APPROVED' AND NEW.recomputed_at IS NULL THEN
        RAISE EXCEPTION
            'plan % cannot be approved before it has been recomputed against '
            'a second pinned inventory (§4.11 step 5 is mandatory, not '
            'optional)', OLD.plan_id
            USING ERRCODE = 'RA011';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION derived.gc_plans_transition_is_legal() IS
    'The plan state machine and the plan''s frozen fields. A plan cannot '
    'reach EXECUTING without a recorded recomputation and a recorded '
    'approval — the two-pass requirement expressed where it cannot be '
    'forgotten.';

DROP TRIGGER IF EXISTS gc_plans_transitions ON gc_plans;
CREATE TRIGGER gc_plans_transitions
    BEFORE UPDATE OR DELETE ON gc_plans
    FOR EACH ROW EXECUTE FUNCTION derived.gc_plans_transition_is_legal();

-- ---------------------------------------------------------------------------
-- EXECUTED PLANS ARE IMMUTABLE BY ANYONE. Separate from the transition
-- trigger because it guards a different property: not "is this transition
-- legal" but "may this row change at all any more".
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.gc_completed_items_are_frozen()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IN ('deleted', 'already-absent')
       AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION
            'item % is in terminal status % and cannot be re-opened; a '
            'resumed execution acts only on UNRESOLVED items',
            OLD.item_id, OLD.status
            USING ERRCODE = 'RA011';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION derived.gc_completed_items_are_frozen() IS
    'A deleted or already-absent item is terminal. Resuming a partially '
    'executed plan acts only on unresolved items, and this is what makes '
    'that structural rather than careful.';

DROP TRIGGER IF EXISTS gc_plan_items_terminal_frozen ON gc_plan_items;
CREATE TRIGGER gc_plan_items_terminal_frozen
    BEFORE UPDATE ON gc_plan_items
    FOR EACH ROW EXECUTE FUNCTION derived.gc_completed_items_are_frozen();

-- ---------------------------------------------------------------------------
-- `gc_plan_execute` JOINS DRAFT 047'S ENUMERATED EXTERNAL ACTION CLASSES.
--
-- 047's `record_external_action` refuses any class outside its literal list
-- (`047:549-554`), and it refuses it deliberately: "an open text column would
-- make this function a general-purpose audit-row writer, which is the thing
-- 031 deliberately refuses to grant anyone". A GC execution IS an external
-- operator action in exactly that sense — its target is S3, outside this
-- database — so it belongs in the enumeration rather than in a widened
-- column, and adding it here keeps 047's refusal intact for everything else.
--
-- CREATE OR REPLACE with the identical signature: the argument list is a
-- function's identity in PostgreSQL, so replacing rather than overloading is
-- what keeps 047's existing grants attached. The body is 047's, verbatim,
-- with one string added to the IN list.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.record_external_action(
    p_idempotency_key text,
    p_action_class    text,
    p_target_scope    text,
    p_reason          text,
    p_expected_state  jsonb DEFAULT NULL,
    p_dry_run         boolean DEFAULT true,
    p_rows_affected   integer DEFAULT 0,
    p_detail          jsonb DEFAULT NULL,
    p_policy_citation text DEFAULT NULL,
    p_dispatcher      text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    v_audit_id bigint;
    v_replay   jsonb;
    v_affected integer;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a reason is mandatory on every mutation (7a ruling 5)';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'an idempotency key is mandatory on a keyed call';
    END IF;
    IF p_target_scope IS NULL OR length(btrim(p_target_scope)) = 0 THEN
        RAISE EXCEPTION 'a target scope is required — there is no unscoped action';
    END IF;
    IF p_action_class NOT IN ('external_batch_terminate',
                              'external_evidence_supersede',
                              'gc_plan_execute') THEN
        RAISE EXCEPTION
          'action_class % is not an external operator action', p_action_class;
    END IF;

    IF NOT p_dry_run THEN
        PERFORM pg_advisory_xact_lock(hashtext('rapid.mutation_key'),
                                      hashtext(p_idempotency_key));
        v_replay := derived.mutation_replay(p_idempotency_key,
                                            p_action_class, p_target_scope);
        IF v_replay IS NOT NULL THEN
            RETURN v_replay;
        END IF;
    END IF;

    -- 030's CHECK forbids a dry run claiming rows changed, so the count is
    -- forced to zero on the rehearsal path rather than trusted from a caller
    -- that may have counted what it *would* have done.
    v_affected := CASE WHEN p_dry_run THEN 0 ELSE coalesce(p_rows_affected, 0) END;

    INSERT INTO derived.mutation_audit
        (actor, dispatcher, action_class, action_tier, target_scope,
         reason, dry_run, rows_affected, policy_citation, detail,
         idempotency_key, expected_state)
    VALUES (session_user, coalesce(p_dispatcher, session_user),
            p_action_class, 'operate', p_target_scope,
            p_reason, p_dry_run, v_affected, p_policy_citation, p_detail,
            p_idempotency_key, p_expected_state)
    RETURNING audit_id INTO v_audit_id;

    RETURN jsonb_build_object(
        'action', p_action_class,
        'dry_run', p_dry_run,
        'replayed', false,
        'rows_affected', v_affected,
        'idempotency_key', p_idempotency_key,
        'audit_id', v_audit_id);
END;
$$;

COMMENT ON FUNCTION derived.record_external_action(text, text, text, text,
    jsonb, boolean, integer, jsonb, text, text) IS
    'Records an operator action whose target is outside this database (an '
    'AWS Batch termination, an S3 closure record, a GC plan execution) in '
    'the same audited history as every database mutation. Enumerated action '
    'classes only — a general audit writer is exactly what 031 refuses to '
    'grant. DRAFT 052 adds gc_plan_execute to the enumeration.';

-- ---------------------------------------------------------------------------
-- Grants: DRAFT 050's posture, not 048's blanket one. Guarded on role
-- existence so this file still applies to a bare scratch database.
--
-- THE PIPELINE WRITE ROLE GETS NO DELETE ANYWHERE HERE, and cannot write plan
-- rows at all. GC is an operator action under the mutation contract, not
-- something a pipeline job does. The pipeline DOES participate in the fence —
-- that is the whole point of a protocol observed by both sides — so it may
-- insert and remove its OWN fence rows.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_pipeline_write')
    THEN
        GRANT SELECT ON gc_plans, gc_plan_items TO rapid_pipeline_write;
        -- STATED EXPLICITLY so a later blanket grant written by habit cannot
        -- silently widen this. A pipeline job that could write a plan row
        -- could authorize its own deletions.
        REVOKE INSERT, UPDATE, DELETE ON gc_plans FROM rapid_pipeline_write;
        REVOKE INSERT, UPDATE, DELETE ON gc_plan_items
            FROM rapid_pipeline_write;

        -- The registration side of the fence.
        GRANT SELECT, INSERT, DELETE ON gc_fences TO rapid_pipeline_write;
        GRANT USAGE, SELECT ON SEQUENCE gc_fences_fence_id_seq
            TO rapid_pipeline_write;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT SELECT ON gc_plans, gc_plan_items, gc_fences TO rapid_read;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_orchestrator')
    THEN
        GRANT SELECT, INSERT, UPDATE ON gc_plans, gc_plan_items
            TO rapid_orchestrator;
        GRANT SELECT, INSERT, DELETE ON gc_fences TO rapid_orchestrator;
        GRANT USAGE, SELECT ON SEQUENCE gc_plans_plan_id_seq,
            gc_plan_items_item_id_seq, gc_fences_fence_id_seq
            TO rapid_orchestrator;
        -- NO DELETE ON PLANS OR ITEMS FOR ANYONE, including the operator
        -- role: §4.11 step 7 retains the audit result, and the triggers above
        -- are the backstop that makes this true even for the owner.
        REVOKE DELETE ON gc_plans, gc_plan_items FROM rapid_orchestrator;
    END IF;
END;
$$;

COMMIT;
