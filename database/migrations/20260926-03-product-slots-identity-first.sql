--------------------------------------------------------------------------------------------------------------------------
-- 20260926-03-product-slots-identity-first.sql
--
-- Corrects 20260926-02-product-slots.sql (supervisor step 5a, 2026-09-26, ruling R21, from
-- the Codex review of the merged change). Two defects in -02's functions, replaced here with
-- CREATE OR REPLACE (-02 itself is never edited):
--   - product_identity_derive() gave some rows a slot while their identity stayed NULL (an
--     association set whose source_sets or base is malformed got slot {field}), so an
--     unresolved row was selectable and promotable. Now every kind returns a slot only
--     together with its identity.
--   - product_identity_fill() stopped the identity phase after 32 passes and then filled
--     slots, so a chain deeper than 32 (33 or more loop dates of association sets) was
--     slotted before its identities were final and the first member resolved kept a slot a
--     duplicate should have withheld. Now the identity phase runs until a pass converts
--     nothing (a cycle stops at its first idle pass; 100000 passes is only a guard), and if
--     the guard trips the slot phase is skipped for that call and every row still NULL is
--     reported unresolved.
-- Then the only rows -02 could have mis-filled (a slot without an identity) lose their slot,
-- and the fill runs once. Additive: functions replaced with the same signatures, no column or
-- table dropped or renamed; idempotent (applies twice in a row without error); nothing raises
-- on data. Grants to rapid_rebuild_pipeline are guarded on the role's existence, as -02's are.
--------------------------------------------------------------------------------------------------------------------------

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

    -- A slot only with an identity (20260926-03, R21): a row whose identity cannot be
    -- derived (an association set with malformed source_sets or base, say) is unresolved,
    -- never selectable.
    IF identity IS NULL THEN
        slot := NULL;
    END IF;

    RETURN NEXT;
END
$fn$;

CREATE OR REPLACE FUNCTION product_identity_fill()
RETURNS TABLE (kind text, converted bigint, unresolved bigint, duplicate_current bigint)
LANGUAGE plpgsql AS $fn$
#variable_conflict use_column
DECLARE
    pass integer := 0;
    at_fixpoint boolean := false;
    changed bigint;
    acc jsonb := '{}'::jsonb;   -- kind -> rows given a slot by this call
    report jsonb;
BEGIN
    -- Identities first, to a fixpoint: until a pass converts nothing (R20, R21). A cycle
    -- converts nothing and stops at its first idle pass; 100000 passes is only a guard.
    -- An identity carries no uniqueness rule and is only ever written NULL -> value, so
    -- writing it pass by pass
    -- decides nothing; every slot depends only on the row's own key and its producers'
    -- identities (R18), so once identities are at their fixpoint each slot is derived in
    -- one pass, below.
    LOOP
        pass := pass + 1;
        UPDATE product_instances p
        SET identity = w.new_identity
        FROM (
            SELECT q.id, d.identity AS new_identity
            FROM product_instances q
            CROSS JOIN LATERAL product_identity_derive(q.kind, q.logical_key) d
            WHERE q.identity IS NULL
        ) w
        WHERE p.id = w.id AND p.identity IS NULL AND w.new_identity IS NOT NULL;
        GET DIAGNOSTICS changed = ROW_COUNT;
        IF changed = 0 THEN
            at_fixpoint := true;
            EXIT;
        END IF;
        EXIT WHEN pass >= 100000;
    END LOOP;

    -- Then every NULL slot at once: the collision rule is applied once over the filled
    -- currents and all prospective currents together, and the slots are written once. A
    -- current row whose (kind, slot) another current row holds or would take is withheld,
    -- every member of such a group alike (R12, R20); a filled slot is never rewritten.
    -- Skipped when the guard stopped the identity phase short of its fixpoint (R21).
    IF at_fixpoint THEN
        WITH work AS (
            SELECT p.id, p.kind, p.custody, d.slot AS new_slot
            FROM product_instances p
            CROSS JOIN LATERAL product_identity_derive(p.kind, p.logical_key) d
            WHERE p.slot IS NULL
        ),
        prospective AS (
            SELECT w.id, w.kind,
                   CASE WHEN w.new_slot IS NOT NULL AND NOT (
                            w.custody = 'current' AND (
                                EXISTS (SELECT 1 FROM product_instances c
                                        WHERE c.custody = 'current' AND c.kind = w.kind
                                          AND c.slot = w.new_slot AND c.id <> w.id)
                                OR EXISTS (SELECT 1 FROM work w2
                                           WHERE w2.custody = 'current'
                                             AND w2.kind = w.kind
                                             AND w2.new_slot = w.new_slot
                                             AND w2.id <> w.id)))
                        THEN w.new_slot END AS give_slot
            FROM work w
        ),
        upd AS (
            UPDATE product_instances p
            SET slot = pr.give_slot
            FROM prospective pr
            WHERE p.id = pr.id AND p.slot IS NULL AND pr.give_slot IS NOT NULL
            RETURNING pr.kind AS k
        )
        SELECT COALESCE(jsonb_object_agg(per_kind.k, per_kind.n), '{}'::jsonb)
        INTO acc
        FROM (SELECT upd.k, count(*) AS n FROM upd GROUP BY upd.k) per_kind;
    END IF;

    -- What is still NULL: a current row whose slot is derivable was withheld
    -- (duplicate_current); every other NULL slot is unresolved.
    WITH remaining AS (
        SELECT p.kind,
               count(*) FILTER (WHERE at_fixpoint AND d.slot IS NOT NULL
                                  AND p.custody = 'current') AS dup,
               count(*) FILTER (WHERE NOT (at_fixpoint AND d.slot IS NOT NULL
                                           AND p.custody = 'current')) AS unres
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

COMMENT ON FUNCTION product_identity_derive(text, jsonb) IS
    'The (slot, identity) of one product instance of the given kind from its logical_key '
    'and its producers'' filled identity (supervisor step 5a, rulings R3, R14, R16, R18, '
    'R21); NULL where a field is missing or malformed, a producer is missing, of the wrong '
    'kind or unresolved, or the kind is unknown. A slot is returned only with an identity.';

COMMENT ON FUNCTION product_identity_fill() IS
    'Fill identity on every product_instances row where it is NULL until a pass converts '
    'nothing (guard 100000 passes), then every NULL slot in one write, never rewriting a '
    'filled value and never giving a current row a (kind, slot) another current row holds '
    'or would take; if the guard trips, no slot is written and every NULL row is reported '
    'unresolved. Logs and returns one row per kind touched (supervisor step 5a, R12, R20, '
    'R21).';

-- The only rows -02 could have mis-filled: a slot without an identity.
UPDATE product_instances SET slot = NULL WHERE slot IS NOT NULL AND identity IS NULL;

SELECT * FROM product_identity_fill();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT EXECUTE ON FUNCTION product_identity_derive(text, jsonb) TO rapid_rebuild_pipeline;
        GRANT EXECUTE ON FUNCTION product_identity_fill() TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
