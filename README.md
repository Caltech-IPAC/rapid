# RAPID

Repository for the RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team.

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time, never committed. Reference bulk data artifacts (product listings, large generated files) with the command to reproduce them instead of committing them.

The [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html) is the design authority for repository boundaries and stage interfaces. [AGENTS.md](AGENTS.md) presents the same rules for coding agents, plus the package map, migrations and test/CI conventions.

### Layout

`rapidpipe/` is the pipeline package; `database/` holds the schema migrations, their applier and the sky-tessellation helpers; `modules/` and `cdf/` hold the `dev` helpers, scripts and configuration files the stages use; `containers/` holds the image recipe; `tests/`, `scripts/` (public-safety and science-drift checks), `docs/` and `c/` (vendored C tool source) complete the tree. [AGENTS.md](AGENTS.md) has the full layout table.

## Package

The distribution is `rapid-pipeline`; its import package is `rapidpipe`. `rapidpipe stage <name>` runs one stage against a declared unit of work, input manifest and output location, implementing [the contract page](https://roman-rapid.readthedocs.io/en/latest/system/stage-contract.html).

`--inputs`/`--outputs` accept a local directory or an `s3://bucket/prefix` location. For S3, the runner fetches (or stages) it in a temporary work directory before running the stage body, using `RAPIDPIPE_WORK` if set, else the system temp directory.

`rapidpipe.db` holds persistence (the connection module and the migrations applier). `rapidpipe.runs` holds runs, units, attempts, product instances and promotion, per [the runs page](https://roman-rapid.readthedocs.io/en/latest/system/runs.html). `rapidpipe.db` gets database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a committed file.

`rapidpipe.stages.admit` is the first stage. It reads a delivery manifest naming one delivered l2 image (a FITS file staged outside the pipeline, not yet a registered product), verifies the delivered bytes and FITS checksums, reads the header and WCS required by the products page's l2-image field list, copies the file into the attempt's output location, and publishes a manifest with a fresh product instance.

`rapidpipe.stages.register` reads that manifest's registration block and writes the `l2files`/`l2filemeta` rows without opening the FITS file. It registers the manifest's own product instance first and records nothing twice on replay. `rapidpipe.science.spatial` supplies the pure spatial derivations that `register` and its tests need: HEALPix indexes, the Roman tessellation tile id and the exact tile-overlap footprint. It imports no stage and can be exercised without a database.

## Container

[`containers/rapid-pipeline`](containers/rapid-pipeline) holds the recipe that builds `rapidpipe` into a runnable image over a base environment supplying its dependencies. Which base image that is and where the built image is published are decided and owned outside this repository, in `rapid_systems`. See [`containers/README.md`](containers/README.md) for how to build the recipe locally.

## Running a run from the command line

`rapidpipe run create/list/show/local` operate on `rapidpipe.runs.repository`:

- `create` records a new run and prints its id.
- `list` and `show` inspect runs, units and attempts.
- `local` runs one stage attempt as a subprocess on the current machine through `rapidpipe.runs.local`, allocating its own attempt id and exclusive output location without Batch.

`rapidpipe.launch` submits units to AWS Batch, resolves a unit's inputs from an upstream stage's selected attempt, and reconciles job results into the run model.

### Run lifecycle

- `rapidpipe run promote <run> --reason R [--who W] [--kinds a,b]` promotes a production run's candidates (one per kind and slot) and prints the promotion id; a policy refusal exits 1.
- `rapidpipe run rollback <promotion> --reason R [--who W]` reverses one promotion by slot, refused if its recorded after instance is no longer current in its slot or the promotion recorded no slot.
- `rapidpipe run finish <run>` marks a run finished once every unit is complete, failed or cancelled; a finished run admits no new units or attempts.
- `rapidpipe run delete <run> [--requested-by U]` deletes a scratch run's S3 object versions and run-scoped science rows, keeping its run-model rows as tombstones; it resumes a run left `deleting`.
- `rapidpipe run pin <run>` / `rapidpipe run unpin <run>` keep a scratch run from expiring (scratch runs expire 14 days after creation unless pinned), or release it.

## Running a stage locally

```
run_id=$(rapidpipe run create --kind scratch --purpose "local smoke test" --stages admit,register)
rapidpipe run local "$run_id" admit --unit e20260821001234/SCA07 \
    --inputs /path/to/delivery --outputs-root /tmp/rapid-local
rapidpipe run local "$run_id" register --unit e20260821001234-reg/SCA07 \
    --inputs /tmp/rapid-local/runs/"$run_id"/admit/e20260821001234/SCA07/<admit-attempt-id> \
    --outputs-root /tmp/rapid-local
rapidpipe run show "$run_id"
```

`register`'s `--inputs` is admit's own output location (printed by `run local admit` as `outputs=...`), since `register` reads the producing attempt's completion manifest rather than the original delivery.

## Running on Batch

`rapidpipe.launch.batch` reads its deployment configuration from the environment only, never from a committed value:

- `RAPIDPIPE_BATCH_JOB_QUEUE` -- the Batch job queue name or ARN.
- `RAPIDPIPE_BATCH_RECLAIM_QUEUE` -- optional: the queue for every further attempt of a unit once one of its attempts, in the run or a run it was seeded from (`--only-failed`, `loop --retry-failed`), was lost to a Spot reclaim (a FAILED job whose `statusReason` starts `Host EC2`, recorded `transient` with `scheduler_metadata.batch.reclaim`). Set it to an on-demand queue while `RAPIDPIPE_BATCH_JOB_QUEUE` names a Spot queue, and a reclaimed unit moves to on-demand capacity and never retries on Spot; any other transient keeps the job queue. Unset, or equal to `RAPIDPIPE_BATCH_JOB_QUEUE`, every attempt goes to the job queue, as before. `run reconcile --resolve-jobless` searches both queues when they differ. (Ruling, supervisor step 3, 2026-10-04: the resubmit's queue is the gap between a classified reclaim and on-demand placement; an EventBridge resubmit would bypass the attempt record, and a lane-to-queue map is a bigger change than the gap.)
- `RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH` / `RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION` -- the Batch job definition name or ARN for a scratch or production run; `RAPIDPIPE_BATCH_JOB_DEFINITION` is the scratch fallback only.
- `RAPIDPIPE_OUTPUTS_ROOT_SCRATCH` / `RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION` -- the `s3://bucket/prefix` under which `runs/<run>/<stage>/<unit>/<attempt>` lives for a scratch or production run; `RAPIDPIPE_OUTPUTS_ROOT` is the scratch fallback only. A production run never falls back to an unsuffixed variable.
- `RAPIDPIPE_SCRATCH_BUCKET` -- optional: the only bucket `run delete` may remove objects from (default: the bucket of the scratch outputs root).
- `RAPIDPIPE_BATCH_JOB_NAME_PREFIX` -- optional, default `rapid`.
- `RAPIDPIPE_CLEANUP_ROLE_ARN` -- optional: the IAM role `run delete` and `run expire` assume (session `rapidpipe-cleanup-<user>`) for their S3 deletes; unset, they use the caller's own credentials. The role's credentials last an hour and do not refresh: `run expire` assumes it afresh for each run, and one run's delete must finish within the hour (else the run stays `deleting` and a later `run delete` resumes it).

```
rapidpipe run submit "$run_id" admit --unit e20260821001234/SCA07 --inputs s3://bucket/deliveries/e20260821001234/SCA07
rapidpipe run submit "$run_id" register --unit e20260821001234-reg/SCA07 --inputs-from-stage admit
rapidpipe run reconcile "$run_id"
```

Each job definition must run the image built from [`containers/rapid-pipeline`](containers/rapid-pipeline), with a retry rule on exit code 75.

### Environment read inside the container

Beyond the deployment variables above, `rapidpipe` reads these directly from the process environment, none committed here:

- `RAPID_PARAMETER_PATH` -- the SSM parameter tree `rapidpipe.db.connection` reads for its `db/server`, `db/port`, `db/name` and `db/secret-id` keys when the `PG*` variables are not set; set by the job definition.
- `RAPIDPIPE_IMAGE_DIGEST`, else `RAPID_IMAGE_DIGEST` -- the image digest a stage's execution record carries (`rapidpipe.stages.contract`); `RAPIDPIPE_IMAGE_DIGEST` wins when both are set.
- `RAPIDPIPE_RELEASE`, else `RAPID_RELEASE_IDENTITY` -- the release identity a stage's execution record carries; `RAPIDPIPE_RELEASE` wins when both are set.
- `RAPID_SOURCE_REVISION` -- the source revision a stage's execution record carries when `git rev-parse HEAD` fails in the container (no `.git` checkout there); `run create` instead asks git directly for the same field and never reads this variable.
- `RAPIDPIPE_LOAD_DATABASE`, `RAPIDPIPE_MAINTAIN_DATABASE`, `RAPIDPIPE_CROSSMATCH_DATABASE`, `RAPIDPIPE_STATISTICS_DATABASE`, `RAPIDPIPE_PRUNE_DATABASE`, `RAPIDPIPE_ALERTS_DATABASE`, `RAPIDPIPE_EXPORT_DATABASE` -- each names a `module:factory` that returns an alternate database for that one stage (`load`, `maintain`, `crossmatch`, `statistics`, `prune`, `alerts`, `export` respectively) in place of PostgreSQL; unset in every deployment, read only by that stage's own fixture, since a stage run as a subprocess cannot be monkeypatched.
- `RAPIDPIPE_DIFFERENCE_TOOLKIT` / `RAPIDPIPE_REFERENCE_TOOLKIT` -- each names a `module:factory` that returns an alternate `Toolkit` for the `difference` or `reference` stage in place of the real tools; unset in every deployment, read only by that stage's own fixture and the `run local` smoke test.
- `RAPIDPIPE_RELEASE_HOOKS` -- launcher-side, not container-side: the default `--hooks-dir` for `release cut`/`release verify`, read on the machine that cuts or verifies a release, not inside a stage's container.

## Walking and inspecting runs

- `rapidpipe run create ... [--seed <run>]` records the run a new one was seeded from: lineage only, inheriting no configuration. `--seed <run> --only-failed` re-runs the seed's failed units instead (see Recovery below).
- `rapidpipe run start <run> --unit U [--stage S] [--inputs [S=]LOC]... [--settings [S=]LOC]... [--template S=LOC]... [--no-wait] [--interval SEC] [--timeout SEC] [--profile]` walks the run's selected stages in order for one unit on Batch. It skips complete stages; each other stage gets its next attempt, waited for by reconciling every `--interval` seconds. A transient or lost result that returns the unit to ready gets another attempt within the run's allowance.

  Inputs are `--inputs S=LOC` (unprefixed: the first stage's), else an input set composed from `--template S=LOC` (refused for `register`, whose inputs are always the producing stage's output), else the selected output of the nearest preceding stage other than `register`.

  It exits 0 when every stage is complete, 1 when a unit is failed or cancelled, and 75 on `--timeout`. Rerunning the command continues, except when an attempt is left running with no Batch job because submission failed after allocation: that exits 64 and names `run reconcile <run> --resolve-jobless`.
- `rapidpipe run status <run> [--watch] [--interval SEC]` reconciles and prints one line per unit; it exits 0 when all are complete, 1 when any failed or was cancelled, 2 while any is running.
- `rapidpipe run inputs <run> <stage> --unit U --from-stage P --template LOC [--dest LOC] [--kind l2-image]` copies a template input set's entries and the producer unit's `--kind` entry (under `l2/`) into one prefix, verifies the copied sizes, binds the unit's inputs and commits. It writes `manifest.json` last, never over an existing one.

  For every run kind, the input set lives under the scratch outputs root. The default `--dest` is `<scratch outputs root>/runs/<run>/inputs/<stage>/<unit>`; a `--dest` elsewhere is refused. `run delete` removes `runs/<run>/inputs/` with the run, refusing if that prefix is outside the scratch bucket.
- `rapidpipe run compare <a> <b>` prints both runs' dispositions, settings and product instances side by side, then `same` (exit 0) or `different` (exit 1).
- `rapidpipe run expire [--now ISO8601]` deletes every expired, unpinned scratch run, one deletion report per run.
- `rapidpipe run timings <run> [--stage S] [--json]` prints one row per attempt: stage, unit, attempt, disposition, `queue_s`/`exec_s`/`reconcile_lag_s`, `fetch_s`/`body_s` and `over_30m`. It follows with a per-stage summary: count, median, p90, max of `exec_s`, and how many exceeded 30 minutes.

  Batch durations come from its job timestamps: `reconcile` copies `createdAt`/`startedAt`/`stoppedAt` into `execution_records.scheduler_metadata` under a `"batch"` key. Stage durations are copied the same way into a `"stage"` key from the stage's execution record's `timing`, when present.

  `publish_s` always prints `-`: the execution record is written before publishing, so this phase reaches only the stage's final log line, not `scheduler_metadata`. A local run or an attempt Batch never resolved prints `-` throughout. The command is read-only, with data on stdout; it exits 0, or 64 for an unknown run.
- `rapidpipe stage run <name> ...` (also `rapidpipe stage <name> ...`), `rapidpipe stage list` and `rapidpipe stage describe <name>` run, list and describe stages.

```
rapidpipe run start "$run_id" --unit r0034001002001001001/SCA01 \
    --inputs s3://bucket/deliveries/r0034001002001001001/SCA01 \
    --template difference=s3://bucket/templates/SCA01-W146
```

## Recovery

Every Batch attempt records the inputs and settings locations it was submitted with (`attempts.inputs_location`, `attempts.settings_location`, migration `20260924-10`).

- `rapidpipe run create --seed <run> --only-failed [--purpose P] [--owner O]` creates a run that re-runs the seed's non-complete units: failed or cancelled, or left running or ready by a lost, killed or job-less latest attempt. It copies the seed's configuration (kind, release or revision and digest, settings and input refs, lane, profile, database target, max attempts, check policy). It refuses a deleting or deleted seed, or one with nothing to re-run. What it re-runs depends on the seed's kind:
  - **Production seed.** Its outputs are project custody, which a re-run may consume. The stages are the seed's from the earliest position holding a non-complete unit. Every non-complete unit is seeded wherever it sits: a pending unit with `units.seeded_from_unit` set and the seed unit's input bindings copied, so the deletion fence protects what the re-run reads.
  - **Scratch seed.** Its outputs are usable only within its own run. Every stage is re-run from the first. Only the first stage's units for the failed unit ids are seeded, carrying that stage's recorded inputs and settings. No seed output is read.
- `rapidpipe run start <new run> --unit U` then resolves a seeded unit's inputs as `--inputs`, else `--template`, else the seed unit's latest attempt's recorded inputs (and its settings, unless `--settings` is given), else the preceding stage's output. A seeded `register` unit keeps the seed's unit id. A stage with no unit for U here or earlier, but a seeded unit for U later, prints `<stage> U inherited from seed <seed>` and is skipped: U completed it in the seed. A stage whose producer was inherited from a production seed reads that producer's selected output in the seed.
- `rapidpipe run reconcile <run> --resolve-jobless [--older-than SECONDS]` looks up each attempt with no disposition and no Batch job on the job queue, by the job name `run start` submits under. One job found is recorded on the attempt and reconciled (`status=REPAIRED`). More than one is left alone (`status=AMBIGUOUS`). With none, an attempt started more than `SECONDS` (default 600) ago is recorded `lost` (`status=NOJOB`), and its unit returns to ready while attempts remain, else failed.

```
rapidpipe run reconcile "$run_id" --resolve-jobless
rerun=$(rapidpipe run create --seed "$run_id" --only-failed)
rapidpipe run start "$rerun" --unit r0034001002001001001/SCA01
```

## Checks and promotion

A check is a named, versioned function over one product instance (`rapidpipe/checks/`). A check policy is a named, versioned TOML file shipped in the package (`rapidpipe/checks/policies/<name>@<version>.toml`) defining which checks apply to each kind, which are required, and their bounds. A policy change is a new version.

One policy ships: `rebuild-trial@1`, the default, with `difference-image-statistics@1` required and `catalog-counts-vs-reference@1` advisory. `rebuild-strict@1`, whose bounds the control run cannot meet, is a test fixture under `tests/fixtures/checks/`.

- `rapidpipe check list` prints the registered checks and the shipped policies.
- `rapidpipe check run <run> [--policy P] [--instance I] [--check NAME@V] [--param k=v]... [--who W]` runs the policy's checks (default: the run's `--check-policy`, else `rebuild-trial@1`) over the run's instances from selected attempts, records one `checks` row per result and prints one line per result; it exits 0 when all passed, 1 when any failed. `--param` overrides one `--check`'s bounds; such a result does not count for promotion under the policy.
- `rapidpipe check show <run> [--instance I]` prints the recorded results, newest first.
- `rapidpipe run promote <run> --reason R [--check-policy P]` refuses (exit 1) unless every required check of the policy has a latest recorded result, under the policy's own bounds, that passed; the promotion records the policy version and the check results it relied on.
- `rapidpipe run create ... [--check-policy P] [--auto-promote]` names the run's policy; `--auto-promote` is refused (exit 1) unless the policy is team-approved for automatic promotion, which no shipped policy is, so `run start` ends with `auto-promote off (policy P)`.

```
rapidpipe check run "$run_id"
rapidpipe run promote "$run_id" --reason "nightly"
```

## Running the processing-date loop

`rapidpipe loop run --spec LOC [--date YYYY-MM-DD]... [--dry-run] [--interval SEC] [--timeout SEC]` is the scheduled loop (`rapidpipe/launch/loop.py`). Its spec is a local or `s3://` TOML document naming a `[loop]` schedule, release, owner, lane, optional check policy and attempt allowance, and `[[dates]]` with their `[[dates.detector_images]]` (delivery, admit settings, difference template and settings).

In spec order, the loop processes each date whose `loop_dates` row is absent or `open`. It creates the date's production run from the release (as `run create --release` does) or resumes the open run, then uses `run start`'s walk for:

- admit, register, difference, finalize, register and load per detector image; the raw difference is never registered;
- maintain per `<yyyymmdd>/SCA<nn>`;
- crossmatch, statistics and prune per field;
- alerts per image.

A field's crossmatch input set carries every source set of the date. Its base is the association set of the most recent earlier complete date of the schedule that crossmatched that field.

The loop records the policy's checks as `scheduler` and promotes the run only if the policy permits automatic promotion. Under `rebuild-trial@1`, the run remains a candidate for a person to promote in date order; a refusal is recorded on the row rather than failing the date. The loop finishes the run and writes the date's record to `loop_dates`.

Only one loop runs per schedule at a time; a second exits 75. Within a date, every unit a phase can run is walked before the date fails. A date whose run was finished elsewhere completes only when every required unit is complete.

The loop exits 0 when every processed date is complete, 1 at the first failed date without starting later ones, 64 on a refusal such as an incomplete release, and 75 on a timeout. Rerunning after a timeout resumes. `--retry-failed` reopens a failed date on a new run seeded from that date's run through `run create --seed <run> --only-failed`, recording the old run in the row's `previous_runs`.

`rapidpipe loop plan --spec LOC` prints what `run` would do; `rapidpipe loop show <schedule> [--json]` prints the schedule's rows.

## Logging and profiling

Every line `rapidpipe` logs has the shape `<ts>Z <LEVEL> run=<r> attempt=<a> stage=<s> unit=<u> <logger> <message>`, with a UTC, millisecond-precision timestamp. `run`/`attempt`/`stage`/`unit` are `-` where not yet known, such as before a stage's argv is parsed or for the CLI's own messages.

Per the repository's Unix convention, commands whose stdout carries data log to stderr. Stage logs and the CLI's own library-warning lines go to stderr; stdout carries only data (`print`). Existing stdout contracts and exit codes are unchanged.

- `RAPIDPIPE_LOG_LEVEL` -- optional: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, overriding the default level. A stage invocation (`rapidpipe stage <name>`, so also `run local`, `run submit`'s Batch job, and `run start`'s walk) defaults to `INFO`; the CLI's own top-level logging (library warnings that would otherwise reach Python's last-resort handler with no timestamp) defaults to `WARNING`.
- A stage invocation also writes its log lines to a file at `<outputs>/log/<stage>.log` alongside its other outputs (not a manifest member, the same as `exec/<attempt>.json`): for a local `--outputs` it lives there directly; for an `s3://` `--outputs` it is uploaded with the rest on success, and re-uploaded on its own, best-effort, on failure (a failed upload logs a warning and never changes the exit code). Skipped under `--dry-run`, which writes nothing.
- The final success line and both error lines carry `elapsed_s=`/`fetch_s=`/`body_s=`/`publish_s=` (the stage's own wall-clock phases -- input and settings fetch, the stage body, and publishing the manifest and outputs -- rounded to 0.1s; a phase not reached prints `-`). The execution record (`exec/<attempt>.json`) gets an additive `timing` key with `started` (UTC ISO), `fetch_s` and `body_s` (no `ended`: the record is written before publish, so the total and publish durations are not known yet).
- `RAPIDPIPE_PROFILE=1` profiles the stage body under `cProfile`, writing `profile/<stage>.pstats` and `profile/<stage>.txt` (sorted by cumulative time, top 40) into the outputs directory before the manifest, so they publish with the attempt like the log file; they are not manifest members either. `run submit`, `run start` and `run local` each take a `--profile` flag that sets this (in the Batch job's `containerOverrides.environment` for `submit`/`start`, in the local subprocess's environment for `local`); refused with exit 64 on a production run, since a profile lands in the attempt's own outputs prefix, which for production is the products bucket, not a scratch location.

## Documentation

Install instructions and documentation are on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/).

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
