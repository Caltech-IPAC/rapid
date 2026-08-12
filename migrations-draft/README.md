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

## Style

These match the stream's own conventions deliberately — read several of the
real migrations before amending one of these. In particular: one `BEGIN;` /
`COMMIT;` per file; `IF NOT EXISTS` / `DO $$ ... $$` guards so a re-run
converges rather than errors; `COMMENT ON` for every new object; a header that
states what changed and *why*, quoting the design or review text that ruled
it; and no `schema_migrations` INSERT — the applier records that, never the
migration itself.
