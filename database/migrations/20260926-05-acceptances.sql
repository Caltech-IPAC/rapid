--------------------------------------------------------------------------------------------------------------------------
-- 20260926-05-acceptances.sql
--
-- Recorded acceptances (supervisor step 6, 2026-09-26, R3): a person's
-- recorded decision that a candidate whose required check failed may
-- stand behind a promotion anyway. One row per acceptance: the instance,
-- who accepted it, why, the check policy that governed it, the `checks`
-- rows that were the evidence (the latest row of every policy check for
-- the instance's kind), and the failed checks' summaries in `detail`.
-- Acceptance is separate from selection: a row changes no custody. There
-- is no revocation; a wrong acceptance is corrected by replacing the
-- product. Additive only, every statement idempotent (IF NOT EXISTS), so
-- the file applies twice without error. Grants to `rapid_rebuild_pipeline`
-- are guarded on the role's existence, as 20260924-11 is, so CI (no such
-- role) applies them as a no-op.
--------------------------------------------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS acceptances (
    id          rapid_ulid PRIMARY KEY,
    instance    rapid_ulid NOT NULL REFERENCES product_instances (id),
    who         text NOT NULL,
    reason      text NOT NULL CHECK (reason <> ''),
    policy_ref  text NOT NULL,
    check_ids   rapid_ulid[] NOT NULL,
    happened_at timestamptz NOT NULL DEFAULT now(),
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS acceptances_instance_idx ON acceptances (instance);

COMMENT ON TABLE acceptances IS
    'Recorded acceptances of candidates whose required check failed (supervisor step 6, '
    '2026-09-26, R3): who, why, the governing policy and the checks rows relied on. '
    'An accepted candidate may stand behind a promotion; acceptance changes no custody '
    'and is never revoked (a wrong one is corrected by replacing the product).';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT ON acceptances TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
