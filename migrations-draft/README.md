# DRAFT migrations — adopted

**This directory formerly held nine DRAFT SQL files staged against
`IPAC-SW/rapid_systems`, the authoritative migration stream this repository
cannot edit.** They were adopted verbatim (byte-identical SQL bodies) into
`rapid_systems` on 2026-08-12 as migrations 044-052. Three further
migrations were authored `rapid_systems`-side in the same session: 053
(`retry_parked_attempts` revives `'blocked'`), 054 (widens
`refimages.checksum`/`diffimages.checksum` to `varchar(64)` with a
`checksum_algorithm` column — CR-8), and 055 (`addExposure`/`addL2File`
rewritten idempotent). The contract tier's pinned revision
(`.github/workflows/contract-tests.yml`) is `rapid_systems` main
`28ea260bee930f3336f3728e4486785a4708f7f3`, one commit past 044-055's
landing — the first live apply run found 053's `COMMENT ON FUNCTION
derived.retry_parked_attempts` was bare-name and ambiguous against 047's
overload of the same name, and 28ea260 fixes it by qualifying the
signature. Nothing here is vendored or read locally, so the nine `.sql`
files were deleted rather than kept as a second copy that could drift from
what was actually applied.

| Draft here (deleted) | Landed as | Purpose | Brief item |
|---|---|---|---|
| `044-submission-protocol.sql` | 044 | `submissions` table: the durable PREPARED → CALLING → BOUND / UNKNOWN → FOUND / LOST record rule 7 requires | C1 |
| `045-work-unit-cancelled-state.sql` | 045 | amends `work_units_state_ck` to admit `'cancelled'` | C3 |
| `046-cancel-work-units-function.sql` | 046 | `derived.cancel_work_units`, plus the work-unit lock in `derived.retry_parked_attempts` | C3 |
| `047-idempotency-and-expected-state.sql` | 047 | `idempotency_key`/`expected_state` on `derived.mutation_audit`, keyed overloads, `derived.mutation_replay`, `derived.record_external_action` | G2, G3 |
| `048-products-and-artifacts.sql` | 048 | `products`, `artifacts` (full 64-char checksum + algorithm), `product_artifacts`, nullable `product_id` FK on `refimages`/`diffimages` | D1, D2 |
| `049-association-sets-and-watermarks.sql` | 049 | `association_sets`, `association_watermarks`, `derived.live_association_set()`, `derived.advance_association_watermark` | F1 |
| `050-alert-outbox-and-publisher.sql` | 050 | `alert_outbox` transactional outbox, `delivery_policies`, `insert_alert_outbox_packet`, `rapid_publisher` role | E1 |
| `051-admission-identity-and-release.sql` | 051 | admission sidecar tables, sealed source manifest, switchable release pointer | H1, H2 |
| `052-gc-plans.sql` | 052 | `gc_plans`/`gc_plan_items`, `gc_fences`, `gc_plan_execute` | H3, H4 |

Each draft's full review rationale (the numbered decision points, the
acceptance-run evidence lines, the fix-round history) lived in this file's
prior revision and is recoverable from git history
(`git log -p -- migrations-draft/README.md`) or, more durably, from the
landed migrations' own headers in `rapid_systems` — each carries the same
argument verbatim, since the SQL bodies are byte-identical to what is
summarized here. The acceptance-run pass/reapply evidence quoted at landing
time:

- 047: `BRIEF-G-DRAFT-047-REAPPLY: PASS exit=0`
- 048: `BRIEF-D-DRAFT-048-REAPPLY: PASS exit=0`
- 049: `BRIEF-F-DRAFT-049-REAPPLY: PASS exit=0`, `BRIEF-F-PASS2-SKIPS: 0` (seven criteria green)
- 050: `BRIEF-E-DRAFT-050: PASS exit=0`, `BRIEF-E-DRAFT-050-REAPPLY: PASS exit=0`, `BRIEF-E-OVERALL: PASS exit=0` (279 passed / zero skips)
- 051, 052: `BRIEF-H-DRAFT-051`, `BRIEF-H-DRAFT-052`, `BRIEF-H-DRAFT-REAPPLY` all `exit=0`

CR-8 (054)'s scope is narrower than 048's checksum work suggests at a
glance: it widens only `refimages.checksum` and `diffimages.checksum`.
`l2files.checksum`, `refimcatalogs.checksum`, `psfs.checksum` and
`diffimmeta.checksum` are separate `varchar(32)` columns 054 explicitly
leaves untouched (054's own header names each). The rapid-side contract
test that asserted the pre-CR-8 truncation as a known defect
(`pipeline/contract/test_artifact_checksum.py`) was flipped at the same
2026-08-12 adoption to assert the widened shape instead.

## How the application behaves toward this schema now

Two different mechanisms cover this now, at two different points, and which
one applies depends on whether the caller can see the route.

**The payload entrypoint preflights per route, at startup, and fails
closed.** `pipeline.intent.schema_contract.ROUTE_MIGRATIONS` layers 049
(`association_watermarks`) onto the crossmatch route's floor and 050
(`alert_outbox`) onto alert-production's, composed by `required_for_route`
and checked by `pipeline/entrypoints/job.py:_database` before any product
work starts. This closed the schema-preflight gap a 2026-08 review found: a
database behind 049 or 050 used to pass the (route-blind) preflight and then
raise `UndefinedTable`/`UndefinedFunction` at the first unguarded call —
`pipeline/association/watermark.py`'s read/advance and
`pipeline/repositories/alert_outbox.py`'s insert had no probe of their own.
`AlertOutboxRepository.outbox_schema_present()` is now called at the top of
`produce_alerts` too, as defense in depth for whatever might reach that stage
without going through the payload's own preflight — it is not the primary
guard.

**Submission-time gathering still probes and degrades, because it has no
preflight to lean on.** `submission/gathering.py`'s
`_association_claim_position` (crossmatch's ordering gate) runs on hosts with
no science stack, before any payload — and therefore before any
`schema_contract` preflight — exists for the job it is about to submit. It
still answers "no watermark" and gathers unordered when 049 is absent,
exactly as before; that degradation was never the schema-preflight gap
described above, because gathering genuinely cannot see the schema the way
the payload's own connection can. `pipeline.intent.cancellation`'s `pg_proc`/`to_regclass`
checks and the contract tier's own `pytest.skip` gates are unaffected by any
of this and continue to key off the deployed schema. Now that `smdc` CI's
pinned `rapid_systems` revision carries 044-055, the draft-schema contract
tests run instead of skipping there; on any database still short of a given
migration (e.g. an environment mid-rollout) they continue to skip cleanly,
exactly as designed.

## Package R (unrelated to the adopted drafts above)

`notes-r-change-requests.md` and `notes-r-evidence.md` are package R's own
working notes — two wiring completions (R1: the live registrar wires
`identity_repository`; R2: application-half preflight on all five entry
points) plus a `rapid_systems` change request (CR-R1: release-identity
environment facts on the reconciler and publisher units). They are
independent of the nine drafts retired above and remain in place.
