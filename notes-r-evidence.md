# Package R — evidence

Two wiring completions the 2026-08-12 re-score identified as rapid-side and
fully specified. Both are wiring: R adds no schema, no new component, and no
new capability — it connects machinery that was already built and already
tested, and was not reachable on the path production actually takes.

Branch `smdc-r-residual`, off `origin/smdc` @ `4b20a0f`.

## R1 — the live registrar wires `identity_repository` (rule 10)

### What was dormant, and how that was confirmed

Two call sites build a registrar. Only one passed an `identity_repository`:

| Site | Passed it? | Reached in production? |
|---|---|---|
| `pipeline/entrypoints/job.py:registrar_for` | yes | no — `JOB_TYPE_REGISTRATION`, a route the registration consumer never takes |
| `pipeline/operator/registrar.py:production_registrar` | **no** | yes — the operator service's own registration pass |

So the only branch production ever ran was `registration/products.py`'s
`identity_repository is None` — the branch its own comment calls "the
pre-rollout path". `products` and `artifacts` were never populated by a live
pass.

This was not inferred for this package: `pipeline/gc/references.py:21-26`
already records it as a live-path fact, and records the consequence — an
anti-join keyed on `artifacts.uri` would classify EVERY REAL PRODUCT as
unreferenced garbage, which is why the GC candidate rule is written as
positive identification instead.

### The wiring

`for_connection(conn)` now passes `identity_repository=...` built over the
SAME connection the pass borrows for its legacy handle. Product rows,
artifact rows, legacy version rows and the registration watermark therefore
commit or roll back together.

Built per connection, inside `for_connection`, rather than once in the
enclosing factory. The operator's three phases each open their own
registration connection; one repository built once could only ever belong to
one of them. That is the same reason this layer is a factory at all
(round-4 finding #2), now applying to the repository.

### The probe, which is the part that is not obvious

**Wiring it unconditionally would have been a defect, not a completion.**
DRAFT 048 is not in the authoritative migration stream, so the live registrar
runs against databases with no `products`/`artifacts` until that change
request lands. On such a database:

1. `register_identity` calls `repository.upsert_product` first thing;
2. `ProductRepository._query` catches the driver's UndefinedTable and
   re-raises it as `RepositoryQueryFailed`;
3. nothing between there and the registration consumer catches it;
4. the consumer's per-attempt transaction rolls back.

Every registration would fail, permanently, for want of a table the legacy
path does not need — a durable rejection, which is exactly what D's P8
forbids ("legacy-only, log why, never invent a key, never durably reject").
The brief's own contract states the required behaviour: "without 048 it
degrades legacy-only."

`identity_repository_for` therefore asks the catalog first
(`_IDENTITY_TABLES_PROBE`, all three of 048's tables) and returns None when
they are absent, logging at WARNING why. Probed rather than discovered by
catching `UndefinedTable`, following the rule
`pipeline/repositories/alert_outbox.py:53-65` states for this same pair of
tables: a failed statement ABORTS the surrounding transaction, and this runs
inside a transaction that also carries the legacy rows and the watermark, so
recovering with a rollback would discard the caller's own writes. One probe
per registration pass, not per attempt.

### Tests

`pipeline/contract/test_live_registrar_identity.py`, three tests, all
driving `production_registrar()` itself — a test that built a registrar the
way `job.py` does would have passed throughout the dormancy, because that
path was always wired. The distinction between the two construction paths IS
the defect.

| Test | Asserts |
|---|---|
| `test_the_live_registrar_passes_an_identity_repository` | 048 present: a `ProductRepository` is passed, on the pass's own connection |
| `test_each_phase_gets_a_repository_on_its_own_connection` | two connections, two distinct repositories, each bound to its own |
| `test_absent_048_degrades_to_legacy_only` | 048 absent: repository is None, no exception, legacy wiring untouched |

## R2 — application-contract preflight in all five entry points (rule 18)

Rule 18: "Services and payloads preflight the **application/schema** contract
at startup" — one contract, two halves. The schema half was checked at four
entry points since brief B and at the fifth since H. The APPLICATION half was
wired into `rapidctl` ALONE; package H scoped that deliberately and recorded
it ("This is NOT extended to the other four").

That left the four that matter most unchecked. `rapidctl` is an operator tool
run by a human who can read a traceback; the other four are the deployed
services and the payload whose results are attributed to a release.

| Entry point | Seam the call was added to |
|---|---|
| `rapid-reconciler` | `pipeline/reconciler/main.py:_preflight_schema` |
| `rapid-operator` | `pipeline/operator/service.py:_verify_work_streams` |
| `rapid-job` | `pipeline/entrypoints/job.py:_database` |
| `rapid-publisher` | `pipeline/publisher/service.py:_preflight_schema` |
| `rapidctl` | already wired by H — asserted here alongside the others |

`require_image_digest` stays at its default TRUE for all four. `rapidctl`'s
`require_image_digest=False` is for a tool run from a shell, which has no
container digest to know; a deployed service and a payload container both
have one, and accepting its absence would accept the misdeployment the check
exists to catch.

Fail-closed verified per entry point rather than assumed: the reconciler,
operator and publisher land in a broad `except Exception` returning
`EXIT_START_FAILED`; the job's last-resort handler returns
`EXIT_UNRECORDABLE`. `ApplicationContractUnmet` is a `RuntimeError`, so all
four paths hold.

### Tests

`ApplicationPreflightTests` in `pipeline/contract/test_publisher_startup.py`
— the file that already enumerates all five entry points and asserts
properties across the set. Each test drives the entry point's OWN preflight
function with the release identity removed from the environment and asserts
`ApplicationContractUnmet`.

**What makes them able to fail:** the stub connection SATISFIES the schema
half (it answers `schema_migrations` with every filename
`REQUIRED_MIGRATIONS` names, read from that tuple rather than hard-coded so
the stub cannot drift and start failing for the wrong reason). So nothing
else in those functions raises, and deleting any one
`verify_application_contract` call fails that entry point's test alone.

## Doubles this change invalidated, and why that is the doubles working

`pipeline/operator/test/test_registrar.py` modelled `products.registrar`'s
real signature — `(dbh, store, fallback_roles=None)` — rather than absorbing
everything through `**kwargs`. Its own comment says why: "Tracking the real
signature is what makes this fake able to refuse a wrong call."

It refused. Adding `identity_repository=` to the live call made both
`RegistrarConnectionTests` raise `TypeError`, and the connection doubles
(bare `object()`) raised `AttributeError: no attribute 'cursor'` once the
probe began asking the catalog. Both are the doubles correctly detecting that
the contract they model had changed — not incidental breakage. They are
updated to track the new signature and to ANSWER the probe (`FakeConnection`,
whose `present` flag puts the wiring on either branch deliberately).

## Acceptance runs

Run 1 (`brief-r-20260812T112803Z`) was launched before those two doubles were
fixed and is recorded here rather than discarded, because it is the run that
proved R1's live-path assertions and the zero-skip gate:

```
BRIEF-R-PASS2-SKIPS: PASS exit=0 (zero skips, as the brief requires)
BRIEF-R-R1-LIVE-WIRING:     exit=0  1 passed, 2 deselected
BRIEF-R-R1-PER-CONNECTION:  exit=0  1 passed, 2 deselected
BRIEF-R-R1-DEGRADES-LEGACY: exit=0  1 passed, 2 deselected
BRIEF-R-R1-REGRESSION-SUITE: exit=1  2 failed, 7 passed   <- stale doubles
BRIEF-R-R2-*:                exit=1  4 failed             <- stub cursor
BRIEF-R-OVERALL: FAIL exit=1
```

Both red causes were in TEST DOUBLES, not production code: the `TypeError`
above, and `_StubCursor` lacking `description` —
`ConnectionExecutor.execute` reads that attribute to decide whether a
statement produced a result set, so the stub raised before any assertion was
reached. The publisher's test passed in run 1 because its preflight is
called on a raw cursor, not through `ConnectionExecutor`, which is the same
asymmetry the production code has.

### The fix probe (database-free), rapid-admin, `exit=0`

Rather than spend an acceptance run on an unverified fix:

```
PROBE-R2: 5 passed, 6 deselected      exit=0   (the five preflight tests)
PROBE-R1-REGRESSION: 9 passed          exit=0   (the ported registrar suite)
PROBE-R-OVERALL: PASS exit=0
```

### Run 2 — LAUNCHED, RESULTS NOT RETRIEVED (credential expiry)

Run 2 (`brief-r-20260812T113253Z`, SSM command
`deca040b-413c-4bd9-ba62-3ba8f4579836`) was launched with both fixes and the
mutation check. **Its results were never read.** The laptop's AWS SSO token
expired at 10:39Z, mid-run; the polling loop died with it. Re-authentication
was attempted twice — the browser flow and `--use-device-code` — and both
require a human to approve, which an unattended run has not got. Neither
attempt wrote a usable token.

So run 2's verdict is UNKNOWN, not green and not red: the SSM command
probably ran to completion on the host, but nothing here read it. It is
recorded that way deliberately rather than assumed successful — an
unretrieved result is not evidence.

**What that leaves unproven: the MUTATION CHECK.**
`scripts/mutation-brief-r-on-rapid-admin.sh` is committed and its six sed
expressions were each verified to match exactly one line in the working tree,
but it has never been EXECUTED. R1's mutations need 048's tables, so it
cannot run in CI (which builds from the authoritative stream alone). The
claim in `test_live_registrar_identity.py` that the wiring assertion was
"verified by doing exactly that" is therefore NOT yet true, and that
docstring is corrected to say so.

### CI run (the contract's sanctioned alternate venue)

`contract-tests.yml` on `smdc-r-residual`, dispatched via
`workflow_dispatch`. It needs no laptop AWS credentials. It builds its
database from the AUTHORITATIVE STREAM ONLY, so it exercises R2 fully (no
database needed) and SKIPS R1's three tests (048 absent) — which is the
CI-green property, not a gap.

Run **31592979558**, head `718d112`, conclusion **success**, `CI_EXIT=0`.
Every step green, including "entry points launch" — all five console scripts
resolve and import with R2's preflight in place:

```
BRIEF-B-ENTRYPOINT-rapid-reconciler: resolved exit=70   (starts, refuses: no AWS env)
BRIEF-B-ENTRYPOINT-rapid-operator:   resolved exit=0
BRIEF-B-ENTRYPOINT-rapid-job:        resolved exit=0
BRIEF-B-ENTRYPOINT-rapidctl:         resolved exit=0
BRIEF-B-ENTRYPOINT-rapid-publisher:  resolved exit=70   (as above)
```

**That R2's tests RAN rather than skipped is established by arithmetic
against run 1's PASS 1**, which used the identical substrate (base stream,
no drafts) before the doubles were fixed:

| | passed | failed | skipped |
|---|---|---|---|
| run 1 PASS 1 (pre-fix) | 294 | **4** | 237 |
| CI (post-fix) | **298** | 0 | 237 |

The four failures became four passes (294 + 4 = 298) and **the skip count is
unchanged at 237** — so the R2 tests were collected and executed in both, not
silently skipped in the green one. That is the check the zero-skip gate makes
on the rapid-admin side, done here by difference because CI runs `-q`.

## What remains open

1. **The mutation check has not been executed.**
   `scripts/mutation-brief-r-on-rapid-admin.sh` is committed and wired into
   the runner as a seventh verdict term; its six sed expressions were each
   verified to match exactly one line. It has never run: R1's mutations need
   DRAFT 048's tables, so rapid-admin is the only venue, and the run carrying
   it lost its credentials. **To close:** re-run
   `scripts/brief-r-acceptance-on-rapid-admin.sh` with valid SSO.

2. **CR-R1 is unlanded** (`notes-r-change-requests.md`). The reconciler and
   publisher units do not supply `RAPID_RELEASE_IDENTITY` /
   `RAPID_IMAGE_DIGEST`, which R2's fail-closed preflight reads. Deploying
   R2 to those two units before the CR lands makes them refuse to start.
   This is `rapid_systems` custody and outside R's scope.

3. **Run 2's verdict is unknown**, not green — see above. R1's three
   contract tests were proven green in run 1, on a database with 048
   applied, so what run 2 would have added is the mutation check and a
   re-confirmation.

## Scope

R touched only what its contract named: the two wiring sites, their tests,
the doubles those changes invalidated, R's own harness scripts, and these
notes. No schema was added or proposed. `rapid_systems` was read
READ-ONLY and never edited. No merge, no PR — branch `smdc-r-residual`
pushed, worktree retained.
