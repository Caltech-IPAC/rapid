-- 050-alert-outbox-and-publisher.sql — the transactional outbox rule 14
-- requires between alert production and the broker, the delivery policy that
-- gates a release's packets, and the `rapid_publisher` service role that
-- drains the outbox.
--
-- DRAFT. Staged in the `rapid` repository under `migrations-draft/` as a
-- proposed change request against this stream; `rapid_systems` owns the schema
-- and this file is not applied by `apply-db-migrations.sh` until its owner
-- adopts it. See that directory's README. Requires 037 (the emission state
-- model whose confirm CAS commits alongside these rows) and DRAFT 048 (the
-- `products`/`diffimages.product_id` binding the product-key identity basis
-- joins through) — apply in order.
--
-- Conformance rule 14, verbatim:
--
--     "Alert packets are written to a transactional outbox in the same
--      transaction as the database effect that produced them; a separate
--      publisher delivers them at-least-once with identical bytes on resend;
--      corrections are new linked events, never rewrites."
--
-- and §2.3 of the minimal viable target ("Controller and publisher"), whose
-- row-claim language this table's state machine implements:
--
--     "The publisher claims rows rather than scanning them, so a second
--      instance is safe by construction even though one runs."
--
-- ============================================================================
-- WHAT "THE SAME TRANSACTION" MEANS HERE, STATED PRECISELY
-- ============================================================================
--
-- Rule 14 says "the same transaction as the database effect that produced
-- them". In the current topology that effect is the ALERT-EFFECT CONFIRMATION
-- transaction — the `transaction(conn)` block in
-- `pipeline/stages/alert_production.py` holding the `alert_emissions` confirm
-- CAS and the `alert_published` milestone. The outbox rows commit there.
--
-- This is deliberately NOT rule 9's result-acceptance transaction. The
-- attempt's terminal record and its closure happen later, in the termination
-- protocol (`pipeline/entrypoints/job.py`, `pipeline/runtime/termination.py`),
-- and the registration consumer that owns that transaction CANNOT construct
-- these packets: it has no provider, no cutouts and no schema. So the outbox
-- write is an ADDITIONAL atomic database-effect transaction at the practical
-- location, and the remaining rule-9 architectural gap — outbox rows are
-- atomic with the emission confirmation, not with attempt acceptance — is
-- recorded here rather than papered over. A future topology that moves packet
-- construction behind acceptance closes it; this migration does not claim to.
--
-- THE ORDER INSIDE THAT TRANSACTION IS FIXED, and the application enforces it:
-- confirm CAS first, verify this attempt still owns the claim token, and ONLY
-- THEN insert outbox rows and the milestone. The confirm CAS can affect zero
-- rows without raising (a takeover is a recorded no-op), so a losing claimant
-- that inserted first would leave outbox rows behind for an emission it did
-- not confirm. It commits neither.
--
-- ============================================================================
-- WHY THE PAYLOAD, ITS CHECKSUM AND THE SCHEMA VERSION ARE ALL STORED
-- ============================================================================
--
-- "Identical bytes on resend" is a claim about the WIRE, and the wire bytes
-- are not the payload bytes. The producer frames the schemaless Avro payload
-- with a Glue header carrying the registry's schema-version UUID
-- (`alerts/kafka_producer.py`, `frame_alert`), and today it looks that UUID up
-- as the registry's LATEST version at publish time
-- (`SchemaVersionNumber={"LatestVersion": True}`). Re-framing the same payload
-- after a registry bump therefore yields DIFFERENT wire bytes.
--
-- So the row stores the payload bytes, their checksum, AND the schema-version
-- UUID pinned at outbox-write time. The publisher frames strictly from these
-- stored fields and performs NO registry lookup on the send path, which is
-- what makes a resend byte-identical at the wire even after the registry
-- moves. The pinned UUID is as immutable as the payload, and is protected by
-- the same trigger below: a row whose UUID could be rewritten would silently
-- change the bytes a resend produces, which is the exact property rule 14
-- names.
--
-- ============================================================================
-- AT-LEAST-ONCE, AND WHY THE CRASH WINDOWS ARE NOT DISTINGUISHED
-- ============================================================================
--
-- Broker acknowledgements arrive in the publisher's process-local delivery
-- callback. There is NO durable ack evidence before finalization — nothing is
-- written between the claim and the `SENT` update. A crash before the send,
-- after the send but before the ack, and after the ack but before the `SENT`
-- update are therefore INDISTINGUISHABLE from the database afterwards, and
-- all three recover the same way: the lease expires, the row is reclaimed, and
-- the identical bytes go out again under the identical key.
--
-- That is the at-least-once contract, stated as a property of this schema
-- rather than left as an accident of it. Consumers deduplicate on `alert_id`,
-- which is why the message key IS `alert_id`. Attempting to distinguish the
-- windows would require an ack write on the send path — a durable write per
-- message, in the hot path, to buy a guarantee (exactly-once) the target
-- explicitly does not make ("No distributed transactions").

BEGIN;

-- ============================================================================
-- alert_outbox — one row per alert packet, written by the pipeline, drained
-- by the publisher
-- ============================================================================
--
-- `alert_id` is the packet's deterministic identity: a digest over the
-- identity-basis discriminator, the source difference image's identity under
-- that basis, the detection's CATALOG key (`id`, `isdiffpos`) and the release
-- identity, computed by `alerts/identity.py` with the canonical-serialization
-- discipline DRAFT 048's product keys already use (versioned serialization,
-- forbidden-inputs guard).
--
-- NOT `sid`. `sid` is DB-generated at catalog load — the COPY column list in
-- `pipeline/stages/post_db.py` does not carry it — so it is realization-local:
-- the same detection reloaded gets a different `sid`, and an identity built on
-- it would not survive a reload. The catalog's own conflict identity is
-- `(pid, id, isdiffpos)` (`pipeline/stages/catalog_db.py`), where `id` is a
-- per-file ordinal and the sign is part of the identity because a product has
-- a positive and a negative file. That triple, with the image named by its
-- basis rather than by `pid`, is what the digest covers.
CREATE TABLE IF NOT EXISTS alert_outbox (
    outbox_id       bigint GENERATED BY DEFAULT AS IDENTITY,
    -- The packet identity, and the Kafka message key. UNIQUE is the
    -- idempotency fence for the outbox write: a re-run of a lost-response
    -- attempt recomputes the same digests and collides here rather than
    -- writing a second copy.
    alert_id        text NOT NULL,
    -- Which identity basis produced `alert_id`. Stored as a column for
    -- queryability, but it is ALSO inside the hashed representation, so the
    -- two bases are namespace-separated by construction and not merely by
    -- this column's value.
    identity_basis  text NOT NULL,
    -- ---- THE DISPATCH ENVELOPE: write-once, trigger-enforced below ---------
    -- The schemaless Avro bytes `alerts/produce.py:serialize_alert` emits.
    payload         bytea NOT NULL,
    -- sha256 of `payload`, stored with its algorithm prefix like every other
    -- content digest in this schema. Redundant with the bytes BY DESIGN: it is
    -- what the collision guard compares, so a same-id insert with different
    -- content fails on a cheap text comparison rather than a bytea one, and an
    -- operator can see two rows differ without reading megabytes of cutouts.
    payload_checksum text NOT NULL,
    -- The Glue registry's schema-version UUID, PINNED at outbox-write time.
    -- The publisher frames with exactly this and never asks the registry, so
    -- a registry bump between the first send and a resend cannot change the
    -- bytes. Write-once for that reason.
    schema_version_id uuid NOT NULL,
    -- The destination topic, resolved at outbox-write time from the parameter
    -- tree. Stored per row rather than read by the publisher from its own
    -- configuration: the packet was built for a topic, and a publisher
    -- reconfigured between write and send must not redirect packets already
    -- committed.
    topic           text NOT NULL,
    release_identity text NOT NULL,
    -- ---- PROVENANCE -------------------------------------------------------
    -- The emission this packet belongs to (037's `alert_emissions` primary
    -- key) and the attempt that wrote it. Provenance, never identity: none of
    -- these enters `alert_id`.
    exposure_id     bigint NOT NULL,
    sca             smallint NOT NULL,
    producing_attempt_id bigint,
    -- Rule 14's corrections-as-new-linked-events, SCHEMA SUPPORT ONLY. No
    -- production path mints a correction today, and none is added by this
    -- change: a correction is a NEW row whose `corrects_alert_id` names the
    -- packet it supersedes, never a rewrite of that packet — which is exactly
    -- why the envelope above is write-once. Self-referential by `alert_id`
    -- (the natural key) rather than by `outbox_id`, so a correction can name
    -- a packet whose row has been archived out of this table.
    corrects_alert_id text,
    -- ---- THE STATE MACHINE ------------------------------------------------
    -- PENDING -> (atomic claim) IN_FLIGHT -> SENT
    --                                     -> PENDING (ambiguous ack, resend)
    --                                     -> REFUSED (definite refusal)
    state           text NOT NULL DEFAULT 'PENDING',
    -- Set together, by the claim, and cleared together by finalization. The
    -- CHECK below makes "claimed rows carry a token and a time, unclaimed
    -- rows carry neither" a database fact rather than a convention, the same
    -- shape 037's `alert_emissions_claim_shape_ck` uses.
    claim_token     text,
    claimed_at      timestamptz,
    -- Ack metadata, written by the finalizing transaction. `broker_metadata`
    -- is whatever the transport reported (partition, offset); it is a record,
    -- never read back as control state.
    sent_at         timestamptz,
    broker_metadata jsonb,
    -- Why a REFUSED row was refused, and how many times an ambiguous ack has
    -- sent this packet again. `resend_count` counts SENDS BEYOND THE FIRST:
    -- it is 0 on a packet delivered on its first attempt.
    refusal_reason  text,
    resend_count    integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT alert_outbox_pkey PRIMARY KEY (outbox_id),
    -- THE IDEMPOTENCY FENCE, enforced by the database rather than promised by
    -- the application (acceptance 1).
    CONSTRAINT alert_outbox_alert_id_uq UNIQUE (alert_id),
    CONSTRAINT alert_outbox_basis_ck
        CHECK (identity_basis IN ('product-key', 'legacy-pid')),
    CONSTRAINT alert_outbox_state_ck
        CHECK (state IN ('PENDING', 'IN_FLIGHT', 'SENT', 'REFUSED')),
    CONSTRAINT alert_outbox_checksum_shape_ck
        CHECK (payload_checksum ~ '^sha256:[0-9a-f]{64}$'),
    -- A claimed row carries the claim; every other state carries none. The
    -- orphan-recovery query reads `claimed_at` to decide reclaimability, so a
    -- row that could be IN_FLIGHT without one would be unreclaimable forever.
    CONSTRAINT alert_outbox_claim_shape_ck
        CHECK ((state = 'IN_FLIGHT'
                AND claim_token IS NOT NULL AND claimed_at IS NOT NULL)
            OR (state <> 'IN_FLIGHT'
                AND claim_token IS NULL AND claimed_at IS NULL)),
    -- A SENT row has a send time; nothing else does. REFUSED carries a reason.
    CONSTRAINT alert_outbox_sent_shape_ck
        CHECK ((state = 'SENT' AND sent_at IS NOT NULL)
            OR (state <> 'SENT' AND sent_at IS NULL)),
    CONSTRAINT alert_outbox_refusal_shape_ck
        CHECK ((state = 'REFUSED' AND refusal_reason IS NOT NULL)
            OR (state <> 'REFUSED' AND refusal_reason IS NULL)),
    CONSTRAINT alert_outbox_resend_count_ck CHECK (resend_count >= 0),
    -- A correction names a different packet, never itself.
    CONSTRAINT alert_outbox_corrects_self_ck
        CHECK (corrects_alert_id IS NULL OR corrects_alert_id <> alert_id)
);

-- THE SEND ORDER, AS AN INDEX. `(created_at, alert_id)` — a real tie-breaker,
-- not `created_at` alone: outbox rows are written in one transaction and share
-- a `now()` to the microsecond, so `ORDER BY created_at` over a batch is
-- underdetermined and two publisher cycles could disagree about which row is
-- "next". Partial on PENDING because that is the only state the claim scans.
CREATE INDEX IF NOT EXISTS alert_outbox_pending_order_idx
    ON alert_outbox USING btree (created_at, alert_id)
    WHERE state = 'PENDING';

-- Orphan recovery reads IN_FLIGHT rows by claim age.
CREATE INDEX IF NOT EXISTS alert_outbox_inflight_claimed_idx
    ON alert_outbox USING btree (claimed_at)
    WHERE state = 'IN_FLIGHT';

-- The health view's per-release rollup, and the policy join.
CREATE INDEX IF NOT EXISTS alert_outbox_release_state_idx
    ON alert_outbox USING btree (release_identity, state);

-- Provenance lookup: "which packets did this emission produce".
CREATE INDEX IF NOT EXISTS alert_outbox_emission_idx
    ON alert_outbox USING btree (exposure_id, sca, release_identity);

COMMENT ON TABLE alert_outbox IS
    'Rule 14''s transactional outbox: one row per alert packet, inserted in '
    'the alert-effect confirmation transaction alongside the alert_emissions '
    'confirm CAS and the alert_published milestone, and drained by the '
    'rapid-publisher process. Identity/payload/basis/schema-version columns '
    'are write-once against every role including the owner (trigger below); '
    'corrections are new rows linked by corrects_alert_id, never rewrites.';
COMMENT ON COLUMN alert_outbox.alert_id IS
    'The packet''s deterministic identity and its Kafka message key: a digest '
    'over (identity_basis, the difference image''s identity under that basis, '
    'the catalog key (id, isdiffpos), release_identity). Never derived from '
    'sid, which is realization-local (DB-generated at catalog load).';
COMMENT ON COLUMN alert_outbox.identity_basis IS
    'Which image identity alert_id was computed over: product-key (the '
    'difference image''s DRAFT 048 product_key) or legacy-pid (pre-D rows '
    'with no product binding, where the legacy pid is the only image identity '
    'of record). Immutable per row: a difference image later gaining a '
    'product binding does NOT re-mint identities for already-outboxed '
    'packets. Also inside the hashed representation, so the bases are '
    'namespace-separated by construction.';
COMMENT ON COLUMN alert_outbox.schema_version_id IS
    'The Glue schema-version UUID pinned at outbox-write time. The publisher '
    'frames with this and never looks the registry up on the send path, which '
    'is what makes a resend byte-identical after a registry version bump.';
COMMENT ON COLUMN alert_outbox.resend_count IS
    'Sends beyond the first, incremented when an ambiguous acknowledgement '
    'returns the row to PENDING. Zero on a packet acknowledged on its first '
    'send.';
COMMENT ON COLUMN alert_outbox.corrects_alert_id IS
    'Rule 14''s corrections-as-new-linked-events. SCHEMA SUPPORT ONLY: no '
    'production path mints corrections as of this migration.';

-- ============================================================================
-- Envelope immutability, enforced by trigger against EVERY role
-- ============================================================================
--
-- Following 030's append-only pattern, and for the same reason it gives: the
-- guarantee must not depend on getting a grant map right. Column grants alone
-- are not sufficient here — the table owner and any SECURITY DEFINER function
-- would bypass them, and the pipeline role's INSERT-only posture is a
-- separate mechanism that says nothing about what the publisher or a future
-- role may rewrite.
--
-- This is narrower than 030's: rows are NOT append-only, because the state
-- machine must move PENDING -> IN_FLIGHT -> SENT. What is frozen is the
-- DISPATCH ENVELOPE — the identity, the bytes, the basis and the pinned
-- schema version. Those decide what goes on the wire; the state columns
-- decide only whether and when.
CREATE OR REPLACE FUNCTION reject_alert_outbox_envelope_rewrite()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.alert_id IS DISTINCT FROM OLD.alert_id
       OR NEW.identity_basis IS DISTINCT FROM OLD.identity_basis
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_checksum IS DISTINCT FROM OLD.payload_checksum
       OR NEW.schema_version_id IS DISTINCT FROM OLD.schema_version_id
       OR NEW.topic IS DISTINCT FROM OLD.topic
       OR NEW.release_identity IS DISTINCT FROM OLD.release_identity
       OR NEW.corrects_alert_id IS DISTINCT FROM OLD.corrects_alert_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
          'alert_outbox dispatch envelope is write-once (rule 14: a resend '
          'delivers identical bytes under an identical key, which a rewritable '
          'payload, checksum, pinned schema version or topic would silently '
          'break); attempted % on alert_id %', TG_OP, OLD.alert_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS alert_outbox_envelope_write_once ON alert_outbox;
CREATE TRIGGER alert_outbox_envelope_write_once
    BEFORE UPDATE ON alert_outbox
    FOR EACH ROW EXECUTE FUNCTION reject_alert_outbox_envelope_rewrite();

-- DELIVERED ROWS ARE UNDELETABLE BY ANYONE. A SENT row is the evidence that a
-- packet went to consumers and a REFUSED row is the evidence that one
-- deliberately did not; deleting either would erase the only record that the
-- delivery decision was made. The trigger is the backstop rather than the
-- grant, exactly as 030 argues: the owner would otherwise be exempt, and the
-- acceptance asserts this holds for EVERY role.
--
-- PENDING and IN_FLIGHT rows remain deletable by the owner — an operator
-- draining a mis-built batch before it ships has a legitimate reason, and no
-- consumer has seen those bytes. The pipeline role cannot delete any row at
-- all, which is a grant (below), not this trigger.
CREATE OR REPLACE FUNCTION reject_delivered_alert_outbox_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
      'alert_outbox row % is % and cannot be deleted (the delivery decision '
      'is the record); attempted %', OLD.alert_id, OLD.state, TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS alert_outbox_delivered_undeletable ON alert_outbox;
CREATE TRIGGER alert_outbox_delivered_undeletable
    BEFORE DELETE ON alert_outbox
    FOR EACH ROW WHEN (OLD.state IN ('SENT', 'REFUSED'))
    EXECUTE FUNCTION reject_delivered_alert_outbox_delete();

-- SAME-ID COLLISION WITH A DIFFERENT ENVELOPE IS A HARD INVARIANT VIOLATION.
--
-- The insert path never says `ON CONFLICT DO NOTHING`. Two packets that agree
-- on `alert_id` must agree on their bytes, because `alert_id` is a digest over
-- what the packet IS: a collision with a different checksum or a different
-- pinned schema version means either the digest inputs are incomplete or two
-- different packets were minted under one identity, and both are defects that
-- a silent no-op would hide until consumers received contradictory bytes under
-- one key.
--
-- A collision with an IDENTICAL envelope is the ordinary idempotent re-run and
-- is absorbed: the row is already there and is already correct. That is the
-- distinction this function draws, and it is why the insert goes through a
-- function rather than a bare statement — the comparison has to happen
-- somewhere the application cannot forget to write.
CREATE OR REPLACE FUNCTION insert_alert_outbox_packet(
    p_alert_id text,
    p_identity_basis text,
    p_payload bytea,
    p_payload_checksum text,
    p_schema_version_id uuid,
    p_topic text,
    p_release_identity text,
    p_exposure_id bigint,
    -- `integer`, NOT `smallint`, even though the COLUMN is smallint.
    --
    -- PostgreSQL resolves function calls by parameter type and will not
    -- implicitly narrow integer -> smallint to find a candidate, so a caller
    -- passing a plain Python int through psycopg2 — which binds it as
    -- `integer` — gets "function insert_alert_outbox_packet(..., integer,
    -- ...) does not exist", a message that names the function it is looking
    -- at and reads as though the migration never applied. Observed live on
    -- this branch's second acceptance run, where the function was present and
    -- every call site failed.
    --
    -- Declaring the parameter wider than the column is the right way round:
    -- the assignment to the smallint column below still range-checks the
    -- value, so an out-of-range SCA is refused by the column rather than
    -- silently accepted — the check moves, it does not disappear.
    p_sca integer,
    p_producing_attempt_id bigint,
    p_corrects_alert_id text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    existing alert_outbox%ROWTYPE;
BEGIN
    INSERT INTO alert_outbox (
        alert_id, identity_basis, payload, payload_checksum,
        schema_version_id, topic, release_identity, exposure_id, sca,
        producing_attempt_id, corrects_alert_id)
    VALUES (
        p_alert_id, p_identity_basis, p_payload, p_payload_checksum,
        p_schema_version_id, p_topic, p_release_identity, p_exposure_id,
        p_sca, p_producing_attempt_id, p_corrects_alert_id)
    ON CONFLICT (alert_id) DO NOTHING;

    IF FOUND THEN
        RETURN 'inserted';
    END IF;

    -- The conflict arm. Read what is there and INSIST it is the same packet.
    SELECT * INTO existing FROM alert_outbox WHERE alert_id = p_alert_id;
    IF existing.payload_checksum IS DISTINCT FROM p_payload_checksum THEN
        RAISE EXCEPTION
          'alert_id % already exists with payload checksum % but was offered '
          '% — one identity, two different packets: either the digest inputs '
          'are incomplete or two packets were minted under one identity',
          p_alert_id, existing.payload_checksum, p_payload_checksum;
    END IF;
    IF existing.schema_version_id IS DISTINCT FROM p_schema_version_id THEN
        RAISE EXCEPTION
          'alert_id % already exists pinned to schema version % but was '
          'offered % — the pinned version is part of the dispatch envelope '
          'and is write-once, because a resend must reproduce the first '
          'send''s wire bytes exactly',
          p_alert_id, existing.schema_version_id, p_schema_version_id;
    END IF;
    IF existing.identity_basis IS DISTINCT FROM p_identity_basis
       OR existing.topic IS DISTINCT FROM p_topic
       OR existing.release_identity IS DISTINCT FROM p_release_identity THEN
        RAISE EXCEPTION
          'alert_id % already exists with a different dispatch envelope '
          '(basis %/% topic %/% release %/%)',
          p_alert_id, existing.identity_basis, p_identity_basis,
          existing.topic, p_topic,
          existing.release_identity, p_release_identity;
    END IF;
    RETURN 'idempotent';
END;
$$;

COMMENT ON FUNCTION insert_alert_outbox_packet(text, text, bytea, text, uuid,
    text, text, bigint, integer, bigint, text) IS
    'The outbox insert path. Absorbs an identical re-insert (the idempotent '
    're-run after a lost response) and RAISES on a same-alert_id collision '
    'whose payload checksum, pinned schema version or any other envelope '
    'field differs. Never ON CONFLICT DO NOTHING at the call site: the '
    'comparison is the invariant.';

-- ============================================================================
-- delivery_policies — which releases may be delivered, checked before EVERY
-- send including resends
-- ============================================================================
--
-- Authorization is per release identity and default-DENY: a release with no
-- row here is unauthorized, so a new release cannot start delivering because
-- someone forgot to write a policy. The live prompt release is authorized by
-- this migration because it is the release the target's §2.1 topology exists
-- to deliver; everything else is an operator decision with a reason and an
-- actor attached.
--
-- CHECKED BY THE PUBLISHER, immediately before every send. Not at claim time
-- and not once per cycle: a revocation between an ambiguous first send and its
-- resend must prevent the resend, which is only true if the check is on the
-- send path itself.
CREATE TABLE IF NOT EXISTS delivery_policies (
    release_identity text NOT NULL,
    authorized      boolean NOT NULL,
    reason          text NOT NULL,
    -- Actor provenance: who decided, and when. Text rather than a role FK
    -- because the actor may be a person, a change request or a migration, and
    -- this table records the decision, not an account.
    actor           text NOT NULL,
    decided_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT delivery_policies_pkey PRIMARY KEY (release_identity)
);

COMMENT ON TABLE delivery_policies IS
    'Per-release delivery authorization, default-DENY (absence is refusal). '
    'Checked by the publisher immediately before every send, including '
    'resends, so a revocation between an ambiguous send and its resend '
    'prevents the resend.';

-- The live prompt release, authorized by the migration per the brief. The
-- INSERT is ON CONFLICT DO NOTHING so a re-application converges rather than
-- overwriting an operator's later decision about this same release — which is
-- the one row in this table a migration has any business asserting.
INSERT INTO delivery_policies (release_identity, authorized, reason, actor)
VALUES ('live', true,
        'The live prompt release is the stream the minimal-viable-target '
        'topology exists to deliver; authorized at schema time so the '
        'delivery path is not gated on an operator action that has no '
        'decision content.',
        'migration:050-alert-outbox-and-publisher')
ON CONFLICT (release_identity) DO NOTHING;

-- ============================================================================
-- Outbox health, for §2.8's clocks
-- ============================================================================
--
-- The target names four latency clocks; two of them are outbox clocks —
-- acceptance→outbox and outbox→broker acknowledgement. Neither had anything to
-- read before this migration (`pipeline/registration/consumer.py` records
-- exactly that: "no query, no view, no dashboard, no latency clock"). These
-- views are what a clock reads.
--
-- Views rather than materialized ones: the outbox is small by construction (it
-- drains continuously) and a stale reading of a liveness signal is worse than
-- a slightly expensive fresh one.
CREATE OR REPLACE VIEW alert_outbox_health AS
SELECT
    o.release_identity,
    o.state,
    -- Whether these rows can go anywhere at all, so "held" is visible as a
    -- state of the world rather than inferred from a stalled PENDING count.
    COALESCE(p.authorized, false) AS release_authorized,
    count(*)                      AS packets,
    min(o.created_at)             AS oldest_created_at,
    max(o.created_at)             AS newest_created_at,
    -- acceptance→outbox is measured at the write: `created_at` IS the moment
    -- the confirmation transaction committed this packet. The age of the
    -- oldest undelivered row is the operational form of that clock.
    max(now() - o.created_at)     AS oldest_age,
    -- outbox→acknowledgement, derivable per the brief: the delivered rows
    -- carry both ends of the interval.
    max(o.sent_at - o.created_at) AS max_delivery_latency,
    avg(o.sent_at - o.created_at) AS avg_delivery_latency,
    sum(o.resend_count)           AS resends
FROM alert_outbox o
LEFT JOIN delivery_policies p USING (release_identity)
GROUP BY o.release_identity, o.state, COALESCE(p.authorized, false);

COMMENT ON VIEW alert_outbox_health IS
    'Per (release, state) outbox rollup for the §2.8 clocks: '
    'acceptance→outbox as the age of the oldest undelivered packet '
    '(created_at is the confirmation transaction''s commit moment), and '
    'outbox→acknowledgement as sent_at - created_at over delivered rows. '
    'Carries the release''s authorization so held packets are visible as '
    'held rather than as a stalled backlog.';

-- Rows whose claim has outlived the lease: the reclaimable orphans. A view
-- rather than a literal in the publisher's SQL so an operator can see what the
-- publisher will pick up next, and so the lease interval has one home.
CREATE OR REPLACE VIEW alert_outbox_stale_claims AS
SELECT alert_id, claim_token, claimed_at, resend_count,
       now() - claimed_at AS claim_age
FROM alert_outbox
WHERE state = 'IN_FLIGHT'
  AND claimed_at < now() - interval '5 minutes';

COMMENT ON VIEW alert_outbox_stale_claims IS
    'IN_FLIGHT rows whose claim has outlived the 5-minute lease and are '
    'reclaimable. A crash anywhere between claim and finalization lands a row '
    'here; recovery always produces an identical-key, identical-bytes resend, '
    'because there is no durable ack evidence before finalization and the '
    'crash windows are therefore indistinguishable (at-least-once).';

-- ============================================================================
-- The rapid_publisher service role
-- ============================================================================
--
-- Same shape as 010-pipeline-service-role.sql and
-- 016-orchestrator-service-role.sql: created NOLOGIN and passwordless
-- (migrations never carry credentials); the rapid-db-config association pass
-- flips it to LOGIN and sets the password from Secrets Manager. Until that
-- runs the role cannot authenticate at all, by construction.
--
-- NO `SET ROLE rapid_admin` around the role creation, per 016's recorded
-- rehearsal failure: rapid_admin is a NOLOGIN group role with object
-- privileges and no CREATEROLE, so wrapping this in SET ROLE fails with
-- "permission denied to create role".
--
-- MEMBERSHIP: NONE. This is where this role departs from 016, deliberately.
-- The orchestrator joined `rapid_pipeline_write` because it writes exactly
-- what the payload writes. The publisher does not: it reads two tables and
-- updates state columns on one of them, and it must NOT reach attempts,
-- work_units, products or anything else. So it gets direct grants of exactly
-- what it needs and no group membership at all — a narrower boundary than a
-- group can express, for a process that touches the outside world.
--
-- THE PUBLISHER CONNECTS DIRECTLY AS THIS ROLE — no `SET ROLE` from another
-- identity. §2.2 of the target puts the publisher on the TRANSACTION-mode
-- pool, and `SET ROLE` needs a session lane: PgBouncer's transaction lane
-- hands the underlying server connection to whoever needs it next between
-- statements, which would drop the role mid-cycle and could leave it set on a
-- connection handed to a stranger (`pipeline/operatorctl/session.py` records
-- this exact reasoning for rapidctl, which is why rapidctl uses the session
-- lane and the publisher cannot).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_publisher') THEN
        CREATE ROLE rapid_publisher NOLOGIN;
    END IF;
END
$$;

COMMENT ON ROLE rapid_publisher IS
    'Service login role for the rapid-publisher process, which drains '
    'alert_outbox to the broker. NO group membership by design: direct '
    'grants of SELECT on alert_outbox + delivery_policies and UPDATE on the '
    'outbox state/claim/ack columns only — no attempts, work_units or '
    'products access, no INSERT or DELETE on the outbox, and no write to '
    'delivery_policies. Created NOLOGIN and passwordless; the rapid-db-config '
    'association pass sets LOGIN and the password from '
    'rapid/db/service/publisher. Connects DIRECTLY as this role (no SET '
    'ROLE): the publisher is transaction-mode pooled and SET ROLE needs a '
    'session lane.';

-- ============================================================================
-- Grants
-- ============================================================================
--
-- Guarded on role existence so the file still applies to a scratch database
-- built without 001's roles, which is exactly what the contract tier's
-- throwaway container is (048's own comment, and the same guard).
--
-- THE PIPELINE WRITER IS INSERT-ONLY ON THE OUTBOX, and this is where DRAFT
-- 048's posture is deliberately NOT copied. 048 grants
-- INSERT, UPDATE, DELETE on its tables to `rapid_pipeline_write`; the outbox
-- is different by design. The pipeline writes packets and then has no further
-- business with them: it must not be able to move a row's state (that is the
-- publisher's protocol and racing it would break the claim), must not touch
-- ack columns (it has no acknowledgements), and must not delete rows —
-- INCLUDING PENDING ones, because a pipeline bug that deleted its own
-- undelivered packets would look exactly like alerts that were never
-- produced.
--
-- The ALL-UPDATE and ALL-DELETE revokes are stated explicitly rather than
-- left implicit in "we only granted INSERT": these tables are created fresh
-- here so no prior grant exists, but the revoke states the intent for anyone
-- reading the grant map or adding a later blanket grant, and it makes the
-- posture survive a future `GRANT ALL ... TO rapid_pipeline_write` written by
-- someone following 048's pattern by habit.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_read') THEN
        GRANT SELECT ON alert_outbox, delivery_policies TO rapid_read;
        GRANT SELECT ON alert_outbox_health, alert_outbox_stale_claims
            TO rapid_read;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_pipeline_write')
    THEN
        -- INSERT ONLY. No UPDATE of any column, no DELETE of any row.
        GRANT SELECT, INSERT ON alert_outbox TO rapid_pipeline_write;
        REVOKE UPDATE, DELETE ON alert_outbox FROM rapid_pipeline_write;
        -- The insert path is a function; the writer must be able to call it.
        GRANT EXECUTE ON FUNCTION insert_alert_outbox_packet(text, text,
            bytea, text, uuid, text, text, bigint, integer, bigint, text)
            TO rapid_pipeline_write;
        -- The pipeline reads policies (it records whether a release is
        -- authorized alongside the packet) but never writes them.
        GRANT SELECT ON delivery_policies TO rapid_pipeline_write;
        REVOKE INSERT, UPDATE, DELETE ON delivery_policies
            FROM rapid_pipeline_write;
        -- The identity column is GENERATED BY DEFAULT, so the sequence is
        -- only touched when the caller omits outbox_id — which the insert
        -- function does.
        GRANT USAGE, SELECT ON SEQUENCE alert_outbox_outbox_id_seq
            TO rapid_pipeline_write;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rapid_publisher') THEN
        GRANT SELECT ON alert_outbox, delivery_policies TO rapid_publisher;
        GRANT SELECT ON alert_outbox_health, alert_outbox_stale_claims
            TO rapid_publisher;
        -- COLUMN-LEVEL UPDATE: the state machine and the ack record, nothing
        -- else. Column grants are not how most of this schema is governed
        -- (016 says so explicitly), but here the boundary IS a column
        -- boundary: the publisher's whole contract is that it moves state and
        -- never changes what goes on the wire. The trigger above is the
        -- backstop that makes this true even if this grant is later widened.
        GRANT UPDATE (state, claim_token, claimed_at, sent_at,
                      broker_metadata, refusal_reason, resend_count)
            ON alert_outbox TO rapid_publisher;
        -- Explicitly NOT granted, stated so the grant map reads as intent:
        -- no INSERT, no DELETE on alert_outbox; no write of any kind to
        -- delivery_policies.
        REVOKE INSERT, DELETE ON alert_outbox FROM rapid_publisher;
        REVOKE INSERT, UPDATE, DELETE ON delivery_policies
            FROM rapid_publisher;
    END IF;
END
$$;

-- PostgreSQL grants EXECUTE on a newly created function to PUBLIC by default,
-- so without these REVOKEs every role shows up holding EXECUTE on the trigger
-- functions and on the insert path — which would make the grant map lie (030's
-- own finding, caught by the rehearsal's grant query rather than by
-- inspection). The trigger functions are revoked from PUBLIC outright; the
-- insert function is revoked from PUBLIC and re-granted above to the one role
-- that may call it.
REVOKE ALL ON FUNCTION reject_alert_outbox_envelope_rewrite() FROM PUBLIC;
REVOKE ALL ON FUNCTION reject_delivered_alert_outbox_delete() FROM PUBLIC;
REVOKE ALL ON FUNCTION insert_alert_outbox_packet(text, text, bytea, text,
    uuid, text, text, bigint, integer, bigint, text) FROM PUBLIC;

COMMIT;
