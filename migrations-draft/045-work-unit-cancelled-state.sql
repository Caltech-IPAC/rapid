-- 045-work-unit-cancelled-state.sql — work_units gains a seventh state:
-- 'cancelled', the operator disposition that is not a failure.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the
-- schema and this file is not applied by `apply-db-migrations.sh` until its
-- owner adopts it. See that directory's README.
--
-- Conformance rule 9 (minimal-viable target § 4): "Cancellation, quarantine,
-- retry and acceptance all take the same work-unit lock in the same order."
-- Implementation brief C, work item C3: "Cancellation becomes an explicit
-- disposition: an operator- or policy-initiated terminal state distinct from
-- failure... If it needs a new `work_units.state` value, that is a DRAFT
-- migration amending `work_units_state_ck` (036:130-132)." It does, and this
-- is that amendment.
--
-- WHY A SEVENTH STATE RATHER THAN REUSING 'failed'. The two answer different
-- questions and an operator surface that cannot tell them apart cannot do its
-- job. `failed` means RAPID attempted the work and the retry policy
-- exhausted — a fact about the work. `cancelled` means someone decided the
-- work should not happen — a fact about a decision, carrying an actor and a
-- reason that `failed` has nowhere to put.
--
-- The sharper argument is mechanical, not semantic: 040's
-- `derived.retry_parked_attempts` selects units in `failed` and CAS-transitions
-- them back to `ready`. A cancelled unit spelled `failed` would therefore be
-- REVIVED by the next scoped retry — silently, and with an audit row claiming
-- a retry of work someone had cancelled. Distinguishing the states is what
-- makes cancellation terminal in fact rather than by convention.
--
-- WHY THE CHECK IS REPLACED RATHER THAN DROPPED AND LEFT OFF. 036 states the
-- enumeration as closed on purpose ("Six states, exactly"), and the value of a
-- closed enumeration is that an unknown state cannot be written by a typo or
-- by code running against a schema it does not match. Widening it to seven
-- keeps that property; dropping it would not.
--
-- NO DATA MIGRATION. This widens what is permitted and narrows nothing, so
-- every existing row still satisfies the new constraint and no row changes.
-- It is an EXPAND migration in the release protocol's sense (minimal-viable
-- target § 2.10): old application code that never writes 'cancelled' runs
-- unchanged against this schema, so it may be applied ahead of the code that
-- uses it, which is the order the protocol asks for.
--
-- THE TERMINALITY OF 'cancelled' IS NOT IN THIS FILE, and that is deliberate.
-- Migration 036 records the same division for every other state: "the DDL's
-- vocabulary CHECK would not catch this at all, since `to_state` alone is
-- always a legal vocabulary member; only the (from, to) PAIR can be illegal,
-- and nothing in the schema encodes pairs." Which states may enter and leave
-- 'cancelled' is `pipeline.intent.writer._TRANSITION_GRAPH`'s to enforce — it
-- admits blocked/ready/submitted -> cancelled under writer='mutation_api' and
-- declares no edge out — and 046's function enforces the same set SQL-side.

BEGIN;

ALTER TABLE public.work_units
    DROP CONSTRAINT IF EXISTS work_units_state_ck;

ALTER TABLE public.work_units
    ADD CONSTRAINT work_units_state_ck CHECK (state IN (
        'blocked', 'ready', 'submitted', 'complete', 'failed', 'quarantined',
        'cancelled'
    ));

COMMENT ON COLUMN public.work_units.state IS
  'The unit''s current state, a derived summary of unit_events (036''s
   invariant 1: history is append-only, state is kept in sync). Seven
   values: 036''s original six plus ''cancelled'' (045) — an operator- or
   policy-initiated terminal disposition, distinct from ''failed'' because
   the retry taxonomy treats the two differently: derived.retry_parked_
   attempts revives ''failed'' units and must never revive a cancelled one.
   Which (from, to) pairs are legal is not encoded here; see
   pipeline.intent.writer._TRANSITION_GRAPH and derived.cancel_work_units.';

-- unit_events.to_state carries the same vocabulary and is bounded by its own
-- CHECK in 036. A cancellation writes an event row like every other
-- transition, so that constraint has to admit the value too — otherwise the
-- state column would accept a transition its own history could not record.
ALTER TABLE public.unit_events
    DROP CONSTRAINT IF EXISTS unit_events_to_state_ck;

ALTER TABLE public.unit_events
    ADD CONSTRAINT unit_events_to_state_ck CHECK (to_state IN (
        'blocked', 'ready', 'submitted', 'complete', 'failed', 'quarantined',
        'cancelled'
    ));

ALTER TABLE public.unit_events
    DROP CONSTRAINT IF EXISTS unit_events_from_state_ck;

-- from_state is NULLABLE — 036: "from_state NULL on the unit's first event
-- (creation)" — so the CHECK admits NULL alongside the vocabulary. A unit is
-- never CREATED cancelled (nothing has decided anything about work that does
-- not exist yet), but the constraint bounds the column's vocabulary rather
-- than the machine's edges, exactly as its to_state sibling does.
ALTER TABLE public.unit_events
    ADD CONSTRAINT unit_events_from_state_ck CHECK (from_state IS NULL OR from_state IN (
        'blocked', 'ready', 'submitted', 'complete', 'failed', 'quarantined',
        'cancelled'
    ));

-- schema_migrations is recorded by apply-db-migrations.sh, not by the
-- migration itself.

COMMIT;
