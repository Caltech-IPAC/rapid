# Constraints & conventions for a new DB-reading + AWS-calling reconciler path

Evidence only, from `pipeline/` (branch `smdc` @ `3792447`), read-only. All citations `file:line`.

---

## 1. `pipeline/repositories/` — the carved repository tier

**Modules** (`pipeline/repositories/`):

| File | Class | Tables |
|---|---|---|
| `__init__.py` | — (package docstring, rule 17) | — |
| `errors.py` | `RepositoryError`, `RepositoryQueryFailed` | — |
| `admission_identity.py` | (no class — pure identity/digest functions) | — |
| `admission.py` | `AdmissionRepository` | `admission_manifests`, `admission_manifest_entries`, `admission_exposures`, `admission_l2files`, `admission_release_pointer`, `admission_releases` (DRAFT 051) |
| `products.py` | `ProductRepository` | `products`, `artifacts`, `product_artifacts` (DRAFT 048) |
| `diffimages.py` | `DiffImageRepository` | `diffimages` |
| `association.py` | `AssociationRepository` (per module docstring, not directly opened) | — |
| `alert_outbox.py` | `AlertOutboxRepository` | outbox tables (DRAFT 050) |
| `skycatalogs.py` | `SkyCatalogRepository` | sky-catalog tables |

**The freeze rule**, stated once and repeated per-module: `pipeline/repositories/__init__.py:1-53`. Verbatim opening:
> "Narrow repositories over the operational tables (conformance rule 17)... `RAPIDDB` is one 5,000-line class with thirty-odd query methods, returning raw tuples, reporting failure by setting `exit_code` and returning `None`" (`pipeline/repositories/__init__.py:1-8`).

Each new repository module repeats its own **"WHY A REPOSITORY AND NOT A `RAPIDDB` METHOD"** section citing the freeze — e.g. `pipeline/repositories/admission.py:9-16`, `pipeline/repositories/products.py:8-14`, `pipeline/repositories/alert_outbox.py:7` (per grep). This is the idiom a new query must follow: **no new query lands on `RAPIDDB`; it lands in a new or existing module under `pipeline/repositories/`.**

### Verbatim pattern, from `admission.py` and `products.py` (both read in full)

**Class shape** — one class per table-family, constructed directly with a connection, no factory inside the module itself:

```python
class AdmissionRepository:
    """Admission writes over a connection the caller owns."""
    def __init__(self, conn):
        self._conn = conn
```
`pipeline/repositories/admission.py:189-193`

```python
class ProductRepository:
    """Writes and reads `products`, `artifacts` and `product_artifacts`.
    Takes a connection it does not own and never commits — the caller's
    transaction owns the boundary. See the module docstring.
    """
    def __init__(self, conn):
        self._conn = conn
```
`pipeline/repositories/products.py:168-176`

**Connection ownership (universal invariant).** Every repository takes a connection it does NOT own and NEVER commits or rolls back on success:
- `pipeline/repositories/admission.py:18-23`: "THIS REPOSITORY NEVER COMMITS AND NEVER OPENS A CONNECTION... A repository that opened its own connection could not be in one transaction with the caller."
- `pipeline/repositories/products.py:16-29`: same, citing "round-3 finding #8" — two connections cannot be one transaction.
- `pipeline/repositories/diffimages.py:103`: "Takes a connection it does not own and never commits."
- `pipeline/repositories/skycatalogs.py:68`: same wording.

**Queries are raw SQL, module-level string constants** (not an ORM, not a query-builder helper). Pattern from `products.py:74-165`:
```python
_UPSERT_PRODUCT_SQL = (
    "INSERT INTO products"
    "  (product_key, product_class, role, identity_payload,"
    "   serialization_version, process_family)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s)"
    " ON CONFLICT (product_key) DO UPDATE SET product_key = EXCLUDED.product_key"
    " RETURNING product_id, product_key, product_class, role"
)
```
Method bodies call `self._one(...)` / `self._query(...)` / `self._execute(...)` with the constant plus a params tuple — never inline f-string SQL, never string-formatted values into the query text.

**Idempotence is `ON CONFLICT` against a real constraint, never SELECT-then-INSERT.** Stated explicitly in both files:
- `pipeline/repositories/admission.py:25-30`: "Every admission insert is `INSERT ... ON CONFLICT (<natural key>) DO ... RETURNING` against a real constraint, never a SELECT-then-INSERT."
- `pipeline/repositories/products.py:31-37`: identical reasoning, citing a "window between the two statements" a concurrent registrar could race.
- The specific idiom for returning a row on conflict is `DO UPDATE SET <key> = EXCLUDED.<key>` (a no-op self-write) rather than `DO NOTHING`, because `DO NOTHING` returns no row and the row is needed by the caller — `pipeline/repositories/products.py:67-73`, `pipeline/repositories/admission.py:457-463`.

**Return type: `typing.NamedTuple`, never raw tuples.** `pipeline/repositories/__init__.py:44-47`; concrete examples `Admission`/`ManifestRecord` (`admission.py:170-186`), `Product`/`Artifact` (`products.py:46-65`).

**Error handling — one private `_query`/`_execute`/`_one` per class, re-typing every driver exception as `RepositoryQueryFailed`, never rolling back:**
```python
def _query(self, method, sql, params):
    try:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return cur.fetchall()
    except Exception as exc:                      # noqa: BLE001 — re-typed
        if _is_invariant_violation(exc):
            raise
        raise RepositoryQueryFailed(method, str(exc)) from exc
```
`pipeline/repositories/admission.py:571-596` (see full docstring on why it does NOT roll back — that's the caller's transaction). `products.py:316-331` is the same shape with an added comment on why *this* one also does not roll back, contrasted against `diffimages.py`'s `_query`, which *does* roll back because its calls are "standalone reads" (`products.py:322-330`) — i.e. rollback-or-not is a documented per-repository decision tied to whether the repository's calls run inside a caller-owned multi-statement transaction.

**DRAFT-schema-gated repositories probe, never catch.** Both `admission.py` and `products.py`(via its caller) ask `to_regclass`/`information_schema`/`pg_proc` before running any real statement, and refuse (fail closed) rather than fall back to legacy stored procedures when the schema is absent:
- `pipeline/repositories/admission.py:51-67` ("DRAFT-051-GATED OBJECTS ARE PROBED, NEVER CAUGHT") and the `_SCHEMA_PROBE` constant at `admission.py:90-94`.
- `pipeline/repositories/admission.py:197-218` (`schema_present()` / `_require_schema()` raising `AdmissionSchemaAbsent`).

**Typed errors, SQLSTATE-classified, never message-text-classified:**
- `pipeline/repositories/errors.py:23-46` — base `RepositoryError`, `RepositoryQueryFailed` (wraps driver exception as `__cause__`).
- `pipeline/repositories/admission.py:599-608` — `_is_invariant_violation` matches on `pgcode`/`sqlstate`, explicitly "never on message text: the message is written for an operator and may be reworded, while the code is the database's own classification."

### Call-site idiom: how a repository is obtained

Two shapes observed, both direct construction — no shared factory module inside `pipeline/repositories/` itself:

**A. Direct construction at the use site**, when the schema is known to exist:
```python
catalogs = SkyCatalogRepository(conn)          # pipeline/generateLightCurveHATSCatalog.py:203
diffimages = DiffImageRepository(conn)          # pipeline/forcedPhotometryForField.py:589
outbox = AlertOutboxRepository(conn)            # pipeline/stages/alert_production.py:299
```

**B. A local closure factory, named `<x>_repository_for(conn)` or `for_connection(conn)`, when the schema is DRAFT-gated and must be probed once per pass** — the exact idiom named in your prompt:
```python
def identity_repository_for(conn):
    """`ProductRepository(conn)`, or None where DRAFT 048 is not deployed."""
    with conn.cursor() as cur:
        cur.execute(_IDENTITY_TABLES_PROBE)
        row = cur.fetchone()
    present = bool(row and (row[0] if not isinstance(row, dict)
                            else next(iter(row.values()))))
    if not present:
        logger.warning(
            "registration is running LEGACY-ONLY: DRAFT 048's products "
            "and artifacts tables are not deployed on this database, so "
            "no product or artifact rows will be written for this pass.")
        return None
    return ProductRepository(conn)
```
`pipeline/operator/registrar.py:134-178` (full method at 100-190). Called once per registration pass, not per attempt (line 159: "ONE PROBE PER REGISTRATION PASS, not per attempt"). This closure is defined inline at the call site inside `registrar_for(...)`, not exported from `pipeline/repositories/`.

`pipeline/entrypoints/job.py:477` passes `identity_repository=ProductRepository(conn)` directly (no probe) — the unconditional-construction variant, used where the caller already knows the schema is present.

**Naming convention**: repository class names are `<Domain>Repository` (PascalCase); factory closures are `<domain>_repository_for(conn)` (snake_case, `_for` suffix) or the more generic `for_connection(conn)` when the closure is scoped to one connection rather than one repository type.

---

## 2. A submissions-table repository does NOT exist in `pipeline/repositories/`

Migration 044's `public.submissions` table is **not** wrapped by any class in `pipeline/repositories/`. Grep for `submissions` across `pipeline/` confirms no repository module references it (`pipeline/repositories/*.py` has zero hits for "submission").

**What exists instead — the closest analogue — is a top-level, non-class, function-based module:** `submission/protocol.py` (442 lines; note this is the top-level `submission/` package, sibling to `pipeline/`, NOT `pipeline/repositories/`).

### `submission/protocol.py` shape

- **No class.** Every operation is a free function taking an `execute` callback as its first argument, not a `conn`:
  ```python
  def is_available(execute):        # submission/protocol.py:178
  def prepare(execute, *, run_id, job_type, job_name, job_queue,
              job_definition, ...):  # submission/protocol.py:191
  def mark_calling(execute, submission_id, now=None):      # :211
  def mark_bound(execute, submission_id, scheduler_job_id, now=None):  # :232
  def mark_unknown(execute, submission_id, detail, ...):   # :243
  def mark_found(execute, submission_id, scheduler_job_id, now=None):  # :271
  def mark_lost(execute, submission_id, now=None):         # :290
  def open_submissions(execute):    # :308
  def attach_attempts(execute, submission_id, attempt_ids): # :322
  def resolve(execute, row, describe, now=None):            # :335
  def resolve_open(execute, describe, now=None):             # :394
  ```
- **States are the six DRAFT-044 values verbatim**, `PREPARED/CALLING/BOUND/UNKNOWN/FOUND/LOST` (`submission/protocol.py:75-84`), with a stated schema-enforced invariant: "`submit_job` IS NEVER RE-CALLED FOR A ROW... DRAFT migration 044 enforces the same thing at the schema (`submissions_call_once_ck`)" (`:41-46`).
- **Callers probe availability rather than catching**, same discipline as `pipeline/repositories/`: `pipeline/seams.py:730-758` calls `protocol.is_available(execute)` before any write, and on absence logs and returns `None` rather than raising — **but critically, this is documented as a DIFFERENT failure posture than admission's**: "A protocol failure NEVER blocks a submission... refusing to submit because the bookkeeping failed would convert a diagnosis aid into an outage" (`pipeline/seams.py:744-749`) — i.e. `submissions`/DRAFT-044 degrades gracefully (unlike `admission`'s hard refusal on DRAFT-051 absence).
- Raw SQL for `submissions` also appears directly in `pipeline/gc/reference_sql.py:47-74` (`ACTIVE_MANIFESTS_SQL`, a bare module-level SQL string, no class at all — same "probe via `to_regclass`, never catch" discipline stated at `reference_sql.py:16-21`).

**If a new query needs `public.submissions`, the closest existing pattern to model is either:**
1. Add functions to `submission/protocol.py` (function + `execute` callback shape, if extending submission-protocol behavior), or
2. Follow the `pipeline/repositories/admission.py` / `products.py` class shape exactly, as a brand-new `pipeline/repositories/submissions.py` module — this is what "any NEW query must live in `pipeline/repositories/`" (per your prompt's framing) would require, since `submission/protocol.py` itself is *not* under `pipeline/repositories/` and predates/parallels the freeze rule rather than implementing it. **Flag: it's undetermined from the code alone whether `submission/protocol.py` is considered an accepted exception to rule 17's carve, or whether it predates the freeze and would itself be non-conformant if written today** — no docstring in either file states an opinion on the other.

---

## 3. `pipeline/contract/` — the contract-test tier

**Every file**: `pipeline/contract/__init__.py`, `conftest.py`, `fixture.py`, `stub_broker.py`, plus 36 `test_*.py` files (full list captured; notably `test_repositories.py`, `test_admission_repository.py`, `test_borrowed_connection.py`, `test_submission_protocol.py`).

### Structure and marking

`pipeline/contract/conftest.py` (full file, 75 lines):

- **Auto-marking**, so no test author can forget: 
  ```python
  def pytest_collection_modifyitems(config, items):
      """Mark everything in this package `contract`."""
      for item in items:
          item.add_marker(pytest.mark.contract)
  ```
  `pipeline/contract/conftest.py:19-22`, with the rationale: "A tier whose membership depends on remembering to say so is a tier that leaks" (`:9-11`).

- **Fixtures**, session-scoped connection + per-test cleanup, NOT full transaction-per-test rollback:
  ```python
  @pytest.fixture(scope="session")
  def target():                    # :26 — resolved libpq target
  @pytest.fixture(scope="session")
  def _session_conn(target):       # :37 — one connection per session
  @pytest.fixture
  def conn(_session_conn):         # :47 — per-test, rolled back only if left open
  @pytest.fixture
  def second_conn():               # :64 — independent connection for concurrency tests
  ```
  Explicitly NOT transaction-wrapped-and-rolled-back-always: "several of these tests need their writes VISIBLE to a second connection (the claim race, the watermark race), which a wrapping transaction would hide" (`conftest.py:49-54`). Instead: "unique run tags, no truncation" is what keeps the tier re-runnable (`:53-54`) — see `fixture.RUN_TAG` referenced in `test_admission_repository.py:49-55`.

- **`pipeline/contract/fixture.py`** — importable without pytest "so the same helpers serve a pytest run in CI and any other runner on rapid-admin" (`conftest.py:3-5`). Key functions: `connection_target()` (:64), `connect()` (:81), `executor(conn)` (:95), `has_table(conn, table_name, schema="public")` (:115), `has_function(conn, function_name, schema="derived")` (:139), `scope(name)` (:170), `ensure_definition(conn)` (:181), plus row-builder helpers `make_logical_job`, `make_attempt`, `make_diffimage`, `make_completed_attempt`, `make_pending_attempt`, `create_unit`.

### What distinguishes contract from stub-tier

- **Default pytest selection excludes it**: conftest's own comment names the default run as `-m 'not contract and not live'` (`conftest.py:9`) — so contract tests need an explicit `-m contract` (or equivalent) to run, and are excluded by default.
- **A contract test gets a real DB connection via the `conn`/`second_conn` fixtures above** — no test double, no in-memory substitute. `fixture.connect()` opens a real libpq connection (`fixture.py:81`).
- **DRAFT-migration-gated contract tests self-skip by probing**, not by a pytest marker: `test_admission_repository.py:14-22` — "**THESE SKIP WHERE DRAFT 051 IS ABSENT AND RUN WHERE IT IS APPLIED**... The skip is decided by `fixture.has_table` PROBING the catalog rather than by catching a failure."
- **CI vs. rapid-admin run different schema states**: CI builds only the authoritative migration stream (skips DRAFT files); the rapid-admin acceptance run applies "base + drafts" and so runs every contract test for real (`test_admission_repository.py:15-18`).

---

## 4. Stub-tier (no-I/O) unit test conventions

- **Location**: co-located `test/` subpackages next to the module under test — e.g. `pipeline/registration/test/test_products.py`, `pipeline/operator/test/test_submission.py`, `pipeline/operator/test/test_registrar.py`, `pipeline/stages/test/test_alert_production.py`, `submission/test/test_submit.py`, `observability/test/test_submission_integration.py` — NOT under `pipeline/contract/`.
- **Naming**: `test_<module>.py`, one file per module under test, individual test functions/methods named `test_<behavior_description>` in full sentence-like snake_case (e.g. `test_an_ambiguous_family_is_refused`, `test_a_family_with_no_active_revision_is_refused` — `pipeline/operator/test/test_submission.py:247,266`).
- **The stub tier's explicit module list** (what CI and rapid-admin both run, unchanged) is enumerated literally in `scripts/run-operational-tests.sh:16-29` — 36 named modules including `pipeline.registration.test.test_products`, `pipeline.registration.test.test_consumer`, `pipeline.reconciler.test.test_scheduler`, `observability.test.test_submission_integration`.
- **CI runs this same script** (`.github/workflows/contract-tests.yml:162-177`, step "the stubbed tier stays green, unchanged") both to prove packaging didn't break it and as a gate independent of the contract tier.

### Refusal-capable fakes — cited examples

Rule (per your prompt): a test double must be able to fail/refuse, not just return canned success.

**`FakeDB` in `pipeline/registration/test/test_products.py:73-90`** (full class read):
```python
class FakeDB:
    """Records the call sequence and hands back the ids the bodies chain on."""

    def __init__(self, exit_code=0):
        self.calls = []
        self.exit_code = exit_code
        self.rfid = 77
        self.pid = 900
        self.version = 3
        self.rfcatid = 11
        self.svid = 22

    def __getattr__(self, name):
        if name.startswith(("add_", "update_", "register_")):
            def record(*args):
                self.calls.append((name, args))
            return record
        raise AttributeError(name)
```
This is refusal-capable in two ways: (1) `exit_code` is a constructor parameter, so a test can instantiate a `FakeDB` that reports the legacy failure signal rather than always succeeding; (2) `__getattr__` raises `AttributeError` for any method name that doesn't match the `add_/update_/register_` prefixes — an unexpected call is a hard failure, not a silently-accepted no-op.

**`FakeBatch` in `pipeline/operator/test/test_submission.py:54`** (class present; not fully read this pass — flagged for follow-up if needed) paired with named refusal tests at the same file: `test_an_ambiguous_family_is_refused` (:247), `test_a_family_with_no_active_revision_is_refused` (:266), `test_a_tree_without_this_route_s_keys_is_refused` (:295) — i.e. the fake is exercised specifically to prove the surrounding code refuses on bad input, not merely that it succeeds on good input.

**`pipeline/contract/stub_broker.py:106`** — a contract-tier double, docstring: "Every send is DEFINITELY refused — terminal, never retried" (grep hit; file not fully read this pass).

Other files with `Fake*`/`Stub*`/`Double*` classes (not individually read, flagged as available for a deeper pass if needed): `pipeline/test/test_seams.py`, `pipeline/test/test_operator.py`, `pipeline/operator/test/test_gathering_registry.py`, `pipeline/stages/test/test_publishing.py`, `pipeline/stages/test/test_context.py`.

---

## 5. CI — `.github/workflows/contract-tests.yml` (only workflow file present; full file read)

- **Trigger**: `push` to branch `smdc` only, plus `workflow_dispatch` (`:33-36`). Concurrency group cancels in-progress runs on the same ref (`:43-45`).
- **Services**: a `postgres:18` container, digest-pinned (`postgres:18@sha256:b913fd5699b8bd23fa4b06d72ecdd939fad43b80fb8651bac06caa0e6d135cac`, `:70`), with the `postgresql-18-q3c` extension installed into the running container at job time (not baked into a derived image) — because "the deployed database is PostgreSQL 18 with the Q3C spherical-indexing extension... matching the fleet" (`:51-59`, install step `:98-113`).
- **Steps in order**: checkout `rapid` → install Q3C into the PG service container → set up Python 3.11 → install `postgresql-client` → `pip install -e '.[test]'` → verify all 5 console entry points (`rapid-reconciler`, `rapid-operator`, `rapid-job`, `rapidctl`, `rapid-publisher`) resolve and import cleanly → run `scripts/run-operational-tests.sh` (the stub tier) → **fail-closed check for `RAPID_SYSTEMS_READ_TOKEN`** (a named failure step, placed as late as possible, `:179-212`) → checkout `IPAC-SW/rapid_systems` at a **pinned SHA** → record the revision → `scripts/run-contract-tests.sh rapid_systems/cloudformation/db-migrations`.
- **No AWS credentials anywhere in this workflow** — stated as a design constraint repeatedly (`:7`, `:103`, `:196`).
- **Pinned `rapid_systems` revision**: `28ea260bee930f3336f3728e4486785a4708f7f3`, appearing twice — as env var `RAPID_SYSTEMS_REF` (`:92`) and as the literal `ref:` on the checkout step (`:225`, with a comment explaining why it's a literal and not `${{ env.* }}`: "the `env` context is not guaranteed to a `with:` input, and a ref that silently resolved to empty would check out the default branch" `:220-224`). This matches the git-log context (recent commit `26dbb1a` "bump the pinned rapid_systems revision past 053's live-found fix").
- **Typical duration**: not stated anywhere in the workflow file or nearby docs found in this pass — **flagged as undetermined**; no timing data was in scope of files read.
- **Secret**: `RAPID_SYSTEMS_READ_TOKEN`, a fine-grained PAT scoped read-only to `IPAC-SW/rapid_systems` contents, because that repo is private and in a different GitHub org (`:10-23`).

---

## 6. Documented architectural invariants relevant to reconciler/AWS/DB interaction

Grepped across `pipeline/repositories/*.py`, `pipeline/contract/*.py`, plus `pipeline/seams.py` and `submission/protocol.py` read directly. Assembled list, each with citation:

1. **`RAPIDDB` is frozen — no new query method may be added to it.** `pipeline/repositories/__init__.py:1-29`; repeated per-module (`admission.py:9`, `products.py:8`).
2. **A repository never opens or commits a connection; it takes the caller's.** `pipeline/repositories/admission.py:18-23`, `products.py:16-29`, `diffimages.py:103`, `skycatalogs.py:68`.
3. **Transaction ownership belongs to the use case, never the repository.** Stated as the reason for invariant 2, `pipeline/repositories/__init__.py:41-43`.
4. **Idempotence is the database's (via `ON CONFLICT`/constraints), never Python's (SELECT-then-INSERT).** `pipeline/repositories/admission.py:25-30`, `products.py:31-37`.
5. **DRAFT-migration-gated schema is probed via `to_regclass`/`information_schema`/`pg_proc`, never discovered by catching `UndefinedTable`.** Catching would abort the caller's open transaction. `pipeline/repositories/admission.py:51-60`, `pipeline/gc/reference_sql.py:16-21`, `pipeline/operator/registrar.py:150-157`.
6. **A repository never rolls back on failure** when its calls run inside a caller-owned multi-statement transaction — rolling back would discard the caller's other uncommitted writes. `pipeline/repositories/admission.py:573-580`, `products.py:322-330`. (Contrast: `diffimages.py`'s repository *does* roll back, because its calls are standalone reads on their own — the choice is deliberate per-repository, not universal.)
7. **Errors are classified by SQLSTATE code, never by message text**, because messages are operator-facing prose subject to rewording. `pipeline/repositories/admission.py:602-608`.
8. **A degraded/absent DRAFT schema fails CLOSED (refuses) rather than falling back to a legacy path that would reintroduce a known duplicate-minting defect** — this is `admission.py`'s posture specifically (`admission.py:62-67`, `110-121`). **Submissions/DRAFT-044 is the opposite posture**: it fails OPEN (degrades gracefully, never blocks) — `pipeline/seams.py:744-749`: "A protocol failure NEVER blocks a submission... refusing to submit because the bookkeeping failed would convert a diagnosis aid into an outage." **A new submissions-adjacent path should determine which posture applies to it explicitly — the codebase does not apply one uniform rule across all DRAFT-gated features.**
9. **No transaction spans an external AWS call (`SubmitJob`).** Conformance rule 7, verbatim: "No transaction spans `SubmitJob`. An ambiguous submission resolves through the durable submission-row protocol (PREPARED -> CALLING -> BOUND / UNKNOWN -> FOUND / LOST); the API call is never repeated for a submission row." `submission/protocol.py:3-6`. Mechanically enforced by committing between `mark_calling` and the Batch call: "the transaction is closed BEFORE the call precisely so no transaction is open during it" `submission/protocol.py:36-39`.
10. **An AWS call is never re-issued for one durable row; ambiguity is resolved by re-querying the AWS-side state (`ListJobs`), never by re-calling the mutating API.** `submission/protocol.py:41-46`, schema-enforced by `submissions_call_once_ck`.
11. **The state machine on the row is authoritative over elapsed-time heuristics** — "the state machine, not the timestamp, is the truth" (quoted from the originating brief) `submission/protocol.py:63-66`; a horizon only bounds how long an inconclusive re-query result may be trusted, it does not itself classify anything.
12. **Repositories return named records (`typing.NamedTuple`), never raw tuples or dicts**, because positional unpacking of a raw tuple silently breaks when a column is inserted mid-schema. `pipeline/repositories/__init__.py:44-47` (cites the concrete historical bug: `forcedPhotometryForField.py:602`).
13. **Repositories raise on failure; there is no `exit_code`-and-`None` sentinel path.** `pipeline/repositories/__init__.py:48-53`.

---

## Flags — undetermined / needs follow-up

- Whether `submission/protocol.py` (function+`execute`-callback shape, outside `pipeline/repositories/`) is a *sanctioned exception* to the rule-17 carve, or a pre-existing module that would itself be judged non-conformant if proposed today. No docstring in either location states a relationship between them.
- Typical CI run duration for `contract-tests.yml` — not stated in the workflow file or any file read this pass.
- `FakeBatch` (`pipeline/operator/test/test_submission.py:54`) and `stub_broker.py`'s full refusal machinery were located but not read in full — flagged if a worker needs the exact refusal-triggering mechanics beyond what's cited above.
- Whether any workflow beyond `contract-tests.yml` exists for this repo — a repo-wide search under `.github/workflows/` found only this one file.
