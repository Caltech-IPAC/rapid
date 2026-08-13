-- DRAFT-repair-refused-outbox.sql — an audited operator repair from REFUSED
-- back to PENDING for alert_outbox rows.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the
-- schema and this file is not applied by `apply-db-migrations.sh` until its
-- owner adopts it. LEFT UNNUMBERED DELIBERATELY: the README's adoption table
-- records that 053-055 are already landed `rapid_systems`-side (053 revives
-- 'blocked' in retry_parked_attempts, 054 is the checksum-width CR, 055 is
-- the idempotent addExposure/addL2File rewrite), so this file's owner
-- assigns whatever number is next when it lands; only the SQL body below is
-- meant to survive verbatim, the same as every draft this directory has
-- adopted before it. Requires 050 (alert_outbox, delivery_policies) and 047
-- (derived.mutation_audit's idempotency_key/expected_state columns,
-- derived.mutation_replay) — apply in order.
--
-- ============================================================================
-- WHAT THIS REPAIRS, AND WHY IT DOES NOT CHANGE THE CLASSIFICATION
-- ============================================================================
--
-- pipeline/publisher/classification.py classifies a broker authorization or
-- protocol/version failure (TopicAuthorizationFailedError,
-- ClusterAuthorizationFailedError, UnsupportedVersionError, ...) as a
-- DEFINITE refusal, and pipeline/publisher/cycle.py marks the row REFUSED —
-- correctly: a retry against the SAME broker ACL or the SAME client/broker
-- version mismatch gets the SAME answer, so retrying it automatically would
-- be an infinite loop against a fixed answer.
--
-- But the condition that produced the refusal is sometimes not fixed at
-- all — it is a team-onboarding ACL that was granted late, or a broker
-- upgrade that resolved a version mismatch. Once an operator has fixed the
-- EXTERNAL condition, the row is still REFUSED and nothing in the schema or
-- the application moves a REFUSED row back to PENDING: the claim only
-- selects PENDING (outbox.py:96-125), and no migration before this one adds
-- a REFUSED -> PENDING transition anywhere. The wave that hit the
-- misconfiguration is lost permanently even after the operator fixes what
-- caused it.
--
-- This migration adds exactly the repair and nothing else. It does NOT
-- reclassify which broker errors are terminal (classification.py's mapping
-- is unchanged and this file does not touch it), does NOT add automatic
-- retry or backoff for REFUSED rows (a row moves only on an explicit,
-- reasoned operator call), and does NOT add a new outbox state — the
-- repaired row re-enters the EXISTING PENDING state and is claimed,
-- checked, and sent by the ordinary cycle exactly as any other PENDING row
-- is, including a fresh policy_authorized check and a fresh classification
-- if it fails again.
--
-- ============================================================================
-- WHY THIS FOLLOWS retry_parked_attempts' SHAPE, NOT record_external_action's
-- ============================================================================
--
-- Draft 047 already has two audited-mutation shapes. `record_external_action`
-- is for actions whose target is OUTSIDE this database (a Batch termination,
-- an S3 closure record) — the database connection exists only to leave a
-- trace. This repair's target is alert_outbox itself, a table in THIS
-- database, so it follows `retry_parked_attempts`' shape instead: the
-- function computes its own candidate population, performs the UPDATE
-- in-transaction, and records what it actually changed — the same "targets
-- specific state" case draft 047's header names, with the same
-- expected-state discipline (the operator's `{"candidates": n}` from a dry
-- run must still match at apply time, or the call refuses rather than
-- silently repairing a different set of rows than the one reviewed).
--
-- ============================================================================
-- SCOPE, AND WHY release_identity RATHER THAN A LIST OF alert_id
-- ============================================================================
--
-- alert_outbox's own scoping idiom is release_identity — the claim's
-- delivery-policy check, delivery_policies' primary key, and the
-- release/state rollup view (alert_outbox_health) are all keyed that way,
-- because the failure classes this repairs (authorization, client/broker
-- version) are properties of A DEPLOYMENT, not of an individual packet: an
-- ACL grant or a broker upgrade clears every REFUSED row that deployment
-- produced, not one. Scoping by release_identity is therefore both the
-- natural unit of "what did the operator just fix" and a bound that keeps
-- the repair from being a blanket UPDATE over the whole table.
--
-- An optional p_max_rows caps the population the same way
-- retry_parked_attempts' p_max_attempts does, so a repair with a
-- surprisingly large candidate count is visible in the dry run's count
-- before anything is written rather than silently touching more rows than
-- an operator reviewed.

BEGIN;

-- ---------------------------------------------------------------------------
-- derived.repair_refused_outbox_rows
-- ---------------------------------------------------------------------------
-- Moves REFUSED alert_outbox rows for one release back to PENDING, clearing
-- the claim columns (already NULL on a REFUSED row per
-- alert_outbox_claim_shape_ck, restated here rather than assumed) and the
-- refusal_reason — alert_outbox_refusal_shape_ck requires refusal_reason
-- IS NOT NULL exactly when state = 'REFUSED', so a state-only UPDATE would
-- violate the same CHECK draft 050 added to keep that pair honest.
--
-- resend_count is left untouched: it counts sends that actually happened,
-- and a REFUSED row's prior sends did happen — the repair returns the row
-- to the ordinary at-least-once flow, it does not erase its history.
CREATE OR REPLACE FUNCTION derived.repair_refused_outbox_rows(
    p_idempotency_key text,
    p_release_identity text,
    p_reason           text,
    p_expected_state   jsonb DEFAULT NULL,
    p_max_rows         integer DEFAULT 200,
    p_dry_run          boolean DEFAULT true,
    p_policy_citation  text DEFAULT NULL,
    p_dispatcher       text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    v_candidates integer;
    v_affected   integer := 0;
    v_audit_id   bigint;
    v_ids        text[];
    v_scope      text;
    v_replay     jsonb;
    v_expect     integer;
BEGIN
    IF p_release_identity IS NULL OR length(btrim(p_release_identity)) = 0 THEN
        RAISE EXCEPTION
          'a release_identity scope is required — there is no unscoped repair';
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a reason is mandatory on every mutation (7a ruling 5)';
    END IF;
    IF p_max_rows IS NULL OR p_max_rows < 1 THEN
        RAISE EXCEPTION 'p_max_rows must be >= 1';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'an idempotency key is mandatory on a keyed call';
    END IF;

    v_scope := format('alert_outbox:release_identity=%s:limit=%s',
                      p_release_identity, p_max_rows);

    IF NOT p_dry_run THEN
        PERFORM pg_advisory_xact_lock(hashtext('rapid.mutation_key'),
                                      hashtext(p_idempotency_key));
        v_replay := derived.mutation_replay(p_idempotency_key,
                                            'repair_refused_outbox', v_scope);
        IF v_replay IS NOT NULL THEN
            RETURN v_replay;
        END IF;
    END IF;

    -- The candidate population: every REFUSED row for this release, oldest
    -- first — the same (created_at, alert_id) order the claim itself uses
    -- (outbox.py's claim_batch), so a repaired row re-enters the queue in
    -- the order it would have sent in had it never been refused.
    SELECT array_agg(alert_id ORDER BY created_at, alert_id)
      FROM (
        SELECT alert_id, created_at
          FROM alert_outbox
         WHERE release_identity = p_release_identity
           AND state = 'REFUSED'
         ORDER BY created_at, alert_id
         LIMIT p_max_rows
      ) s
      INTO v_ids;

    v_candidates := coalesce(array_length(v_ids, 1), 0);

    IF p_expected_state IS NOT NULL AND p_expected_state ? 'candidates' THEN
        v_expect := (p_expected_state ->> 'candidates')::integer;
        IF v_expect IS DISTINCT FROM v_candidates THEN
            RAISE EXCEPTION
              'expected-state mismatch: caller expected % REFUSED row(s) '
              'for release %, found %', v_expect, p_release_identity,
              v_candidates
              USING ERRCODE = 'RA001';
        END IF;
    END IF;

    IF NOT p_dry_run AND v_candidates > 0 THEN
        UPDATE alert_outbox
           SET state = 'PENDING',
               claim_token = NULL,
               claimed_at = NULL,
               refusal_reason = NULL
         WHERE alert_id = ANY(v_ids)
           AND state = 'REFUSED';
        GET DIAGNOSTICS v_affected = ROW_COUNT;
    END IF;

    INSERT INTO derived.mutation_audit
        (actor, dispatcher, action_class, action_tier, target_scope,
         reason, dry_run, rows_affected, policy_citation, detail,
         idempotency_key, expected_state)
    VALUES (session_user, coalesce(p_dispatcher, session_user),
            'repair_refused_outbox', 'operate', v_scope,
            p_reason, p_dry_run, v_affected, p_policy_citation,
            jsonb_build_object('release_identity', p_release_identity,
                               'candidates', v_candidates,
                               'alert_ids', to_jsonb(coalesce(v_ids, '{}'::text[]))),
            p_idempotency_key, p_expected_state)
    RETURNING audit_id INTO v_audit_id;

    RETURN jsonb_build_object(
        'action', 'repair_refused_outbox',
        'dry_run', p_dry_run,
        'replayed', false,
        'release_identity', p_release_identity,
        'candidates', v_candidates,
        'rows_affected', v_affected,
        'idempotency_key', p_idempotency_key,
        'audit_id', v_audit_id);
END;
$$;

COMMENT ON FUNCTION derived.repair_refused_outbox_rows(text, text, text, jsonb, integer, boolean, text, text) IS
  'Operate tier, keyed: move REFUSED alert_outbox rows for one release back '
  'to PENDING after an operator has fixed the external condition (broker '
  'ACL, client/broker version) that terminalized them. Mandatory '
  'idempotency key, optional expected candidate count refusing on mismatch '
  '(RA001), dry-run default. Does not reclassify any error and does not add '
  'automatic retry — the repaired row re-enters the ordinary PENDING flow '
  'and is claimed, authorized and sent by the next publisher cycle exactly '
  'as any other PENDING row.';

-- ---------------------------------------------------------------------------
-- Grant: rapid_operator only.
-- ---------------------------------------------------------------------------
-- Same posture as 047's two operate-tier functions: PUBLIC-revoked first
-- (PostgreSQL grants EXECUTE to PUBLIC on creation), then granted to
-- rapid_operator alone. Not granted to rapid_orchestrator or rapid_publisher:
-- this is a human-reasoned repair of a misconfiguration an operator just
-- fixed, not an automated action any service policy authorizes, and
-- rapid_publisher in particular holds no grant on alert_outbox.state at all
-- outside its own claim/finalize columns (050's column-level GRANT UPDATE) —
-- this function's SECURITY DEFINER body is what makes the repair possible
-- without widening that grant.
REVOKE ALL ON FUNCTION derived.repair_refused_outbox_rows(text, text, text, jsonb, integer, boolean, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION derived.repair_refused_outbox_rows(text, text, text, jsonb, integer, boolean, text, text)
  TO rapid_operator;

-- write_mutation_audit remains untouched and granted to nobody; 030's
-- append-only trigger is untouched; no role gains
-- INSERT/UPDATE/DELETE on derived.mutation_audit directly. No role's grant
-- on alert_outbox itself changes — this function's SECURITY DEFINER body is
-- the only new write path, and it writes only rows already REFUSED for the
-- named release.

-- no-grant: creates no table (one function only); mutation_audit's own
-- grants are unchanged.

-- schema_migrations is recorded by apply-db-migrations.sh, not by the
-- migration itself.

COMMIT;
