# DRAFT migrations — briefs C and G

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
(`rapid_systems` at `e2b5ebcf3eb33e1bb3afd9b392525ac1507ce62d`). The drafts
are numbered 044-046 to follow it. If the stream advances past 043 before
these are adopted, they are renumbered on adoption — the numbers here record
what they were written against, not a claim on those slots.

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

## How the application behaves while these are unapplied

Every code path that needs draft schema **probes for it and degrades
explicitly** rather than assuming it. `pipeline.intent.cancellation.
is_available` asks `pg_proc` whether 046's function exists; the contract tests
covering C1 and C3's cancellation skip cleanly when their schema is absent.
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
