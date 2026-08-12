-- 051-admission-identity-and-release.sql — the admission identity rule 20
-- requires, the sealed source manifest that makes admission replayable, and
-- the switchable "release for future admissions" pointer rule 18's rollback
-- clause needs.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the schema
-- and this file is not applied by `apply-db-migrations.sh` until its owner
-- adopts it. See that directory's README. Requires 006 (the `exposures` and
-- `l2files` tables it attaches identity to) and DRAFT 047 (`derived.
-- mutation_audit`'s idempotency/expected-state pair, which the release-pointer
-- mutation function writes through) — apply in order.
--
-- Conformance rule 20, verbatim:
--
--     "The admission source is durable and replayable; admission is
--      idempotent and a repeated observation returns its existing admission."
--
-- and rule 18's rollback clause:
--
--     "...rollback changes only the release used for future admissions."
--
-- ============================================================================
-- THE DEFECT THIS FILE ADDRESSES, READ FROM THIS STREAM'S OWN SQL
-- ============================================================================
--
-- `exposures` already HAS a database-enforced natural key —
-- `CONSTRAINT exposurespk UNIQUE (dateobs)` (006-core-tables.sql:194). On that
-- one point the system is better than the conformance baseline assumed. What
-- is wrong is what `addexposure` (008-functions.sql:250-355) does with it:
--
--   * it is SELECT-THEN-INSERT — `select expid into expid__ from Exposures
--     where dateobs = dateobs_;` (:290-293) followed by a conditional INSERT.
--     Two concurrent admissions of the same observation both read NULL and
--     both insert; the loser takes a unique violation on `exposurespk` instead
--     of RECEIVING THE EXISTING ADMISSION. Rule 20 asks for the latter.
--
--   * on a repeat it OVERWRITES. The `else` branch (:331-345) updates every
--     field INCLUDING `created = now()`. Re-admitting an observation silently
--     mutates its own admission record and destroys the original ingest
--     timestamp, which is unrecoverable afterwards. Rule 20 says a repeat
--     RETURNS its existing admission; it does not say a repeat may redefine
--     it.
--
-- The L2 half is the deeper defect. `l2filespk UNIQUE (expid, sca, version)`
-- (006-core-tables.sql:330) puts the VERSION inside the uniqueness, and
-- `addl2file` computes `coalesce(max(version), 0) + 1` (008-functions.sql:438-
-- 446) — so the `max+1` deliberately sidesteps the constraint and RE-RUNNING AN
-- INGEST FOR THE SAME L2 FILE MINTS A NEW ADMISSION ROW. There is no
-- `(expid, sca)`-level natural key and no content uniqueness anywhere.
--
-- The only thing standing between a replayed ingest and duplicate admissions
-- today is an application-side check that is filename-basename scoped and
-- disabled by a single environment variable, `DONTCHECKALREADYINGESTED`
-- (`database/sims/db_register_socsim_files.py:88-98`, applied at :886-892).
-- Admission idempotency is currently a convention with a kill switch, not an
-- invariant.
--
-- ============================================================================
-- WHY IDENTITY IS DEFINED PER GRAIN, AND DIFFERENTLY
-- ============================================================================
--
-- Ingestion is PER-L2-DETECTOR-FILE (`db_register_socsim_files.py`: each file
-- is downloaded, `register_exposure` is called, then the `(expid, sca)` L2 row
-- is registered). There is therefore no exposure-level "admitted file" whose
-- checksum could enter an exposure identity, and the two grains get different
-- identity rules:
--
--   EXPOSURE GRAIN — identity is `dateobs` ALONE, matching the database's
--   actual natural key. No checksum participates. An exposure is an
--   OBSERVATIONAL FACT, not a file: the same pointing at the same instant is
--   the same exposure however many detector files carry it, and whatever their
--   bytes are.
--
--   L2 GRAIN — identity is a CONTENT KEY over `(expid, sca)` plus the source
--   content checksum of that file. This is the grain where a file, and
--   therefore a checksum, actually exists.
--
-- Both are computed by `pipeline/repositories/admission_identity.py` under a
-- versioned canonical serialization with a forbidden-input guard, the same
-- discipline `pipeline/registration/identity.py` established for rule 10.
-- Paths, filenames, basenames, bucket names, S3 keys, attempt/run identity and
-- the ingest wall-clock are all forbidden at BOTH grains: a filename is not an
-- identity, and that conflation is precisely the current defect.
--
-- The two grains are NAMESPACE-SEPARATED inside the serialization
-- (`admission_grain` is a hashed component), so an exposure identity and an L2
-- identity can never collide even if their other components coincided.
--
-- ============================================================================
-- THE CONFLICT POLICY IS REFUSAL, NOT OVERWRITE AND NOT SILENT ACCEPT
-- ============================================================================
--
-- Rule 20 says a repeat returns its existing admission. It does not say a
-- repeat may REDEFINE it. So:
--
--   * the same `dateobs` arriving with conflicting observational facts (a
--     different field, filter, exposure time, mjdobs or healpix cell) is
--     REFUSED, with the conflicting column named and BOTH values reported;
--   * the same `(expid, sca)` arriving with a different source checksum is
--     REFUSED, never silently re-versioned.
--
-- Both refusals are raised by `derived.admit_exposure` / `derived.admit_l2file`
-- below with SQLSTATE **RA010**, so the application classifies them by code
-- and never by message text (`pipeline/operatorctl/contract.py`'s established
-- discipline for RA001/RA002).
--
-- ============================================================================
-- WHY THE ADMISSION TABLES ARE SEPARATE FROM `exposures` / `l2files`
-- ============================================================================
--
-- Two reasons, both about not breaking what already reads those tables.
--
-- 1. NO READER IS MIGRATED. `exposures` and `l2files` keep every column and
--    every constraint they have; the legacy stored procedures keep working
--    unchanged for any caller still on them. This file ADDS a sidecar
--    admission record keyed to the same row, exactly as DRAFT 048 added a
--    nullable `product_id` to `refimages`/`diffimages` without migrating a
--    reader.
--
-- 2. THE L2 GRAIN NEEDS A CONSTRAINT `l2files` CANNOT CARRY. The natural key
--    rule 20 wants at that grain is `(expid, sca)` + content, and `l2files`
--    already has `l2filespk UNIQUE (expid, sca, version)` with live rows
--    exercising the version dimension. Adding `UNIQUE (expid, sca)` to that
--    table would refuse to apply against any database holding a genuine
--    re-version. The sidecar carries the constraint the new path needs while
--    leaving the old table's shape alone.
--
-- Note also that `l2files.checksum` is `character varying(32)`
-- (006-core-tables.sql:259) and therefore TRUNCATES every SHA-256 it is given
-- — the latent defect brief D flagged as CR-8, still unlanded. The admission
-- sidecar stores its own full-width `source_checksum` with the algorithm
-- recorded, so admission identity never depends on a truncated value.
--
-- ============================================================================
-- THE SEALED SOURCE MANIFEST, AND WHY NAMES AND CHECKSUMS ARE NOT ENOUGH
-- ============================================================================
--
-- Rule 20 asks for a DURABLE, REPLAYABLE admission source. A manifest listing
-- only object names and checksums cannot deliver that here: the current
-- writers parse FITS headers and WCS to derive many database fields FROM THE
-- SOURCE BYTES (`db_register_socsim_files.py`, checksum computed alongside), so
-- replaying from names alone would need the bytes to still exist and to still
-- parse identically.
--
-- So the manifest is three things at once:
--
--   * an ENUMERATION of the source objects with their checksums AND their
--     immutable version references (`source_version_id`, the input bucket's
--     own object-version identifier where available), so a replay can name the
--     exact bytes rather than whatever now sits at that key;
--   * a SEALED/UNSEALED state, so a partially-enumerated manifest is never
--     mistaken for a whole one;
--   * the carrier for EVERY PARSED ADMISSION FACT (`admitted_facts` jsonb on
--     the admission rows), so a replay reconstructs the database rows FROM
--     RECORDED FACTS rather than by re-parsing bytes that may no longer exist.
--
-- CRASH ORDER IS FIXED AND IS THE POINT OF THE `sealed_at` COLUMN. A manifest
-- is created UNSEALED, its entries are written, and it is sealed ONLY after
-- every entry is durable. Admissions may then reference it. A crash therefore
-- leaves either a complete replayable record or an EXPLICITLY UNSEALED one —
-- never a sealed manifest whose entries are partial. The FK from
-- `admission_exposures.manifest_id` plus the `admission_manifests_sealed_ck`
-- trigger below enforce that an admission cannot cite an unsealed manifest.
--
-- ============================================================================
-- THE RELEASE POINTER (rule 18)
-- ============================================================================
--
-- `ExecutionBinding` (`observability/attempts.py`) is mandatory and complete
-- and pins a release at SUBMISSION — a retry inherits the pin and does not
-- float. That half of rule 18 already conforms. What has no mechanism is
-- "rollback changes only the release used for FUTURE ADMISSIONS", because
-- admission has no release concept at all: `addexposure`'s parameters are pure
-- observational facts.
--
-- `admission_release_pointer` is that mechanism: a single-valued, audited,
-- switchable pointer, DISTINCT from the release any in-flight work is pinned
-- to. Changing it affects only admissions made after the change; it never
-- rewrites an existing admission's stamp and never touches in-flight work.
-- That separation IS the rule-18 clause.
--
-- It is single-valued by a PARTIAL UNIQUE INDEX on `is_current`, not by
-- convention — the same discipline DRAFT 049 used for "at most one live prompt
-- set". Superseded rows are RETAINED rather than updated, so the pointer's
-- history is its own audit trail and a rollback is visible as what it was.
--
-- Mutation goes through `derived.set_admission_release`, a SECURITY DEFINER
-- function under G's full mutation contract (actor, reason, idempotency key,
-- expected state, dry-run) writing `derived.mutation_audit` through DRAFT
-- 047's keyed path. There is deliberately NO table-level UPDATE/INSERT grant
-- on the pointer for any pipeline role: a job that could switch the release
-- could silently escape the pin it is supposed to obey.
--
-- A pointer naming an unresolvable release is REFUSED AT MUTATION TIME rather
-- than discovered at submission time — `admission_releases` is the registry of
-- known, resolvable release identities and the pointer FKs to it.
--
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- The known releases. A pointer may only name a row here, which is what makes
-- "a pointer naming an unknown release is refused at mutation time" a database
-- guarantee rather than an application check.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admission_releases (
    release_identity  text        NOT NULL,
    manifest_uri      text,
    manifest_checksum text,
    registered_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT admission_releases_pkey PRIMARY KEY (release_identity),
    CONSTRAINT admission_releases_identity_ck
        CHECK (length(btrim(release_identity)) > 0)
);

COMMENT ON TABLE admission_releases IS
    'Release identities that resolve to an immutable release manifest. The '
    'admission release pointer FKs here, so naming an unknown release is '
    'refused at mutation time rather than discovered at submission time '
    '(rule 18).';
COMMENT ON COLUMN admission_releases.manifest_uri IS
    'Where the immutable release manifest lives. Nullable: a release may be '
    'registered before its manifest is published, and the resolvability the '
    'pointer requires is the ROW existing, not the URI being reachable from '
    'the database.';

-- ---------------------------------------------------------------------------
-- The pointer itself: append-a-row-and-move-the-flag, never an in-place
-- rewrite, so the history is the audit trail.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admission_release_pointer (
    pointer_id       bigint GENERATED BY DEFAULT AS IDENTITY,
    release_identity text        NOT NULL,
    is_current       boolean     NOT NULL DEFAULT true,
    set_at           timestamptz NOT NULL DEFAULT now(),
    set_by           text        NOT NULL,
    reason           text        NOT NULL,
    audit_id         bigint,
    CONSTRAINT admission_release_pointer_pkey PRIMARY KEY (pointer_id),
    CONSTRAINT admission_release_pointer_release_fk
        FOREIGN KEY (release_identity)
        REFERENCES admission_releases(release_identity),
    CONSTRAINT admission_release_pointer_reason_ck
        CHECK (length(btrim(reason)) > 0)
);

-- AT MOST ONE CURRENT POINTER, enforced by the database rather than by
-- convention (DRAFT 049's "at most one live prompt set" discipline). A partial
-- unique index, so superseded rows accumulate freely.
CREATE UNIQUE INDEX IF NOT EXISTS admission_release_pointer_current_uq
    ON admission_release_pointer (is_current) WHERE is_current;

CREATE INDEX IF NOT EXISTS admission_release_pointer_set_at_idx
    ON admission_release_pointer USING btree (set_at DESC);

COMMENT ON TABLE admission_release_pointer IS
    'The release future admissions are stamped with (rule 18: "rollback '
    'changes only the release used for future admissions"). Distinct from the '
    'release in-flight work is pinned to via ExecutionBinding — switching '
    'this never rewrites an existing admission stamp and never touches '
    'in-flight work. Superseded rows are retained, not updated.';
COMMENT ON COLUMN admission_release_pointer.is_current IS
    'Exactly one true row at a time, by admission_release_pointer_current_uq. '
    'A switch inserts the new row and clears the old flag in one transaction.';

-- ---------------------------------------------------------------------------
-- The sealed source manifest.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admission_manifests (
    manifest_id      bigint GENERATED BY DEFAULT AS IDENTITY,
    manifest_key     text        NOT NULL,
    source_scope     text        NOT NULL,
    entry_count      integer,
    entries_checksum text,
    release_identity text        NOT NULL,
    byte_custody     text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    sealed_at        timestamptz,
    CONSTRAINT admission_manifests_pkey PRIMARY KEY (manifest_id),
    CONSTRAINT admission_manifests_key_uq UNIQUE (manifest_key),
    CONSTRAINT admission_manifests_release_fk
        FOREIGN KEY (release_identity)
        REFERENCES admission_releases(release_identity),
    -- BYTE CUSTODY IS STATED, NEVER ASSUMED (rule 20's durability clause).
    -- Either this pipeline retains the source bytes, or the manifest pins an
    -- immutable external version and the durability guarantee is only as
    -- strong as that external retention. A manifest must say which.
    CONSTRAINT admission_manifests_custody_ck
        CHECK (byte_custody IN ('pipeline-retained', 'external-versioned',
                                'none')),
    -- A SEALED MANIFEST MUST BE COMPLETE. Sealing without a recorded entry
    -- count and entries checksum would be sealing without knowing what was
    -- sealed.
    CONSTRAINT admission_manifests_sealed_complete_ck
        CHECK (sealed_at IS NULL
               OR (entry_count IS NOT NULL AND entries_checksum IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS admission_manifests_sealed_idx
    ON admission_manifests USING btree (sealed_at)
    WHERE sealed_at IS NOT NULL;

COMMENT ON TABLE admission_manifests IS
    'The durable, replayable admission source (rule 20). Created UNSEALED, '
    'filled with entries, and sealed only once every entry is durable — so a '
    'crash leaves either a complete replayable record or an explicitly '
    'unsealed one, never a sealed manifest with partial entries.';
COMMENT ON COLUMN admission_manifests.byte_custody IS
    'Whether the source bytes are retained by this pipeline '
    '(pipeline-retained), pinned only by an immutable external object version '
    '(external-versioned), or neither (none — replay from recorded facts '
    'only). Stated rather than assumed: the replay guarantee is only as '
    'durable as what this column names.';
COMMENT ON COLUMN admission_manifests.entries_checksum IS
    'A checksum over the canonical serialization of the entry list, so a '
    'sealed manifest that later disagrees with its entries is detectable.';

CREATE TABLE IF NOT EXISTS admission_manifest_entries (
    manifest_id       bigint      NOT NULL,
    source_key        text        NOT NULL,
    source_bucket     text        NOT NULL,
    source_version_id text,
    source_checksum   text        NOT NULL,
    checksum_algorithm text       NOT NULL DEFAULT 'sha256',
    source_bytes      bigint,
    CONSTRAINT admission_manifest_entries_pkey
        PRIMARY KEY (manifest_id, source_bucket, source_key),
    CONSTRAINT admission_manifest_entries_manifest_fk
        FOREIGN KEY (manifest_id) REFERENCES admission_manifests(manifest_id),
    -- FULL-WIDTH, WITH THE ALGORITHM RECORDED. `l2files.checksum` is
    -- varchar(32) and truncates every SHA-256 (CR-8, unlanded); admission
    -- identity must never depend on a truncated value.
    CONSTRAINT admission_manifest_entries_checksum_ck
        CHECK (length(source_checksum) BETWEEN 32 AND 128),
    CONSTRAINT admission_manifest_entries_algorithm_ck
        CHECK (checksum_algorithm IN ('sha256', 'md5'))
);

COMMENT ON TABLE admission_manifest_entries IS
    'One enumerated source object per row, with its content checksum AND its '
    'immutable version reference where the input bucket provides one. The '
    'version reference is what lets a replay name the exact bytes rather than '
    'whatever now sits at that key.';
COMMENT ON COLUMN admission_manifest_entries.source_version_id IS
    'The input bucket''s own object-version identifier. Nullable because not '
    'every input bucket is versioned; where it is NULL the manifest''s '
    'byte_custody says what the replay guarantee actually rests on.';

-- ---------------------------------------------------------------------------
-- The admission records themselves — one sidecar row per admitted grain.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admission_exposures (
    admission_id       bigint GENERATED BY DEFAULT AS IDENTITY,
    admission_identity text        NOT NULL,
    expid              integer     NOT NULL,
    manifest_id        bigint,
    release_identity   text        NOT NULL,
    admitted_facts     jsonb       NOT NULL,
    admitted_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT admission_exposures_pkey PRIMARY KEY (admission_id),
    -- THE IDENTITY IS THE NATURAL KEY, and it is UNIQUE in the database so
    -- `INSERT ... ON CONFLICT ... RETURNING` can return the existing
    -- admission rather than racing (rule 20's "returns its existing
    -- admission").
    CONSTRAINT admission_exposures_identity_uq UNIQUE (admission_identity),
    -- ONE ADMISSION PER EXPOSURE ROW. Without this a second identity could
    -- attach to the same expid and the sidecar would stop being a record of
    -- one admission.
    CONSTRAINT admission_exposures_expid_uq UNIQUE (expid),
    CONSTRAINT admission_exposures_expid_fk
        FOREIGN KEY (expid) REFERENCES exposures(expid),
    CONSTRAINT admission_exposures_manifest_fk
        FOREIGN KEY (manifest_id) REFERENCES admission_manifests(manifest_id),
    CONSTRAINT admission_exposures_release_fk
        FOREIGN KEY (release_identity)
        REFERENCES admission_releases(release_identity),
    CONSTRAINT admission_exposures_identity_ck
        CHECK (admission_identity LIKE 'sha256:%')
);

CREATE INDEX IF NOT EXISTS admission_exposures_manifest_idx
    ON admission_exposures USING btree (manifest_id);
CREATE INDEX IF NOT EXISTS admission_exposures_release_idx
    ON admission_exposures USING btree (release_identity);

COMMENT ON TABLE admission_exposures IS
    'The admission record for one exposure. Identity is dateobs ALONE '
    '(matching exposurespk, the database''s own natural key); no checksum '
    'participates, because an exposure is an observational fact and not a '
    'file. `admitted_at` is written once and never rewritten — unlike '
    'addexposure''s update branch, which overwrites created = now() on every '
    'repeat.';
COMMENT ON COLUMN admission_exposures.admitted_facts IS
    'Every parsed admission fact, so a replay reconstructs the row from '
    'recorded facts rather than by re-parsing source bytes that may no longer '
    'exist (rule 20''s replayability clause).';
COMMENT ON COLUMN admission_exposures.release_identity IS
    'The release this admission was made under, stamped from the pointer at '
    'admission time. Never rewritten by a later pointer switch — that is the '
    'rule-18 separation.';

CREATE TABLE IF NOT EXISTS admission_l2files (
    admission_id       bigint GENERATED BY DEFAULT AS IDENTITY,
    admission_identity text        NOT NULL,
    rid                integer     NOT NULL,
    expid              integer     NOT NULL,
    sca                smallint    NOT NULL,
    source_checksum    text        NOT NULL,
    checksum_algorithm text        NOT NULL DEFAULT 'sha256',
    manifest_id        bigint,
    release_identity   text        NOT NULL,
    admitted_facts     jsonb       NOT NULL,
    admitted_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT admission_l2files_pkey PRIMARY KEY (admission_id),
    CONSTRAINT admission_l2files_identity_uq UNIQUE (admission_identity),
    -- THE NATURAL KEY THE L2 GRAIN HAS NEVER HAD. `l2filespk` is
    -- (expid, sca, version) — uniqueness that INCLUDES the version, which is
    -- exactly what lets `addl2file`'s max+1 mint a duplicate admission. This
    -- constraint is at (expid, sca), so a re-ingest of the same detector file
    -- CONFLICTS and returns instead of re-versioning.
    CONSTRAINT admission_l2files_grain_uq UNIQUE (expid, sca),
    CONSTRAINT admission_l2files_rid_uq UNIQUE (rid),
    CONSTRAINT admission_l2files_rid_fk
        FOREIGN KEY (rid) REFERENCES l2files(rid),
    CONSTRAINT admission_l2files_expid_fk
        FOREIGN KEY (expid) REFERENCES exposures(expid),
    CONSTRAINT admission_l2files_manifest_fk
        FOREIGN KEY (manifest_id) REFERENCES admission_manifests(manifest_id),
    CONSTRAINT admission_l2files_release_fk
        FOREIGN KEY (release_identity)
        REFERENCES admission_releases(release_identity),
    CONSTRAINT admission_l2files_identity_ck
        CHECK (admission_identity LIKE 'sha256:%'),
    CONSTRAINT admission_l2files_checksum_ck
        CHECK (length(source_checksum) BETWEEN 32 AND 128),
    CONSTRAINT admission_l2files_algorithm_ck
        CHECK (checksum_algorithm IN ('sha256', 'md5'))
);

CREATE INDEX IF NOT EXISTS admission_l2files_manifest_idx
    ON admission_l2files USING btree (manifest_id);
CREATE INDEX IF NOT EXISTS admission_l2files_release_idx
    ON admission_l2files USING btree (release_identity);

COMMENT ON TABLE admission_l2files IS
    'The admission record for one L2 detector file. Identity is a content key '
    'over (expid, sca) plus the source content checksum — the grain where a '
    'file, and therefore a checksum, actually exists. The (expid, sca) UNIQUE '
    'is the natural key l2files has never had: l2filespk includes the '
    'version, which is what lets addl2file''s max+1 mint a duplicate.';
COMMENT ON COLUMN admission_l2files.source_checksum IS
    'Full-width, with its algorithm recorded. l2files.checksum is '
    'varchar(32) and truncates every SHA-256 (CR-8, unlanded), so admission '
    'identity never reads that column.';

-- ---------------------------------------------------------------------------
-- A sealed manifest is a precondition for citing it. Enforced by trigger
-- rather than by application discipline, for DRAFT 050's stated reason: the
-- table owner and any SECURITY DEFINER function bypass column grants, so a
-- property this load-bearing must not depend on getting a grant map right.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.admission_manifest_must_be_sealed()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    sealed_ timestamptz;
BEGIN
    IF NEW.manifest_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT sealed_at INTO sealed_
    FROM admission_manifests WHERE manifest_id = NEW.manifest_id;
    IF sealed_ IS NULL THEN
        RAISE EXCEPTION
            'admission cites manifest % which is not sealed; a manifest is '
            'sealed only once every entry is durable, so citing an unsealed '
            'one would record an admission against a source that may still '
            'be partial (rule 20)', NEW.manifest_id
            USING ERRCODE = 'RA010';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION derived.admission_manifest_must_be_sealed() IS
    'Refuses an admission that cites an unsealed manifest. The crash-ordering '
    'guarantee rule 20 needs: seal last, so a crash leaves a complete record '
    'or an explicitly unsealed one.';

DROP TRIGGER IF EXISTS admission_exposures_manifest_sealed
    ON admission_exposures;
CREATE TRIGGER admission_exposures_manifest_sealed
    BEFORE INSERT OR UPDATE ON admission_exposures
    FOR EACH ROW EXECUTE FUNCTION derived.admission_manifest_must_be_sealed();

DROP TRIGGER IF EXISTS admission_l2files_manifest_sealed
    ON admission_l2files;
CREATE TRIGGER admission_l2files_manifest_sealed
    BEFORE INSERT OR UPDATE ON admission_l2files
    FOR EACH ROW EXECUTE FUNCTION derived.admission_manifest_must_be_sealed();

-- ---------------------------------------------------------------------------
-- `admitted_at` IS WRITE-ONCE. This is the direct repair of addexposure's
-- `created = now()` overwrite: whatever else an UPDATE does, it cannot rewrite
-- the moment the observation was first admitted.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.admission_admitted_at_is_write_once()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.admitted_at IS DISTINCT FROM OLD.admitted_at THEN
        RAISE EXCEPTION
            'admitted_at is write-once and cannot be rewritten (was %, '
            'attempted %); a repeated observation returns its existing '
            'admission rather than mutating it (rule 20). This is the '
            'invariant addexposure''s update branch breaks with '
            'created = now().', OLD.admitted_at, NEW.admitted_at
            USING ERRCODE = 'RA010';
    END IF;
    IF NEW.admission_identity IS DISTINCT FROM OLD.admission_identity THEN
        RAISE EXCEPTION
            'admission_identity is immutable (was %, attempted %)',
            OLD.admission_identity, NEW.admission_identity
            USING ERRCODE = 'RA010';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION derived.admission_admitted_at_is_write_once() IS
    'Freezes admitted_at and admission_identity against any UPDATE. The '
    'direct repair of addexposure''s created = now() overwrite, which '
    'destroys the original ingest timestamp on every repeat.';

DROP TRIGGER IF EXISTS admission_exposures_admitted_at_frozen
    ON admission_exposures;
CREATE TRIGGER admission_exposures_admitted_at_frozen
    BEFORE UPDATE ON admission_exposures
    FOR EACH ROW
    EXECUTE FUNCTION derived.admission_admitted_at_is_write_once();

DROP TRIGGER IF EXISTS admission_l2files_admitted_at_frozen
    ON admission_l2files;
CREATE TRIGGER admission_l2files_admitted_at_frozen
    BEFORE UPDATE ON admission_l2files
    FOR EACH ROW
    EXECUTE FUNCTION derived.admission_admitted_at_is_write_once();

-- ---------------------------------------------------------------------------
-- The audited release-pointer mutation, under G's full contract.
--
-- Keyed FIRST (p_idempotency_key), per DRAFT 047's stated reason: an argument
-- list is a function's identity in PostgreSQL, and putting the key first means
-- no existing call can bind here by accident.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.set_admission_release(
    p_idempotency_key text,
    p_release_identity text,
    p_reason          text,
    p_expected_state  jsonb   DEFAULT NULL,
    p_dry_run         boolean DEFAULT true,
    p_policy_citation text    DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = derived, public, pg_temp
AS $$
DECLARE
    current_    text;
    replay_     jsonb;
    audit_id_   bigint;
    result_     jsonb;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a reason is mandatory for every operator mutation'
            USING ERRCODE = 'RA001';
    END IF;
    IF p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) = 0 THEN
        RAISE EXCEPTION 'an idempotency key is mandatory'
            USING ERRCODE = 'RA002';
    END IF;

    -- REPLAY BEFORE ANYTHING ELSE. A re-run with the same key returns the
    -- recorded outcome instead of applying a second time (047's shared
    -- lookup; dry runs never consume a key).
    replay_ := derived.mutation_replay(p_idempotency_key);
    IF replay_ IS NOT NULL AND NOT p_dry_run THEN
        RETURN replay_ || jsonb_build_object('replayed', true);
    END IF;

    SELECT release_identity INTO current_
    FROM admission_release_pointer WHERE is_current;

    -- THE POINTER MAY ONLY NAME A KNOWN RELEASE, and the refusal happens
    -- HERE — at mutation time — rather than being discovered at submission
    -- time when work is already in flight.
    IF NOT EXISTS (SELECT 1 FROM admission_releases
                   WHERE release_identity = p_release_identity) THEN
        RAISE EXCEPTION
            'release % is not registered in admission_releases and does not '
            'resolve to an immutable release manifest; register it before '
            'pointing admissions at it', p_release_identity
            USING ERRCODE = 'RA001';
    END IF;

    -- EXPECTED STATE, if the operator stated one.
    IF p_expected_state IS NOT NULL
       AND p_expected_state ? 'current_release'
       AND coalesce(current_, '') IS DISTINCT FROM
           (p_expected_state ->> 'current_release') THEN
        RAISE EXCEPTION
            'expected current release % but found %',
            p_expected_state ->> 'current_release', coalesce(current_, '<none>')
            USING ERRCODE = 'RA001';
    END IF;

    result_ := jsonb_build_object(
        'action', 'set_admission_release',
        'previous_release', current_,
        'requested_release', p_release_identity,
        'changed', (coalesce(current_, '') IS DISTINCT FROM p_release_identity),
        'dry_run', p_dry_run);

    IF p_dry_run THEN
        RETURN result_ || jsonb_build_object('rows_affected', 0);
    END IF;

    audit_id_ := derived.write_mutation_audit(
        'admission_release_set',
        'admission_release_pointer:' || p_release_identity,
        p_reason, 1, p_idempotency_key, p_expected_state, false,
        p_policy_citation);

    -- SUPERSEDE, NEVER REWRITE. The old row keeps its actor, reason and
    -- timestamp; only its current flag clears. A rollback is then visible in
    -- the table as exactly what it was.
    UPDATE admission_release_pointer SET is_current = false WHERE is_current;
    INSERT INTO admission_release_pointer
        (release_identity, is_current, set_by, reason, audit_id)
    VALUES (p_release_identity, true, session_user, p_reason, audit_id_);

    RETURN result_ || jsonb_build_object('rows_affected', 1,
                                         'audit_id', audit_id_);
END;
$$;

COMMENT ON FUNCTION derived.set_admission_release(text, text, text, jsonb,
                                                  boolean, text) IS
    'Switch the release future admissions are stamped with, under the full '
    'mutation contract (rule 16). Dry-run by default; supersedes rather than '
    'rewrites; refuses an unregistered release at mutation time. Never '
    'touches an existing admission''s stamp or any in-flight work — that '
    'separation is rule 18''s rollback clause.';

-- ---------------------------------------------------------------------------
-- Grants. DRAFT 050's posture, not 048's blanket one, and guarded on role
-- existence so this file still applies to a bare scratch database.
--
-- The pipeline write role may INSERT admissions and manifests (that is what
-- ingest does) and READ the pointer (it must, to stamp). It may NOT write the
-- pointer or the release registry: a job that could switch the release could
-- silently escape the pin it is supposed to obey.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_pipeline_write')
    THEN
        GRANT SELECT, INSERT ON admission_manifests,
            admission_manifest_entries, admission_exposures,
            admission_l2files TO rapid_pipeline_write;
        -- Sealing a manifest is an UPDATE of exactly one column.
        GRANT UPDATE (sealed_at, entry_count, entries_checksum)
            ON admission_manifests TO rapid_pipeline_write;
        GRANT SELECT ON admission_release_pointer, admission_releases
            TO rapid_pipeline_write;
        -- STATED EXPLICITLY so a later blanket grant written by habit does
        -- not silently widen this: the pipeline never moves the pointer.
        REVOKE INSERT, UPDATE, DELETE ON admission_release_pointer
            FROM rapid_pipeline_write;
        REVOKE INSERT, UPDATE, DELETE ON admission_releases
            FROM rapid_pipeline_write;
        REVOKE DELETE ON admission_manifests, admission_manifest_entries,
            admission_exposures, admission_l2files FROM rapid_pipeline_write;
        GRANT USAGE, SELECT ON SEQUENCE
            admission_manifests_manifest_id_seq,
            admission_exposures_admission_id_seq,
            admission_l2files_admission_id_seq TO rapid_pipeline_write;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT SELECT ON admission_manifests, admission_manifest_entries,
            admission_exposures, admission_l2files,
            admission_release_pointer, admission_releases TO rapid_read;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_orchestrator')
    THEN
        GRANT EXECUTE ON FUNCTION derived.set_admission_release(
            text, text, text, jsonb, boolean, text) TO rapid_orchestrator;
    END IF;
END;
$$;

COMMIT;
