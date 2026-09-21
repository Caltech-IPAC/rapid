--------------------------------------------------------------------------------------------------------------------------
-- 20260921-02-run-model.sql
--
-- Runs, units of work, attempts, product instances and the three custody
-- states (scratch, candidate, current), plus promotion and its reversal.
-- Authority: rapid_docs' runs page
-- (https://roman-rapid.readthedocs.io/en/latest/system/runs.html), its
-- "Tables" list and "Rules" section, and the companion products page for
-- product/result-set identity. This migration adds tables beside the
-- `dev` schema landed in 20260921-01-baseline.sql; it renames and drops
-- nothing there (lead's ruling, specification.md "Schema" section).
--
-- Choices the runs page left open, made here and recorded in
-- LEDGER-runmodel.md:
--   - Identifiers: `text` ULIDs, 26 characters, Crockford base32 alphabet
--     (0-9, A-Z minus I L O U), enforced by a CHECK constraint on every id
--     column. The runs page's "Identifiers" section requires this shape;
--     it does not require a kind-prefixed id (the products page's example
--     manifest uses prefixes like `pi-diff-...` for display, but the
--     tables it describes are redesigned by this work, and the page says
--     the instance id itself is what a manifest and this schema carry).
--     Plain, unprefixed 26-character ULIDs are simplest and satisfy the
--     "text ULID" instruction literally.
--   - Enumerations: `text` with CHECK constraints, one degenerate value
--     list per column, rather than a schema-level ENUM type -- ENUM value
--     changes require ALTER TYPE and complicate the migrations-are-never-
--     edited discipline (migrations README) more than a CHECK swap would.
--   - `logical_key` is `jsonb`. Current-selection uniqueness is the
--     partial unique index on `(kind, logical_key)` where custody =
--     'current', named in the runs page's "Custody" rule directly.
--   - Timestamps are `timestamptz` throughout (the baseline's `dev`
--     tables use naked `timestamp`; this migration does not touch them).
--   - Foreign keys throughout, including from `result_sets` back to
--     `product_instances` (one-to-one) and from `promotion_changes` to
--     `product_instances` (nullable before/after, per the runs page).
--   - `current_selection` is a plain view over `product_instances` where
--     custody = 'current' -- the runs page names it exactly this: "The
--     current selection is a view over the instance table".
--------------------------------------------------------------------------------------------------------------------------

-- ======================================================================
-- Identifier shape: a domain, not per-column CHECKs repeated everywhere.
-- ======================================================================
--
-- A DOMAIN keeps the "26-char, Crockford base32" rule in exactly one
-- place; every id column below is declared `rapid_ulid` rather than
-- repeating the same CHECK on ten tables. Crockford base32 excludes
-- I, L, O and U to avoid confusion with 1, 1, 0 and V; case-insensitive
-- by convention, but rapidpipe.db.ids emits uppercase, so the constraint
-- requires uppercase (a lowercase id is not what this schema's own writer
-- ever produces, and accepting both would let two textually different
-- strings collide on meaning).
CREATE DOMAIN rapid_ulid AS text
    CHECK (
        length(VALUE) = 26
        AND VALUE ~ '^[0-9A-HJKMNP-TV-Z]{26}$'
    );

-- ======================================================================
-- TABLE: runs
-- ======================================================================

CREATE TABLE runs (
    id                          rapid_ulid PRIMARY KEY,
    kind                        text NOT NULL
        CHECK (kind IN ('scratch', 'production')),
    owner                       text NOT NULL,
    created                     timestamptz NOT NULL DEFAULT now(),
    state                       text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'finished', 'deleting', 'deleted')),
    purpose                     text,
    selected_stages              text[] NOT NULL DEFAULT '{}',
    code_revision                text NOT NULL,
    image_digest                 text,
    schema_version                text NOT NULL,
    settings_overlay_ref          text,
    input_selection_ref           text,
    lane                        text NOT NULL,
    resource_profile              text NOT NULL,
    database_target               text NOT NULL,
    max_attempts_per_unit          integer NOT NULL DEFAULT 1
        CHECK (max_attempts_per_unit >= 1),
    auto_promote                 boolean NOT NULL DEFAULT false,
    check_policy_ref              text,
    seed_run                    rapid_ulid REFERENCES runs (id),
    expires_at                   timestamptz,
    pinned                      boolean NOT NULL DEFAULT false,
    deleted_at                   timestamptz,
    finished_at                  timestamptz
);

COMMENT ON TABLE runs IS
    'One row per run: a pass over a chosen input set with fixed kind, '
    'code, settings and stage selection (runs page, "Tables" and "Runs").';

CREATE INDEX runs_kind_idx ON runs (kind);
CREATE INDEX runs_state_idx ON runs (state);
CREATE INDEX runs_owner_idx ON runs (owner);
CREATE INDEX runs_seed_run_idx ON runs (seed_run);

-- ======================================================================
-- TABLE: units
-- ======================================================================

CREATE TABLE units (
    id                     rapid_ulid PRIMARY KEY,
    run                   rapid_ulid NOT NULL REFERENCES runs (id),
    stage                 text NOT NULL,
    unit_kind               text NOT NULL
        CHECK (unit_kind IN ('exposure', 'detector-image', 'field', 'processing-date')),
    unit_id                text NOT NULL,
    state                 text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'ready', 'running', 'complete', 'failed', 'cancelled')),
    selected_attempt         rapid_ulid,
    cancel_reason            text,
    created                timestamptz NOT NULL DEFAULT now(),
    updated                timestamptz NOT NULL DEFAULT now(),
    -- One unit per (run, stage, unit_id): unit identity is unique within
    -- a run and stage (runs page, "Identifiers").
    UNIQUE (run, stage, unit_id)
);

COMMENT ON TABLE units IS
    'One row per piece of work in a run: a stage applied to one unit id '
    '(runs page, "Tables" and "Units"). selected_attempt is set only by '
    'select_attempt(), after the referenced attempts table exists; the FK '
    'is added at the end of this file to avoid a forward reference.';

CREATE INDEX units_run_idx ON units (run);
CREATE INDEX units_run_stage_idx ON units (run, stage);
CREATE INDEX units_state_idx ON units (state);
CREATE INDEX units_selected_attempt_idx ON units (selected_attempt);

-- ======================================================================
-- TABLE: attempts
-- ======================================================================

CREATE TABLE attempts (
    id                   rapid_ulid PRIMARY KEY,
    run                 rapid_ulid NOT NULL REFERENCES runs (id),
    stage               text NOT NULL,
    unit                rapid_ulid NOT NULL REFERENCES units (id),
    started              timestamptz NOT NULL DEFAULT now(),
    ended                timestamptz,
    exit_code             integer,
    -- NULL while queued or running; set only on completion (runs page,
    -- "Attempts": "an attempt has no disposition while queued or running").
    disposition          text
        CHECK (disposition IN ('succeeded', 'failed', 'transient', 'killed', 'lost')),
    output_location        text NOT NULL,
    scheduler_job_id       text
);

COMMENT ON TABLE attempts IS
    'One row per execution attempt of a unit, with its own exclusive '
    'output location (runs page, "Tables" and "Attempts").';

CREATE INDEX attempts_run_idx ON attempts (run);
CREATE INDEX attempts_unit_idx ON attempts (unit);
CREATE INDEX attempts_disposition_idx ON attempts (disposition);

-- Now that attempts exists, close the forward reference from units.
ALTER TABLE units
    ADD CONSTRAINT units_selected_attempt_fk
    FOREIGN KEY (selected_attempt) REFERENCES attempts (id);

-- ======================================================================
-- TABLE: execution_records
-- ======================================================================

CREATE TABLE execution_records (
    attempt                rapid_ulid PRIMARY KEY REFERENCES attempts (id),
    source_revision          text NOT NULL,
    working_copy_patch       text,
    image_digest             text,
    schema_version            text NOT NULL,
    resolved_settings         jsonb NOT NULL DEFAULT '{}'::jsonb,
    settings_hash             text NOT NULL,
    scheduler_metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created                 timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE execution_records IS
    'One row per attempt: the execution record contents are stored here, '
    'not referenced into the run prefix (runs page, "Tables"; stage '
    'contract, "The manifest").';

-- ======================================================================
-- TABLE: product_instances
-- ======================================================================

CREATE TABLE product_instances (
    id                     rapid_ulid PRIMARY KEY,
    kind                   text NOT NULL,
    logical_key             jsonb NOT NULL,
    run                    rapid_ulid NOT NULL REFERENCES runs (id),
    producing_stage          text NOT NULL,
    producing_attempt        rapid_ulid NOT NULL REFERENCES attempts (id),
    registering_attempt       rapid_ulid NOT NULL REFERENCES attempts (id),
    custody                 text NOT NULL
        CHECK (custody IN ('scratch', 'candidate', 'current')),
    format_version           text NOT NULL,
    primary_location          text NOT NULL,
    manifest_ref              text NOT NULL,
    published_at             timestamptz NOT NULL DEFAULT now(),
    deletion_state            text NOT NULL DEFAULT 'retained'
        CHECK (deletion_state IN ('retained', 'deleted'))
);

COMMENT ON TABLE product_instances IS
    'One row per product an attempt made, file or result set (runs page, '
    '"Tables" and "Instances"; products page, "Identity"). custody and '
    'deletion_state change only through promotion and deletion.';

CREATE INDEX product_instances_kind_idx ON product_instances (kind);
CREATE INDEX product_instances_run_idx ON product_instances (run);
CREATE INDEX product_instances_custody_idx ON product_instances (custody);
CREATE INDEX product_instances_producing_attempt_idx ON product_instances (producing_attempt);
CREATE INDEX product_instances_logical_key_idx ON product_instances USING gin (logical_key);

-- At most one current instance per kind and logical key (runs page,
-- "Custody"): a partial unique index, not a plain UNIQUE, because the
-- uniqueness rule applies only among custody = 'current' rows -- many
-- scratch and candidate instances may share a logical key.
CREATE UNIQUE INDEX product_instances_current_key_uq
    ON product_instances (kind, logical_key)
    WHERE custody = 'current';

-- ======================================================================
-- TABLE: product_members
-- ======================================================================

CREATE TABLE product_members (
    id                rapid_ulid PRIMARY KEY,
    instance          rapid_ulid NOT NULL REFERENCES product_instances (id),
    role              text NOT NULL,
    path              text NOT NULL,
    bytes             bigint NOT NULL CHECK (bytes >= 0),
    sha256            text NOT NULL,
    UNIQUE (instance, role)
);

COMMENT ON TABLE product_members IS
    'One row per file in a bundle product (runs page, "Tables"; products '
    'page, "File products": a bundle is one product with several member '
    'files, each with its role, size and SHA-256).';

CREATE INDEX product_members_instance_idx ON product_members (instance);

-- ======================================================================
-- TABLE: result_sets
-- ======================================================================

CREATE TABLE result_sets (
    instance          rapid_ulid PRIMARY KEY REFERENCES product_instances (id),
    complete          boolean NOT NULL DEFAULT false,
    row_count          bigint
        CHECK (row_count IS NULL OR row_count >= 0)
);

COMMENT ON TABLE result_sets IS
    'One-to-one extension of product_instances for database-row products '
    '(runs page, "Tables"; products page, "Database result sets"). '
    'Completion is recorded here, so an empty result set can be complete.';

-- ======================================================================
-- TABLE: promotions
-- ======================================================================

CREATE TABLE promotions (
    id                     rapid_ulid PRIMARY KEY,
    who                    text NOT NULL,
    happened_at              timestamptz NOT NULL DEFAULT now(),
    reason                 text NOT NULL,
    check_policy_version     text,
    check_result_ids         rapid_ulid[] NOT NULL DEFAULT '{}',
    request_context          jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE promotions IS
    'One row per promotion action (runs page, "Tables" and "Promotion").';

-- ======================================================================
-- TABLE: promotion_changes
-- ======================================================================

CREATE TABLE promotion_changes (
    id                 rapid_ulid PRIMARY KEY,
    promotion          rapid_ulid NOT NULL REFERENCES promotions (id),
    kind               text NOT NULL,
    logical_key         jsonb NOT NULL,
    before_instance      rapid_ulid REFERENCES product_instances (id),
    after_instance       rapid_ulid REFERENCES product_instances (id)
);

COMMENT ON TABLE promotion_changes IS
    'One row per affected logical key in a promotion, before and after '
    'instance each nullable (runs page, "Tables" and "Promotion": "Each '
    'promotion records a before and after instance for every affected '
    'key, either nullable.").';

CREATE INDEX promotion_changes_promotion_idx ON promotion_changes (promotion);
CREATE INDEX promotion_changes_kind_key_idx ON promotion_changes (kind, logical_key);

-- ======================================================================
-- TABLE: checks
-- ======================================================================

CREATE TABLE checks (
    id                 rapid_ulid PRIMARY KEY,
    instance           rapid_ulid NOT NULL REFERENCES product_instances (id),
    check_name          text NOT NULL,
    version            text NOT NULL,
    required           boolean NOT NULL DEFAULT true,
    outcome            text NOT NULL
        CHECK (outcome IN ('passed', 'failed')),
    happened_at         timestamptz NOT NULL DEFAULT now(),
    detail             jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE checks IS
    'One row per verification result against a product instance (runs '
    'page, "Tables"; specification.md, "Promotion": "Each candidate '
    'records its check results.").';

CREATE INDEX checks_instance_idx ON checks (instance);

-- ======================================================================
-- TABLE: dependencies
-- ======================================================================

CREATE TABLE dependencies (
    id                 rapid_ulid PRIMARY KEY,
    consumer_instance    rapid_ulid NOT NULL REFERENCES product_instances (id),
    producer_instance    rapid_ulid NOT NULL REFERENCES product_instances (id),
    UNIQUE (consumer_instance, producer_instance)
);

COMMENT ON TABLE dependencies IS
    'One row per provenance edge: consumer_instance was built from '
    'producer_instance (runs page, "Tables"; specification.md, "Runs": '
    '"Provenance is the point of all of this.").';

CREATE INDEX dependencies_consumer_idx ON dependencies (consumer_instance);
CREATE INDEX dependencies_producer_idx ON dependencies (producer_instance);

-- ======================================================================
-- TABLE: unit_inputs
-- ======================================================================
--
-- Declared last among the "core" tables because it references both units
-- and product_instances. One row per frozen input binding (runs page,
-- "Tables" and "Units": "Inputs are bound in unit_inputs before execution
-- and retained for retries.").

CREATE TABLE unit_inputs (
    id                  rapid_ulid PRIMARY KEY,
    unit                rapid_ulid NOT NULL REFERENCES units (id),
    producer_instance     rapid_ulid NOT NULL REFERENCES product_instances (id),
    UNIQUE (unit, producer_instance)
);

COMMENT ON TABLE unit_inputs IS
    'One row per frozen input binding: unit consumes producer_instance '
    '(runs page, "Tables" and "Units").';

CREATE INDEX unit_inputs_unit_idx ON unit_inputs (unit);
CREATE INDEX unit_inputs_producer_instance_idx ON unit_inputs (producer_instance);

-- ======================================================================
-- VIEW: current_selection
-- ======================================================================
--
-- "The current selection is a view over the instance table and includes
-- file products and result sets alike." (runs page, "Custody"). A plain
-- view, not materialized: the partial unique index above already makes
-- the underlying query cheap (index-only for the common kind/logical_key
-- lookup), and a materialized view would need its own refresh discipline
-- that nothing in the runs page asks for.

CREATE VIEW current_selection AS
    SELECT
        pi.id,
        pi.kind,
        pi.logical_key,
        pi.run,
        pi.producing_stage,
        pi.producing_attempt,
        pi.registering_attempt,
        pi.format_version,
        pi.primary_location,
        pi.manifest_ref,
        pi.published_at,
        pi.deletion_state,
        rs.complete AS result_set_complete,
        rs.row_count AS result_set_row_count
    FROM product_instances pi
    LEFT JOIN result_sets rs ON rs.instance = pi.id
    WHERE pi.custody = 'current';

COMMENT ON VIEW current_selection IS
    'Current-custody product instances, file and result-set alike (runs '
    'page, "Custody"). Consumers resolve a related selection from one '
    'database snapshot by querying this view inside one transaction.';
