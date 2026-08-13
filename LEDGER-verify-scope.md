# Adversarial scope/constraint verify — brief S (3792447..HEAD)

> **Redacted before commit (supervisor).** This ledger's criterion-8 finding
> originally quoted the SMDC account number verbatim four times — necessarily,
> since reporting the leak was its job. Committing it as written would have
> reintroduced the very defect it found, in the same public repo. Every
> occurrence is now `<SMDC-ACCOUNT>`; the finding, the live hook output and the
> line citations are otherwise untouched. The leak itself was fixed by amending
> `3ed9b93` (now `0922242`); `.githooks/pre-push --scan HEAD` reports clean.

Read-only verify. No edits made. All checks run from this repo (`rapid`,
branch `smdc`) only; rapid_systems/rapid_plan/rapid_docs were not opened —
repo isolation was checked from this repo's diff/history instead.

## 1. RAPIDDB frozen — RESPECTED
`git diff 3792447..HEAD -- pipeline/repositories/` empty. Only mention of
`RAPIDDB` in the whole diff is a prose line in `notes-s-evidence.md:67`
("No migration, no RAPIDDB change…"), not code. `submission_for_attempt`
(`submission/protocol.py:350-366`) matches `open_submissions`'s idiom
exactly: module-level SQL constant (`_SUBMISSION_FOR_ATTEMPT_SQL`, one
`SELECT`, no commit), `execute` as first arg, `dict(zip(columns, rows[0]))`
return — same shape as `open_submissions` (protocol.py:323 area, reads via
`execute` and returns dicts). No commit anywhere in the new function.

## 2. No migration, no schema change — RESPECTED
`git diff 3792447..HEAD --name-only | grep -iE '\.sql$|migrat'` → empty.

## 3. No new IAM / env var / constructor dependency — RESPECTED
`ReconcilerService.__init__` signature unchanged (no diff hunk touches it).
`grep -n 'os.environ\|os.getenv'` over the full diff → no hits. S1 builds
`batch_describer(self.batch)` from the existing `self.batch` attribute
(`service.py` `_resolve_submissions`), not a new dependency.

## 4. Horizon VALUES unchanged — RESPECTED
`git diff 3792447..HEAD -- pipeline/reconciler/horizons.py` is empty — the
file isn't touched at all. `beyond_submission_horizon` is imported and
called exactly as before; only the *branching around* the call changed in
`service.py`, not the constants or the horizon function itself.

## 5. Rule 9 (outbox/acceptance-transaction) untouched — RESPECTED
`git diff 3792447..HEAD --name-only | grep -iE 'writer\.py|alert_production\.py|outbox'`
→ empty. No changes to `writer.py`, `alert_production.py`, or outbox logic.

## 6. Repo isolation — RESPECTED
No file outside `rapid/` in the diff (`git diff --stat` lists only files
under this repo). `scripts/brief-s-acceptance-on-rapid-admin.sh:52-56` uses
`MIGRATIONS_SRC=${MIGRATIONS_SRC:-../rapid_systems/cloudformation/db-migrations}`,
same pattern as `brief-r-acceptance-on-rapid-admin.sh:52-54` — a read-only
sibling checkout staged into a tarball (`tar -czf`, line 106 both scripts),
never written to. This matches the brief's sanctioned exception.

## 7. Nothing executes locally — RESPECTED
`brief-s-acceptance-on-rapid-admin.sh` contains no `docker run`, no local
psql/pytest invocation — it stages a tarball to S3 and drives execution via
`aws ssm send-command`, polling `get-command-invocation` (line ~161-177),
never `aws ssm wait`. Account guard: `ACCOUNT=$(aws sts get-caller-identity
--query Account --output text)` derived at runtime, comment explicitly notes
this is because the account number must never be written down (script
header, lines ~34-36). `contract-brief-s-on-rapid-admin.sh` has
`trap cleanup EXIT` / `trap 'exit 130' INT TERM` (lines 68-69) — but that
script is itself invoked via SSM on rapid-admin, not locally (its `docker
run -p 55439:...` executes remotely). No stub-tier-only carve-out is
violated — the only local-eligible tier (no-I/O pytest) is not what these
scripts run.

## 8. Account-number guard — **VIOLATED** (highest severity)
`notes-s-evidence.md:260` contains a literal, unredacted SMDC account
number:
```
prefix (`s3://rapid-build-artifacts-<SMDC-ACCOUNT>/db-migrations-staging/
```
Confirmed against the live hook, not just by inspection:
```
$ .githooks/pre-push --scan HEAD
pre-push: BLOCKED [smdc-account] notes-s-evidence.md:260: <SMDC-ACCOUNT>
pre-push: BLOCKED [aws-account-12digit] notes-s-evidence.md:260: <SMDC-ACCOUNT>
pre-push: scan of HEAD found violations
```
`.githooks/pre-push` is active (`git config core.hooksPath` → this repo's
own `.githooks`) and this is one of the two
literals intentionally given **no allowlist path** (`SMDC_ACCOUNT` matched
with `no` in the `PATTERNS` heredoc, vs. `aws-account-12digit`/`users-path`
which take `yes`). A real `git push` of this branch would hit `fail=1` and
abort — so the number cannot reach the public remote via the normal path —
but it is a real, unredacted account number **sitting in local git history
right now** (commit `3ed9b93`), on the exact branch slated to merge. It was
introduced despite the script it documents doing the right thing (runtime
`aws sts get-caller-identity`, explicit comment about never writing the
number down) — the evidence note transcribed a runtime-derived S3 path
verbatim instead of redacting it. This is exactly the "cannot be undone by
a later commit" case the brief called out: fixing it now means rewriting
this branch's history before merge, not just adding a follow-up commit.

## 9. Port collision — RESPECTED
`contract-brief-s-on-rapid-admin.sh:56`: `PGPORT_HOST=${PGPORT_HOST:-55439}`,
with a comment at line 54 listing prior briefs' ports (…R 55438) so a
leftover container can't collide. Confirmed 55439 appears nowhere else
under `scripts/` for any other brief.

## 10. Scope creep — RESPECTED, with one note
Diff is limited to: `submission/protocol.py` (+35, the one lookup function),
`pipeline/reconciler/service.py` (S1/S2 wiring), test files, three new
acceptance scripts, and `notes-s-evidence.md`. No drive-by refactors
spotted. `stubs.py`'s `FakeBatch` extension (list_jobs + refusal capability)
is brief-mandated (§5, "test doubles must be refusal-capable"), not
scope creep. `_OPEN_COLUMNS` gained one column (`submission_id`) — required
by S2, not a widening of submission fields onto every row (brief explicitly
forbade widening with submission *fields*; a bare FK id is what §"job
name/queue problem" implies is needed to look the row up, and job_name/
job_queue themselves were correctly kept OUT of `_OPEN_COLUMNS`, fetched
instead through `submission_for_attempt`). The only non-code scope item is
the account-number leak in criterion 8, which is a defect, not creep.

---

## Summary

**VIOLATED: Criterion 8 (account-number guard) — `notes-s-evidence.md:260`
contains the literal SMDC account number `<SMDC-ACCOUNT>` in a public repo's
git history (commit `3ed9b93`).**

All other 9 constraint groups: RESPECTED.
