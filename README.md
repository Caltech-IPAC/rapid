# RAPID
Repository for RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time and are never committed here. Bulk data artifacts (product listings, large generated files) are referenced, with the command to reproduce them, not committed. The design authority for this repository's boundaries and stage interfaces is the [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html).

### Package

The distribution built from this repository is `rapid-pipeline`; its import package is `rapidpipe`. `rapidpipe stage <name>` runs one stage against a declared unit of work, input manifest and output location. The stage contract that `rapidpipe` implements is documented at [the contract page](https://roman-rapid.readthedocs.io/en/latest/system/stage-contract.html). `--inputs`/`--outputs` accept either a local directory or an `s3://bucket/prefix` location; for an S3 location the runner fetches (or stages) it under a temporary work directory before running the stage body, chosen from `RAPIDPIPE_WORK` if set, else the system temp directory. `rapidpipe.db` holds persistence (the connection module and the migrations applier) and `rapidpipe.runs` holds runs, units, attempts, product instances and promotion, per [the runs page](https://roman-rapid.readthedocs.io/en/latest/system/runs.html). `rapidpipe.db` reaches its database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a file committed to this repository.

`rapidpipe.stages.admit` is the first stage: it reads a delivery manifest naming one delivered l2 image (a FITS file staged outside the pipeline, not yet a registered product), verifies the delivered bytes and FITS checksums, reads the header and WCS the products page's l2-image field list needs, copies the file into the attempt's output location, and publishes a manifest with a fresh product instance. `rapidpipe.stages.register` reads that manifest's registration block and writes the `l2files`/`l2filemeta` rows without opening the FITS file itself, registering the manifest's own product instance first and recording nothing twice on a replay. `rapidpipe.science.spatial` holds the pure spatial derivations both `register` and its tests need -- HEALPix indexes, the Roman tessellation tile id, and the exact tile-overlap footprint -- with no import of any stage, so it can be exercised without a database.

`rapidpipe run create/list/show/local` operate on `rapidpipe.runs.repository`: `create` records a new run and prints its id, `list` and `show` inspect runs, units and attempts, and `local` runs one stage attempt as a subprocess on the current machine, through `rapidpipe.runs.local`, allocating its own attempt id and an exclusive output location without needing Batch. `rapidpipe.launch` submits units to AWS Batch, resolves a unit's inputs from an upstream stage's selected attempt, and reconciles submitted jobs' results back into the run model.

- `rapidpipe run promote <run> --reason R [--who W] [--kinds a,b]` promotes a production run's candidates (one per kind and logical key) and prints the promotion id.
- `rapidpipe run rollback <promotion> --reason R [--who W]` reverses one promotion, refused if a later promotion changed any of its keys.
- `rapidpipe run finish <run>` marks a run finished once every unit is complete, failed or cancelled; a finished run admits no new units or attempts.
- `rapidpipe run delete <run> [--requested-by U]` deletes a scratch run's S3 object versions and run-scoped science rows, keeping its run-model rows as tombstones; it resumes a run left `deleting`.
- `rapidpipe run pin <run>` / `rapidpipe run unpin <run>` keep a scratch run from expiring (scratch runs expire 14 days after creation unless pinned), or release it.

#### Running on Batch

`rapidpipe.launch.batch` reads its deployment configuration from the environment only, never from a committed value:

- `RAPIDPIPE_BATCH_JOB_QUEUE` -- the Batch job queue name or ARN.
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

#### Running a run from the command line

- `rapidpipe run create ... [--seed <run>]` records the run a new one was seeded from: lineage only, inheriting no configuration.
- `rapidpipe run start <run> --unit U [--stage S] [--inputs [S=]LOC]... [--settings [S=]LOC]... [--template S=LOC]... [--no-wait] [--interval SEC] [--timeout SEC]` walks the run's selected stages in order for one unit on Batch: a complete stage is skipped, any other gets its next attempt, and each attempt is waited for by reconciling every `--interval` seconds. A transient or lost result that returns the unit to ready gets another attempt within the run's allowance. A stage's inputs are `--inputs S=LOC` (unprefixed: the first stage's), else an input set composed from `--template S=LOC` (refused for `register`, whose inputs are always the producing stage's output), else the selected output of the nearest preceding stage other than `register`. It exits 0 when every stage is complete, 1 when a unit is failed or cancelled, and 75 on `--timeout`; rerunning the same command continues, except on an attempt left running with no Batch job (its submission failed after allocation), which exits 64 and must be resolved by hand.
- `rapidpipe run status <run> [--watch] [--interval SEC]` reconciles and prints one line per unit; it exits 0 when all are complete, 1 when any failed or was cancelled, 2 while any is running.
- `rapidpipe run inputs <run> <stage> --unit U --from-stage P --template LOC [--dest LOC] [--kind l2-image]` copies a template input set's entries and the producer unit's `--kind` entry (under `l2/`) into one prefix, verifies the copied sizes, binds the unit's inputs and commits, then writes its `manifest.json` last (never over an existing one). The input set always lives under the scratch outputs root, for every run kind: the default `--dest` is `<scratch outputs root>/runs/<run>/inputs/<stage>/<unit>`, a `--dest` elsewhere is refused, and `run delete` removes `runs/<run>/inputs/` with the run (refused if that prefix is outside the scratch bucket).
- `rapidpipe run compare <a> <b>` prints both runs' dispositions, settings and product instances side by side, then `same` (exit 0) or `different` (exit 1).
- `rapidpipe run expire [--now ISO8601]` deletes every expired, unpinned scratch run, one deletion report per run.
- `rapidpipe stage run <name> ...` (also `rapidpipe stage <name> ...`), `rapidpipe stage list` and `rapidpipe stage describe <name>` run, list and describe stages.

```
rapidpipe run start "$run_id" --unit r0034001002001001001/SCA01 \
    --inputs s3://bucket/deliveries/r0034001002001001001/SCA01 \
    --template difference=s3://bucket/templates/SCA01-W146
```

#### Running a stage locally

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

### Container

[`containers/rapid-pipeline`](containers/rapid-pipeline) holds the recipe that builds `rapidpipe` into a runnable image over a base environment supplying its dependencies. Which base image that is and where the built image is published are decided and owned outside this repository, in `rapid_systems`. See [`containers/README.md`](containers/README.md) for how to build the recipe locally.

## Documentation

Install instructions and documentation are available on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to get involved. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
