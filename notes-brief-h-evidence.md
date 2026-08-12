# Brief H — current-head evidence

Every citation below was re-verified against this branch's head (`brief-h`,
branched from `smdc` at `066c353`) or, for `rapid_systems`, read-only against
the sibling checkout at `83f1a38283167132654706ea092d047312f35d4b` and
cross-checked via the GitHub contents API. The brief requires re-verification
rather than trust; where a finding differs from the brief, that is called out.

## The migration seam

`rapid_systems/cloudformation/db-migrations/` holds **44 files, ending at
`043-registration-function-grants.sql`** — confirmed twice, from the sibling
checkout and from `gh api repos/IPAC-SW/rapid_systems/contents/...`. This
repository's `migrations-draft/` holds 044–050. **H numbers from 051.**

## The admission defect (rule 20) — brief's fixed findings, all confirmed

| Finding | Citation | Verified |
|---|---|---|
| `exposures` has a DB-enforced natural key | `006-core-tables.sql:194` — `CONSTRAINT exposurespk UNIQUE (dateobs)` | yes |
| L2 uniqueness *includes* the version | `006-core-tables.sql:330` — `CONSTRAINT l2filespk UNIQUE (expid, sca, version)` | yes |
| `addExposure` is select-then-insert | `008-functions.sql:290-293` — `select expid into expid__ from Exposures where dateobs = dateobs_;` then `if (expid__ is null)` | yes |
| `addExposure` OVERWRITES on repeat, destroying the ingest stamp | `008-functions.sql:331-345` — the `else` branch's `update Exposures set ... created = now()` | yes |

The `created = now()` in the update branch is the sharpest half: rule 20 says a
repeat *returns* its existing admission, and this mutates it — the original
admission timestamp is unrecoverable afterwards.

## The GC scope and the lifecycle-expiry route (rule 21)

**This corrects the brief, in H's favour, and the correction is load-bearing
for what rule 21 scores.**

The brief states that "S3 lifecycle rules delete objects on tag expiry ...
entirely outside the allowlist, horizon, recomputation, fencing and recorded
plan", and instructs H to draft a CR removing that route so the two-pass
process becomes the system's only deletion path.

Read against the authoritative templates, the expiry rules do not reach the
declared scope:

- `cloudformation/rapid-product-buckets.yaml` is **74 lines**, defines exactly
  two buckets — `roman-rapid-products` (:23) and `roman-rapid-alerts` (:47) —
  and contains **no `LifecycleConfiguration` at all**. Both carry
  `VersioningConfiguration: Status: Enabled` (:29-30, :53-54).
- Every expiry rule in the account's storage templates is in
  `cloudformation/rapid-storage-buckets.yaml`, and each lands on a bucket
  outside the declared scope:

  | Rule | Line | Bucket |
  |---|---|---|
  | `ExpirationInDays: 90` | :202 | `roman-rapid-logs` |
  | `success-expire-90d` (`retention-class=success`) | :489-491 | `roman-rapid-diagnostics` |
  | `NoncurrentVersionExpirationInDays: 30` + `ExpiredObjectDeleteMarker` | :568-569 | `roman-rapid-meta` |
  | `ExpirationInDays: 180` | :760 | `roman-rapid-build` |

The tag-expiry route `pipeline/reconciler/retention.py`'s docstring describes
is therefore real, but it acts on the **diagnostics** bucket — attempt
bundles — and the declared GC scope in this package is the **products bucket
only**. Inside the declared scope there is, today, no lifecycle expiry route
at all.

**What this changes.** The residual rule-21 gap is narrower than the brief
assumed: it is not that a competing deletion route runs against the objects H
governs, but that the *bundle* retention scheme deletes on tag expiry in a
bucket H does not govern. The CR in the ledger is written to that corrected
finding. H still does not claim rule 21 CONFORMS — the exclusivity claim is
repository-scoped, and widening GC scope to the diagnostics bucket is
explicitly out of scope here.

**The declared scope is `roman-rapid-products` only.** The records, diagnostics,
backup, `rapid-pipeline-files`, logs, meta, build and simulation input buckets
are out of scope and no plan may name them.

## Deletion routes in this repository today

`grep -rn 'delete_object' --include='*.py'` over `pipeline submission database
observability alerts aws modules scripts` returns **zero matches**. There is no
object-deletion code in this repository at all before H.

`aws s3 rm` appears only in per-suite acceptance-harness cleanup, removing that
run's own S3 staging prefix — ten call sites, every one under a `test/`
directory or `scripts/`, e.g. `pipeline/runtime/test/run-on-rapid-admin.sh:211`,
`pipeline/registration/test/run-fixd-chain-on-rapid-db.sh:134`. These are the
enumerated approved exclusions for the criterion-11 exclusivity assertion.

## pgBackRest retention (the horizon input)

`cloudformation/rapid-postgres-pgbackrest.conf:32` sets
`repo1-retention-full=4` — four full backups, weekly full + daily differential
with continuous WAL archiving. That is a *count*, not a duration: the
authoritative PITR duration is not stated anywhere in the repo, and the cadence
is expressly provisional. The horizon is therefore an external input that
**fails closed** — a GC run with no configured horizon deletes nothing and says
why. CR-H4 in the ledger requests the authoritative duration.
