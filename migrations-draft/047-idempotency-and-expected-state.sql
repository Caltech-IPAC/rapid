-- 047-idempotency-and-expected-state.sql — the two contract fields the
-- mutation API is missing: a caller-supplied idempotency key, and a
-- caller-supplied expected current state that refuses on mismatch.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the
-- schema and this file is not applied by `apply-db-migrations.sh` until its
-- owner adopts it. See that directory's README. Requires 030 and 031 (the
-- audit table and the two operate-tier functions) — apply in order.
--
-- Conformance rule 16; implementation brief G, work item G2:
--
--     "DRAFT migration(s) amending the mutation-function signatures (or
--      adding wrappers) so every mutating call takes an idempotency key
--      (repeat call with the same key = the recorded prior outcome, not a
--      second mutation) and, where the action targets specific state, an
--      expected-state input that refuses on mismatch. Audit rows record
--      both."
--
--     "Preserve 030's invariants exactly: append-only trigger untouched,
--      write_mutation_audit stays ungranted, one-path rule intact, role
--      grants unchanged in spirit."
--
-- ============================================================================
-- WHY OVERLOADS AND NOT AMENDED SIGNATURES
-- ============================================================================
--
-- The brief permits either. This file adds NEW OVERLOADS of the two 031
-- functions rather than changing their argument lists, for three reasons
-- that are all consequences of 031 having already shipped:
--
--   (a) 031's grants name their functions by full argument-type signature
--       (`GRANT EXECUTE ON FUNCTION derived.retry_parked_attempts(text, text,
--       integer, boolean, text, text) TO rapid_orchestrator`). Changing an
--       argument list does not amend a function in PostgreSQL — it creates a
--       different function and orphans the old one, silently leaving the
--       previous signature in place holding its previous grants. An
--       "amendment" would therefore have to DROP the old signature, which
--       revokes the orchestrator's enumerated grant as a side effect and
--       makes a grant-map change out of what was meant to be an API change.
--
--   (b) `rapid_orchestrator` is the one automated caller and it calls
--       `retry_parked_attempts` by its current signature. A migration that
--       breaks a live caller mid-stream is a migration that has to be
--       coordinated with a deploy; an additive overload is not.
--
--   (c) The old signature remains a legitimate call for the case the new
--       fields do not apply to — a one-shot human action with no retry
--       intent has no key to supply, and forcing one would mean inventing a
--       key at the call site purely to satisfy a parameter, which is how an
--       idempotency key becomes a random UUID per call and therefore no
--       idempotency at all.
--
-- PostgreSQL resolves the overloads unambiguously: the new ones take
-- p_idempotency_key text as their FIRST argument, so no call that omits it
-- can accidentally bind to them, and no defaulted-argument ambiguity arises
-- (a defaulted key added to the existing signature WOULD be ambiguous
-- against the same call spelled positionally).
--
-- ============================================================================
-- WHAT AN IDEMPOTENCY KEY MEANS HERE
-- ============================================================================
--
-- "Repeat call with the same key = the recorded prior outcome, not a second
-- mutation." The audit history is already the record of what happened, is
-- already append-only, and is already written in the mutating transaction —
-- so it is also the natural place to look up "did this key already run?".
-- No second bookkeeping table: a key's outcome IS its audit row, and the
-- lookup is an index probe against the column added below.
--
-- THE UNIQUENESS IS PARTIAL, AND ONLY OVER REAL RUNS. A dry run with a key
-- must not consume that key — the whole point of dry-run-then-apply is that
-- the operator issues the same call twice, once to see the plan and once to
-- mean it, and a key burned by the rehearsal would make every apply a
-- replay of its own preview. So the unique index excludes dry runs, and the
-- replay lookup below likewise considers only non-dry-run rows.
--
-- WHY A UNIQUE INDEX AND NOT ONLY THE LOOKUP. The lookup is
-- read-then-write, which two concurrent callers can interleave: both read
-- "no prior row", both mutate. The partial unique index is what makes that
-- race a constraint violation rather than a double mutation, and the
-- functions below take an advisory lock on the key so the ordinary case
-- serializes cleanly instead of one caller taking an error.
--
-- ============================================================================
-- WHAT AN EXPECTED-STATE INPUT MEANS HERE
-- ============================================================================
--
-- Compare-and-swap at the API boundary: the caller states the state it
-- believes it is acting on, and the function refuses if the database
-- disagrees. This is the optimistic-concurrency control 031's audit-history
-- NOT EXISTS approximates but does not provide — that check is dedup ("has
-- this already been released?"), which is a different question from "is the
-- world still as the operator saw it when they decided?".
--
-- The refusal is a RAISE with a pinned SQLSTATE, not a NULL return or a
-- jsonb field, so a caller cannot miss it by not reading the result. The
-- SQLSTATE is what `pipeline/intent/errors.py` classifies on, and the two
-- codes below are in the user-defined range (class 'RA', the repo's own
-- prefix — PostgreSQL reserves nothing there and the class is unused by the
-- stream today):
--
--   RA001 — expected-state mismatch. The world moved; the operator decides
--           again with fresh eyes. Nothing is written: the mismatch is not
--           a mutation, and auditing a refusal as if it were one would put
--           rows in the history for actions that never happened.
--   RA002 — idempotency-key conflict. The same key was used for a
--           DIFFERENT action or scope. This is a caller bug (a key reused
--           across two distinct intentions), not a replay, and it must not
--           silently return the other action's outcome.
--
-- A replay is NOT an error: same key, same action, same scope returns the
-- prior outcome with `replayed: true` and writes nothing.

BEGIN;

-- ---------------------------------------------------------------------------
-- The two columns.
-- ---------------------------------------------------------------------------
-- Added to the existing audit table rather than to a side table: the audit
-- row is the record of the action, and a contract field kept somewhere else
-- would be a second place to look when reconstructing what an operator did.
--
-- Both are nullable, and deliberately so. 030's rows and every call through
-- 031's original signatures carry neither, and a NOT NULL column with a
-- backfilled placeholder would assert those calls had an idempotency key
-- when they did not. NULL here means "this call made no idempotency claim",
-- which is the truth about them.
ALTER TABLE derived.mutation_audit
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS expected_state  jsonb;

COMMENT ON COLUMN derived.mutation_audit.idempotency_key IS
  'Caller-supplied key making a mutating call replayable: a repeat call '
  'with the same key returns this row''s recorded outcome instead of '
  'mutating again. NULL for calls that made no idempotency claim.';

COMMENT ON COLUMN derived.mutation_audit.expected_state IS
  'Caller-supplied expected current state, checked before acting and '
  'recorded as evidence of what the operator believed. NULL where the '
  'action targets no specific state.';

-- The key's outcome must be findable by key alone, and only one real run may
-- ever hold a given key. Partial on both counts: dry runs do not consume a
-- key (see the header), and NULL keys — every pre-existing row, and every
-- call through the original signatures — are not constrained against each
-- other at all.
CREATE UNIQUE INDEX IF NOT EXISTS mutation_audit_idempotency_key_uq
    ON derived.mutation_audit (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND NOT dry_run;

COMMENT ON INDEX derived.mutation_audit_idempotency_key_uq IS
  'One real run per idempotency key. Partial: dry runs do not consume a '
  'key, so the rehearsal-then-apply pair an operator is meant to issue is '
  'not itself a double-use.';

-- ---------------------------------------------------------------------------
-- The replay lookup, shared by every keyed function.
-- ---------------------------------------------------------------------------
-- One function answers "has this key already run, and if so with what
-- outcome?", so the semantics cannot drift between the keyed actions. It is
-- read-only, holds no grant of its own beyond what the SECURITY DEFINER
-- callers give it, and returns the prior row's own recorded result.
--
-- The action/scope agreement check lives here rather than in each caller
-- because reusing a key across two different intentions is the one case
-- where returning the prior outcome would be actively wrong: the caller
-- would receive a success for an action it did not perform.
CREATE OR REPLACE FUNCTION derived.mutation_replay(
    p_idempotency_key text,
    p_action_class    text,
    p_target_scope    text
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    v_prior derived.mutation_audit%ROWTYPE;
BEGIN
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_prior
      FROM derived.mutation_audit
     WHERE idempotency_key = p_idempotency_key
       AND NOT dry_run
     LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- Same key, different intention: a caller bug, and the one case that
    -- must not return the prior outcome as if it were this call's.
    IF v_prior.action_class IS DISTINCT FROM p_action_class
       OR v_prior.target_scope IS DISTINCT FROM p_target_scope THEN
        RAISE EXCEPTION
          'idempotency key % was used for action %/% and cannot be reused '
          'for %/%', p_idempotency_key, v_prior.action_class,
          v_prior.target_scope, p_action_class, p_target_scope
          USING ERRCODE = 'RA002';
    END IF;

    RETURN jsonb_build_object(
        'action', v_prior.action_class,
        'dry_run', false,
        'replayed', true,
        'rows_affected', v_prior.rows_affected,
        'audit_id', v_prior.audit_id,
        'performed_at', v_prior.performed_at,
        'detail', v_prior.detail);
END;
$$;

COMMENT ON FUNCTION derived.mutation_replay IS
  'Returns the recorded outcome of a prior real run under this idempotency '
  'key, or NULL if the key is unused. Raises RA002 when the key was used '
  'for a different action or scope — a reused key is a caller bug, never a '
  'replay.';

-- ---------------------------------------------------------------------------
-- Keyed overload 1: extend the problems vocabulary.
-- ---------------------------------------------------------------------------
-- Expected state for this action is the presence or absence of the category
-- being added: `{"already_present": false}` says "I am adding something I
-- believe is new". If someone else added it first, the operator's premise is
-- gone and the call refuses rather than reporting a no-op success.
CREATE OR REPLACE FUNCTION derived.add_problem_category(
    p_idempotency_key text,
    p_category        text,
    p_description     text,
    p_reason          text,
    p_expected_state  jsonb DEFAULT NULL,
    p_dry_run         boolean DEFAULT true,
    p_policy_citation text DEFAULT NULL,
    p_dispatcher      text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    v_exists   boolean;
    v_audit_id bigint;
    v_affected integer := 0;
    v_scope    text;
    v_replay   jsonb;
    v_expect   boolean;
BEGIN
    IF p_category IS NULL OR length(btrim(p_category)) = 0 THEN
        RAISE EXCEPTION 'category is required';
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a reason is mandatory on every mutation (7a ruling 5)';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'an idempotency key is mandatory on a keyed call';
    END IF;

    v_scope := format('problem_categories:%s', p_category);

    -- Serialize same-key callers before the read, so the ordinary concurrent
    -- case waits and replays rather than colliding on the unique index.
    -- Transaction-scoped: released at commit or rollback, never outliving
    -- the work it guards on a pooled connection.
    IF NOT p_dry_run THEN
        PERFORM pg_advisory_xact_lock(hashtext('rapid.mutation_key'),
                                      hashtext(p_idempotency_key));
        v_replay := derived.mutation_replay(p_idempotency_key,
                                            'problem_vocabulary_extend', v_scope);
        IF v_replay IS NOT NULL THEN
            RETURN v_replay;
        END IF;
    END IF;

    SELECT EXISTS (SELECT 1 FROM derived.problem_categories
                   WHERE problem_category = p_category) INTO v_exists;

    -- Expected state, checked BEFORE any write and refusing on mismatch.
    -- Nothing is audited on refusal: a refused call did not mutate, and a
    -- history that records it as an action would report operator actions
    -- that never took place.
    IF p_expected_state IS NOT NULL
       AND p_expected_state ? 'already_present' THEN
        v_expect := (p_expected_state ->> 'already_present')::boolean;
        IF v_expect IS DISTINCT FROM v_exists THEN
            RAISE EXCEPTION
              'expected-state mismatch: caller expected already_present=%, '
              'actual %', v_expect, v_exists
              USING ERRCODE = 'RA001';
        END IF;
    END IF;

    IF NOT v_exists AND NOT p_dry_run THEN
        INSERT INTO derived.problem_categories
            (problem_category, description, seeded)
        VALUES (p_category, p_description, false);
        v_affected := 1;
    END IF;

    INSERT INTO derived.mutation_audit
        (actor, dispatcher, action_class, action_tier, target_scope,
         reason, dry_run, rows_affected, policy_citation, detail,
         idempotency_key, expected_state)
    VALUES (session_user, coalesce(p_dispatcher, session_user),
            'problem_vocabulary_extend', 'operate', v_scope,
            p_reason, p_dry_run, v_affected, p_policy_citation,
            jsonb_build_object('category', p_category,
                               'already_present', v_exists,
                               'description', p_description),
            p_idempotency_key, p_expected_state)
    RETURNING audit_id INTO v_audit_id;

    IF v_affected = 1 THEN
        UPDATE derived.problem_categories
           SET added_by_audit_id = v_audit_id
         WHERE problem_category = p_category;
    END IF;

    RETURN jsonb_build_object(
        'action', 'problem_vocabulary_extend',
        'dry_run', p_dry_run,
        'replayed', false,
        'already_present', v_exists,
        'rows_affected', v_affected,
        'audit_id', v_audit_id,
        'idempotency_key', p_idempotency_key,
        'would_add', (NOT v_exists));
END;
$$;

COMMENT ON FUNCTION derived.add_problem_category(text, text, text, text, jsonb, boolean, text, text) IS
  'Operate tier, keyed: extend the problems-taxonomy vocabulary under the '
  'full contract — mandatory idempotency key, optional expected state '
  'refusing on mismatch (RA001), dry-run default. A repeat real call with '
  'the same key returns the prior outcome and mutates nothing.';

-- WHY THE AUDIT ROW IS WRITTEN INLINE AND NOT VIA write_mutation_audit.
-- The shared writer's signature carries no idempotency key or expected
-- state, and widening it would change a function 031 deliberately grants to
-- NOBODY — the one-path guarantee rests on that function being unreachable
-- and unchanged. The INSERT here is the same INSERT it performs, in the same
-- transaction, from a SECURITY DEFINER body owned by the same role, with two
-- more columns. The one-path rule is intact: these functions remain the only
-- write path to the table, because no role holds INSERT on it.

-- ---------------------------------------------------------------------------
-- Keyed overload 2: scoped retry of parked attempts.
-- ---------------------------------------------------------------------------
-- Expected state for this action is the candidate count the operator saw in
-- their dry run: `{"candidates": 7}` says "I am releasing the seven attempts
-- I just looked at". If the population changed between the rehearsal and the
-- apply — a new failure parked, a concurrent operator released some — the
-- premise of the decision is gone and the call refuses.
--
-- This is exactly the case the brief calls "where the action targets
-- specific state": the previous signature would happily release a different
-- set of attempts than the one the operator reviewed, and report success.
CREATE OR REPLACE FUNCTION derived.retry_parked_attempts(
    p_idempotency_key text,
    p_run_id          text,
    p_reason          text,
    p_expected_state  jsonb DEFAULT NULL,
    p_max_attempts    integer DEFAULT 50,
    p_dry_run         boolean DEFAULT true,
    p_policy_citation text DEFAULT NULL,
    p_dispatcher      text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    v_candidates integer;
    v_affected   integer := 0;
    v_audit_id   bigint;
    v_warn       text := NULL;
    v_scale_soft constant integer := 500;
    v_ids        bigint[];
    v_scope      text;
    v_replay     jsonb;
    v_expect     integer;
BEGIN
    IF p_run_id IS NULL OR length(btrim(p_run_id)) = 0 THEN
        RAISE EXCEPTION 'a run_id scope is required — there is no unscoped retry';
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a reason is mandatory on every mutation (7a ruling 5)';
    END IF;
    IF p_max_attempts IS NULL OR p_max_attempts < 1 THEN
        RAISE EXCEPTION 'p_max_attempts must be >= 1';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'an idempotency key is mandatory on a keyed call';
    END IF;

    v_scope := format('attempts:run_id=%s:limit=%s', p_run_id, p_max_attempts);

    IF NOT p_dry_run THEN
        PERFORM pg_advisory_xact_lock(hashtext('rapid.mutation_key'),
                                      hashtext(p_idempotency_key));
        v_replay := derived.mutation_replay(p_idempotency_key,
                                            'scoped_retry', v_scope);
        IF v_replay IS NOT NULL THEN
            RETURN v_replay;
        END IF;
    END IF;

    -- The candidate population, computed exactly as the unkeyed function
    -- computes it — same predicate, same ordering, same bound. The two
    -- overloads must agree on what "parked" means, so this stays a copy of
    -- that query rather than a variation on it.
    SELECT array_agg(attempt_id ORDER BY attempt_id)
      FROM (
        SELECT a.attempt_id
        FROM public.attempts a
        WHERE a.run_id = p_run_id
          AND a.rapid_outcome = 'failure'
          AND a.lifecycle_state IN ('terminal_after_start',
                                    'terminal_without_start')
          AND a.error_category IS NOT NULL
          AND NOT EXISTS (
                SELECT 1 FROM derived.mutation_audit ma
                WHERE ma.action_class = 'scoped_retry'
                  AND NOT ma.dry_run
                  AND ma.detail -> 'attempt_ids' @> to_jsonb(a.attempt_id)
              )
        ORDER BY a.attempt_id
        LIMIT p_max_attempts
      ) s
      INTO v_ids;

    v_candidates := coalesce(array_length(v_ids, 1), 0);

    IF p_expected_state IS NOT NULL AND p_expected_state ? 'candidates' THEN
        v_expect := (p_expected_state ->> 'candidates')::integer;
        IF v_expect IS DISTINCT FROM v_candidates THEN
            RAISE EXCEPTION
              'expected-state mismatch: caller expected % candidate '
              'attempts, found %', v_expect, v_candidates
              USING ERRCODE = 'RA001';
        END IF;
    END IF;

    IF v_candidates >= v_scale_soft THEN
        v_warn := format(
          'scale advisory: %s attempts in scope (soft threshold %s) — '
          'proceeding, not refusing', v_candidates, v_scale_soft);
    END IF;

    IF NOT p_dry_run AND v_candidates > 0 THEN
        v_affected := v_candidates;
    END IF;

    INSERT INTO derived.mutation_audit
        (actor, dispatcher, action_class, action_tier, target_scope,
         reason, dry_run, rows_affected, policy_citation, detail,
         idempotency_key, expected_state)
    VALUES (session_user, coalesce(p_dispatcher, session_user),
            'scoped_retry', 'operate', v_scope,
            p_reason, p_dry_run, v_affected, p_policy_citation,
            jsonb_build_object('run_id', p_run_id,
                               'candidates', v_candidates,
                               'attempt_ids', to_jsonb(coalesce(v_ids, '{}'::bigint[])),
                               'scale_advisory', v_warn),
            p_idempotency_key, p_expected_state)
    RETURNING audit_id INTO v_audit_id;

    RETURN jsonb_build_object(
        'action', 'scoped_retry',
        'dry_run', p_dry_run,
        'replayed', false,
        'run_id', p_run_id,
        'candidates', v_candidates,
        'rows_affected', v_affected,
        'scale_advisory', v_warn,
        'idempotency_key', p_idempotency_key,
        'audit_id', v_audit_id);
END;
$$;

COMMENT ON FUNCTION derived.retry_parked_attempts(text, text, text, jsonb, integer, boolean, text, text) IS
  'Operate tier, keyed: release parked attempts within a mandatory run_id '
  'scope under the full contract — mandatory idempotency key, optional '
  'expected candidate count refusing on mismatch (RA001), dry-run default. '
  'A repeat real call with the same key returns the prior outcome.';

-- ---------------------------------------------------------------------------
-- Operator-action recording for actions whose target is not this database.
-- ---------------------------------------------------------------------------
-- Brief G, work item G3: "the ledger records operator actions, not only
-- database mutations". Terminating a Batch job is an operator action with
-- real consequences and no row in this database to point at — the effect is
-- in AWS. The tool that performs it takes a DB connection for exactly one
-- purpose: to leave the same audited trace every other operator action
-- leaves.
--
-- WHY A SEPARATE FUNCTION AND NOT write_mutation_audit MADE GRANTABLE.
-- write_mutation_audit is granted to nobody so that a caller cannot forge an
-- audit row without performing a real mutation; granting it would trade away
-- exactly that guarantee. This function is narrower and that is what makes it
-- safe to grant: it accepts only the 'external' tier's action classes, so the
-- worst a caller can do with it is claim an external action it did not take —
-- which is a claim about AWS, not a forged claim about this database's own
-- audited mutations. Rows it writes are distinguishable by action_class, so
-- an auditor can always separate "this database changed" from "an operator
-- reports having changed something elsewhere".
--
-- The tier is 'operate': 030's CHECK admits read/operate/decide/break_glass,
-- and an external operator action is an operate-tier action performed by a
-- human with a reason. The externality is carried in the action_class prefix
-- and in detail, not by inventing a tier the CHECK would reject.
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
    -- The enumerated external classes. An open text column would make this
    -- function a general-purpose audit-row writer, which is the thing 031
    -- deliberately refuses to grant anyone.
    IF p_action_class NOT IN ('external_batch_terminate',
                              'external_evidence_supersede') THEN
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

COMMENT ON FUNCTION derived.record_external_action IS
  'Records an operator action whose target is outside this database (an '
  'AWS Batch termination, an S3 closure record) in the same audited history '
  'as every database mutation. Enumerated action classes only — a general '
  'audit writer is exactly what 031 refuses to grant.';

-- ---------------------------------------------------------------------------
-- Grants: unchanged in spirit.
-- ---------------------------------------------------------------------------
-- Every new function is PUBLIC-revoked first (PostgreSQL grants EXECUTE to
-- PUBLIC on creation) and then granted to the same roles that hold the
-- unkeyed originals — rapid_operator for the human operate tier, plus
-- rapid_orchestrator for scoped retry alone, which is the one class its
-- versioned policy document authorizes. No role gains an action class it did
-- not already hold; the keyed overloads are the same two actions under a
-- fuller contract.
REVOKE ALL ON FUNCTION derived.mutation_replay(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION derived.add_problem_category(text, text, text, text, jsonb, boolean, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION derived.retry_parked_attempts(text, text, text, jsonb, integer, boolean, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION derived.record_external_action(text, text, text, text, jsonb, boolean, integer, jsonb, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION derived.add_problem_category(text, text, text, text, jsonb, boolean, text, text)
  TO rapid_operator;
GRANT EXECUTE ON FUNCTION derived.retry_parked_attempts(text, text, text, jsonb, integer, boolean, text, text)
  TO rapid_operator;
GRANT EXECUTE ON FUNCTION derived.record_external_action(text, text, text, text, jsonb, boolean, integer, jsonb, text, text)
  TO rapid_operator;

-- mutation_replay is granted to the operate tier too: it is read-only over
-- rows rapid_operator can already SELECT, and a caller that holds the keyed
-- functions needs to be able to ask what a key already did without issuing
-- the mutation again.
GRANT EXECUTE ON FUNCTION derived.mutation_replay(text, text, text) TO rapid_operator;

-- The enumerated service caller keeps exactly its one class, now also in the
-- keyed spelling. Vocabulary extension and external-action recording remain
-- deliberately NOT granted to it.
GRANT EXECUTE ON FUNCTION derived.retry_parked_attempts(text, text, text, jsonb, integer, boolean, text, text)
  TO rapid_orchestrator;

-- write_mutation_audit is untouched and still granted to nobody. 030's
-- append-only trigger is untouched. No role gains INSERT/UPDATE/DELETE on
-- derived.mutation_audit — the two columns added above are written only by
-- the SECURITY DEFINER bodies in this file.

-- no-grant: creates no table (columns and functions only); table grants live
-- in 030, and the added columns inherit them.

-- schema_migrations is recorded by apply-db-migrations.sh, not by the
-- migration itself.

COMMIT;
