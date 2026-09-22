--------------------------------------------------------------------------------------------------------------------------
-- 20260921-04-diffimmeta-source-counts.sql
--
-- Adds `diffimmeta.source_counts`, a nullable jsonb column holding the
-- difference-image manifest's per-catalog-type, per-sign source counts.
-- Authority: rapid_docs' products page
-- (https://roman-rapid.readthedocs.io/en/latest/system/products.html),
-- the difference-image registration field list -- "source counts per
-- catalog type and sign | manifest" -- and the complete manifest example,
-- whose `registration.source_counts` field is
-- `{"sextractor": {"positive": 412, "negative": 388}, "photutils":
-- {"positive": 405, "negative": 391}}`.
--
-- `diffimmeta` already has `nsexcatsources integer NOT NULL`, the
-- SExtractor positive-catalog source count (20260921-01-baseline.sql).
-- That column stays untouched, so nothing that already queries it
-- breaks. `source_counts` is separate because the field list calls for
-- the full per-catalog-type, per-sign structure the manifest carries,
-- which `nsexcatsources` alone cannot represent (it holds one count for
-- one catalog family and sign). Whether both catalog families
-- (SExtractor and Photutils) are retained in the rebuild is an open lead
-- decision (products page, "Not decided here"), so this column simply
-- stores whatever catalog-type keys the manifest lists, forcing no
-- answer to that question.
--
-- Nullable, like the run/attempt/instance columns in
-- 20260921-03-difference-run-columns.sql, for the same reason: legacy
-- rows written before this migration carry none of it. Deliberately a
-- separate file from that migration so this column, which encodes a
-- choice the products page marks as not yet settled, can be reverted on
-- its own without touching the run-model columns.
--
-- Recorded in LEDGER-difference-columns.md.
--------------------------------------------------------------------------------------------------------------------------

ALTER TABLE diffimmeta ADD COLUMN source_counts jsonb;

ALTER TABLE diffimmeta
    ADD CONSTRAINT diffimmeta_source_counts_is_object
    CHECK (source_counts IS NULL OR jsonb_typeof(source_counts) = 'object');

COMMENT ON COLUMN diffimmeta.source_counts IS
    'Per-catalog-type, per-sign source counts from the manifest '
    '(products page, difference-image registration field list and "A '
    'complete manifest"), e.g. {"sextractor": {"positive": 412, '
    '"negative": 388}, "photutils": {"positive": 405, "negative": 391}}. '
    'NULL for rows written before this column existed. nsexcatsources '
    'stays as the existing SExtractor-positive count for callers that '
    'already query it.';
