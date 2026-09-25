--------------------------------------------------------------------------------------------------------------------------
-- 20260924-08-releases.sql
--
-- Releases: one row per tagged, built and deployed version of the rebuilt
-- pipeline, and one row per job definition a release deployed. Authority:
-- rapid_docs system/releases.md and specification.md "Releases" ("Each
-- release records its source commit, image digest and the schema versions
-- it supports; each run records the image digest and schema version it
-- used"; "A candidate becomes current only if its recorded image is a
-- released artifact"). Written by `python -m rapidpipe.release cut`
-- (rapidpipe/release/core.py), which inserts the row only after the
-- release's own `migrate` hook has applied this file.
--
-- Also adds `runs.release` (the release a run was created from, when it
-- was) and `execution_records.release` (the release identity the attempt's
-- job definition carried; no foreign key, because an execution record is
-- written from what the container reports, and a job may carry a tag this
-- database never recorded). Additive only; nothing renamed or dropped.
-- Grants to `rapid_rebuild_pipeline` are guarded on the role's existence,
-- as 20260924-06 is, so CI (no such role) applies them as a no-op.
--------------------------------------------------------------------------------------------------------------------------

CREATE TABLE releases (
    tag             text PRIMARY KEY,
    source_revision text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    schema_version  text NOT NULL,
    image_digest    text CHECK (image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'),
    image_ref       text,
    state           text NOT NULL CHECK (state IN ('migrated','built','deployed','complete')),
    cut_by          text NOT NULL,
    cut_at          timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    notes           jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE releases IS
    'One row per release of the rebuilt pipeline (rapid_docs system/releases.md): '
    'the immutable tag, its source commit, the schema version its tree ships, and '
    'the image digest built from it. state advances migrated -> built -> deployed '
    '-> complete as `rapidpipe release cut` checkpoints each hook; only a complete '
    'release makes a run eligible for promotion.';
COMMENT ON COLUMN releases.tag IS
    'The annotated git tag rebuild-v0.<n>; never moved or deleted (releases.md).';
COMMENT ON COLUMN releases.schema_version IS
    'Greatest migration filename in the tagged tree''s database/migrations/.';
COMMENT ON COLUMN releases.notes IS
    'Freezing facts checked on resume: hooks_dir_revision, database_target, '
    'skipped hooks; plus hook results worth keeping (image_tag, pins_rows).';

CREATE TABLE release_deployments (
    id             bigserial PRIMARY KEY,
    release        text NOT NULL REFERENCES releases (tag),
    consumer       text NOT NULL,
    job_definition text NOT NULL,        -- name:revision
    deployed_at    timestamptz NOT NULL DEFAULT now(),
    deployed_by    text NOT NULL,
    UNIQUE (release, consumer)
);

COMMENT ON TABLE release_deployments IS
    'One row per Batch job definition revision a release deployed, keyed by '
    'consumer (rapid_docs system/releases.md). A run created from the release '
    'submits to exactly this name:revision, never the unversioned name.';

ALTER TABLE runs ADD COLUMN release text REFERENCES releases (tag);
COMMENT ON COLUMN runs.release IS
    'The complete release this run was created from (rapidpipe run create '
    '--release), or NULL for a run from an arbitrary commit (releases.md).';

ALTER TABLE execution_records ADD COLUMN release text;
COMMENT ON COLUMN execution_records.release IS
    'The release identity the attempt''s job carried (RAPIDPIPE_RELEASE or '
    'RAPID_RELEASE_IDENTITY), NULL when unreleased (releases.md). No foreign '
    'key: recorded as reported.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_rebuild_pipeline') THEN
        GRANT SELECT, INSERT, UPDATE ON releases TO rapid_rebuild_pipeline;
        GRANT SELECT, INSERT, UPDATE ON release_deployments TO rapid_rebuild_pipeline;
        GRANT SELECT, UPDATE, USAGE ON SEQUENCE release_deployments_id_seq TO rapid_rebuild_pipeline;
    ELSE
        RAISE NOTICE 'role rapid_rebuild_pipeline does not exist here; grants skipped';
    END IF;
END
$$;
