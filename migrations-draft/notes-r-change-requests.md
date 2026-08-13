# Package R — change requests against `rapid_systems`

**Not landed, and not landable by this package.** `rapid_systems` is
single-writer custody and R never edits it; R's own contract excludes it
explicitly. The item below is CR text for its owner.

The `rapid_systems` side was verified READ-ONLY, from the sibling checkout at
`83f1a38283167132654706ea092d047312f35d4b`. No clone, no checkout, no edit.

---

## CR-R1 — the reconciler and publisher units must carry the release identity

**Files:** `cloudformation/rapid-reconciler-service.yaml` and
`cloudformation/rapid-alert-publication.yaml`.

**What to add**, mirroring `rapid-vpo-service.yaml:348-359` exactly — the same
two facts, resolved the same way at deploy time:

```
Environment=RAPID_IMAGE_DIGEST=__IMAGEDIGEST__
Environment=RAPID_RELEASE_IDENTITY=__RELEASE__
```

with the same `sed -e "s|__RELEASE__|${RELEASE_IDENTITY}|g"` substitution the
VPO stack already performs (`rapid-vpo-service.yaml:412`), and
`RELEASE_IDENTITY="smdc-${IMAGE_TAG%%-*}"` derived as it is there (`:306`).
The reconciler currently passes only `-e RAPID_RECONCILER_ROLE_ARN`,
`-e RAPID_DB_SECRET_ID` and `-e RAPID_RECONCILER_POLL_SECONDS` (`:226-228`);
the publisher's unit sets only `AWS_REGION` and `PYTHONUNBUFFERED`
(`:529-530`).

**Why it is needed.** R2 adds rule 18's application-half preflight to all five
entry points. That check is FAIL-CLOSED by design: a process that cannot state
its own release refuses to start. Two of the five units do not supply the
facts it reads, so **on the next deploy of those two units the reconciler and
publisher would exit `EXIT_START_FAILED` and restart-loop** — correct code
refusing an incomplete deployment, but a service outage all the same.

**This is the same defect this stack has already had once, and its own comment
records it** (`rapid-vpo-service.yaml:340-346`, 2026-08-08): `submission_env`
read three variables with no defaults, the VPO unit set none of them, "the
service could not have submitted anything, and had only escaped failing
because gathering found no work." The variables were added to that unit and
not to the other two. R2 is what makes the omission observable at startup
instead of at first use.

**The environment policy admits these**, and the VPO comment states the
reasoning to reuse verbatim: they are per-deployment IDENTITY facts, not
configuration, so the environment is their proper home. `RAPID_IMAGE_DIGEST`
should be extracted from each unit's OWN `ImageRef` at deploy time rather than
passed as a second parameter — a separate pin could disagree with the image
actually running, which is the skew the execution binding exists to prevent.

## The ordering constraint, and how R avoids being blocked by it

**This CR must land before R2's branch is deployed** — not before it is
merged. The two are independent as code: R2's tests pass without it (they
drive the preflight functions directly, and the environment they assert on is
the one the test controls), and the check is correct in either order.

R does not gate on it, and does not weaken the check to route around it.
`require_image_digest=False` would make the reconciler and publisher start
without their digest, which is precisely the misdeployment the check exists to
catch — it would convert a loud, diagnosable start failure into the silent
unattributable-results state rule 18 forbids. A fail-closed check whose
failure is inconvenient is still the check working.

**Suggested landing order:** CR-R1 into `rapid_systems`, deploy the two units,
then deploy R2. If R2 reaches a deploy first, the two services will refuse to
start with `ApplicationContractUnmet` naming exactly which variables are
missing — a diagnosable failure with a one-line fix, not a mystery.
