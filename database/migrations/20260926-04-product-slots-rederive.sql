--------------------------------------------------------------------------------------------------------------------------
-- 20260926-04-product-slots-rederive.sql
--
-- Re-derives every slot once under the corrected fill (supervisor step 5a, 2026-09-26, ruling
-- R22, from the Codex review of 20260926-03). -03's one-time correction cleared only slots
-- whose identity was NULL, so two kinds of row -02 could have left wrong survive it: the
-- shallow member of a duplicate-current pair that got its slot under -02's 32-pass bound
-- before its deeper peer resolved (a premature winner), and a promotion_changes.slot
-- backfilled by -02 that names a slot its instance no longer holds (whose rollback would
-- then look in the wrong slot). This file clears every product_instances.slot (identities
-- are not touched), runs product_identity_fill() once, which reproduces every slot from the
-- stored identities with the collision rule applied once over all current rows, so no
-- premature winner survives, and then recomputes promotion_changes.slot from the instances
-- it names: the slot every non-NULL instance of the change holds, or NULL when one of them
-- has none (or they differ), in which case rollback of that change uses its logical key.
--
-- Safe: wherever this repository's migrations persist today, -02, -03 and this file apply in
-- the same applier run (-02 was never applied alone anywhere persistent), so no promotion
-- has been made between them; both tables are locked SHARE ROW EXCLUSIVE for the file's
-- transaction, so no registration or promotion interleaves. Idempotent: a second apply
-- re-derives the same slots from the same identities. Additive: no object is created,
-- dropped or renamed, so no grant is needed. Nothing here raises on data.
--------------------------------------------------------------------------------------------------------------------------

LOCK TABLE product_instances, promotion_changes IN SHARE ROW EXCLUSIVE MODE;

-- Every slot goes; identities stay.
UPDATE product_instances SET slot = NULL WHERE slot IS NOT NULL;

-- Identities to their fixpoint (already there), then every slot at once.
SELECT * FROM product_identity_fill();

-- A recorded change keeps a slot only when every instance it names holds that same slot.
UPDATE promotion_changes pc
SET slot = CASE
        WHEN COALESCE(a.slot, b.slot) IS NOT NULL
         AND (pc.after_instance IS NULL OR a.slot IS NOT NULL)
         AND (pc.before_instance IS NULL OR b.slot IS NOT NULL)
         AND (a.slot IS NULL OR b.slot IS NULL OR a.slot = b.slot)
        THEN COALESCE(a.slot, b.slot)
    END
FROM promotion_changes x
LEFT JOIN product_instances a ON a.id = x.after_instance
LEFT JOIN product_instances b ON b.id = x.before_instance
WHERE pc.id = x.id;
