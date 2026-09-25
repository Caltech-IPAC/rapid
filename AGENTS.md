# AGENTS.md

Operating contract for any coding agent working in this repository (the
`rapid` repository, Caltech-IPAC/rapid), self-contained for an agent
with no other context.

## What this repository is

This is the RAPID (Roman Alerts Promptly from Image Differencing)
pipeline: it builds and runs the software that finds transients in
Roman Space Telescope images and issues alerts. The repository is
**public and portable**. Never commit account identifiers, bucket
names tied to an account, hostnames, or credentials; they are injected
at deploy time through environment variables. `scripts/check-public-safety.sh`
scans for these (12-digit AWS account numbers, personal filesystem
paths, personal note-taking directories, the operations database host
IP, a retired ECR alias) and runs in CI (`public-safety.yml`) and in
`.githooks/pre-push`; install the hook with
`git config core.hooksPath .githooks` before your first push.

**Design authority** is the readthedocs system pages
(https://roman-rapid.readthedocs.io/en/latest/system/), sourced from
the `rapid_docs` repository's `system/` directory: `specification.md`
(requirements), `stage-contract.md` (package layout and per-stage
contract), `runs.md` (runs/attempts/custody), `products.md`,
`releases.md`, `tool.md`, `loop.md`, `checks.md`, and per-stage pages.
When a page and this code disagree, **the page is corrected first**;
never quietly patch code around a stale page. This repository's own
README.md carries a longer prose walkthrough of the same rules and the
CLI surface; where the two overlap, this file is the summary and
README.md has the detail.

## Branches and releases

- `rebuild` is the rebuild's integration branch. All rebuild work lands
  on it by pull request; nobody pushes to it directly.
- `main` and `dev` belong to the team's existing (pre-rebuild) pipeline.
  Do not touch them from rebuild work.
- Releases are annotated tags `rebuild-v0.<n>` cut from `rebuild`'s head
  by `rapidpipe release cut` (`rapidpipe.release.core`), which tags,
  migrates, records, builds, deploys and pins in one guarded sequence.
  Once cut, the scheme becomes `v1.<n>` at the point the rebuild
  replaces `main` (not yet).
- Pull requests target `rebuild`. Merge by rebase merge (squash only
  when the branch itself carries a merge commit). CI green is the
  merge gate; never merge a red PR.

## The `rapidpipe` package map

The distribution is `rapid-pipeline`; the import package is `rapidpipe`
(`pyproject.toml`). Dependency direction is fixed and enforced by
convention, not by a lint rule: `rapidpipe.products` defines
identifiers and manifest types with no import of `runs`, `db` or
stages; `rapidpipe.db` provides persistence with no import of `runs` or
stages; `rapidpipe.runs` composes products and persistence; stage
modules never import other stage modules, `launch`, or `cli`;
`rapidpipe.science` never imports stages, `launch` or `cli`.

| Subpackage | Holds |
|---|---|
| `stages/` | One module per stage (`admit`, `reference`, `difference`, `finalize`, `register`, `load`, `maintain`, `crossmatch`, `statistics`, `prune`, `alerts`, `photometry`, `export`), plus the shared runner `contract.py` and `settings.py`. Each is directly runnable as `python -m rapidpipe.stages.<name>` and via `rapidpipe stage <name>`. Exit codes: 0 success, 64 usage, 65 bad/missing input, 69 declared-but-not-implemented (`photometry` only in this build; `export` was ported for real in step 8), 70 unclassified error, 75 retryable transient failure. |
| `science/` | Pure algorithms and tool wrappers the stages call (`difference`, `reference`, `finalize`, `load`, `crossmatch`, `statistics`, `alerts`, plus `spatial` for HEALPix/tessellation). No stage, `launch` or CLI imports. |
| `products/` | Product identifiers, kinds, manifest types (`manifest.py`), storage layout (`storage.py`), and per-kind modules (`l2image`, `refimage`, `diffimage`, `psf`, `alertcontainer`, `catalogexport`). |
| `db/` | Persistence: `connection.py`, per-table modules (`l2files`, `refimages`, `diffimages`, `sources`, `objects`, `psfs`, `alerts`, `ids`), and the migrations applier (`database/apply-migrations.sh`, not itself under `rapidpipe/`). |
| `runs/` | `repository.py` (runs, units, attempts, instances, promotion), `local.py` (subprocess execution of one attempt), `cleanup.py` (deletion guard and blocking-reference checks). |
| `launch/` | `batch.py` (turning a run into Batch jobs, reading results back) and `loop.py` (the processing-date loop). |
| `release/` | `core.py` (`cut`/`show`/`list`/`verify`), `hooks.py` (the account-specific hook contract), `__main__.py`. |
| `checks/` | `registry.py`, `builtin.py`, `policy.py`, `runner.py`, and shipped policies under `policies/<name>@<version>.toml` (`rebuild-trial@1`, `rebuild-strict@1`). |
| `cli/` | The `rapidpipe` command-line tool: `main.py` dispatches to `runctl.py` (`run ...`), `stagectl.py` (`stage ...`), `checkctl.py` (`check ...`), `loopctl.py` (`loop ...`); `release` dispatches into `rapidpipe.release`. |
| `selftest/` | `runner.py` plus per-stage modules and `support/` fakes; drives `make stage-<name>` and `rapidpipe selftest --stage <name> [--real-tools]`. `fixtures/` ships each stage's packaged expected output. |
| `settings/` | Per-stage default settings and schema, `<name>.toml`, merged recursively with a `--settings` overlay. |

## Migrations

`database/migrations/*.sql`, applied in filename order by
`database/apply-migrations.sh` against `PG*` environment variables
(PostgreSQL 16+ with the Q3C extension; CI uses PostgreSQL 18). Full
rules are in `database/migrations/README.md`; the essentials:

- **New files only**, named `YYYYMMDD-NN-short-name.sql` (today's date,
  next unused two-digit sequence for that date). Never edit a file once
  applied anywhere; the applier hashes each applied file and refuses a
  changed one.
- **The migration number is claimed at merge, not at authorship**: if
  another PR merges its own same-date `NN` first, rebase and take the
  next free number before your PR merges.
- **One change per file.** A schema change and the code that needs it
  land in the same pull request; CI (`db-migrations.yml`) applies the
  whole stream to a fresh database, re-applies to confirm idempotence,
  and checks that a modified already-applied file is refused.
- **Additive only while an earlier release's runs are open** (no drop
  or rename of a column or table a live release still reads): this is
  a constraint of the release model (`releases.md`, ruling R7), not
  only a migrations-file rule.
- Grants to the `rapid_rebuild_pipeline` role follow the pattern of the
  existing grant migrations (e.g. `20260923-01-rebuild-pipeline-grants.sql`,
  `20260924-06-objects-grants.sql`); a new table or child-table set gets
  its own grants migration alongside it.

## Tests and CI

Every code change ships with tests; a change to database behaviour gets
a `tests/db` test, not only a mocked `tests/unit` one.

| Suite | Workflow | Runs against |
|---|---|---|
| `tests/unit` | `unit-tests.yml` | No database; `rapidpipe.db`/`rapidpipe.runs` import `psycopg2` but tests mock it. Run locally: `python -m pytest tests/unit -q` in an interpreter that has `pip install -r requirements.txt pytest` — requirements.txt is the one dependency authority, and an existing environment (a conda env with the science stack) is preferred over a per-checkout venv (Ben, 2026-09-21: "use existing envs, no venvs"). |
| `tests/db` | `db-migrations.yml` | Real PostgreSQL 18 + Q3C service container, migrations applied first. Needs a live PostgreSQL; there is none on the laptop, so this suite is CI-only for a laptop-based agent. |
| `tests/cli` | `cli-behaviour.yml` | Real PostgreSQL 18 + Q3C, Batch and S3 faked (`tests/unit/fakebatch.py`, `fakes3.py`). Black-box: argv in, exit code/stdout/stderr/database state out. CI-only, same reason. |
| n/a | `container.yml` | Builds `containers/rapid-pipeline` against a public stand-in base image and smoke-tests `--version` and `stage admit --help`. Proves the build recipe only, not the production science environment. |
| n/a | `public-safety.yml` | `scripts/check-public-safety.sh`, see above. |

Stage fixtures under `tests/fixtures/<name>/` back both `make
stage-<name>` (runs the stage locally, `fake` tools by default, `real`
where a target exists) and `rapidpipe selftest --stage <name>
[--real-tools]`, which submits the same fixture as an ordinary Batch
job against the deployed image: the venue for exercising a stage
against real tools and a real database, owned by `rapid_systems`'
Batch recipes, not run from this repository's own CI. A release is cut
with `python -m rapidpipe.release cut` (or `rapidpipe release cut`),
which invokes `rapid_systems`-supplied hooks for the account-specific
steps (migrate, build, deploy, pins); this repository names no account,
host or bucket itself.

## Settings and secrets

Each stage ships default settings and a schema at
`rapidpipe/settings/<name>.toml`; `--settings` supplies an overlay that
merges recursively, replacing scalars and arrays the overlay names.
Unknown keys or invalid values fail with exit 64. Algorithm parameters
(including random seeds) belong in settings; deployment locations and
credentials never do.

Database credentials reach `rapidpipe.db` only through the `PG*`
environment variables or an AWS Secrets Manager secret named by
`RAPID_DB_SECRET_ID`, never as a command-line argument and never
committed to this repository. The same rule applies to every other
account-specific value the tool reads (`RAPIDPIPE_BATCH_JOB_QUEUE`,
`RAPIDPIPE_OUTPUTS_ROOT_*`, `RAPIDPIPE_CLEANUP_ROLE_ARN`, and siblings
documented in README.md): named environment variables only.

## Writing conventions

- When porting a stage or script from the team's existing `dev`
  pipeline, cite `dev`'s script name in the docstring or commit message
  so the correspondence is traceable.
- Every departure from `dev`'s behaviour is stated on the relevant
  `rapid_docs` page, not only in a code comment. The page is the
  design authority, so it is where a reader (and a future agent) looks
  first.
- Rulings (a supervisor's or the lead's decision that changes a
  contract) are dated and attributed on the `rapid_docs` page they
  affect (for example "supervisor step 6, 2026-09-24"); do not bury a
  ruling in a code comment alone.
- No em dashes in docs or commit messages written for this repository
  (house style; use a comma, colon, or a new sentence instead).

## Pull requests from an agent

Base branch `rebuild`. Body describes what changed and why, and which
`rapid_docs` page(s) it implements or corrects. Watch CI to green
before handing back; do not merge your own PR unless the person
directing the work has said you may.
