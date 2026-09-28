# AGENTS.md

Self-contained operating contract for coding agents in the `rapid`
repository, Caltech-IPAC/rapid.

## What this repository is

RAPID (Roman Alerts Promptly from Image Differencing) builds and runs
the pipeline software that finds transients in Roman Space Telescope
images and issues alerts. This repository is **public and portable**.
Never commit account identifiers, account-specific bucket names,
hostnames, or credentials; inject them at deploy time through environment
variables. `scripts/check-public-safety.sh` scans for 12-digit AWS account
numbers, personal filesystem paths, personal note-taking directories,
the operations database host IP and a retired ECR alias. It runs in CI
(`public-safety.yml`) and `.githooks/pre-push`. Before your first push,
install the hook with `git config core.hooksPath .githooks`.

## Design authority

**Design authority** is the readthedocs system pages
(https://roman-rapid.readthedocs.io/en/latest/system/), sourced from
the `rapid_docs` repository's `system/` directory: `specification.md`
(requirements), `stage-contract.md` (package layout and per-stage
contract), `runs.md` (runs/attempts/custody), `products.md`,
`releases.md`, `tool.md`, `loop.md`, `checks.md`, `observability.md`
(logging, monitoring, job timing), and per-stage pages; `operations.md`
holds the direction pass's operations-readiness proposal (2026-09-26),
not yet ruled.
When a page and code disagree, **correct the page first**; never quietly
patch code around a stale page. README.md gives a longer prose walkthrough
of these rules and the CLI surface. Where they overlap, this file is the
summary and README.md has the detail.

## Branches, pull requests and releases

- `rebuild` is the rebuild's integration branch. All rebuild work lands
  on it by pull request; nobody pushes to it directly.
- `main` and `dev` belong to the team's existing (pre-rebuild) pipeline.
  Do not touch them from rebuild work.
- Pull requests target `rebuild`. Describe what changed, why, and which
  `rapid_docs` page(s) the PR implements or corrects. Watch CI to green
  before handing back; CI green is the merge gate, never merge a red PR.
  Use rebase merge (squash only when the branch carries a merge commit).
  Do not merge your own PR without permission from the person directing
  the work.
- Releases are annotated tags `rebuild-v0.<n>` cut from `rebuild`'s head
  by `rapidpipe release cut` (`rapidpipe.release.core`), which tags,
  migrates, records, builds, deploys and pins in one guarded sequence.
  Once cut, the scheme becomes `v1.<n>` at the point the rebuild
  replaces `main` (not yet).

Cut a release with `python -m rapidpipe.release cut` (or
`rapidpipe release cut`). It invokes `rapid_systems`-supplied hooks for
the account-specific steps (migrate, build, deploy, pins); this repository
names no account, host or bucket itself.

## Repository layout

| Path | Holds |
|---|---|
| `rapidpipe/` | The pipeline package (map below). |
| `database/` | `migrations/` and `apply-migrations.sh` (the schema), and `modules/utils/roman_tessellation{,_db}.py` (closed-form sky tessellation, imported by `rapidpipe.science`). |
| `modules/` | Helpers the stages use from `dev`: `sip_tpv/` (imported by `science/difference/resample.py`), `zogy/` and `sfft/` (scripts the difference stage runs from `/code/modules/...`). |
| `cdf/` | SExtractor/SWarp configuration files the stages read at `/code/cdf`. |
| `c/` | Vendored C tool source; the image takes the tools from the base image's RPMs instead. |
| `containers/` | The pipeline image recipe (`rapid-pipeline/build.sh`, `Containerfile`). |
| `tests/` | `unit/`, `db/`, `cli/` and `fixtures/<stage>/`. |
| `scripts/` | `check-public-safety.sh`, `science-drift.sh` and its watch list. |
| `docs/` | The Sphinx source of the readthedocs site. |

`dev`'s `pipeline/`, `alerts/`, `aws/`, `soc/`, `sims/`, `docker/`,
`modules/{coadd,fake_src,solarsystem,utils}` and most of `database/` and
`scripts/` are not carried on `rebuild`; `dev` keeps them, and
`scripts/science-drift.sh` watches the paths the rebuild ported from.

## The `rapidpipe` package map

The distribution is `rapid-pipeline`; the import package is `rapidpipe`
(`pyproject.toml`). `tests/unit/test_dependency_direction.py` enforces a
fixed dependency order across every subpackage and top-level module,
including lazy and relative imports. A unit imports only units strictly
below it in this order:

Leaf modules
(`exitcodes`, `log`, `revision`, `seams`: no `rapidpipe` import) <
`products` < `db` and `science` (which do not import each other) <
`checks` < `runs` < `stages` < `launch` < `selftest` < `cli`. `release`
imports only the leaves and `db`, and only `cli` imports it. Stage modules
never import other stage modules.

A failure names the offending edge and file:line. A new edge up the order
is a design question for the stage-contract page, not a test to relax.

| Subpackage | Holds |
|---|---|
| `stages/` | One module per stage (`admit`, `reference`, `difference`, `finalize`, `register`, `load`, `maintain`, `crossmatch`, `statistics`, `prune`, `alerts`, `export`), plus the shared runner `contract.py` and `settings.py`. Each is directly runnable as `python -m rapidpipe.stages.<name>` and via `rapidpipe stage <name>`. Exit codes: 0 success, 64 usage, 65 bad/missing input, 69 declared-but-not-implemented (reserved: no stage in this build returns it), 70 unclassified error, 75 retryable transient failure. |
| `science/` | Pure algorithms and tool wrappers the stages call (`difference`, `reference`, `finalize`, `load`, `crossmatch`, `statistics`, `alerts`, plus `spatial` for HEALPix/tessellation). No stage, `launch` or CLI imports. |
| `products/` | Product identifiers, kinds, manifest types (`manifest.py`), storage layout (`storage.py`), per-kind modules (`l2image`, `refimage`, `diffimage`, `psf`, `alertcontainer`, `catalogexport`), `units.py` (`register`'s producer-keyed unit id), and `spatial` (the pure HEALPix and tessellation derivations `db` needs; `science.spatial` re-exports them). |
| `db/` | Persistence: `connection.py`, per-table modules (`l2files`, `refimages`, `diffimages`, `sources`, `objects`, `psfs`, `alerts`, `ids`), and the migrations applier (`database/apply-migrations.sh`, not itself under `rapidpipe/`). |
| `runs/` | `repository.py` (runs, units, attempts, instances, promotion by slot: each instance's `slot` and `identity` are derived from its `logical_key` in SQL by `product_identity_fill()`, migration `20260926-02-product-slots.sql`, never in Python), `create.py` (recording a run: `run create`, `--seed --only-failed`), `slots.py` (promotion selectors and the frozen plans of `run promote-plan`), `local.py` (subprocess execution of one attempt), `cleanup.py` (deletion guard and blocking-reference checks), `inputs.py` (binding a unit's inputs from its input-set manifest), `binding.py` (`bind_input_set`, the one path by which both composers admit, bind and write an input set), `checking.py` (running and recording candidate checks, automatic promotion). |
| `launch/` | `batch.py` (turning a run into Batch jobs, reading results back), `walk.py` (the run walk: `run start`, the loop's per-unit walk), `loop.py` (the processing-date loop) and `discovery.py` (the loop's inbox discovery and delivery classification). |
| `release/` | `core.py` (`cut`/`show`/`list`/`verify`), `hooks.py` (the account-specific hook contract), `__main__.py`. |
| `checks/` | `registry.py`, `builtin.py`, `policy.py`, and the shipped policy under `policies/<name>@<version>.toml` (`rebuild-trial@1`; `rebuild-strict@1` is a test fixture in `tests/fixtures/checks/`). |
| `cli/` | The `rapidpipe` command-line tool: `main.py` dispatches to `runctl.py` (`run ...`), `stagectl.py` (`stage ...`), `checkctl.py` (`check ...`), `loopctl.py` (`loop ...`); `release` dispatches into `rapidpipe.release`. |
| `selftest/` | `runner.py` plus per-stage modules and `support/` fakes; drives `make stage-<name>` and `rapidpipe selftest --stage <name> [--real-tools]`. `fixtures/` ships each stage's packaged expected output. |
| `exitcodes.py` | A bare top-level module, not a subpackage: `ExitCode`, the one exit-code vocabulary, and `ArgumentParser` (parse failures exit 64). Stdlib-only, so every subpackage, `science` included, may import it. |
| `settings/` | Per-stage default settings and schema, `<name>.toml`, merged recursively with a `--settings` overlay. |

## Migrations

`database/apply-migrations.sh` applies `database/migrations/*.sql` in
filename order using `PG*` environment variables. It requires PostgreSQL
16+ with the Q3C extension; CI uses PostgreSQL 18. Full rules are in
`database/migrations/README.md`:

- **New files only**, named `YYYYMMDD-NN-short-name.sql` (today's date,
  next unused two-digit sequence for that date). Never edit a file applied
  anywhere; the applier hashes each applied file and refuses changes.
- **The migration number is claimed at merge, not at authorship**: if
  another PR merges the same-date `NN` first, rebase and take the next
  free number before your PR merges.
- **One change per file.** A schema change and the code that needs it
  land in the same pull request. CI (`db-migrations.yml`) applies the
  whole stream to a fresh database, re-applies it to confirm idempotence,
  and checks that changes to an already-applied file are refused.
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
| `tests/unit` | `unit-tests.yml` | No database; `rapidpipe.db`/`rapidpipe.runs` import `psycopg2` but tests mock it. Run locally: `python -m pytest tests/unit -q` in any interpreter that has `pip install -r requirements.txt pytest`; requirements.txt is the one dependency authority (pyproject.toml reads it, the CI workflows install from it). No per-checkout venv is needed or assumed. |
| `tests/db` | `db-migrations.yml` | Real PostgreSQL 18 + Q3C service container, migrations applied first. Needs a live PostgreSQL; there is none on the laptop, so this suite is CI-only for a laptop-based agent. |
| `tests/cli` | `cli-behaviour.yml` | Real PostgreSQL 18 + Q3C, Batch and S3 faked (`tests/unit/fakebatch.py`, `fakes3.py`). Black-box: argv in, exit code/stdout/stderr/database state out. CI-only, same reason. |
| n/a | `container.yml` | Builds `containers/rapid-pipeline` against a public stand-in base image and smoke-tests `--version`, `stage admit --help`, the imports of the kept helpers outside `rapidpipe/` and the presence of the `/code` script and config files. Proves the build recipe only, not the production science environment. |
| n/a | `public-safety.yml` | `scripts/check-public-safety.sh`, see above. |

Stage fixtures under `tests/fixtures/<name>/` back both:

- `make stage-<name>` (runs the stage locally, `fake` tools by default,
  or `real` where a target exists).
- `rapidpipe selftest --stage <name> [--real-tools]`: submits the same
  fixture as an ordinary Batch job against the deployed image. This is
  the venue for exercising a stage against real tools and a real database,
  owned by `rapid_systems`' Batch recipes, outside this repository's CI.

## Settings and secrets

Each stage ships default settings and a schema at
`rapidpipe/settings/<name>.toml`. A `--settings` overlay merges
recursively, replacing the scalars and arrays it names. Unknown keys or
invalid values fail with exit 64. Algorithm parameters (including random
seeds) belong in settings; deployment locations and credentials never do.

Database credentials reach `rapidpipe.db` only through `PG*` environment
variables or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`,
never as command-line arguments or committed to this repository. Every other
account-specific value the tool reads follows the same rule (`RAPIDPIPE_BATCH_JOB_QUEUE`,
`RAPIDPIPE_OUTPUTS_ROOT_*`, `RAPIDPIPE_CLEANUP_ROLE_ARN`, and siblings
documented in README.md): named environment variables only.

## Output and logging

A command prints only data on stdout. Everything else goes through
`rapidpipe.log` to stderr in one line shape:
`<UTC> <LEVEL> run= attempt= stage= unit= <logger> <message>`.
A stage invocation writes the same lines to `log/<stage>.log` beside the
attempt's outputs. New subcommands follow this split; commands that output
records should offer `--json` (the direction pass's proposal, 2026-09-26).
`RAPIDPIPE_LOG_LEVEL` sets the level; `--profile` (or
`RAPIDPIPE_PROFILE=1`) profiles a stage body on a scratch run.

The exit-code vocabulary is
`rapidpipe/exitcodes.py` (`ExitCode`); stages use its subset
`STAGE_EXIT_CODES` (`rapidpipe/stages/contract.py`); every parser is
`rapidpipe.exitcodes.ArgumentParser`, so a parse failure exits 64. The
tables on the `rapid_docs` tool and stage-contract pages document it; do
not invent a new code or a second list.

## Writing conventions

- When porting a stage or script from the team's existing `dev` pipeline,
  cite `dev`'s script name in the docstring or commit message for traceability.
- State every departure from `dev`'s behaviour on the relevant `rapid_docs`
  page, not only in a code comment. Readers and future agents look first
  to that page as the design authority.
- Rulings (a supervisor's or the lead's decision that changes a
  contract) are dated and attributed on the `rapid_docs` page they
  affect (for example "supervisor step 6, 2026-09-24"); do not bury a
  ruling in a code comment alone.
- No em dashes in docs or commit messages written for this repository
  (house style; use a comma, colon, or a new sentence instead).

### Ported-from headers

Every module under `rapidpipe/science` and `rapidpipe/stages`, plus
`rapidpipe/products/spatial.py`,
`rapidpipe/settings/difference.toml` and `reference.toml`, carries a
line 1 comment `# ported-from: <dev path>[, <dev path>...] @ <8-hex dev
commit>`, or `# ported-from: none` for rebuild-only code.
`tests/unit/test_ported_from.py` fails the build if a module lacks one.
`scripts/science-drift.sh` reads these headers from the tree, never a
separate table. It reports unported `origin/dev` commits per pin, watched
paths (`scripts/science-drift-watch.txt`) and settings-.ini drift.
`.github/workflows/science-drift.yml` runs it on push/PR to `rebuild` but
never fails the build. Port listed dev commits by hand or record them as
declined; never auto-apply them.
