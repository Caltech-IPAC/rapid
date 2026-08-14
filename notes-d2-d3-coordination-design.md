# Wave D — process hardening — ledger

> **Why this file is in the repo.** This is the Wave D working ledger from the
> 2026-08-13 overnight improvement campaign, preserved verbatim because its
> D2/D3 sections are the only written specification of the `rapid_systems`-side
> coordination-marker / acknowledgment-gate design. That half was NOT built:
> D2 shipped as a post-hoc CI contract test rather than an applier preflight,
> D3 shipped a rapid-side `pipeline/intent/consumer_readiness.py` predicate that
> nothing yet calls, and no migration-header field was added. The design below
> is the handoff spec for finishing both. It was rescued from the delegate run's
> scratch directory before that directory was deleted; the worktree path and
> branch it names were removed at the end of that run and no longer exist.
> Campaign context: `~/Claude/rapid-threads.md` §2b.

Worktree: `rapid-wt-d` (removed at end of run), branch `campaign/d-process` (based on `smdc`).
`rapid_systems` was read-only throughout — no edits made there; all rapid_systems-side work is
specified below for the supervisor to act on.

Sequence followed exactly as briefed: D5 first (stated sequencing constraint), then D1, D2, D3, D4.

Tree is clean (`git status --short` empty) at the end of this run.

## Commits (all on `campaign/d-process`, none pushed/merged)

| SHA | Subject |
|---|---|
| `53c72abb` | D5: marker-driven operational-suite discovery, replacing the hand-maintained list |
| `ff255014` | D1: contract tests for the applier's ledger/checksum properties |
| `768ca0b5` | D2: live grant-matrix floor for the cumulative REVOKE gap validate.sh cannot see |
| `751e8ca3` | D3: machine-readable consumer-before-schema coordination requirements |
| `ccf605a5` | D4: PR-gating decision -- PROPOSED to stay push-only, not ratified |

---

## D5 — operational suite discovery via markers

**Files changed:**
- `alerts/test/conftest.py` — added `pytest_collection_modifyitems`, auto-marking `test_live_db.py`'s
  two tests `live` by filename. Why: that file is `test_`-prefixed (unlike every other `live_*.py`
  manual-tier module in the tree) so it collects under a bare `pytest`; it already self-skips when
  the DB/AWS env is incomplete, but on a host with ambient credentials (rapid-admin, a laptop with a
  tunnel up) a bare `pytest` would have made a real `psycopg2.connect()` + `boto3` S3 GET. Scoped to
  this directory (not a repo-root `conftest.py`) because a root conftest collides under
  `--import-mode=importlib` with this file's own `from conftest import ...` convention — tried it,
  confirmed the collision (`test_clips.py`/`test_provider.py`/`test_benchmark.py` all failed to
  import `CHIP_PID` etc. because pytest's rootdir-conftest import wins the bare-name race).
- `pipeline/contract/conftest.py` — **bugfix**, found while generalizing the auto-marking pattern:
  its `pytest_collection_modifyitems` looped over pytest's session-wide `items` unconditionally,
  marking **every** collected test `contract`, not just its own directory's, whenever that conftest
  loaded as part of a larger run. This is root-caused as the "whole-tree collection deselects
  everything" quirk your brief flagged: any run spanning `pipeline/contract` and a sibling directory
  (e.g. `pytest pipeline`) deselected the sibling's stub tests too, because they'd all picked up the
  `contract` marker. Fixed to check `item.path` against the conftest's own directory before marking.
  Verified: `pytest pipeline/association pipeline/contract --collect-only` went from "0 selected,
  589 deselected" to "3 selected, 586 deselected" after the fix.
- `pyproject.toml` — `addopts` gained `--import-mode=importlib`; `testpaths` gained `alerts`.
  The importlib-mode change fixes the second pre-existing collection blocker:
  `database/modules/utils/test/` is the only `test/` package in the tree with an `__init__.py`
  while its parents (`database/`, `database/modules/`) deliberately have none (PEP 420 namespace
  packages, per the `[tool.setuptools]` comment already in the file). Prepend-mode import can't
  build a unique dotted path through un-packaged parents and falls back to importing the directory
  as bare top-level `test`, colliding with every other `test/` package in the tree — this was the
  actual mechanism behind the "5 errors under database/modules/utils/test/" baseline your brief
  named. `importlib` mode imports by file path, sidesteps the collision entirely, and needs no
  `__init__.py` added anywhere (so the namespace-package layout is untouched).
- `scripts/run-operational-tests.sh` — the hand-maintained ~48-entry `MODULES` literal replaced by
  `pytest --collect-only -q --import-mode=importlib`, filtered to unique file paths and converted to
  dotted names, against the same `testpaths`/`addopts` `pyproject.toml` declares — discovery and the
  default `pytest` invocation can now never disagree. The per-module isolated-subprocess loop
  (unittest first, pytest fallback), the PASS/FAIL table, and the interpreter-argument interface (the
  script is invoked by ~10 `contract-brief-*-on-rapid-admin.sh` scripts and
  `run-operational-tests-on-rapid-admin.sh` — checked all call sites, none needed a change) are
  byte-identical to before. Discovery finds 65 modules against the old list's 48: every old entry is
  still discovered (`comm -23` of old vs. new = empty, no coverage lost), plus 15 the hand list had
  silently fallen behind on — all 7 of `alerts/test/test_*.py`, 4 pre-existing
  `database/modules/utils/test/test_rapid_db*.py`/`test_roman_tessellation.py` files, and 2 more
  (`pipeline/reconciler/test/test_supersede_lost_evidence.py`,
  `pipeline/stages/test/test_science_fidelity.py`, `submission/test/test_manifest_wire.py`,
  `submission/test/test_typed_payloads.py` — none of these had an obvious reason for the old list's
  omission; likely just drift).

**"Is bare `pytest` safe?" — YES: safe, but NOT green. These are different claims and are kept
separate below on the supervisor's correction (their run against 53c72abb: 1848 passed / 25 failed /
5 skipped / 588 deselected, exit 1).**

**What "safe" means and what was proven:** a default `pytest` invocation (no `-m` override, no env
var required to avoid touching live infrastructure) never opens a real database connection or makes a
real AWS call. That property holds:
```
$ uv run --with pytest --with fastavro --with psycopg2-binary --with boto3 \
    --with numpy --with astropy --with photutils --with reproject --with fitsio \
    --with scipy --with pandas python3 -m pytest --collect-only -q
1878/2466 tests collected (588 deselected) in 0.94s   # exit 0, ZERO collection errors
```
Collection is clean (the pre-existing 5-collection-error quirk under `database/modules/utils/test/`
is fixed as a side effect of the `importlib` mode change, needed anyway for D5's own discovery step —
confirmed pre-existing by `git stash`-and-rerun before my changes, same 5 errors, not something I
introduced then fixed). Every `contract`/`live` test is correctly deselected (588 = 586 contract + 2
live), so nothing in a default run reaches for a live database or AWS credentials. **This is the
property the campaign asked D5 to prove, and it holds.**

**What "safe" does NOT mean, and where I initially got the ledger wrong:** it does not mean the run is
green. I originally reported a full run using `RAPID_SW=$(pwd) pytest -q`, which masked a second
pre-existing environmental gap — 12 tests in `submission/test/test_gathering.py`
(`CoaddInputsTests`/`GatherReferenceUnitsTests`) call `gathering.coadd_input_rows(...)` without first
patching `RAPID_SW` into their own environment (unlike the one test in that file that does,
`test_the_window_comes_from_release_content_by_default`), so they raise
`pipeline.runtime.errors.ConfigError: RAPID_SW is not set, so the release's...` under a genuinely bare
invocation. Rerun with no `RAPID_SW` set at all, matching what a developer or CI actually gets by
default:
```
$ uv run --with pytest --with fastavro --with psycopg2-binary --with boto3 \
    --with numpy --with astropy --with photutils --with reproject --with fitsio \
    --with scipy --with pandas python3 -m pytest -q
25 failed, 1856 passed, 5 skipped, 599 deselected, 427 subtests passed   # exit 1
```
(599 deselected here vs. 588 at 53c72abb because this run is at the tip commit, after D1-D3 added more
stub/contract-marked tests. Re-measured directly rather than computed from memory: checked out
53c72abb into a disposable worktree and reran the identical command there —
**25 failed, 1847 passed, 6 skipped, 588 deselected, exit 1** — matching the supervisor's reported
25 failed / 588 deselected exactly, with 1847 passed / 6 skipped here against their reported 1848 /
5 (a 1-test difference not chased further; both numbers agree on the fact that matters, 25 failures,
same cause, both against 53c72abb). The 25 failures and their two root causes, below, are identical
at both 53c72abb and the tip.)

**So: a default `pytest` run is safe (never touches live infrastructure) but not green (25
pre-existing failures surface once `alerts/` and the rest of the tree are actually discoverable). A
developer running a bare `pytest` today should expect exactly this: 25 failed, ~1850-1870 passed
depending on exact commit, exit 1 — and should read the failures as two known, pre-existing,
out-of-scope gaps (below), not as something D5 broke.**

**Both failure groups confirmed pre-existing, not introduced by D5:**

1. **13 failures in `alerts/test/`** — confirmed pre-existing by the supervisor against baseline commit
   `e22faf00` (`alerts/test/test_provider.py` alone: 9 failed / 3 passed there). All trace to one root
   cause: `KeyError: "Source row is missing expected columns: ['id'] (renamed or dropped in
   storage?)"` raised at `alerts/providers.py:174` (a `Source.from_row(strict=True)` column-presence
   check), hit by `test_benchmark.py::test_benchmark_writes_wellformed_timing_log`,
   `test_clips.py::test_clip_centers_on_zero_based_source_position`,
   `test_clips.py::test_clip_wcs_reproduces_catalog_position`, and 8 tests in `test_provider.py`; plus
   one unrelated `TypeError: '>' not supported between instances of 'NoneType' and 'int'` in
   `test_benchmark.py::test_memory_helpers_available`.
2. **12 failures in `submission/test/test_gathering.py`** — confirmed pre-existing: the file is
   byte-identical between `e22faf00` and this branch's HEAD (`git diff --stat e22faf00 HEAD --
   submission/test/test_gathering.py` is empty), and the failure reproduces on the committed tip with
   no working-tree changes present. `CoaddInputsTests`/`GatherReferenceUnitsTests` read `RAPID_SW`
   from the ambient environment without patching it (one sibling test in the same file, `test_the_
   window_comes_from_release_content_by_default`, does patch it correctly and passes) — a genuine
   environmental prerequisite this test file has always had, invisible before because
   `submission/test/test_gathering.py` was never run as part of a truly bare invocation (the old
   `run-operational-tests.sh` module list included `submission.test.test_gathering` but every
   documented invocation of that script — see `run-operational-tests-on-rapid-admin.sh`,
   `contract-brief-*-on-rapid-admin.sh` — sets `RAPID_SW` before calling it).

**Both are follow-up items, not fixed here and not this wave's to fix (out of scope: D5 is tier
plumbing, not a bugfix to `alerts/providers.py` or `submission/test/test_gathering.py`'s fixture
setup). Not marked xfail, skipped, or excluded — doing either would undo the exact thing D5 exists to
prove (that these tests are real and reachable).** Flagging explicitly for a follow-up: someone who
owns `alerts/providers.py`'s row-shape contract, and someone who owns `test_gathering.py`'s fixture
setup (likely a one-line fix: patch `RAPID_SW` in `CoaddInputsTests`/`GatherReferenceUnitsTests`
`setUp()` the same way the one already-passing test in that file does), should each take a pass.

**Command log** (all under `uv run --with pytest --with fastavro --with psycopg2-binary --with boto3
--with numpy --with astropy --with photutils --with reproject --with fitsio --with scipy --with
pandas python3 -m pytest ...`, laptop, stub-tier only per the MVT amendment A-3 carve-out):

| Command | Result | Exit |
|---|---|---|
| `--collect-only -q` (pre-fix baseline) | 5 collection errors, `database/modules/utils/test/*` | interrupted |
| `--collect-only -q --import-mode=importlib` | 2366 collected, 0 errors | 0 |
| `pipeline --collect-only -q` (pre-fix, contract-conftest bug live) | 0 selected, 1731 deselected | 0 (silently wrong) |
| `pipeline --collect-only -q` (post-fix) | 1145/1731 selected, 586 deselected (=contract count) | 0 |
| `--collect-only -q` (post D5, full tree) | 1878/2466 selected, 588 deselected | 0 |
| `-q`, no `RAPID_SW` (genuinely bare, at tip after D1-D4) | **25 failed**, 1856 passed, 5 skipped, 599 deselected | 1 |
| `-q`, no `RAPID_SW` (53c72abb / D5 alone; supervisor reported 1848/25/5/588, I independently re-measured in a disposable worktree) | **25 failed**, 1847 passed, 6 skipped, 588 deselected | 1 |
| `RAPID_SW=$(pwd) -q` (masks the 12 test_gathering.py failures — do not use this to judge "is bare pytest green") | 13 failed, 1868 passed, 5 skipped, 599 deselected | 1 |
| `bash scripts/run-operational-tests.sh python3` (persistent venv, package installed editable, `RAPID_SW` set — matches how every documented call site invokes it) | 65/65 modules; 4 `alerts` modules show the same 13 failures | 1, `RESULT: FAIL` (correctly) |

---

## D1 — CI-through-applier tier

**File added:** `pipeline/contract/test_applier_ledger.py` (5 tests).

**What it tests and why this shape, not another:** the gap is that CI's contract job applies the
stream through `scripts/run-contract-tests.sh`'s own loop (`psql -f`, then a bare `INSERT ... ON
CONFLICT DO NOTHING`) — it reproduces the schema but exercises none of the real applier's own ledger
logic (checksum recording, checksum comparison, refusal on a changed already-applied file). The real
applier (`rapid_systems/cloudformation/apply-db-migrations.sh`) cannot run verbatim in this CI job:
it's bash built for SSM delivery to a podman container on rapid-admin, stages files through a
`STAGE_DIR` a remote host populates, and this workflow deliberately holds no AWS credentials. I chose
to restate the applier's **SQL shape** directly (`_apply_via_ledger()`: the same `INSERT ... ON
CONFLICT (filename) DO NOTHING`, the same `SELECT sha256 ... WHERE filename = ...` comparison, the
same refusal) rather than either (a) trying to shell out to a stub/mock of the applier — which would
test a re-implementation, not the property — or (b) the easiest option, a test that CI's current loop
is unchanged, which would prove nothing about the actual gap. Ran the applier script's own
"pending-files" section (read in full) to get the exact statement shapes before writing this.

Tests: first-apply records filename+sha256; an unchanged re-apply is skipped not re-executed (proven
with a `CREATE TABLE` that would raise `DuplicateTable` on a genuine second execution); a changed hash
on an already-applied file raises and names the file; a NULL recorded sha256 (pre-070 rows) is treated
as "nothing to check against" not a mismatch; a malformed hash is rejected by 070's own
`schema_migrations_sha256_shape_ck` CHECK constraint (asserted against the real constraint, not a
Python re-derivation of it). All use temp-table shadowing of `schema_migrations`
(`test_schema_preflight.py`'s existing technique) so the shared table's rows — every other contract
test's fixture rows — stay untouched.

**No workflow change needed.** `scripts/run-contract-tests.sh` already runs `pytest pipeline/contract
-m contract` as a directory sweep, so this file is picked up by the existing job automatically.

**Deliberately not covered:** the applier's advisory-lock discipline across concurrent runs — a
distinct property needing two genuinely concurrent invocations (this directory's `second_conn`
fixture, used by `test_association_claim_order.py`/`test_work_unit_cas.py`, is the existing pattern
for that shape). Named as a follow-on, not silently assumed covered.

**Verification:** written and marked `contract`; **not run** locally (no database on the laptop, per
project policy). Verified via:
```
$ uv run --with pytest --with psycopg2-binary python3 -m pytest pipeline/contract/test_applier_ledger.py \
    --collect-only -q -m contract --import-mode=importlib
5 tests collected   # exit 0, all correctly auto-marked `contract` (via the D5 conftest fix)
```

---

## D2 — pre-apply grant lint

**File added:** `pipeline/contract/test_grant_matrix.py` (6 tests).

**The gap, precisely:** `validate.sh`'s grant lint is per-file and syntactic — a `CREATE TABLE` or
`DROP FUNCTION` must carry *a* `GRANT` line or a `-- no-grant:` comment. It has a structural blind
spot: a migration whose only ACL statement is a `REVOKE` of something an earlier file granted trips
neither rule (creates no table, drops no function), so nothing checks the revoke actually landed or
that it didn't strand a capability nothing replaced. **073** (`REVOKE CREATE ON SCHEMA public FROM
rapid_pipeline_write`) and **078** (`REVOKE` raw `UPDATE` on `work_units`, not yet on `main` — lives
in `rapid_systems-wt-c`) are exactly this shape, each guarded only by a hand-written prose
"COORDINATION REQUIREMENT" paragraph.

**What's in the file:** a floor, not an exhaustive re-derivation (existing per-feature files —
`test_alert_outbox_grants.py`, `test_operator_grants.py` — already cover their own features in
depth; duplicating them would be redundant and brittle). Six assertions, each citing the specific
migration/line making the claim:
- 073's revoke landed (`has_schema_privilege(..., 'CREATE') is False`)
- 073's own "USAGE UNCHANGED" claim still holds (catches an over-broad follow-on accidentally
  revoking USAGE too)
- 078's revoke landed on `work_units` UPDATE (skips cleanly if 078 hasn't applied — probed via the
  catalog fact itself, matching this directory's schema-probe discipline, not by reading a filename)
- 078's own "SELECT/INSERT UNCHANGED" claim
- 078's own explicit "campaigns is a deliberate carve-out, not an oversight" claim — this is the
  assertion that would catch someone "finishing the job" by revoking campaigns' UPDATE too, before
  the constrained-function replacement 078's header says is a prerequisite exists
- the 001/002 baseline (`rapid_pipeline_write` inherits `rapid_read` via role membership) every later
  narrowing is measured against

**Verified 078's premise is current**, not stale: as of this worktree, `pipeline/intent/writer.py`
still issues the raw `UPDATE work_units SET ...` statements at lines 502, 551, 587 — so the
`test_078_*` tests will correctly skip against the currently-pinned CI stream (which only has
migrations through 075) and will correctly start asserting once 078 is pinned AND the consumer switch
ships. This is the same fact D3 below encodes independently and cross-checks.

**Verification:**
```
$ uv run --with pytest --with psycopg2-binary python3 -m pytest pipeline/contract/test_grant_matrix.py \
    --collect-only -q -m contract --import-mode=importlib
6 tests collected   # exit 0, all correctly auto-marked `contract`
```

### rapid_systems-side specification (precise, for the supervisor)

**What to build:** a live pre-apply preflight in `apply-db-migrations.sh`, complementing
`validate.sh`'s static per-file lint, checking the database's actual cumulative ACL state against a
small set of expected facts before/after each migration in the pending set is applied.

**Where it hooks:** in the "pending-files" section (the file's own comment block starting `# ---
pending-files: advisory-locked, atomic apply+record, checksummed`, roughly lines 442-600 as of this
read). Specifically: **after** the combined script's advisory lock is acquired and **before** the
combined script is sent to `psql_super_stdin -f -` (i.e., before any of the pending files' SQL runs),
run a read-only catalog query for each pending file that carries a structured coordination marker (see
D3 below for the marker format) asking whether the current ACL state matches what that file's revoke
assumes is already true consumer-side. **This is the applier-side enforcement of exactly the
`ConsumerNotReady` check D3 builds on the rapid-side** — the applier cannot read rapid's Python source
directly, but it CAN and should refuse to apply a migration whose own header states a coordination
requirement without some positive signal that the requirement was checked. Two implementable options,
in order of preference:

1. **(Preferred) A manifest-based handshake.** When the rapid-side CI pin-bump check (D3's consumer
   readiness module, invoked at the point `RAPID_SYSTEMS_READ_TOKEN`'s pinned ref advances) confirms a
   `CONSUMER_BEFORE_SCHEMA` entry is ready, it writes a small signed/checksummed acknowledgment
   artifact (e.g. `rapid-systems-ready.json: {"073-revoke-create-on-schema-public.sql":
   "<rapid-repo-commit-sha-that-satisfied-it>"}`) that the applier reads and requires to be present
   (and naming a commit that is actually deployed) for any pending file whose header carries a
   `-- coordination-requires:` marker, refusing with `exit 1` naming the migration and the missing
   acknowledgment otherwise.
2. **(Simpler, weaker) An explicit `--acknowledge-coordination <migration-filename>` CLI flag** the
   operator must pass per coordination-marked pending file, refusing to include that file in the
   combined script without it. Weaker because it trusts the operator's judgment rather than a checked
   fact, but requires no new artifact format and is implementable in an afternoon.

**What it should refuse:** applying (queuing into the combined script) any pending file carrying a
`-- coordination-requires:` header marker (D3's format) unless the corresponding acknowledgment is
present — `exit 1`, naming the migration file and quoting its own COORDINATION REQUIREMENT paragraph,
before any SQL in the combined script runs (i.e., this check must happen in the same pre-flight pass
that already computes `pending_files`, not inside the combined-script transaction, so a refusal here
never leaves a partially-applied combined script).

**What it should NOT do:** attempt to check rapid-side source directly (wrong repo, wrong trust
boundary for a script that runs with production DB superuser credentials) — the check belongs on the
rapid-side CI/pin-bump step (D3), and the applier's job is only to require evidence that check ran and
passed.

---

## D3 — cross-repo sequencing marker

**Files added:**
- `pipeline/intent/consumer_readiness.py` — the mirror image of `schema_contract.py` (which asks "does
  the deployed schema satisfy this build's requirements", checked at service startup); this asks "is
  this build's code ready for a schema change about to be pinned", checked at pin-bump/apply time. A
  `CONSUMER_BEFORE_SCHEMA` registry of `ReadinessEntry` objects, each a migration filename, a source
  path, and a regex pattern that must be **absent** from that source before the migration may be
  pinned/applied — restating 073's and 078's own "COORDINATION REQUIREMENT" prose paragraphs as
  predicates. `unready(repo_root)` returns the subset not yet satisfied.
- `pipeline/intent/test/test_consumer_readiness.py` — 8 stub-tier tests: predicate mechanics against a
  synthetic `tmp_path` tree (doesn't depend on `writer.py`'s exact current contents), plus the real
  registry evaluated against this actual repository, which is the strongest verification available:
  **073's entry correctly evaluates `ready=True`** (`catalog_db.py` already routes through
  `derived.create_child_table()`, confirmed no `CREATE\s+TABLE` match remains — matches the git log's
  own record, commit `f69fa240` "route child-table creation through derived.create_child_table()")
  and **078's entry correctly evaluates `ready=False`** (`writer.py:502,551,587` still issue the raw
  `UPDATE work_units SET ...` the pattern matches). This is a genuine cross-check against ground
  truth, not a fixture asserting what I expected — I verified both facts independently with `grep`
  before writing the entries, then confirmed the module's own evaluation agreed.

**Scope, explicitly stated in the module docstring:** consumer-before-schema only (073, 078). **075 is
the opposite direction** (schema-before-consumer — the migration is safe to apply early; it's the
*consumer* change that must wait) and is out of this module's scope by design: that direction's risk
lives entirely on the rapid_systems/deploy side (an old, unswitched consumer against a newer schema is
ordinary expand/contract, not a hazard this repo's readiness state can create). Only 073 and 078
currently carry the "COORDINATION REQUIREMENT" header shape in the migration stream (checked via
`grep -rn "COORDINATION REQUIREMENT"` across `rapid_systems/cloudformation/db-migrations/`).

**Marker format proposed for the header** (structured comment, machine-parseable, restates what
`consumer_readiness.py`'s `ReadinessEntry` already encodes so the two stay in lockstep):
```sql
-- coordination-requires: rapid:<source-path>#<pattern-must-be-absent>
-- coordination-direction: consumer-before-schema | schema-before-consumer
```
e.g. for 078: `-- coordination-requires: rapid:pipeline/intent/writer.py#UPDATE\s+work_units\s+SET`.
This is deliberately grep-checkable prose, not a new DSL — consistent with the existing
"COORDINATION REQUIREMENT" paragraphs being prose a human reads; the marker is an adjunct a machine
can also read, not a replacement for the paragraph.

**Verification:**
```
$ uv run --with pytest python3 -m pytest pipeline/intent/test/test_consumer_readiness.py -q --import-mode=importlib
8 passed   # exit 0
```
Plus the full-tree collection re-run afterward: `1886/2485 tests collected (599 deselected)` (+19
tests total across D1/D2/D3's three new files vs. the D5 baseline of 1878/2466), 0 errors.

### rapid_systems-side specification (precise, for the supervisor)

**What to build:** two things, one per coordination direction.

1. **Header marker parsing + a pin-bump-time check (belongs partly in `rapid`, already built — see
   `consumer_readiness.py` above; the rapid_systems-side half is emitting the marker itself).** Add
   the `-- coordination-requires:` / `-- coordination-direction:` header lines (format above) to 073
   and 078's existing headers (both already state the same fact in prose; this is additive, not a
   rewrite) and to any future consumer-before-schema migration. This is a small, mechanical,
   low-risk change to two files plus a convention note in `cloudformation/db-migrations/README.md`.

2. **Applier-time enforcement — see D2's rapid_systems-side spec above.** The applier's refusal
   mechanism (requiring an acknowledgment artifact or an explicit flag before including a
   coordination-marked pending file in the combined script) is the SAME hook D2 asked for, because
   both are "the applier must not silently apply a migration whose header says it needs something
   checked first." D2 and D3's rapid_systems asks should be implemented together, not as two separate
   preflight passes — they're the same mechanism (a coordination-marker-driven acknowledgment gate)
   applied to two related but distinct fact classes (grant state for D2, consumer-code state for D3).

3. **075's direction (schema-before-consumer) is NOT this marker's job to enforce on the apply side** —
   075 is safe to apply early by its own header's argument, so the applier has nothing to refuse there.
   If a machine-checkable guard is ever wanted for that direction, it belongs on the *rapid-side
   deploy* (refuse to deploy a consumer image that assumes 075's new column/CHECK values until
   `schema_contract.py`'s own floor confirms 075 is applied) — `schema_contract.py` already does
   exactly this for 075 today (it's in `REQUIRED_MIGRATIONS`), so no new mechanism is needed for that
   direction; flagging this only so the supervisor doesn't read D3's consumer-before-schema-only scope
   as an oversight.

---

## D4 — PR-gating decision

**PROPOSED, NOT RATIFIED**, per the run's conservative-by-default instruction. Decision: **leave the
contract-tests workflow gating PRs unchanged — `push` (smdc) + `workflow_dispatch` only, do not add
`pull_request`.**

**Reasoning (also recorded as a comment block in `.github/workflows/contract-tests.yml`, right above
`name: contract-tests`, so it sits where a future editor considering this exact change will look):**

- **Runtime/flakiness:** 466+ contract tests (measured: `grep -c "def test_"` across
  `pipeline/contract/*.py` before D1-D3's additions; +19 after), zero PR-triggered run history to
  gauge either — confirmed via `git log -- .github/workflows/contract-tests.yml`, which shows this
  is genuinely the repo's first Actions workflow, proven only on `push` so far (10+ pin-bump commits
  in recent history, all `push`-triggered).
- **The decisive factor — the cross-org token.** `Caltech-IPAC/rapid` is **PUBLIC** (verified live:
  `gh repo view Caltech-IPAC/rapid --json isPrivate,visibility` → `{"isPrivate":false,"visibility":
  "PUBLIC"}`). A `pull_request`-triggered run from a **fork** gets no access to repository secrets by
  default (GitHub's standard security model) — `RAPID_SYSTEMS_READ_TOKEN` would be absent, and the
  workflow's own "fail closed if the cross-org read secret is not provisioned" step would then fail
  **every fork PR, unconditionally**, for a reason unrelated to the PR's own content. A required check
  a legitimate contributor class can never pass is worse than no check.
- `pull_request_target` is the standard fix (runs in the base repo's context, secrets available) but
  carries its own well-known fork-secret-exposure risk if the base-workflow-vs-head-content split is
  mishandled — explicitly not a change to design and land unattended in this pass.

**Revisit trigger:** once (a) `push`-triggered runs have a runtime/flakiness track record, and (b) a
`pull_request_target` design (or a contribution-model change, e.g. requiring org membership to open
PRs) closes the fork-secret gap.

**Implemented:** only the comment block (33 lines) documenting the decision — `on:` is byte-identical
before/after, verified by re-parsing the YAML with `pyyaml` and diffing the `on:` key.

---

## Contradicts-the-brief notes

- The brief's phrase "the whole-tree deselects everything — a known pre-existing quirk" turned out to
  have a root cause I could fix (the `pipeline/contract/conftest.py` session-wide-marking bug), not
  just a fact to route around. Fixed it as part of D5 since it directly blocks marker-driven
  whole-tree discovery (the mechanism D5 step 3 needs) and is inside the exact conftest pattern D5
  asks to generalize to `alerts/`.
- The brief's "5 collection errors under database/modules/utils/test/, confirmed at baseline" is
  confirmed and now fixed (not left as a known carve-out) as a side effect of the `--import-mode=
  importlib` change D5 needed anyway for its own discovery mechanism.
- `alerts/` joining `testpaths` surfaces 13 real, pre-existing stub-tier failures (see D5 section
  above) — flagged, not fixed, not hidden.
- **Supervisor correction, incorporated above:** my first pass reported "bare pytest is safe" using a
  `RAPID_SW=$(pwd)`-qualified run, which is not what "bare" means and masked 12 further pre-existing
  failures in `submission/test/test_gathering.py` (RAPID_SW read from the ambient environment without
  being patched, in two of that file's three test classes). Corrected: a genuinely bare run is
  **25 failed / exit 1**, not 13/exit 1 — the "safe" property (never touches live DB/AWS) still holds
  and is the property the campaign asked for; "safe" and "green" are now stated as two separate claims
  throughout the D5 section, and both failure groups (13 alerts/, 12 test_gathering.py) are logged as
  follow-up items for their respective owners.
