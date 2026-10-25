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
the `rapid_docs` repository's `system/` directory: `foundation.md` (the
schema, runs, jobs, the publisher, official bindings, the verbs and the
thin chain), `specification.md` (requirements), `tool.md` (the command-line
tool), `loop.md`, `datasets.md`, `decisions.md`, `observability.md`
(logging, monitoring, job timing), and per-stage pages; `operations.md`
holds the direction pass's operations-readiness proposal (2026-09-26),
not yet ruled.
When a page and code disagree, **correct the page first**; never quietly
patch code around a stale page. README.md gives a longer prose walkthrough
of these rules and the CLI surface. Where they overlap, this file is the
summary and README.md has the detail.

## Branches and pull requests

- `rebuild` is the rebuild's integration branch. All rebuild work lands
  on it by pull request; nobody pushes to it directly.
- `main` and `dev` belong to the team's existing (pre-rebuild) pipeline.
  Do not touch them from rebuild work.
- Pull requests target `rebuild`. Describe what changed, why, and which
  `rapid_docs` page(s) the PR implements or corrects. Watch CI to green
  before handing back; CI green is the merge gate, never merge a red PR.
  Use rebase merge (squash only when the branch carries a merge commit).
  Agents merge their own PRs on `rebuild` once CI is green (Ben,
  2026-09-23).

## Repository layout

| Path | Holds |
|---|---|
| `rapidpipe/` | The pipeline package (map below). |
| `database/` | `schema.sql` (the whole schema, applied with one `psql -f`), `queries.sql` (the read queries the official views answer, each named, run by `tests/db/test_queries.py`), and `modules/utils/roman_tessellation{,_db}.py` (closed-form sky tessellation, imported by `rapidpipe.science`). |
| `modules/` | Helpers the stages use from `dev`: `sip_tpv/` (imported by `science/difference/resample.py`), `zogy/` and `sfft/` (scripts the difference stage runs from `/code/modules/...`). |
| `cdf/` | SExtractor/SWarp configuration files the stages read at `/code/cdf`. |
| `c/` | Vendored C tool source; the image takes the tools from the base image's RPMs instead. |
| `containers/` | The pipeline image recipe (`rapid-pipeline/build.sh`, `Containerfile`). |
| `tests/` | `unit/`, `db/` (with `db/proof/`, the procedure proof's synthetic steps in CI; the by-hand `thin_proof.py` retired with the associate stage, 2026-10-24), `cli/` and `fixtures/<stage>/`. |
| `scripts/` | `check-public-safety.sh`, `science-drift.sh` and its watch list, `read_packets.py` (the packet reader: a packet set's validation report, outside the pipeline), `consume_packets.py` (a run's packet sets read back at their recorded offsets, counted, and one set per field compared byte for byte with the outbox), `copy_epsf.py` (CRDS ePSF references copied by name under the prefix a run's `[psf] epsf_cache` names, once), `check_run.sh` (a run's three checks, `validate_run.py`, `read_packets.py` over its bound sets and `consume_packets.py`, and one verdict line). |
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
(`exitcodes`, `log`: no `rapidpipe` import) <
`products` < `db`, `science` and `foundation` (which do not import one
another) < `stages` < `launch` < `cli`. Stage modules
never import other stage modules.

A failure names the offending edge and file:line. A new edge up the order
is a design question for `foundation.md`, not a test to relax.

| Subpackage | Holds |
|---|---|
| `stages/` | One module per stage. The foundation's stages are one function, `run(job: JobContext) -> None`, run by `rapidpipe job local`/`job run`/`job submit`: `import_products` (stage `import`), `intake` (one SOC L2 ASDF file, read anonymously from a public source root when the run says so, converted by `dev`'s `build_fits_file` into its gzipped FITS, the pixels of `[intake] dq_mask_bits` NaN as commissioning #238 sets them, with its `exposures` and `l2files` rows), `packet` (`dev`'s alerts package at schema 00.06: Avro packets with three cutouts, forced photometry and Gaia matches, the object catalog and window read as they stand and recorded, and the packet index) `reference` (`dev`'s awaicgen coadd of the named `l2files`, with its SExtractor catalog: `refimages`, `refimimages`, `refimcatalogs`) `difference` (`dev`'s ZOGY subtraction of the named `l2files` row against the named `refimages` row with the science PSF and the reference PSF it measures on the resampled reference, SFFT and naive beside it as `dev` runs them: `diffimages` and the capped `sources` set; then the association, crossmatch and statistics in one, its sources matched in memory against the objects of its run in each source's field and its neighbours as it read them, each founding in its own field: `objects` founded here and `memberships`, reconciled at publish) and `psf` (new science: one filter and SCA's science `psfs` row, reduced from the CRDS ePSF reference the L2 file names, fetched by name). A stage's exit, through `job run`: 0 success, 64 usage, 65 bad/missing input, 70 unclassified error, 75 retryable transient failure. The older `main(argv)` contract's `admit` and `finalize` were retired 2026-10-25. The run model's database stages (`register`, `load`, `maintain`, `crossmatch`, `statistics`, `prune`, `alerts`, `export`) were retired with its schema; the foundation plan's Part 5 ports their science (into `difference` and `packet`; the `associate` stage that first held it retired 2026-10-24). |
| `science/` | Pure algorithms and tool wrappers the stages call (`difference`, `reference`, `intake`, `load`, `crossmatch`, `statistics`, `alerts`, plus `spatial` for HEALPix/tessellation). No stage, `launch` or CLI imports. |
| `products/` | Product identifiers, kinds, manifest types (`manifest.py`), storage layout (`storage.py`), per-kind modules (`l2image`, `diffimage`), `filters`, and `spatial` (the pure HEALPix and tessellation derivations; `science.spatial` re-exports them). |
| `db/` | `connection.py` (one connection from the `PG*` environment, a secret or a parameter tree) and `ids.py` (ULIDs). |
| `foundation/` | The foundation plan's operations over `database/schema.sql`: `bookkeeping.py` (`create_run`, `create_job`, `run_show`, `job_show`), `official.py` (`change`, `undo`, `apply`, `Refused`), `publish.py` (`publish`, the manifest format, `PublishError`), `catalog.py` (reconcile at publish: a difference job's founded objects attached to those founded since its read, under the field's lock), `local.py` (`JobContext`, the foundation's stage contract, and `run_job`, one attempt run here and published), `prefix.py` (`get` and `put` for `file://` and `s3://` URLs), `layers.py` (`resolve`: a run's settings body from the base, the dataset overlay and the `--set`s, with the record of its layers), `publish_kafka.py` (`deliver`, the one send boundary, which a packet job calls for its own set as its last act, and `publish`, the sweep of a run's unstamped official sets: each set stamped `jobs.kafka_published_at` with its acknowledgements' partition and offsets), `loop.py` (one pass of `rapidpipe loop`: the source root's new stems for a selection admitted and every ready job submitted one stage forward, no field chains or order, ending at the topic). The verbs in `cli/main.py` wrap them. |
| `launch/` | `__init__.py`: `submit` (a recorded job to AWS Batch as `job run --job <id>`). No retry and no attempt bookkeeping. |
| `cli/` | The `rapidpipe` command-line tool: `main.py` holds the foundation verbs (`run create`, `run show`, `job show`, `job local`, `job run`, `job submit`, `official change`, `official undo`, `publish`, `loop`). |
| `exitcodes.py` | A bare top-level module, not a subpackage: `ExitCode`, the one exit-code vocabulary, and `ArgumentParser` (parse failures exit 64). Stdlib-only, so every subpackage, `science` included, may import it. |
| `settings/` | `foundation-thin.toml`, a foundation run's base settings body (`run create`'s default `--settings`), one table per stage (the reference stage's as subtables, one per `dev` ini section); `datasets/<dataset>.toml`, one overlay per dataset holding only the keys that differ, each with its measurement (`run create --overlay`, `rapidpipe.foundation.layers`). |

## The schema

`database/schema.sql` is the whole schema, applied to an empty
PostgreSQL 18 database with `psql -v ON_ERROR_STOP=1 -f
database/schema.sql`; it creates the Q3C extension and its indexes when
Q3C is installed and says so in a NOTICE when it is not. The dated files
beside it each brought SMDC's existing database to `schema.sql` once and
are never applied after a fresh one (the rule: `database/README.md`).

## Tests and CI

Every code change ships with tests; a change to database behaviour gets
a `tests/db` test, not only a mocked `tests/unit` one.

| Suite | Workflow | Runs against |
|---|---|---|
| `tests/unit` | `unit-tests.yml` | No database; `rapidpipe.db`/`rapidpipe.foundation` import `psycopg2` but tests never connect. Run locally: `python -m pytest tests/unit -q` in any interpreter that has `pip install -r requirements.txt pytest`; requirements.txt is the one dependency authority (pyproject.toml reads it, the CI workflows install from it). No per-checkout venv is needed or assumed. |
| `tests/db` | `foundation.yml` | Real PostgreSQL 18 + Q3C service container, `schema.sql` applied first: the schema, the publisher, change groups and the procedure proof (`tests/db/proof/`). Fails, never skips, when `PGHOST` is unset; any throwaway PostgreSQL 18 serves locally. |
| `tests/cli` | `foundation.yml` | The five verbs against the same database: argv in, exit code/stdout/stderr/database state out. |
| n/a | `container.yml` | Builds `containers/rapid-pipeline` against a public stand-in base image and smoke-tests `--version`, `--help`, the imports of the kept helpers outside `rapidpipe/` and the presence of the `/code` script and config files. Proves the build recipe only, not the production science environment. |
| n/a | `public-safety.yml` | `scripts/check-public-safety.sh`, see above. |

The stages' fake external tools are `tests/support/fakedifftools.py` and
`fakereftools.py`; a stage's real tools run in the image, under a job.

## Settings and secrets

A run's settings body is resolved once, at `run create`, from
`rapidpipe/settings/foundation-thin.toml`, the dataset's overlay and the
`--set`s (`rapidpipe.foundation.layers`); a key the base lacks is refused.
Algorithm parameters (including random
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
New subcommands follow this split; commands that output
records should offer `--json` (the direction pass's proposal, 2026-09-26).
`RAPIDPIPE_LOG_LEVEL` sets the level.

The exit-code vocabulary is
`rapidpipe/exitcodes.py` (`ExitCode`); every parser is
`rapidpipe.exitcodes.ArgumentParser`, so a parse failure exits 64. The
table on the `rapid_docs` tool page documents it; do not invent a new
code or a second list.

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
`rapidpipe/products/spatial.py` and
`rapidpipe/settings/foundation-thin.toml` (pinned to `dev`'s science
.ini), carries a
line 1 comment `# ported-from: <dev path>[, <dev path>...] @ <8-hex dev
commit>`, or `# ported-from: none` for rebuild-only code.
`tests/unit/test_ported_from.py` fails the build if a module lacks one.
`scripts/science-drift.sh` reads these headers from the tree, never a
separate table. It reports unported `origin/dev` commits per pin, watched
paths (`scripts/science-drift-watch.txt`) and settings-.ini drift.
`.github/workflows/science-drift.yml` runs it on push/PR to `rebuild` but
never fails the build. Port listed dev commits by hand or record them as
declined; never auto-apply them.
