-- 048-products-and-artifacts.sql — the product/artifact distinction rule 10
-- requires: one canonical product per deterministic identity, attempt-scoped
-- artifact rows for the bytes, and an explicit binding between them.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the schema
-- and this file is not applied by `apply-db-migrations.sh` until its owner
-- adopts it. See that directory's README. Requires 006 (refimages, diffimages)
-- and 011 (attempts) — apply in order.
--
-- Conformance rule 10, verbatim:
--
--     "Products and artifacts are distinct records. Scientific identity is a
--      deterministic digest of process specification, canonical subject,
--      ordered inputs and role — never a path, Batch ID or array index."
--
-- and the governing "database uniqueness" principle, which is why the product
-- key is UNIQUE here and not merely computed in Python: an identity the
-- application promises and the database does not enforce is an identity that
-- holds until the first concurrent writer.
--
-- ============================================================================
-- WHAT WAS WRONG, AND WHAT THESE THREE TABLES SEPARATE
-- ============================================================================
--
-- Before this file the schema had no artifact concept at all. A published file
-- reached the database as columns ON the product row — `refimages.filename`
-- and `refimages.checksum`, `diffimages.filename` and `diffimages.checksum` —
-- so "these bytes exist, are durable and are checksummed" and "this is the
-- scientific product with this identity" were the same row. Two consequences
-- followed, and both are defects rule 10 names:
--
--   (a) A re-attempt at the same science had to mint a NEW product row
--       (`version = max(version) + 1`) purely to record a new set of bytes,
--       because the bytes were on the product. So the operations tables count
--       executions, not products, and the same science reprocessed under a new
--       release is indistinguishable from a genuinely different product.
--
--   (b) Uniqueness rested on the S3 path, which embeds `run_id` and
--       `attempt_id` (`pipeline/stages/context.py:130-165`). That is identity
--       by execution accident: it guarantees two attempts never collide, and
--       guarantees nothing whatever about two attempts at the SAME science
--       being recognisably the same product.
--
-- The three tables below separate the two notions:
--
--   `products`         — ONE row per deterministic identity. The product key
--                        is the digest; the row is the canonical record of
--                        "this scientific product exists". Never attempt-
--                        scoped, never versioned by execution.
--   `artifacts`        — one row per published file per attempt. Attempt-
--                        scoped by design: a re-attempt produces new artifact
--                        rows even for byte-identical outputs, because they
--                        ARE different bytes-at-a-location events, and the
--                        checksum is what says whether the content matched.
--   `product_artifacts`— which artifact currently realizes which product, and
--                        which legacy row that binding corresponds to. This is
--                        where retry semantics live: the binding moves, the
--                        product does not.
--
-- ============================================================================
-- WHAT THIS FILE DOES NOT DO
-- ============================================================================
--
-- It does not migrate a single reader. `refimages` and `diffimages` keep every
-- column they have, populated exactly as today — `filename`, `checksum`,
-- `version`, `vbest` — because the production reader set is broader than the
-- registration writers: reference selection (`get_best_reference_image`),
-- post-DB gathering over `diffimages.filename`, forced photometry's URI+
-- checksum join, alert production's companion-file directory anchoring, the
-- `pid`/`vbest` currency sweeps, and catalog-load's sibling-catalogue
-- derivation. Those keep working unchanged; migrating them is later work.
-- The legacy rows gain a FK to the product, so they BIND to the identity
-- without BEING it.
--
-- It also does not fix `refimages.checksum` / `diffimages.checksum` being
-- `character varying(32)` (006-core-tables.sql:393,448). A SHA-256 is 64 hex
-- characters, so those columns truncate every checksum they are given and have
-- done so since the schema was written — a latent defect flagged by brief D
-- and left for its own change request, because widening a live column is a
-- separate decision from adding these tables. `artifacts.checksum` below
-- simply does it right: 64 characters, with the algorithm recorded beside it.

BEGIN;

-- ============================================================================
-- products — one row per deterministic identity
-- ============================================================================
--
-- `product_key` is `sha256:<64 hex>`, computed by
-- `pipeline/registration/identity.py` over the canonical serialization of
-- process specification + canonical subject + ordered inputs + role. The
-- algorithm prefix is part of the stored value so a row says how its identity
-- was computed rather than leaving it to be inferred from the length.
--
-- THE UNIQUE CONSTRAINT IS THE POINT. `product_key` is the natural key; the
-- surrogate `product_id` exists only because a 71-character text FK repeated
-- across binding rows is a poor physical key, and it is deliberately NOT the
-- identity — nothing computes it, nothing reproduces it, and no consumer may
-- key off it across databases.
CREATE TABLE IF NOT EXISTS products (
    product_id      bigint GENERATED BY DEFAULT AS IDENTITY,
    product_key     text NOT NULL,
    product_class   text NOT NULL,
    role            text NOT NULL,
    -- The four components, stored as the canonical payload that was hashed.
    -- Kept whole rather than decomposed into columns because it IS the
    -- serialization the digest was taken over: a decomposition could drift
    -- from what was hashed, and then the row would assert an identity nobody
    -- could recompute. Anything wanting to query a component reads it out of
    -- the JSON, which is exact by construction.
    identity_payload jsonb NOT NULL,
    -- The serialization version, lifted out of the payload so a future
    -- canonical-form change can be found without scanning JSON.
    serialization_version integer NOT NULL,
    -- The process family (`ppid`), lifted out for the same reason: the
    -- operations tables are partitioned and indexed by it and an operator
    -- asking "which products did pipeline 15 make" should not need a JSON
    -- path. Redundant with the payload BY DESIGN, and the CHECK below makes
    -- the redundancy non-divergent.
    process_family  smallint NOT NULL,
    first_seen      timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT products_pkey PRIMARY KEY (product_id),
    -- Rule 10's identity, enforced by the database rather than promised by
    -- the application.
    CONSTRAINT products_product_key_uq UNIQUE (product_key),
    CONSTRAINT products_product_key_shape_ck
        CHECK (product_key ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT products_class_ck
        CHECK (product_class IN ('difference_image', 'reference_image')),
    -- The lifted columns must agree with the payload they were lifted from.
    CONSTRAINT products_payload_agrees_ck CHECK (
        (identity_payload ->> 'product_class') = product_class
        AND (identity_payload ->> 'role') = role
        AND (identity_payload ->> 'serialization_version')::integer
            = serialization_version
        AND (identity_payload -> 'process_specification' ->> 'process_family')::integer
            = process_family
    ),
    CONSTRAINT products_ppid_fk FOREIGN KEY (process_family)
        REFERENCES pipelines(ppid)
);

CREATE INDEX IF NOT EXISTS products_class_idx
    ON products USING btree (product_class);
CREATE INDEX IF NOT EXISTS products_process_family_idx
    ON products USING btree (process_family);
CREATE INDEX IF NOT EXISTS products_first_seen_idx
    ON products USING btree (first_seen);

COMMENT ON TABLE products IS
    'One canonical row per deterministic product identity (rule 10). The '
    'product key is a sha256 digest over process specification, canonical '
    'subject, ordered inputs and role — never a path, Batch id or array '
    'index. Not attempt-scoped: a retry or a reprocessing that agrees on all '
    'four components resolves to THIS row.';
COMMENT ON COLUMN products.product_key IS
    'sha256:<64 hex> over the canonical serialization in identity_payload. '
    'The natural key; UNIQUE-constrained because an identity the application '
    'promises and the database does not enforce holds only until the first '
    'concurrent writer.';
COMMENT ON COLUMN products.identity_payload IS
    'The exact canonical object the digest was taken over, stored whole so '
    'the identity remains recomputable. Contains no URI, path, filename, '
    'surrogate id, run/attempt/Batch identifier or array index.';
COMMENT ON COLUMN products.product_id IS
    'A physical surrogate for FK use only. NOT the identity: nothing '
    'reproduces it and no consumer may key off it across databases.';

-- ============================================================================
-- artifacts — one row per published file per attempt
-- ============================================================================
--
-- ATTEMPT-SCOPED BY DESIGN. A re-attempt produces new artifact rows even when
-- the bytes are identical, because an artifact records a publication event —
-- these bytes, at this address, produced by this attempt — and two attempts
-- publishing identical bytes are two such events. What must NOT produce new
-- rows is a REPLAY of the same attempt at the same record sequence, and that
-- is what the unique constraint below enforces: the registration consumer may
-- re-run a candidate at the same `(attempt_id, record_sequence)` any number of
-- times and the second run inserts nothing.
--
-- The URI is here, and only here. Rule 10 forbids paths as IDENTITY, not as
-- addresses for bytes — after this file the S3 key (which embeds run and
-- attempt) is load-bearing only as an artifact's storage address, which is
-- exactly what it is good for.
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id     bigint GENERATED BY DEFAULT AS IDENTITY,
    attempt_id      bigint NOT NULL,
    -- The reconciler's closure-record sequence for this attempt, the same
    -- number the registration watermark advances to. Half of the replay key.
    record_sequence integer NOT NULL,
    -- The name the publishing stage published under (`sfft_diffimage`,
    -- `reference_sexcat`, ...). The third component of the replay key: one
    -- attempt publishes several files at one record sequence.
    published_name  text NOT NULL,
    uri             text NOT NULL,
    -- THE CHECKSUM, DONE RIGHT. `character varying(32)` on the legacy product
    -- tables truncates a SHA-256 to its first 32 characters, which is a
    -- collision domain no one chose and a comparison that silently succeeds
    -- against the wrong bytes. 64 characters, lower-case hex, CHECK-enforced
    -- against the recorded algorithm.
    checksum_algorithm text NOT NULL DEFAULT 'sha256',
    checksum        text NOT NULL,
    size_bytes      bigint,
    -- What KIND of bytes: the publishing stage's `product_type`, e.g.
    -- 'difference_image', 'catalog'. Distinct from the product's `role`,
    -- which is a contract name; an artifact may have a content type and no
    -- product at all (the unselected ZOGY and naive variants).
    content_type    text,
    -- BUILD PROVENANCE LIVES HERE, NOT IN THE PRODUCT KEY. The image digest
    -- and source revision identify the build that produced these bytes. They
    -- are deliberately excluded from product identity — the key tracks the
    -- specified science process, not the build that executed it — and they
    -- are recorded here so nothing is lost by that exclusion.
    image_digest    text,
    source_revision text,
    created         timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT artifacts_pkey PRIMARY KEY (artifact_id),
    -- REPLAY-UNIQUENESS, DATABASE-ENFORCED. Brief D: "Replay of the same
    -- (attempt_id, record_sequence) produces none." Enforced here rather than
    -- by a Python find-or-insert, so a concurrent second registrar cannot
    -- interleave between the SELECT and the INSERT.
    CONSTRAINT artifacts_replay_uq
        UNIQUE (attempt_id, record_sequence, published_name),
    CONSTRAINT artifacts_checksum_ck CHECK (
        (checksum_algorithm = 'sha256' AND checksum ~ '^[0-9a-f]{64}$')
        OR (checksum_algorithm <> 'sha256' AND length(checksum) > 0)
    ),
    CONSTRAINT artifacts_size_ck CHECK (size_bytes IS NULL OR size_bytes >= 0),
    CONSTRAINT artifacts_attempt_fk FOREIGN KEY (attempt_id)
        REFERENCES attempts(attempt_id)
);

CREATE INDEX IF NOT EXISTS artifacts_attempt_idx
    ON artifacts USING btree (attempt_id);
CREATE INDEX IF NOT EXISTS artifacts_created_idx
    ON artifacts USING btree (created);
CREATE INDEX IF NOT EXISTS artifacts_checksum_idx
    ON artifacts USING btree (checksum);

COMMENT ON TABLE artifacts IS
    'One durable record of bytes per published file per attempt (rule 10). '
    'Attempt-scoped: a re-attempt produces new rows even for byte-identical '
    'outputs, while a replay of the same (attempt_id, record_sequence) '
    'produces none — enforced by artifacts_replay_uq, not by application '
    'convention.';
COMMENT ON COLUMN artifacts.checksum IS
    'The FULL digest: 64 lower-case hex characters for sha256. The legacy '
    'refimages.checksum / diffimages.checksum are varchar(32) and truncate '
    'exactly this value — a latent defect this column deliberately does not '
    'reproduce.';
COMMENT ON COLUMN artifacts.uri IS
    'The storage address of these bytes. An address, never an identity: rule '
    '10 forbids a path as product identity, which is what products.product_key '
    'now carries.';
COMMENT ON COLUMN artifacts.image_digest IS
    'The container image that produced these bytes. Build provenance belongs '
    'on the artifact, not in the product key: two builds of the same reviewed '
    'science specification are the same product.';

-- ============================================================================
-- product_artifacts — which artifact currently realizes which product
-- ============================================================================
--
-- The explicit association rule 10's cardinality requires, carrying the
-- CURRENT binding. On a retry the product row is unchanged, new artifact rows
-- are written, and the binding is repointed — which is how `vbest` semantics
-- survive: the legacy row that is `vbest = 1` is the one this binding names.
--
-- `is_current` is a partial-unique-indexed flag rather than a "latest wins"
-- convention, because "which artifact realizes this product right now" is a
-- question with exactly one answer and the database should be the thing that
-- says so. The partial index permits any number of superseded bindings and
-- exactly one current binding per (product, published name).
CREATE TABLE IF NOT EXISTS product_artifacts (
    product_artifact_id bigint GENERATED BY DEFAULT AS IDENTITY,
    product_id      bigint NOT NULL,
    artifact_id     bigint NOT NULL,
    -- Which legacy row this binding corresponds to, so today's consumers and
    -- the new identity model name the same object. Exactly one of the two
    -- pairs is set, per the CHECK: a reference image binds (rfid, version), a
    -- difference image binds (pid, version).
    legacy_rfid     integer,
    legacy_pid      integer,
    legacy_version  smallint,
    is_current      boolean DEFAULT true NOT NULL,
    bound_at        timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT product_artifacts_pkey PRIMARY KEY (product_artifact_id),
    CONSTRAINT product_artifacts_product_fk FOREIGN KEY (product_id)
        REFERENCES products(product_id) ON DELETE CASCADE,
    CONSTRAINT product_artifacts_artifact_fk FOREIGN KEY (artifact_id)
        REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    CONSTRAINT product_artifacts_uq UNIQUE (product_id, artifact_id),
    CONSTRAINT product_artifacts_legacy_ck CHECK (
        (legacy_rfid IS NOT NULL AND legacy_pid IS NULL)
        OR (legacy_pid IS NOT NULL AND legacy_rfid IS NULL)
        OR (legacy_rfid IS NULL AND legacy_pid IS NULL)
    ),
    CONSTRAINT product_artifacts_legacy_version_ck CHECK (
        (legacy_rfid IS NULL AND legacy_pid IS NULL)
        OR legacy_version IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS product_artifacts_product_idx
    ON product_artifacts USING btree (product_id);
CREATE INDEX IF NOT EXISTS product_artifacts_artifact_idx
    ON product_artifacts USING btree (artifact_id);

-- Exactly one CURRENT binding per product. Partial, so superseded bindings
-- accumulate as history rather than being deleted — the retry record is worth
-- keeping, and a delete-and-reinsert would lose it.
CREATE UNIQUE INDEX IF NOT EXISTS product_artifacts_one_current_uq
    ON product_artifacts (product_id) WHERE is_current;

COMMENT ON TABLE product_artifacts IS
    'Which artifact currently realizes which product, and which legacy '
    '(rfid|pid, version) row that binding corresponds to. On retry the '
    'product is unchanged, new artifacts are written, and the binding is '
    'repointed — which is how vbest semantics are preserved for existing '
    'consumers.';
COMMENT ON COLUMN product_artifacts.is_current IS
    'Exactly one true row per product, enforced by the partial unique index '
    'product_artifacts_one_current_uq. Superseded bindings are kept as '
    'history rather than deleted.';

-- ============================================================================
-- The legacy rows BIND to the product without BEING the identity
-- ============================================================================
--
-- A nullable FK, added to the two existing product tables. Nullable because
-- every row already in these tables predates product identity and there is no
-- key to backfill them with — the identity components (the workflow-definition
-- checksum, the release digest, the ordered inputs) are not recoverable from
-- the row. A NOT NULL column would make this migration unappliable to any
-- database with history, which is every database that matters.
--
-- `(rfid|pid, version)` remains exactly what it is today: the legacy
-- addressing consumers read, and `(attempt_id, record_sequence)` remains the
-- replay-dedup mechanism. Neither is conflated with identity here or in the
-- code — that is the whole point of the separation.
ALTER TABLE refimages
    ADD COLUMN IF NOT EXISTS product_id bigint;
ALTER TABLE diffimages
    ADD COLUMN IF NOT EXISTS product_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'refimages_product_fk') THEN
        ALTER TABLE refimages
            ADD CONSTRAINT refimages_product_fk FOREIGN KEY (product_id)
            REFERENCES products(product_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'diffimages_product_fk') THEN
        ALTER TABLE diffimages
            ADD CONSTRAINT diffimages_product_fk FOREIGN KEY (product_id)
            REFERENCES products(product_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS refimages_product_idx
    ON refimages USING btree (product_id);
CREATE INDEX IF NOT EXISTS diffimages_product_idx
    ON diffimages USING btree (product_id);

COMMENT ON COLUMN refimages.product_id IS
    'The deterministic product identity this legacy row realizes (rule 10). '
    'Nullable: rows predating product identity cannot be backfilled, because '
    'the identity components are not recoverable from the row. (rfid, '
    'version) remains the legacy addressing and is never the identity.';
COMMENT ON COLUMN diffimages.product_id IS
    'The deterministic product identity this legacy row realizes (rule 10). '
    'Nullable for the same reason as refimages.product_id. (pid, version) '
    'remains the legacy addressing and is never the identity.';

-- ============================================================================
-- Grants, matching the stream's existing posture for these tables
-- ============================================================================
--
-- The GROUP roles 001 creates and 002 grants to: `rapid_read` reads,
-- `rapid_pipeline_write` writes. Per-user roles get membership, never a
-- direct object grant (002's own first sentence) — so these three tables are
-- granted exactly like every other table in `public`.
--
-- GRANTED EXPLICITLY RATHER THAN LEFT TO 002's DEFAULT PRIVILEGES, and the
-- reason is written down in 002 itself: `ALTER DEFAULT PRIVILEGES FOR ROLE
-- rapid_admin` applies only to objects created by a session literally
-- authenticated as (or `SET ROLE`d to) rapid_admin, and NOT through role
-- membership — verified live 2026-07-14 per that file's note. A migration
-- applied by any other role therefore creates these tables with no group
-- grants at all, silently, and the failure surfaces later as the pipeline
-- being unable to write a product row. Guarded on role existence so the file
-- still applies to a scratch database built without 001's roles, which is
-- exactly what the contract tier's throwaway container is.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT SELECT ON products, artifacts, product_artifacts TO rapid_read;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_pipeline_write')
    THEN
        GRANT INSERT, UPDATE, DELETE
            ON products, artifacts, product_artifacts
            TO rapid_pipeline_write;
    END IF;
END
$$;

COMMIT;
