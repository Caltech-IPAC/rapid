--------------------------------------------------------------------------------------------------------------------------
-- 20260926-02-product-slots.sql
--
-- Supersession by slot (supervisor step 5a, 2026-09-26, rulings R1-R3 and R12-R16). Every
-- product instance gains two derived columns beside its `logical_key` (renamed on the
-- products page the provenance key: it still names the exact input instances consumed and
-- is not changed here):
--   - `identity`: the product's logical identity, built only from delivered facts and
--     science choices, never from an instance id (operations.md, "Replacement scope");
--   - `slot`: the part of the identity a consumer selects on. At most one instance is
--     current per (kind, slot), enforced by the new partial unique index
--     `product_instances_current_slot_uq`, and promotion replaces by slot.
-- Both are derived by the database, never by the stage that wrote the manifest:
-- `product_identity_derive(kind, logical_key)` computes one row's pair from its provenance
-- key and its producers' already-filled identity (the derivation table of ruling R3, with
-- R14's hash chain for association sets; a producer's slot is read from its identity, R18), and `product_identity_fill()` fills
-- every row whose slot or identity is NULL, pass by pass (at most 32), withholding the slot
-- of any current row whose (kind, slot) another current row holds or would take in the same
-- pass (R12: an ambiguous conversion is refused, left NULL and counted, never raised). The
-- fill logs one row per kind it touched in `slot_backfill_log` and returns the same rows.
-- This file calls it once (the backfill of every existing row), then backfills
-- `promotion_changes.slot`, then installs the unique index.
--
-- Additive: it adds columns, a table, functions and indexes; it drops, renames or rewrites
-- nothing a live release reads. The old index `product_instances_current_key_uq` on
-- (kind, logical_key) stays. An image of an earlier release registers exactly as before
-- (it never writes the new columns; registration and promotion fill them). Every statement
-- is idempotent (IF NOT EXISTS, OR REPLACE, fills that touch only NULLs), so the file applies
-- twice in a row without error, and nothing in it raises on data. Grants to
-- `rapid_rebuild_pipeline` are guarded on the role's existence, as 20260924-11 is.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE product_instances ADD COLUMN IF NOT EXISTS slot jsonb;
ALTER TABLE product_instances ADD COLUMN IF NOT EXISTS identity jsonb;
ALTER TABLE promotion_changes ADD COLUMN IF NOT EXISTS slot jsonb;

COMMENT ON COLUMN product_instances.logical_key IS
    'The provenance key: what the producing stage wrote in its manifest entry, naming the '
    'exact input instances consumed (products page, "Identity").';
COMMENT ON COLUMN product_instances.identity IS
    'The logical identity, derived by product_identity_fill() from logical_key and the '
    'producers'' identities: delivered facts and science choices, never an instance id. '
    'NULL while unresolved.';
COMMENT ON COLUMN product_instances.slot IS
    'The selector: the part of identity a consumer selects on. At most one current '
    'instance per (kind, slot); promotion replaces by slot. NULL while unresolved, or '
    'withheld from a current row whose (kind, slot) another current row holds.';
COMMENT ON COLUMN promotion_changes.slot IS
    'The slot the change replaced in; NULL for a change selected by logical_key (a '
    'promotion recorded before 20260926-02 whose instances stayed unresolved).';

CREATE TABLE IF NOT EXISTS slot_backfill_log (
    filled_at          timestamptz NOT NULL DEFAULT now(),
    kind               text NOT NULL,
    converted          bigint NOT NULL,
    unresolved         bigint NOT NULL,
    duplicate_current  bigint NOT NULL
);

COMMENT ON TABLE slot_backfill_log IS
    'One row per kind per product_identity_fill() call that converted a row or left one '
    'NULL: converted = rows given a slot by the call; unresolved = rows whose slot could '
    'not be derived (a missing or malformed field or producer, an unknown kind, or the '
    'pass bound); duplicate_current = current rows whose derivable slot was withheld '
    'because another current row holds or would take it (supervisor step 5a, 2026-09-26).';

CREATE INDEX IF NOT EXISTS product_instances_slot_null_idx
    ON product_instances (kind) WHERE slot IS NULL OR identity IS NULL;

-- ======================================================================
-- The derivation (ruling R3, amended by R14 and R16)
-- ======================================================================

-- A JSON scalar a slot or identity may carry: a string or a number, else NULL.
CREATE OR REPLACE FUNCTION product_identity_scalar(v jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE WHEN jsonb_typeof(v) IN ('string', 'number') THEN v END
$fn$;

-- A reference to a producer instance: a JSON string, as text, else NULL.
CREATE OR REPLACE FUNCTION product_identity_ref(v jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE WHEN jsonb_typeof(v) = 'string' THEN v #>> '{}' END
$fn$;

-- The subset of an identity object holding exactly ``fields``; NULL unless all are present.
CREATE OR REPLACE FUNCTION product_identity_pick(i jsonb, fields text[]) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE WHEN jsonb_typeof(i) = 'object' AND i ?& fields
                THEN (SELECT jsonb_object_agg(f, i -> f) FROM unnest(fields) AS f) END
$fn$;

-- The slot of a product of ``p_kind`` read from its identity (ruling R18): every slot is
-- a subset of its kind's identity fields, and identity is stored even when a duplicate
-- current row's slot is withheld, so a consumer derives from its producer's identity and
-- a descendant of a withheld producer is classified on its own.
CREATE OR REPLACE FUNCTION product_identity_slot(p_kind text, i jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    m jsonb;
BEGIN
    RETURN CASE p_kind
        WHEN 'l2-image' THEN product_identity_pick(i, ARRAY['detector', 'exposure'])
        WHEN 'psf' THEN product_identity_pick(i, ARRAY['detector', 'filter'])
        WHEN 'reference-image' THEN product_identity_pick(i, ARRAY['field', 'filter'])
        WHEN 'reference-catalog' THEN
            product_identity_pick(i, ARRAY['catalog_type', 'field', 'filter'])
        WHEN 'difference-image' THEN
            product_identity_pick(i, ARRAY['detector', 'differencer', 'exposure'])
        WHEN 'source-catalog' THEN product_identity_pick(
            i, ARRAY['catalog_type', 'detector', 'differencer', 'exposure', 'sign'])
        WHEN 'source-set' THEN product_identity_pick(
            i, ARRAY['catalog_type', 'detector', 'differencer', 'exposure'])
        WHEN 'alert-container' THEN
            product_identity_pick(i, ARRAY['detector', 'differencer', 'exposure'])
        WHEN 'alert-set' THEN
            product_identity_pick(i, ARRAY['detector', 'differencer', 'exposure'])
        WHEN 'association-set' THEN product_identity_pick(i, ARRAY['field'])
        WHEN 'pruned-set' THEN product_identity_pick(i -> 'association', ARRAY['field'])
        WHEN 'statistics-set' THEN
            product_identity_slot(i ->> 'membership_kind', i -> 'membership')
            || product_identity_pick(i, ARRAY['membership_kind'])
        WHEN 'light-curve' THEN product_identity_pick(i, ARRAY['field', 'request_id'])
        WHEN 'catalog-export' THEN product_identity_pick(i, ARRAY['export_type', 'field'])
    END;
END
$fn$;

CREATE OR REPLACE FUNCTION product_identity_derive(p_kind text, k jsonb)
RETURNS TABLE (slot jsonb, identity jsonb)
LANGUAGE plpgsql STABLE AS $fn$
#variable_conflict use_column
DECLARE
    a jsonb; b jsonb; c jsonb; d jsonb;
    pk text; ps jsonb; pv jsonb;          -- a producer's kind, slot, identity
    rk text; rv jsonb;                    -- a second producer's kind, identity
    elem jsonb; vs jsonb[] := '{}'; ok boolean := true;
BEGIN
    slot := NULL;
    identity := NULL;
    IF k IS NULL OR jsonb_typeof(k) <> 'object' THEN
        RETURN NEXT;
        RETURN;
    END IF;

    CASE p_kind
    WHEN 'l2-image' THEN
        a := product_identity_scalar(k -> 'exposure');
        b := product_identity_scalar(k -> 'detector');
        c := product_identity_scalar(k -> 'version');
        IF a IS NOT NULL AND b IS NOT NULL THEN
            slot := jsonb_build_object('detector', b, 'exposure', a);
            IF c IS NOT NULL THEN
                identity := slot || jsonb_build_object('version', c);
            END IF;
        END IF;

    WHEN 'psf' THEN
        a := product_identity_scalar(k -> 'filter');
        b := product_identity_scalar(k -> 'detector');
        c := product_identity_scalar(k -> 'version');
        IF a IS NOT NULL AND b IS NOT NULL THEN
            slot := jsonb_build_object('detector', b, 'filter', a);
            IF c IS NOT NULL THEN
                identity := slot || jsonb_build_object('version', c);
            END IF;
        END IF;

    WHEN 'reference-image' THEN
        a := product_identity_scalar(k -> 'field');
        b := product_identity_scalar(k -> 'filter');
        c := product_identity_scalar(k -> 'recipe');
        d := product_identity_scalar(k -> 'version');
        IF a IS NOT NULL AND b IS NOT NULL THEN
            slot := jsonb_build_object('field', a, 'filter', b);
            IF c IS NOT NULL AND d IS NOT NULL THEN
                identity := slot || jsonb_build_object('recipe', c, 'version', d);
            END IF;
        END IF;

    WHEN 'reference-catalog' THEN
        a := product_identity_scalar(k -> 'catalog_type');
        SELECT pi.kind, product_identity_slot(pi.kind, pi.identity), pi.identity
        INTO pk, ps, pv
        FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'reference');
        IF a IS NOT NULL AND pk = 'reference-image' THEN
            IF ps IS NOT NULL THEN
                slot := ps || jsonb_build_object('catalog_type', a);
            END IF;
            IF pv IS NOT NULL THEN
                identity := pv || jsonb_build_object('catalog_type', a);
            END IF;
        END IF;

    WHEN 'difference-image' THEN
        a := product_identity_scalar(k -> 'differencer');
        b := product_identity_scalar(k -> 'settings_hash');
        SELECT pi.kind, product_identity_slot(pi.kind, pi.identity), pi.identity
        INTO pk, ps, pv
        FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'l2');
        SELECT pi.kind, pi.identity INTO rk, rv
        FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'reference');
        IF a IS NOT NULL AND pk = 'l2-image' THEN
            IF ps IS NOT NULL THEN
                slot := ps || jsonb_build_object('differencer', a);
            END IF;
            IF pv IS NOT NULL AND b IS NOT NULL AND rk = 'reference-image' AND rv IS NOT NULL THEN
                identity := pv || jsonb_build_object(
                    'differencer', a, 'reference', rv, 'settings_hash', b);
            END IF;
        END IF;

    WHEN 'source-catalog', 'source-set', 'alert-container', 'alert-set' THEN
        IF p_kind = 'source-catalog' THEN
            a := product_identity_scalar(k -> 'catalog_type');
            b := product_identity_scalar(k -> 'sign');
            ok := a IS NOT NULL AND b IS NOT NULL;
            c := jsonb_build_object('catalog_type', a, 'sign', b);
            d := c;
        ELSIF p_kind = 'source-set' THEN
            a := product_identity_scalar(k -> 'catalog_type');
            ok := a IS NOT NULL;
            c := jsonb_build_object('catalog_type', a);
            d := c;
        ELSE
            -- alert-container and alert-set share the slot of their difference image
            -- (R3: one alert set per difference); identity adds schema_version.
            a := product_identity_scalar(k -> 'schema_version');
            ok := a IS NOT NULL;
            c := '{}'::jsonb;
            d := jsonb_build_object('schema_version', a);
        END IF;
        SELECT pi.kind, product_identity_slot(pi.kind, pi.identity), pi.identity
        INTO pk, ps, pv
        FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'difference');
        IF ok AND pk = 'difference-image' THEN
            IF ps IS NOT NULL THEN
                slot := ps || c;
            END IF;
            IF pv IS NOT NULL THEN
                identity := pv || d;
            END IF;
        END IF;

    WHEN 'association-set' THEN
        a := product_identity_scalar(k -> 'field');
        b := product_identity_scalar(k -> 'settings_hash');
        IF a IS NOT NULL THEN
            slot := jsonb_build_object('field', a);
        END IF;
        -- identity: {base, field, settings_hash, source_sets}; base is the sha256 of the
        -- base association's identity text (R14), JSON null for a set with no base.
        IF b IS NOT NULL AND k ? 'base' AND jsonb_typeof(k -> 'source_sets') = 'array' THEN
            FOR elem IN SELECT e FROM jsonb_array_elements(k -> 'source_sets') AS e LOOP
                pk := NULL;
                pv := NULL;
                SELECT pi.kind, pi.identity INTO pk, pv
                FROM product_instances pi WHERE pi.id = product_identity_ref(elem);
                IF pk IS DISTINCT FROM 'source-set' OR pv IS NULL THEN
                    ok := false;
                    EXIT;
                END IF;
                vs := vs || pv;
            END LOOP;
            IF jsonb_typeof(k -> 'base') = 'null' THEN
                c := 'null'::jsonb;
            ELSE
                SELECT pi.kind, pi.identity INTO rk, rv
                FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'base');
                IF rk = 'association-set' AND rv IS NOT NULL THEN
                    c := to_jsonb(encode(sha256(convert_to(rv::text, 'UTF8')), 'hex'));
                ELSE
                    ok := false;
                END IF;
            END IF;
            IF ok AND a IS NOT NULL THEN
                identity := jsonb_build_object(
                    'base', c, 'field', a, 'settings_hash', b,
                    'source_sets', COALESCE(
                        (SELECT jsonb_agg(v ORDER BY v::text) FROM unnest(vs) AS v),
                        '[]'::jsonb));
            END IF;
        END IF;

    WHEN 'pruned-set' THEN
        a := product_identity_scalar(k -> 'settings_hash');
        SELECT pi.kind, product_identity_slot(pi.kind, pi.identity), pi.identity
        INTO pk, ps, pv
        FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'base');
        IF pk = 'association-set' THEN
            slot := ps;
            IF pv IS NOT NULL AND a IS NOT NULL THEN
                identity := jsonb_build_object('association', pv, 'settings_hash', a);
            END IF;
        END IF;

    WHEN 'statistics-set' THEN
        SELECT pi.kind, product_identity_slot(pi.kind, pi.identity), pi.identity
        INTO pk, ps, pv
        FROM product_instances pi WHERE pi.id = product_identity_ref(k -> 'membership');
        IF pk IS NOT NULL THEN
            IF ps IS NOT NULL THEN
                slot := ps || jsonb_build_object('membership_kind', pk);
            END IF;
            IF pv IS NOT NULL THEN
                identity := jsonb_build_object('membership', pv, 'membership_kind', pk);
            END IF;
        END IF;

    WHEN 'light-curve' THEN
        -- Declared, no producer in this port: slot {field, request_id} when both are
        -- present; identity adds nothing (open, R3).
        a := product_identity_scalar(k -> 'field');
        b := product_identity_scalar(k -> 'request_id');
        IF a IS NOT NULL AND b IS NOT NULL THEN
            slot := jsonb_build_object('field', a, 'request_id', b);
            identity := slot;
        END IF;

    WHEN 'catalog-export' THEN
        a := product_identity_scalar(k -> 'field');
        b := product_identity_scalar(k -> 'export_type');
        IF a IS NOT NULL AND b IS NOT NULL THEN
            slot := jsonb_build_object('export_type', b, 'field', a);
            identity := k;
        END IF;

    ELSE
        -- An unknown kind: unresolved.
        NULL;
    END CASE;

    RETURN NEXT;
END
$fn$;

COMMENT ON FUNCTION product_identity_derive(text, jsonb) IS
    'The (slot, identity) of one product instance of the given kind from its logical_key '
    'and its producers'' filled identity (supervisor step 5a, rulings R3, R14, R16, R18); NULL where a field is missing or malformed, a producer is missing, of the wrong '
    'kind or unresolved, or the kind is unknown.';

-- ======================================================================
-- The fill (ruling R12)
-- ======================================================================

CREATE OR REPLACE FUNCTION product_identity_fill()
RETURNS TABLE (kind text, converted bigint, unresolved bigint, duplicate_current bigint)
LANGUAGE plpgsql AS $fn$
#variable_conflict use_column
DECLARE
    pass integer := 0;
    changed bigint;
    gave jsonb;                 -- kind -> rows given a slot by one pass
    acc jsonb := '{}'::jsonb;   -- kind -> rows given a slot by this call
    report jsonb;
    e record;
BEGIN
    LOOP
        pass := pass + 1;
        WITH work AS (
            SELECT p.id, p.kind, p.custody, p.slot AS old_slot, d.slot AS new_slot,
                   d.identity AS new_identity
            FROM product_instances p
            CROSS JOIN LATERAL product_identity_derive(p.kind, p.logical_key) d
            WHERE p.slot IS NULL OR p.identity IS NULL
        ),
        prospective AS (
            -- A current row's derived slot is withheld when another current row holds
            -- it or would take it in this pass (R12).
            SELECT w.id, w.kind, w.new_identity,
                   CASE WHEN w.old_slot IS NULL AND w.new_slot IS NOT NULL AND NOT (
                            w.custody = 'current' AND (
                                EXISTS (SELECT 1 FROM product_instances c
                                        WHERE c.custody = 'current' AND c.kind = w.kind
                                          AND c.slot = w.new_slot AND c.id <> w.id)
                                OR EXISTS (SELECT 1 FROM work w2
                                           WHERE w2.custody = 'current'
                                             AND w2.old_slot IS NULL
                                             AND w2.kind = w.kind
                                             AND w2.new_slot = w.new_slot
                                             AND w2.id <> w.id)))
                        THEN w.new_slot END AS give_slot
            FROM work w
        ),
        upd AS (
            UPDATE product_instances p
            SET slot = COALESCE(p.slot, pr.give_slot),
                identity = COALESCE(p.identity, pr.new_identity)
            FROM prospective pr
            WHERE p.id = pr.id
              AND ((p.slot IS NULL AND pr.give_slot IS NOT NULL)
                   OR (p.identity IS NULL AND pr.new_identity IS NOT NULL))
            RETURNING pr.kind AS k, pr.give_slot IS NOT NULL AS gave_slot
        ),
        per_kind AS (
            SELECT upd.k, count(*) AS n, count(*) FILTER (WHERE upd.gave_slot) AS g
            FROM upd GROUP BY upd.k
        )
        SELECT COALESCE(sum(per_kind.n), 0),
               COALESCE(jsonb_object_agg(per_kind.k, per_kind.g)
                        FILTER (WHERE per_kind.g > 0), '{}'::jsonb)
        INTO changed, gave
        FROM per_kind;

        FOR e IN SELECT j.key, j.value::bigint AS n FROM jsonb_each_text(gave) j LOOP
            acc := acc || jsonb_build_object(e.key, COALESCE((acc ->> e.key)::bigint, 0) + e.n);
        END LOOP;
        EXIT WHEN changed = 0 OR pass >= 32;
    END LOOP;

    -- What is still NULL: a current row whose slot is derivable was withheld
    -- (duplicate_current); every other NULL slot is unresolved.
    WITH remaining AS (
        SELECT p.kind,
               count(*) FILTER (WHERE d.slot IS NOT NULL AND p.custody = 'current') AS dup,
               count(*) FILTER (WHERE NOT (d.slot IS NOT NULL AND p.custody = 'current'))
                   AS unres
        FROM product_instances p
        CROSS JOIN LATERAL product_identity_derive(p.kind, p.logical_key) d
        WHERE p.slot IS NULL
        GROUP BY p.kind
    ),
    converted_by_kind AS (
        SELECT j.key AS kind, j.value::bigint AS conv FROM jsonb_each_text(acc) j
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'kind', COALESCE(cv.kind, rm.kind),
               'converted', COALESCE(cv.conv, 0),
               'unresolved', COALESCE(rm.unres, 0),
               'duplicate_current', COALESCE(rm.dup, 0))
               ORDER BY COALESCE(cv.kind, rm.kind)), '[]'::jsonb)
    INTO report
    FROM converted_by_kind cv FULL JOIN remaining rm ON rm.kind = cv.kind;

    INSERT INTO slot_backfill_log (kind, converted, unresolved, duplicate_current)
    SELECT x.kind, x.converted, x.unresolved, x.duplicate_current
    FROM jsonb_to_recordset(report)
         AS x(kind text, converted bigint, unresolved bigint, duplicate_current bigint);

    RETURN QUERY
    SELECT x.kind, x.converted, x.unresolved, x.duplicate_current
    FROM jsonb_to_recordset(report)
         AS x(kind text, converted bigint, unresolved bigint, duplicate_current bigint)
    ORDER BY x.kind;
END
$fn$;

COMMENT ON FUNCTION product_identity_fill() IS
    'Fill slot and identity on every product_instances row where either is NULL, at most '
    '32 passes, never rewriting a filled value and never giving a current row a (kind, '
    'slot) another current row holds or would take; log and return one row per kind '
    'touched (supervisor step 5a, 2026-09-26, R12).';

-- ======================================================================
-- The backfill, then the index
-- ======================================================================

SELECT * FROM product_identity_fill();

-- A recorded change keeps a slot only when every instance it names has that same slot;
-- otherwise it stays NULL and rolls back by logical_key (R9).
UPDATE promotion_changes pc
SET slot = COALESCE(a.slot, b.slot)
FROM promotion_changes x
LEFT JOIN product_instances a ON a.id = x.after_instance
LEFT JOIN product_instances b ON b.id = x.before_instance
WHERE pc.id = x.id
  AND pc.slot IS NULL
  AND COALESCE(a.slot, b.slot) IS NOT NULL
  AND (x.after_instance IS NULL OR a.slot IS NOT NULL)
  AND (x.before_instance IS NULL OR b.slot IS NOT NULL)
  AND (a.slot IS NULL OR b.slot IS NULL OR a.slot = b.slot);

CREATE UNIQUE INDEX IF NOT EXISTS product_instances_current_slot_uq
    ON product_instances (kind, slot)
    WHERE custody = 'current' AND slot IS NOT NULL;

CREATE INDEX IF NOT EXISTS promotion_changes_kind_slot_idx
    ON promotion_changes (kind, slot);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT ON slot_backfill_log TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_scalar(jsonb) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_ref(jsonb) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_pick(jsonb, text[]) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_slot(text, jsonb) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_derive(text, jsonb) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_fill() TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
