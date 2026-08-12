# DRAFT migrations — briefs C, G and D

**These files are not the schema. They are proposed change requests against
`IPAC-SW/rapid_systems`, staged here because this repository cannot edit that
one.**

`rapid_systems/cloudformation/db-migrations/` is the authoritative migration
stream and `rapid_systems` owns it: the contract tier consumes that directory
read-only at a pinned revision, "never vendored into this repository and never
edited here", because a local copy that drifted would make every result in
that tier a statement about a database nobody deploys. Brief C needs new
schema for two of its four work items, so the drafts live here — on this
branch, outside the applier's path, numbered to follow the stream's current
head — and become change requests against `rapid_systems` for its owner to
review and apply.

Stream head when these were written: **043**
(`rapid_systems` at `e2b5ebcf3eb33e1bb3afd9b392525ac1507ce62d`; still 043 at
`83f1a38283167132654706ea092d047312f35d4b`, the revision brief D ran
against — 44 stream files at both). The drafts are numbered 044 onward to
follow it. If the stream advances past 043 before these are adopted, they
are renumbered on adoption — the numbers here record what they were written
against, not a claim on those slots.

The acceptance run reported `rapid_systems` at
`3d7c6b420fa3b0cedff7276ec8b10ef2ac574478`, one commit ahead: that repo
advanced while this work was in progress. The migration stream is IDENTICAL at
both revisions — `3d7c6b4` touches only
`cloudformation/rapid-{reconciler,vpo}-service.yaml`, no file under
`db-migrations/`, and both revisions carry the same 44 stream files — so the
schema these drafts were written against and the schema they were accepted
against are the same schema, and 044-046 remain the next free numbers.

| Draft | Purpose | Brief item |
|---|---|---|
| `044-submission-protocol.sql` | `submissions` table: the durable PREPARED → CALLING → BOUND / UNKNOWN → FOUND / LOST record rule 7 requires | C1 |
| `045-work-unit-cancelled-state.sql` | amends `work_units_state_ck` to admit `'cancelled'` — the seventh state | C3 |
| `046-cancel-work-units-function.sql` | `derived.cancel_work_units`, the audited mutation-API entry point, plus the work-unit lock in `derived.retry_parked_attempts` | C3 |
| `047-idempotency-and-expected-state.sql` | the mutation contract's two missing fields: an `idempotency_key` / `expected_state` column pair on `derived.mutation_audit`, keyed OVERLOADS of `add_problem_category` and `retry_parked_attempts` taking both, the shared `derived.mutation_replay` lookup, and `derived.record_external_action` for operator actions whose target is outside this database | G2, G3 |
| `048-products-and-artifacts.sql` | `products` (one row per deterministic product key, UNIQUE-constrained), `artifacts` (one row per published file per attempt, replay-unique on attempt + record sequence + published name, full 64-character checksum with its algorithm), `product_artifacts` (the current binding, one current row per product), plus a nullable `product_id` FK on `refimages` and `diffimages` | D1, D2 |
| `049-association-sets-and-watermarks.sql` | `association_sets` (immutable set identity, at most one live prompt set, seeded as a well-known row), `association_watermarks` keyed `(association_set, lane)` with a sequence-shaped `(proc_date, field)` value, `derived.live_association_set()` as the single well-known-row lookup, `derived.association_table_name` for set-scoped clone naming, and `derived.advance_association_watermark` as the CAS-guarded monotonic advance | F1 |
| `050-alert-outbox-and-publisher.sql` | `alert_outbox` (rule 14's transactional outbox: one row per alert packet, `alert_id` UNIQUE, the dispatch envelope write-once by trigger, a PENDING → IN_FLIGHT → SENT/REFUSED state machine with claim token and lease), `delivery_policies` (per-release authorization, default-DENY, checked before every send), `insert_alert_outbox_packet` (the collision-guarding insert path), two health views for §2.8's outbox clocks, and the `rapid_publisher` NOLOGIN service role with column-level UPDATE on the state columns alone | E1 |

## Brief G's draft (047), for its reviewer

Written against stream head **044** (`rapid_systems` at
`14e4ccf8971a1f6edeb6e7d24771b99d4a574e9a`), which is the revision the brief G
acceptance run applied; 047 follows C's 044-046 and is the next free number.

Three review points, each argued at length in the file's own header:

1. **Overloads, not amended signatures.** Changing an argument list in
   PostgreSQL creates a different function and orphans the old one with its
   grants; an "amendment" would have to DROP the current signature and so
   silently revoke `rapid_orchestrator`'s enumerated EXECUTE. The keyed
   functions take `p_idempotency_key` FIRST, so no existing call can bind to
   them and no defaulted-argument ambiguity arises.
2. **The key's uniqueness is partial, over real runs only.** A dry run must
   not consume a key, or every apply would replay its own preview. The
   partial unique index and the replay lookup both exclude `dry_run`.
3. **030's invariants are untouched.** The append-only trigger, the
   ungranted `write_mutation_audit`, and the no-direct-write grant posture
   are unchanged — and asserted, not assumed, by
   `pipeline/contract/test_operator_grants.py`.

Applied twice in the acceptance run to demonstrate idempotence
(`BRIEF-G-DRAFT-047-REAPPLY: PASS exit=0`).

## Brief D's draft (048), for its reviewer

Written against stream head **043** (`rapid_systems` at
`83f1a38283167132654706ea092d047312f35d4b`), the revision the brief D
acceptance run applied; 048 follows G's 047 and is the next free number. The
stream is unchanged from the 44 files C and G were written against.

Four review points, each argued at length in the file's own header:

1. **Three tables, not one.** `products` is one row per deterministic
   identity and is NOT attempt-scoped; `artifacts` is one row per published
   file per attempt and IS; `product_artifacts` carries which artifact
   currently realizes which product. Rule 10's "products and artifacts are
   distinct records" is what the split implements, and the retry case is
   what it buys: the product row is unchanged, the new bytes get their own
   artifact, and the binding moves.
2. **The uniqueness is in the database.** `products_product_key_uq` and
   `artifacts_replay_uq` are constraints, not application conventions,
   because the registration consumer is explicitly concurrent (per-attempt
   leases, watermark re-reads) and a find-or-insert in Python has a window
   a second registrar can interleave into.
3. **No reader is migrated.** Every legacy column on `refimages` and
   `diffimages` keeps being populated exactly as before; the two tables gain
   one appended, nullable `product_id` FK and nothing else. Nullable because
   rows predating product identity cannot be backfilled — the identity
   components are not recoverable from the row. The acceptance run asserts
   the named production readers' RESULTS are unchanged, not merely that rows
   still exist.
4. **The checksum is done right, and the legacy defect is only flagged.**
   `artifacts.checksum` is 64 characters with its algorithm recorded and a
   CHECK constraint. `refimages.checksum` and `diffimages.checksum` remain
   `varchar(32)` (`006-core-tables.sql:393,448`) and therefore still
   truncate every SHA-256 they are given — a latent defect brief D flags as
   a candidate change request and explicitly puts out of scope, since
   widening a live column is its own decision. A contract test asserts the
   defect still exists, so the flag cannot go stale unnoticed.

Applied twice in the acceptance run to demonstrate idempotence
(`BRIEF-D-DRAFT-048-REAPPLY: PASS exit=0`).

## Brief F's draft (049), for its reviewer

Written against stream head **043** (`rapid_systems` at
`e2b5ebcf3eb33e1bb3afd9b392525ac1507ce62d`, 44 stream files), the same head
briefs C, G and D were written and accepted against. 049 is the next free
number after D's 048.

Six review points, each of which was a decision rather than a default:

1. **The live set keeps today's clone names.** `derived.association_table_name`
   returns `astroobjects_<field>` unchanged for the live prompt set and
   `astroobjects_s<set>_<field>` for any other. Adopting this file therefore
   renames nothing, moves no data and needs no backfill, and every existing
   reader keeps working. Reprocessing isolation is a consequence of the naming
   rather than a rule anything enforces: a reprocessing set cannot mutate the
   live tables because it never names them.
2. **The watermark value is sequence-shaped, not boolean** — the last accepted
   unit's `(proc_date, field)`, which is the canonical claim order's own key.
   This mirrors the registration watermark deliberately so re-acceptance and
   supersession need no special case, and the guard is the UPDATE's own WHERE
   clause, never a read-then-write in the application.
3. **`watermark_proc_date` is `text`, not `date`.** The pipeline's processing
   date is a zero-padded `YYYYMMDD` string everywhere
   (`submission.payloads.CrossmatchPayload.proc_date`), and text comparison of
   that form is the same order as date comparison. A `date` column would need
   a conversion at every comparison and would silently mis-order against a
   string-shaped argument.
4. **At most one live prompt set**, enforced by a partial unique index rather
   than by convention. The design says "the live set" in the singular
   throughout; this is where that becomes true.
5. **No table-level write grant on `association_sets`.** Sets are registered by
   migration or by an operator procedure, never by a pipeline job: the set
   identity is immutable, and a job that could mint one could silently escape
   the ordering it is supposed to obey. The read role is `rapid_read`, per
   `002-grants.sql` — not `rapid_pipeline_read`, which does not exist.
6. **`derived.association_table_name` is dropped before creation and its
   parameters are `p_`-prefixed.** Naming a parameter after the column it
   compares against made PL/pgSQL resolve the qualified reference as a table
   and fail with "missing FROM-clause entry"; `CREATE OR REPLACE` cannot then
   rename the parameters of an already-created function, so the drop is what
   lets a database holding an earlier revision take the corrected one.

The new advisory-lock namespace is `AL 0x414C`, placed as LEVEL 3 beneath the
existing `R4 0x5234` / `W6 0x5732` (level 1) and `WU 0x5755` (level 2). The
existing order is unchanged and unreordered; AL is the lowest level, which is
what keeps the order total and therefore deadlock-free. The four namespaces
are asserted pairwise distinct in the contract tier.

Applied twice in the acceptance run to demonstrate idempotence
(`BRIEF-F-DRAFT-049-REAPPLY: PASS exit=0`), and the recorded acceptance run
reported `BRIEF-F-PASS2-SKIPS: 0` with all seven criteria green.

**Fix round 1.** The application side of this draft moved out of `RAPIDDB`,
which is frozen: the two reads the claim path makes now live in
`pipeline/repositories/association.py` over a connection the caller owns, and
`database/modules/utils/rapid_db.py` is byte-identical with `smdc`. The
cross-date gate no longer derives its own answer to "which (date, field) pairs
are science work" — it shares `RAPIDDB.get_fields_with_science_jobs_for_
processing_date`'s predicate, because the two independently written versions
could and did disagree (a succeeded attempt whose difference image is
superseded is work to one and not the other).
`pipeline/contract/test_association_work_inventory.py` proves the agreement
against real rows across the edge states rather than asserting it in a
comment, and gives both repository methods their first real-SQL coverage.

## How the application behaves while these are unapplied

Every code path that needs draft schema **probes for it and degrades
explicitly** rather than assuming it. `pipeline.intent.cancellation.
is_available` asks `pg_proc` whether 046's function exists; the contract tests
covering C1 and C3's cancellation skip cleanly when their schema is absent.
`RAPIDDB.get_association_claim_position` probes `to_regclass` for 049's
`association_watermarks` and answers `None` when it is not there, at which
point `gather_crossmatch_units` logs that it is gathering without the rule 19
ordering gate and behaves exactly as it did before brief F. The ordering is a
property of the deployed schema, so a deployment without it does not get to
claim the ordering — and is not broken by its absence either.
That is what keeps `smdc` CI green — CI builds its database from the
authoritative stream, which does not contain these files, so the draft-schema
tests skip there and the rest of the suite still gates regressions.

The acceptance runs on rapid-admin apply the base stream and then these
drafts, in order, which is the only venue where the draft-schema tests
actually execute.

## Brief E's draft (050), for its reviewer

Written against stream head **83f1a38283167132654706ea092d047312f35d4b**
(44 stream files) — the same 44-file stream C, G, D and F were written and
accepted against. 050 is the next free number after F's 049. It depends on
**037** (the emission state model whose confirm CAS its rows commit beside)
and on **DRAFT 048** (the `products` / `diffimages.product_id` binding the
product-key identity basis joins through).

Seven review points, each a decision rather than a default:

1. **The outbox rows commit in the ALERT-EFFECT CONFIRMATION transaction, and
   the file says so plainly rather than claiming rule-9 acceptance.** Rule 14
   asks for "the same transaction as the database effect that produced them".
   In today's topology that effect is `alert_production.py`'s confirm CAS +
   `alert_published` milestone, NOT rule 9's result-acceptance transaction —
   the registration consumer that owns the latter cannot construct these
   packets (no provider, no cutouts, no schema). The remaining architectural
   gap is recorded in the migration header verbatim for the re-score.
2. **The order inside that transaction is fixed and load-bearing**: confirm
   CAS → token check → outbox rows → milestone. The confirm CAS can affect
   zero rows WITHOUT raising (a takeover is a recorded no-op), so a claimant
   that inserted first would leave packets behind for an emission it never
   confirmed, and the publisher — which knows nothing about emissions — would
   deliver them. A losing claimant commits neither.
3. **The payload, its checksum AND the pinned schema-version UUID are all
   stored, because "identical bytes" is a claim about the WIRE.** The producer
   frames with the registry's LATEST version at publish time
   (`SchemaVersionNumber={"LatestVersion": True}`), so the same payload
   re-framed after a registry bump yields different wire bytes. The publisher
   frames strictly from the stored fields and never asks the registry on the
   send path; that is the whole mechanism, and the pinned UUID is write-once
   under the same protection as the bytes.
4. **Immutability is a TRIGGER, not a grant** (030's append-only pattern). The
   table owner and any SECURITY DEFINER function bypass column grants, and
   "identical bytes on resend" must not depend on getting a grant map right.
   Narrower than 030's: rows are not append-only — the state machine must move
   — so what is frozen is the DISPATCH ENVELOPE (identity, bytes, checksum,
   pinned version, topic, release, `created_at`). `SENT`/`REFUSED` rows are
   undeletable by anyone, `PENDING` ones remain deletable by the owner
   (draining a mis-built batch nobody has seen is legitimate).
5. **The pipeline writer is INSERT-ONLY, deliberately NOT copying 048's
   table-wide INSERT/UPDATE/DELETE posture** (`048:404`). The outbox is
   different by design: the pipeline writes packets and then has no further
   business with them. It cannot move a row's state (that is the publisher's
   protocol and racing it would break the claim), cannot touch ack columns,
   and cannot delete any row — `PENDING` included, because a bug that deleted
   its own undelivered packets would look exactly like alerts that were never
   produced. The ALL-UPDATE / ALL-DELETE revokes are stated explicitly so the
   posture survives a later blanket grant written by habit.
6. **`rapid_publisher` has NO group membership**, which is where it departs
   from 016's orchestrator. That role joined `rapid_pipeline_write` because it
   writes what the payload writes; the publisher does not — it reads two
   tables and updates state columns on one. It gets direct grants of exactly
   that and nothing else, a narrower boundary than a group can express, for
   the one process that touches the outside world. It CONNECTS DIRECTLY (no
   `SET ROLE`): it is transaction-mode pooled (§2.2) and `SET ROLE` needs a
   session lane (`operatorctl/session.py:37-43`).
7. **`p_sca` is declared `integer` though the column is `smallint`.**
   PostgreSQL will not implicitly narrow integer → smallint to resolve a
   function call, so a caller passing a plain Python int through psycopg2 gets
   "function ... does not exist" — a message that names the function it is
   looking at and reads as though the migration never applied. Found live on
   this branch's second acceptance run. The assignment to the smallint column
   still range-checks the value, so the check moves rather than disappearing.

8. **The application side reaches this schema through a carved repository,
   never through `RAPIDDB`.** `pipeline/repositories/alert_outbox.py` owns the
   two calls the alert-production stage makes — `insert_packet` and
   `product_key_for_difference_image` — beside D's `products.py` and F's
   `association.py`. `RAPIDDB` is frozen (brief G's ratified merge decision;
   rule 17), and the first revision of this work put both queries there and
   was correctly refused, exactly as F's was.

   The carve is not a move, and the difference is worth a reviewer's eye. The
   `RAPIDDB` revision recovered from an unapplied DRAFT 048 by catching
   `UndefinedTable` and calling `conn.rollback()` — safe in that class, where
   every read autocommits and owns nothing, and CATASTROPHIC on this path,
   where the same rollback would discard the confirm CAS the caller had
   already written inside the confirmation transaction. The repository asks
   the catalog first (`product_binding_present`), so a missing 048 never
   aborts the caller's transaction to be discovered. It also never commits and
   never rolls back, for the reason `products.py`'s `_query` records: the
   caller owns the boundary.

   One thing IS deliberately not wrapped: the migration's own collision RAISE
   (SQLSTATE **P0001**) passes through the repository unwrapped, so a
   same-id-different-envelope insert fails the attempt instead of arriving as
   a typed "query failed" a caller may reasonably treat as retryable.

**Applied and re-applied cleanly** in the recorded acceptance run
(`BRIEF-E-DRAFT-050: PASS exit=0`, `BRIEF-E-DRAFT-050-REAPPLY: PASS exit=0`
— idempotent), with the full contract tier at 279 passed / **zero skips** and
`BRIEF-E-OVERALL: PASS exit=0`.

**The grant posture is asserted twice, on purpose.** The catalog-metadata
tests ask what the grant map says (`has_table_privilege`,
`has_column_privilege`); the behavioural tests `SET LOCAL ROLE` to
`rapid_pipeline_write` and `rapid_publisher` and ATTEMPT each forbidden
operation, asserting `InsufficientPrivilege` specifically. Neither subsumes
the other — a passing catalog test beside a failing behavioural one would mean
the map lies, and the reverse would mean the refusal holds today for a reason
the map does not document.

**Deployment still owes two things this file cannot do**, both drafted as
`rapid_systems` change-request text in the brief-E worker's ledger: the
LOGIN/password association pass for `rapid_publisher` (from
`rapid/db/service/publisher`), and a pgbouncer user line so the pooler admits
it with a deliberately sized pool. Until the first runs, the role cannot
authenticate at all, by construction.

## Brief H's drafts (051, 052), for their reviewer

Written against stream head **043** (`rapid_systems` at
`83f1a38283167132654706ea092d047312f35d4b`, 44 stream files) — re-verified
twice on 2026-08-12, from the sibling checkout and via the GitHub contents
API. 051 and 052 follow E's 050. 051 depends on **006** (the `exposures` and
`l2files` tables it attaches identity to) and on **DRAFT 047**
(`derived.mutation_audit`'s idempotency/expected-state pair); 052 depends on
047 as well.

| Draft | Purpose | Brief item |
|---|---|---|
| `051-admission-identity-and-release.sql` | the two admission sidecar tables, the sealed source manifest, and the switchable release pointer with its audited mutation | H1, H2 |
| `052-gc-plans.sql` | `gc_plans` / `gc_plan_items` (the recorded, checksummed, immutable two-pass deletion plan), `gc_fences`, and `gc_plan_execute` joining 047's enumerated external action classes | H3, H4 |

Seven review points:

1. **The two grains are defined separately and differently, and that is the
   whole of rule 20's repo-side half.** The exposure grain's identity is
   `dateobs` ALONE, matching the database's own natural key
   (`exposurespk UNIQUE (dateobs)`, `006:194`) — no checksum participates,
   because an exposure is an observational fact and not a file, and ingestion
   is per-detector-file so there is no exposure-level file to hash. The L2
   grain's identity is a content key over `(expid, sca)` plus the source
   checksum, which is the grain where a file exists.

2. **`admission_l2files` carries the `(expid, sca)` UNIQUE that `l2files` has
   never had, and deliberately does not add it to `l2files`.** `l2filespk` is
   `(expid, sca, version)` — uniqueness that INCLUDES the version — which is
   exactly what lets `addl2file`'s `coalesce(max(version), 0) + 1`
   (`008:438-446`) sidestep it and mint a new admission row per re-ingest.
   Adding `UNIQUE (expid, sca)` to `l2files` itself **would refuse to apply
   against any database holding a genuine re-version**, so the sidecar carries
   the constraint the new path needs and the legacy table's shape is
   untouched. No reader is migrated.

3. **`admitted_at` is write-once by TRIGGER, not by convention.** This is the
   direct repair of `addExposure`'s `else` branch (`008:331-345`), which
   updates every field including `created = now()` and so destroys the
   original ingest timestamp, unrecoverably, on every repeat. A trigger rather
   than a grant because the owner and any SECURITY DEFINER function bypass
   column grants.

4. **Sealing is the LAST write, and citing an unsealed manifest is refused.**
   That ordering is the crash guarantee: at any instant a manifest is either
   unsealed (citing no admissions, because the trigger forbids it) or sealed
   (with every entry durable, because they were written first). There is no
   third state.

5. **The GC plan is a LIST and it is immutable once computed.** Pass-one
   candidate rows and the checksum over them are never deleted and never
   rewritten; recomputation records its verdict as a STATUS on the existing
   row, so a dropped candidate stays visible as `excluded-on-recompute`. A
   plan whose items were deleted to reflect a recomputation would be a plan
   that lies about what it computed, and its checksum would have to move to
   match — at which point it is evidence of nothing.

6. **`gc_plan_execute` joins 047's ENUMERATED action classes rather than
   widening the column.** 047 refuses any class outside its literal list on
   purpose ("an open text column would make this function a general-purpose
   audit-row writer, which is the thing 031 deliberately refuses to grant
   anyone"), so 052 replaces the function with 047's body verbatim plus one
   string. Reproducing that body loosely would have silently changed
   behaviour — an early revision did, and was caught by diffing against the
   real definition rather than by a test.

7. **The fence is a database row, never an S3 tag.** `PutObjectTagging`
   replaces an object's entire tag set with no merge, and
   `pipeline/reconciler/retention.py:219` rewrites the canonical full set on
   every classification — so a GC hold expressed as a tag would survive
   exactly until the next reclassification.

**A warning for the next brief's harness, learned here.** The first acceptance
smoke applied 051, re-applied it idempotently, created all six tables and all
three functions, and fired both triggers — **and passed while
`set_admission_release` carried two fatal signature errors**, because it never
CALLED the function. PL/pgSQL resolves a callee's signature at EXECUTION, not
at creation, so an unexecuted function body is unverified however green the
apply looks. Every DRAFT function should be exercised, not merely applied.

Applied and re-applied cleanly in the recorded acceptance run
(`BRIEF-H-DRAFT-051`, `BRIEF-H-DRAFT-052` and `BRIEF-H-DRAFT-REAPPLY` all
`exit=0`).

## Style

These match the stream's own conventions deliberately — read several of the
real migrations before amending one of these. In particular: one `BEGIN;` /
`COMMIT;` per file; `IF NOT EXISTS` / `DO $$ ... $$` guards so a re-run
converges rather than errors; `COMMENT ON` for every new object; a header that
states what changed and *why*, quoting the design or review text that ruled
it; and no `schema_migrations` INSERT — the applier records that, never the
migration itself.
