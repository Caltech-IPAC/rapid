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

## Two schema facts the brief states incorrectly

Both were caught by verifying against the head rather than trusting the brief,
and both would have produced SQL that fails to execute or silently mis-joins.

1. **`work_units` has no `unit_key` column, and its primary key is
   `work_unit_id`, not `unit_id`.** `036-intent-schema-v1.sql:111-122` gives
   the table `work_unit_id`, `job_type`, `input_scope`, `operational_class`,
   `definition_version`, `state`, `blocked_reason`, `superseded_by_unit_id`,
   `created_at`, `updated_at`. The persisted unit identity is
   `(job_type, input_scope)`. `ProcessingUnit.key` — the value
   `product_prefix()` interpolates — is the declared-subject tuple joined with
   `/`, and `input_scope` is that same grammar with the leading `job_type`
   element dropped (`submission/subjects.build_input_scope`, wrapped at
   `pipeline/seams.py:411`). The canonical round trip therefore rebuilds the
   unit key as `job_type || '/' || input_scope`, which is what
   `reference_sql.attempt_facts` does.

2. **`superseded_by_unit_id` DOES exist** — `036-intent-schema-v1.sql:118`,
   with its FK at `:123-124`, its self-reference CHECK at `:149`, and the
   partial unique index on `superseded_by_unit_id IS NULL` at `:185-190`. The
   brief says "there is no `superseded_by_unit_id` column; do not invent one"
   while defining FULLY DISCHARGED.

   **The brief's ruling is nonetheless implemented exactly as written**, and
   the correction does not change the code. The instruction's *substance* is
   that FULLY DISCHARGED must not be read as the schema's supersession
   concept — and it is not: `is_fully_discharged` tests
   `state IN ('complete','cancelled')`, the registration watermark, and the
   live-attempt count, and never consults supersession. The column's existence
   is recorded here only so the next reader is not misled into thinking a
   supersession-based predicate was unavailable rather than deliberately
   unused. Using it would in fact be wrong: a unit can be superseded from any
   state, including `ready`, so a superseded unit is not thereby discharged.

## The re-derived reference set — three classes the brief's list omits

The brief requires the reference set be **re-derived** by enumerating every S3
write path in the declared scope before the anti-join is written, and that
anything found beyond its list be added and recorded as a finding. That
enumeration found three, each of which would have been classified as
unreferenced garbage:

1. **HATS catalogs written by `aws s3 sync`** —
   `pipeline/generateSourceHATSCatalog.py:237` and
   `generateLightCurveHATSCatalog.py:384` recursively sync a whole local
   directory to `<products-bucket>/<catalog_name>`. The prefix is **stable and
   shared, carrying no run or attempt id**, configured from the legacy INI key
   `JOB_PARAMS/product_s3_bucket_base`, and successive runs overwrite by
   design. This is inside the declared scope, has no database reference of any
   kind, and is not attempt-scoped — so it is retained twice over: once as
   unattributable, once as not-on-the-allowlist.

2. **Content-addressed configuration snapshots** —
   `pipeline/runtime/termination.py:217` writes
   `{prefix}/config-snapshots/sha256/{digest}.json` through `put_if_absent`.
   Being content-addressed, **one object is shared by every array child of
   every attempt that resolved to the same configuration**. Attempt-scoped
   reasoning is therefore actively wrong for this class: the attempt that
   happened to write it may be long discharged while a thousand live attempts
   still depend on it. Retained.

3. **The `unidentified-attempt` degraded prefix** —
   `pipeline/stages/context.py:184` returns
   `{job_type}/{unit.key}/unidentified-attempt` when `run_id` or `attempt_id`
   is absent. It carries **no run or attempt identity at all**, so canonical
   round-trip attribution cannot reconstruct it and refuses it. This is a real
   key shape in the products bucket, not a hypothetical, and it is exactly the
   "parse is not a round trip" case: a naive parser looking for `attempt-N`
   finds nothing and might treat the object as legacy-layout garbage.

Two further findings that shape the design rather than the reference set:

- **Published-but-unregistered is a NORMAL state, not orphan evidence.**
  `pipeline/registration/products.py:273` selects the registering difference
  image **by role binding** — an attempt publishes three difference-image
  variants and exactly one is registered. The other two are permanently
  unreferenced by construction. This corroborates the brief's decisive
  constraint from the registration side.

- **`reference_image_uri` is a CROSS-ATTEMPT reference**
  (`submission/payloads.py:341`). A reference image published by one attempt is
  cited by many later science manifests, so attempt-scoped product deletion
  would break work that has nothing to do with the producing attempt. The four
  URI fields in the payload family are exactly `science_image_uri` (:308),
  `psf_uri` (:332), `reference_image_uri` (:341) and `coadd_inputs_uri` (:433)
  — enumerated from the dataclasses rather than taken from the brief's partial
  list, as required.

Also confirmed: `pipeline/reconciler/retention.py`'s `put_object_tagging`
(:219) is a **full-set rewrite** with no merge, so any GC hold expressed as an
object tag would be silently dropped by the next classification. H therefore
expresses its fence in the database, never as an S3 tag.

## pgBackRest retention (the horizon input)

`cloudformation/rapid-postgres-pgbackrest.conf:32` sets
`repo1-retention-full=4` — four full backups, weekly full + daily differential
with continuous WAL archiving. That is a *count*, not a duration: the
authoritative PITR duration is not stated anywhere in the repo, and the cadence
is expressly provisional. The horizon is therefore an external input that
**fails closed** — a GC run with no configured horizon deletes nothing and says
why. CR-H4 in the ledger requests the authoritative duration.
