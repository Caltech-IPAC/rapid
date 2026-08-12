-- 049-association-sets-and-watermarks.sql — the association ordering rule 19
-- requires: an immutable `association_set` identity scoping association
-- output, and a persistent per-(set, lane) watermark advanced in the same
-- transaction as the associations it accepts.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the schema
-- and this file is not applied by `apply-db-migrations.sh` until its owner
-- adopts it. See that directory's README. Requires 007 (the sources/merges/
-- astroobjects family this scopes) — apply in order.
--
-- Conformance rule 19, verbatim:
--
--     "Association is processed in canonical `(observation_time,
--      detection_id)` order behind a persistent watermark per
--      `(association_set, lane)` — initially one lane per set — advanced in
--      the same transaction as the accepted associations; reprocessing sets
--      order themselves and never regress the live watermark; tasks within a
--      set run concurrently only with disjoint conflict neighborhoods
--      (vacuous at one lane per set)."
--
-- and §2.5 of the minimal viable target, which is the design statement rule 19
-- compresses. The sentence this file exists to answer is §2.5's second one:
--
--     "Serial execution does not by itself guarantee that a later
--      observation's association cannot run ahead of an earlier one still in
--      retry."
--
-- ============================================================================
-- THE VOCABULARY MAPPING
-- ============================================================================
--
-- Rule 19 speaks in the target's generic vocabulary — `observation_time`,
-- `detection_id`, `lane`. Nothing in this schema carries those names, so the
-- mapping is fixed here and every code path uses it:
--
--   A-work unit             one crossmatch job, identity (proc_date, field),
--                           which is `submission.payloads.CrossmatchPayload`'s
--                           own grain.
--   canonical claim order   ascending (proc_date, field). `proc_date` is the
--                           observation-time proxy AT THE WORK GRAIN — a
--                           crossmatch unit associates the detections of that
--                           processing date — and `field` is the
--                           deterministic tiebreak among the units of one
--                           date. This is rule 19's `(observation_time,
--                           detection_id)` at the grain association is
--                           actually claimed in.
--   canonical detection     within one unit, ascending (mjdobs, sid) over the
--   order                   source rows. `sources.mjdobs` (007-sources-family
--                           .sql:101, `double precision NOT NULL`, "MJD OBS of
--                           exposure", indexed by `sources_mjdobs_idx`) IS the
--                           observation time; `sid` (bigint, defaulted from
--                           the single global `sources_sid_seq`, so monotone
--                           in insert order across the whole child family) is
--                           the detection identity and the tiebreak. Both
--                           halves of rule 19's pair exist as real columns —
--                           the vocabulary mapping's "if none exists" fallback
--                           to `sid` alone does not apply here.
--   association_set         an immutable identity scoping association output
--                           and its watermark. Day one there is exactly one:
--                           the live prompt set, inserted below.
--   lane                    1 per set initially. The watermark is keyed
--                           (association_set, lane) so later lanes multiply
--                           ROWS, not the model.
--
-- ============================================================================
-- WHAT EXISTS TODAY, AND WHY IT IS NOT ORDERING
-- ============================================================================
--
-- Nothing. Repo-wide there is no `association_set`, no association watermark
-- and no ordering key of any kind. The one watermark in the codebase is
-- registration's (`attempts.registered_record_sequence`, 011 + brief C), which
-- is attempt-scoped and answers a different question: "has this attempt's
-- terminal record been registered at this sequence yet". It says nothing about
-- the ORDER in which association work is claimed, and it is not touched here.
--
-- The claim path today (`submission/gathering.py`, `gather_crossmatch_units`)
-- yields every unblocked field of every gate-passing processing date, in
-- whatever order the field enumeration returns, onto the unbounded bulk queue.
-- Two readiness gates exist and are correct as far as they go — catalog-load
-- completeness per date, and no blocking attempt per field — but neither is an
-- ORDER. So §2.5's failure is available today by construction: date d2's field
-- is gatherable while date d1's field sits failed-and-retryable, and the
-- scheduler is free to run d2 first.
--
-- This file supplies the durable half of the repair: the set identity, and the
-- watermark the claim path consults and the acceptance path advances.
--
-- ============================================================================
-- WHY THE WATERMARK VALUE IS SEQUENCE-SHAPED, NOT A BOOLEAN
-- ============================================================================
--
-- It mirrors C's registration watermark deliberately (`registered_record_
-- sequence`, and `_MARK_REGISTERED_SQL`'s CAS in `pipeline/registration/
-- consumer.py:168-171`). A boolean "this unit is done" would need a special
-- case for every re-acceptance and every supersession. A watermark that
-- records WHICH unit was last accepted — here the pair (proc_date, field),
-- the canonical order's own key — needs none: "is this unit at or behind the
-- frontier" is a comparison, and re-accepting a unit at or behind it is a
-- no-op by the same comparison that refuses a regression.
--
-- Monotonicity is enforced by the UPDATE's own WHERE clause, never by a
-- read-then-write in the application. That is the same choice C made and for
-- the same reason: a predicate the application evaluates holds until the first
-- concurrent writer; a predicate the database evaluates as part of the UPDATE
-- is the guard. `derived.advance_association_watermark` below is that CAS, and
-- it reports through its own return value whether it moved.
--
-- ============================================================================
-- SET SCOPING AND REPROCESSING ISOLATION — STRUCTURAL, NOT POLICED
-- ============================================================================
--
-- Association output lands in the per-field clone families
-- `astroobjects_<field>` and `merges_<field>` (`create_field_tables`,
-- `pipeline/stages/post_db.py:489-524`). Those become SET-SCOPED, by name:
--
--   * the LIVE set keeps today's names exactly — `astroobjects_4641773`,
--     `merges_4641773`. No rename, no data motion, no backfill. Every existing
--     row, index, grant and reader is untouched, which is the only acceptable
--     answer for a live prompt set.
--   * a NON-LIVE set materializes its own clone family under its own prefix —
--     `astroobjects_s<set>_<field>`, `merges_s<set>_<field>`.
--
-- Reprocessing isolation is then STRUCTURAL rather than policed: a
-- reprocessing set cannot mutate the live tables because it never names them.
-- There is no rule to enforce, no trigger to check and no grant to withhold —
-- the isolation is a property of the table names the code computes from the
-- set. `derived.association_table_name` below is the single place that
-- computation lives, so "which family does this set write" has exactly one
-- answer in SQL and in Python (`pipeline.association.sets.table_name`, which
-- mirrors it).
--
-- Rule 19's "reprocessing sets order themselves and never regress the live
-- watermark" is then also structural: watermarks are per-set rows, so a
-- reprocessing set's advance touches its own row and cannot reach the live
-- one.
--
-- ============================================================================
-- THE LOCK ORDER
-- ============================================================================
--
-- The per-(set, lane) claim lease gets a new advisory namespace, 0x414C ('AL'
-- — association lane), and a documented position in the order. The existing
-- order is unchanged and unreordered; this extends it:
--
--     LEVEL 1  R4 0x5234  the registrar's per-attempt lease
--                         (`pipeline.registration.consumer`)
--     LEVEL 1  W6 0x5732  the reconciler's per-attempt lease
--                         (`pipeline.reconciler.lease`) — distinct from R4 so
--                         the two never serialize against each other
--     LEVEL 2  WU 0x5755  the per-work-unit lock (`pipeline.intent.lock`),
--                         always taken UNDERNEATH a level-1 lease
--     LEVEL 3  AL 0x414C  the per-(association_set, lane) claim lease, added
--                         here, always taken UNDERNEATH any of the above
--
-- AL IS THE LOWEST LEVEL, and that is what keeps the order total and therefore
-- deadlock-free. The association acceptance transaction takes AL as its FIRST
-- statement and holds nothing above it; a path that already holds WU (a
-- disposition) may take AL beneath it, never the reverse. The key is
-- (association_set, lane) — two small dense integer spaces — so it collides
-- with no attempt id and no work_unit_id, which is the same reasoning
-- `WORK_UNIT_NAMESPACE` records for itself.
--
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- The set registry
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS association_sets (
    association_set     integer     PRIMARY KEY,
    -- 'live_prompt' or 'reprocessing'. Constrained rather than free text: the
    -- kind decides the clone-family naming (the live set keeps today's names),
    -- so an unrecognised kind is not a label problem, it is an unanswerable
    -- question about which tables to write.
    kind                text        NOT NULL,
    -- Human-facing, for the operator surface. Not an identity.
    label               text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT association_sets_kind_ck
        CHECK (kind IN ('live_prompt', 'reprocessing'))
);

COMMENT ON TABLE association_sets IS
    'Immutable association-set identities (rule 19). Association output and '
    'its watermark are scoped to a set; the live prompt set keeps the '
    'unprefixed clone-family names, a reprocessing set materializes its own '
    'isolated family and so cannot mutate live tables.';
COMMENT ON COLUMN association_sets.association_set IS
    'The set identity. Assigned, never generated: 1 is the well-known live '
    'prompt set and no code path outside derived.live_association_set() may '
    'hard-code it.';
COMMENT ON COLUMN association_sets.kind IS
    'live_prompt or reprocessing. Decides clone-family naming, which is what '
    'makes reprocessing isolation structural rather than policed.';

-- AT MOST ONE LIVE PROMPT SET, enforced by the database rather than by
-- convention. "The live set" is a phrase the whole design uses in the
-- singular; a schema that admitted two would make it ambiguous exactly when
-- it mattered.
CREATE UNIQUE INDEX IF NOT EXISTS association_sets_one_live
    ON association_sets ((kind))
    WHERE kind = 'live_prompt';

COMMENT ON INDEX association_sets_one_live IS
    'At most one live prompt set. The design says "the live set" in the '
    'singular throughout; this is where that becomes true.';

-- The well-known live row. `ON CONFLICT DO NOTHING` so re-application
-- converges rather than erroring, and so an operator who has already inserted
-- it is not overwritten.
INSERT INTO association_sets (association_set, kind, label)
VALUES (1, 'live_prompt', 'live prompt associations')
ON CONFLICT (association_set) DO NOTHING;

-- ----------------------------------------------------------------------------
-- The watermarks
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS association_watermarks (
    association_set     integer     NOT NULL
        REFERENCES association_sets (association_set),
    -- One lane per set initially (§2.5). Concurrent lanes later multiply rows
    -- here, not the model.
    lane                integer     NOT NULL DEFAULT 0,

    -- THE WATERMARK VALUE: the last ACCEPTED unit's (proc_date, field), which
    -- is the canonical claim order's own key. NULL/NULL means "nothing
    -- accepted in this set yet", so every unit is ahead of the frontier and
    -- the first one in canonical order is claimable.
    --
    -- `proc_date` is the pipeline's `YYYYMMDD` string form throughout
    -- (`submission.payloads.CrossmatchPayload.proc_date`), and it is stored as
    -- text for exactly that reason: a date type here would need a conversion
    -- at every comparison, and text comparison of a zero-padded YYYYMMDD is
    -- the same order as date comparison. `_validate_proc_date` in
    -- `submission/gathering.py` is what keeps the form honest.
    watermark_proc_date text        NULL,
    watermark_field     integer     NULL,

    advanced_at         timestamptz NULL,

    PRIMARY KEY (association_set, lane),
    -- The two halves move together or not at all: a watermark with a date and
    -- no field is not a position in the canonical order.
    CONSTRAINT association_watermarks_pair_ck
        CHECK ((watermark_proc_date IS NULL) = (watermark_field IS NULL))
);

COMMENT ON TABLE association_watermarks IS
    'The persistent per-(association_set, lane) association watermark '
    '(rule 19). Its value is the last ACCEPTED unit''s (proc_date, field) — '
    'sequence-shaped, not boolean, mirroring the registration watermark, so '
    're-acceptance and supersession need no special case. Advanced in the '
    'same transaction as the associations it accepts.';
COMMENT ON COLUMN association_watermarks.lane IS
    'One lane (0) per set initially. Keyed here so later lanes multiply rows '
    'rather than changing the model.';
COMMENT ON COLUMN association_watermarks.watermark_proc_date IS
    'Last accepted unit''s processing date, YYYYMMDD text — the '
    'observation-time proxy at the work grain. NULL with watermark_field NULL '
    'means nothing accepted in this set yet.';
COMMENT ON COLUMN association_watermarks.watermark_field IS
    'Last accepted unit''s field — the deterministic tiebreak completing the '
    'canonical order key.';

-- The live set's lane-0 watermark, at the origin. Created by the migration so
-- no code path has to handle "the row does not exist yet" as a live case: a
-- claim reads it, an advance CASes it, and both find a row.
INSERT INTO association_watermarks (association_set, lane)
VALUES (1, 0)
ON CONFLICT (association_set, lane) DO NOTHING;

-- ----------------------------------------------------------------------------
-- The single well-known-row lookup
-- ----------------------------------------------------------------------------

-- THE ONLY PLACE THE LIVE SET IS NAMED. The brief's constraint, verbatim:
-- "the schema and every code path key on the set from day one — no SQL or
-- code hard-codes the live set outside a single well-known-row lookup". This
-- function is that lookup on the SQL side; `pipeline.association.sets
-- .live_association_set` is its Python counterpart and reads this table too.
CREATE OR REPLACE FUNCTION derived.live_association_set()
RETURNS integer
LANGUAGE sql
STABLE
AS $$
    SELECT association_set FROM association_sets WHERE kind = 'live_prompt';
$$;

COMMENT ON FUNCTION derived.live_association_set() IS
    'The one well-known-row lookup for the live prompt set. No other SQL may '
    'hard-code the live set identity (rule 19 / brief F1).';

-- ----------------------------------------------------------------------------
-- Set-scoped clone-family naming
-- ----------------------------------------------------------------------------

-- The single computation of "which table family does this set write". The
-- live set keeps today's unprefixed names — no rename, no data motion, no
-- backfill; every non-live set gets its own prefix and is therefore isolated
-- from the live tables BY CONSTRUCTION, not by a rule anything has to enforce.
-- DROPPED FIRST, then created. `CREATE OR REPLACE FUNCTION` cannot RENAME an
-- existing function's parameters — it fails with "cannot change name of input
-- parameter" — so a database that already took an earlier revision of this
-- draft would refuse the corrected one, and the second application in the
-- idempotence check would refuse it too. The drop is by full signature so it
-- can only ever remove this exact function.
DROP FUNCTION IF EXISTS derived.association_table_name(text, integer, integer);

-- The parameters are `p_`-prefixed and the column they compare against is
-- table-qualified. Naming a parameter `association_set` — the same as the
-- column — made PL/pgSQL resolve the qualified form
-- `derived.association_table_name.association_set` as a TABLE reference and
-- fail with "missing FROM-clause entry", caught by the contract tier's set
-- isolation test on the first acceptance run. The prefix removes the
-- ambiguity at the source rather than working around it.
CREATE OR REPLACE FUNCTION derived.association_table_name(
    p_prototype text, p_association_set integer, p_field integer)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    set_kind text;
BEGIN
    SELECT s.kind INTO set_kind
      FROM association_sets AS s
     WHERE s.association_set = p_association_set;

    IF set_kind IS NULL THEN
        RAISE EXCEPTION 'unknown association_set %', p_association_set
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF set_kind = 'live_prompt' THEN
        -- Today's names, unchanged. This branch is why adopting this
        -- migration moves no data.
        RETURN format('%s_%s', p_prototype, p_field);
    END IF;

    RETURN format('%s_s%s_%s', p_prototype, p_association_set, p_field);
END;
$$;

COMMENT ON FUNCTION derived.association_table_name(text, integer, integer) IS
    'The set-scoped clone-family name. The live set keeps the unprefixed '
    'names (no rename, no backfill); a non-live set gets its own prefix, '
    'which is what makes reprocessing isolation structural — a reprocessing '
    'set never names a live table. Mirrored in Python by '
    'pipeline.association.sets.table_name.';

-- ----------------------------------------------------------------------------
-- The CAS advance
-- ----------------------------------------------------------------------------

-- THE MONOTONIC ADVANCE, guarded by the UPDATE's own WHERE clause exactly as
-- `_MARK_REGISTERED_SQL` guards the registration watermark
-- (`pipeline/registration/consumer.py:168-171`). Returns TRUE when the
-- watermark moved, FALSE when the CAS refused — a refusal is a normal
-- outcome, not an error: it is what a stale retry landing late, or a
-- concurrent duplicate attempt of the same unit, is supposed to produce.
--
-- The comparison is ROW-WISE on (proc_date, field), which in PostgreSQL is
-- exactly the canonical order's lexicographic comparison, so the predicate
-- and the ORDER BY the claim path uses cannot drift apart. The NULL watermark
-- (nothing accepted yet) is admitted by the first disjunct.
CREATE OR REPLACE FUNCTION derived.advance_association_watermark(
    p_association_set integer, p_lane integer,
    p_proc_date text, p_field integer)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    moved integer;
BEGIN
    UPDATE association_watermarks AS w
       SET watermark_proc_date = p_proc_date,
           watermark_field     = p_field,
           advanced_at         = now()
     WHERE w.association_set = p_association_set
       AND w.lane            = p_lane
       AND (w.watermark_proc_date IS NULL
            OR (w.watermark_proc_date, w.watermark_field)
                < (p_proc_date, p_field));

    GET DIAGNOSTICS moved = ROW_COUNT;
    RETURN moved = 1;
END;
$$;

COMMENT ON FUNCTION derived.advance_association_watermark(
    integer, integer, text, integer) IS
    'CAS-guarded monotonic watermark advance (rule 19). Returns TRUE when it '
    'moved, FALSE when the guard refused a regression or a re-advance. The '
    'guard is the UPDATE''s own WHERE clause — never a read-then-write in the '
    'application — mirroring _MARK_REGISTERED_SQL.';

-- ----------------------------------------------------------------------------
-- Grants
-- ----------------------------------------------------------------------------

-- Mirrors the stream's own posture: the pipeline write role reads and advances,
-- the read role reads. No table-level write grant on `association_sets` — sets
-- are registered by migration or by an operator procedure, never by a pipeline
-- job, because the set identity is immutable and a job that could mint one
-- could silently escape the ordering it is supposed to obey.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_pipeline_write')
    THEN
        GRANT SELECT ON association_sets TO rapid_pipeline_write;
        GRANT SELECT, UPDATE ON association_watermarks TO rapid_pipeline_write;
        GRANT EXECUTE ON FUNCTION derived.live_association_set()
            TO rapid_pipeline_write;
        GRANT EXECUTE ON FUNCTION derived.association_table_name(
            text, integer, integer) TO rapid_pipeline_write;
        GRANT EXECUTE ON FUNCTION derived.advance_association_watermark(
            integer, integer, text, integer) TO rapid_pipeline_write;
    END IF;

    -- `rapid_read`, not `rapid_pipeline_read`: the read role's name comes from
    -- 002-grants.sql, which is the authority. Guessing it would have been
    -- worse than a plain error here — the EXISTS guard would have skipped the
    -- grant silently and the posture test would have found a role with no
    -- access it was supposed to have.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read')
    THEN
        GRANT SELECT ON association_sets TO rapid_read;
        GRANT SELECT ON association_watermarks TO rapid_read;
        GRANT EXECUTE ON FUNCTION derived.live_association_set()
            TO rapid_read;
    END IF;
END;
$$;

COMMIT;
