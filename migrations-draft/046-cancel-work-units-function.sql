-- 046-cancel-work-units-function.sql — derived.cancel_work_units, the audited
-- entry point for the seventh state; and the work-unit lock added to
-- derived.retry_parked_attempts so the SQL side honours the same discipline
-- the Python side now does.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the
-- schema and this file is not applied by `apply-db-migrations.sh` until its
-- owner adopts it. See that directory's README. Requires 045 (the
-- 'cancelled' vocabulary) — apply in order.
--
-- Conformance rule 9; implementation brief C, work item C3:
--
--     "Cancellation becomes an explicit disposition: an operator- or
--      policy-initiated terminal state distinct from failure, taking the same
--      lock, recording who/why (compose with the mutation-audit machinery
--      that migrations 030-031 provide rather than inventing a parallel
--      record)."
--
--     "Migration 040's SQL-side transition function must honor the same
--      discipline — a DRAFT amendment if it cannot."
--
-- Both halves are here because they are one change: a lock discipline that
-- only one of two writers observes is not a discipline. 040's function and
-- this one now take the same advisory lock, in the same namespace, on the
-- same key, before reading the state they decide from.
--
-- ============================================================================
-- THE LOCK
-- ============================================================================
--
-- Namespace 0x5755 ('WU'), keyed on work_unit_id, via pg_advisory_xact_lock's
-- two-argument form — the same lock `pipeline.intent.lock` takes from the
-- application side, so a Python transition and a SQL cancellation of one unit
-- genuinely serialize against each other.
--
-- WRITTEN DECIMAL BELOW, AS 22357. That is 0x5755, and it is spelled decimal
-- in the SQL because a hex literal here would be `x'5755'`, a BIT-STRING
-- literal needing a cast through bit(16) — a needlessly obscure spelling for
-- a constant two languages must agree on exactly. `pipeline.intent.lock`
-- spells it 0x5755 because Python's hex literal is an integer already. The
-- two are the same number; this note is the only place that has to say so. That cross-language agreement is
-- the whole point: before this, `pipeline/intent/writer.py`'s transitions were
-- bare CAS with zero locks and 040's function was bare CAS with zero locks,
-- so two writers could each read a consistent view, each decide, and one's
-- decision land on a unit the other's decision was already based on.
--
-- WHY THE CAS SURVIVES UNDERNEATH IT. The lock serializes DECIDERS; the CAS
-- verifies that the state a decider read is still the state it transitions
-- from. They catch different failures — an interleaving anomaly versus a lost
-- update by a writer that never read — and neither substitutes for the other.
-- Both functions below keep their CAS exactly as it was.
--
-- WHY xact-SCOPED, NEVER SESSION-SCOPED. pg_advisory_xact_lock releases at
-- commit or rollback and even if the backend dies. Session advisory locks are
-- forbidden on the transaction-pooled path by the database design: PgBouncer
-- hands a session to whichever client needs it next, so a session-scoped lock
-- outlives the work it guarded and lands on a stranger.
--
-- THE ORDER, for the record (the full statement lives in
-- `pipeline/intent/lock.py` and is repeated at both attempt-lease namespace
-- definitions): attempt lease (W6 0x5732 reconciler / R4 0x5234 registrar)
-- is level 1; this work-unit lock is level 2 and is always taken beneath
-- whichever attempt lease is held, never above one. No holder of WU ever
-- waits for W6 or R4, so no cycle can form. Within one call that locks
-- SEVERAL units — which `cancel_work_units` can, unlike 040's per-unit loop
-- — the units are locked in ASCENDING work_unit_id order, so two concurrent
-- multi-unit calls over overlapping sets cannot deadlock against each other.
--
-- ============================================================================
-- WHY THE MUTATION API AND NOT A PYTHON WRITER
-- ============================================================================
--
-- 030's one-path rule is strict: "NO role receives INSERT, UPDATE, or DELETE
-- on any table in this file. Writes arrive exclusively through the SECURITY
-- DEFINER functions in 031, whose EXECUTE grants are the entire write
-- authorization surface." A Python cancellation writing its own audit row
-- would need a grant that does not exist, and creating a second ledger to
-- route around that is exactly what brief C forbids ("rather than inventing a
-- parallel record"). So cancellation is a function here, in 031's own shape —
-- same reason/dry_run/policy_citation/dispatcher parameters, same
-- write_mutation_audit call, same jsonb result — and
-- `pipeline.intent.cancellation` is its caller.
--
-- ============================================================================
-- WHICH UNITS ARE CANCELLABLE
-- ============================================================================
--
-- blocked, ready and submitted — the three non-terminal states — mirroring
-- quarantine's entry set in 036 for the identical reason ("an operator
-- override quarantining a unit must be able to interrupt it at whatever state
-- it is caught in"). complete, failed, quarantined and cancelled are refused:
-- complete work happened and cancelling it would be a claim about the past;
-- failed and quarantined are terminal operator/policy verdicts already, and
-- overwriting one with another blurs two deliberately distinct statements;
-- cancelling a cancelled unit is a no-op that should say so.
--
-- A refusal is NEVER an exception. Like 031's scale advisory and 040's
-- units_not_failed counter, a unit found in a non-cancellable state is
-- COUNTED and REPORTED with the state it was found in — the
-- report-what-you-could-not-do posture this stream uses everywhere, which is
-- what lets one call over a mixed set do the part it can and tell the caller
-- precisely what it did not.
--
-- NO ATTEMPT IS TOUCHED. Cancelling a unit does not consume, fail or
-- otherwise write an attempt row: attempts record physical execution, and a
-- cancellation is a statement about intent. A submitted unit's in-flight
-- Batch job is not killed here either — terminating live jobs is the
-- operator surface's business (`aws/terminate_batch_jobs.py`, package G) and
-- deliberately not this function's; the unit stops being work RAPID intends,
-- and the reconciler classifies whatever the scheduler does next as it always
-- would.

BEGIN;

-- ---------------------------------------------------------------------------
-- derived.cancel_work_units
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.cancel_work_units(
    p_work_unit_ids   bigint[],
    p_reason          text,
    p_dry_run         boolean DEFAULT true,
    p_policy_citation text DEFAULT NULL,
    p_dispatcher      text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    v_requested       integer;
    v_cancelled       integer := 0;
    v_audit_id        bigint;
    v_unit_id         bigint;
    v_state           text;
    v_ordered         bigint[];
    v_refused         jsonb := '[]'::jsonb;
    v_cancelled_ids   bigint[] := '{}'::bigint[];
BEGIN
    IF p_work_unit_ids IS NULL OR array_length(p_work_unit_ids, 1) IS NULL THEN
        RAISE EXCEPTION 'at least one work unit id is required — there is no unscoped cancellation';
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a reason is mandatory on every mutation (7a ruling 5)';
    END IF;

    -- ASCENDING ORDER, DEDUPLICATED. The order is the deadlock-avoidance
    -- convention stated in this file's header: two concurrent calls over
    -- overlapping unit sets acquire the shared units in the same sequence, so
    -- neither can hold one the other needs while waiting for one the other
    -- holds. The dedup keeps a caller's repeated id from taking the same lock
    -- twice and counting one unit as two.
    SELECT array_agg(DISTINCT x ORDER BY x)
      FROM unnest(p_work_unit_ids) AS t(x)
      INTO v_ordered;

    v_requested := coalesce(array_length(v_ordered, 1), 0);

    FOREACH v_unit_id IN ARRAY v_ordered
    LOOP
        -- THE LOCK, BEFORE THE READ THAT DECIDES. Taken even on the dry-run
        -- path: a rehearsal that reports "this unit would cancel" while
        -- another writer is mid-transition on it reports a state that was
        -- never true, and the whole value of a dry run is that the operator
        -- can trust what it says.
        PERFORM pg_advisory_xact_lock(22357, v_unit_id::int);

        SELECT state FROM public.work_units
         WHERE work_unit_id = v_unit_id
           INTO v_state;

        IF v_state IS NULL THEN
            v_refused := v_refused || jsonb_build_object(
                'work_unit_id', v_unit_id, 'state', null,
                'why', 'no such work unit');
            CONTINUE;
        END IF;

        IF v_state NOT IN ('blocked', 'ready', 'submitted') THEN
            v_refused := v_refused || jsonb_build_object(
                'work_unit_id', v_unit_id, 'state', v_state,
                'why', 'not a cancellable state');
            CONTINUE;
        END IF;

        IF p_dry_run THEN
            -- Reported as cancellable, nothing written. 030's CHECK
            -- (NOT dry_run OR coalesce(rows_affected,0) = 0) is what makes
            -- this honest at the ledger as well as here.
            v_cancelled := v_cancelled + 1;
            v_cancelled_ids := v_cancelled_ids || v_unit_id;
            CONTINUE;
        END IF;

        -- The CAS survives under the lock (see header). `state = v_state`
        -- re-verifies the exact state read above rather than merely "still
        -- cancellable": a unit that moved ready -> submitted between the read
        -- and the write is a different decision, not the same one.
        UPDATE public.work_units
           SET state = 'cancelled',
               blocked_reason = NULL,
               updated_at = now()
         WHERE work_unit_id = v_unit_id
           AND state = v_state;

        IF NOT FOUND THEN
            v_refused := v_refused || jsonb_build_object(
                'work_unit_id', v_unit_id, 'state', v_state,
                'why', 'state changed under the lock');
            CONTINUE;
        END IF;

        -- blocked_reason is cleared above because 036's
        -- work_units_blocked_reason_ck requires it NULL in any state but
        -- 'blocked'; cancelling a parked unit would otherwise violate that
        -- CHECK. The reason is not lost — it is in the unit's event history,
        -- which is where 036 says the account of how a unit got somewhere
        -- lives ("state is a derived summary, not the record of how it got
        -- there").
        INSERT INTO public.unit_events
            (work_unit_id, from_state, to_state, writer, reason, detail)
        VALUES
            (v_unit_id, v_state, 'cancelled', 'mutation_api', p_reason,
             jsonb_build_object('action_class', 'cancel_work_units',
                                'policy_citation', p_policy_citation));

        v_cancelled := v_cancelled + 1;
        v_cancelled_ids := v_cancelled_ids || v_unit_id;
    END LOOP;

    v_audit_id := derived.write_mutation_audit(
        'cancel_work_units', 'decide',
        format('work_units:ids=%s', v_requested),
        p_reason, p_dry_run,
        CASE WHEN p_dry_run THEN 0 ELSE v_cancelled END,
        p_policy_citation, p_dispatcher,
        jsonb_build_object('requested', v_requested,
                           'work_unit_ids', to_jsonb(v_ordered),
                           'units_cancelled', v_cancelled,
                           'cancelled_ids', to_jsonb(v_cancelled_ids),
                           'refused', v_refused));

    RETURN jsonb_build_object(
        'action', 'cancel_work_units',
        'dry_run', p_dry_run,
        'requested', v_requested,
        'units_cancelled', v_cancelled,
        'cancelled_ids', to_jsonb(v_cancelled_ids),
        'refused', v_refused,
        'audit_id', v_audit_id);
END;
$$;

COMMENT ON FUNCTION derived.cancel_work_units(bigint[], text, boolean, text, text) IS
  'Cancel work units through the audited mutation API (rule 9, brief C3). '
  'Takes the work-unit advisory lock (0x5755 ''WU'', the same namespace '
  'pipeline.intent.lock uses) on each unit in ascending id order before '
  'reading the state it decides from, then CAS-transitions blocked/ready/'
  'submitted -> cancelled, writing a unit_events row (writer=mutation_api) '
  'and a mutation_audit row in the same transaction. Terminal states and '
  'unknown ids are counted and reported in `refused`, never raised. '
  'Cancellation is distinct from failure: derived.retry_parked_attempts '
  'revives ''failed'' units and never sees a cancelled one. No attempt row '
  'is written and no running Batch job is terminated — cancellation is a '
  'statement about intent, not about physical execution. Dry-run by default.';

-- EXECUTE to the operator role only. Unlike 031's scoped retry, which
-- rapid_orchestrator also holds so the service can act on policy, cancelling
-- work is an operator or policy DECISION (action_tier 'decide', not
-- 'operate') and no automated service concludes that work should not happen.
-- Granting it to the orchestrator would put "stop doing this work" inside the
-- loop that does the work.
REVOKE ALL ON FUNCTION derived.cancel_work_units(bigint[], text, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION derived.cancel_work_units(bigint[], text, boolean, text, text) TO rapid_operator;

-- ---------------------------------------------------------------------------
-- derived.retry_parked_attempts — the same lock, added
-- ---------------------------------------------------------------------------
-- 040's per-unit loop CAS-transitions failed -> ready with no lock at all
-- ("CAS-only in both Python and the SQL function — no advisory/row lock in
-- the DB function either"). Brief C3 requires it to honour the one discipline;
-- this is that amendment.
--
-- COORDINATED WITH CR-2, NOT DRAFTED AGAINST THE BROKEN TEXT (brief C3:
-- "note: CR-2 against 040 is already open for its `state='failed'`
-- hard-coding; coordinate your draft with that correction rather than
-- drafting against the broken text"). This migration therefore does NOT
-- rewrite 040's body wholesale — doing so would collide with CR-2 and one of
-- the two corrections would silently lose. It adds ONLY the lock acquisition,
-- as a single statement at the top of the existing loop, leaving the
-- selection predicate and the CAS's from-state exactly as CR-2 finds them.
-- Whichever of the two lands first, the other still applies cleanly.
--
-- WHY A SEPARATE STATEMENT RATHER THAN CREATE OR REPLACE OF THE WHOLE BODY.
-- The lock must be taken inside the loop, before each unit's UPDATE, and
-- PL/pgSQL offers no way to inject a statement into an existing function
-- body: replacing the function means restating it. So this file DOES restate
-- it — but restates 040's text verbatim apart from the one added PERFORM,
-- and says so here, so a reviewer holding CR-2 can see the diff is one line
-- and rebase it trivially rather than reconciling two rewrites.
--
-- The parameter list is unchanged from 031/040 (text, text, integer, boolean,
-- text, text), so this is CREATE OR REPLACE in place under the same OID —
-- 039's DROP-first discipline applies only when the parameter list changes —
-- and 031's existing grants to rapid_operator and rapid_orchestrator carry
-- forward untouched.
--
-- The added line, for the reviewer's convenience:
--
--     FOREACH v_unit_id IN ARRAY coalesce(v_unit_ids, '{}'::bigint[])
--     LOOP
--   +     PERFORM pg_advisory_xact_lock(22357, v_unit_id::int);
--         UPDATE public.work_units ...
--
-- 040 already builds v_unit_ids with `array_agg(DISTINCT a.work_unit_id)`,
-- which returns them in ascending order, so the ascending-order convention
-- this file's header states is already satisfied by that aggregate and needs
-- no additional sort here.

-- 040's body is therefore RESTATED below, verbatim apart from that one
-- PERFORM. It is restated rather than described because a change request
-- that cannot be applied cannot be tested, and this file's whole purpose is
-- to be applied on rapid-admin so the lock discipline is DEMONSTRATED under
-- real concurrency rather than asserted. The restatement was produced
-- mechanically — 040's text between its CREATE OR REPLACE and its closing
-- $$; with the single PERFORM inserted at the head of the existing loop —
-- so a reviewer can diff it against 040 and see exactly one hunk.
--
-- ON ADOPTION, if CR-2 has already amended 040, this block is re-derived
-- from 040's then-current text by applying the same one-line insertion,
-- rather than merged as text. That instruction is the coordination the
-- brief asks for: the lock change is one line and rebases trivially; what
-- must not happen is two independent rewrites of one function racing.

CREATE OR REPLACE FUNCTION derived.retry_parked_attempts(
    p_run_id          text,
    p_reason          text,
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
    v_candidates          integer;
    v_affected            integer := 0;
    v_audit_id            bigint;
    v_warn                text := NULL;
    v_scale_soft constant integer := 500;
    v_ids                 bigint[];
    v_unit_ids            bigint[];
    v_units_without       integer := 0;
    v_units_transitioned  integer := 0;
    v_units_not_failed    integer := 0;
    v_unit_id             bigint;
    v_transitioned_id     bigint;
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

    -- The parked population: application-side failure, terminal, and not
    -- already released by an earlier executed call (the audit history is
    -- what records a release, so that is what is checked). Ordered and
    -- bounded so the same call is repeatable. Unchanged from 031.
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

    IF v_candidates >= v_scale_soft THEN
        v_warn := format(
          'scale advisory: %s attempts in scope (soft threshold %s) — '
          'proceeding, not refusing', v_candidates, v_scale_soft);
    END IF;

    -- Unit-level dedup: selected attempts map to their DISTINCT work units
    -- via attempts.work_unit_id (036's FK) — multiple failed attempts on the
    -- same unit cause exactly one transition. Attempts with no work unit
    -- (NULL FK — pre-intent-layer attempts, per 036) are excluded from the
    -- mapped set and counted separately below, never silently dropped.
    IF v_candidates > 0 THEN
        SELECT array_agg(DISTINCT a.work_unit_id)
          FROM public.attempts a
          WHERE a.attempt_id = ANY (v_ids)
            AND a.work_unit_id IS NOT NULL
          INTO v_unit_ids;

        SELECT count(*)
          FROM unnest(v_ids) AS x(attempt_id)
          JOIN public.attempts a ON a.attempt_id = x.attempt_id
          WHERE a.work_unit_id IS NULL
          INTO v_units_without;
    END IF;

    -- CAS-guarded failed->ready, one unit at a time so each transition gets
    -- its own unit_events row (append-only, one row per transition — 036).
    -- A per-unit loop rather than a set-based UPDATE ... RETURNING because
    -- each successful transition needs an individually authored event row
    -- in the same transaction; the loop keeps that pairing obvious rather
    -- than reconstructing it from a RETURNING set afterward.
    IF NOT p_dry_run THEN
        FOREACH v_unit_id IN ARRAY coalesce(v_unit_ids, '{}'::bigint[])
        LOOP
            -- THE WORK-UNIT LOCK (046, brief C3): the ONE line this
            -- restatement adds to 040's verbatim text.
            PERFORM pg_advisory_xact_lock(22357, v_unit_id::int);

            UPDATE public.work_units
               SET state = 'ready',
                   updated_at = now()
             WHERE work_unit_id = v_unit_id
               AND state = 'failed'
            RETURNING work_unit_id INTO v_transitioned_id;

            IF FOUND THEN
                INSERT INTO public.unit_events
                    (work_unit_id, from_state, to_state, writer, reason, detail)
                VALUES
                    (v_unit_id, 'failed', 'ready', 'mutation_api', p_reason,
                     jsonb_build_object('action_class', 'scoped_retry',
                                        'run_id', p_run_id));
                v_units_transitioned := v_units_transitioned + 1;
            ELSE
                -- Not in 'failed' at CAS time — already ready/submitted/
                -- complete/blocked/quarantined by a race or a prior release.
                -- Counted, never an error (see header).
                v_units_not_failed := v_units_not_failed + 1;
            END IF;
        END LOOP;
        v_affected := v_units_transitioned;
    ELSE
        -- Dry run: report what WOULD transition without touching anything,
        -- by checking current state without the CAS UPDATE.
        SELECT count(*) FILTER (WHERE u.state = 'failed'),
               count(*) FILTER (WHERE u.state <> 'failed')
          FROM unnest(coalesce(v_unit_ids, '{}'::bigint[])) AS x(work_unit_id)
          JOIN public.work_units u ON u.work_unit_id = x.work_unit_id
          INTO v_units_transitioned, v_units_not_failed;
    END IF;

    v_audit_id := derived.write_mutation_audit(
        'scoped_retry', 'operate',
        format('attempts:run_id=%s:limit=%s', p_run_id, p_max_attempts),
        p_reason, p_dry_run, v_affected, p_policy_citation, p_dispatcher,
        jsonb_build_object('run_id', p_run_id,
                           'candidates', v_candidates,
                           'attempt_ids', to_jsonb(coalesce(v_ids, '{}'::bigint[])),
                           'unit_ids', to_jsonb(coalesce(v_unit_ids, '{}'::bigint[])),
                           'units_transitioned', v_units_transitioned,
                           'units_not_failed', v_units_not_failed,
                           'attempts_without_unit', v_units_without,
                           'scale_advisory', v_warn));

    RETURN jsonb_build_object(
        'action', 'scoped_retry',
        'dry_run', p_dry_run,
        'run_id', p_run_id,
        'candidates', v_candidates,
        'rows_affected', v_affected,
        'units_transitioned', v_units_transitioned,
        'units_not_failed', v_units_not_failed,
        'attempts_without_unit', v_units_without,
        'scale_advisory', v_warn,
        'audit_id', v_audit_id);
END;
$$;

COMMENT ON FUNCTION derived.retry_parked_attempts IS
  'Operate tier: release parked (application-failed) attempts for retry by '
  'CAS-transitioning their DISTINCT work units failed->ready (unit-level '
  'dedup — 040, ruling 13 scoped_retry clause). Each transition writes one '
  'unit_events row (writer=mutation_api) alongside the mutation_audit row, '
  'same transaction. AS AMENDED BY 046: each unit''s work-unit advisory '
  'lock (0x5755 ''WU'') is taken before its CAS, so this function and the '
  'application-side writers serialize on one discipline (rule 9). A unit '
  'not in ''failed'' at CAS time is a counted no-op, never an error. '
  'Signature unchanged from 031/040 — CREATE OR REPLACE in place, grants '
  'carry forward on the same OID.';

-- No REVOKE/GRANT: the signature is byte-identical to 031's and 040's, so
-- this replaces the body in place under the same OID and 031's existing
-- EXECUTE grants to rapid_operator and rapid_orchestrator stay attached.

-- schema_migrations is recorded by apply-db-migrations.sh, not by the
-- migration itself.

COMMIT;
