# Brief H — proposals ledger

> **FIX ROUND 1 (2026-08-12).** The first submission of this package shipped
> the machinery for H1, H2 and H4 with **the real call graph never connected**.
> Adversarial diff-vs-brief review found four blocking wiring gaps, all four
> verified before the round began. Read this note as: the components below were
> designed as described in the first round, and only became *reachable in
> production* in fix round 1. What was fixed:
>
> - **B1 — admission was dead on arrival.** All three ingest scripts imported
>   the admission lifecycle and called only `record_exposure_admission`, which
>   reads run state that only `begin_admission_run` establishes — so every
>   real exposure raised `ReleasePointerUnset`. The full lifecycle is now
>   wired into all three: open unsealed → read the pointer once → enumerate
>   every source object → admit → **seal last, and only on a clean run**.
> - **B2 — the L2 defect rule 20 exists to fix was untouched.**
>   `record_l2file_admission` was imported by all three scripts and called by
>   none; `addl2file`'s `max(version)+1` re-versioning was unchanged in
>   production. It is now called at each script's L2 registration.
> - **B3 — the GC was unreachable by an operator.** `rapidctl` had no
>   `gc-compute-plan` and no `gc-execute-plan`, and `pipeline/gc/execute.py`
>   had **zero production call sites**. All three steps now exist under the
>   full mutation contract.
> - **B4 — the release stamp never reached `ExecutionBinding`.**
>   `pipeline/seams.py` was never touched, so work took its release from the
>   submitting process's environment and rule 18 was not repaired. The seam
>   now resolves the admitted release and refuses a disagreement loudly.
> - **N1** (active-manifest body expansion) and **N2** (fence re-verifies the
>   discharge watermark) were closed in the same round rather than deferred.
>
> **The lesson, which is the reusable part:** every one of these survived a
> green 474-test acceptance run, because the tests exercised units and the
> integration was assumed. Import-level presence read as wiring. Fix round 1
> adds tests that parse for CALLS, drive the operator surface end to end, and
> assert the executor is genuinely reached — each written to fail if the
> wiring were removed again.

Every decision this brief left to the worker, plus every finding that is a
decision for the merge gate rather than for this package. **Nothing here is
ratified**; the brief's fixed rulings are implemented as written and are not
relisted. Where the unattended decision rule applied, the conservative option
was taken and is marked as such.

## P-H1 — The catalog probe uses `to_regclass`, not `pg_proc`

**Decision taken:** draft-gated objects are probed with `to_regclass(...)` and
`information_schema.columns`, matching what `pipeline/repositories/` already
does, rather than the `pg_proc`/`pg_class`-by-exact-signature the brief names.

**Why:** the brief asks for "catalog probes (`pg_proc`/`pg_class` by exact
signature) for any DRAFT-051-gated object rather than catching
`UndefinedTable`/`UndefinedFunction`". The *substance* of that instruction —
ask the catalog first, never catch-and-roll-back inside a caller's open
transaction — is what `alert_outbox.py:55-64` argues for and is implemented in
full. But the repository package contains **zero** references to `pg_proc` or
`pg_class`; all three existing probes use `to_regclass`
(`association.py:54`, `alert_outbox.py:52,66`). Introducing a second probe
spelling for the same job would make the convention ambiguous for the next
worker. `to_regclass` answers exactly the question asked here (does this
relation exist) and takes the same catalog-read path.

Where H probes for a **function** rather than a relation
(`derived.set_admission_release`), `pg_proc` by exact signature IS used — there
is no `to_regclass` equivalent for a function, and that is the one case the
brief's spelling is the only one available.

## P-H2 — Three pre-existing defects found in the admission writers

Found while mapping the admission path; **none is fixed by this package**
beyond what the carve necessarily touches, because each is a live-behaviour
change in a pre-pipeline-convention script and is outside H's scope. Recorded
for the merge gate.

1. **`dbh.add_l2file(...)` in the troxel script did not exist — FIXED IN FIX
   ROUND 1.** `RAPIDDB` defines only `add_l2file_fourth_order`
   (`rapid_db.py:536`) and `add_l2file_fifth_order` (`:651`); that call site
   named neither, so the script raised `AttributeError` on its first L2
   registration, with no `try`/`except` around it — its L2 path was **dead**.

   Originally recorded here as "found but not fixed, outside H's scope". Fix
   round 1 **fixed it**, because it had become blocking: B2 requires all three
   production ingest scripts to admit through the carved repository, and a
   script whose L2 path cannot execute cannot do that. The change is minimal
   and provably safe — the existing argument list already matched the
   4th-order signature exactly (same 57 positional arguments, same order), so
   **the method name was the entire defect**. A contract test now asserts that
   no ingest script calls a `dbh.*` method `RAPIDDB` does not define, checked
   against the real class rather than a list.

2. **`db_register_troxel_sim_files.py:445` calls `register_l2filemeta` with 17
   arguments against the 19-parameter signature** at `rapid_db.py:840`
   (trailing `mjdobs` missing). socsim (`:818`) and rimtimsim (`:477`) both
   pass the full set. A second latent `TypeError` in the same script.

3. **A checksum failure calls `exit(0)` in all three scripts** (socsim `:717`,
   rimtimsim `:378`, troxel `:348`) — a failure reported to the scheduler as
   success. This is the "no false cleans" failure mode in the ingest path.
   Proposed fix: a nonzero exit. Not taken here because changing an ingest
   script's exit code changes operational behaviour outside H's rules.

**Proposal:** land 1 and 2 as a small correctness fix in a follow-up package,
and 3 with an operational note. H's carve routes all three scripts through the
admission repository, which removes the *idempotency* defect from all three,
but does not repair these three unrelated bugs.

## P-H3 — `DONTCHECKALREADYINGESTED` is removed, not routed to the mutation API

**Decision taken (conservative):** the environment variable no longer disables
admission idempotency anywhere. No replacement escape hatch is added.

**Why:** the brief requires the opt-out gone and says that if an operator
escape hatch is genuinely needed it must go through G's mutation contract
rather than a bare environment variable, recording which was chosen. Adding a
new operator-facing "admit anyway" mutation would be **widening** the operator
surface on the strength of a hypothetical need, and it is the option that
cannot be undone quietly. Removing the variable and adding nothing is the
conservative half: idempotency becomes an invariant, and if an escape hatch
turns out to be needed, adding one later through the mutation contract is a
small, reviewable change. Recorded as a proposal because "no escape hatch at
all" is an operational decision, not a purely technical one.

Note the variable's current semantics are worse than they look: socsim `:92-96`
tests only `is None`, so `DONTCHECKALREADYINGESTED=0` and
`DONTCHECKALREADYINGESTED=false` both **disable** the check. It is also absent
from the other two scripts entirely, so those two have never had even the
filename-scoped guard.

## P-H9 — Two signature defects in DRAFT 051, found and fixed in-run

**Recorded because the way they were found is the reusable lesson, not
because they survived.** Both were in `derived.set_admission_release`, and
both would have been fatal on its first real call:

1. **`derived.mutation_replay` was called with one argument.** 047 defines it
   with three — `(p_idempotency_key, p_action_class, p_target_scope)` — and
   raises RA002 when a key was used for a *different* action, which a
   one-argument call could not express. No one-argument overload exists, so
   every call would have failed with `undefined_function`.

2. **`derived.write_mutation_audit` was called with eight arguments in the
   wrong order.** Its real signature (`031-mutation-functions.sql:77-86`) is
   `(action_class, action_tier, target_scope, reason, dry_run, rows_affected,
   policy_citation, dispatcher, detail)` — nine parameters, and **it carries
   no idempotency key and no expected state at all**. 047's own header says
   the keyed functions must therefore write their audit row INLINE, which is
   exactly what the fix does.

**THE LESSON, WHICH IS WHY THIS IS RECORDED:** the first acceptance smoke
applied DRAFT 051, re-applied it idempotently, created all six tables and all
three functions, and fired both triggers — **and passed while carrying both
defects**, because it never CALLED the function. Applying a migration proves
its SQL parses and its objects are created; PL/pgSQL resolves a callee's
signature at execution, not at creation, so an unexecuted function body is
unverified no matter how green the apply looks.

The second smoke (`SMOKE051B-*`) calls it: dry run, real apply, replay,
unknown release, wrong expected state, supersession, empty reason, empty key.
**Any DRAFT function this arc ships should be exercised, not merely applied.**
That generalizes beyond H and is worth carrying into the next brief's
harness template.

## P-H4 — Self-approval of a GC plan is permitted, and recorded as such

**Decision taken (the brief's stated conservative default):** the approving
actor may be the computing actor, and the plan records that it was a
self-approval.

**Why:** RAPID is a single-operator system today; requiring a second actor
would make the GC unusable rather than safer, and an unusable safety mechanism
gets bypassed. The recording is what makes it reviewable: `approved_by` and
`computed_by` are both stored, and their equality is visible in the plan
rendering rather than hidden.

## P-H5 — The deletable-class allowlist ships EMPTY

**Decision taken:** no object class is on the allowlist. The GC computes plans,
records them, and deletes nothing.

**Why:** the brief fixes this as the governing clause — a class joins the
allowlist only when the brief or a ratified proposal names it, with the durable
reference surface that makes its absence meaningful. The brief names no class,
and none of today's classes has such a surface (`artifacts` is not populated on
the live path — see P-H6). Adding one would be inventing the ratification the
brief requires.

**Consequence, stated as the brief requires:** on today's schema this GC will
delete **nothing at all**. That is the correct and conforming outcome for this
package. Rule 21 requires that deletion happen only through this mechanism, not
that the mechanism reclaim anything.

## P-H6 — `artifacts` rollout status: VERIFIED NOT POPULATED on the live path

The brief requires this be verified before any GC logic trusts that table, and
recorded. **Verified at this branch's head, and it confirms the brief:**

- `pipeline/operator/registrar.py` — `production_registrar` contains **zero**
  occurrences of `identity_repository`, so the live registration path never
  passes one;
- the **only** call site that does is `pipeline/entrypoints/job.py:477`
  (`identity_repository=ProductRepository(conn)`), on the
  `JOB_TYPE_REGISTRATION` route that `pipeline/registration/consumer.py:43`
  itself describes as **dormant**;
- `pipeline/registration/products.py:233` names the
  `identity_repository is None` branch "the pre-rollout path".

**On the scratch database `artifacts` therefore exists (DRAFT 048 applies) and
is empty of anything the live path wrote.** The resulting deletable population
is **zero**, for two independent reasons: nothing is on the allowlist, and no
horizon is configured.

The design consequence was taken from the start rather than discovered: the
reference set is defined by **enumeration over scopes** rather than by
`artifacts.uri` alone, precisely because that table cannot be trusted as the
join surface. An anti-join keyed on it would have classified every real
product as unreferenced garbage.

**Out of scope, and explicitly not done:** rolling out `artifacts` population
to the live registrar. H verifies and records its status; it does not change
the registration wiring.

## P-H7 — Rule 21's residual gap is narrower than the brief assumed

**Finding, verified read-only against `rapid_systems` — see
`notes-brief-h-evidence.md`.** The brief instructs H to draft a CR removing the
S3 lifecycle-expiry deletion route so the two-pass process becomes the system's
only deletion path. Read against the authoritative templates:

- `rapid-product-buckets.yaml` (74 lines) defines `roman-rapid-products` and
  `roman-rapid-alerts` and has **no `LifecycleConfiguration` at all**;
- every expiry rule in the account lands on `roman-rapid-logs`,
  `roman-rapid-diagnostics`, `roman-rapid-meta` or `roman-rapid-build`, all
  **outside H's declared scope**.

So inside the declared scope there is no competing deletion route today. The
CR (CR-H5 in the ledger) is written to that corrected finding: it asks that any
future lifecycle expiry on the products bucket be added only through the GC
mechanism, and that the diagnostics-bucket tag-expiry scheme be reconciled with
rule 21 when GC scope is widened to cover attempt bundles.

**H still does not claim rule 21 CONFORMS.** The exclusivity assertion is
repository-scoped, and widening GC scope beyond the products bucket is
explicitly out of scope. Rule 21 scores **PARTIAL** for the reasons in the
ledger — but the reason is "the mechanism governs one bucket and the tag-expiry
scheme governs another", not "a competing route deletes the objects H governs".

## P-H10 — Two schema facts the brief states incorrectly

Both verified against the head; neither changes the code, and both are
recorded so the next reader is not misled. Full detail in
`notes-brief-h-evidence.md`.

1. **`work_units` has no `unit_key` column and its PK is `work_unit_id`**, not
   `unit_id` (`036-intent-schema-v1.sql:111-122`). The persisted unit identity
   is `(job_type, input_scope)`, and the canonical round trip therefore
   rebuilds the S3 key component as `job_type || '/' || input_scope`. Caught
   before the SQL ran, by reading the migration rather than trusting the
   brief's column names.

2. **`superseded_by_unit_id` DOES exist** (`036:118`), contrary to the brief's
   "there is no `superseded_by_unit_id` column; do not invent one". The
   brief's *ruling* is nonetheless implemented exactly as written —
   `is_fully_discharged` never consults supersession — and using it would in
   fact be wrong, since a unit can be superseded from any state including
   `ready`, so a superseded unit is not thereby discharged.

## P-H11 — `btrim` strips spaces only, across this whole function family

`length(btrim(x)) = 0` is the mandatory-field guard every keyed mutation in
this stream uses (`047:182,253,256`), and PostgreSQL's one-argument `btrim`
strips **spaces only** — so a reason of `E'\t\n '` has length 2 and is
ACCEPTED. 051 matches its siblings rather than being quietly stricter than
them.

**Proposal (not taken):** widen the guard to `btrim(x, E' \t\n\r')` across the
whole family in one change. Doing it in 051 alone would create exactly the
kind of silent inconsistency between sibling functions that is worse than the
shared laxity. Recorded as a candidate CR.

## P-H8 — The horizon fails closed with no default

**Decision taken:** there is no default horizon that permits deletion. A GC run
with no configured horizon computes its plan, marks every candidate ineligible
for want of a horizon, and says so.

**Why:** the brief fixes this ("no default that permits deletion"). Recorded
here because the *operational* consequence is a decision someone must own: until
the authoritative pgBackRest PITR duration is supplied (CR-H4), GC deletes
nothing even if a class were allowlisted. `repo1-retention-full=4` is a **count
of full backups**, not a duration, and the cadence is expressly provisional —
so no duration can be derived from the repo.
