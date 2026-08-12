# Brief H — change requests against `rapid_systems`

**None of these is landed and none may be landed by this package.**
`rapid_systems` is single-writer custody and H never edits it. Every item here
is CR text for its owner, and the two rules whose conformance depends on them
(20 and 21) will NOT score CONFORMS at the re-score while they are unlanded —
which is expected and recorded, not a shortfall to chase.

The `rapid_systems` side was verified READ-ONLY throughout: from the sibling
checkout at `83f1a38283167132654706ea092d047312f35d4b` and via
`gh api repos/IPAC-SW/rapid_systems/contents/...`. No clone, no checkout, no
edit.

---

## CR-H1 — Adopt DRAFT 051 (admission identity and release pointer)

**File:** `migrations-draft/051-admission-identity-and-release.sql`, to land as
`cloudformation/db-migrations/051-...` (renumber on adoption if the stream has
advanced past 050 by then; it ended at **043** when this was written, with
044–050 also pending as CR-3..7 and CR-10).

**What it adds:** the two admission sidecar tables (`admission_exposures`
keyed on a `dateobs`-only identity, `admission_l2files` on a content key over
`(expid, sca)` + source checksum, each identity UNIQUE); the sealed source
manifest (`admission_manifests` / `admission_manifest_entries`); the
switchable release pointer (`admission_releases`,
`admission_release_pointer`) and its audited SECURITY DEFINER mutation
`derived.set_admission_release`; write-once triggers on `admitted_at` and
`admission_identity`.

**Verified:** applies cleanly on the full 44-file stream plus drafts 044–050,
re-applies idempotently, creates all six tables and all three functions, and
both triggers refuse as designed. Recorded on rapid-admin, `exit=0`.

**Why it is needed:** without it the admission path **refuses to admit** — it
does not fall back to the legacy stored procedures, because falling back would
reintroduce the duplicate-minting this package exists to remove.

---

## CR-H2 — Rewrite `addExposure` on `ON CONFLICT`, and stop it overwriting

**File:** `cloudformation/db-migrations/008-functions.sql:250-355`.

**The defect, verified at the current head:**

1. It is **select-then-insert** — `select expid into expid__ from Exposures
   where dateobs = dateobs_;` (`:290-293`) followed by a conditional INSERT.
   Two concurrent admissions of one observation both read NULL and both
   insert; the loser takes a unique violation on `exposurespk` instead of
   RECEIVING THE EXISTING ADMISSION. Rule 20 asks for the latter, and this is
   the same shape rule 6 already condemned for work units.

2. On a repeat it **OVERWRITES**. The `else` branch (`:331-345`) updates every
   field **including `created = now()`**, so re-admitting an observation
   silently mutates its own admission record and destroys the original ingest
   timestamp — unrecoverably.

**Requested change:**

```sql
    insert into Exposures (dateobs, mjdobs, field, hp6, hp9, fid,
                           exptime, status, infobits)
    values (dateobs_, mjdobs_, field_, hp6_, hp9_, fid_,
            exptime_, status_, infobits_)
    on conflict on constraint exposurespk do update
       set dateobs = Exposures.dateobs          -- a no-op, to force RETURNING
    returning expid into strict expid_;
```

`created` is **not** in the update list, and `do update` rather than
`do nothing` only because `do nothing` returns no row while this path must
return the existing admission. If a conflicting-facts case should be refused
rather than ignored, raise inside the function on a mismatch — the repo side
already refuses it (`AdmissionConflict`), so the two agree.

**Compatibility:** the signature is unchanged, so every existing caller keeps
working and no grant moves.

---

## CR-H3 — Give the L2 grain a natural key, and stop `addl2file` re-versioning

**Files:** `006-core-tables.sql:330` (`l2filespk UNIQUE (expid, sca, version)`)
and `008-functions.sql:438-446` (and again at `:586-593`).

**The defect:** uniqueness **includes the version**, and `addl2file` computes
`coalesce(max(version), 0) + 1`, so the `max+1` sidesteps the constraint by
construction: **re-running an ingest for the same L2 file mints a NEW
admission row** rather than returning the existing one. There is no
`(expid, sca)`-level natural key and no content (checksum) uniqueness
anywhere.

**Requested change, in two parts:**

1. A content-uniqueness constraint the re-ingest path can conflict against.
   DRAFT 051 carries this on its sidecar (`admission_l2files_grain_uq` on
   `(expid, sca)`) precisely because adding `UNIQUE (expid, sca)` to `l2files`
   itself **would refuse to apply against any database holding a genuine
   re-version** — so the sidecar is the safe half and this CR is the question
   of whether `l2files` should follow. **Recommendation: leave `l2files`
   alone** and treat the sidecar as the admission record; the legacy table
   keeps its version dimension for readers that use it.

2. `addl2file` should take an explicit `version_` rather than deriving
   `max+1`, so the caller states which version it is registering and a replay
   states the same one. Deriving it inside the function is what makes replay
   impossible.

---

## CR-H4 — Supply the authoritative pgBackRest PITR retention DURATION

**Files:** `cloudformation/rapid-postgres-pgbackrest.conf:32`
(`repo1-retention-full=4`) and `cloudformation/rapid-db-backup-schedule.yaml`.

**The problem:** `repo1-retention-full=4` is a **count of full backups**, not a
duration, and the weekly-full/daily-differential cadence is expressly
provisional. **No PITR duration can be derived from the repository.**

**What H needs:** the authoritative number of days the PITR window actually
covers, stated as a duration, so the GC safety horizon can exceed it.

**Until it is supplied:** the horizon is unset, and a GC run with no
configured horizon **deletes nothing and says why**. There is deliberately no
default that permits deletion. This is a fail-closed state, not an outage.

---

## CR-H5 — Reconcile the lifecycle-expiry deletion route with rule 21

**This CR is narrower than the brief anticipated, and the correction is
recorded in `notes-brief-h-evidence.md`.**

The brief asks for a CR removing or neutralizing "the applicable
lifecycle-expiry route so that the two-pass process becomes the system's only
deletion path". Read against the authoritative templates:

- `cloudformation/rapid-product-buckets.yaml` is **74 lines**, defines
  `roman-rapid-products` (:23) and `roman-rapid-alerts` (:47), and contains
  **no `LifecycleConfiguration` at all**. Both have
  `VersioningConfiguration: Status: Enabled`.
- Every expiry rule in the account is in
  `cloudformation/rapid-storage-buckets.yaml`, on a bucket outside H's
  declared scope: `roman-rapid-logs` (:202), `roman-rapid-diagnostics`
  (:489-491, the `retention-class=success` tag filter),
  `roman-rapid-meta` (:568-569), `roman-rapid-build` (:760).

**So inside the declared GC scope — the products bucket — there is today no
competing deletion route.** Rule 21's exclusivity is therefore violated only
for the *diagnostics* bucket's attempt bundles, which this package does not
govern.

**Requested, in two parts:**

1. **A standing constraint, not a change:** any future
   `LifecycleConfiguration` with an expiry or `NoncurrentVersionExpiration`
   rule on `roman-rapid-products` must not be added without routing it
   through the GC mechanism. Adding one would silently reintroduce a deletion
   path outside the allowlist, horizon, recomputation, fencing and recorded
   plan. Worth a comment in the template saying so.

2. **When GC scope is widened to attempt bundles** (a later, separately argued
   change — H explicitly retains and reports them), the
   `success-expire-90d` rule on `roman-rapid-diagnostics` must be removed or
   neutralized at the same time, or the two mechanisms will both be deleting
   the same objects on different rules.

**Until part 2 lands, rule 21 scores PARTIAL — pending CR**, and H does not
claim otherwise. What H delivers is the repository-side mechanism, fail-closed
and exclusive within this repository's production code.

---

## CR-H7 — Version the simulation input buckets (fix round 1)

The admission manifest records each source object's **immutable version
reference** so a replay can name the exact bytes rather than whatever now sits
at that key. Fix round 1 wired that enumeration into all three ingest scripts,
which now call `head_object` on every input and record whatever `VersionId`
comes back.

**If the input bucket is not versioned, `VersionId` is absent** and the column
is NULL — at which point the manifest's `byte_custody` value
(`external-versioned`) overstates the guarantee, because there is no external
version to pin. The scripts still work and the admission is still recorded;
what degrades is the strength of the replay claim.

**Requested:** enable `VersioningConfiguration: Status: Enabled` on the
simulation input buckets (`roman-rapid-inputs-gbtds-sim` and any sibling the
ingest scripts read), as `rapid-product-buckets.yaml` already does for
`roman-rapid-products` and `roman-rapid-alerts`.

**Until it lands:** record the honest custody. An operator running an ingest
against an unversioned input bucket should pass `byte_custody='none'`, which
says the replay rests on recorded facts alone. This is a one-word change at
each `begin_admission_run` call site and is deliberately left to whoever knows
the deployed bucket configuration rather than being guessed here.

## CR-H6 — Widen the legacy `checksum varchar(32)` columns (restates CR-8)

Already raised by brief D and still unlanded; restated because H depends on
it. `l2files.checksum` is `character varying(32)`
(`006-core-tables.sql:259`), as are `refimages.checksum` (`:394`),
`diffimages.checksum` (`:448`) and three others — **so every SHA-256 written
to them is truncated to 32 characters.**

H works around it rather than waiting: `admission_l2files.source_checksum` is
its own full-width column with the algorithm recorded, and admission identity
never reads the legacy column. The CR still matters for everything else that
does.

---

## Deployment note, not a schema CR

DRAFT 051 grants `SELECT` on `admission_release_pointer` and
`admission_releases` to `rapid_pipeline_write`, and explicitly REVOKEs
`INSERT`/`UPDATE`/`DELETE` on both: a pipeline job that could switch the
release could silently escape the pin it is supposed to obey. Mutation is
`rapid_orchestrator`'s alone, through `derived.set_admission_release`.

No new LOGIN role is introduced by H, so — unlike brief E's `rapid_publisher`
— there is no password-association pass or pgbouncer user line owed here.
