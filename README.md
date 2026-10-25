# RAPID

Repository for the RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team.

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Getting started

This takes one epoch of the HLWAS simulation (pointing `001_0001`, SCA 8, F146) through the whole chain on your own machine: intake, PSF, a reference from two earlier passes, the difference, and the epoch's packet set. Every stage runs inside the pipeline image, which carries the tools (`sex`, `swarp`, `awaicgen`, `bkgest`), against a throwaway PostgreSQL 18. It took four and a half minutes on a 16 GB machine. To start a night on the team's database and Batch instead, read [Starting a night](https://roman-rapid.readthedocs.io/en/latest/system/loop.html#starting-a-night).

### What you need

- `podman` (or `docker` under the same commands) and `jq`, and this checkout, with your shell at its top.
- At least 16 GB of memory (the difference peaked at 15.2 GB) and 2 GB of disk.
- Network access to the public bucket `s3://stpubdata` (read unsigned, no AWS account) and to `https://roman-crds.stsci.edu` (the PSF stage's reference).
- The pipeline image: the team's `rapid-pipeline:rebuild-<sha>` from the registry `rapid_systems` names, or one you build ([`containers/README.md`](containers/README.md)).
- A Gaia DR3 catalog covering the field, as one Parquet file (the team's is `gaia-dr3-hlwas-r00342-box.parquet`).
- PostgreSQL 18 as the public `docker.io/library/postgres:18` image, the one CI uses.

### The environment

The local values are set, and the team deployment's values are commented. The packet job sends its set to Kafka as its last act. Locally, the three `RAPIDPIPE_KAFKA_*` variables point that send at a closed local port. The send fails, the job still publishes and exits 0, and nothing reaches the team's topic.

```sh
export IMAGE=<the pipeline image>                 # e.g. rapid-pipeline:rebuild-<sha>
export GAIA=<your Gaia DR3 Parquet file>
export PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=rapid PGUSER=postgres PGPASSWORD=local
export RAPIDPIPE_OUTPUTS_ROOT=file:///work/out    # ./rapid-local/out, as the container sees it
export AWS_REGION=us-east-1                       # the public bucket's region
export RAPIDPIPE_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9 RAPIDPIPE_KAFKA_TOPIC=local RAPIDPIPE_KAFKA_MAX_REQUEST_BYTES=1048576
# The team's database and Batch instead:
#   unset PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD RAPIDPIPE_KAFKA_BOOTSTRAP_SERVERS RAPIDPIPE_KAFKA_TOPIC RAPIDPIPE_KAFKA_MAX_REQUEST_BYTES
#   export RAPID_PARAMETER_PATH=<the deployment's parameter tree>   # its db/* keys name the database and its secret
#   (or keep PGHOST, PGPORT and PGDATABASE and export RAPID_DB_SECRET_ID=<the database secret> for the credential)
#   export RAPIDPIPE_OUTPUTS_ROOT=s3://<bucket>/<prefix>
#   export RAPIDPIPE_BATCH_JOB_QUEUE=<the Batch job queue>
#   export AWS_PROFILE=<your profile> AWS_REGION=<the deployment's region>
R=$PWD L=$PWD/rapid-local                         # this checkout and the run's working directory
ctr() { podman run --rm --pod rapid-local -v "$L:/work:Z" -v "$R/scripts:/scripts:ro,z" \
  -e PGHOST -e PGPORT -e PGDATABASE -e PGUSER -e PGPASSWORD -e RAPIDPIPE_OUTPUTS_ROOT -e AWS_REGION \
  -e RAPIDPIPE_KAFKA_BOOTSTRAP_SERVERS -e RAPIDPIPE_KAFKA_TOPIC -e RAPIDPIPE_KAFKA_MAX_REQUEST_BYTES \
  --entrypoint "$1" "$IMAGE" "${@:2}"; }
rp() { ctr /usr/local/bin/rapidpipe-entrypoint "$@"; }     # the rapidpipe command line, in the image
job_id() { jq -e 'select(.published and .refusal == null) | .job'; }   # a published job's id, else nothing and exit 4
sql() { podman exec rapid-local-db psql -U postgres -d rapid -X -tA -c "$1"; }
```

### The database

```sh
mkdir -p "$L/out" && cp "$GAIA" "$L/gaia.parquet"
podman pod create --name rapid-local
podman run -d --pod rapid-local --name rapid-local-db -e POSTGRES_PASSWORD=local -e POSTGRES_DB=rapid docker.io/library/postgres:18
until podman exec rapid-local-db pg_isready -h 127.0.0.1 -U postgres -d rapid; do sleep 1; done
podman exec -i rapid-local-db psql -U postgres -d rapid -X -q -v ON_ERROR_STOP=1 < database/schema.sql
```

`schema.sql` builds a fresh database whole. It prints one NOTICE because `postgres:18` lacks q3c. The dated `database/2026-*.sql` files bring an older database forward and fail on a fresh one, so do not apply them here.

### One epoch

A run is the recipe, and it is made live for its dataset so that each job's products become official as it publishes. Each `job local` runs one stage in the container and prints its result as JSON. `job_id` keeps the job's id, or fails if the job did not publish; the stage's log goes to stderr. Dataset `r00342-local` is yours alone, and F146 is fid 8 (`W146` in `filters`).

```sh
SRC=s3://stpubdata/roman/nexus/soc_simulations/r00342/l2/
REV=$(podman image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")
RUN=$(rp run create --who "$USER" --why "local quickstart" --revision "$REV" \
  --overlay /code/rapidpipe/settings/datasets/hlwas-r00342.toml --set "intake.source_root=\"$SRC\"" \
  --survey HLWAS --dataset r00342-local)
echo '[{"open": {"survey": "HLWAS", "dataset": "r00342-local", "run": '$RUN'}}]' > "$L/live.json"
rp official change --group /work/live.json --who "$USER" --why "local run $RUN live"

J_GAIA=$(rp job local --run $RUN --stage import \
  --scope '{"source_dir": "/work", "gaia": {"name": "gaia", "version": "DR3", "path": "gaia.parquet"}}' \
  --expects '[{"product": "catalogs", "key": {"dataset": "r00342-local", "name": "gaia", "version": "DR3"}, "binding": null}]' | job_id)
GAIA_IN='{"product": "catalogs", "key": {"dataset": "r00342-local", "name": "gaia", "version": "DR3"}, "source_job": '$J_GAIA'}'

J_PSF=$(rp job local --run $RUN --stage psf --scope '{"dataset": "r00342-local", "file": "r0034201001001004001_0001_wfi08_f146"}' \
  --expects '[{"product": "psfs", "key": {"dataset": "r00342-local", "fid": 8, "sca": 8, "kind": "science"}, "binding": null}]' | job_id)
PSF_IN='{"product": "psfs", "key": {"dataset": "r00342-local", "fid": 8, "sca": 8, "kind": "science"}, "source_job": '$J_PSF'}'

intake() {   # intake EXPID STEM: one SOC L2 file to an l2files row
  rp job local --run $RUN --stage intake --scope '{"dataset": "r00342-local", "file": "'$2'"}' --inputs "[$PSF_IN, $GAIA_IN]" \
    --expects '[{"product": "exposures", "key": {"dataset": "r00342-local", "expid": '$1'}, "binding": null},
                {"product": "l2files", "key": {"dataset": "r00342-local", "expid": '$1', "sca": 8, "fid": 8}, "binding": null}]' | job_id
}
l2() { echo '{"product": "l2files", "key": {"dataset": "r00342-local", "expid": '$1', "sca": 8, "fid": 8}, "source_job": '$2'}'; }
J_1=$(intake 1040101 r0034201001001004001_0001_wfi08_f146)    # pass 1
J_2=$(intake 2040101 r0034201002001004001_0001_wfi08_f146)    # pass 2
J_4=$(intake 4040101 r0034201004001004001_0001_wfi08_f146)    # pass 4, the epoch
FIELD=$(sql "SELECT field FROM l2files_official WHERE dataset = 'r00342-local' AND expid = 1040101")

J_REF=$(rp job local --run $RUN --stage reference --scope '{"field": '$FIELD', "fid": 8, "sca": 8}' \
  --inputs "[$(l2 1040101 $J_1), $(l2 2040101 $J_2)]" \
  --expects '[{"product": "refimages", "key": {"dataset": "r00342-local", "field": '$FIELD', "fid": 8}, "binding": null}]' | job_id)
REF_IN='{"product": "refimages", "key": {"dataset": "r00342-local", "field": '$FIELD', "fid": 8}, "source_job": '$J_REF'}'

EPOCH='{"dataset": "r00342-local", "expid": 4040101, "sca": 8, "fid": 8}'
J_DIFF=$(rp job local --run $RUN --stage difference --scope '{"expid": 4040101, "sca": 8, "fid": 8}' \
  --inputs "[$(l2 4040101 $J_4), $PSF_IN, $REF_IN]" \
  --expects '[{"product": "diffimages", "key": '"$EPOCH"', "binding": null}, {"product": "sources", "key": '"$EPOCH"', "binding": null}]' | job_id)

J_PKT=$(rp job local --run $RUN --stage packet --scope '{"expid": 4040101, "sca": 8, "fid": 8}' \
  --inputs '['"$REF_IN, $GAIA_IN, $PSF_IN"', {"product": "sources", "key": '"$EPOCH"', "source_job": '$J_DIFF'}]' \
  --expects '[{"product": "packets", "key": '"$EPOCH"', "binding": null}]' | job_id)
```

The packet job's JSON ends with `"kafka": {"error": "KafkaTimeoutError: Unable to bootstrap from ['127.0.0.1:9']"}`, which is the redirect working.

### Check it

```sh
ctr /opt/rapid/conda/envs/rapid/bin/python3 /scripts/validate_run.py --run $RUN     # "run N: 6/6 checks PASS"
ctr /opt/rapid/conda/envs/rapid/bin/python3 /scripts/read_packets.py --job $J_PKT   # one set, failures 0
sql "SELECT count(*) FROM packets_official WHERE dataset = 'r00342-local'"
```

The products are under `rapid-local/out/$RUN/<stage>/<job>/1/`. `rp job show <job>` prints a job's inputs, manifest and bindings. When you are done, `podman pod rm -f rapid-local` removes the database and both containers. `rapid-local/` stays until you delete it.

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time, never committed. Reference bulk data artifacts (product listings, large generated files) with the command to reproduce them instead of committing them.

The [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html) is the design authority for repository boundaries and stage interfaces. [AGENTS.md](AGENTS.md) presents the same rules for coding agents, plus the package map and test/CI conventions.

### Layout

`rapidpipe/` is the pipeline package; `database/` holds the schema (`schema.sql`) and the sky-tessellation helpers; `modules/` and `cdf/` hold the `dev` helpers, scripts and configuration files the stages use; `containers/` holds the image recipe; `tests/`, `scripts/` (public-safety and science-drift checks), `docs/` and `c/` (vendored C tool source) complete the tree. [AGENTS.md](AGENTS.md) has the full layout table.

## Package

The distribution is `rapid-pipeline`; its import package is `rapidpipe`. The foundation plan (v5, 2026-10-20) is the design authority for its database side: one schema file, runs and jobs, the publisher, official bindings changed in recorded groups, and five verbs. `rapidpipe.foundation` holds those operations over [`database/schema.sql`](database/schema.sql); `rapidpipe.db` holds the connection, which gets database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a committed file.

`rapidpipe stage <name>` runs one stage on the older contract (`admit` and `finalize`, which the loop does not run) against a declared unit of work, input manifest and output location, implementing [the contract page](https://roman-rapid.readthedocs.io/en/latest/system/stage-contract.html). `--inputs`/`--outputs` accept a local directory or an `s3://bucket/prefix` location. For S3, the runner fetches (or stages) it in a temporary work directory before running the stage body, using `RAPIDPIPE_WORK` if set, else the system temp directory. Only `admit` and `finalize` are still on this contract, and the loop runs neither; every other stage runs under a job (`job local`, `job run`, below).

## Container

[`containers/rapid-pipeline`](containers/rapid-pipeline) holds the recipe that builds `rapidpipe` into a runnable image over a base environment supplying its dependencies. Which base image that is and where the built image is published are decided and owned outside this repository, in `rapid_systems`. See [`containers/README.md`](containers/README.md) for how to build the recipe locally.

## The command line

The database is built by `psql -v ON_ERROR_STOP=1 -f database/schema.sql` on an empty PostgreSQL 18 ([`database/README.md`](database/README.md)). The verbs:

- `rapidpipe run create --who W --why Y --revision REV [--settings FILE] [--overlay DATASET.toml] [--set TABLE.key=VALUE ...] --survey S --dataset D` records a run (a recipe someone launched for a reason) and prints its id. The body it stores is resolved here, once, from three layers: `FILE` (default the shipped `rapidpipe/settings/foundation-thin.toml`, dev's values; TOML, or JSON when its name ends in `.json`), the dataset's overlay (`rapidpipe/settings/datasets/`, only the keys that differ, each with the measurement that earned it), then each `--set` in order (a TOML value, strings quoted). A key the base lacks is refused, naming it, except the two the base documents as absent by default (`intake.source_root`, `reference.selection.before_mjd`). The run also stores the body's hash and each layer's name and SHA-256 (`runs.layers`), which `run show` prints.
- `rapidpipe official change --group FILE --who W --why Y` applies one change group in one transaction and prints its id. `FILE` is a JSON list of items, each with `close` (the id of an open binding) and/or `open` (a product binding `{"product", "key", "job"}` or the live binding `{"survey", "dataset", "run"}`).
- `rapidpipe official undo <group-id> --who W --why Y` applies the inverse of a change group (close what it opened, open as new rows what it closed) and prints the new group's id.
- `rapidpipe run show <id>` prints the run, its live binding and its jobs as JSON.
- `rapidpipe job show <id>` prints the job, its recorded inputs and every binding of its rows as JSON.
- `rapidpipe job local --run R --stage NAME --scope JSON [--inputs JSON] [--expects JSON]` records a job under run `R` with its inputs (the lists `rapidpipe.foundation.create_job` takes) and runs it here; `rapidpipe job run --job J` runs an existing job's next attempt. Running a job reads it first and stops at once if it is published; otherwise it calls the stage's `run(job)` in this process with a `rapidpipe.foundation.JobContext` (job, run, scope, the run's settings body, the recorded inputs, the attempt's prefix `<RAPIDPIPE_OUTPUTS_ROOT>/<run>/<stage>/<job>/<attempt>/`), then publishes the attempt and prints the publisher's result as JSON. The stage writes its files and `manifest.json` and never publishes. The outputs root is a `file://` or `s3://` URL; under `s3://` the stage writes a local working directory that is uploaded, manifest last, before publishing. `rapidpipe job submit` takes `job local`'s arguments and `--job-definition NAME`, records the job and submits one Batch job on `RAPIDPIPE_BATCH_JOB_QUEUE` whose command is `job run --job J` in the image, passing `RAPIDPIPE_OUTPUTS_ROOT` (`s3://` only) and `RAPID_DB_SECRET_ID` when set. The foundation's stages are `import` (`rapidpipe.stages.import_products`) and `packet` (scope `{"expid", "sca", "fid"}`; inputs: the epoch's source set, the science `psfs`, the Gaia `catalogs` product, the `refimages`; it reads the triggers' objects and members, the forced window's `diffimages` and `l2files` and the official `packets` sets' forced rows as they stand at its read and records them; outputs Avro 00.06 packets and the `packets`, `forced` and `external` tables) and `reference` (scope `{"field", "fid", "sca"}`; inputs: the `l2files` to coadd; outputs the mosaic, its coverage and uncertainty maps and SExtractor catalog, and the `refimages`, `refimimages` and `refimcatalogs` tables), with `intake`, `psf` and `difference` beside them ([Getting started](#getting-started) runs all six); `admit` and `finalize` keep the older contract.

`python scripts/read_packets.py [--job N ...] [--out FILE]` reads published packet sets back (the Avro, `packets_official`, `forced_official`) and writes a validation report, exiting non-zero on any failure; a packet job sends its own official set to the Kafka topic its run's `[kafka]` table names as its last act, one message per packet keyed by the dataset with the set and sid in its `rapid-set` and `rapid-sid` headers, stamping `jobs.kafka_published_at` and the partition and first and last offsets after every acknowledgement; `rapidpipe publish --run N` sends the run's official sets not yet stamped (the re-publication verb and the loop's recovery sweep), and `python scripts/consume_packets.py --run N` reads each set's recorded range back, counts its packets and compares the bytes of one set per field with the outbox; `scripts/check_run.sh --run N [--settings F]` runs a run's three checks (`validate_run.py`, `read_packets.py` over its bound packet sets eight processes at a time, `consume_packets.py`) and prints one line, `Check run N: CLEAN|FAIL validate ... reader ... consumer ...` with each one's count and seconds, exiting non-zero on any failure; `rapidpipe loop --run N --filters F146 --scas 8 --pointings VVV_EEEE,...|all --job-definition D [--once]` lists the run's `[intake] source_root`, submits every ready job of the selection one stage forward on Batch (intake, reference, difference, which associates its own sources with the objects of their fields as they stand, then a packet per official source set, in any order) and ends each pass with `publish`, printing one summary line per pass and repeating every `--interval` seconds until a pass finds the selection drained (exit 0) or a job of the run has failed or was published with its binding refused (exit 1); `database/queries.sql` holds the read queries the official views answer.

A change group refused because a binding it names is no longer open, or because a key it opens already has an open binding, and a job published but refused its automatic binding, exit 1 with the binding named on stderr; a run, job or group that does not exist, or a file that cannot be read, exits 64.

`rapidpipe stage run <name> ...` (also `rapidpipe stage <name> ...`), `rapidpipe stage list` and `rapidpipe stage describe <name>` run, list and describe the older-contract stages, `admit` and `finalize`, which the loop does not run; `rapidpipe selftest --stage finalize` runs a stage's packaged fixture.

## Running a stage under a job

`rapidpipe.launch` starts a job's stage once its job row exists (`rapidpipe.foundation.create_job`): `submit` sends `job run --job <id>` to AWS Batch under the job name `<prefix>-<stage>-<job>`, and `run_locally` runs an old-contract stage as a subprocess. Batch retries under the job definition's retry strategy; nothing here retries. The job definition is the `--job-definition` argument of `job submit` and `loop`; the rest comes from the environment:

- `RAPIDPIPE_BATCH_JOB_QUEUE` -- the Batch job queue name or ARN.
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
