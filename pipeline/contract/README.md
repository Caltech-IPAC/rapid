# The contract tier

PostgreSQL-backed tests against the **authoritative migration schema** —
conformance rule 23: "These rules are exercised against the authoritative
migration schema with PostgreSQL-backed tests — claiming under real
concurrency, fail-then-succeed retry histories, and every active query/write
family — not against hand-built fakes."

## The three tiers

| Tier | Marker | What it needs | Gate? |
|---|---|---|---|
| stub | `stub` | nothing (psycopg2/boto3 stubbed into `sys.modules`) | yes — `scripts/run-operational-tests.sh` |
| contract | `contract` | a real PostgreSQL built from the migration stream | yes — `scripts/run-contract-tests.sh` |
| live | `live` | deployed infrastructure | no, manual |

The contract tier is **opt-in**: `pyproject.toml` sets
`addopts = "-m 'not contract and not live'"`, so a bare `pytest` never tries
to open a database. Both runners select their tier explicitly.

## Running it

The suite is **location-parameterized** — it takes its target from the standard
libpq variables and from nothing else, which is what makes the CI run and the
rapid-admin run the same run:

```
PGHOST=... PGPORT=... PGUSER=... PGPASSWORD=... PGDATABASE=rapid \
  scripts/run-contract-tests.sh <path-to-rapid_systems/cloudformation/db-migrations>
```

The migration directory is consumed **read-only, at a pinned revision**. It is
never vendored into this repository and never edited here: `rapid_systems` owns
the schema, and a local copy that drifted would make every result in this tier
a statement about a database nobody deploys.

- **In CI**: `.github/workflows/contract-tests.yml` fetches it from
  `IPAC-SW/rapid_systems` at a pinned SHA, using the repo secret
  `RAPID_SYSTEMS_READ_TOKEN` (an owner provisioning step — see the workflow
  header; the two repos are in different organizations, so the workflow's own
  token cannot reach it).
- **On rapid-admin**: `scripts/contract-on-rapid-admin.sh` stages the same
  directory to a scratch prefix and runs the same script against a throwaway
  container.

## Why each family is here rather than in the stub tier

Every test in this directory asserts a property of PostgreSQL. If it could be
asserted against a fake, it would belong in the stub tier and would run for
free.

- **`test_attempt_claiming.py`** — `resolve_attempt` is a PL/pgSQL function
  defined by migration 013. *This repository does not contain it.* Its
  advisory-lock key derivation, its post-lock recheck and its two partial
  unique indexes are the behaviour under test, and no fake in this repo can
  be faithful to code that is not in this repo.
- **`test_work_unit_cas.py`** — the partial unique index picks a winner
  between two concurrent creators; the `WHERE state = %s` CAS is exclusive
  under concurrency because of row locking. The previous fake replayed a
  scripted 23505 while the production path had no re-SELECT at all: the fake
  agreed with code that could not work.
- **`test_registration_watermark.py`** — the watermark predicate is evaluated
  by the database against what another transaction committed while this one
  waited. Against a fake it is a tautology. Advisory-lock *scoping* (0x5234
  vs 0x5732 on one attempt id) has no Python-observable expression at all.
- **`test_retry_history.py`** — `blocked` requires a non-NULL `blocked_reason`
  by CHECK constraint, so "parks with a reason" is only really tested where
  the constraint runs.
- **`test_borrowed_connection.py`** — "commit suppression" is a claim about
  transaction boundaries, asserted here from a *second connection's*
  visibility rather than from a counter on a fake.
- **`test_schema_preflight.py`** — `schema_migrations` is populated by the
  applier and by nothing else, so the preflight can only be tested honestly
  against a database an applier has run against.
- **`test_double_agreement.py`** — one live-vs-double agreement probe per
  protocol. Each builds a deliberately broken double and asserts both that
  the live system refuses the call *and* that the double accepts it. Asserting
  only the first would pass on a database that had quietly dropped the
  constraint; asserting both makes each probe a statement about the double.

## Fixture honesty

Each test builds its own rows under a unique run tag (`fixture.RUN_TAG`) and
deletes only what it created. Nothing truncates a table or assumes an empty
database, so a re-run is safe, two runs may share one database, and a failure
leaves its rows behind for inspection.
