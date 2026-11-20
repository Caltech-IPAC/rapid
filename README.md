# RAPID

Repository for the RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team.

[![Documentation Status](https://readthedocs.org/projects/roman-rapid/badge/?version=latest)](https://roman-rapid.readthedocs.io/en/latest/)

## Getting started

Two worked examples, in plain commands, each ending in a result you can check: [a local test](#a-local-test) runs one epoch through the whole chain on your own machine against a throwaway database, and [a flight program on SMDC](#a-flight-program-on-smdc) runs one on the team's database and Batch, then stops and cleans up after it. Both use `scripts/dev.sh`, which runs `rapidpipe` from this checkout inside a container of the team's pipeline image, so nothing is installed and nothing is looked up by hand.

### What you need

- A RAPID workstation (an SMDC login and `rapid_systems`' README, Setup, get you one), with `podman` (or `docker` under the same commands) and this checkout, your shell at its top. Its instance role carries the image pull, the reads of the public buckets and the RuBR-AT model, and, for the flight example, the team's database and Batch.
- At least 16 GB of memory (the difference peaked at 15.2 GB) and 3 GB of disk beside the image.
- Network access to the public bucket `s3://stpubdata` (read unsigned) and to `https://roman-crds.stsci.edu` (the PSF stage's reference).

### A local test

One epoch of the HLWAS simulation (pointing `001_0001`, SCA 8, F146, pass 4) against a reference built from passes 1 and 2: the loop runs the PSF, three intakes, the reference, the difference and the epoch's packet set here, one after another, with no Batch. It took five and a half minutes on a workstation with the image already pulled (2026-11-20). The work folder is `rapid-work/` beside this checkout (`RAPID_DEV_WORK` moves it), seen as `/work` inside the container; the run is the database's first, so its id is 1.

```sh
scripts/dev.sh image pull
```

Logs in to the team's registry, pulls the newest `rebuild-` image and remembers it for the commands below; it prints the image's name.

```sh
scripts/dev.sh db start
```

Starts a throwaway PostgreSQL 18 beside the containers with this checkout's `database/schema.sql` applied (one NOTICE: `postgres:18` lacks q3c). The local target, every command's default below, points the pipeline at it, writes products under the work folder's `out/`, and points a packet job's Kafka send at a closed local port, so nothing reaches the team's topic.

```sh
scripts/dev.sh run -- python -m rapidpipe.science.catalogs.store ingest-gaia --dest /work/gaia --leaves 2/138 --jobs 1
```

Builds a local Gaia DR3 mirror of the one leaf the field is on, order-2 pixel 138 (a 212 MB download from `https://data.lsdb.io/hats/gaia_dr3/gaia/`), in the work folder. It prints its record, `"leaves_done": 1` and `"complete": false`: it is one leaf of 2,016, so it is registered below as partial, which the team's mirror never is.

```sh
scripts/dev.sh run -- rapidpipe run create --why "local test" \
  --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set 'intake.source_root="s3://stpubdata/roman/nexus/soc_simulations/r00342/l2/"' \
  --set reference.selection.before_mjd=61591.4056 \
  --set packet.ned_match=false --set packet.lvs_match=false --set packet.ss_match=false \
  --survey HLWAS --dataset r00342-local
```

Records the run, the settings resolved once from the shipped base, the HLWAS overlay and the `--set` terms, and prints its id, `1`. The source root and the reference bound (F146's, between its third and fourth passes) are the two keys the base leaves absent. The three matches switched off are NED, NED-LVS and the solar-system predictions, none of which is built locally, so the packets carry Gaia matches and null NED and solar-system fields. `--who` is your login name and `--revision` this checkout's commit, both by default.

```sh
cat > ../rapid-work/live.json <<'END'
[{"open": {"survey": "HLWAS", "dataset": "r00342-local", "run": 1}}]
END
scripts/dev.sh run -- rapidpipe official change --group /work/live.json --why "run 1 live"
```

Makes run 1 the live run of its dataset, so each job's products become official as it publishes. It prints the change group's id, `1`.

```sh
scripts/dev.sh run -- rapidpipe job local --run 1 --stage import \
  --scope '{"gaia": {"name": "gaia", "version": "DR3", "prefix": "/work/gaia", "partial": true}}' \
  --expects '[{"product": "catalogs", "key": {"dataset": "r00342-local", "name": "gaia", "version": "DR3"}, "binding": null}]'
```

Registers the local mirror as the dataset's Gaia catalog, which the intake and the packet stage read. It prints the job's result, `"published": true` and `"refusal": null`.

```sh
scripts/dev.sh run -- rapidpipe loop --run 1 --local --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,4
```

Runs the selection to the end here: each pass prints one JSON line and runs the jobs it planned, in stage order, until a pass finds nothing left, and exits 0. Passes 1 and 2 are before the bound, so they are the reference's frames; pass 4 is differenced and packeted. The packet job's send is refused on stderr, `packet set <job>: not produced (KafkaTimeoutError: ... Unable to bootstrap from ['127.0.0.1:9']); left unstamped`, with its traceback, then `job <job> (packet) published`: the redirect working. The last pass's line reads `"drained": true`.

```sh
scripts/dev.sh run -- python scripts/validate_run.py --run 1
scripts/dev.sh run -- python scripts/read_packets.py
```

The result: `validate_run.py` ends `run 1: 6/6 checks PASS`, and `read_packets.py` reads the official packet set back and prints `Packet reader: 1 set(s), CLEAN`, both exiting 0. The products are under `../rapid-work/out/1/<stage>/<job>/1/`; `scripts/dev.sh run -- rapidpipe run show 1` prints the run and its jobs, and `scripts/dev.sh run -- rapidpipe job show <job>` one job's inputs and bindings.

```sh
scripts/dev.sh db stop
```

Removes the throwaway database, everything in it, and the pod it ran in. `../rapid-work/` stays until you delete it; a second test needs `db start` and the commands above again, or, on the same database, a dataset name of its own (`--dataset r00342-local2` in each command), since a dataset has one live run.

### A flight program on SMDC

One night of program 1049 (four F158 WFI02 epochs, observation 2 visit 1) on the team's database and Batch, under a dataset of your own: `flight` itself is one dataset, the team's, whose live run admits new files and is changed only on purpose ([datasets](https://roman-rapid.readthedocs.io/en/latest/system/datasets.html)). It differs from the local test in four ways: `--target deployed`, so the container reaches the team's database under the workstation's role; the jobs run on Batch, from the image, not from this checkout; the catalogs are the team's copies; and the run is stopped and cleaned up at the end. The values in angle brackets are the team's bucket and queue names, which this repository does not carry (it names no account, bucket or host): the scratch bucket, the Batch queue and the catalog copies' prefixes are in step 1 of [Starting a night](https://roman-rapid.readthedocs.io/en/latest/system/loop.html#starting-a-night), the inputs bucket on the [datasets page](https://roman-rapid.readthedocs.io/en/latest/system/datasets.html).

```sh
scripts/dev.sh image pull
scripts/dev.sh image rev
scripts/dev.sh sync <the commit image rev printed>
```

Pulls the newest image, prints the commit and branches it was built from, and puts this checkout at that commit. A Batch job runs the image's commit, so with the checkout there too the run records that commit as its revision by itself; at any other commit `run create` refuses until you name `--revision`. This example needs an image built from `rebuild` after the serial exposure id and this command-line pass landed: at an older commit `--exposures`, `loop --dry-run`, `run stop` and `run cleanup` do not exist, so if `image rev` names no such commit, stop here until one is registered.

```sh
export RAPIDPIPE_OUTPUTS_ROOT=s3://<the scratch bucket>/rapidpipe/<your login>
export RAPIDPIPE_BATCH_JOB_QUEUE=<the prompt queue>
```

Where the run's products go and the Batch queue the loop submits to; the deployed target passes both into the container. Put them in your shell profile once.

```sh
scripts/dev.sh run --target deployed -- rapidpipe run create --why "program 1049 trial" \
  --overlay rapidpipe/settings/datasets/flight.toml \
  --set 'intake.source_root="s3://<the inputs bucket>/flight-1049/"' \
  --set reference.selection.before_mjd=61337.4594 \
  --survey <the survey> --dataset flight-1049-<your initials>
```

Records the run under the flight overlay and prints its id, called `N` below. Every value is yours to choose but the overlay: the source root is the prefix `scripts/stream_l2_files.py 1049` filled ([The command line](#the-command-line)); the bound decides which epochs build the reference (before it) and which are differenced (at or after it), and 61337.4594 is program 1049's, the first three epochs building the reference for the fourth ([datasets](https://roman-rapid.readthedocs.io/en/latest/system/datasets.html)); the survey is the name the live binding is kept under with the dataset (whether `run create` should require one is an open question).

```sh
cat > ../rapid-work/live-N.json <<'END'
[{"open": {"survey": "<the survey>", "dataset": "flight-1049-<your initials>", "run": N}}]
END
scripts/dev.sh run --target deployed -- rapidpipe official change --group /work/live-N.json --why "run N live"
```

Makes run `N` the live run of your dataset; it prints the change group's id.

```sh
scripts/dev.sh run --target deployed -- rapidpipe job local --run N --stage import \
  --scope '{"gaia": {"version": "DR3", "prefix": "<the Gaia mirror>"}, "ned": {"version": "36.1_20260527_v2", "prefix": "<the NED copy>"}, "lvs": {"version": "20260424", "prefix": "<the NED copy>"}, "ss": {"version": "rolling", "prefix": "<the ss root>"}}' \
  --expects '[{"product": "catalogs", "key": {"dataset": "flight-1049-<your initials>", "name": "gaia", "version": "DR3"}, "binding": null}, {"product": "catalogs", "key": {"dataset": "flight-1049-<your initials>", "name": "ned", "version": "36.1_20260527_v2"}, "binding": null}, {"product": "catalogs", "key": {"dataset": "flight-1049-<your initials>", "name": "lvs", "version": "20260424"}, "binding": null}, {"product": "catalogs", "key": {"dataset": "flight-1049-<your initials>", "name": "ss", "version": "rolling"}, "binding": null}]'
```

Registers the team's four catalog copies under your dataset, once; it names each copy's prefix and copies nothing. It prints `"published": true`. Without a copy whose `[packet] <name>_match` is on, the loop stops with exit 64 when the first packet job is due, naming the missing one.

```sh
scripts/dev.sh run --target deployed -- rapidpipe loop --run N --programs 1049 --dry-run
```

Prints the first pass's plan as JSON and does nothing: it submits nothing, records nothing and assigns no exposure id. On a fresh run the plan is one `psf` submission and `"new"`, the count of files the selection admits that no intake has taken in (4 here); the intakes are planned the pass after the PSF is published, so a dry run then lists them, and a new exposure under `unassigned`. Read it before spending Batch time; a selection that admits no file is refused, naming the term that admits none.

```sh
scripts/dev.sh run --target deployed -- rapidpipe loop --run N --programs 1049 --job-definition rapid-rebuild
```

Submits the selection one stage forward per pass on Batch and prints one JSON line a pass, every two minutes, until a pass finds it drained: exit 0, or 1 when a job failed, named on stderr (`rapidpipe job show <id>` gives its `refusal`). `rapid-rebuild` without a revision is the newest revision of the scratch job definition, which the release registers for the newest image; `scripts/dev.sh image rev` and the `revision` that `rapidpipe run show N` prints should name the same commit.

```sh
scripts/dev.sh run --target deployed -- scripts/check_run.sh --run N
```

The result: one line, `Check run N: CLEAN validate 6/6 <s> s reader M/M <s> s consumer M/M <s> s` (each check followed by its seconds), and exit 0. Any failure prints `FAIL` and exits non-zero.

```sh
scripts/dev.sh run --target deployed -- rapidpipe run stop --run N --why "trial checked"
```

Records the run as stopped (who, when, why), so no further job of it is admitted on any path (the loop, `job local`, `job submit`, `job run`), and asks Batch to end any job of it still queued or running. It prints what it did as JSON. Stop a run that is going wrong the same way, at any time.

```sh
scripts/dev.sh run --target deployed -- rapidpipe run cleanup --run N
```

Lists, as JSON with file counts, the run's attempt directories that hold no published product (failed attempts, and published jobs' other attempts), and removes nothing; published and official products are counted and never listed. It refuses, naming the job, while any job is not shown to have ended. `--delete` removes exactly that list; on S3 it needs `s3:DeleteObject` under the outputs root, which the workstation roles do not hold today, so ask for the removal until they do.

A trial's live binding is closed once it is checked, so the live list names only what is running: `rapidpipe run show N` prints its id as `"live"`, and a group file `[{"close": <that id>}]` given to `official change` closes it.

### Taking a run's products out of official

`rapidpipe official undo <group> --why Y` applies the inverse of one change group: it closes every binding the group opened and opens again, as new rows, every binding it closed. A run's products became official in many groups, one per published job (the `"group"` each job's result printed, and the `opened_group` of each binding `rapidpipe job show <job>` lists), plus the group that made it live. To retract a run, undo those groups, the newest first; a group whose binding has since moved (a later run superseded it) is refused, naming that binding and the change group that closed it, which is undone first if the later run's binding is to go too; otherwise the later run's binding stands.

What it leaves in place: every row the run's jobs wrote (the `*_official` views stop showing the retracted ones, since they read through the open bindings), every file under the outputs root, the jobs and their attempts, and the closed bindings themselves, with who closed them, when and why. Work of other runs built on them stays as it is: a reference another run reused, an object a later difference joined. A packet already sent is not recalled: the packets are on the Kafka topic for its retention and consumers may have read them. `official undo` of the undo puts the products back.

### Working on a branch in a container

The team's images are in the SMDC account's private registry, repository `rapid-pipeline`, tagged `rebuild-<sha>-<base8>` for each commit of `rebuild` built for Batch (the full sha, then the first eight hex of the base image's digest, so one commit built on two bases has two tags); the image's commit is its `org.opencontainers.image.revision` label, which `scripts/dev.sh image rev` prints. You can build one yourself ([`containers/README.md`](containers/README.md)). The same workstation carries the rapid Python environment at `/opt/rapid/conda/envs/rapid` (`rapid_systems`' README, Python environments).

To run a branch of this checkout, its tests included, in a container of the team's image on your workstation, without building an image: `rapid dev <command>` from your laptop (`rapid_systems`' `bin/rapid`), which syncs the workstation's checkout to the commit your laptop's checkout is on and runs `scripts/dev.sh <command>` there. The commands are dev.sh's (`scripts/dev.sh --help`, or `rapid dev --help`). The container mounts the checkout at `/code` ahead of the `rapidpipe` installed in the image (`PYTHONPATH`), so `rapidpipe`, `python -m pytest` and the stages run the checkout's code on the image's dependencies and tools, and `work/` at `/work`. A branch that changes `requirements.txt` needs a rebuilt image, since the image's libraries are the ones that run. The target is what the container runs against:

- **`--target local`** (the default): a throwaway PostgreSQL 18 beside the container with `database/schema.sql` applied, `PG*` set to it, products under the work folder's `out/`. For the test suites and scratch runs. The work folder is yours, mounted at `/work`: inputs you hand the container and what you take out, with `out/` inside it as the pipeline's outputs root. It is `RAPID_DEV_WORK`, by default `rapid-work/` beside the checkout; set it once in your shell profile to put it elsewhere (`work/` inside the checkout is git-ignored).
- **`--target deployed`**: `RAPID_PARAMETER_PATH=/rapid/rebuild`, so the container reaches the deployed database under the workstation's instance role (`rapid_systems`' `docs/workstation-submission.md`), with `RAPIDPIPE_OUTPUTS_ROOT` from your environment. A run there is a real run: use a dataset of your own.

```sh
git push                                           # the workstation is synced to this commit
rapid dev image pull                               # the newest rebuild image; `image pull TAG` for another
rapid dev db start                                 # the throwaway database (stop, reset, status)
rapid dev shell                                    # bash in a container, this checkout at /code
rapid dev run -- python -m pytest tests/unit -q    # one command instead of a shell
rapid dev run -- python -m pytest tests/db tests/cli -q
rapid dev run --target deployed -- rapidpipe run show 7
rapid dev --ref rebuild shell                      # the integration branch instead of your commit
```

On the workstation itself the same commands are `scripts/dev.sh image pull`, `scripts/dev.sh db start` and so on, with `scripts/dev.sh sync REF` as the sync `rapid dev` does first. The image carries no git: commit from the workstation, not the container. For a VS Code session in the same container, open the checkout over Remote-SSH (`rapid vscode`), export `RAPID_DEV_IMAGE=$(scripts/dev.sh image pull)` in your login shell, and "Reopen in Container" with `.devcontainer/rebuild-local` (starts the throwaway database itself) or `.devcontainer/rebuild-deployed`.

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time, never committed. Reference bulk data artifacts (product listings, large generated files) with the command to reproduce them instead of committing them.

The [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html) is the design authority for repository boundaries and stage interfaces. [AGENTS.md](AGENTS.md) presents the same rules for coding agents, plus the package map and test/CI conventions.

### Layout

`rapidpipe/` is the pipeline package; `database/` holds the schema (`schema.sql`) and the sky-tessellation helpers; `modules/` and `cdf/` hold the `dev` helpers, scripts and configuration files the stages use; `containers/` holds the image recipe; `tests/`, `scripts/` (public-safety and science-drift checks), `docs/` and `c/` (vendored C tool source) complete the tree. [AGENTS.md](AGENTS.md) has the full layout table.

## Package

The distribution is `rapid-pipeline`; its import package is `rapidpipe`. The foundation plan (v5, 2026-10-20) is the design authority for its database side: one schema file, runs and jobs, the publisher, official bindings changed in recorded groups, and the verbs below. `rapidpipe.foundation` holds those operations over [`database/schema.sql`](database/schema.sql); `rapidpipe.db` holds the connection, which gets database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a committed file.

Every stage runs under a job (`job local`, `job run`, below): one contract, `rapidpipe.foundation.JobContext`.

## Container

[`containers/rapid-pipeline`](containers/rapid-pipeline) holds the recipe that builds `rapidpipe` into a runnable image over a base environment supplying its dependencies. Which base image that is and where the built image is published are decided and owned outside this repository, in `rapid_systems`. See [`containers/README.md`](containers/README.md) for how to build the recipe locally.

## The command line

The database is built by `psql -v ON_ERROR_STOP=1 -f database/schema.sql` on an empty PostgreSQL 18 ([`database/README.md`](database/README.md)). The verbs:

- `rapidpipe run create [--who W] --why Y [--revision REV] [--settings FILE] [--overlay DATASET.toml] [--set TABLE.key=VALUE ...] --survey S --dataset D` records a run (a recipe someone launched for a reason) and prints its id. `--who` is your login name when left out, here, on `run stop` and on the two `official` verbs (never `root`, which names nobody: say who then). `--revision` is `RAPID_SOURCE_REVISION`, the commit of the code running, where the environment sets it: the image does, to the commit it was built from, and `scripts/dev.sh` does when its checkout and its image leave one answer; where it is unset the run names its revision itself. The body it stores is resolved here, once, from three layers: `FILE` (default the shipped `rapidpipe/settings/foundation-thin.toml`, dev's values; TOML, or JSON when its name ends in `.json`), the dataset's overlay (`rapidpipe/settings/datasets/`, only the keys that differ, each with the measurement that earned it), then each `--set` in order (a TOML value, strings quoted). A key the base lacks is refused, naming it, except the two the base documents as absent by default (`intake.source_root`, `reference.selection.before_mjd`). The run also stores the body's hash and each layer's name and SHA-256 (`runs.layers`), which `run show` prints.
- `rapidpipe official change --group FILE [--who W] --why Y` applies one change group in one transaction and prints its id. `FILE` is a JSON list of items, each with `close` (the id of an open binding) and/or `open` (a product binding `{"product", "key", "job"}` or the live binding `{"survey", "dataset", "run"}`).
- `rapidpipe official undo <group-id> [--who W] --why Y` applies the inverse of a change group (close what it opened, open as new rows what it closed) and prints the new group's id.
- `rapidpipe run show <id>` prints the run, its live binding and its jobs as JSON. `rapidpipe run show <id> --toml FILE` writes the run's stored settings body to `FILE` as TOML instead: a base that `rapidpipe run create --settings FILE` stores as the same body, with the same `settings_hash`, so a run's settings can be read, compared with another's, or used again without retyping its overlay and `--set` terms.
- `rapidpipe run stop --run N [--who W] --why Y` stops a run: it records who, when and why on the run, marks `failed` the jobs recorded and not yet submitted, and asks Batch to cancel or terminate every job of the run that has not ended, printing what it did as JSON. From then on the run takes no job: `loop`, `job local`, `job submit` and `job run` refuse it (exit 1), saying who stopped it and why, and a container Batch starts after the stop does no work. A submission in flight when it is called is waited for and ended with the rest. A stopped run is not started again; a new run takes the work on. It may be repeated, to ask Batch again.
- `rapidpipe run cleanup --run N [--delete]` is for a stopped run whose every job has ended: it prints, as JSON, the attempt directories under `RAPIDPIPE_OUTPUTS_ROOT` that belong to the run's jobs and hold no published product (a failed job's attempts, and a published job's attempts other than the one it published), with their file counts, and removes nothing. Published attempts, which hold every product a row or an official binding names, are counted and never listed; files under the run's prefix that are no job's attempt are counted as unrecognised and never removed. With `--delete` it removes exactly the listed files, checking each attempt again as it comes to it; one published meanwhile is kept and named, exit 1. It refuses, naming the job and what would end it, while any job is not shown to have ended: a row still `launched` or `running`, a Batch job Batch reports active, or one with no recorded end that Batch no longer describes (Batch forgets a job about a week after it ends, so list a stopped run within the week). On S3 the removal needs `s3:DeleteObject` under the run's prefix.
- `rapidpipe job show <id>` prints the job, its recorded inputs and every binding of its rows as JSON.
- `rapidpipe job wait --job N [N ...] [--timeout S] [--interval S]` blocks until every named job is `published` or `failed`, reconciling their runs with Batch at each check (every 30 s by default, giving up after `--timeout`, 7,000 s by default), and prints their states: exit 0 when every job published, 1 when one failed, 2 when the timeout came first. It is the one waiter for jobs submitted by hand.
- `rapidpipe job local --run R --stage NAME --scope JSON [--inputs JSON] [--expects JSON]` records a job under run `R` with its inputs (the lists `rapidpipe.foundation.create_job` takes) and runs it here; `rapidpipe job run --job J` runs an existing job's next attempt. Running a job reads it first and stops at once if it is published; otherwise it calls the stage's `run(job)` in this process with a `rapidpipe.foundation.JobContext` (job, run, scope, the run's settings body, the recorded inputs, the attempt's prefix `<RAPIDPIPE_OUTPUTS_ROOT>/<run>/<stage>/<job>/<attempt>/`), then publishes the attempt and prints the publisher's result as JSON. The stage writes its files and `manifest.json` and never publishes. The outputs root is a `file://` or `s3://` URL; under `s3://` the stage writes a local working directory that is uploaded, manifest last, before publishing. `rapidpipe job submit` takes `job local`'s arguments, or `--job J` in their place for a new attempt of an existing job, and `--job-definition NAME` always (`--job-queue Q` names another queue for this one submission), records the job and submits one Batch job on `RAPIDPIPE_BATCH_JOB_QUEUE` whose command is `job run --job J` in the image, passing `RAPIDPIPE_OUTPUTS_ROOT` (`s3://` only) and `RAPID_DB_SECRET_ID` when set. An exposure's `expid` is the integer `exposure_ids` holds for the exposure's name in its dataset (`rapidpipe exposure id --dataset D --name N` prints it, assigning one when the dataset has none; the loop resolves it for every file it takes in), so an `import` of an epoch and a hand-run `intake` take their scope's `expid` from that verb. The foundation's stages are `import` (`rapidpipe.stages.import_products`) and `packet` (scope `{"expid", "sca", "fid"}`; inputs: the epoch's source set, the science `psfs`, the Gaia `catalogs` product, the `refimages`; it reads the triggers' objects and members (the xobjects and xmemberships of the set's xsources, under the shipped `[packet] alert_catalog = "xsources"`), their exposures, the forced window's `diffimages` and `l2files` and the official `packets` sets' forced rows as they stand at its read and records them; outputs Avro 00.08 packets and the `packets`, `forced` and `external` tables) and `reference` (scope `{"field", "fid", "sca"}`; inputs: the `l2files` to coadd; outputs the mosaic, its coverage and uncertainty maps and SExtractor catalog, and the `refimages`, `refimimages`, `refimcatalogs` and `refimmeta` tables), with `intake`, `psf` and `difference` beside them ([Getting started](#getting-started) runs all six).

`python scripts/read_packets.py [--job N ...] [--out FILE]` reads published packet sets back (the Avro, `packets_official`, `forced_official`) and writes a validation report, exiting non-zero on any failure; a packet job sends its own official set to the Kafka topic its run's `[kafka]` table names as its last act, one message per packet keyed by the dataset with the set and sid in its `rapid-set` and `rapid-sid` headers, stamping `jobs.kafka_published_at` and the partition and first and last offsets after every acknowledgement; `rapidpipe publish --run N [--settings FILE]` sends the run's official sets not yet stamped (the re-publication verb and the loop's recovery sweep), and `python scripts/consume_packets.py --run N` reads each set's recorded range back, counts its packets and compares the bytes of one set per field with the outbox; `scripts/check_run.sh --run N [--settings F]` runs a run's three checks (`validate_run.py`, `read_packets.py` over its bound packet sets eight processes at a time, `consume_packets.py`) and prints one line, `Check run N: CLEAN|FAIL validate ... reader ... consumer ...` with each one's count and seconds, exiting non-zero on any failure; `rapidpipe loop --run N [--filters F146] [--scas 8] [--pointings VVV_EEEE,...] [--passes P,...] [--exposures PREFIX,...] [--programs P,...] [--stems STEM,...] [--mjd LO-HI | --dates LO..HI] (--job-definition D [--job-queue Q] | --local | --dry-run) [--stage NAME] [--once] [--max-intake N] [--interval S]` (every selection term `all` when left out, `--dates` and `--mjd` included; each term's `--help` says whether it bounds the files taken in, the official L2 rows, or both, and a first pass that admits no file is refused naming the term that admits none) lists the run's `[intake] source_root`, submits every ready job of the selection one stage forward on Batch (intake, reference, difference, which associates its own sources and xsources with the objects and xobjects of their fields as they stand, then a packet per official source set, in any order) and ends each pass with `publish`, printing one summary line per pass and repeating every `--interval` seconds (120 by default) until a pass finds the selection drained (exit 0) or a job of the run has failed or was published with its binding refused (exit 1); `loop --local` runs each planned job here instead, as `job local` runs one, with no Batch and no job definition, passes repeating at once; `loop --dry-run` prints one pass's plan as JSON and does nothing; `--stage NAME` keeps one stage's jobs and implies `--once` ([the loop page](https://roman-rapid.readthedocs.io/en/latest/system/loop.html)); `database/queries.sql` holds the read queries the official views answer.

`python scripts/stream_l2_files.py PROGRAM --source-root s3://<bucket>/<prefix>/` copies a program's guided WFI L2 files from MAST to `<prefix><stem>_cal.asdf`, outside the pipeline and before any run (`--filter`, `--detector`, `--pass`, `--segment`, `--visit`, `--mjdmin` and `--mjdmax` cut the search, `--unguided` keeps unguided exposures, `--dryrun` copies nothing, `--workers N` copies N at a time; each file is one conditional PUT, so nothing under the root is overwritten); it reads the MAST token from `MAST_API_TOKEN`. The loop reads the same prefix as the run's `intake.source_root`, so one shell variable connects them:

```sh
SRC=s3://<bucket>/<prefix>/
python scripts/stream_l2_files.py 1049 --source-root "$SRC"
rapidpipe run create ... --set "intake.source_root=\"$SRC\""
```

A change group refused because a binding it names is no longer open, or because a key it opens already has an open binding, and a job published but refused its automatic binding, exit 1 with the binding named on stderr; a run, job or group that does not exist, or a file that cannot be read, exits 64.

## Running a stage under a job

`rapidpipe.launch` starts a job's stage once its job row exists (`rapidpipe.foundation.create_job`): `submit` sends `job run --job <id>` to AWS Batch under the job name `<prefix>-<stage>-<job>`. Batch retries under the job definition's retry strategy; nothing here retries. The job definition is the `--job-definition` argument of `job submit` and `loop`; the rest comes from the environment:

- `RAPIDPIPE_BATCH_JOB_QUEUE` -- the Batch job queue name or ARN.
- `RAPIDPIPE_BATCH_JOB_NAME_PREFIX` -- optional, default `rapid`.

### Environment read inside the container

`rapidpipe` reads these directly from the process environment, none committed here:

- `RAPID_PARAMETER_PATH` -- the SSM parameter tree `rapidpipe.db.connection` reads for its `db/server`, `db/port`, `db/name` and `db/secret-id` keys when the `PG*` variables are not set; set by the job definition.
- `RAPID_IMAGE_DIGEST` -- the image digest a job's row records (`jobs.image_digest`) when its attempt starts.

## Logging and profiling

Every line `rapidpipe` logs has the shape `<ts>Z <LEVEL> run=<r> attempt=<a> stage=<s> unit=<u> <logger> <message>`, with a UTC, millisecond-precision timestamp. `run`/`attempt`/`stage`/`unit` are `-` where not bound, as for the CLI's own messages.

Per the repository's Unix convention, commands whose stdout carries data log to stderr. Stage logs and the CLI's own library-warning lines go to stderr; stdout carries only data (`print`).

- `RAPIDPIPE_LOG_LEVEL` -- optional: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, overriding the default level. The CLI logs `rapidpipe`'s own lines at `INFO` and every other library's at `WARNING` (library warnings that would otherwise reach Python's last-resort handler with no timestamp).

## Documentation

The documentation is on [Read the Docs](https://roman-rapid.readthedocs.io/en/latest/), built from the `rapid_docs` repository. `dev`'s Sphinx pages stay on `dev`.

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
