# RAPID

Repository for the RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team.

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time, never committed. Reference bulk data artifacts (product listings, large generated files) with the command to reproduce them instead of committing them.

The [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html) is the design authority for repository boundaries and stage interfaces. [AGENTS.md](AGENTS.md) presents the same rules for coding agents, plus the package map and test/CI conventions.

### Layout

`rapidpipe/` is the pipeline package; `database/` holds the schema (`schema.sql`) and the sky-tessellation helpers; `modules/` and `cdf/` hold the `dev` helpers, scripts and configuration files the stages use; `containers/` holds the image recipe; `tests/`, `scripts/` (public-safety and science-drift checks), `docs/` and `c/` (vendored C tool source) complete the tree. [AGENTS.md](AGENTS.md) has the full layout table.

## Package

The distribution is `rapid-pipeline`; its import package is `rapidpipe`. The foundation plan (v5, 2026-10-20) is the design authority for its database side: one schema file, runs and jobs, the publisher, official bindings changed in recorded groups, and five verbs. `rapidpipe.foundation` holds those operations over [`database/schema.sql`](database/schema.sql); `rapidpipe.db` holds the connection, which gets database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a committed file.

`rapidpipe stage <name>` runs one stage against a declared unit of work, input manifest and output location, implementing [the contract page](https://roman-rapid.readthedocs.io/en/latest/system/stage-contract.html). `--inputs`/`--outputs` accept a local directory or an `s3://bucket/prefix` location. For S3, the runner fetches (or stages) it in a temporary work directory before running the stage body, using `RAPIDPIPE_WORK` if set, else the system temp directory. Three stages ship on this contract: `admit`, `difference` and `finalize`. They write files and declare no database access; wrapping each in the publish-last shape (inputs recorded on the job at launch, declared Parquet outputs, the publisher called last) is the plan's next step, and the database stages of the earlier run model are ported then.

## Container

[`containers/rapid-pipeline`](containers/rapid-pipeline) holds the recipe that builds `rapidpipe` into a runnable image over a base environment supplying its dependencies. Which base image that is and where the built image is published are decided and owned outside this repository, in `rapid_systems`. See [`containers/README.md`](containers/README.md) for how to build the recipe locally.

## The command line

The database is built by `psql -v ON_ERROR_STOP=1 -f database/schema.sql` on an empty PostgreSQL 18 ([`database/README.md`](database/README.md)). The verbs:

- `rapidpipe run create --who W --why Y --revision REV [--settings FILE] [--overlay DATASET.toml] [--set TABLE.key=VALUE ...] --survey S --dataset D` records a run (a recipe someone launched for a reason) and prints its id. The body it stores is resolved here, once, from three layers: `FILE` (default the shipped `rapidpipe/settings/foundation-thin.toml`, dev's values; TOML, or JSON when its name ends in `.json`), the dataset's overlay (`rapidpipe/settings/datasets/`, only the keys that differ, each with the measurement that earned it), then each `--set` in order (a TOML value, strings quoted). A key the base lacks is refused, naming it, except the two the base documents as absent by default (`intake.source_root`, `reference.selection.before_mjd`). The run also stores the body's hash and each layer's name and SHA-256 (`runs.layers`), which `run show` prints.
- `rapidpipe official change --group FILE --who W --why Y` applies one change group in one transaction and prints its id. `FILE` is a JSON list of items, each with `close` (the id of an open binding) and/or `open` (a product binding `{"product", "key", "job"}` or the live binding `{"survey", "dataset", "run"}`).
- `rapidpipe official undo <group-id> --who W --why Y` applies the inverse of a change group (close what it opened, open as new rows what it closed) and prints the new group's id.
- `rapidpipe run show <id>` prints the run, its live binding and its jobs as JSON.
- `rapidpipe job show <id>` prints the job, its recorded inputs and every binding of its rows as JSON.
- `rapidpipe job local --run R --stage NAME --scope JSON [--inputs JSON] [--expects JSON]` records a job under run `R` with its inputs (the lists `rapidpipe.foundation.create_job` takes) and runs it here; `rapidpipe job run --job J` runs an existing job's next attempt. Running a job reads it first and stops at once if it is published; otherwise it calls the stage's `run(job)` in this process with a `rapidpipe.foundation.JobContext` (job, run, scope, the run's settings body, the recorded inputs, the attempt's prefix `<RAPIDPIPE_OUTPUTS_ROOT>/<run>/<stage>/<job>/<attempt>/`), then publishes the attempt and prints the publisher's result as JSON. The stage writes its files and `manifest.json` and never publishes. The outputs root is a `file://` or `s3://` URL; under `s3://` the stage writes a local working directory that is uploaded, manifest last, before publishing. `rapidpipe job submit` takes `job local`'s arguments and `--job-definition NAME`, records the job and submits one Batch job on `RAPIDPIPE_BATCH_JOB_QUEUE` whose command is `job run --job J` in the image, passing `RAPIDPIPE_OUTPUTS_ROOT` (`s3://` only) and `RAPID_DB_SECRET_ID` when set. The foundation's stages are `import` (`rapidpipe.stages.import_products`) and `packet` (scope `{"expid", "sca", "fid"}`; inputs: the epoch's source set, the science `psfs`, the Gaia `catalogs` product, the `refimages`; it reads the triggers' objects and members, the forced window's `diffimages` and `l2files` and the official `packets` sets' forced rows as they stand at its read and records them; outputs Avro 00.06 packets and the `packets`, `forced` and `external` tables) and `reference` (scope `{"field", "fid", "sca"}`; inputs: the `l2files` to coadd; outputs the mosaic, its coverage and uncertainty maps and SExtractor catalog, and the `refimages`, `refimimages` and `refimcatalogs` tables) so far; `admit`, `difference` and `finalize` keep the older contract until their port.

`python scripts/read_packets.py [--job N ...] [--out FILE]` reads published packet sets back (the Avro, `packets_official`, `forced_official`) and writes a validation report, exiting non-zero on any failure; a packet job sends its own official set to the Kafka topic its run's `[kafka]` table names as its last act, one message per packet keyed by the dataset with the set and sid in its `rapid-set` and `rapid-sid` headers, stamping `jobs.kafka_published_at` and the partition and first and last offsets after every acknowledgement; `rapidpipe publish --run N` sends the run's official sets not yet stamped (the re-publication verb and the loop's recovery sweep), and `python scripts/consume_packets.py --run N` reads each set's recorded range back, counts its packets and compares the bytes of one set per field with the outbox; `scripts/check_run.sh --run N [--settings F]` runs a run's three checks (`validate_run.py`, `read_packets.py` over its bound packet sets eight processes at a time, `consume_packets.py`) and prints one line, `Check run N: CLEAN|FAIL validate ... reader ... consumer ...` with each one's count and seconds, exiting non-zero on any failure; `rapidpipe loop --run N --filters F146 --scas 8 --pointings VVV_EEEE,...|all --job-definition D [--once]` lists the run's `[intake] source_root`, submits every ready job of the selection one stage forward on Batch (intake, reference, difference, which associates its own sources with the field's objects as they stand, then a packet per official source set, in any order) and ends each pass with `publish`, printing one summary line per pass and repeating every `--interval` seconds until a pass finds the selection drained (exit 0) or a job of the run has failed (exit 2); `database/queries.sql` holds the read queries the official views answer.

A change group refused because a binding it names is no longer open, or because a key it opens already has an open binding, and a job published but refused its automatic binding, exit 1 with the binding named on stderr; a run, job or group that does not exist, or a file that cannot be read, exits 64.

`rapidpipe stage run <name> ...` (also `rapidpipe stage <name> ...`), `rapidpipe stage list` and `rapidpipe stage describe <name>` run, list and describe stages; `rapidpipe selftest --stage finalize` runs a stage's packaged fixture.

## Running a stage under a job

`rapidpipe.launch` starts a job's stage once its job row exists (`rapidpipe.foundation.create_job`): `submit` sends `rapidpipe stage <name> <argv>` to AWS Batch under the job name `<prefix>-<stage>-<job>`, and `run_locally` runs it as a subprocess. Batch retries under the job definition's retry strategy; nothing here retries. Its configuration comes from the environment only:

- `RAPIDPIPE_BATCH_JOB_QUEUE` -- the Batch job queue name or ARN.
- `RAPIDPIPE_BATCH_JOB_DEFINITION` -- the Batch job definition name or ARN.
- `RAPIDPIPE_BATCH_JOB_NAME_PREFIX` -- optional, default `rapid`.

### Environment read inside the container

`rapidpipe` reads these directly from the process environment, none committed here:

- `RAPID_PARAMETER_PATH` -- the SSM parameter tree `rapidpipe.db.connection` reads for its `db/server`, `db/port`, `db/name` and `db/secret-id` keys when the `PG*` variables are not set; set by the job definition.
- `RAPIDPIPE_IMAGE_DIGEST`, else `RAPID_IMAGE_DIGEST` -- the image digest a stage's execution record carries (`rapidpipe.stages.contract`); `RAPIDPIPE_IMAGE_DIGEST` wins when both are set.
- `RAPIDPIPE_RELEASE`, else `RAPID_RELEASE_IDENTITY` -- the release identity a stage's execution record carries; `RAPIDPIPE_RELEASE` wins when both are set.
- `RAPID_SOURCE_REVISION` -- the source revision a stage's execution record carries when `git rev-parse HEAD` fails in the container (no `.git` checkout there).

## Logging and profiling

Every line `rapidpipe` logs has the shape `<ts>Z <LEVEL> run=<r> attempt=<a> stage=<s> unit=<u> <logger> <message>`, with a UTC, millisecond-precision timestamp. `run`/`attempt`/`stage`/`unit` are `-` where not yet known, such as before a stage's argv is parsed or for the CLI's own messages.

Per the repository's Unix convention, commands whose stdout carries data log to stderr. Stage logs and the CLI's own library-warning lines go to stderr; stdout carries only data (`print`).

- `RAPIDPIPE_LOG_LEVEL` -- optional: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, overriding the default level. A stage invocation defaults to `INFO`; the CLI's own top-level logging (library warnings that would otherwise reach Python's last-resort handler with no timestamp) defaults to `WARNING`.
- A stage invocation also writes its log lines to a file at `<outputs>/log/<stage>.log` alongside its other outputs (not a manifest member, the same as `exec/<attempt>.json`): for a local `--outputs` it lives there directly; for an `s3://` `--outputs` it is uploaded with the rest on success, and re-uploaded on its own, best-effort, on failure (a failed upload logs a warning and never changes the exit code). Skipped under `--dry-run`, which writes nothing.
- The final success line and both error lines carry `elapsed_s=`/`fetch_s=`/`body_s=`/`publish_s=` (the stage's own wall-clock phases -- input and settings fetch, the stage body, and publishing the manifest and outputs -- rounded to 0.1s; a phase not reached prints `-`). The execution record (`exec/<attempt>.json`) gets an additive `timing` key with `started` (UTC ISO), `fetch_s` and `body_s`.
- `RAPIDPIPE_PROFILE=1` profiles the stage body under `cProfile`, writing `profile/<stage>.pstats` and `profile/<stage>.txt` (sorted by cumulative time, top 40) into the outputs directory before the manifest, so they publish with the attempt like the log file; they are not manifest members either.

## Documentation

Install instructions and documentation are on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/).

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
