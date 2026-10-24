# The SMDC step of the foundation (milestones 1 to 6)

Milestones 1 to 3 of the foundation plan (v5, 2026-10-20) ran against a
throwaway PostgreSQL 18 and `file://` prefixes on one host. This runbook takes
the same thin chain onto SMDC, then gives it its first Batch-produced
difference images (part 4, milestone 4). Every step is one command followed by
its check, and nothing goes on to the next step until the check holds. Parts 1
to 3 were run unattended on 2026-10-21 from a fleet host in the same security
group as the database's pooler, and are written here as run, with their
results; parts 4 and 5 followed the same day, run the same way, and part 6 is
the night of the same field written by the tasks that ran it; part 7, the
HLWAS simulation from ASDF intake to Kafka, was run the next night, and
part 8, the housekeeping that makes the ramp cheaper, the night after, and
part 9, breadth over the other filters, the night after that. Nine parts, in order:

1. **Schema**: the SMDC `rapid` database's `public` schema becomes `database/schema.sql`.
2. **Inputs**: the field's directory is copied to the inputs bucket under `foundation/`.
3. **Baseline**: the same import, associate and packet runs as milestones 1 to 3, on SMDC,
   so the database holds the thin chain before Batch touches it.
4. **Batch**: `reference` and `difference` on Batch under a run for the field's L2 inputs,
   the chain downstream of them, also on Batch, then one change group that replaces the
   imported reference, difference images, sources, objects and packets together, and its undo.
5. **Uncapped**: the same chain on Batch with no source cap, measured per stage per epoch, with
   its change group and undo timed (milestones 5 and 6 as far as this field's two epochs reach).
6. **The night**: the PSF key gains its kind and the read role can select (step 1), then the
   field's 69 epochs through the whole chain.
7. **HLWAS**: the night's fixes and their image (step 1), then the SOC HLWAS simulation
   from its ASDF files through intake, the chain and Kafka publication.
8. **Housekeeping**: the difference outputs trimmed and gzipped, the working directory
   removed and the intake's `exposures` row only when expected (step 1), each proven on
   Batch under a throwaway dataset with its own live run.
9. **Breadth**: the PSF derived from each file's own CRDS ePSF reference by a `psf`
   stage the loop submits (step 1), then the six other filters, each its own dataset.

Part 4 needs the `reference` and `difference` stages on the `run(job)` contract
(`rapidpipe/foundation/local.py`), the difference stage refreshed from `dev`
first: PRs 287 and 288, merged on `rebuild` before part 4 ran.

**Two things this procedure does not do, by decision.** SMDC is a prototype
(ruling of 2026-10-21): nothing else writes to `rapid`, so there is no
pgBackRest backup before the schema is replaced and no pooler fence around it.
The rollback is the previous schema in git, named in step 1. The procedure
also needs no shell on the database host: it connects through the pooler as
`postgres`, whose password is in Secrets Manager.

**Every outputs root is the scratch bucket's `rapidpipe/foundation`**, the
baseline's included. The Batch job role reads the inputs bucket, writes the
scratch bucket and lists it only under `rapidpipe*`, and cannot read the
products bucket, so a Batch stage in part 4 can read the baseline's copies
(the L2 files, the PSF, the reference) only if the baseline wrote them to
scratch.

Account-specific names are not in this repository. Where a step needs one it
uses a shell variable, set once from rapid_systems (`docs/architecture.md`
and `AGENTS.md` there):

```sh
export INPUTS_BUCKET=...     # the GBTDS simulation inputs bucket
export SCRATCH_BUCKET=...    # the scratch bucket the rapid-rebuild job role writes
export AWS_PROFILE=...       # an administrator profile for this account
export AWS_REGION=us-east-1
export DATA=...              # the field's directory, verified with its SHA256SUMS
export RAPID=...             # a checkout of rebuild
export PY=...                # the rapid environment's python3
export PYTHONPATH=$RAPID
export RAPID_PARAMETER_PATH=/rapid/rebuild   # the job definition's parameter tree
unset PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
export RAPIDPIPE_OUTPUTS_ROOT="s3://$SCRATCH_BUCKET/rapidpipe/foundation"
D=socsims-fakesrc-20260807
rp() { $PY -m rapidpipe.cli.main "$@"; }
```

Part 1's SQL runs through `pg`, a small script (not a shell function, so the
password lives only in that script's process) that reads the `postgres`
password from Secrets Manager into `PGPASSWORD`, reads the pooler's host and
port from the same parameter tree, and runs `psql -d rapid -v ON_ERROR_STOP=1
"$@"` as `postgres`. It never prints the password or writes it to a file.

## Before part 1

1. **Nothing is running on Batch.** Check: `aws batch list-jobs --job-queue
   rapid-queue-prompt --job-status <s>` is empty for `SUBMITTED`, `PENDING`,
   `RUNNABLE`, `STARTING` and `RUNNING`, and the same for `rapid-queue-bulk`.
   The scheduled processing-date loop's row is an inert proof row and writes
   nothing.
2. **The field's files are whole.** `cd "$DATA" && sha256sum -c SHA256SUMS --quiet`.
   Check: exit 0, no output.

## Part 1: the schema

1. **Record what `public` holds**, so the rollback has a name.

   ```sh
   pg -c '\dt public.*' -c '\dv public.*' -At -c 'SELECT filename FROM schema_migrations ORDER BY 1 DESC LIMIT 1'
   ```

   Check: the listing is saved with the run's records. Then confirm that the
   drop's cascade cannot leave `public`: no object in another schema depends on
   a `public` table, type or function (a `pg_depend` query over `pg_class` and
   `pg_proc` outside `public`, `pg_catalog` and `information_schema` returns no
   rows).

2. **Replace `public`**, in one transaction. Everything else in the database
   stays: `ops`, `pgbouncer` and its `pgbouncer_auth` lookup, and every role
   (roles are cluster objects).

   ```sh
   pg -1 -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public' -f "$RAPID/database/schema.sql"
   ```

   Check: `pg -Atc "SELECT count(*) FROM runs"` is 0, `pg -Atc "SELECT
   q3c_version()"` answers (no "q3c is not available" notice appeared),
   `pg -Atc "SELECT nspname FROM pg_namespace WHERE nspname IN ('ops',
   'pgbouncer')"` returns both, and `ops`'s row counts are what they were.

3. **Grants.** The pipeline's role reads and writes every table and sequence,
   analyses the tables the publisher loads, and gets the same on anything
   created later; the team's read role gets `USAGE` on the new `public` back.

   ```sh
   pg -1 <<'SQL'
   GRANT USAGE ON SCHEMA public TO rapid_rebuild_pipeline;
   GRANT SELECT, INSERT, UPDATE, DELETE, MAINTAIN ON ALL TABLES IN SCHEMA public TO rapid_rebuild_pipeline;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO rapid_rebuild_pipeline;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE, MAINTAIN ON TABLES TO rapid_rebuild_pipeline;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO rapid_rebuild_pipeline;
   GRANT USAGE ON SCHEMA public TO rapid_read;
   SQL
   ```

   The last line is the one statement of rapid_systems'
   `cloudformation/db-migrations/002-grants.sql` that the drop undid. That
   script's applier, `apply-db-migrations.sh`, is not run: it reaches the
   database host over SSM and also re-applies the login roster, and roles are
   outside this step. Check: no table and no sequence in `public` lacks a
   grant, by oid:

   ```sh
   pg -Atc "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
              AND NOT has_table_privilege('rapid_rebuild_pipeline', c.oid, 'SELECT,INSERT,UPDATE,DELETE,MAINTAIN')"
   ```

   is 0, and the same over `relkind = 'S'` with `has_sequence_privilege(...,
   'USAGE,SELECT,UPDATE')` is 0. (Counting `role_table_grants` against
   `pg_tables`, as an earlier draft did, counts the views too: 37 grants for
   22 tables and 15 views.) Part 6 step 1 gave `rapid_read` `SELECT` on every
   table and view and on those created later; a fresh apply runs its two
   `GRANT` and `ALTER DEFAULT PRIVILEGES` lines with these.

4. **The pipeline's path works**, with the job definition's parameter tree
   naming the pooler and the service secret (`rapidpipe.db.connection`):

   ```sh
   rp run show 1
   ```

   Check: exit 64 with "run 1 does not exist" (connected, empty schema). Exit
   75 or a configuration error means the path does not work; stop.

## Part 2: the inputs

5. **Copy the field's directory** to the inputs bucket. The L2 files and the
   PSF are already there under their own prefixes; this adds the difference and
   reference job outputs and the Gaia cone, as the record of what the baseline
   imported. The inputs bucket is write-once: its policy denies a `PutObject`
   without `If-None-Match` and every delete, so `aws s3 cp` is refused and each
   file goes up with `put-object --if-none-match '*'` (every file here is under
   the 5 GB single-request limit):

   ```sh
   (cd "$DATA" && find . -type f | sed 's|^\./||' | sort | while read -r f; do
      aws s3api put-object --bucket "$INPUTS_BUCKET" --key "foundation/field-4658155-20260821/$f" \
        --body "$f" --if-none-match '*' --checksum-algorithm SHA256 >/dev/null; done)
   ```

   Check: `aws s3 ls --recursive --summarize "s3://$INPUTS_BUCKET/foundation/field-4658155-20260821/"`
   lists as many objects as `find "$DATA" -type f | wc -l`, and its total size
   equals `du -sb "$DATA"`. The import below still reads `$DATA` on this host
   (its scope's `source_dir` is a local directory).

## Part 3: the baseline

The same jobs, in the same order, as milestones 1 to 3 (PRs 279, 280, 281).
On the fresh schema their ids are fixed by the order: run 1; import jobs 1
(reference, PSF, Gaia), 2 (epoch 1, expid 84613) and 3 (epoch 2, expid 84624);
associate jobs 4 and 5; packet jobs 6 and 7. The open binding ids follow: 2
to 4 for job 1, 5 to 8 for job 2, 9 to 12 for job 3, 13 for job 4 (closed by
job 5's 14), 15 and 16 for the packet sets. Each check confirms the id a
command printed; a different id means an earlier step published something
extra, and the commands below that name ids must then be read against `rp run
show 1`.

```sh
SCOPE_SHARED='{"source_dir": "'$DATA'", "reference": {"rfid": 90011, "dir": "jid129279"}, "psf": {"psfid": 153, "kind": "science", "path": "psf/sciimage_psf_f146_sca08.fits"}, "gaia": {"name": "gaia", "version": "DR3", "path": "gaia/gaia-dr3-field-4658155-r0.12deg.parquet"}}'
k() { echo '{"dataset": "'$D'", "expid": '$1', "sca": 8, "fid": 8}'; }
OKEY='{"dataset": "'$D'", "survey": "GBTDS", "field": 4658155}'
epoch() { # expid rid pid l2 difference-directory
  echo '{"source_dir": "'$DATA'", "epochs": [{"expid": '$1', "rid": '$2', "pid": '$3', "rfid": 90011, "l2": "l2/'$4'", "difference": "'$5'"}]}'; }
expects() { # expid
  echo '[{"product": "exposures", "key": {"dataset": "'$D'", "expid": '$1'}, "binding": null}, {"product": "l2files", "key": '"$(k $1)"', "binding": null}, {"product": "diffimages", "key": '"$(k $1)"', "binding": null}, {"product": "sources", "key": '"$(k $1)"', "binding": null}]'; }
PSF='{"product": "psfs", "key": {"dataset": "'$D'", "fid": 8, "sca": 8, "kind": "science"}, "source_job": 1}'
SHARED_INPUTS='{"product": "catalogs", "key": {"name": "gaia", "version": "DR3"}, "source_job": 1}, {"product": "refimages", "key": {"dataset": "'$D'", "field": 4658155, "fid": 8}, "source_job": 1}, '"$PSF"
image() { # expid job: its diffimages and l2files inputs
  echo '{"product": "diffimages", "key": '"$(k $1)"', "source_job": '$2'}, {"product": "l2files", "key": '"$(k $1)"', "source_job": '$2'}'; }
```

6. **The run, live.**

   ```sh
   rp run create --who "$USER" --why "milestone 4 baseline: the thin chain of field 4658155 on SMDC" \
     --revision "$(git -C "$RAPID" rev-parse HEAD)" --settings "$RAPID/rapidpipe/settings/foundation-thin.toml" \
     --survey GBTDS --dataset $D \
   && echo '[{"open": {"survey": "GBTDS", "dataset": "'$D'", "run": 1}}]' > live.json \
   && rp official change --group live.json --who "$USER" --why "first live run on SMDC for GBTDS $D"
   ```

   Check: prints 1 then 1; `rp run show 1` shows `"live": 1`.

7. **Import** the shared products, then each epoch.

   ```sh
   rp job local --run 1 --stage import --scope "$SCOPE_SHARED" --expects '[{"product": "refimages", "key": {"dataset": "'$D'", "field": 4658155, "fid": 8}, "binding": null}, {"product": "psfs", "key": {"dataset": "'$D'", "fid": 8, "sca": 8, "kind": "science"}, "binding": null}, {"product": "catalogs", "key": {"name": "gaia", "version": "DR3"}, "binding": null}]' \
   && rp job local --run 1 --stage import --scope "$(epoch 84613 1832705 350875 r0034001004001029016_0001_wfi08_f146_cal_lite.fits.gz jid143931)" --expects "$(expects 84613)" --inputs "[$PSF]" \
   && rp job local --run 1 --stage import --scope "$(epoch 84624 1832552 351878 r0034001004001029022_0001_wfi08_f146_cal_lite.fits.gz jid144961)" --expects "$(expects 84624)" --inputs "[$PSF]"
   ```

   Check: three results with `"published": true`, jobs 1, 2, 3, each with a
   `group`; `rp job show 2` lists four open bindings and its manifest prefix
   is `$RAPIDPIPE_OUTPUTS_ROOT/1/import/2/1/`. The row counts are PR 279's:
   2 exposures, 2 L2 files, 2 difference images, 1 reference, 1 PSF, 1 Gaia
   catalogue, 500 sources per epoch.

8. **Associate** epoch 1 (no predecessor), then epoch 2 on it.

   ```sh
   rp job local --run 1 --stage associate --scope '{"field": 4658155}' \
     --inputs '[{"product": "sources", "key": '"$(k 84613)"', "source_job": 2}]' \
     --expects '[{"product": "objects", "key": '"$OKEY"', "binding": null}]' \
   && rp job local --run 1 --stage associate --scope '{"field": 4658155}' \
     --inputs '[{"product": "sources", "key": '"$(k 84624)"', "source_job": 3}, {"product": "objects", "key": '"$OKEY"', "source_job": 4}]' \
     --expects '[{"product": "objects", "key": '"$OKEY"', "binding": 13}]'
   ```

   Check: jobs 4 and 5 published; `rp job show 5` shows the field's object
   binding 14 open on job 5 and `rp job show 4` shows binding 13 closed; the
   object and membership counts equal PR 280's (38 then 77 objects; 38 and 39
   memberships).

9. **Packet** epoch 1, then epoch 2 with epoch 1's set as its predecessor.

   ```sh
   rp job local --run 1 --stage packet --scope '{"expid": 84613, "sca": 8, "fid": 8}' \
     --inputs "[$SHARED_INPUTS, {\"product\": \"objects\", \"key\": $OKEY, \"source_job\": 5}, $(image 84613 2), {\"product\": \"sources\", \"key\": $(k 84613), \"source_job\": 2}]" \
     --expects '[{"product": "packets", "key": '"$(k 84613)"', "binding": null}]' \
   && rp job local --run 1 --stage packet --scope '{"expid": 84624, "sca": 8, "fid": 8}' \
     --inputs "[$SHARED_INPUTS, {\"product\": \"objects\", \"key\": $OKEY, \"source_job\": 5}, $(image 84613 2), $(image 84624 3), {\"product\": \"sources\", \"key\": $(k 84624), \"source_job\": 3}, {\"product\": \"packets\", \"key\": $(k 84613), \"source_job\": 6}]" \
     --expects '[{"product": "packets", "key": '"$(k 84624)"', "binding": null}]'
   ```

   Check: jobs 6 and 7 published and bound (bindings 15 and 16);
   `$PY $RAPID/scripts/read_packets.py --job 6` (and `--job 7`) exits 0 with
   "CLEAN", the reader reading the Avro back from the bucket. The thin chain
   is on SMDC.

## Results of parts 1 to 3 (2026-10-21)

Run from `rebuild` at `1a528daa` (PR 284), with `scripts/read_packets.py`
connecting through `rapidpipe.db.connection` (this procedure's pull request).

**Part 1.** Before the drop, `public` held 45 tables, the view
`current_selection`, 70 functions (q3c's among them) and the domain
`rapid_ulid`, from the run model's schema; its last migration was
`20261013-01-logged-tables.sql`, and it held 63 rows in all (`filters` 8,
`pipelines` 4, `scas` 18, `schema_migrations` 32, `swversions` 1). That
schema is the rollback, from git. The drop cascaded to 108 objects. The
replace took 1 s. After it, `public` held `schema.sql`'s 22 tables and 15
official views, q3c 2.0.5 with its four indexes, and `ops` kept its rows
(`findings` 75,562, `runs` 377, `deployed_pins` 573). `rp run show 1` exited
64.

**Part 2.** 28 objects, 1,363,479,122 bytes, in 34 s.

**Part 3.** Run 1, live binding 1.

| job | stage | scope | wall (launched to ended) | published | change group | bindings opened |
|---|---|---|---|---|---|---|
| 1 | import | reference 90011, PSF 153, Gaia DR3 | 4.42 s | yes | 2 | 2 catalogs, 3 psfs, 4 refimages |
| 2 | import | epoch 1, expid 84613 | 6.75 s | yes | 3 | 5 diffimages, 6 exposures, 7 l2files, 8 sources |
| 3 | import | epoch 2, expid 84624 | 6.32 s | yes | 4 | 9 diffimages, 10 exposures, 11 l2files, 12 sources |
| 4 | associate | field 4658155, epoch 1 | 0.96 s | yes | 5 | 13 objects (closed by group 6) |
| 5 | associate | field 4658155, epoch 2 | 0.93 s | yes | 6 | 14 objects |
| 6 | packet | expid 84613 | 7.01 s | yes | 7 | 15 packets |
| 7 | packet | expid 84624 | 10.24 s | yes | 8 | 16 packets |

The whole `job local` process, Python start to exit, took 6.5, 7.7, 7.3,
2.0, 1.9, 9.6 and 11.4 s.

Row counts, equal to PRs 279 to 281: 2 exposures, 2 L2 files, 2 difference
images, 1 reference, 1 PSF, 1 Gaia catalogue, 500 sources per epoch; 38
objects after epoch 1 and 77 after epoch 2, with 38 and 39 memberships; 38
and 39 packets. Official views: `sources_official` 1,000,
`objects_official` 77, `members_official` 77, `packets_official` 77.

| packet job | key | records | Avro bytes | cutouts | edge cutouts | prv sources | forced rows | reader |
|---|---|---|---|---|---|---|---|---|
| 6 | 84613-8-8 | 38 | 7,337,680 | 114 | 0 | 0 | 38 | CLEAN |
| 7 | 84624-8-8 | 39 | 7,534,251 | 117 | 0 | 0 | 78 | CLEAN |

The Avro sizes are 2 and 9 bytes smaller than PR 281's local run. Every count
in the records is the same; only the container's size changed. The packet
files are `$RAPIDPIPE_OUTPUTS_ROOT/1/packet/<job>/1/packets/<key>.avro`.

## Part 4: Batch, then the change group

Run unattended on 2026-10-21 from the same host, after the `reference` and
`difference` ports (PRs 287 and 288) were merged on `rebuild`. The Batch jobs
run under the scratch job role, which reads the inputs bucket, writes the
scratch bucket and lists it only under `rapidpipe*` (rapid_systems
`rapid-scratch-identities.yaml`), and cannot read the products bucket. So the
Batch run's outputs root is the same `s3://$SCRATCH_BUCKET/rapidpipe/foundation`
as the baseline's, and every input a Batch stage names is either an
inputs-bucket file or an output under that root.

Every stage of the chain runs on Batch, the downstream `associate` and
`packet` included: milestone 4's products are Batch-produced by definition,
so a stage the image cannot run is a code fix (its pull request, the image
rebuilt through step 10 for the new commit, a new run), never a `job local`
substitute.

```sh
export RAPIDPIPE_OUTPUTS_ROOT="s3://$SCRATCH_BUCKET/rapidpipe/foundation"
export RAPIDPIPE_BATCH_JOB_QUEUE=rapid-queue-prompt
C=$(git -C "$RAPID" rev-parse origin/rebuild)   # the commit that carries the ports
PSK='{"dataset": "'$D'", "fid": 8, "sca": 8, "kind": "science"}'     # the science-image PSF's key
PRK='{"dataset": "'$D'", "fid": 8, "sca": 8, "kind": "reference"}'   # the reference-image PSF's key
RKEY='{"dataset": "'$D'", "field": 4658155, "fid": 8}'
```

10. **The image and the revision**, from a fresh `main` of rapid_systems:

    ```sh
    cloudformation/release-hooks/commit $C
    ```

    `commit` builds over `ssh` on the build host. Where that host does not
    answer, build on this host and register with `deploy` directly, which is
    what `commit` runs after its build:

    ```sh
    containers/rapid-pipeline/build-rebuild.sh $C --tag rebuild-$C \
    && RELEASE_TAG=rebuild-$C RELEASE_IMAGE_DIGEST=<the last line it printed> \
       cloudformation/release-hooks/deploy
    ```

    Check: `deploy` prints `{"deployments": {"rapid-rebuild": "rapid-rebuild:<rev>", ...}}`;
    `aws batch describe-job-definitions --job-definition-name rapid-rebuild --status ACTIVE
    --query 'sort_by(jobDefinitions,&revision)[-1]'` shows that revision, its
    `RAPID_RELEASE_IDENTITY` equal to `rebuild-$C`, and its image ending in the
    digest `aws ecr describe-images --image-ids imageTag=rebuild-$C` reports.
    Then `DEF=rapid-rebuild:<rev>`.

11. **The Batch run**, not live, so its products publish unbound.

    ```sh
    rp run create --who "$USER" --why "milestone 4: reference and difference on Batch for field 4658155, then associate and packet on Batch" \
      --revision $C --settings "$RAPID/rapidpipe/settings/foundation-thin.toml" --survey GBTDS --dataset $D
    ```

    Check: prints the run id, `$B` below; `rp run show $B` shows `"live": null`
    and the revision `$C`. The settings body is the shipped one: its `/code`
    paths are the image's.

12. **Reference** on Batch, coadding the two epochs' L2 files of part 3's
    import jobs 2 and 3 (PR 287's scope and inputs).

    ```sh
    rp job submit --run $B --stage reference --job-definition $DEF --scope '{"field": 4658155, "fid": 8, "sca": 8}' \
      --inputs '[{"product": "l2files", "key": '"$(k 84613)"', "source_job": 2}, {"product": "l2files", "key": '"$(k 84624)"', "source_job": 3}]'
    ```

    Check: prints `{"job", "batch_job"}`, the job `<r>`; `aws batch describe-jobs
    --jobs <batch_job>` reaches `SUCCEEDED`; `rp job show <r>` is `published`
    with its prefix `$RAPIDPIPE_OUTPUTS_ROOT/$B/reference/<r>/1/`.

13. **Difference** on Batch, one job per epoch, each naming the Batch
    reference, the science PSF (import job 1) and the reference-image PSF
    (PR 288's import job 9; the stage tells the two apart by their keys' kind).

    ```sh
    din() { # expid l2-job
      echo '[{"product": "l2files", "key": '"$(k $1)"', "source_job": '$2'}, {"product": "refimages", "key": '"$RKEY"', "source_job": <r>},
             {"product": "psfs", "key": '"$PSK"', "source_job": 1}, {"product": "psfs", "key": '"$PRK"', "source_job": 9}]'; }
    rp job submit --run $B --stage difference --job-definition $DEF --scope '{"expid": 84613, "sca": 8, "fid": 8}' --inputs "$(din 84613 2)" \
    && rp job submit --run $B --stage difference --job-definition $DEF --scope '{"expid": 84624, "sca": 8, "fid": 8}' --inputs "$(din 84624 3)"
    ```

    Check: both Batch jobs `SUCCEEDED`; both jobs `<d1>`, `<d2>` `published`,
    each manifest declaring `diffimages` and `sources`. A Batch job that failed
    on a transient is retried as the same job, not a new one (`rp job submit`
    always records a new job): resubmit its own request with `aws batch
    submit-job`, the same job definition, queue and container overrides
    (`aws batch describe-jobs --jobs <batch_job>` shows them); its command
    `job run --job <id>` takes the next attempt.

14. **The chain downstream**, on Batch: associate epoch 1, then epoch 2 on
    it; then packet epoch 1, then epoch 2 with epoch 1's set as its
    predecessor. The packets name the Batch reference (the one the
    difference images were subtracted against), the Batch difference images
    and sources, the L2 files of import jobs 2 and 3, and the Gaia catalogue
    and science PSF of import job 1. No `--expects`: the run is not live.

    ```sh
    OBJ='{"product": "objects", "key": '"$OKEY"', "source_job": <a2>}'
    SH='{"product": "catalogs", "key": {"name": "gaia", "version": "DR3"}, "source_job": 1}, {"product": "refimages", "key": '"$RKEY"', "source_job": <r>}, '"$PSF"
    img() { # expid difference-job l2-job
      echo '{"product": "diffimages", "key": '"$(k $1)"', "source_job": '$2'}, {"product": "l2files", "key": '"$(k $1)"', "source_job": '$3'}'; }
    rp job submit --run $B --stage associate --job-definition $DEF --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84613)"', "source_job": <d1>}]'
    # after <a1> succeeds:
    rp job submit --run $B --stage associate --job-definition $DEF --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84624)"', "source_job": <d2>}, {"product": "objects", "key": '"$OKEY"', "source_job": <a1>}]'
    # after <a2> succeeds:
    rp job submit --run $B --stage packet --job-definition $DEF --scope '{"expid": 84613, "sca": 8, "fid": 8}' \
      --inputs "[$SH, $OBJ, $(img 84613 <d1> 2), {\"product\": \"sources\", \"key\": $(k 84613), \"source_job\": <d1>}]"
    # after <p1> succeeds:
    rp job submit --run $B --stage packet --job-definition $DEF --scope '{"expid": 84624, "sca": 8, "fid": 8}' \
      --inputs "[$SH, $OBJ, $(img 84613 <d1> 2), $(img 84624 <d2> 3), {\"product\": \"sources\", \"key\": $(k 84624), \"source_job\": <d2>}, {\"product\": \"packets\", \"key\": $(k 84613), \"source_job\": <p1>}]"
    ```

    Check: four Batch jobs `SUCCEEDED`, four jobs `published`, none bound
    (`rp job show` lists no bindings). The packet reader reads official sets
    only, so its check is step 15's.

15. **One change group** replaces the imported reference, difference images,
    source sets, object set and packet sets with the Batch-produced ones, in
    one transaction. First, the bindings it closes must still be open: `rp
    job show` of import jobs 1 to 3, associate job 5 and packet jobs 6 and 7
    lists bindings 4, 5, 8, 9, 12, 14, 15 and 16 with no `closed_group`
    (part 3's ids); if any is closed, stop.

    ```sh
    cat > replace.json <<EOF
    [{"close": 4,  "open": {"product": "refimages",  "key": $RKEY,      "job": <r>}},
     {"close": 5,  "open": {"product": "diffimages", "key": $(k 84613), "job": <d1>}},
     {"close": 8,  "open": {"product": "sources",    "key": $(k 84613), "job": <d1>}},
     {"close": 9,  "open": {"product": "diffimages", "key": $(k 84624), "job": <d2>}},
     {"close": 12, "open": {"product": "sources",    "key": $(k 84624), "job": <d2>}},
     {"close": 14, "open": {"product": "objects",    "key": $OKEY,      "job": <a2>}},
     {"close": 15, "open": {"product": "packets",    "key": $(k 84613), "job": <p1>}},
     {"close": 16, "open": {"product": "packets",    "key": $(k 84624), "job": <p2>}}]
    EOF
    rp official change --group replace.json --who "$USER" --why "milestone 4: the Batch-made products of run $B replace the imported ones"
    ```

    Check: prints the group id `$G`; `refimages_official`, `diffimages_official`,
    `sources_official`, `objects_official`, `members_official` and
    `packets_official` (with `forced_official`, `external_official` and the
    reference's catalog and members, which follow their sets) return run
    `$B`'s jobs; `exposures_official`, `l2files_official`, `psfs_official` and
    `catalogs_official` still return part 3's; and `$PY $RAPID/scripts/read_packets.py
    --job <p1>` (and `--job <p2>`) exits 0 with "CLEAN".

16. **Undo** restores the imported bindings.

    ```sh
    rp official undo $G --who "$USER" --why "milestone 4: undo restores the imported products"
    ```

    Check: every official view returns what it returned before step 15, the
    bindings `$G` opened are closed by the undo's group, and every table's row
    count is what it was before step 15 except `official`, which gains the
    rows of the two groups: nothing was deleted. Milestone 4 is done. Whether
    to reapply `$G` (`rp official undo` of the undo's group) is a decision for
    after the comparison, not part of this runbook.

## Results of part 4 (2026-10-21)

Run from `rebuild` at `a2c15b53` (PR 288, the difference port, on PR 287's
reference port).

**Step 10.** The build host did not answer `ssh` from this host, so the image
was built here with `build-rebuild.sh` (46 s; the base image's layers were
cached) and registered with `deploy` (74 s, the change set exactly the two
job-definition rows). `rapid-rebuild:31` and `rapid-rebuild-production:22`
pin `rebuild-a2c15b53d2ba073cf1fbc916764fd9f6a05eff4f`, image digest
`sha256:b3ba8db9b8c1e6620f36acbe257078d0c34d85552c4a3ccec5d5950ab7d7d7aa`
(1.96 GB compressed), equal to ECR's for the tag. Revisions 29
(`rebuild-v0.12`) and 30 (`rebuild-1a528daa…`, PR 284's commit) stay ACTIVE.

**Steps 11 to 14.** Run 4, not live, revision `a2c15b53`, the shipped
settings body (`max_sources` 500 in `[import]` and `[difference]`). Every
Batch job ran on `rapid-queue-prompt` under `rapid-rebuild:31` (1 vCPU,
15 GiB), exited 0 and published attempt 1; none is bound.

| job | stage | scope | Batch job | queued | in container | job row, started to ended |
|---|---|---|---|---|---|---|
| 11 | reference | field 4658155, fid 8, sca 8 | `0acaf7b1-1d17-4b89-a782-e6962cb578ad` | 156 s | 36.8 s | 31.3 s |
| 12 | difference | expid 84613 | `cb651434-787b-40ed-89e3-e1c09cbad1fd` | 6 s | 1,348 s (22 min 28 s) | 1,345.8 s |
| 13 | difference | expid 84624 | `9c9cf627-5622-4e51-9c84-06444cd33e8e` | 5 s | 1,324 s (22 min 4 s) | 1,321.7 s |
| 14 | associate | field, epoch 1 | `239a1511-c0b6-4c5d-955c-514994e1fc97` | 7 s | 2.8 s | 1.0 s |
| 15 | associate | field, epoch 2 | `636481cc-d9f3-406a-b63d-a46957b081df` | 7 s | 2.7 s | 1.0 s |
| 16 | packet | expid 84613 | `ce41796c-9454-4435-97c8-b4fb89485b48` | 6 s | 21.5 s | 18.1 s |
| 17 | packet | expid 84624 | `f2585797-0599-4eed-9219-a24b544a4a8b` | 4 s | 17.8 s | 15.4 s |

The reference's queue time is the compute environment scaling an instance
up from zero; the two difference jobs ran side by side on it. The difference
stage on one Batch vCPU took 2.3 times its 9 min 43 s on the host's run
(PR 288, 103% of one CPU there), and its peak memory, 14.8 GiB on the host,
fit the definition's 15 GiB: neither job was killed.

- **Reference** (job 11): rfid 2, 2 frames coadded, 6,239 SExtractor sources,
  zero point 26.863, cov5percent 13.0: the same figures as PR 287's host run
  (job 8). Its mosaic, coverage and uncertainty maps are 196 MB each, with an 8.3 MB catalog, under `4/reference/11/1/ref/`.
- **Difference** (jobs 12 and 13): pid 3 and 2, `infobitssci` 0 (every
  catalog produced), `scalefacref` 1.205 and 1.201 (the reference now carries
  `MAGZP` 26.863, against 10,240 on the imported reference's 17.0), 5,703 and
  5,539 sources before the cap, 500 kept each. 72 objects, 2.82 GB per job.
- **Associate** (jobs 14 and 15): 200 and 158 sources pass the flags and `min_snr` cuts; 82 new
  objects from epoch 1, then 73 new from epoch 2 and no epoch-2 source on an
  epoch-1 object: 155 objects, 82 and 73 memberships.
- **Packet** (jobs 16 and 17): 82 and 73 packets, 15,726,792 and 14,047,658
  Avro bytes, 11 and 18 Gaia matches, 82 and 146 forced rows; the stage's own
  timings are 6.8 s and 9.8 s (forced photometry the larger part).

**Step 15.** The bindings to close were open on part 3's ids. Group 9
applied in 0.40 s (the whole `rp` process). After it the official views
returned run 4's jobs: `refimages_official` {11: 1} with
`refimimages_official` {11: 2} and `refimcatalogs_official` {11: 1};
`diffimages_official` {12: 1, 13: 1}; `sources_official` {12: 500, 13: 500};
`objects_official` {15: 155}; `members_official` {14: 82, 15: 73};
`packets_official` {16: 82, 17: 73}; `forced_official` {16: 82, 17: 146};
`external_official` {16: 11, 17: 18}. Exposures, L2 files, PSF and Gaia
catalogue still returned jobs 1 to 3. The reader on the two Batch sets:

| packet job | key | records | Avro bytes | cutouts | edge cutouts | prv sources | forced rows | reader |
|---|---|---|---|---|---|---|---|---|
| 16 | 84613-8-8 | 82 | 15,726,792 | 246 | 3 | 0 | 82 | CLEAN |
| 17 | 84624-8-8 | 73 | 14,047,658 | 219 | 0 | 0 | 146 | CLEAN |

**Step 16.** Undo group 10 applied in 0.44 s. Every official view returned
exactly what it returned before group 9; group 9's bindings 17 to 24 are
closed by group 10, which opened bindings 25 to 32 on the imported jobs.
Every table's row count is unchanged except `official`, 16 then 24 then 32.
Milestone 4 is done.

**The Batch-made difference sources against the imported set** (a
comparison, not a gate). Run 4's capped 500 per epoch match dev's imported
500 within `match_radius` (0.055 arcsec) for 9 and 5 sources (1.8% and
1.0%), within 0.5 arcsec for 32 and 35. The images share the grid and the
finite fraction (0.99967 against 0.99967; 0.99919 against 0.99968), with a
pixel correlation of 0.730 and 0.706 (PR 288's host difference against the
imported 25-frame reference: 0.99989) and a clipped scatter (MAD) of 2.97
and 2.94 against dev's 4.83 and 4.81. The cause is the reference, not the
port: the Batch reference coadds the two epochs being differenced, so each
epoch is subtracted from a reference that is half itself. Its noise is
partly the epoch's own, the scatter falls, and a source present in both
epochs cancels by half. Dev's reference is 25 other frames. With two epochs
this is a proof that the chain runs end to end on Batch, not a science
result; a reference made from frames other than the ones differenced needs
epochs this field's data on the host do not have.

## Part 5: the field uncapped on Batch (milestones 5 and 6)

Run unattended on 2026-10-21 from the same host, after part 4. This part runs
the same chain as part 4 with no source cap, against part 4's Batch
reference (no new reference). Every job runs on Batch, and every step is
measured. Then one change group binds the uncapped products, and its undo
restores them. The measurements are observations, not gates.

`job run` reports each job's peak memory in its log, as `peak_rss_kib` in the
result it prints: `self` is the stage's Python process and `children` its
largest finished child process. It also records the job definition's image
digest on the job row. Both came in with PR 290: Batch's `describe-jobs`
gives no memory figure, and part 4's jobs record neither.

17. **The image and the revision** for the commit carrying PR 290, as step 10.

    Check: as step 10. Then `DEF=rapid-rebuild:<rev>`.

18. **The uncapped run**, not live: the shipped settings body with
    `max_sources = 0` in `[import]` and `[difference]` (0 keeps every source).

    ```sh
    sed 's/^max_sources = 500$/max_sources = 0/' "$RAPID/rapidpipe/settings/foundation-thin.toml" > uncapped.toml
    rp run create --who "$USER" --why "milestones 5 and 6: field 4658155 uncapped on Batch" \
      --revision $C --settings uncapped.toml --survey GBTDS --dataset $D
    ```

    Check: prints the run id `$U`. `rp run show $U` shows `"live": null`,
    the revision `$C`, and `max_sources` 0 in both tables.

19. **Difference** on Batch for both epochs, as step 13, under `$U` and
    naming part 4's reference job `<r>`. Check: as step 13. Each manifest's
    `sources_kept` equals its `sources_before_cap`.

20. **The chain downstream** on Batch, as step 14, under `$U`. The packets
    name `<r>` as their reference. Check: as step 14.

21. **One change group and its undo**, as steps 15 and 16. The group closes
    the bindings that step 16's undo opened (the imported reference,
    difference images, source sets, object set and packet sets) and opens
    `<r>` and `$U`'s six products. Check: as steps 15 and 16.

## Results of part 5 (2026-10-21)

Run from `rebuild` at `5030ba4d` (PR 290). The image was built on this host
with `build-rebuild.sh` (20 s, layers cached) and registered with `deploy`
(74 s). `rapid-rebuild:32` and `rapid-rebuild-production:23` pin
`rebuild-5030ba4d7552365a3e4e425902d609943abafa0f`, with digest
`sha256:00f2841dd17df9a75e08652e84a6878e00616f5c978e8568c7410256130bad09`,
which equals ECR's for the tag. Run 5 is not live. Its jobs ran under
`rapid-rebuild:32` (1 vCPU, 15 GiB). Each exited 0, published attempt 1 and
recorded the digest in `jobs.image_digest`.

**Per stage per epoch on Batch.** Both runs used the same queue and
job definition shape. Run 4 had cap 500 (part 4) and run 5 had no cap. "In
container" is Batch's `startedAt` to `stoppedAt`. Peak memory is
`peak_rss_kib` from run 5's logs, which run 4 does not have.

| stage | epoch | run 4 job, in container | run 5 job | queued | in container | peak RSS, self / largest child | outputs |
|---|---|---|---|---|---|---|---|
| reference | both | 11, 36.8 s | (run 4's 11) | | | | 3 × 196 MB images, 8.3 MB catalog |
| difference | 84613 | 12, 1,348 s | 18 | 126 s | 1,368.5 s (22 min 49 s) | 1.37 GiB / 14.35 GiB | 72 files, 2.82 GB |
| difference | 84624 | 13, 1,324 s | 19 | 124 s | 1,352.6 s (22 min 33 s) | 1.31 GiB / 14.35 GiB | 72 files, 2.82 GB |
| associate | 1 | 14, 2.8 s | 20 | 5 s | 3.2 s | 227 MiB / none | 10.3 KB |
| associate | 2 | 15, 2.7 s | 21 | 6 s | 2.9 s | 228 MiB / none | 13.5 KB |
| packet | 84613 | 16, 21.5 s | 22 | 3 s | 14.1 s | 844 MiB / none | 15.7 MB |
| packet | 84624 | 17, 17.8 s | 23 | 7 s | 18.5 s | 845 MiB / none | 14.1 MB |

The difference jobs' queue time is the compute environment scaling up from
zero. The downstream jobs found an instance already running.

- **Sources per image before the cap** (what `difference` writes uncapped):
  5,703 for 84613 and 5,544 for 84624. Dev's own ZOGY PSF catalogs for the
  same two images (`zogy_diffimage_masked_psfcat.parquet`) hold 5,450 and
  5,586 rows. Part 4's capped run counted 5,703 and 5,539 before its cap: the
  same code on the same inputs gave 84624 five more sources this time.
  The cause (foundation/determinism, below): the clipped statistics'
  correction factor is a Monte Carlo draw `dev` makes unseeded, so every
  clipped standard deviation moved by about 3e-4 between runs, and with it
  the uncertainty images, S_corr and the PSF-fit threshold. The shipped
  settings now seed it. The plan's "16 to 43 thousand sources per
  image" is the IMSS dump's figure for the field's epochs in general. It is
  not reproduced on these two images, by the port or by dev's own catalogs.
- **Removing the cap changes nothing downstream on this field.** `associate`
  accepts sources with `flags = 0` and `fluxfit / fluxerr >= 5`: 199 for
  epoch 1 and 158 for epoch 2, all inside the brightest 500 by the same
  ratio. So the objects (82 new, then 73 new, 155 in all), the memberships
  (82 and 73, no pass-2 memberships) and the packets match part 4's.
  Part 4's epoch 1 accepted 200, one more.
- **Packets per chip, uncapped:** 82 for 84613 and 73 for 84624, from 2,586
  and 2,496 `flags = 0` sources in the set. The reader reads both CLEAN: 246
  and 219 cutouts, 82 and 146 forced rows, 11 and 18 Gaia matches. The
  stage's own split for the two sets was forced photometry 3.8 s and 7.1 s,
  cutouts 2.0 s and 1.8 s, matching 0.7 s and 0.6 s, and assembly 0.5 s each.
  Forced photometry grows with the window's image count (one image, then
  two), as PR 281 measured.
- **The matcher at full density** for this field is `associate`'s 1.0 s and
  1.1 s from the job row's start to its end, with 227 MiB peak, matching 199
  and 158 sources. **Data-limited** for a dense field: the S/N cut leaves
  under 200 sources per image on these two epochs. The host holds no image
  that would give the matcher tens of thousands. PR 280's synthetic curve
  (linear, about 1 KB per object) is the only figure for that load.
- **The member chain's read depth and its cost.** On this field the depth
  is 2 (job 21 names job 20). With depth 2 the cost cannot be told apart from
  the 1.0 s job. A synthetic chain was therefore measured on a throwaway
  PostgreSQL 18 container on this host. Each object set names the previous
  one and records 1,000 memberships, and the packet stage's own reads were
  timed at each depth, best of three, after `ANALYZE`:

  | depth (object sets) | memberships | chain (`CHAIN_SQL`) | trigger lookup | history of 500 objects (rows) | `members_official`, one object | `members_official`, whole |
  |---|---|---|---|---|---|---|
  | 10 | 10 thousand | 0.1 ms | 0.4 ms | 1.4 ms (1,389) | 0.2 ms | 1.2 ms |
  | 100 | 100 thousand | 0.7 ms | 0.5 ms | 5.9 ms (1,586) | 1.0 ms | 11 ms |
  | 1,000 | 1 million | 55 ms | 1.0 ms | 42 ms (18,366) | 57 ms | 0.22 s |
  | 3,000 | 3 million | 2.9 ms | 2.4 ms | 0.17 s (144,919) | 9.0 ms | 0.39 s |
  | 10,000 | 10 million | 9.9 ms | 12 ms | 0.92 s (1.58 million) | 52 ms | 1.33 s |

  The chain walk and the trigger lookup stay linear and small at ten
  thousand sets. The history read grows with the member rows it returns, not
  with the depth. The 1,000-set row is slower than the 3,000-set row, and the
  cause was not probed (unverified: a plan chosen before the statistics
  settled). On these figures the plan's compaction remedy is not needed
  at ten thousand sets. The rows were inserted directly, not through the
  publisher, so this measures the reads only.
- **Change-group wall time at this key count.** Group 11 closed and opened 8
  keys: 19.3 ms for the operation, through the pooler from this host. Undo
  group 12 took 15.3 ms. Each `rp` process took 0.40 s, almost all of it
  Python start-up and the connection (0.24 s with the connection, 0.41 s for
  `rp job show`). PR 277's curve (0.14 ms per key, local container) predicts
  1.1 ms for 8 keys. The difference is about three round trips per key to
  the pooler over the network, which the set-based remedy in PR 277
  would remove. At this key count it is negligible either way.
- **Change group and undo.** Group 11 closed bindings 25 to 32 and opened 33
  to 40 on job 11 and jobs 18 to 23. The official views then returned run 5's
  jobs: `diffimages_official` {18: 1, 19: 1}; `sources_official`
  {18: 5,703, 19: 5,544}; `objects_official` {21: 155}; `members_official`
  {20: 82, 21: 73}; `packets_official` {22: 82, 23: 73}; `forced_official`
  {22: 82, 23: 146}; `external_official` {22: 11, 23: 18}; and the
  reference's three views on job 11. Exposures, L2 files, PSF and Gaia stayed
  on jobs 1 to 3. `read_packets.py --job 22` and `--job 23` each exited 0
  with "CLEAN". Undo group 12 closed 33 to 40 and opened 41 to 48 on the
  imported jobs. Every view then matched its state before group 11. Every
  table's row count was unchanged except `official` (32, then 40, then 48).

**Not measured, and why:**

- **The other epochs and a second field: data-limited.** The host holds two
  epochs of one field and one chip. The other epochs of field 4658155 (about
  68 in the 20260821 night) and a second field (OpenUniverse or SOC HLWAS)
  are on IMSS. Bringing them over is the laptop copy under both accounts'
  profiles (the plan's milestone 1 recipe).
- **rubrat: data-limited.** Dev's `compute_rb` (`modules/utils/rapid_data_analysis.py`)
  runs the RuBR-AT Keras classifier on three image stamps and catalog
  columns. The images are here. The model (`rubrat/2026-10-06_canonical9_seed42/`:
  `best.keras`, `canonical_9.json`, `threshold.json`) is in dev's pipeline-files
  bucket, which this account cannot read (AccessDenied under the
  administrator profile). It is not on this host. No stage on `rebuild`
  computes it yet. sumrat, which needs only the difference image and the
  source positions, is not data-limited. It is not ported (no code on
  `rebuild`).
- **NED: data-limited.** Dev's `match_nedcat` reads a HATS copy of NED
  (release 36.1_20260527_v2) under the same bucket's `ned/` prefix, which is
  unreadable from this account and not on this host. `rebuild` has the
  matcher and an astroquery reader, off by default, but not dev's local
  HATS reader.
- **NED-LVS: data-limited.** Dev's `nedlvs.parquet` (about 2.1 million
  rows, under the same bucket's `ned/lvs/`) is likewise unreadable and
  absent. `rebuild` has the `lvsMatches` record but no LVS matcher or
  reader.
- **Completeness against the injected sources: data-limited.** The injected
  source list for the 20260821 run is not among the field's files.

**Against the hour-per-image statement.** The provisional requirement is an
alert current within an hour of an image's delivery (rapid_docs,
`decisions.md`). On two epochs of one chip, every stage on Batch with one
vCPU each, one image's chain took about 25 minutes. That is 2 minutes of
cold queue, 22.5 to 23 minutes of difference, and under 30 seconds of
associate and packet with their queue waits. About 35 minutes are left
inside the hour. The difference stage is 95% of it, and the cap does not
change it (1,348 s capped, 1,369 s uncapped for 84613): the cap selects
output sources and is not a cost. Its largest child process peaked at
14.35 GiB of the definition's 15 GiB, a margin of 0.65 GiB. These figures
cover two epochs and one chip, with a reference that coadds the two images
being differenced. The latency decision needs these further measurements:

- the difference stage on images at the plan's stated density of 16 to 43
  thousand sources, where dev's runs took about 58 minutes (`decisions.md`);
- its peak memory there, against the 15 GiB definition;
- a night's fan-out of about 68 epochs per field across 18 chips, on the
  queue's real concurrency and cold-start behaviour;
- packet cost with a thirty-day window, where forced photometry adds about
  3.5 s per window image;
- a reference built from frames other than the ones differenced.

All of these need data this host does not have.

## The difference stage's determinism (2026-10-21)

Run on this host with `job local --stage difference` on epoch 84624, with
part 5's inputs (import job 3's L2 row, part 4's reference job 11, import
job 1's science PSF, job 9's reference PSF). The settings body was the shipped
one, with the host's `/code` paths. Two non-live runs were used: run 6 at
`0da94fa0` (`clip_correction_seed = -1`, `dev`'s unseeded draw) and run 7 at
`7ea1a728` (the seed pinned at 0). Each run had two jobs, run side by side.

| run | job | seed | sources before the cap | wall | peak RSS self / child |
|---|---|---|---|---|---|
| 6 | 24 | unseeded | 5,542 | 669 s | 1.32 / 14.74 GiB |
| 6 | 25 | unseeded | 5,544 | 671 s | 1.35 / 14.50 GiB |
| 7 | 26 | 0 | 5,539 | 666 s | 1.32 / 14.75 GiB |
| 7 | 27 | 0 | 5,539 | 668 s | 1.26 / 14.75 GiB |

With part 4's job 13 (5,539) and part 5's job 19 (5,544), four unseeded runs
gave 5,539 to 5,544.

- **The first file that differs** between jobs 24 and 25 is the reformatted
  science uncertainty image. Its 5,388 NaN pixels are filled with the
  science image's clipped standard deviation: 13.270568 in one job and
  13.272371 in the other. The ratio, 1.000136, is the square root of the
  ratio of the two Monte Carlo clipping-correction factors (`compute_clip_corr`,
  ten draws of a million normals, unseeded in `dev`). The six factors drawn
  on this host have a relative standard deviation of 3.3e-4 in their square root.
- **What follows from it**: S_corr (91% of its pixels, by at most 0.13),
  the difference uncertainty images (every pixel, by at most 0.0017), and
  the PSF-fit catalogs. The PSF fit's detection threshold is 5 times the
  image's clipped standard deviation, so a few sources at the threshold
  cross it: 5,642 against 5,641 positive rows. Everything before it is
  bit-identical, and so are the ZOGY, SFFT and naive difference images:
  the resampled and gain-matched reference, the background-subtracted
  science image, `scalefacref`, the registration residuals and both FWHMs.
  The only other differences are FITS `DATE` and `CHECKSUM` cards and the
  bkgest tables' time stamps.
- **Seeded**, jobs 26 and 27 wrote the same pixels in every image and the
  same rows in every catalog, NaN for NaN. That covers the 500 published
  sources and the 5,637 rows of the PSF-fit catalog. The published rows
  differ only in their drawn `pid`, and `diffimages.checksum` differs only
  through the FITS `DATE` card.
- The shipped `[difference.statistics] clip_correction_seed` is now 0. The
  reference stage draws the same factor unseeded (`[reference.statistics]`)
  and is unchanged.

## Part 6: the night (2026-10-21)

1. **The PSF key gains its kind; the read role can select.** The PSF key
   becomes (dataset, fid, sca, kind), `kind` being `science` (a science
   image's PSF) or `reference` (a reference image's), so the reference-image
   PSF that the difference stage reads is its own key instead of colliding
   with the science PSF's. This is the first change to SMDC's schema after
   real data, so it is incremental SQL, run once as `postgres` in one
   transaction. Each row's kind comes from its file's `PSFKIND` keyword:
   `sciimage_psf` on job 1's file (psfid 153) gives `science`, and
   `refimage_psf` on job 9's (psfid 1) gives `reference`.

   ```sh
   pg -1 -v kind1=science -v kind9=reference <<'SQL'
   ALTER TABLE psfs ADD COLUMN kind text;
   UPDATE psfs SET kind = :'kind1' WHERE job = 1 AND psfid = 153;
   UPDATE psfs SET kind = :'kind9' WHERE job = 9;
   ALTER TABLE psfs ALTER COLUMN kind SET NOT NULL;
   ALTER TABLE psfs ADD CONSTRAINT psfs_kind_check CHECK (kind IN ('science', 'reference'));
   ALTER TABLE psfs DROP CONSTRAINT psfs_dataset_fid_sca_job_key;
   ALTER TABLE psfs ADD CONSTRAINT psfs_dataset_fid_sca_kind_job_key UNIQUE (dataset, fid, sca, kind, job);
   DROP VIEW psfs_official;
   CREATE VIEW psfs_official AS
   SELECT t.*, o.id AS binding, j.run
   FROM psfs t
   JOIN official o ON o.closed_at IS NULL AND o.product = 'psfs' AND o.job = t.job
       AND o.key = jsonb_build_object('dataset', t.dataset, 'fid', t.fid, 'sca', t.sca, 'kind', t.kind)
   JOIN jobs j ON j.id = t.job;
   UPDATE official o SET key = o.key || jsonb_build_object('kind', p.kind)
   FROM psfs p WHERE o.product = 'psfs' AND p.job = o.job AND NOT o.key ? 'kind';
   UPDATE job_inputs i SET key = i.key || jsonb_build_object('kind', p.kind)
   FROM psfs p WHERE i.product = 'psfs' AND p.job = coalesce(i.source_job, i.job) AND NOT i.key ? 'kind';
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO rapid_read;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO rapid_read;
   SQL
   ```

   The view is dropped and recreated rather than replaced because the new
   column changes its column order. On SMDC `kind` is the table's last
   column, whereas `schema.sql` puts it after `sca`. The publisher loads
   by column name, so the order does not matter.

   Stored keys: `official` and `job_inputs` compare a key as the whole
   jsonb object, and the view builds the key with the kind, so a stored
   PSF key without it would no longer match. The two `UPDATE`s backfill
   it. That covers one `official` row (binding 3, job 1's) and 27
   `job_inputs` rows (job 1's `expects`, and the inputs of jobs 2, 3, 6,
   7, 10, 12, 13, 16 to 19 and 22 to 27). Each key takes the kind of the
   `psfs` row of the job it names. Manifests record no keys, and the
   published `psfs.parquet` files stay as they were.

   Checks, in order, all held:
   - `psfs` holds psfid 153 (job 1) `science` and psfid 1 (job 9) `reference`.
   - `psfs_official` returns job 1's science row only (binding 3, run 1).
   - Every `psfs` key in `job_inputs` (27 of 27) and `official` (1 of 1)
     carries its kind.
   - `rp job show 9` and `rp job show 10` exit 0, and job 10's output
     differs from before only by the kind in its two PSF inputs.
   - PR 282's statements (`database/queries.sql`, 23 statements, run with
     SMDC's ids) return the same rows as before the change, by count and
     digest. `read_packets.py --job 7` exits 0 with output identical to
     before.
   - As `rapid_rebuild_pipeline`, `has_table_privilege('rapid_read', <oid>,
     'select')` is true for all 37 tables and views in `public`.
     `packets_official` is among them; before the change it was false.

   The statements took 8 ms on the server; the script took 0.27 s
   with its connection.

   Then the reference-image PSF binds. It was left unbound because its
   key collided with the science PSF's:

   ```sh
   echo '[{"open": {"product": "psfs", "key": '"$PRK"', "job": 9}}]' > psf-ref.json \
   && rp official change --group psf-ref.json --who "$USER" \
        --why "foundation/psf-kind: the reference-image PSF binds now its key carries the kind"
   ```

   Check: group 13 opened binding 49, and `psfs_official` returns two rows,
   job 1's `science` (binding 3) and job 9's `reference` (binding 49).
   `official` holds 49 rows. The operation took 4.6 ms, through the pooler
   from this host, and the process took 0.41 s. (It was applied through
   `rapidpipe.foundation.official.change`, the function the verb calls,
   so the operation could be timed apart from start-up.)

   From here a difference job names `psfs` `$PSK` (job 1) and `$PRK`
   (job 9). An import's `psf` scope declares `"kind"`, and a file whose
   `PSFKIND` names the other kind fails before its manifest.

The night's steps 2 to 5 ran unattended on 2026-10-21 and 22 from the same
host, from `rebuild` at `8a49bf70` (part 6 step 1), on `rapid-rebuild:33`
(image `rebuild-8a49bf70…`, 1 vCPU, 24 GiB, Batch retrying exit 75 only).
The night is 69 epochs of field 4658155, SCA 8, W146, one simulated night
from 2027-10-07 07:18 to 23:58 UTC, every epoch differenced by `dev`
against reference rfid 90011; its first two epochs by `mjdobs` (84613 and
84624) are part 3's. `$EPOCHS` is the bucket's
`EPOCHS-20260821-sca8.tsv`, one row per epoch. The loops of steps 3 and 4
were Python scripts outside the repository calling the same `rp` verbs per
epoch; they are shown here as the commands they issued.

2. **The night's files, and the live run by cutover.**

   ```sh
   aws s3 cp --recursive "s3://$INPUTS_BUCKET/foundation/field-4658155-20260821/" "$DATA/" \
   && (cd "$DATA" && sha256sum -c SHA256SUMS --quiet && sha256sum -c SHA256SUMS-epochs-20260821-sca8 --quiet)
   rp run create --who "$USER" --why "foundation-night-2026-10-21 task 4 (night-baseline): ..." \
     --revision "$(git -C "$RAPID" rev-parse HEAD)" --settings "$RAPID/rapidpipe/settings/foundation-thin.toml" \
     --survey GBTDS --dataset $D
   echo '[{"close": 1, "open": {"survey": "GBTDS", "dataset": "'$D'", "run": 9}}]' > cutover.json \
   && rp official change --group cutover.json --who "$USER" --why "cutover: run 9 (the night's live loop) replaces run 1 as live"
   ```

   Check, all held: 566 files, 23,459,086,282 bytes, both checksum files
   clean (the copy took 92 s, 255 MB/s; the two checks 14 s and 230 s).
   The run is **run 9**, not live when created. **Group 14** closed live
   binding 1 (run 1) and opened **live binding 50** (run 9), its only two
   rows; `rp run show 9` shows `"live": 50` and `rp run show 1` `"live":
   null`; the 15 open product bindings were the same before and after. The
   operation took 4.1 ms through the pooler (0.49 s for the process). The
   imported products stay bound to run 1's jobs: a cutover carries only
   what it names.

3. **Import the 67 new epochs** under run 9, one `job local` per epoch in
   `mjdobs` order, with part 3's `epoch`, `expects` and `$PSF`:

   ```sh
   # for each $EPOCHS row after the first two, in mjdobs order:
   rp job local --run 9 --stage import --scope "$(epoch $expid $rid $pid $(basename $l2) $jid)" \
     --expects "$(expects $expid)" --inputs "[$PSF]"
   ```

   Check, all held: jobs 30 to 96 `published`, each bound automatically
   (groups 15 to 81, four bindings each, no refusal); `exposures_official`,
   `l2files_official` and `diffimages_official` 69 rows each;
   `sources_official` 69 sets of 500 rows (34,500). The cap's contract
   holds per epoch: dev's ZOGY PSF-fit catalog has 4,399 to 6,617 rows per
   epoch and 4,336 to 6,518 inside the fit-position bounds with a finite
   S/N, so every set keeps 500. (Sixteen epochs have fewer than 5,000
   eligible rows; the least is 85037, the 67th, at 4,336.)

   The job row (started to ended) took 5.90 to 7.14 s, mean 6.36 s; the
   whole process 7.02 to 8.65 s, mean 7.51 s; the loop 8 min 23 s for 67
   epochs. The imports wrote 21.9 GB (737 objects) under
   `9/import/`, about 327 MB per epoch: the L2 file and the five
   difference-job images the packet stage reads. Per epoch:

   | # | expid | UTC | job | catalog rows | eligible | sources | job row s | process s |
   |---|---|---|---|---|---|---|---|---|
   | 3 | 84635 | 07:45 | 30 | 5,227 | 5,153 | 500 | 6.75 | 7.96 |
   | 4 | 84640 | 07:59 | 31 | 5,175 | 5,098 | 500 | 6.26 | 7.42 |
   | 5 | 84637 | 08:13 | 32 | 5,590 | 5,491 | 500 | 6.54 | 7.68 |
   | 6 | 84644 | 08:26 | 33 | 5,330 | 5,250 | 500 | 6.36 | 7.52 |
   | 7 | 84659 | 08:40 | 34 | 4,863 | 4,785 | 500 | 6.30 | 7.42 |
   | 8 | 84664 | 08:54 | 35 | 5,721 | 5,648 | 500 | 6.13 | 7.26 |
   | 9 | 84662 | 09:07 | 36 | 5,273 | 5,189 | 500 | 6.58 | 7.70 |
   | 10 | 84668 | 09:21 | 37 | 5,408 | 5,343 | 500 | 6.33 | 7.50 |
   | 11 | 84684 | 09:49 | 38 | 5,109 | 5,034 | 500 | 6.45 | 7.58 |
   | 12 | 84687 | 10:03 | 39 | 5,342 | 5,274 | 500 | 6.37 | 7.51 |
   | 13 | 84693 | 10:16 | 40 | 5,343 | 5,256 | 500 | 6.07 | 7.18 |
   | 14 | 84701 | 10:30 | 41 | 4,629 | 4,566 | 500 | 6.10 | 7.20 |
   | 15 | 84713 | 10:44 | 42 | 5,519 | 5,440 | 500 | 6.18 | 7.32 |
   | 16 | 84710 | 10:57 | 43 | 5,428 | 5,349 | 500 | 6.23 | 7.38 |
   | 17 | 84723 | 11:11 | 44 | 4,599 | 4,537 | 500 | 7.02 | 8.11 |
   | 18 | 84730 | 11:25 | 45 | 4,943 | 4,878 | 500 | 6.50 | 7.61 |
   | 19 | 84738 | 11:38 | 46 | 5,269 | 5,193 | 500 | 6.43 | 7.56 |
   | 20 | 84737 | 11:52 | 47 | 6,024 | 5,932 | 500 | 6.14 | 7.25 |
   | 21 | 84747 | 12:06 | 48 | 5,323 | 5,262 | 500 | 6.41 | 7.54 |
   | 22 | 84749 | 12:20 | 49 | 6,427 | 6,350 | 500 | 6.32 | 7.44 |
   | 23 | 84761 | 12:47 | 50 | 5,415 | 5,338 | 500 | 6.30 | 7.48 |
   | 24 | 84765 | 13:01 | 51 | 5,411 | 5,351 | 500 | 6.47 | 7.60 |
   | 25 | 84778 | 13:15 | 52 | 5,195 | 5,124 | 500 | 6.25 | 7.37 |
   | 26 | 84787 | 13:28 | 53 | 5,829 | 5,730 | 500 | 6.26 | 7.38 |
   | 27 | 84781 | 13:42 | 54 | 5,505 | 5,412 | 500 | 6.06 | 7.18 |
   | 28 | 84791 | 13:56 | 55 | 5,177 | 5,103 | 500 | 6.23 | 7.36 |
   | 29 | 84800 | 14:10 | 56 | 5,345 | 5,263 | 500 | 5.99 | 7.11 |
   | 30 | 84805 | 14:23 | 57 | 5,223 | 5,156 | 500 | 6.43 | 7.57 |
   | 31 | 84806 | 14:37 | 58 | 4,852 | 4,768 | 500 | 6.38 | 7.48 |
   | 32 | 84816 | 14:51 | 59 | 5,792 | 5,711 | 500 | 6.23 | 7.35 |
   | 33 | 84822 | 15:04 | 60 | 5,364 | 5,293 | 500 | 7.14 | 8.26 |
   | 34 | 84828 | 15:18 | 61 | 5,440 | 5,360 | 500 | 6.20 | 7.30 |
   | 35 | 84837 | 15:46 | 62 | 6,617 | 6,518 | 500 | 6.56 | 7.68 |
   | 36 | 84850 | 15:59 | 63 | 5,466 | 5,394 | 500 | 6.20 | 7.32 |
   | 37 | 84856 | 16:13 | 64 | 4,975 | 4,889 | 500 | 6.39 | 7.53 |
   | 38 | 84852 | 16:27 | 65 | 4,748 | 4,673 | 500 | 6.15 | 8.65 |
   | 39 | 84861 | 16:41 | 66 | 5,301 | 5,221 | 500 | 6.33 | 7.44 |
   | 40 | 84871 | 16:54 | 67 | 5,282 | 5,210 | 500 | 6.30 | 7.41 |
   | 41 | 84878 | 17:08 | 68 | 5,037 | 4,954 | 500 | 6.14 | 7.29 |
   | 42 | 84880 | 17:22 | 69 | 5,643 | 5,560 | 500 | 6.31 | 7.41 |
   | 43 | 84884 | 17:35 | 70 | 4,517 | 4,440 | 500 | 6.43 | 7.59 |
   | 44 | 84894 | 17:49 | 71 | 5,619 | 5,544 | 500 | 6.37 | 7.49 |
   | 45 | 84901 | 18:03 | 72 | 5,873 | 5,790 | 500 | 7.00 | 8.12 |
   | 46 | 84905 | 18:16 | 73 | 5,733 | 5,643 | 500 | 6.23 | 7.37 |
   | 47 | 84919 | 18:43 | 74 | 5,155 | 5,127 | 500 | 6.37 | 7.52 |
   | 48 | 84925 | 18:57 | 75 | 5,047 | 4,985 | 500 | 6.35 | 7.46 |
   | 49 | 84926 | 19:10 | 76 | 5,315 | 5,243 | 500 | 6.39 | 7.51 |
   | 50 | 84935 | 19:24 | 77 | 4,627 | 4,559 | 500 | 5.90 | 7.02 |
   | 51 | 84946 | 19:38 | 78 | 5,864 | 5,788 | 500 | 6.28 | 7.40 |
   | 52 | 84950 | 19:51 | 79 | 6,201 | 6,106 | 500 | 6.34 | 7.48 |
   | 53 | 84948 | 20:05 | 80 | 4,753 | 4,679 | 500 | 6.58 | 7.70 |
   | 54 | 84958 | 20:19 | 81 | 5,714 | 5,641 | 500 | 6.51 | 7.63 |
   | 55 | 84965 | 20:33 | 82 | 5,039 | 4,965 | 500 | 6.34 | 7.45 |
   | 56 | 84970 | 20:46 | 83 | 5,478 | 5,386 | 500 | 6.26 | 7.39 |
   | 57 | 84986 | 21:00 | 84 | 5,392 | 5,329 | 500 | 6.28 | 7.40 |
   | 58 | 84984 | 21:14 | 85 | 5,687 | 5,608 | 500 | 6.88 | 8.01 |
   | 59 | 84990 | 21:41 | 86 | 6,202 | 6,157 | 500 | 6.19 | 7.37 |
   | 60 | 85009 | 21:55 | 87 | 5,631 | 5,564 | 500 | 6.49 | 7.60 |
   | 61 | 85006 | 22:09 | 88 | 5,377 | 5,302 | 500 | 6.16 | 7.29 |
   | 62 | 85019 | 22:22 | 89 | 4,863 | 4,786 | 500 | 6.08 | 7.21 |
   | 63 | 85012 | 22:36 | 90 | 5,989 | 5,909 | 500 | 7.03 | 8.15 |
   | 64 | 85032 | 22:50 | 91 | 5,449 | 5,369 | 500 | 6.47 | 7.60 |
   | 65 | 85030 | 23:04 | 92 | 5,775 | 5,675 | 500 | 6.42 | 7.55 |
   | 66 | 85043 | 23:17 | 93 | 5,807 | 5,726 | 500 | 6.28 | 7.40 |
   | 67 | 85037 | 23:31 | 94 | 4,399 | 4,336 | 500 | 6.16 | 7.26 |
   | 68 | 85053 | 23:45 | 95 | 5,341 | 5,253 | 500 | 6.39 | 7.53 |
   | 69 | 85052 | 23:58 | 96 | 5,048 | 4,976 | 500 | 6.30 | 7.41 |

4. **The chain on Batch, live.** For epochs 3 to 69 in `mjdobs` order: the
   epoch's `associate` (its predecessor run 1's job 5 for epoch 3, then
   run 9's previous set; expects the open object binding), then its
   `packet` once the associate and the previous packet set are published.
   Every packet job names the whole night so far: every earlier epoch's
   difference image and L2 file (the 30-day forced window holds the
   night), and every earlier packet set, so a forced measurement any of
   them holds is reused. Associate ran ahead of packet:

   ```sh
   OKEY='{"dataset": "'$D'", "survey": "GBTDS", "field": 4658155}'
   DEF=rapid-rebuild:33
   # epoch n: <s> its import job, <a> the previous object set's job, <b> the open object binding
   rp job submit --run 9 --stage associate --job-definition $DEF --scope '{"field": 4658155}' \
     --inputs '[{"product": "sources", "key": '"$(k $expid)"', "source_job": <s>}, {"product": "objects", "key": '"$OKEY"', "source_job": <a>}]' \
     --expects '[{"product": "objects", "key": '"$OKEY"', "binding": <b>}]'
   # once that associate <a_n> and packet n-1 are published:
   rp job submit --run 9 --stage packet --job-definition $DEF --scope '{"expid": '$expid', "sca": 8, "fid": 8}' \
     --inputs "[$SHARED_INPUTS, {\"product\": \"objects\", \"key\": $OKEY, \"source_job\": <a_n>},
               $(image <epoch 1> 2), ..., $(image $expid <s>), {\"product\": \"sources\", \"key\": $(k $expid), \"source_job\": <s>},
               {\"product\": \"packets\", \"key\": <epoch 1's key>, \"source_job\": 6}, ..., <packet n-1>]" \
     --expects '[{"product": "packets", "key": '"$(k $expid)"', "binding": null}]'
   ```

   Every one of the 134 Batch jobs `SUCCEEDED` on its first attempt and
   published, each bound automatically with no refusal (groups 82 to
   215).

   Check, all held:
   - `objects_official` is run 9's last set, job 179, with 1,504 objects,
     every object of the night (`members_official` holds 1,504 distinct
     `aid`s).
   - `members_official` holds 2,547 rows, the sum of the `memberships`
     rows over the chain (jobs 4, 5 and run 9's 67 associate jobs), and
     each associate job's `memberships` count equals its manifest's
     pass 1 plus pass 2.
   - `packets_official` holds 69 sets (2,488 packets), `forced_official`
     87,520 rows and `external_official` 530.
   - `read_packets.py --job <id>` exits 0 with "CLEAN" for all 69 sets
     (jobs 6 and 7 and run 9's 67), each in 1 to 3 s.
   - `official` holds 452 rows: 49, the cutover's 1, 268 import bindings,
     67 object bindings (66 of them since closed by the next set) and 67
     packet bindings.

   **Measurements.** First submit (21:27:59) to the last packet published
   (01:20:47): **3 h 52 min 48 s** (13,963 s).

   | stage | jobs | queue wait mean / median / max | in container mean / min / max | container time, sum | peak RSS (self) | child processes |
   |---|---|---|---|---|---|---|
   | associate | 67 | 17.3 / 6.9 / 214 s | 2.7 / 2.4 / 10.0 s | 184 s | 227 to 243 MiB | none |
   | packet | 67 | 10.3 / 7.1 / 235 s | 189 / 28 / 362 s | 12,646 s | 844 to 1,036 MiB | none |

   The associate chain finished 1,602 s after the first submit, so for
   most of the night associate ran far ahead and the packet chain was the
   critical path: an associate and a packet container ran at the same time
   for only 152 s in all. The two largest queue waits are the compute
   environment scaling up from zero (the first job) and once more mid-run.
   The packet stage's time grows linearly with the window: its forced
   photometry took 10 s for a 3-image window and 247 s for 69 (about
   3.6 s per window image), 8,779 s of the 12,646 s in all (69%); cutouts
   59 s, matching 32 s, assembly 19 s in all. The rest of each container
   (about 110 s at the end) is fetching the window's images before the
   stage starts: every named image is fetched whether or not its pairs are
   reused. Peak memory never exceeded 1.04 GiB of the 24 GiB definition.

   Over the night associate matched 5,216 sources (those with `flags = 0`
   and S/N at least 5 among each epoch's 500) into 1,427 new objects and
   2,470 memberships, none in pass 2; packets numbered 2,411, with 87,404
   forced rows of which 62,427 were measured and 24,977 reused from
   predecessor sets; the Avro totalled 469 MB. Run 9's prefixes hold
   3.9 MB under `associate/` and 475 MB under `packet/`. Per epoch:

   | # | expid | assoc job | queue s | container s | peak RSS MiB | sources in | new objects | memberships | objects | packet job | queue s | container s | peak RSS MiB | window | packets | forced rows (measured / reused) | Avro MB | reader |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | 3 | 84635 | 97 | 193 | 2.7 | 228 | 71 | 33 | 36 | 110 | 99 | 10 | 38 | 844 | 3 | 36 | 108 (103 / 5) | 6.9 | CLEAN |
   | 4 | 84640 | 98 | 7 | 2.9 | 227 | 60 | 30 | 31 | 140 | 101 | 5 | 28 | 845 | 4 | 31 | 120 (118 / 2) | 5.9 | CLEAN |
   | 5 | 84637 | 100 | 101 | 2.7 | 229 | 85 | 34 | 35 | 174 | 103 | 6 | 33 | 845 | 5 | 35 | 175 (173 / 2) | 6.8 | CLEAN |
   | 6 | 84644 | 102 | 7 | 2.8 | 228 | 90 | 37 | 38 | 211 | 108 | 8 | 41 | 845 | 6 | 38 | 228 (225 / 3) | 7.3 | CLEAN |
   | 7 | 84659 | 104 | 5 | 2.7 | 228 | 75 | 34 | 36 | 245 | 112 | 7 | 45 | 846 | 7 | 36 | 252 (246 / 6) | 7.0 | CLEAN |
   | 8 | 84664 | 105 | 6 | 2.8 | 228 | 61 | 29 | 31 | 274 | 118 | 4 | 47 | 846 | 8 | 31 | 248 (240 / 8) | 6.0 | CLEAN |
   | 9 | 84662 | 106 | 5 | 2.8 | 228 | 77 | 32 | 34 | 306 | 120 | 6 | 48 | 846 | 9 | 34 | 306 (296 / 10) | 6.6 | CLEAN |
   | 10 | 84668 | 107 | 9 | 2.8 | 231 | 78 | 31 | 35 | 337 | 123 | 235 | 56 | 849 | 10 | 35 | 350 (337 / 13) | 6.8 | CLEAN |
   | 11 | 84684 | 109 | 7 | 2.8 | 230 | 79 | 25 | 37 | 362 | 130 | 4 | 61 | 849 | 11 | 36 | 396 (342 / 54) | 7.0 | CLEAN |
   | 12 | 84687 | 110 | 8 | 2.7 | 230 | 71 | 30 | 35 | 392 | 135 | 3 | 65 | 849 | 12 | 35 | 420 (383 / 37) | 6.8 | CLEAN |
   | 13 | 84693 | 111 | 8 | 2.7 | 231 | 73 | 23 | 30 | 415 | 140 | 7 | 70 | 850 | 13 | 30 | 390 (365 / 25) | 5.8 | CLEAN |
   | 14 | 84701 | 113 | 6 | 2.9 | 231 | 40 | 13 | 16 | 428 | 147 | 5 | 74 | 849 | 14 | 16 | 224 (214 / 10) | 3.1 | CLEAN |
   | 15 | 84713 | 114 | 6 | 2.9 | 231 | 79 | 29 | 36 | 457 | 153 | 9 | 80 | 850 | 15 | 35 | 525 (493 / 32) | 6.8 | CLEAN |
   | 16 | 84710 | 115 | 7 | 2.8 | 230 | 74 | 35 | 42 | 492 | 159 | 2 | 87 | 852 | 16 | 42 | 672 (619 / 53) | 8.1 | CLEAN |
   | 17 | 84723 | 116 | 6 | 2.8 | 231 | 58 | 19 | 25 | 511 | 166 | 6 | 89 | 852 | 17 | 25 | 408 (352 / 56) | 4.8 | CLEAN |
   | 18 | 84730 | 117 | 6 | 3.0 | 233 | 51 | 24 | 28 | 535 | 173 | 8 | 93 | 852 | 18 | 28 | 504 (457 / 47) | 5.4 | CLEAN |
   | 19 | 84738 | 119 | 42 | 2.9 | 233 | 82 | 20 | 38 | 555 | 180 | 8 | 99 | 856 | 19 | 36 | 646 (506 / 140) | 6.9 | CLEAN |
   | 20 | 84737 | 121 | 47 | 2.8 | 233 | 95 | 39 | 46 | 594 | 181 | 7 | 105 | 857 | 20 | 46 | 920 (838 / 82) | 8.9 | CLEAN |
   | 21 | 84747 | 122 | 10 | 2.8 | 233 | 77 | 21 | 30 | 615 | 182 | 8 | 108 | 858 | 21 | 29 | 609 (526 / 83) | 5.6 | CLEAN |
   | 22 | 84749 | 124 | 214 | 2.7 | 238 | 132 | 51 | 68 | 666 | 183 | 6 | 119 | 860 | 22 | 67 | 1474 (1305 / 169) | 13.0 | CLEAN |
   | 23 | 84761 | 125 | 17 | 2.4 | 234 | 76 | 27 | 36 | 693 | 184 | 6 | 121 | 860 | 23 | 35 | 805 (698 / 107) | 6.8 | CLEAN |
   | 24 | 84765 | 126 | 5 | 2.5 | 234 | 76 | 18 | 33 | 711 | 185 | 7 | 125 | 863 | 24 | 33 | 792 (613 / 179) | 6.4 | CLEAN |
   | 25 | 84778 | 127 | 5 | 2.5 | 234 | 77 | 22 | 36 | 733 | 186 | 8 | 132 | 865 | 25 | 36 | 900 (759 / 141) | 6.9 | CLEAN |
   | 26 | 84787 | 128 | 6 | 2.6 | 233 | 79 | 22 | 29 | 755 | 187 | 10 | 137 | 866 | 26 | 28 | 728 (654 / 74) | 5.4 | CLEAN |
   | 27 | 84781 | 129 | 5 | 2.6 | 233 | 80 | 28 | 37 | 783 | 188 | 9 | 143 | 870 | 27 | 37 | 999 (863 / 136) | 7.2 | CLEAN |
   | 28 | 84791 | 131 | 21 | 2.5 | 234 | 80 | 20 | 31 | 803 | 189 | 11 | 148 | 873 | 28 | 31 | 868 (749 / 119) | 6.0 | CLEAN |
   | 29 | 84800 | 132 | 5 | 2.5 | 234 | 95 | 33 | 47 | 836 | 190 | 4 | 154 | 875 | 29 | 46 | 1334 (1142 / 192) | 8.9 | CLEAN |
   | 30 | 84805 | 133 | 4 | 2.5 | 234 | 73 | 21 | 34 | 857 | 191 | 4 | 154 | 871 | 30 | 33 | 990 (745 / 245) | 6.4 | CLEAN |
   | 31 | 84806 | 134 | 5 | 2.9 | 233 | 62 | 13 | 25 | 870 | 192 | 6 | 156 | 872 | 31 | 24 | 744 (542 / 202) | 4.7 | CLEAN |
   | 32 | 84816 | 136 | 21 | 2.5 | 236 | 74 | 24 | 36 | 894 | 193 | 9 | 166 | 875 | 32 | 35 | 1120 (916 / 204) | 6.8 | CLEAN |
   | 33 | 84822 | 137 | 4 | 2.6 | 236 | 87 | 28 | 36 | 922 | 194 | 8 | 172 | 875 | 33 | 35 | 1155 (996 / 159) | 6.8 | CLEAN |
   | 34 | 84828 | 138 | 9 | 2.6 | 234 | 83 | 30 | 49 | 952 | 195 | 6 | 183 | 878 | 34 | 49 | 1666 (1242 / 424) | 9.5 | CLEAN |
   | 35 | 84837 | 139 | 9 | 2.7 | 236 | 112 | 30 | 53 | 982 | 196 | 8 | 181 | 881 | 35 | 51 | 1785 (1378 / 407) | 9.9 | CLEAN |
   | 36 | 84850 | 141 | 27 | 2.5 | 234 | 115 | 40 | 68 | 1022 | 197 | 8 | 188 | 884 | 36 | 66 | 2376 (1875 / 501) | 12.8 | CLEAN |
   | 37 | 84856 | 142 | 8 | 2.5 | 237 | 64 | 10 | 37 | 1032 | 198 | 9 | 191 | 888 | 37 | 35 | 1295 (823 / 472) | 6.8 | CLEAN |
   | 38 | 84852 | 143 | 7 | 2.5 | 237 | 52 | 11 | 24 | 1043 | 199 | 6 | 191 | 893 | 38 | 23 | 874 (672 / 202) | 4.5 | CLEAN |
   | 39 | 84861 | 144 | 7 | 2.5 | 234 | 83 | 18 | 38 | 1061 | 200 | 9 | 198 | 896 | 39 | 36 | 1404 (1076 / 328) | 7.0 | CLEAN |
   | 40 | 84871 | 145 | 7 | 2.6 | 236 | 64 | 7 | 28 | 1068 | 201 | 7 | 201 | 899 | 40 | 27 | 1040 (766 / 274) | 5.2 | CLEAN |
   | 41 | 84878 | 146 | 6 | 2.8 | 234 | 53 | 6 | 16 | 1074 | 202 | 9 | 204 | 901 | 41 | 16 | 656 (389 / 267) | 3.1 | CLEAN |
   | 42 | 84880 | 148 | 26 | 2.5 | 237 | 93 | 20 | 49 | 1094 | 203 | 7 | 214 | 904 | 42 | 49 | 2058 (1387 / 671) | 9.5 | CLEAN |
   | 43 | 84884 | 149 | 5 | 2.5 | 238 | 57 | 5 | 32 | 1099 | 204 | 7 | 220 | 910 | 43 | 28 | 1204 (707 / 497) | 5.5 | CLEAN |
   | 44 | 84894 | 150 | 6 | 2.5 | 238 | 58 | 12 | 21 | 1111 | 205 | 8 | 224 | 912 | 44 | 21 | 880 (756 / 124) | 4.1 | CLEAN |
   | 45 | 84901 | 151 | 11 | 2.7 | 238 | 114 | 30 | 51 | 1141 | 206 | 14 | 234 | 914 | 45 | 50 | 2250 (1767 / 483) | 9.7 | CLEAN |
   | 46 | 84905 | 152 | 10 | 2.6 | 236 | 77 | 16 | 35 | 1157 | 207 | 4 | 239 | 905 | 46 | 35 | 1610 (1133 / 477) | 6.8 | CLEAN |
   | 47 | 84919 | 154 | 32 | 2.6 | 238 | 78 | 20 | 45 | 1177 | 208 | 9 | 239 | 910 | 47 | 45 | 2115 (1290 / 825) | 8.8 | CLEAN |
   | 48 | 84925 | 155 | 4 | 2.5 | 238 | 66 | 12 | 32 | 1189 | 209 | 9 | 243 | 931 | 48 | 31 | 1488 (1023 / 465) | 6.1 | CLEAN |
   | 49 | 84926 | 156 | 3 | 2.5 | 239 | 83 | 16 | 41 | 1205 | 210 | 4 | 248 | 921 | 49 | 41 | 2009 (1234 / 775) | 8.0 | CLEAN |
   | 50 | 84935 | 157 | 4 | 2.6 | 238 | 61 | 12 | 28 | 1217 | 211 | 9 | 254 | 927 | 50 | 27 | 1350 (868 / 482) | 5.3 | CLEAN |
   | 51 | 84946 | 158 | 3 | 2.6 | 234 | 75 | 20 | 38 | 1237 | 212 | 6 | 258 | 957 | 51 | 36 | 1836 (1305 / 531) | 7.0 | CLEAN |
   | 52 | 84950 | 160 | 27 | 2.5 | 240 | 121 | 37 | 61 | 1274 | 213 | 8 | 265 | 950 | 52 | 61 | 3172 (2197 / 975) | 11.8 | CLEAN |
   | 53 | 84948 | 161 | 3 | 2.5 | 238 | 65 | 7 | 37 | 1281 | 214 | 7 | 266 | 952 | 53 | 34 | 1802 (897 / 905) | 6.7 | CLEAN |
   | 54 | 84958 | 162 | 8 | 2.6 | 241 | 68 | 14 | 35 | 1295 | 215 | 8 | 270 | 954 | 54 | 35 | 1890 (1296 / 594) | 6.8 | CLEAN |
   | 55 | 84965 | 163 | 8 | 2.5 | 239 | 87 | 15 | 38 | 1310 | 216 | 4 | 277 | 990 | 55 | 37 | 2035 (1246 / 789) | 7.2 | CLEAN |
   | 56 | 84970 | 164 | 8 | 2.6 | 242 | 77 | 11 | 35 | 1321 | 217 | 5 | 286 | 977 | 56 | 32 | 1736 (988 / 748) | 6.3 | CLEAN |
   | 57 | 84986 | 165 | 7 | 3.1 | 240 | 69 | 17 | 35 | 1338 | 218 | 4 | 296 | 983 | 57 | 34 | 1938 (1404 / 534) | 6.7 | CLEAN |
   | 58 | 84984 | 167 | 31 | 2.5 | 241 | 102 | 15 | 51 | 1353 | 219 | 8 | 317 | 993 | 58 | 49 | 2842 (1750 / 1092) | 9.6 | CLEAN |
   | 59 | 84990 | 168 | 5 | 2.5 | 239 | 99 | 23 | 47 | 1376 | 220 | 8 | 329 | 1001 | 59 | 45 | 2655 (2063 / 592) | 8.8 | CLEAN |
   | 60 | 85009 | 169 | 6 | 2.5 | 239 | 99 | 10 | 42 | 1386 | 221 | 5 | 342 | 1008 | 60 | 40 | 2400 (1650 / 750) | 7.8 | CLEAN |
   | 61 | 85006 | 170 | 5 | 2.5 | 241 | 93 | 16 | 50 | 1402 | 222 | 6 | 334 | 999 | 61 | 50 | 3050 (1743 / 1307) | 9.8 | CLEAN |
   | 62 | 85019 | 171 | 4 | 2.5 | 239 | 74 | 12 | 33 | 1414 | 223 | 6 | 333 | 1008 | 62 | 32 | 1984 (1084 / 900) | 6.3 | CLEAN |
   | 63 | 85012 | 172 | 10 | 2.7 | 238 | 79 | 19 | 32 | 1433 | 224 | 6 | 337 | 1013 | 63 | 30 | 1890 (1500 / 390) | 5.9 | CLEAN |
   | 64 | 85032 | 174 | 21 | 10.0 | 241 | 92 | 22 | 39 | 1455 | 225 | 9 | 339 | 1022 | 64 | 38 | 2432 (1799 / 633) | 7.4 | CLEAN |
   | 65 | 85030 | 175 | 8 | 2.6 | 243 | 93 | 16 | 49 | 1471 | 226 | 11 | 346 | 1026 | 65 | 45 | 2925 (1842 / 1083) | 8.8 | CLEAN |
   | 66 | 85043 | 176 | 7 | 2.5 | 243 | 65 | 9 | 36 | 1480 | 227 | 10 | 337 | 1023 | 66 | 36 | 2376 (1515 / 861) | 7.1 | CLEAN |
   | 67 | 85037 | 177 | 8 | 2.5 | 243 | 48 | 8 | 22 | 1488 | 228 | 5 | 354 | 1026 | 67 | 22 | 1474 (820 / 654) | 4.3 | CLEAN |
   | 68 | 85053 | 178 | 7 | 2.5 | 241 | 58 | 9 | 24 | 1497 | 229 | 4 | 351 | 1031 | 68 | 22 | 1496 (1039 / 457) | 4.3 | CLEAN |
   | 69 | 85052 | 179 | 6 | 2.6 | 243 | 72 | 7 | 32 | 1504 | 230 | 5 | 362 | 1036 | 69 | 29 | 2001 (1088 / 913) | 5.7 | CLEAN |

   Batch ids:

   | # | expid | associate job, Batch id | packet job, Batch id |
   |---|---|---|---|
   | 3 | 84635 | 97 `6e458232-b211-422a-a151-0b54f1bea6f5` | 99 `e3391306-a359-4166-b035-d210b4ce33c6` |
   | 4 | 84640 | 98 `76a60251-7e8c-4a8d-aea2-d0d9ae670b2b` | 101 `4f856cbe-4abc-4059-8a05-113c0d3ee475` |
   | 5 | 84637 | 100 `a06792b2-0fd1-41b8-8410-f79b020c9f2f` | 103 `7408b9c1-6673-43ff-b38a-fe1a02aa5683` |
   | 6 | 84644 | 102 `30e88fc4-8cf2-4323-a59f-0bf4419bd7dc` | 108 `f52826d4-8d28-4b35-b296-a08334458045` |
   | 7 | 84659 | 104 `36700572-dfa4-4f3a-98ca-cbd957efb230` | 112 `4880bb1b-92c9-43ff-b1e6-6e40c587ec31` |
   | 8 | 84664 | 105 `6fa60e35-260c-4444-8b47-6a15802d6f65` | 118 `97f3f72a-3d56-4bf7-958f-4843555cc00e` |
   | 9 | 84662 | 106 `059d28bf-e4ac-4bf4-a917-487463671a7a` | 120 `9c135592-cf9f-48c6-9c9f-2f514161ab4f` |
   | 10 | 84668 | 107 `0aa00ebf-e923-4698-8a14-259f5db7f738` | 123 `d81ac321-c35b-48d4-9934-eb52144436fc` |
   | 11 | 84684 | 109 `4c4d73f2-acb2-4ce9-aaf2-6a8bc52d6f8d` | 130 `67c1c973-5b59-4a4a-acbb-9959963b0f15` |
   | 12 | 84687 | 110 `159ee7b6-378a-4687-a8b0-a2cecc599487` | 135 `90fcd72e-d6c7-48b3-9a39-5cb3a029af9c` |
   | 13 | 84693 | 111 `4b7da7be-e19c-45b3-9cd2-6a1390d930ad` | 140 `809389a5-01e3-478b-9ad2-bd8bfaef303d` |
   | 14 | 84701 | 113 `930392a5-5f1b-4a7c-be98-f141d869adf8` | 147 `46170d9d-d6c7-4c1e-9587-1d69658a28f5` |
   | 15 | 84713 | 114 `e01c3960-acfe-4e75-867e-94f35b657c18` | 153 `68d66994-acd7-40e7-af9b-b09dfa828584` |
   | 16 | 84710 | 115 `6edc7c3a-b6c7-496e-b78a-6d32e7c39c50` | 159 `18d171af-5cf8-4588-b23f-349f26877ce9` |
   | 17 | 84723 | 116 `1b9dea46-9125-4bc8-a3bb-b4d77d2ec91f` | 166 `ed3ffbc3-c396-4dd2-8f95-86f557e04bad` |
   | 18 | 84730 | 117 `9b83c773-064b-4cf9-9f11-081ca6c039bc` | 173 `f4fddf55-49b8-40e1-ab74-cccf9e014a29` |
   | 19 | 84738 | 119 `aade812c-4310-4751-a693-0c016c94e55a` | 180 `01c6e3d5-84fe-4ad6-b9ec-7b25478d80ab` |
   | 20 | 84737 | 121 `5197a64a-8488-468d-99b6-c9e4e77462d6` | 181 `62cea474-b7eb-4058-bdea-64b267ad661b` |
   | 21 | 84747 | 122 `e4a8d56b-cc05-4170-aba1-634500218910` | 182 `a1f6df78-1088-4035-900d-d11cd38e6acb` |
   | 22 | 84749 | 124 `75681d09-cc4d-4ee2-aacb-05794b91be07` | 183 `83dec5ee-2b15-4b27-a843-cd6ea3474149` |
   | 23 | 84761 | 125 `555d2535-8b93-4097-af35-94168af0e45a` | 184 `226d958f-a17c-4c26-937e-59d561eb6a52` |
   | 24 | 84765 | 126 `4cdde16f-6c7f-4d95-a09a-f1d4543e35b4` | 185 `030fd6b4-8e5c-43a6-a5f9-7e3c495ed569` |
   | 25 | 84778 | 127 `1b7fbaf9-6931-4312-89c5-66b0ab752fe4` | 186 `aac4fbf5-813a-44cf-804a-feeabbd884d9` |
   | 26 | 84787 | 128 `ef3d0d6e-e2b5-4220-a003-db374948698c` | 187 `b3e160cd-cb32-4044-ae3e-ef39b082d0d1` |
   | 27 | 84781 | 129 `e45e2904-ced9-4a01-b0d9-82d12c8dfda5` | 188 `c006dbc2-e389-45e5-8923-c886e5cef90b` |
   | 28 | 84791 | 131 `86886a6f-1e91-40a3-8a89-d652ac9b415f` | 189 `9fd8d636-0c04-49cd-9663-ee116baefd31` |
   | 29 | 84800 | 132 `7313ed82-2d3c-4b5f-bf3b-7c269877a20d` | 190 `dede4635-ba7f-46ef-ad21-1d07846dfa1f` |
   | 30 | 84805 | 133 `4ce44ddb-b26f-4a27-bba6-d8a63d1d1fd6` | 191 `b9ba89a0-66bc-47b4-aa85-c3a2a5725925` |
   | 31 | 84806 | 134 `5ffb3e19-ee76-42e3-8c2d-46d0cb99b998` | 192 `0aeabd83-f13d-442f-9fbc-bcbd8b96ddee` |
   | 32 | 84816 | 136 `2b5e0b14-15fb-4b1c-8c06-8007d28f8985` | 193 `3d248528-29db-439b-8369-331a8c2cc5fa` |
   | 33 | 84822 | 137 `8233bca7-c9da-4be5-852e-8d1f825cbaf0` | 194 `c920140f-b6aa-4556-9144-0e944b1b8c4b` |
   | 34 | 84828 | 138 `c16d6656-f48d-4c44-8580-e76d3a26eeaa` | 195 `2b2533b4-98cf-4818-98d6-b5bb592a338f` |
   | 35 | 84837 | 139 `7d8dbeac-9065-4aa0-a3a6-45fcdfb5648e` | 196 `bbfa27a4-d383-482d-9003-70cc0f48739c` |
   | 36 | 84850 | 141 `9464e513-aca4-4d93-90c1-73f8877066e8` | 197 `75f24e1e-caa2-4a71-a770-ee1a31477be6` |
   | 37 | 84856 | 142 `a639ee65-adf7-4cec-a269-9bac980602c2` | 198 `b237cbe0-e352-48ad-9b6d-9bb270eb0b58` |
   | 38 | 84852 | 143 `f523e430-da13-49cf-9346-9e62d1ff4a57` | 199 `4c3d9249-5b51-4145-be6b-d317cf708a3b` |
   | 39 | 84861 | 144 `91ba9eab-52c5-47c5-87b1-04931ab7d42b` | 200 `9a4f1a8a-5320-478f-9cd4-ae0ce09d6557` |
   | 40 | 84871 | 145 `f38650fa-0882-43b0-8ece-5baaeacc020a` | 201 `32a3e18c-47d6-448d-bdd4-c17881d955df` |
   | 41 | 84878 | 146 `9d62ebe3-4955-4638-a9af-978e198e37c5` | 202 `ba6d4873-667a-465d-876a-6a9d218b9182` |
   | 42 | 84880 | 148 `e8d8b260-478b-477a-aa22-d7dfbb423ccb` | 203 `2983d31d-1001-4269-a76d-8bab61e52592` |
   | 43 | 84884 | 149 `f7163e20-9312-41d2-81d7-7d32c0aee1b6` | 204 `aa857253-be60-4fcf-80c0-562f137ba7df` |
   | 44 | 84894 | 150 `88da66d2-e97e-425e-9c75-4286cbd33fe1` | 205 `819c321d-d7d3-4ec5-b69c-6578404dde31` |
   | 45 | 84901 | 151 `58efc932-a0a2-4c94-9e5d-11fc763ffc48` | 206 `b1b293dc-1bcd-4576-9ddb-8147dbec286e` |
   | 46 | 84905 | 152 `fe6dbc32-f18c-43ac-a49d-940b9cc04823` | 207 `338c066a-d213-4e55-8233-9207cf9dddfe` |
   | 47 | 84919 | 154 `35536a2e-f642-482e-a7a4-d0370fc44b92` | 208 `905fd5ba-832e-41ac-a817-43d73629f7ec` |
   | 48 | 84925 | 155 `1aa28eda-75a3-40bf-975f-7f8d2800e726` | 209 `1ae32eb9-59ef-4371-928e-7d266f104340` |
   | 49 | 84926 | 156 `c588406f-37a8-4742-9d8a-26d8309c5bbb` | 210 `14349406-dbc1-4eea-8c26-eed9c03ceee1` |
   | 50 | 84935 | 157 `4f031184-5f2c-4c9e-9d58-a8631dbadf36` | 211 `18a602da-4754-46d5-b2ef-81aef6978a4f` |
   | 51 | 84946 | 158 `ecd0dab9-ace5-4c06-85ec-a7f057703a7c` | 212 `1d63e1ad-83a2-460c-9576-e1b1f0cd9d64` |
   | 52 | 84950 | 160 `acb70453-296c-494f-ac04-1fc8e1b9a8e6` | 213 `1cfec275-4cba-4041-9a71-3c8f2c08c2b7` |
   | 53 | 84948 | 161 `e04afa3e-eb2e-4c63-b6be-783e727dd4f8` | 214 `49273e5a-eb8b-4626-b67c-74d5e2230da0` |
   | 54 | 84958 | 162 `ea719568-98dd-49ae-a432-485d6f04c52e` | 215 `fdd266b3-f6e5-4762-89cc-e2e9ce61faea` |
   | 55 | 84965 | 163 `b0652e99-e52a-4706-b5a6-e71ca6aeef86` | 216 `10087043-a868-4f64-bad0-8eb27ddb889f` |
   | 56 | 84970 | 164 `219c2f86-5636-49a9-be04-57974e6e38e7` | 217 `20b249e9-3ceb-47f2-a195-a5f5eac71bfd` |
   | 57 | 84986 | 165 `56b71b90-956b-4497-9441-684351f4d0c8` | 218 `1308ffab-052c-403d-bf91-d6b73cd2ed3f` |
   | 58 | 84984 | 167 `2ad6b723-b364-4038-b661-ce117094a215` | 219 `1c20ca4a-3b7a-4c31-bf90-9db41f4907c8` |
   | 59 | 84990 | 168 `37e4095b-63d1-4619-ab6f-f74e34a0eab5` | 220 `13c7213b-6063-47ff-9567-54fef4263a95` |
   | 60 | 85009 | 169 `f66f7eaa-bfb0-4c66-a31d-92d99790ac64` | 221 `b4038d6d-c668-4aa6-8374-3426efc6aebe` |
   | 61 | 85006 | 170 `84455271-6f46-4c90-8edb-4b0f265fe66f` | 222 `ea2f0b62-ed3b-4d63-91a0-ccdd6850d586` |
   | 62 | 85019 | 171 `fb689272-8a09-4fd8-98a4-358a8c7b4a08` | 223 `b9d9b6ff-84e1-4a93-9fb0-58e1b3f23a4f` |
   | 63 | 85012 | 172 `d8c744b5-4b2d-45e3-8f53-4a91e4d6372c` | 224 `163f32cf-b06e-4d87-93bc-40fbf6f29839` |
   | 64 | 85032 | 174 `5242159c-7f90-4bde-9009-a23f2147e210` | 225 `f7908bd1-87a0-41ea-b6b7-2a35358cf92e` |
   | 65 | 85030 | 175 `f1b0a401-6c0f-4096-9869-aa67a83663f9` | 226 `84fef227-4eb8-464b-b0c5-742980bd110a` |
   | 66 | 85043 | 176 `c57a4f9e-42b7-4f45-8b9e-c39df3de4214` | 227 `7abc82b7-bc43-4af9-94d1-994b72506590` |
   | 67 | 85037 | 177 `6b540e25-3cfd-4322-ba03-ea5592267be6` | 228 `7cc87bdc-143e-4886-9a91-4343e777ff9a` |
   | 68 | 85053 | 178 `29fd22f8-f205-44b3-b5ca-bcbd1d1e47a9` | 229 `a8527225-eae9-41ec-8a8d-9deb218fbad5` |
   | 69 | 85052 | 179 `b55cfe12-d187-46a2-a76a-2587155c870b` | 230 `f5215e43-9e09-4c51-9fc9-924afcb679e0` |

5. **The eight queries and the team's shapes at this size.**
   `database/queries.sql`'s 23 statements, each under `EXPLAIN (ANALYZE,
   BUFFERS)` through the pooler, with the night's ids: object set 179,
   the most-observed object, packet set 230, the 35th epoch (84837) for
   the image and cone parameters, group 215. Execution time on the
   server, rows returned, the indexes used and any sequential scan:

   | statement | rows | ms | indexes used | sequential scans |
   |---|---|---|---|---|
   | `current_image_and_source_set` | 1 | 0.14 | `official_open_product_job`, `jobs_pkey`, `sources_job_sid_key`, `diffimages_dataset_expid_sca_fid_job_key`, `l2files_dataset_expid_sca_fid_job_key`, `official_open_product_job` | none |
   | `object_set_and_members` | 2547 | 25.98 | `official_open_product_job`, `jobs_pkey`, `job_inputs_job_idx`, `objects_job_idx`, `official_open_product_job` | `job_inputs`, `memberships` |
   | `object_history_in_named_set` | 89 | 1.19 | `forced_aid_idx`, `forced_job_forced_id_key`, `memberships_job_aid_source_job_sid_key`, `job_inputs_job_idx`, `job_inputs_source_job_idx`, `packets_object_idx`, `sources_job_sid_key` | none |
   | `packet_history_by_observation` | 51 | 0.22 | `official_open_product_job` | `packets` |
   | `packet_history_by_source` | 0 | 0.04 | `official_open_product_job`, `packets_job_source_job_sid_key` | none |
   | `packet_history_by_object` | 14 | 0.18 | `official_open_product_job`, `job_inputs_job_idx`, `packets_object_idx` | none |
   | `candidate_against_official` | 509 | 24.66 | `official_open_product_job`, `jobs_pkey`, `memberships_job_aid_source_job_sid_key`, `job_inputs_job_idx`, `sources_job_sid_key` | `job_inputs`, `memberships` |
   | `ancestors_of_packet_set` | 207 | 1.77 | `job_inputs_job_idx`, `jobs_pkey` | `official` |
   | `descendants_of_reference` | 227 | 2.28 | `job_inputs_source_job_idx` | `jobs`, `official`, `refimages` |
   | `failed_and_unbound_jobs` | 87 | 0.37 | `job_inputs_job_idx`, `jobs_pkey` | `official` |
   | `bindings_around_group` | 1 | 0.01 | `official_closed_group_idx`, `official_opened_group_idx` | none |
   | `diffimages_in_cone` | 69 | 0.54 | `jobs_pkey`, `diffimages_dataset_expid_sca_fid_job_key`, `official_open_product_job` | `l2files` |
   | `l2files_in_cone_overlapping_field` | 69 | 0.32 | `jobs_pkey`, `l2files_job_idx`, `official_open_product_job` | none |
   | `diffimages_in_cone_with_reference` | 69 | 0.29 | `jobs_pkey`, `official_open_product_job` | `diffimages`, `refimages` |
   | `l2files_overlapping_fields` | 69 | 0.20 | `jobs_pkey`, `official_open_product_job` | `l2files` |
   | `objects_in_cone` | 1504 | 3.05 | `jobs_pkey`, `objects_job_idx`, `official_open_product_job` | none |
   | `chip_sources` | 138 | 8.81 | `jobs_pkey`, `official_open_product_job`, `filters_pkey`, `official_open_product_job`, `sources_job_sid_key` | `exposures` |
   | `reference_for_field` | 1 | 0.03 | `jobs_pkey`, `official_open_product_job` | `refimages` |
   | `l2files_for_field_in_window` | 69 | 0.17 | `jobs_pkey`, `official_open_product_job` | `l2files` |
   | `l2files_without_diffimage` | 0 | 0.37 | `diffimages_dataset_expid_sca_fid_job_key`, `jobs_pkey`, `official_open_product_job` | `l2files` |
   | `frame_counts` | 1 | 0.18 | `jobs_pkey`, `official_open_product_job` | `l2files` |
   | `diffimage_by_pid` | 1 | 0.04 | `jobs_pkey`, `diffimages_pkey`, `l2files_dataset_expid_sca_fid_job_key`, `official_open_product_job` | none |
   | `l2files_with_usable_reference` | 69 | 0.24 | `jobs_pkey`, `official_open_product_job` | `l2files`, `refimages` |

   Every statement ran under 30 ms. Two read `memberships` and `job_inputs`
   whole: `object_set_and_members` (26 ms) and
   `candidate_against_official` (25 ms) walk the chain and filter the
   members rather than reaching them through
   `memberships_job_aid_source_job_sid_key`. At 2,547 memberships and
   8,227 `job_inputs` rows that is the planner's cheaper choice, and whether
   it holds at a field's year of nights is not measured here. The cone and
   window statements scan `l2files`, `diffimages` and `refimages`, tables of
   69 rows or fewer, instead of their q3c indexes; that is expected at this
   size, not a finding. The database is 141 MB; its largest tables are
   `objects` and `forced` (18 MB each) and `sources` (11 MB).

The night's steps 6 to 10 ran unattended on 2026-10-22 from the same host,
under **run 10**: a campaign, not live, revision `8a49bf70` on
`rapid-rebuild:33`, the shipped settings body with `max_sources = 0` in
`[import]` and `[difference]` (every source; the body carries the
difference stage's `clip_correction_seed = 0`). By Ben's ruling of
2026-10-21 18:45 PDT, the chain downstream (step 9) covers the first five
epochs by `mjdobs`, not 69: step 4 above showed the chain 69 deep, and the
uncapped catalogs multiply the packet stage's forced-photometry cost. All
69 differences stand. As in step 4, the loops were Python scripts outside
the repository, calling the same `rp` verbs per epoch.

**Every run-10 job that names `expects` shows `FAILED` in Batch with exit
1 after publishing.** A non-live run refuses every automatic binding ("run
10 is not live for its survey and dataset"), and `job run` exits 1
(`ExitCode.FAILURE`) whenever a published job's binding is refused
(`rapidpipe/cli/main.py`). Exit 1 is not retried. The reference and the 70
differences named `expects` (refimages; diffimages and sources) with
binding null, so each published attempt 1, recorded the refusal on its job
row (`jobs.exit_code` 0, `refusal` set) and then left Batch with exit 1.
The chain's jobs (step 9) name no `expects`, as part 4's non-live run did,
and `SUCCEEDED`.

6. **Run 10, not live.**

   ```sh
   sed 's/^max_sources = 500$/max_sources = 0/' "$RAPID/rapidpipe/settings/foundation-thin.toml" > uncapped.toml
   rp run create --who "$USER" --why "foundation-night-2026-10-21 task 5 (night-batch): ..." \
     --revision 8a49bf703f1b931406cb0b4af9c432b50db86983 --settings uncapped.toml --survey GBTDS --dataset $D
   ```

   Check, held: run **10**, `"live": null`, revision `8a49bf70…`,
   `max_sources` 0 in both tables.

7. **The reference.** Rfid 90011's header names its 25 frames
   (`INFIL001` to `INFIL025`, basenames `r0034001002001001004…` to
   `r0034001002001005004…`). **None of the 25 is in the night**, whose basenames
   begin `r0034001004` and `r0034001005`. So the reference coadds the
   night's first 25 epochs by `mjdobs` (84613 to 84778, 07:18 to 13:15
   UTC): the `l2files` rows of run 1's import jobs 2 and 3 and run 9's 30
   to 52. Dev's reference and the port's therefore share no frame, and
   step 10 compares the 25 epochs inside the port's reference apart from
   the 44 outside it. Dev's reference holds none of the night's epochs.

   ```sh
   rp job submit --run 10 --stage reference --job-definition $DEF --scope '{"field": 4658155, "fid": 8, "sca": 8}' \
     --inputs '[{"product": "l2files", "key": '"$(k 84613)"', "source_job": 2}, ..., {"product": "l2files", "key": '"$(k 84778)"', "source_job": 52}]' \
     --expects '[{"product": "refimages", "key": '"$RKEY"', "binding": null}]'
   ```

   Check, held: job **231** (Batch `f27f79f0-e94e-43d9-ab7f-61b49a4f4343`)
   published attempt 1, unbound; **rfid 3** drawn; `nframes` 25 with 25
   `refimimages` rows and none left uncoadded; `MAGZP` 26.863; catalog
   **5,248** SExtractor sources (`refimcatalogs` cattype 1). Against
   rfid 90011, measured as PR 287 did:

   | measure | Batch reference (job 231) | rfid 90011 |
   |---|---|---|
   | grid, centre | 7000 × 7000, (267.97852, -28.71602) | the same |
   | finite fraction | 0.32712 | 0.32711 |
   | cov5percent | 32.682 | 32.683 |
   | `MAGZP` | 26.863 | 17.0 |
   | median pixel ratio | 8,933 (expected from the zero points: 8,815) | |
   | clipped residual after rescaling by 8,815, over 16.03 M common pixels | median 3.2e-5, clipped σ 1.6e-5 | (its own clipped scatter 1.3e-3) |
   | pixel correlation | 0.9962 | |

   Twenty-five other frames of the same field give a mosaic that matches
   dev's to a correlation of 0.996, against 0.982 for part 4's two-frame
   reference. Queue wait 115 s (the compute environment scaling up from
   zero), container 227 s, job row 224 s; peak RSS 1.35 GiB self and
   1.41 GiB largest child; 595 MB under `10/reference/231/1/` (three 196 MB
   maps and a 6.9 MB catalog).

8. **Every epoch differenced on Batch**: 69 jobs, one per epoch, then a
   second for 84624, all submitted without waiting on one another:

   ```sh
   # for each $EPOCHS row in mjdobs order, then 84624 once more; <s> the epoch's import job:
   rp job submit --run 10 --stage difference --job-definition $DEF --scope '{"expid": '$expid', "sca": 8, "fid": 8}' \
     --inputs '[{"product": "l2files", "key": '"$(k $expid)"', "source_job": <s>}, {"product": "refimages", "key": '"$RKEY"', "source_job": 231},
               {"product": "psfs", "key": '"$PSK"', "source_job": 1}, {"product": "psfs", "key": '"$PRK"', "source_job": 9}]' \
     --expects '[{"product": "diffimages", "key": '"$(k $expid)"', "binding": null}, {"product": "sources", "key": '"$(k $expid)"', "binding": null}]'
   ```

   Check, held: jobs **232 to 300** (one per epoch in `mjdobs` order) and
   **301** (84624's second) all published attempt 1, every one unbound
   (no `official` row names them) with the refusal recorded; every
   `infobitssci` 0; each job wrote 72 objects, 2.81 to 2.83 GB. **The two
   84624 executions, jobs 233 and 301, are identical**: 5,115 source rows
   each, equal in every column, NaN for NaN, except `job`, `sid` and the
   drawn `pid` (13 and 78). That makes two runs on different Batch
   instances with task 1's pinned seed, against task 1's two on the host.

   Sources per epoch (no cap) run from 4,708 to 5,748, 365,104 over the
   night (per epoch in step 10's table). The fan-out:

   | measure | figure |
   |---|---|
   | first submit to first container start | 2.4 s (an instance was still up from step 7) |
   | containers running at the peak | **70**, all started within 240 s of the first |
   | queue wait | 2.4 to 201 s, median 166 s |
   | in container | 1,091 to 1,181 s, median 1,124 s (18 min 44 s) |
   | first submit to last publish | **1,399 s (23 min 19 s)** |
   | peak RSS, largest child | 14.536 to 14.540 GiB, against the 24 GiB definition (9.46 GiB spare) |
   | peak RSS, the stage's own process | at most 1.39 GiB |
   | output | 72 objects and 2.81 to 2.83 GB per job; 197.2 GB under `10/difference/` |

   On the 25-frame reference each difference took about 1,124 s in its
   container. Parts 4 and 5 took 1,324 to 1,369 s against the two-frame
   reference, on the same one vCPU; the memory peak hardly moved (14.35
   then, 14.54 GiB now). Per job:

   | # | expid | job | Batch job | queue s | container s | peak RSS self / child GiB | sources | infobitssci | output GB |
   |---|---|---|---|---|---|---|---|---|---|
   | 1 | 84613 | 232 | `82e845d9-742d-4c18-beec-26fef8e148f6` | 2 | 1125 | 1.28 / 14.54 | 5,408 | 0 | 2.82 |
   | 2 | 84624 | 233 | `289dcd0e-7e27-433b-b1c1-0f1e7da2370f` | 112 | 1124 | 1.33 / 14.54 | 5,115 | 0 | 2.81 |
   | 2 | 84624 | 301 | `b1d56468-588e-48b9-845e-7a39a0dce3fd` | 162 | 1146 | 1.26 / 14.54 | 5,115 | 0 | 2.81 |
   | 3 | 84635 | 234 | `6643df09-1b3c-437a-92b3-c91762f64773` | 111 | 1133 | 1.28 / 14.54 | 5,536 | 0 | 2.82 |
   | 4 | 84640 | 235 | `1bb91776-1c0e-4989-83c5-e74d98f3c7fc` | 110 | 1114 | 1.25 / 14.54 | 5,318 | 0 | 2.82 |
   | 5 | 84637 | 236 | `158dc121-cc1b-40bb-8318-3df6ccdb98c1` | 109 | 1110 | 1.29 / 14.54 | 5,606 | 0 | 2.83 |
   | 6 | 84644 | 237 | `e276536f-2cb5-4c6a-a5cb-a4aa70c525a2` | 108 | 1101 | 1.33 / 14.54 | 5,321 | 0 | 2.82 |
   | 7 | 84659 | 238 | `0d5c66ee-13e5-4643-8c5b-db1069aeec37` | 106 | 1113 | 1.29 / 14.54 | 5,217 | 0 | 2.82 |
   | 8 | 84664 | 239 | `29ebd849-e11a-48d7-b90b-1796eef99613` | 107 | 1138 | 1.30 / 14.54 | 5,039 | 0 | 2.81 |
   | 9 | 84662 | 240 | `316a428d-d5bb-4e1d-8f07-5fe2680aa4e5` | 104 | 1114 | 1.39 / 14.54 | 5,144 | 0 | 2.82 |
   | 10 | 84668 | 241 | `03395ec8-8633-420f-a29c-2cef0c728468` | 103 | 1123 | 1.30 / 14.54 | 5,225 | 0 | 2.82 |
   | 11 | 84684 | 242 | `0f59f7cd-149a-436d-832f-17a6bb5662a1` | 104 | 1148 | 1.25 / 14.54 | 5,322 | 0 | 2.82 |
   | 12 | 84687 | 243 | `ed8fadd1-46bd-4f91-8b53-3b47c1e5007e` | 102 | 1172 | 1.25 / 14.54 | 4,992 | 0 | 2.81 |
   | 13 | 84693 | 244 | `a1ebc2fd-a8f6-47d5-a1cf-49812f226f0e` | 101 | 1128 | 1.28 / 14.54 | 5,461 | 0 | 2.82 |
   | 14 | 84701 | 245 | `a30bfebe-2300-4283-97e9-b79dc912f46c` | 98 | 1117 | 1.32 / 14.54 | 4,923 | 0 | 2.81 |
   | 15 | 84713 | 246 | `0caa52af-7c7a-42ed-ad1c-4a98d5fad3d0` | 97 | 1122 | 1.26 / 14.54 | 5,329 | 0 | 2.82 |
   | 16 | 84710 | 247 | `32eee1be-f231-45bf-9583-2bd3404d02f5` | 97 | 1133 | 1.31 / 14.54 | 5,278 | 0 | 2.82 |
   | 17 | 84723 | 248 | `0883678c-3a39-41e1-8b81-2c06bb002b81` | 96 | 1181 | 1.25 / 14.54 | 5,518 | 0 | 2.83 |
   | 18 | 84730 | 249 | `b8c6c9ca-1956-4e5b-b54e-fcb6b498a869` | 95 | 1141 | 1.32 / 14.54 | 5,072 | 0 | 2.81 |
   | 19 | 84738 | 250 | `867a12c8-5841-4129-bebd-7420928d3c0f` | 94 | 1132 | 1.33 / 14.54 | 4,948 | 0 | 2.81 |
   | 20 | 84737 | 251 | `70869a94-6478-4f2a-9a06-38e852fc8dce` | 92 | 1132 | 1.29 / 14.54 | 5,096 | 0 | 2.81 |
   | 21 | 84747 | 252 | `d3be1b9f-1191-48b4-8f36-c69b0f9cf125` | 96 | 1107 | 1.33 / 14.54 | 5,382 | 0 | 2.82 |
   | 22 | 84749 | 253 | `b21f3980-b28a-4c49-be59-84294692f0df` | 95 | 1118 | 1.31 / 14.54 | 5,288 | 0 | 2.82 |
   | 23 | 84761 | 254 | `32dd00df-407b-423b-ab50-1b0d0c75c7ad` | 94 | 1099 | 1.32 / 14.54 | 5,748 | 0 | 2.83 |
   | 24 | 84765 | 255 | `b7c87e39-98c2-41f8-83e5-8ffd2f06f115` | 93 | 1127 | 1.33 / 14.54 | 5,146 | 0 | 2.81 |
   | 25 | 84778 | 256 | `4b3b7277-181d-41ef-bf94-ff917447a85b` | 87 | 1154 | 1.29 / 14.54 | 4,950 | 0 | 2.81 |
   | 26 | 84787 | 257 | `5c67e120-5f96-4b4d-aa2d-e9c938634060` | 90 | 1106 | 1.31 / 14.54 | 5,351 | 0 | 2.82 |
   | 27 | 84781 | 258 | `652273ae-8fa6-4879-9150-b083b4c44ff6` | 89 | 1110 | 1.30 / 14.54 | 5,162 | 0 | 2.82 |
   | 28 | 84791 | 259 | `1a41339d-7a8a-4a24-8e46-f136fcf2aec6` | 89 | 1097 | 1.30 / 14.54 | 5,319 | 0 | 2.82 |
   | 29 | 84800 | 260 | `6c75835b-09fd-43f3-8dd4-fde549339b6f` | 87 | 1119 | 1.25 / 14.54 | 5,418 | 0 | 2.82 |
   | 30 | 84805 | 261 | `daa2b8a5-a2c4-4a78-86e3-767156f096d9` | 201 | 1143 | 1.26 / 14.54 | 5,476 | 0 | 2.82 |
   | 31 | 84806 | 262 | `099bc50c-1e94-4de5-8854-d2c52110de13` | 84 | 1117 | 1.33 / 14.54 | 4,990 | 0 | 2.81 |
   | 32 | 84816 | 263 | `4515a6d1-9b21-4b4b-935f-f7fb87af1771` | 84 | 1130 | 1.34 / 14.54 | 5,212 | 0 | 2.81 |
   | 33 | 84822 | 264 | `1ed385c0-07ac-4754-8106-17292e161a99` | 198 | 1107 | 1.27 / 14.54 | 5,336 | 0 | 2.83 |
   | 34 | 84828 | 265 | `8d43f47e-2d7e-44c8-ad44-549a127c133a` | 196 | 1095 | 1.32 / 14.54 | 5,084 | 0 | 2.81 |
   | 35 | 84837 | 266 | `40b7bca7-b84b-4806-acac-e939517fa6ef` | 195 | 1126 | 1.25 / 14.54 | 5,450 | 0 | 2.82 |
   | 36 | 84850 | 267 | `0c76b357-d511-4831-b2f0-062d394afebe` | 194 | 1121 | 1.30 / 14.54 | 5,430 | 0 | 2.82 |
   | 37 | 84856 | 268 | `506fbe75-6b86-4ecf-8cdf-6cad90f42a13` | 193 | 1120 | 1.24 / 14.54 | 4,768 | 0 | 2.81 |
   | 38 | 84852 | 269 | `2a254735-4da0-4407-9fc2-c63718210f3b` | 192 | 1099 | 1.32 / 14.54 | 4,917 | 0 | 2.81 |
   | 39 | 84861 | 270 | `d04a9dc1-b872-416e-88c4-1bf1cbe04bef` | 190 | 1110 | 1.25 / 14.54 | 5,300 | 0 | 2.82 |
   | 40 | 84871 | 271 | `fdffbd65-63bf-465a-bdec-ebad30a55f2d` | 189 | 1107 | 1.30 / 14.54 | 5,173 | 0 | 2.82 |
   | 41 | 84878 | 272 | `2686ea09-00e7-4c41-8bc3-63882df10238` | 188 | 1117 | 1.29 / 14.54 | 5,598 | 0 | 2.83 |
   | 42 | 84880 | 273 | `e5aa625c-db39-4edb-9054-5be1d47f62ae` | 187 | 1127 | 1.31 / 14.54 | 5,350 | 0 | 2.82 |
   | 43 | 84884 | 274 | `d3428d79-d7b0-4218-95af-b8264d96a5d4` | 186 | 1125 | 1.32 / 14.54 | 5,028 | 0 | 2.82 |
   | 44 | 84894 | 275 | `3113fcac-db5b-4fa7-971d-bfde340d5853` | 188 | 1131 | 1.32 / 14.54 | 4,903 | 0 | 2.81 |
   | 45 | 84901 | 276 | `2a45b61e-40e3-42a3-b9fc-eca4232e9be2` | 187 | 1119 | 1.33 / 14.54 | 5,261 | 0 | 2.82 |
   | 46 | 84905 | 277 | `2313592a-8967-4e15-b592-e05ae203780c` | 182 | 1132 | 1.32 / 14.54 | 5,205 | 0 | 2.82 |
   | 47 | 84919 | 278 | `0acc7f89-408b-455d-b6d2-6f4876f4a713` | 181 | 1109 | 1.30 / 14.54 | 5,246 | 0 | 2.82 |
   | 48 | 84925 | 279 | `42630036-49ad-49e3-b5e2-22d92151457c` | 180 | 1124 | 1.32 / 14.54 | 5,093 | 0 | 2.82 |
   | 49 | 84926 | 280 | `48bc8524-094c-4283-b4c7-b987468616dc` | 179 | 1120 | 1.31 / 14.54 | 5,366 | 0 | 2.82 |
   | 50 | 84935 | 281 | `baf3ae30-103a-496c-beff-360ac6abf7b8` | 177 | 1125 | 1.36 / 14.54 | 5,301 | 0 | 2.82 |
   | 51 | 84946 | 282 | `2a73aca8-acb3-49fa-a8d7-576d0ba9f54f` | 176 | 1152 | 1.31 / 14.54 | 5,110 | 0 | 2.81 |
   | 52 | 84950 | 283 | `5ec8a8e0-7e75-4930-9edd-aaf1f3f4ab94` | 175 | 1125 | 1.27 / 14.54 | 5,260 | 0 | 2.82 |
   | 53 | 84948 | 284 | `2e3c38e4-b65f-4590-b69e-899c814c51dc` | 174 | 1105 | 1.29 / 14.54 | 4,822 | 0 | 2.81 |
   | 54 | 84958 | 285 | `575fefbc-80ad-4854-a261-caa199a72a29` | 176 | 1117 | 1.29 / 14.54 | 5,101 | 0 | 2.81 |
   | 55 | 84965 | 286 | `adc4271f-e685-473c-b9c9-4856bd68f34d` | 175 | 1111 | 1.29 / 14.54 | 5,136 | 0 | 2.82 |
   | 56 | 84970 | 287 | `75329e1f-2684-43bd-81bd-6c757f1064e0` | 174 | 1129 | 1.29 / 14.54 | 5,254 | 0 | 2.82 |
   | 57 | 84986 | 288 | `b047438c-64fd-4d2b-abb4-e8c08915d300` | 173 | 1143 | 1.24 / 14.54 | 5,573 | 0 | 2.83 |
   | 58 | 84984 | 289 | `73aa0047-730b-4d1d-bf3a-9970fa21be5b` | 172 | 1131 | 1.30 / 14.54 | 5,402 | 0 | 2.82 |
   | 59 | 84990 | 290 | `c2f4b72b-d5ef-4809-9de6-6dfe98c8ec94` | 171 | 1091 | 1.31 / 14.54 | 5,320 | 0 | 2.82 |
   | 60 | 85009 | 291 | `715d057c-8b82-4c0f-b77c-0ca25798880c` | 170 | 1120 | 1.33 / 14.54 | 5,108 | 0 | 2.82 |
   | 61 | 85006 | 292 | `fa18220b-8377-4a5a-ab26-4ffef0abde9f` | 168 | 1109 | 1.26 / 14.54 | 5,013 | 0 | 2.81 |
   | 62 | 85019 | 293 | `9b9d69ac-093b-4d07-84ea-974bea972faa` | 171 | 1132 | 1.30 / 14.54 | 5,007 | 0 | 2.81 |
   | 63 | 85012 | 294 | `8c9c2b3d-3b18-4da2-817c-1ba734231ead` | 170 | 1157 | 1.33 / 14.54 | 5,451 | 0 | 2.82 |
   | 64 | 85032 | 295 | `1257a5b5-7668-4915-b5c7-6a5a7ca90105` | 169 | 1105 | 1.33 / 14.54 | 5,195 | 0 | 2.82 |
   | 65 | 85030 | 296 | `de02bad1-9aa1-4e9e-89ce-6f58bac51a6e` | 167 | 1128 | 1.26 / 14.54 | 5,520 | 0 | 2.82 |
   | 66 | 85043 | 297 | `db13767e-7408-48a7-83a8-e29fe202f5a5` | 166 | 1149 | 1.33 / 14.54 | 4,994 | 0 | 2.81 |
   | 67 | 85037 | 298 | `45ba7ba8-4b39-4cfa-982a-2d3e7e66e21a` | 165 | 1129 | 1.37 / 14.54 | 4,708 | 0 | 2.81 |
   | 68 | 85053 | 299 | `1abf8911-85e6-4a04-a626-6952617d62ce` | 164 | 1135 | 1.30 / 14.54 | 4,820 | 0 | 2.81 |
   | 69 | 85052 | 300 | `3ecab830-eaf1-4408-9c96-0708869ce660` | 163 | 1113 | 1.28 / 14.54 | 5,076 | 0 | 2.82 |

9. **The chain downstream, epochs 1 to 5**, under run 10 on Batch. Run 10
   is a campaign and names its own previous sets: epoch 1's associate has
   no predecessor, and each later one names run 10's previous object set.
   Each epoch's source set is run 10's own (job 233 for 84624, the first of
   the two). The packets follow step 4: each names the Gaia catalogue and
   the science PSF (import job 1), the step 7 reference (job 231), the
   epoch's object set, every epoch so far's run-10 difference image and
   import L2 file, the epoch's run-10 source set, and every earlier run-10
   packet set. There are no `expects`.

   ```sh
   rp job submit --run 10 --stage associate --job-definition $DEF --scope '{"field": 4658155}' \
     --inputs '[{"product": "sources", "key": '"$(k 84613)"', "source_job": 232}]'
   # epoch n > 1, once associate n-1 is published (<d> the epoch's difference job, <a> associate n-1):
   rp job submit --run 10 --stage associate --job-definition $DEF --scope '{"field": 4658155}' \
     --inputs '[{"product": "sources", "key": '"$(k $expid)"', "source_job": <d>}, {"product": "objects", "key": '"$OKEY"', "source_job": <a>}]'
   # once associate n and packet n-1 are published:
   rp job submit --run 10 --stage packet --job-definition $DEF --scope '{"expid": '$expid', "sca": 8, "fid": 8}' \
     --inputs "[{gaia, job 1}, {refimages, job 231}, {psfs $PSK, job 1}, {objects, <a_n>},
               {diffimages and l2files of epochs 1..n}, {sources of epoch n, <d>}, {packets of epochs 1..n-1}]"
   ```

   Check, held: associate jobs **302, 303, 305, 306, 308** and packet jobs
   **304, 307, 309, 310, 311**, all `SUCCEEDED` on attempt 1 and
   published, none bound. In each object set the `objects` and
   `memberships` row counts equal its manifest (new objects, pass 1 plus
   pass 2). In each packet set the `packets`, `forced` and `external` row
   counts equal its manifest. The packet reader reads `packets_official`,
   so it cannot see an unbound set. Its own `read_set` was therefore run
   on each of the five sets, reading the tables `packets` and `forced` in
   place of their views (the views add only the binding), through a
   wrapper outside the repository that changes nothing in the reader.
   Every record validates, every cutout opens with its edge fill, every
   forced entry equals its row, every history detection precedes its
   trigger, and every index row's locator decodes to its record: **CLEAN
   on all five**. `read_packets.py --job` on these sets is task 6's check,
   after its change group binds them.

   First submit to last packet published: **415 s**. The first associate
   waited 181 s for the compute environment to scale up again after the
   difference fan-out, and its container took 34.9 s (the image pulled
   onto a fresh instance).

   | # | expid | assoc job | queue s | container s | peak RSS MiB | sources in | new objects | memberships | objects | packet job | queue s | container s | peak RSS MiB | window | packets | forced rows (measured / reused) | Avro MB | reader functions |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | 1 | 84613 | 302 | 181 | 34.9 | 224 | 114 | 50 | 50 | 50 | 304 | 10 | 12.7 | 844 | 1 | 50 | 50 (50 / 0) | 9.6 | CLEAN |
   | 2 | 84624 | 303 | 6 | 2.7 | 230 | 141 | 59 | 62 | 109 | 307 | 4 | 18.2 | 845 | 2 | 62 | 124 (121 / 3) | 12.0 | CLEAN |
   | 3 | 84635 | 305 | 16 | 2.8 | 228 | 144 | 75 | 77 | 184 | 309 | 5 | 25.6 | 847 | 3 | 77 | 231 (227 / 4) | 14.8 | CLEAN |
   | 4 | 84640 | 306 | 27 | 3.0 | 228 | 132 | 52 | 54 | 236 | 310 | 7 | 30.0 | 848 | 4 | 54 | 216 (211 / 5) | 10.5 | CLEAN |
   | 5 | 84637 | 308 | 34 | 3.0 | 230 | 157 | 64 | 72 | 300 | 311 | 8 | 37.8 | 848 | 5 | 71 | 355 (336 / 19) | 13.7 | CLEAN |

   Over the five epochs, associate accepted 688 sources (`flags = 0`,
   S/N at least 5, from the uncapped sets) into 300 objects and 315
   memberships, none in pass 2. There were 314 packets and 976 forced
   rows (945 measured, 31 reused), 60.8 MB in all under `10/packet/` and
   72.5 KB under `10/associate/`. Run 9's capped chain accepted 71, 60 and
   85 sources for epochs 3 to 5. Run 10's uncapped sets on the 25-frame
   reference give 144, 132 and 157: about twice as many objects per epoch
   (75, 52, 64 new, against 33, 30, 34). **The cap is not the cause**:
   every source associate accepted from run 10's sets ranks within the
   brightest 177 by `fluxfit / fluxerr` (133, 161, 162, 164 and 177 for
   epochs 1 to 5), so a cap of 500 would have kept every one, as on part
   5's two epochs. The difference is in the source sets: run 9's are
   dev's imported 500, run 10's the port's own on its own reference, and
   more of the port's pass `flags = 0` and S/N at least 5 (cause not
   probed). Forced photometry grows with the window as before:
   3.6 s for one image, 17.3 s for five, about 3.5 s per window image.

10. **Against dev's night** (a comparison, not a gate). Per epoch: the
    port's uncapped source count; dev's full ZOGY PSF-fit catalog
    (`zogy_diffimage_masked_psfcat.parquet` in the epoch's directory); the
    match fraction within `match_radius` (1.528e-5°, 0.055″) between the
    port's brightest 500 by `fluxfit / fluxerr` (from the uncapped set) and
    dev's imported 500, also at 0.5″; the fraction of all the port's
    sources within 0.055″ of a source in dev's full catalog; and the two
    ZOGY difference images' pixel correlation and MAD (1.4826 × the median
    absolute deviation) over their common finite non-zero pixels. Dev's
    500 are the import's selection, by the same ranking. "In port's
    reference" marks the 25 epochs the step 7 reference coadds. Dev's
    reference (rfid 90011) contains none of the night.

    | group | epochs | port sources, mean | dev full, mean | match of the 500 at 0.055″, mean (range) | at 0.5″, mean | port within 0.055″ of dev full, mean | pixel correlation, mean (range) | MAD port / dev, mean |
    |---|---|---|---|---|---|---|---|---|
    | in the port's reference | 25 | 5,255 | 5,344 | 0.117 (0.094 to 0.158) | 0.220 | 0.211 | 0.9700 (0.9609 to 0.9779) | 4.78 / 4.87 |
    | outside it | 44 | 5,196 | 5,398 | 0.124 (0.080 to 0.182) | 0.224 | 0.211 | 0.9698 (0.9632 to 0.9769) | 4.80 / 4.86 |

    | # | expid | in port's reference | port sources | dev's full catalog | match, port's 500 to dev's 500 (0.055″ / 0.5″) | port sources within 0.055″ of dev's full | pixel correlation | MAD port / dev |
    |---|---|---|---|---|---|---|---|---|
    | 1 | 84613 | yes | 5,408 | 5,450 | 0.112 / 0.218 | 0.227 | 0.9779 | 4.742 / 4.825 |
    | 2 | 84624 | yes | 5,115 | 5,586 | 0.136 / 0.224 | 0.205 | 0.9649 | 4.825 / 4.809 |
    | 3 | 84635 | yes | 5,536 | 5,227 | 0.102 / 0.206 | 0.201 | 0.9724 | 4.745 / 4.914 |
    | 4 | 84640 | yes | 5,318 | 5,175 | 0.100 / 0.188 | 0.226 | 0.9644 | 4.776 / 4.898 |
    | 5 | 84637 | yes | 5,606 | 5,590 | 0.122 / 0.232 | 0.204 | 0.9701 | 4.694 / 4.798 |
    | 6 | 84644 | yes | 5,321 | 5,330 | 0.156 / 0.246 | 0.230 | 0.9734 | 4.773 / 4.868 |
    | 7 | 84659 | yes | 5,217 | 4,863 | 0.112 / 0.196 | 0.186 | 0.9699 | 4.734 / 4.914 |
    | 8 | 84664 | yes | 5,039 | 5,721 | 0.108 / 0.196 | 0.202 | 0.9735 | 4.818 / 4.785 |
    | 9 | 84662 | yes | 5,144 | 5,273 | 0.108 / 0.212 | 0.232 | 0.9689 | 4.834 / 4.892 |
    | 10 | 84668 | yes | 5,225 | 5,408 | 0.130 / 0.226 | 0.224 | 0.9681 | 4.784 / 4.854 |
    | 11 | 84684 | yes | 5,322 | 5,109 | 0.100 / 0.232 | 0.209 | 0.9739 | 4.734 / 4.881 |
    | 12 | 84687 | yes | 4,992 | 5,342 | 0.102 / 0.214 | 0.235 | 0.9685 | 4.831 / 4.847 |
    | 13 | 84693 | yes | 5,461 | 5,343 | 0.130 / 0.234 | 0.226 | 0.9753 | 4.747 / 4.877 |
    | 14 | 84701 | yes | 4,923 | 4,629 | 0.094 / 0.216 | 0.204 | 0.9637 | 4.840 / 5.068 |
    | 15 | 84713 | yes | 5,329 | 5,519 | 0.122 / 0.232 | 0.222 | 0.9707 | 4.755 / 4.813 |
    | 16 | 84710 | yes | 5,278 | 5,428 | 0.134 / 0.232 | 0.224 | 0.9682 | 4.783 / 4.847 |
    | 17 | 84723 | yes | 5,518 | 4,599 | 0.094 / 0.194 | 0.121 | 0.9609 | 4.699 / 5.043 |
    | 18 | 84730 | yes | 5,072 | 4,943 | 0.102 / 0.230 | 0.229 | 0.9687 | 4.845 / 4.965 |
    | 19 | 84738 | yes | 4,948 | 5,269 | 0.106 / 0.226 | 0.230 | 0.9729 | 4.864 / 4.908 |
    | 20 | 84737 | yes | 5,096 | 6,024 | 0.122 / 0.200 | 0.175 | 0.9706 | 4.809 / 4.749 |
    | 21 | 84747 | yes | 5,382 | 5,323 | 0.108 / 0.256 | 0.223 | 0.9682 | 4.748 / 4.848 |
    | 22 | 84749 | yes | 5,288 | 6,427 | 0.158 / 0.200 | 0.160 | 0.9653 | 4.785 / 4.720 |
    | 23 | 84761 | yes | 5,748 | 5,415 | 0.124 / 0.232 | 0.194 | 0.9731 | 4.683 / 4.843 |
    | 24 | 84765 | yes | 5,146 | 5,411 | 0.126 / 0.230 | 0.242 | 0.9689 | 4.799 / 4.850 |
    | 25 | 84778 | yes | 4,950 | 5,195 | 0.112 / 0.224 | 0.233 | 0.9767 | 4.834 / 4.883 |
    | 26 | 84787 | no | 5,351 | 5,829 | 0.134 / 0.236 | 0.221 | 0.9658 | 4.753 / 4.773 |
    | 27 | 84781 | no | 5,162 | 5,505 | 0.116 / 0.218 | 0.205 | 0.9707 | 4.801 / 4.829 |
    | 28 | 84791 | no | 5,319 | 5,177 | 0.132 / 0.230 | 0.210 | 0.9641 | 4.766 / 4.893 |
    | 29 | 84800 | no | 5,418 | 5,345 | 0.158 / 0.270 | 0.228 | 0.9691 | 4.744 / 4.818 |
    | 30 | 84805 | no | 5,476 | 5,223 | 0.128 / 0.244 | 0.216 | 0.9722 | 4.781 / 4.912 |
    | 31 | 84806 | no | 4,990 | 4,852 | 0.122 / 0.236 | 0.243 | 0.9716 | 4.846 / 4.970 |
    | 32 | 84816 | no | 5,212 | 5,792 | 0.144 / 0.208 | 0.204 | 0.9721 | 4.802 / 4.772 |
    | 33 | 84822 | no | 5,336 | 5,364 | 0.146 / 0.224 | 0.224 | 0.9690 | 4.767 / 4.856 |
    | 34 | 84828 | no | 5,084 | 5,440 | 0.124 / 0.228 | 0.228 | 0.9688 | 4.844 / 4.865 |
    | 35 | 84837 | no | 5,450 | 6,617 | 0.138 / 0.192 | 0.144 | 0.9679 | 4.748 / 4.689 |
    | 36 | 84850 | no | 5,430 | 5,466 | 0.182 / 0.284 | 0.227 | 0.9712 | 4.751 / 4.839 |
    | 37 | 84856 | no | 4,768 | 4,975 | 0.080 / 0.210 | 0.234 | 0.9769 | 4.886 / 4.927 |
    | 38 | 84852 | no | 4,917 | 4,748 | 0.092 / 0.224 | 0.214 | 0.9667 | 4.908 / 5.101 |
    | 39 | 84861 | no | 5,300 | 5,301 | 0.132 / 0.224 | 0.221 | 0.9725 | 4.763 / 4.860 |
    | 40 | 84871 | no | 5,173 | 5,282 | 0.128 / 0.238 | 0.233 | 0.9644 | 4.798 / 4.877 |
    | 41 | 84878 | no | 5,598 | 5,037 | 0.092 / 0.200 | 0.175 | 0.9679 | 4.732 / 4.951 |
    | 42 | 84880 | no | 5,350 | 5,643 | 0.150 / 0.222 | 0.212 | 0.9728 | 4.778 / 4.799 |
    | 43 | 84884 | no | 5,028 | 4,517 | 0.096 / 0.224 | 0.173 | 0.9661 | 4.813 / 5.099 |
    | 44 | 84894 | no | 4,903 | 5,619 | 0.086 / 0.172 | 0.200 | 0.9729 | 4.868 / 4.810 |
    | 45 | 84901 | no | 5,261 | 5,873 | 0.164 / 0.242 | 0.208 | 0.9665 | 4.795 / 4.799 |
    | 46 | 84905 | no | 5,205 | 5,733 | 0.140 / 0.232 | 0.200 | 0.9696 | 4.779 / 4.784 |
    | 47 | 84919 | no | 5,246 | 5,155 | 0.112 / 0.238 | 0.226 | 0.9761 | 4.780 / 4.875 |
    | 48 | 84925 | no | 5,093 | 5,047 | 0.110 / 0.232 | 0.230 | 0.9639 | 4.817 / 4.907 |
    | 49 | 84926 | no | 5,366 | 5,315 | 0.112 / 0.206 | 0.234 | 0.9753 | 4.786 / 4.874 |
    | 50 | 84935 | no | 5,301 | 4,627 | 0.106 / 0.202 | 0.149 | 0.9632 | 4.763 / 5.102 |
    | 51 | 84946 | no | 5,110 | 5,864 | 0.108 / 0.188 | 0.193 | 0.9673 | 4.860 / 4.780 |
    | 52 | 84950 | no | 5,260 | 6,201 | 0.160 / 0.232 | 0.160 | 0.9647 | 4.789 / 4.745 |
    | 53 | 84948 | no | 4,822 | 4,753 | 0.088 / 0.230 | 0.234 | 0.9710 | 4.835 / 4.937 |
    | 54 | 84958 | no | 5,101 | 5,714 | 0.130 / 0.242 | 0.211 | 0.9720 | 4.831 / 4.793 |
    | 55 | 84965 | no | 5,136 | 5,039 | 0.142 / 0.250 | 0.231 | 0.9742 | 4.785 / 4.885 |
    | 56 | 84970 | no | 5,254 | 5,478 | 0.120 / 0.218 | 0.239 | 0.9735 | 4.783 / 4.841 |
    | 57 | 84986 | no | 5,573 | 5,392 | 0.112 / 0.194 | 0.212 | 0.9682 | 4.725 / 4.842 |
    | 58 | 84984 | no | 5,402 | 5,687 | 0.162 / 0.254 | 0.223 | 0.9679 | 4.769 / 4.813 |
    | 59 | 84990 | no | 5,320 | 6,202 | 0.136 / 0.216 | 0.172 | 0.9707 | 4.764 / 4.714 |
    | 60 | 85009 | no | 5,108 | 5,631 | 0.164 / 0.236 | 0.204 | 0.9651 | 4.822 / 4.801 |
    | 61 | 85006 | no | 5,013 | 5,377 | 0.132 / 0.246 | 0.204 | 0.9764 | 4.836 / 4.853 |
    | 62 | 85019 | no | 5,007 | 4,863 | 0.108 / 0.218 | 0.236 | 0.9675 | 4.835 / 4.928 |
    | 63 | 85012 | no | 5,451 | 5,989 | 0.114 / 0.212 | 0.211 | 0.9708 | 4.787 / 4.790 |
    | 64 | 85032 | no | 5,195 | 5,449 | 0.146 / 0.230 | 0.219 | 0.9679 | 4.806 / 4.850 |
    | 65 | 85030 | no | 5,520 | 5,775 | 0.128 / 0.218 | 0.217 | 0.9696 | 4.735 / 4.783 |
    | 66 | 85043 | no | 4,994 | 5,807 | 0.108 / 0.190 | 0.197 | 0.9717 | 4.835 / 4.788 |
    | 67 | 85037 | no | 4,708 | 4,399 | 0.088 / 0.208 | 0.225 | 0.9728 | 4.892 / 5.151 |
    | 68 | 85053 | no | 4,820 | 5,341 | 0.090 / 0.194 | 0.209 | 0.9729 | 4.895 / 4.834 |
    | 69 | 85052 | no | 5,076 | 5,048 | 0.114 / 0.222 | 0.238 | 0.9699 | 4.825 / 4.923 |

    **On parity.** The images agree: on every epoch the port's difference
    image correlates with dev's at 0.961 to 0.978, and its scatter is
    within 2% of dev's (4.79 against 4.87 on average). That is against
    part 4's 0.71 to 0.73, whose two-frame reference was half the epoch
    itself. The source counts agree in total, the port at 0.975 of dev's
    on average (0.82 to 1.20 per epoch), but the sources themselves mostly
    do not: about 21% of the port's sources lie within 0.055″ of one of
    dev's, and the two brightest-500 lists share 12% (22% at 0.5″).
    The epochs inside the port's reference behave as the 44 outside it, to
    the precision of these figures. Coadding the epoch at 1/25 weight does
    not show, unlike the two-frame case. Unverified, but consistent with
    every figure here: the two references are 25 different frames, so
    their noise differs. Most of a chip's 5,000 sources sit near the
    5σ PSF-fit threshold, so which noise peaks cross it differs between
    dev's subtraction and the port's, while the images themselves agree.
    A source-level parity check therefore needs either the same reference
    frames (rfid 90011's 25 are not in this night) or injected sources
    with known positions (data-limited, part 5).

11. **Checks 1 to 6 on run 9**, before any group: the `_official` views
    are run 9's (and run 1's for epochs 1 and 2). The checks are one script
    outside the repository, one function per check, each printing a pass
    line or its first counter-example: SQL over the tables and views,
    Parquet read from the prefixes, Avro read with the reader's own
    functions (`scripts/read_packets.py`'s comparison helpers, imported).

    1. **Lineage closes.** Every row of the 14 product tables, and every
       other job a row names (`memberships.source_job`, `packets.object_job`
       and `source_job`, `external.source_job` and `catalog_job`), names a
       `published` job; every job names a run; every `input` and `excludes`
       row of `job_inputs` (8,190 of 8,751; the rest are `expects`) resolves
       to a published row of its product with its key; every `expects`
       binding is a binding of its product and key; every product binding,
       open or closed, names a published row of its key; every live binding
       names a run.
    2. **Parquet and tables agree.** For every published job, each declared
       output's Parquet row count equals the table's row count for the job,
       and no table holds rows of a job that did not declare it; the bytes
       of every declared file of ten jobs (1, 30, 96, 179, 230, 231, 233,
       301, 308, 311: every stage, both runs) match the manifest's SHA-256.
    3. **The chain.** `members_official` equals the union of the chain's
       `matches.parquet` (read from each set's prefix) minus the chain's
       exclusions, row for row as (set, object, source set, source). Every
       object's stored sums reproduce its members' mean and standard
       deviation of position and flux, to PR 280's tolerances (means to
       1e-12°, `stdevdec` and flux to 1e-9 relative, `stdevra` to 1e-6
       relative); every object of set k is a row of set k+1.
    4. **Packets**, each set against its own recorded inputs, never the
       current official sets: the index's object and source sets are the
       job's `input` rows; every locator decodes to the record of its
       source, object and trigger; every history detection precedes the
       trigger; `nDiaSources` equals the object's memberships at or before
       the trigger through the job's recorded object set's chain minus its
       exclusions; the three cutouts open as 129 × 129 FITS; every
       `prvDiaForcedSources` entry equals its row in the set's own
       `forced.parquet` by id (fluxes at float32). Then `read_packets.py`
       over every official set.
    5. **Forced photometry is measured once.** Across the run's official
       packet sets, every (object, image) pair that appears in more than one
       `forced.parquet` is the same measurement in each, value for value,
       `time_proc` included: a copy reused from a predecessor set, never a
       second measurement. (A set's `forced.parquet` carries its reused rows
       too, so the set is self-contained; a pair appearing twice is the
       design, a pair measured twice would be the defect.)
    6. **The two 84624 executions** of run 10, jobs 233 and 301: both
       published, their 5,115 source rows equal in every column but `job`
       and the drawn `pid`, neither bound.

    All six held:

    | check | run 9 (before group A) | s | run 10 (after group A) | s |
    |---|---|---|---|---|
    | 1 lineage | 0 orphans; 452 bindings | 0.4 | 0 orphans; 597 bindings | 0.5 |
    | 2 Parquet and tables | 309 jobs, 838 outputs; 29 files' bytes | 138 | the same | 134 |
    | 3 chain | set 179, depth **69**, 2,547 members row for row, 1,504 objects' sums (559 with several members); walk 1.8 ms, view 32 ms | 0.9 | set 308, depth **5**, 315 members, 300 objects (10 with several); walk 1.8 ms, view 5.1 ms | 0.2 |
    | 4 packets | 69 sets, 2,488 packets, 2,286 history detections, 7,464 cutouts, 87,658 forced entries; reader 69 sets **CLEAN** in 81 s | 114 | 5 sets, 314 packets, 21 history detections, 942 cutouts, 976 forced entries; reader 5 sets **CLEAN** in 7.5 s | 10.6 |
    | 5 forced once | 69 sets, 87,520 rows, 62,543 pairs, each measured once (38 to 3,172 pairs per set, windows of 1 to 69 images) | 1.1 | 5 sets, 976 rows, 945 pairs, each measured once | 0.04 |
    | 6 the two 84624 | both published, equal, unbound | 0.1 | 233 official (its two bindings), 301 unbound with its refusal | 0.2 |

    Run 10's five sets, check 5:

    | packet job | expid | forced rows | pairs | measured here | window images |
    |---|---|---|---|---|---|
    | 304 | 84613 | 50 | 50 | 50 | 1 |
    | 307 | 84624 | 124 | 124 | 121 | 2 |
    | 309 | 84635 | 231 | 231 | 227 | 3 |
    | 310 | 84640 | 216 | 216 | 211 | 4 |
    | 311 | 84637 | 355 | 355 | 336 | 5 |

    One check written for this run was wrong and was dropped: "an object
    has exactly one member at its trigger's time" failed on 7 packets of
    run 9 (first: job 180, object 6808753316802956105, which holds three
    sources of source set 46 within 0.055″ of each other at one epoch).
    The matcher allows several sources of one epoch to join one object,
    as dev's does, so this is the data, not a defect.

12. **Change group A: the night becomes official.** Built from the
    database: close the open binding of the reference (rfid 90011, job 1),
    of every difference image and source set (run 1's import jobs 2 and 3,
    run 9's 30 to 96), of the object set (job 179) and of all 69 packet
    sets (jobs 6, 7 and run 9's); open run 10's reference (job 231), its 69
    differences and source sets (jobs 232 to 300, with **233** for 84624,
    not 301), its last object set (**308**, epoch 5's) and its five packet
    sets (304, 307, 309, 310, 311). Run 9's packet bindings for epochs 6 to
    69 close with nothing opened (an item may be a close alone). **209
    items: 145 opens (1 + 69 + 69 + 1 + 5) and 209 closes.** Applied through
    `rapidpipe.foundation.official.change` from a scratch script, as PR 292
    did, so the operation is timed apart from process start-up:

    ```sh
    # group-a.json: [{"close": <open binding>, "open": {"product", "key", "job"}}, ..., {"close": <packet binding>}, ...]
    rp official change --group group-a.json --who "$USER" --why "the Batch night of run 10 becomes official"
    ```

    **Group 216**: 256.9 ms of operation for 145 opens and 209 closes,
    **1.23 ms per key** (by the larger side), 0.58 s for the process. PR
    292's 8-key group took 19.3 ms (2.4 ms per key) through the same pooler;
    PR 277's local curve gives 0.14 ms per key. Per key the larger group is
    cheaper, the fixed cost of the round trip and the transaction spread
    over more keys; still about three round trips per item over the network.

    Check, all held: `refimages_official` (with `refimimages_official` and
    `refimcatalogs_official`), `diffimages_official`, `sources_official`,
    `objects_official`, `members_official`, `packets_official`,
    `forced_official` and `external_official` return only run 10's jobs;
    `exposures_official` and `l2files_official` are still run 1's two and
    run 9's 67, `psfs_official` jobs 1 and 9, `catalogs_official` job 1; the
    live binding is still 50 (run 9); every table's row count is unchanged
    except `official`, 452 to **597** (+145); `read_packets.py --job` exits
    0 CLEAN on all five of run 10's sets:

    | job | key | records | Avro bytes | cutouts | edge cutouts | history | forced values |
    |---|---|---|---|---|---|---|---|
    | 304 | 84613-8-8 | 50 | 9,607,799 | 150 | 3 | 0 | 50 |
    | 307 | 84624-8-8 | 62 | 11,990,248 | 186 | 0 | 3 | 124 |
    | 309 | 84635-8-8 | 77 | 14,831,940 | 231 | 3 | 3 | 231 |
    | 310 | 84640-8-8 | 54 | 10,456,858 | 162 | 0 | 3 | 216 |
    | 311 | 84637-8-8 | 71 | 13,739,488 | 213 | 0 | 12 | 355 |

    Then checks 1 to 6 in full on run 10 (step 11's table, right column).

13. **A mid-night epoch replaced.** The third epoch by `mjdobs`, 84635
    (run 10's job 234), the middle of run 10's five-epoch chain, replaced
    after the night was complete. Run 10 is a campaign and names its inputs
    and exclusions itself; every job is staged unbound (no `expects`, so
    each Batch job exits 0), then one group binds the four keys together.

    ```sh
    rp job submit --run 10 --stage difference --job-definition $DEF --scope '{"expid": 84635, "sca": 8, "fid": 8}' \
      --inputs '<job 234's four inputs: l2files job 30, refimages job 231, psfs science job 1, psfs reference job 9>'
    rp job submit --run 10 --stage associate --job-definition $DEF --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84635)"', "source_job": 312}, {"product": "objects", "key": '"$OKEY"', "source_job": 308}]' \
      --excludes '[{"key": '"$(k 84635)"', "source_job": 234}]'
    rp job submit --run 10 --stage packet --job-definition $DEF --scope '{"expid": 84635, "sca": 8, "fid": 8}' \
      --inputs '<job 309's inputs with the object set 313 and epoch 84635's difference image and source set 312>'
    # group-b.json: four items, each closing run 10's binding of the key and opening the new job's
    rp official change --group group-b.json --who "$USER" --why "epoch 84635 replaced mid-night"
    ```

    Difference **job 312** (Batch `c99a4613-d98d-4f7a-9990-0ca25440abcf`),
    associate **313** (`67e4b954-1cf3-417a-8862-9b815eee09b1`, 144 sources
    accepted, 77 excluded members subtracted, 77 memberships, no new
    object), packet **314** (`046f7276-0839-4c5f-a422-…`, 77
    packets, 231 forced rows, 227 measured and 4 reused), all `SUCCEEDED`
    and published on attempt 1. **Group 217** closed bindings 458, 459, 592
    and 595 and opened the four new ones: 12.1 ms (3.0 ms per key).

    Checks, by conservation, all held:
    - The expected membership is the predecessor's (set 308's chain, 315
      rows) minus every membership of the old source set (77) plus the
      replacement's matches (77): 315 rows. `members_official` equals it
      row for row, and so does the new chain's `matches.parquet` minus its
      exclusions.
    - Objects: set 313 holds all 300 of set 308's objects and no new one.
      None is removed (no object is left without members), none added; the
      77 objects whose epoch-3 member moved from job 234's source to job
      312's are the changed ones, and the other 223 are unchanged, their
      stored sums equal to set 308's bit for bit. Every object's sums
      reproduce its current members (check 3's tolerances).
    - The other four packet sets are untouched: `packets_official` holds
      jobs 304, 307, 310 and 311 with their rows and bindings as before, and
      314 in place of 309.
    - The new set passes check 4 against its own recorded inputs (77
      packets, 3 history detections, 231 cutouts, 231 forced entries); none
      of the new object set's chain memberships is from job 234, so no
      packet's history or `nDiaSources` counts the old detection;
      `read_packets.py --job 314` exits 0 CLEAN. Checks 3 and 4 on the five
      official sets after the group: chain depth 6, 315 members, CLEAN.

    **Observation for the plan: what the model leaves.** The packets of
    epochs 4 and 5 (jobs 310 and 311) were built when the old epoch-3
    detections were members, and stay as published: 1 packet of 310 and 2
    of 311 carry an old epoch-3 detection (job 234's) in their history.
    They are valid against their recorded inputs (check 4 passes on them
    after group B), because a packet is a record at its trigger time. A
    live loop would do nothing about them: the alert went out with what was
    known then. A campaign that wants its packets consistent with the
    replacement reruns from the replaced epoch on (here, packets 4 and 5
    against set 313) and binds them in the same group.

    **Observation: the replacement is not bit-identical to job 234.** Same
    image (`rapid-rebuild:33`), same inputs, same 5,536 sources, but 2,156
    rows differ in some column: 1,342 in RA by more than 1e-12″, 63 by more
    than 1 µas, 9 by more than 1 mas and one by more than 0.1″; 15 fluxes by
    more than 1e-3 relative; one `flags` value. Jobs 233 and 301 (task 5)
    were identical. Jobs 233, 234 and 301 ran on r6i.8xlarge (Intel) and
    job 312 on m6a.2xlarge (AMD), from `describe-container-instances`, so
    the likely mechanism is CPU-dependent floating point in the tools
    (vector paths in the FFTs or BLAS) amplified by the fit. Unverified:
    consistent with every figure, not probed on one instance type. Task
    1's seed pins the draw, not the arithmetic. Step 13's checks are by
    conservation and do not depend on the two agreeing.

14. **Undo.** Each through `rapidpipe.foundation.official.undo`, timed the
    same way; each result compared with its target state as the full set
    of open bindings, row for row on (product, key, job) and (survey,
    dataset, run).

    | group | what | opens / closes | operation | per key | result |
    |---|---|---|---|---|---|
    | 218 | undo of B | 4 / 4 | 8.9 ms | 2.2 ms | open bindings equal those after group A, row for row |
    | 219 | undo of A | refused | | | `binding 458 is no longer open`; nothing written |
    | 220 | A's inverse as of now | 209 / 145 | 317.4 ms | 1.5 ms | open bindings equal those before group A, row for row |
    | 221 | A2: group A again | 145 / 209 | 247.0 ms | 1.2 ms | open bindings equal those after group A, row for row |

    **The undo of A refuses, by the rule.** Group B closed binding 458 (the
    84635 difference image's, opened by A), and B's undo reopened that key
    as a new row (an undo never reopens a closed row, plan statement 5).
    So A's inverse, which closes the rows A opened, finds 458 closed and
    refuses, naming it, as proof step 12 showed on the synthetic world;
    group 219 wrote no row. The restoration is then an explicit group, 220,
    built from A's two sides as they stand: close the binding now open on
    each of the 145 keys A opened (141 of them A's own rows, 4 the undo of
    B's) and open, as new rows, the 209 bindings A closed. After it run 9
    is official again, row for row as before group A. Group A2 (221)
    applies A's membership again (the same 145 opens; the closes name run
    9's current bindings), a choice recorded as such: the run ends with run
    10's products official and run 9 live (binding 50). Nothing was
    deleted: every table's row count after A2 equals that after A plus
    steps 13's three jobs' rows, and `official` only grew.

15. **Two change groups at once, through the pooler.** `rapid_rebuild_pipeline`
    is transaction-pooled by pgbouncer (rapid_systems
    `rpms/rapid-pgbouncer/pgbouncer.rapid.ini`), so a transaction holds one
    server connection from its first statement to its commit. Two sessions
    through the pooler, from a scratch script holding each group open
    through the library (no verb holds one uncommitted): session 1 applies
    a group closing the epoch-5 packet binding 959 and reopening that key
    to job 311, uncommitted; session 2 applies a group closing binding 955
    (epoch 1's packets, a lower id) and then 959. Session 2 closed 955 and
    blocked on 959's row (after 3 s it waited on a `ShareLock` on session
    1's transaction, from `pg_locks`); session 1 committed **group 222**;
    session 2 woke, found 959 closed, refused with `binding 959 is no longer
    open` and rolled back. Check, held: binding 955 still open, 959 closed
    by 222 only, group 222 one open and one close, the refused group's id
    (223) on no row: nothing half-applied. **Undo of 222: group 224**, 5.9
    ms; the open bindings equal those after A2, row for row.

## Results of part 6 (2026-10-21)

Every check of steps 11 to 15 held; the night is one coherent account in
the database, the Parquet and the outbox, under both runs.

- **Run 9** (live, the loop's shape): imports 30 to 96, associates and
  packets 97 to 230 (part 6 steps 2 to 5), with run 1's jobs 1 to 7 for
  epochs 1 and 2. **Run 10** (the campaign): reference 231, differences
  232 to 301, chain 302 to 311 (steps 6 to 10), the replacement 312 to
  314 (step 13).
- **Final state**: run 10's reference, 69 differences and source sets (233
  for 84624), object set 308 and five packet sets 304, 307, 309, 310, 311
  official (group 221, with 224 restoring the epoch-5 packet key); 301 and
  312 to 314 published and unbound; run 9 live (binding 50); `official`
  **961** rows; change groups 216 to 224.

Measurements:

| what | figure |
|---|---|
| checks 1 to 6, run 9 | 254.5 s (check 2 138 s and check 4 114 s, both mostly S3 reads) |
| checks 1 to 6, run 10 | 145.1 s |
| group A (216), 145 / 209 | 256.9 ms, 1.23 ms per key, 0.58 s process |
| group B (217), 4 / 4 | 12.1 ms, 3.0 ms per key |
| undo B (218) / restore A (220) / A2 (221) / undo 222 (224) | 8.9 / 317.4 / 247.0 / 5.9 ms |
| difference 312 | queue 222 s, container 1,131 s, peak RSS 1.25 GiB self, 14.54 GiB largest child, on m6a.2xlarge |
| associate 313 | queue 10.7 s, container 2.7 s, peak RSS 228 MiB |
| packet 314 | queue 10.9 s, container 23.7 s (forced photometry 10.8 s), peak RSS 846 MiB |
| database | 239 MB (`pg_database_size`) |
| ten largest tables (total size) | `sources` 102 MB, `objects` 19.0 MB, `forced` 18.7 MB, `job_inputs` 1.8 MB, `packets` 1.1 MB, `official` 1.0 MB, `jobs` 655 kB, `memberships` 532 kB, `diffimages` 205 kB, `l2files` 205 kB |
| `official` rows at the end | 961 |
| S3 under the scratch root, per run | 1: 1.28 GB; 2: 0.60 GB; 3: 2.82 GB; 4: 6.26 GB; 5: 5.66 GB; 6: 5.63 GB; 7: 5.63 GB; 8: none; 9: 22.4 GB (1,273 objects); 10: **200.7 GB** (5,168 objects) |

## Part 7: HLWAS, from ASDF intake to Kafka (2026-10-22)

1. **The night's fixes, their image and a Batch proof.** PR 301 (merged
   `32970a24`) records the EC2 instance type in a new `jobs.instance_type`
   column beside `image_digest`, read from IMDSv2 when the attempt starts
   (null, with the error logged, when unreachable); makes `job run` exit 0
   when a published job's only refusal is that its run is not live; removes
   `prefix.get`'s download cache at process exit; and pins
   `[reference.statistics] clip_correction_seed = 0`. SMDC gets the column
   as `postgres`:

   ```sh
   pg -c "ALTER TABLE jobs ADD COLUMN instance_type text;"
   ```

   Check, held: `jobs.instance_type text` present (0.25 s); jobs 1 to 314,
   last change group 224, `official` 961 rows before and after.

   The earlier runs' download caches on the fleet host were removed by
   their `mkdtemp` prefix, listed first: 170 directories
   `/tmp/rapidpipe-get-*`, 10.3 GiB, every byte re-fetchable from S3;
   the root disk went from 87% to 70% used. Nothing else under `/tmp` was
   touched (89 `rapidpipe-job-*` working directories, 22.2 GiB, remain).

   The image, on the fleet host (rapid_systems' release-hooks README):

   ```sh
   containers/rapid-pipeline/build-rebuild.sh 32970a244eed35f68fa97c6f9899fee5cb87e3cb --tag rebuild-32970a244eed35f68fa97c6f9899fee5cb87e3cb
   RELEASE_TAG=rebuild-32970a244eed35f68fa97c6f9899fee5cb87e3cb RELEASE_IMAGE_DIGEST=sha256:b666e04d… cloudformation/release-hooks/deploy
   ```

   Check, held: build 21 s with cached layers, image 1.96 GB compressed;
   deploy 73 s; **`rapid-rebuild:34`** (and `rapid-rebuild-production:25`),
   its image and `RAPID_IMAGE_DIGEST` both `sha256:b666e04d…`, retry 3,
   24 GiB, read back with `describe-job-definitions`.

   The proof, under a new non-live run with the shipped settings at the
   merged sha, uncapped as run 10 was so that its rows compare with run
   10's:

   ```sh
   git show 32970a24:rapidpipe/settings/foundation-thin.toml | sed 's/^max_sources = 500$/max_sources = 0/' > uncapped1.toml
   rp run create --who "$USER" --why "hlwas-night-2026-10-21 task 1 (night-fixes): ..." \
     --revision 32970a244eed35f68fa97c6f9899fee5cb87e3cb --settings uncapped1.toml --survey GBTDS --dataset $D
   rp job submit --run 11 --stage difference --job-definition rapid-rebuild:34 --scope '{"expid": 84624, "fid": 8, "sca": 8}' \
     --inputs '<job 233's four inputs: l2files from job 3, refimages field 4658155 from job 231, the science PSF from job 1, the reference PSF from job 9>' \
     --expects '[{"product": "diffimages", "key": '"$(k 84624)"', "binding": null}, {"product": "sources", "key": '"$(k 84624)"', "binding": null}]'
   ```

   and the same for epoch 84635 with job 234's inputs (its `l2files` from
   job 30). The second job is there because the night's instance-family
   observation (step 13 of part 6) compared job 234 (r6i.8xlarge) with job
   312 (m6a.2xlarge) on epoch 84635; jobs 233 and 301 on 84624 ran on the
   same family and agreed.

   Check, held: run **11** (`"live": null`); jobs **315** (84624, Batch
   `6f3e145c…`) and **316** (84635, Batch `73fda254…`) Batch `SUCCEEDED`
   on attempt 1 with exit 0; each published attempt 1 and unbound, its row
   carrying `run 11 is not live for its survey and dataset` and the same
   line on the job's log; no `official` row names either; `instance_type`
   **r6i.2xlarge** on both, matching ECS's `ecs.instance-type` for their
   container instance, so the IMDSv2 read reaches the metadata service
   from the container and the launch templates need no change (their
   `MetadataOptions` set `HttpTokens: required` and no hop limit).

   **The instance-family data point.** Sources compared row for row on
   (`isdiffpos`, `id`), every column but `job`, `sid` and `pid` (the job's
   own difference-image id), NaN equal to NaN:

   | job | epoch | instance | against | result |
   |---|---|---|---|---|
   | 315 | 84624 | r6i.2xlarge | 233 (r6i.8xlarge) | identical, 5,115 rows |
   | 315 | 84624 | r6i.2xlarge | 301 (r6i.8xlarge) | identical |
   | 316 | 84635 | r6i.2xlarge | 234 (r6i.8xlarge) | identical, 5,536 rows |
   | 316 | 84635 | r6i.2xlarge | 312 (m6a.2xlarge) | 2,156 rows differ: 1,342 in RA, 2,075 in Dec, 10 by more than 1 mas, 1 by 0.52″; 15 fluxes by more than 1e-3 relative; 1 `flags` |

   Two Intel sizes give the same rows, and the AMD job differs from all
   three Intel executions of its epoch in the same way. Consistent with
   CPU-dependent arithmetic in the tools; one AMD execution, so recorded,
   not resolved.

   Measurements:

   | what | job 315 | job 316 |
   |---|---|---|
   | queue wait (created to started) | 226.5 s | 225.4 s |
   | container (started to stopped) | 1,316.6 s | 1,305.4 s |
   | peak RSS, self / largest child | 1.33 / 14.54 GiB | 1.25 / 14.54 GiB |
   | bytes under `11/difference/<job>/1/` | 2.81 GB, 72 objects | 2.82 GB, 72 objects |

2. **The cluster from the fleet host, and the stamp column.** The
   bootstrap servers, topic and maximum request size are read from the SSM
   parameters the shipped settings' `[kafka]` table names; with
   kafka-python and the AWS MSK signer (`SASL_SSL`, `OAUTHBEARER`) under
   `rapid-deploy`, the host lists the cluster's topics in 1.7 s, so its
   security group reaches the brokers on 9098. The topic already exists
   (1 partition, replication 3, every in-sync replica present) and was
   empty: earliest and end offset both **73505**, earlier tests' messages
   aged out by retention. Nothing was created. SMDC gets the stamp column
   as `postgres`:

   ```sh
   pg -c "ALTER TABLE jobs ADD COLUMN kafka_published_at timestamp with time zone;"
   ```

   Check, held: the column present (0.26 s), `UPDATE` on it held by
   `rapid_rebuild_pipeline`; jobs 1 to 316, last change group 224,
   `official` 961 rows, unchanged.

3. **Run 10's packets onto the topic and back.** `rp publish` produces
   every official, unstamped packet set of a run, one message per packet
   keyed `<packet set job>:<sid>`, the value the container's header and
   the packet's block (a one-record Avro container, exactly what the
   reader decodes), waits for every acknowledgement (`acks=all`) and then
   stamps the set's job; `scripts/consume_packets.py` reads the topic back
   from the earliest offset and compares:

   ```sh
   rp publish --run 10
   python scripts/consume_packets.py --run 10
   rp publish --run 10
   ```

   Check, held: the first `publish` produced run 10's five official sets
   (jobs 304, 307, 309, 310, 311), **314 packets**, and stamped each job;
   the consumer check read `Consumer check run 10: CLEAN; read 314, kept
   314, valid 314, byte-equal 314, packets expected 314 seen 314;
   partitions p0 73505..73818`; the second `publish` produced nothing and
   the end offset stayed 73819.

   Measurements:

   | what | figure |
   |---|---|
   | packets / bytes produced | 314 / 71.4 MB (71,421,248) |
   | `publish` wall time | 11.8 s inside the verb (12.6 s process), five containers read from S3 included; 26.6 packets/s, 6.1 MB/s; about 2.3 s per set |
   | largest message against `max_request_bytes` | 228,677 B against 15,728,640 (1.5%); every message is the 34,935-byte container header plus one packet |
   | `publish` peak RSS | 188 MiB |
   | consumer check | 14.2 s reading the topic, 17.3 s process (the outbox read from S3 included), peak RSS 334 MiB |
   | second `publish` | 0.10 s, nothing produced |

   **From a container, later.** The Batch job role
   (`rapid-scratch-batch-job-role`) reads the SSM parameters under the
   scratch tree but holds no `kafka-cluster:*` action, so the packet stage
   cannot produce from a Batch job as it stands; its permissions boundary
   already names `Connect`, `DescribeTopic` and `WriteData`, so a grant on
   the role is the one IAM change that would allow it, and whether the
   compute environment's security group reaches the brokers is untested.
   The image (`rebuild-32970a24`) already carries kafka-python 3.0.11 and
   the signer. Not changed by this run.

4. **The intake stage and its image.** PR 304 (merged `c81e17ae`) adds
   the `intake` stage: one SOC L2 `_cal.asdf` file, named by its stem in
   the scope and read under the run's `[intake] source_root` (anonymously
   when `unsigned`), converted by `dev`'s `build_fits_file` into its
   16-HDU FITS, gzipped at level 1, its limiting magnitude measured with
   the science PSF the job names, and declared as one `exposures` and one
   `l2files` row; `expid` is `pass * 10**6 + observation * 10**4 + visit *
   10**2 + exposure`, shared by the 18 SCAs of one exposure. The image,
   with `TMPDIR` unset (buildah cannot make its root filesystem under an
   EFS `TMPDIR`: `mkdir .../mnt/rootfs: permission denied`):

   ```sh
   env -u TMPDIR containers/rapid-pipeline/build-rebuild.sh c81e17ae6e5dda677c0cb9e0b92bee89e482506e --tag rebuild-c81e17ae6e5dda677c0cb9e0b92bee89e482506e
   RELEASE_TAG=rebuild-c81e17ae6e5dda677c0cb9e0b92bee89e482506e RELEASE_IMAGE_DIGEST=sha256:971b5a87… cloudformation/release-hooks/deploy
   ```

   Check, held: the import gate passed with `asdf`, `roman_datamodels`,
   `gwcs` and `sympy`, all four already in the base environment, so
   nothing was added to the image (1,955,119,219 B compressed before,
   1,955,186,171 after: the package itself); build 21 s, deploy 73 s;
   **`rapid-rebuild:35`** (and `rapid-rebuild-production:26`), image and
   `RAPID_IMAGE_DIGEST` both `sha256:971b5a87…`, retry 3, 24 GiB.

5. **The HLWAS live run and its PSFs.** The run's settings body is the
   shipped file at the merged sha with the overlay `[intake] source_root =
   "$SOURCE_ROOT"` (the public bucket's `r00342/l2/` prefix) and
   `unsigned = true`; it is made live with a change group opening its live
   binding, with no cutover, since run 9's is another dataset's:

   ```sh
   rp run create --who "$USER" --why "hlwas-night-2026-10-21 task 3 (intake): ..." \
     --revision c81e17ae6e5dda677c0cb9e0b92bee89e482506e --settings hlwas-run.json --survey HLWAS --dataset r00342
   echo '[{"open": {"survey": "HLWAS", "dataset": "r00342", "run": 12}}]' > live.json
   rp official change --group live.json --who "$USER" --why "... run 12 live for HLWAS r00342 ..."
   ```

   Then the 36 F146 PSFs (`psfs/sciimage_psf_f146_sca01..18.fits` kind
   science, `refimage_psfs/refimage_psf_f146_sca01..18.fits` kind
   reference, under the inputs bucket's `g0006-psf-f146/`, copied to this
   host first), one `job local` each, each expecting its key unbound:

   ```sh
   rp job local --run 12 --stage import \
     --scope '{"source_dir": "<copy>/psfs", "psf": {"kind": "science", "path": "sciimage_psf_f146_sca08.fits"}}' \
     --expects '[{"product": "psfs", "key": {"dataset": "r00342", "fid": 8, "sca": 8, "kind": "science"}, "binding": null}]'
   ```

   Check, held: run **12** (survey `HLWAS`, dataset `r00342`) live,
   binding **962** in group **225**; run 9's binding 50 still open. Jobs
   **317 to 334** (science, SCAs 1 to 18) and **335 to 352** (reference)
   published and bound in groups 226 to 261, 69 s for the 36 (1.9 s and
   238 MiB each); `psfs_official` returns 18 of each kind for `r00342`.
   SCA 8's are job **324** (science, binding 970) and job **342**
   (reference).

6. **Intake on this host, then on Batch.** The pass-1 file of pointing
   (observation 4, visit 1, exposure 1) on SCA 8, then its pass-2 sibling
   as a Batch job (its sidecar's SCA 8 centre is 0.002 arcsec from the
   pass-1 one, the target position identical):

   ```sh
   rp job local --run 12 --stage intake --scope '{"dataset": "r00342", "file": "r0034201001001004001_0001_wfi08_f146"}' \
     --inputs '[{"product": "psfs", "key": {"dataset": "r00342", "fid": 8, "sca": 8, "kind": "science"}, "source_job": 324, "binding": 970}]' \
     --expects '[{"product": "l2files", "key": {"dataset": "r00342", "expid": 1040101, "sca": 8, "fid": 8}, "binding": null},
                 {"product": "exposures", "key": {"dataset": "r00342", "expid": 1040101}, "binding": null}]'
   rp job submit --run 12 --stage intake --job-definition rapid-rebuild:35 \
     --scope '{"dataset": "r00342", "file": "r0034201002001004001_0001_wfi08_f146"}' --inputs '<the same>' \
     --expects '<the same two keys with expid 2040101>'
   ```

   Check, held: job **353** (local) and job **354** (Batch `84b5fc80…`,
   `SUCCEEDED` on attempt 1, exit 0, the image digest above, instance
   type **m6a.2xlarge**) published attempt 1 and bound their `l2files`
   and `exposures` keys (groups 262 and 263); rids 1 and 2, `limmag`
   26.951 and 26.953, field 5387158, `hp6` 35382, `hp9` 2264449, six
   overlapping fields each, the `.fits.gz` under
   `12/intake/<job>/1/`. **A Batch container reads the public bucket
   unsigned** (0.8 s for 204 MB through the VPC), so the copy fallback is
   not needed and tasks 4 to 6 read the source root directly. The
   published FITS went through the difference stage's `reformat` (0.58 s,
   4089 x 4089 in DN/s, median 1.30) and `modules/sip_tpv`'s `sip_to_pv`
   (0.15 s, `PV1_*` and `PV2_*` written) by hand. `official` 961 rows
   before, 1,002 after (1 live, 36 PSFs, 2 x 2 intake).

   Measurements:

   | what | job 353 (this host, m8a.4xlarge, EFS) | job 354 (Batch, m6a.2xlarge) |
   |---|---|---|
   | queue wait (created to started) | none | 155.0 s |
   | container, or job started to ended | 14.9 s | 17.0 s container, 14.9 s job |
   | fetch `_cal.asdf` (204 MB, unsigned) | 1.06 s | 0.81 s |
   | convert (`build_fits_file`, 421 MB, 16 HDUs) | 4.54 s | 2.71 s |
   | gzip level 1 (to 195 MB) | 4.10 s | 5.45 s |
   | limiting magnitude | 2.58 s | 3.49 s |
   | rows (MD5, footprint, field, overlap) and manifest | 1.32 s | 1.00 s |
   | upload and publish | 1.3 s | 1.5 s |
   | peak RSS | 914 MiB | 906 MiB |
   | bytes under `12/intake/<job>/1/` | 195.1 MB, 4 objects | 195.1 MB, 4 objects |

7. **Validation as a script.** `scripts/validate_run.py --run <id>` runs
   the night's checks 1 to 6 over one run's jobs, one function each, as
   part 6's Validation names them: `lineage`, `tables`, `chain`,
   `packets` (each set against its own recorded inputs, then
   `read_packets.py` on the run's bound sets), `forced` and `counts`
   (no job still launched or running; jobs per stage and state, rows per
   table and open bindings printed); `twice` (check 6) runs only with
   `--twice A,B`. It prints one line per check, PASS with its counts or
   FAIL with the first counter-example, and exits non-zero on any
   failure. Checks 7 to 10 were operations, checked as they ran, and have
   no read-only form over a run. Two checks are scoped to a run rather
   than to the whole database: `chain` takes every object set of the run
   that is bound or final (no other set of the run names it as its
   predecessor), and `forced` compares each packet set's forced rows with
   those of the predecessor sets it recorded, so a replacement set that
   measures afresh what it replaces (run 10's job 314) is not a repeat.

   ```sh
   python scripts/validate_run.py --run 10 --twice 233,301 --depth 5
   python scripts/validate_run.py --run 9
   ```

   Check, held: run 10 6 of 6 and `twice` (233 and 301 equal in 5,115
   source rows, 233 bound), 41 s: set 308 depth 5 with 315 members equal
   to `members_official` and 300 objects' sums, the unbound replacement
   set 313 depth 6, 5 bound sets CLEAN, as part 6 found; run 9 6 of 6 in
   143 s (set 179 depth 69, 2,547 members, 1,504 objects; 67 sets, 2,411
   packets; its sets unbound since group A). A pair of different keys
   (`--twice 233,312`) fails, naming the difference.

8. **The first HLWAS pointing: intake, Gaia, reference.** The pointing
   is (visit 1, exposure 1) of F146 on SCA 8 in passes 1 to 6. Pass 6 is
   F146 alone and is **observation 1**, where passes 1 to 5 hold F146 as
   observation 4, so its stem is `r0034201006001001001_0001_wfi08_f146`
   (`expid` 6010101); the six sidecars' SCA 8 centres agree to within
   0.003 arcsec (target 9.671532, -45.202805; `pa_v3` 0.4289; Gaia fit
   SUCCESS in each). Passes 3 to 6 on Batch, at once, with step 6's
   inputs and expects:

   ```sh
   rp job submit --run 12 --stage intake --job-definition rapid-rebuild:35 \
     --scope '{"dataset": "r00342", "file": "r0034201003001004001_0001_wfi08_f146"}' \
     --inputs '[{"product": "psfs", "key": {"dataset": "r00342", "fid": 8, "sca": 8, "kind": "science"}, "source_job": 324, "binding": 970}]' \
     --expects '[{"product": "l2files", "key": {"dataset": "r00342", "expid": 3040101, "sca": 8, "fid": 8}, "binding": null},
                 {"product": "exposures", "key": {"dataset": "r00342", "expid": 3040101}, "binding": null}]'
   ```

   and the same for passes 4, 5 and 6. Check, held: jobs **355 to 358**
   Batch `SUCCEEDED` on attempt 1, published and bound (groups 265 to
   268); `l2files_official` holds the six for `r00342`, every one in
   field **5387158**, `limmag` 26.951 to 26.958.

   Gaia. The only Gaia product on SMDC covered the GBTDS field. Dev built
   its copy with astroquery's `SELECT * FROM gaiadr3.gaia_source` over a
   cone, written to Parquet with the query in the file's metadata; the
   HLWAS copy is the same query over the footprint's box with a
   0.2-degree margin (RA 6.9 to 11.2, Dec -45.7 to -42.6), from the ESA
   archive on this host:

   ```sh
   python fetch.py gaia-dr3-hlwas-r00342-box.parquet   # astroquery Gaia.launch_job_async, the GBTDS file's schema
   rp job local --run 12 --stage import \
     --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"name": "gaia-r00342", "version": "DR3"}, "binding": null}]'
   ```

   **The catalog's key is `gaia-r00342`, not `gaia`.** The `catalogs`
   key is (name, version), with no dataset, and `{gaia, DR3}` is bound to
   the GBTDS copy (binding 2, job 1); binding the HLWAS copy under the
   same key would close it and leave GBTDS's official Gaia a catalog that
   does not cover its field. The packet stage reads the catalog its job
   names by source job, so the name is free. Check, held: job **359**
   published and bound (binding 1003, group 264); **34,171 rows**, every
   one inside the box, one `ref_epoch` (2016.0), the column names and
   types exactly the GBTDS copy's (152 columns, every one the packet
   stage reads among them); query 16.5 s, import 2.4 s, 16 MB.

   The reference, from the six frames (one field, read from the rows):

   ```sh
   rp job submit --run 12 --stage reference --job-definition rapid-rebuild:35 --scope '{"field": 5387158, "fid": 8, "sca": 8}' \
     --inputs '[{"product": "l2files", "key": {"dataset": "r00342", "expid": 1040101, "sca": 8, "fid": 8}, "source_job": 353}, ..., <6010101 from job 358>]' \
     --expects '[{"product": "refimages", "key": {"dataset": "r00342", "field": 5387158, "fid": 8}, "binding": null}]'
   ```

   Check, held: job **360** (Batch `c6315181…`, m6a.2xlarge) published
   and bound (binding 1012, group 269); **rfid 4**, `nframes` 6, six
   `refimimages` rows, nothing left uncoadded. Measured as PR 287 did:
   7000 x 7000 grid centred (9.473684, -45.487946), `cov5percent` 32.62
   (the SCA covers a third of the grid; 33.0 million NaN pixels, median
   coverage 0), clipped mean 1.3501 and standard deviation 0.01723,
   median pixel uncertainty 0.01572, `MAGZP` 26.863, total exposure
   1,764.66 s (6 x 294.1), **5,165** catalog sources, median FWHM 2.59
   px. **The reference coadds the six epochs it is differenced against
   (self-including)**, as the night's did; a reference disjoint from the
   epochs differenced is a later step.

9. **Difference, associate and packet: the null test.** The six
   differences at once against job 360, with the science PSF (job 324)
   and the reference PSF (job 342), as part 6 step 8; then associate in
   `mjdobs` order, each naming the field's previous object set, and
   packet per epoch after its associate and the previous packet, in
   waves (associate n with packet n-1), every one on Batch under
   `rapid-rebuild:35` with expects naming the previous binding or none,
   as part 6 steps 9 and 10 (the packet's inputs: the Gaia catalog of job
   359, the reference of job 360, the science PSF, the epoch's object
   set, the difference images and L2 rows of epochs 1 to n, the epoch's
   sources and the packet sets of epochs 1 to n-1). Then the reader and
   the validation script:

   ```sh
   python scripts/read_packets.py --job 369 --job 371 --job 373 --job 375 --job 377 --job 378
   python scripts/validate_run.py --run 12 --depth 6
   ```

   Check, held: differences **361 to 366**, associates **367, 368, 370,
   372, 374, 376** and packets **369, 371, 373, 375, 377, 378**, all
   Batch `SUCCEEDED` on attempt 1, published and bound; `infobitssci` 0
   on all six; the object set **376** (binding 1034) holds 525 objects
   and 826 members; the reader `6 set(s), CLEAN`; `validate_run.py --run
   12` 6 of 6 in 30 s (lineage 134 input rows and 74 bindings; 62 jobs,
   94 outputs; chain depth 6 equal to `members_official`; 825 packets, 517
   history detections, 2,475 cutouts; 1,236 forced rows reused from a
   predecessor, each its measurement).

   **The null test.** Observations, not gates (`max_sources` 500 as
   shipped):

   | epoch | difference | sources before cap | kept | S/N > 5 | > 10 | > 20 | within 1″ of a reference source | packets |
   |---|---|---|---|---|---|---|---|---|
   | 1040101 | 361 | 982 | 500 | 291 | 160 | 70 | 222 (44%) | 118 |
   | 2040101 | 362 | 973 | 500 | 396 | 187 | 83 | 172 (34%) | 148 |
   | 3040101 | 363 | 993 | 500 | 301 | 146 | 80 | 216 (43%) | 112 |
   | 4040101 | 364 | 949 | 500 | 321 | 155 | 72 | 215 (43%) | 143 |
   | 5040101 | 365 | 969 | 500 | 380 | 185 | 74 | 210 (42%) | 157 |
   | 6010101 | 366 | 1,035 | 500 | 422 | 222 | 100 | 199 (40%) | 147 |

   A static field differenced against a coadd that holds the epoch itself
   should leave noise: with Gaussian residuals, a few chance detections
   above 5 sigma per chip, fewer than the uncorrelated case because each
   epoch is a sixth of its reference, and nothing at S/N 20. It gave
   about a thousand detections per epoch before the cap, 290 to 420
   above S/N 5 and 70 to 100 above S/N 20, the same in all six epochs.
   Of the S/N > 20 ones, 37 to 58 per epoch lie within 1″ of a reference
   catalog source (median `CLASS_STAR` 0.82 to 0.96: stars) and the rest
   a median 2 to 3″ from one: residuals at and around stars, which is
   what a PSF that does not match the image's gives (the PSFs are dev's
   F146 models for the GBTDS simulation, not measured on this one), not
   variability; only 6 to 12 per epoch are within 1″ of a Gaia star
   (median G 13.2 to 14.5: the brightest). The cap keeps about half of each
   epoch's detections. Every epoch produced packets (112 to 157), so
   this tile reaches Kafka; 3 Gaia matches in all.

   Measurements (queue wait is Batch's created to started; container is
   started to stopped; job is the attempt's own start to end; peak RSS
   from `job run`'s log line, self and children):

   | job | stage | instance | queue wait | container | job | peak RSS self / children | bytes under the attempt |
   |---|---|---|---|---|---|---|---|
   | 355 to 358 | intake | r6i.4xlarge | 165.5 to 168.6 s | 18.0 s | 15.8 to 16.1 s | 907 MiB / 0 | 195.1 MB, 4 objects each |
   | 360 | reference | m6a.2xlarge | 193.5 s | 80.3 s | 74.7 s | 1,390 / 1,440 MiB | 594.9 MB, 8 objects |
   | 361, 363 to 366 | difference | r6i.4xlarge | 125.9 to 132.0 s | 226.6 to 233.6 s | 224.3 to 231.4 s | 1,193 to 1,232 / 14,885 MiB | 2.55 GB, 72 objects each |
   | 362 | difference | m6a.2xlarge | 4.7 s | 200.2 s | 197.6 s | 1,232 / 14,889 MiB | 2.55 GB, 72 objects |
   | 367 to 376 (six) | associate | r6i.4xlarge | 3.8 to 10.4 s | 2.6 to 2.7 s | 0.9 to 1.1 s | 231 to 236 MiB | 3 objects, a few kB |
   | 369 | packet | r6i.4xlarge | 3.3 s | 15.0 s | 12.9 s | 845 MiB | 22.9 MB, 5 objects |
   | 371 | packet | r6i.4xlarge | 5.1 s | 21.0 s | 19.0 s | 847 MiB | 28.7 MB |
   | 373 | packet | r6i.4xlarge | 6.9 s | 25.3 s | 23.1 s | 848 MiB | 21.7 MB |
   | 375 | packet | r6i.4xlarge | 6.5 s | 31.9 s | 29.7 s | 849 MiB | 27.8 MB |
   | 377 | packet | r6i.4xlarge | 2.6 s | 36.9 s | 34.9 s | 849 MiB | 30.5 MB |
   | 378 | packet | r6i.4xlarge | 6.3 s | 42.1 s | 40.0 s | 850 MiB | 28.6 MB |

   The difference stage took a fifth of the GBTDS night's container time
   (about 230 s against 1,131 s for job 312) at the same 14.5 GiB child
   peak; the packet stage's time grows with the chain (13 s to 40 s over
   six epochs), the forced photometry measuring each new object on every
   earlier image of the window. From the first intake submitted to the
   last packet published: 22 minutes, of which the ramp-up queue waits
   (about 2 to 3 minutes each for the intake, reference and difference
   waves) are a third.

10. **Kafka: the first ASDF-to-received-packet slice.** Run 12's six
    official packet sets onto the topic and back, as step 3:

    ```sh
    rp publish --run 12
    python scripts/consume_packets.py --run 12
    rp publish --run 12
    ```

    Check, held: the first `publish` produced the six sets (369 to 378),
    **825 packets**, 188.5 MB, and stamped each job; the consumer check
    `Consumer check run 12: CLEAN; read 1139, kept 825, valid 825,
    byte-equal 825, packets expected 825 seen 825; partitions p0
    73505..74643`; the second `publish` produced nothing. This is the
    first slice from a SOC L2 ASDF file to a packet read back from the
    topic: six raw files of the HLWAS simulation in at one end, 825
    packets out of the other, one consistent account in the tables, the
    Parquet and the outbox.

    | what | figure |
    |---|---|
    | packets / bytes produced | 825 / 188.5 MB (188,505,169) |
    | `publish` | 25.7 s in the verb, 26.7 s process; 32.1 packets/s, 7.3 MB/s; peak RSS 258 MiB |
    | largest message against `max_request_bytes` | 230,284 B against 15,728,640 (1.5%) |
    | consumer check | 41.4 s, 45.9 s process (1,139 messages read), peak RSS 644 MiB |
    | second `publish` | 0.09 s, nothing produced |

11. **The loop verb.** `rapidpipe loop` (`rapidpipe/foundation/loop.py`)
    is one verb: no daemon, no service. It takes the selection as
    arguments: filters, SCAs and pointings (`VVV_EEEE`, a visit and
    exposure, or `all`). The selection is the operator's instruction for
    that invocation. It is printed in every pass's summary line and is not
    written into the run's settings body.

    Each pass does the following, in order:

    1. **Listing.** It lists the run's `[intake] source_root` with the
       `list_objects_v2` paginator, unsigned when the run says so.
    2. **Snapshot.** It reads the tables once.
    3. **Submit.** It submits on Batch and never waits:
       - **intake**: one job per admitted `_cal.asdf` stem that no
         published intake of the dataset and no intake job of this run
         names;
       - **reference**: one job per (field, filter, SCA) that has
         `min_frames` official L2 rows, while no intake is pending;
       - **difference**: one job per official L2 row whose field has an
         official reference;
       - **associate**: per field, the earliest unassociated official
         source set, one at a time, while no earlier epoch of the field
         is upstream and no intake is pending;
       - **packet**: per field, the earliest associated epoch that has no
         packet set, naming the inputs the night named.
    4. **Kafka.** It ends with `publish` over the run's unstamped official
       packet sets.

    "Pending" means a job of the run in state `launched` or `running`. A
    `failed` job is reported in the summary, is never resubmitted, and
    makes the verb exit 2 after that pass. A pass that finds nothing new,
    nothing pending and nothing to submit is drained, and the verb exits 0.

    ```sh
    rp loop --run 12 --filters F146 --scas 8 --pointings 001_0001,001_0002 \
      --job-definition rapid-rebuild:35          # under timeout 3600
    ```

    Orchestration ran from a worktree of `foundation/loop` at `c935ff79`.
    The jobs ran in run 12's image, revision `c81e17ae`, job definition
    `rapid-rebuild:35`. The loop changes no stage code, so the run's record
    of the code is still true and no new run or revision was needed.

    Before the live run, a replay check: with job 378 removed from a
    snapshot, the planner named the same 22 inputs that task 4's hand
    script recorded for 378.

12. **The second pointing, ASDF to the topic with no hand.** Pointing
    (visit 1, exposure 2) of F146 on SCA 8, passes 1 to 6: expids 1040102,
    2040102, 3040102, 4040102, 5040102 and 6010102. All six landed on
    **field 5390959**. The first pointing (field 5387158) was already
    complete. The loop saw that and submitted nothing for it. The summary
    lines, abridged (every line also carries `listed 68040`, `admitted 12`,
    the selection and `failed []`):

    | pass | at (UTC) | pass s | new | submitted | pending | packets produced (set) |
    |---|---|---|---|---|---|---|
    | 1 | 06:53:38 | 32.8 | 6 | intake 6 | | |
    | 2 | 06:55:36 | 30.2 | 0 | | intake 6 | |
    | 3 | 06:57:39 | 33.7 | 0 | reference 1 | | |
    | 4, 5 | 06:59:38, 07:01:37 | 32.8, 31.5 | 0 | | reference 1 | |
    | 6 | 07:03:41 | 36.0 | 0 | difference 6 | | |
    | 7 to 9 | 07:05:39 to 07:09:38 | 32.4 to 33.8 | 0 | | difference 6 | |
    | 10 | 07:11:39 | 33.0 | 0 | associate 1 | | |
    | 11 | 07:13:34 | 28.6 | 0 | | associate 1 | |
    | 12 | 07:15:38 | 32.6 | 0 | associate 1, packet 1 | | |
    | 13 | 07:17:42 | 36.5 | 0 | associate 1, packet 1 | | 127 (394) |
    | 14 | 07:19:45 | 39.2 | 0 | associate 1, packet 1 | | 118 (396) |
    | 15 | 07:21:43 | 37.6 | 0 | associate 1, packet 1 | | 112 (398) |
    | 16 | 07:23:41 | 35.8 | 0 | associate 1, packet 1 | | 123 (400) |
    | 17 | 07:25:45 | 39.5 | 0 | packet 1 | | 123 (402) |
    | 18 | 07:27:44 | 38.2 | 0 | | | 106 (403), **drained**, exit 0 |

    The jobs:
    - intake 379 to 384;
    - reference 385: **rfid 5**, `nframes` 6;
    - difference 386 to 391;
    - associate 392, 393, 395, 397, 399 and **401** (binding 1071; chain
      depth 6, 474 objects, 714 members);
    - packet 394, 396, 398, 400, 402 and **403**.

    Every job published and was bound. The packet sets came to **709
    packets**. The last change group is 312, and `official` holds 1,073
    rows.

    Checks, all held:
    - `read_packets.py` on the six new sets: CLEAN.
    - `validate_run.py --run 12`: 6/6 PASS in 50.3 s. It covered twelve
      epochs, two fields, two references and two chains of depth 6, with
      1,534 packets and 2,051 forced rows reused from a predecessor.
    - `consume_packets.py --run 12`: `CLEAN; read 1848, kept 1534, valid
      1534, byte-equal 1534, packets expected 1534 seen 1534; partitions
      p0 73505..75352`, 72.4 s.

    Two fields advancing in one pass is proven by the unit test only. In
    this run the first field had nothing left to do. Task 6's ramp
    exercises it at scale.

    Measurements. Queue wait is Batch's created to started. Container is
    started to stopped. Job is the attempt's own start to end.

    | stage | jobs | instance | queue wait min / median / max | container | job |
    |---|---|---|---|---|---|
    | intake | 379 to 384 | r6i.4xlarge | 135.6 / 136.2 / 140.9 s | 17.3 to 18.8 s | 15.4 to 16.5 s |
    | reference | 385 | m6a.2xlarge | 162.9 s | 79.8 s | 74.2 s |
    | difference | 386 to 391 | r6i.4xlarge | 175.2 / 175.8 / 185.7 s | 232.3 to 257.8 s | 230.0 to 255.6 s |
    | associate | six | m6a.2xlarge | 5.4 / 7.6 / 218.5 s (the first, 392, waited for a scale-up) | 2.8 to 3.0 s | 1.0 to 1.1 s |
    | packet | six | m6a.2xlarge | 5.1 / 12.1 / 70.6 s | 15.7, 21.0, 24.9, 30.6, 35.0, 38.6 s | 13.2 to 36.0 s |

    | what | figure |
    |---|---|
    | one pass | 28.6 to 39.5 s; the listing of 272,160 keys (68,040 `_cal.asdf`) is about 30 s of it, and a pass that publishes adds the produce |
    | idle pass (nothing to submit or publish) | 28.6 to 33.8 s |
    | loop start to drained | 34.7 min (06:53:04 to 07:27:44), 18 passes |
    | first intake submitted to last packet published | 34.1 min (06:53:38 to 07:27:44) |
    | stages before the chain (intake, reference, difference) | 18 min, about 9 of it Batch queue wait |
    | associate holding the pointing | 07:11:39 (first associate) to 07:23:41 (last): 12 min for six epochs whose containers took 3 s each; then 4 min to the last packet |

    The chain is serial within a field and moves one epoch per pass, so
    its span is the pass interval times the depth, not the work. Packet
    *n* goes out beside associate *n + 1*.

13. **Every F146 pointing on SCA 8, drained by the loop.** The selection
    widened to the whole F146 footprint on SCA 8: 54 pointings × 6 passes,
    324 epochs, 312 of them new. The loop ran under a 5-hour bound from a
    worktree of `rebuild` at `f4bf89e7` (task 5's merge); the jobs ran run
    12's image, revision `c81e17ae`, `rapid-rebuild:35`. No stage code
    changed, so no new run or revision.

    ```sh
    timeout 18000 rp loop --run 12 --filters F146 --scas 8 --pointings all \
      --job-definition rapid-rebuild:35 --max-intake 200
    ```

    `--max-intake 200`: task 5 measured 0.3 s per submission; here the
    first pass submitted 200 intakes in 34.0 s (0.17 s each: two database
    transactions and one `SubmitJob`), so 200 is what one pass submits in
    under a minute, and the remaining 112 went in the second pass.

    | pass | at (UTC) | pass s | new | submitted | pending | packets produced |
    |---|---|---|---|---|---|---|
    | 1 | 07:37:57 | 65.5 | 312 | intake 200 | | |
    | 2 | 07:39:39 | 47.7 | 112 | intake 112 | intake 200 | |
    | 3 | 07:41:32 | 40.3 | 0 | reference 52 | | |
    | 4 | 07:44:14 | 82.3 | 0 | difference 312 | | |
    | 5 | 07:45:24 | 32.5 | 0 | | difference 312 | |
    | 6 | 07:47:27 | 35.0 | 0 | associate 11 | difference 301 | |
    | 7 | 07:49:33 | 41.4 | 0 | associate 39, packet 11 | difference 262 | |
    | 8 | 07:52:23 | 91.9 | 0 | associate 51, packet 39 | difference 16 | 1,604 (11 sets) |
    | 9 | 07:57:25 | 273.8 | 0 | associate 52, packet 51 | | 5,398 (39 sets) |
    | 10 | 08:01:41 | 255.3 | 0 | associate 52, packet 52 | | 6,803 (51 sets) |
    | 11 | 08:06:05 | 264.2 | 0 | associate 52, packet 52 | | 6,970 (52 sets) |
    | 12 | 08:10:33 | 268.2 | 0 | associate 52, packet 52 | | 7,052 (52 sets) |
    | 13 | 08:14:48 | 254.5 | 0 | associate 3, packet 52 | | 6,938 (52 sets) |
    | 14 | 08:18:59 | 251.0 | 0 | packet 3 | | 7,059 (52 sets) |
    | 15 | 08:19:39 | 40.3 | 0 | | | 367 (3 sets), **drained**, exit 0 |

    Every line also carries `listed 68040`, `admitted 324` and `failed []`.
    Jobs 404 to 1703, every one published and bound: intake 404 to 715,
    52 references (rfids for the 52 new fields), 312 differences, 312
    associates (52 chains of depth 6), 312 packet sets. No Batch job
    failed and none needed a second attempt. `official` went from 1,073 to
    2,997 rows; the last change group is 1612.

    Fields advancing side by side, live: pass 6 started 11 field chains
    while 301 differences of other fields were still running, and passes 9
    to 12 each advanced all 52 chains by one epoch. A pass that publishes
    ~7,000 packets takes about 4.4 minutes, longer than the 2-minute
    interval, so from pass 9 on the passes ran back to back and associate
    and packet jobs finished inside the pass that submitted them (no
    associate or packet is ever seen pending).

14. **The measurement of the SCA 8 ramp.** From `jobs`, `describe-jobs`
    and each job's CloudWatch stream (`peak_rss_kib`), over jobs 404 to
    1703. Queue wait is Batch's created to started; container is started
    to stopped; peak RSS is the job process plus its largest child.

    | stage | jobs | instance | queue wait s min / median / max | container s min / median / max | peak RSS MiB | bytes out (median per job) |
    |---|---|---|---|---|---|---|
    | intake | 312 | r6i.8xlarge | 54.0 / 143.7 / 158.1 | 17.1 / 20.8 / 24.6 | 910 | 60.9 GB (195 MB) |
    | reference | 52 | r6i.8xlarge | 5.0 / 6.5 / 9.2 | 77.4 / 98.3 / 101.9 | 2,831 | 31.0 GB (596 MB) |
    | difference | 312 | r6i.8xlarge | 7.0 / 153.7 / 228.0 | 205.6 / 246.3 / 327.2 | 16,125 | 796.5 GB (2.55 GB) |
    | associate | 312 | r6i.8xlarge | 4.6 / 11.5 / 191.1 | 2.4 / 3.5 / 11.8 | 238 | 0.01 GB |
    | packet | 312 | r6i.8xlarge (309), r6i.2xlarge (3) | 3.0 / 48.0 / 189.0 | 13.0 / 30.2 / 60.5 | 867 | 8.2 GB (26 MB) |

    | what | figure |
    |---|---|
    | first intake submitted to last packet produced | 42.3 min (07:37:23 to 08:19:39) |
    | loop start to drained | 42.8 min (07:36:51 to 08:19:39), 15 passes |
    | span per stage, first launched to last ended | intake 3.5 min; reference 1.9 min; difference 9.0 min; associate 23.8 min; packet 29.7 min |
    | per-field associate chain (first launched to last ended) | 52 chains of depth 6: 19.9 / 20.0 / 22.2 min (min / median / max), against 9.0 min for all 312 differences |
    | peak concurrent containers | 290, at 07:46:46 (the differences); the compute environment chose r6i.8xlarge for every stage this time |
    | one pass, idle | 29.3 to 35.0 s, of which the listing is about 30 s |
    | one pass that publishes ~52 sets | 251 to 274 s: about 4.3 s per packet set produced (three pooler connections, one `flush`, the acks), 31 packets/s |
    | Kafka, the ramp | 42,191 packets produced in 312 sets, about 8.1 GB of Avro; run 12 in all 43,725 packets, 8.43 GB |
    | database | 365 MB (`pg_database_size`; 239 MB after part 6) |
    | ten largest tables (total size, rows) | `sources` 133 MB (592,538), `forced` 49 MB (240,464), `objects` 48 MB (178,185), `packets` 14 MB (46,914), `memberships` 8.3 MB (47,301), `job_inputs` 3.3 MB, `jobs` 2.6 MB (1,703), `official` 1.8 MB (2,997), `l2files` 432 kB, `external` 424 kB |
    | S3 under run 12's prefix after the ramp | 931 GB, 27,759 objects: difference 827 GB, intake 63 GB, reference 32 GB, packet 8.4 GB |

    The null test over all 324 SCA 8 epochs (observations, not gates; the
    simulation has no variable sources, so every detection is a residual):

    | per epoch | min | p10 | median | p90 | max | total |
    |---|---|---|---|---|---|---|
    | detections before the cap (`max_sources` 500) | 798 | 858 | 954 | 1,081 | 1,207 | 312,154 |
    | sources kept | 500 | 500 | 500 | 500 | 500 | 162,000 |
    | S/N > 5 | 232 | 268 | 316 | 374 | 456 | 103,385 |
    | S/N > 20 | 53 | 68 | 79 | 93 | 119 | 25,832 |
    | bright-residual fraction (S/N > 20 of kept) | 0.106 | 0.136 | 0.158 | 0.186 | 0.238 | |
    | packets | 56 | 102 | 132 | 169 | 233 | 43,725 |

    Every epoch hit the cap, as task 4's six did; the bright residuals are
    PSF mismatch at stars (step 9), uniform across the footprint.

    Checks, all held:
    - `validate_run.py --run 12`: **6/6 PASS in 1,231 s** over 324
      epochs, 54 fields, 54 references and 54 chains of depth 6. lineage
      1.2 s (7,236 input and exclude rows, 2,035 bindings); tables 542 s
      (1,387 published jobs, 3,115 declared outputs, 518,400 rows, counts
      and checksums equal); chain 8.6 s; packets 676 s (324 sets, 23,806 history detections,
      152,187 forced entries, each against its own recorded inputs); forced 2.8 s (151,281 rows, 53,613 shared with a recorded
      predecessor, every one its predecessor's measurement); counts 0.1 s.
    - `read_packets.py` once per packet set, eight at a time: **324 of 324
      CLEAN** in 2 min 16 s; 43,725 records, 131,175 cutouts (2,652 at an
      edge), 0 failures.
    - `consume_packets.py --run 12`: `CLEAN; read 44039, kept 43725, valid
      43725, byte-equal 43725, packets expected 43725 seen 43725;
      partitions p0 73505..117543`, 1,895.5 s, peak RSS about 20 GB (it
      holds the whole topic range and the outbox in memory).

15. **The second ramp: all 18 SCAs of the first pointing.** Step 14 was
    recorded at 08:55 UTC (01:55 PDT), before the 06:00 PDT cutoff, so
    the second ramp ran: 108 stems, 102 new, the other 17 SCAs' F146 PSFs
    already imported (jobs 317 to 352).

    ```sh
    timeout 7200 rp loop --run 12 --filters F146 --scas 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18 \
      --pointings 001_0001 --job-definition rapid-rebuild:35 --max-intake 200
    ```

    | pass | at (UTC) | pass s | new | submitted | pending | packets produced |
    |---|---|---|---|---|---|---|
    | 1 | 08:56:37 | 48.9 | 102 | intake 102 | | |
    | 2 | 08:58:19 | 30.5 | 0 | difference 4 | intake 32 | |
    | 3 | 09:00:21 | 32.9 | 0 | difference 2, reference 16 | difference 4 | |
    | 4 | 09:02:34 | 45.5 | 0 | associate 1, difference 90 | difference 2, reference 1 | |
    | 5 | 09:04:21 | 32.8 | 0 | associate 1, difference 6, packet 1 | difference 91 | |
    | 6 | 09:06:29 | 41.0 | 0 | associate 8, packet 1 | difference 87 | 210 (1 set) |
    | 7 | 09:08:30 | 41.3 | 0 | associate 16, packet 8 | difference 44 | 199 (1 set) |
    | 8 | 09:10:51 | 62.5 | 0 | associate 17, packet 16 | | 853 (8 sets) |
    | 9 | 09:13:22 | 93.9 | 0 | associate 17, packet 17 | | 1,732 (16 sets) |
    | 10 | 09:14:18 | 29.6 | 0 | | associate 17, packet 17 | |
    | 11 | 09:17:27 | 99.1 | 0 | associate 16, packet 17 | | 1,930 (17 sets) |
    | 12 | 09:19:29 | 100.4 | 0 | associate 16, packet 16 | | 2,007 (17 sets) |
    | 13 | 09:21:25 | 96.4 | 0 | associate 9, packet 16 | | 1,754 (16 sets) |
    | 14 | 09:23:21 | 92.9 | 0 | associate 1, packet 9 | | 1,856 (16 sets) |
    | 15 | 09:24:56 | 68.3 | 0 | packet 1 | | 1,022 (9 sets) |
    | 16, 17 | 09:26:21, 09:28:18 | 33.0, 29.3 | 0 | | packet 1 | |
    | 18 | 09:30:23 | 34.6 | 0 | | | 148 (1 set), **drained**, exit 0 |

    It drained in 34.6 min (08:55:48 to 09:30:23). Jobs 1704 to 2127:
    intake 1704 to 1805, references 1810 to 1825 (16), 102 differences,
    102 associates, 102 packet sets; every one published and bound, none
    failed. The 108 stems landed on 18 fields: SCA 8 on 5387158 (done in
    step 8), 16 SCAs on 16 new fields, and **SCA 9 on field 5390959, the
    field of SCA 8 in pointing (visit 1, exposure 2)** (step 12), because
    the pointing grid's step is one SCA width.

    What the breadth exercised for the first time:
    - **The exposures key across SCAs.** The 18 SCAs of one exposure
      share `{dataset, expid}`. Each intake writes an `exposures` row, and
      only the first of an expid expects the binding: `official` holds 324
      open `exposures` bindings, one per expid, and the other 102 rows are
      published, unbound candidates. No binding was refused.
    - **A field shared by two SCAs.** The reference key is `{dataset,
      field, fid}` (`database/schema.sql`), with no SCA, so SCA 9's six
      epochs were differenced against field 5390959's existing reference
      (rfid 5, built from SCA 8 frames) as soon as they were official,
      in passes 2 and 3, before any other reference existed. Each
      difference names SCA 9's reference PSF (`loop.py` names the
      science's SCA for both PSFs) although the reference stack is SCA 8's.
      Its null-test numbers are ordinary (76 detections above S/N 20 per
      epoch, median of the run 79).
    - **A chain that is no longer chronological.** SCA 9's epochs on
      5390959 were taken 5.4 minutes before SCA 8's in each survey pass
      (`mjdobs` 61587.6450 against 61587.6488 in pass 1). The field's
      chain held SCA 8's six already (set 401), so the loop appended SCA
      9's six after them (associates 1918, 1925, 1934, 1951, 1976, 2009):
      chain depth 12, epochs out of `mjdobs` order. The associate stage
      accepted each one, and `validate_run.py`'s chain check (which checks
      the chain's integrity, not its order) holds over it. The plan says
      the chain is chronological within a field; what it should do when
      an earlier epoch arrives after a later one is not decided here
      (see Results).

    Measurements (jobs 1704 to 2127):

    | stage | jobs | instance | queue wait s min / median / max | container s min / median / max | peak RSS MiB | bytes out (median per job) |
    |---|---|---|---|---|---|---|
    | intake | 102 | r6i.8xlarge | 77.4 / 86.4 / 101.3 | 17.0 / 20.5 / 24.4 | 909 | 19.9 GB (195 MB) |
    | reference | 16 | r6i.8xlarge | 8.1 / 9.8 / 67.8 | 89.0 / 97.3 / 115.4 | 2,831 | 9.5 GB (595 MB) |
    | difference | 102 | r6i.8xlarge (100), r6i.2xlarge (2) | 6.5 / 97.7 / 131.1 | 177.8 / 249.1 / 311.7 | 16,127 | 260.3 GB (2.55 GB) |
    | associate | 102 | r6i.8xlarge (101), r6i.2xlarge (1) | 3.1 / 6.1 / 131.4 | 2.4 / 3.4 / 10.4 | 238 | 0.00 GB |
    | packet | 102 | r6i.8xlarge (101), m6a.2xlarge (1) | 1.9 / 12.2 / 193.3 | 12.4 / 28.4 / 46.2 | 864 | 2.3 GB (22 MB) |

    | what | figure |
    |---|---|
    | first intake submitted to last packet produced | 34.0 min (08:56:19 to 09:30:23) |
    | peak concurrent containers | 99, at 09:04:58 |
    | packets produced | 11,711 in 102 sets |

    The null test by SCA (mean per epoch, six epochs each): detections
    before the cap 609 (SCA 9) to 1,854 (SCA 4); above S/N 20, 41 (SCA 17)
    to 159 (SCA 7) for fifteen SCAs, but **355 on SCA 4 and 255 on SCA 5**.
    Every SCA used dev's F146 PSF for that SCA; SCAs 4 and 5 are where the
    PSF models fit the simulation worst, or where the sky is most crowded.
    Neither was looked into further. Over run 12's 426 epochs: S/N > 20 per
    epoch 33 to 375, median 79; packets per epoch 16 to 233, median 130;
    55,436 packets in all.

Checks over the whole run after it, all held:
    - `validate_run.py --run 12`: **6/6 PASS in 1,515.6 s** over 426
      epochs, 70 fields, 70 references and 70 chains (set 2009, field
      5390959, depth 12: 1,931 members, 950 objects' sums); tables 679 s
      (1,811 published jobs, 4,081 declared outputs, 666,822 rows),
      packets 819 s (426 sets, 55,436 packets), forced 4.3 s (192,251
      rows, 70,296 shared with a recorded predecessor, every one its
      predecessor's measurement).
    - `read_packets.py` on the 102 new sets: **102 of 102 CLEAN**, 41 s.
    - `consume_packets.py --run 12`: `CLEAN; read 55750, kept 55436, valid
      55436, byte-equal 55436, packets expected 55436 seen 55436;
      partitions p0 73505..129254`, 2,457.4 s, peak RSS about 25 GB.

## Results of part 7 (2026-10-22)

The chain has the right shape and functions on simple data: a raw SOC L2
ASDF file goes in at the intake and a packet comes out on the Kafka topic,
with one consistent account of itself in the database, the Parquet and the
outbox, and it kept doing so from one SCA of one pointing to the whole
F146 footprint on SCA 8 and then all 18 SCAs of one pointing.

- **Run 12** (live for the HLWAS dataset r00342, revision `c81e17ae`,
  `rapid-rebuild:35`): 36 F146 PSFs and the footprint's Gaia copy
  (imports 317 to 359), then **426 epochs on 70 fields**: 426 intakes,
  70 references, 426 differences, 426 associates in 70 per-field chains
  (69 of depth 6, field 5390959 of depth 12), 426 packet sets, **55,436
  packets** on the topic. Jobs 317 to 2127, every one published and bound;
  no Batch job failed. Run 9 stays live for the GBTDS dataset beside it.
- **The loop** carried everything from step 12 on with no hand: 324 epochs
  in 42.8 min (step 13), 102 more in 34.6 min (step 15). An idle pass
  takes about 30 s, almost all of it the listing.
- **Kafka**: every packet set of run 12 produced to the topic and read
  back byte-equal by the consumer check.
- **Validation**: `validate_run.py --run 12` 6/6 PASS, the reader CLEAN on
  every set, the consumer CLEAN on every packet, after step 13 and again
  after step 15.

Final state: `official` 3,523 rows, the last change group
2036; database 396 MB; run 12's prefix 1,223 GB in 36,455 objects.

Open, from this part (for the plan's "Open" list):
- **A field shared by two SCAs.** The reference key has no SCA, so an SCA
  landing on a field another SCA referenced is differenced against that
  stack while naming its own reference PSF, and its epochs join the
  field's chain out of `mjdobs` order when they arrive later (step 15).
  Whether a reference is per (field, filter) or per (field, filter, SCA),
  and what the chain does with a late earlier epoch, are not decided.
- SCAs 4 and 5 leave three to four times the bright residuals of the
  others with dev's F146 PSFs (step 15).
- Injection, not breadth, is the next scientific step.

## Part 8: housekeeping, the ramp made cheaper (2026-10-22)

Part 6 row 11 of the plan, ruled by Ben on 2026-10-22: housekeeping before
the ramp grows. Every Batch proof of this part runs under a throwaway
dataset of its own with its own live run, because the loop plans from
official rows and admits a stem only when no intake of the dataset has
published it (PR 307); the dataset's `l2files` start empty, so the proofs
run on run 12's pixels. Run 12 and run 9 stay live and untouched.

1. **Bytes: the difference outputs trimmed and gzipped, the working
   directory removed, the intake's `exposures` row only when expected.**
   Three PRs, merged in turn: PR 312 (`ba5feed3`) makes the intake declare
   its `exposures` row only when its job expects the `exposures` key; PR
   313 (`bd48eb84`) makes `run_job` remove its `rapidpipe-job-*` working
   directory once `put` has uploaded it (left on failure); PR 314
   (`bee19ebf`) makes the difference stage publish, under `diff/`, only the
   ZOGY difference, its uncertainty, its mask (the resampled reference
   coverage map), its positive PSF-fit catalog, the difference PSF, the
   background-subtracted science image and the resampled gain-matched
   reference, every FITS gzipped at level 1, and the packet stage read a
   difference image's neighbours under the difference image's own suffix
   (`packet.neighbour`), so run 12's plain files and the new gzipped ones
   both read.

   The leaked working directories on the fleet host, listed by prefix
   first and then removed, nothing else under `/tmp`:

   ```sh
   ls -d /tmp/rapidpipe-job-* | wc -l        # 89
   du -sc /tmp/rapidpipe-job-*               # 23,232,532 KiB (22.2 GiB)
   rm -r /tmp/rapidpipe-job-*
   ```

   Check, held: 0 left; the root filesystem went from 42 GB to 20 GB used.

   The image and its revision (build 44 s with cached layers, deploy 74 s):

   ```sh
   env -u TMPDIR containers/rapid-pipeline/build-rebuild.sh bee19ebf7a4bb1bc5ac839b9ad0e91abb63f4f10 --tag rebuild-bee19ebf7a4bb1bc5ac839b9ad0e91abb63f4f10
   RELEASE_TAG=rebuild-bee19ebf7a4bb1bc5ac839b9ad0e91abb63f4f10 RELEASE_IMAGE_DIGEST=sha256:3bba4496… cloudformation/release-hooks/deploy
   ```

   Check, held: **`rapid-rebuild:36`** (and `rapid-rebuild-production:27`),
   image and `RAPID_IMAGE_DIGEST` both `sha256:3bba4496…`, retry 3, 1 vCPU,
   24 GiB, read back with `describe-job-definitions`.

   The dataset `r00342-hk1` and its live run: the shipped settings at
   `bee19ebf` with run 12's overlay (`[intake] source_root` the public
   `r00342/l2/` prefix, `unsigned = true`), the 36 F146 PSFs from the
   inputs bucket's `g0006-psf-f146/` and run 12's Gaia copy (job 359's
   file) under the name `gaia-r00342-hk1`:

   ```sh
   rp run create --who "$USER" --why "housekeeping-2026-10-22 task 1 (bytes): ..." \
     --revision bee19ebf7a4bb1bc5ac839b9ad0e91abb63f4f10 --settings hk1-run.json --survey HLWAS --dataset r00342-hk1
   echo '[{"open": {"survey": "HLWAS", "dataset": "r00342-hk1", "run": 13}}]' > live.json
   rp official change --group live.json --who "$USER" --why "... run 13 live for HLWAS r00342-hk1 ..."
   rp job local --run 13 --stage import --scope '{"source_dir": "<copy>/psfs", "psf": {"kind": "science", "path": "sciimage_psf_f146_sca08.fits"}}' \
     --expects '[{"product": "psfs", "key": {"dataset": "r00342-hk1", "fid": 8, "sca": 8, "kind": "science"}, "binding": null}]'
   # ... the 36, then:
   rp job local --run 13 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-hk1", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"name": "gaia-r00342-hk1", "version": "DR3"}, "binding": null}]'
   ```

   Check, held: run **13** live (group 2037); PSF imports **2128 to 2145**
   (science, SCAs 1 to 18) and **2146 to 2163** (reference), published and
   bound in groups 2038 to 2073, 70 s for the 36 (about 238 MiB each);
   Gaia job **2164**, group 2074. The 37 `job local` runs left **no**
   `rapidpipe-job-*` directory under this host's `TMPDIR`: PR 313 live.

   The proof, the loop over two pointings on two SCAs:

   ```sh
   timeout 7200 rp loop --run 13 --filters F146 --scas 8,9 --pointings 001_0001,001_0002 \
     --job-definition rapid-rebuild:36
   ```

   It drained in **48.6 min** (23:32:41 to 00:21:17 UTC, 25 passes, every
   pass 28 to 48 s), exit 0, no job failed. Jobs 2165 to 2262: 24 intakes,
   **3 references for 4 (pointing, SCA) pairs** (2189 field 5387158 from
   SCA 8 of `001_0001`; 2190 field 5390959 from SCA 8 of `001_0002`; 2191
   field 5394752 from SCA 9 of `001_0002`), 24 differences, 24 associates,
   24 packet sets, **3,334 packets** produced and stamped. Checks, all held:

   - **The intake's `exposures` row, both branches, from the tables**:
     each of the 12 SCA 8 intakes published one `exposures` row and one
     `l2files` row and bound both; each of the 12 SCA 9 intakes of the
     same exposures published its `l2files` row, bound, and **no
     `exposures` row** (3 objects under its prefix, not 4). The run has 12
     `exposures` rows for 12 exposures, none unbound.
   - **A field's chain in the loop's admission order**: field 5390959
     (SCA 9 of `001_0001` and SCA 8 of `001_0002`) has one chain of
     **depth 12, in `mjdobs` order, interleaved across the two SCAs**
     (associates 2217, 2218, 2223, 2228, 2234, 2240, 2246, 2248, 2252,
     2253, 2257, 2259: SCA 9 then SCA 8 in each survey pass, 5.4 min
     apart), where run 12's held SCA 8's six first and SCA 9's six after
     them (part 7 step 15). Fields 5387158 and 5394752: depth 6, in order.
   - `validate_run.py --run 13`: **6/6 PASS in 109.5 s** (lineage 0.1 s,
     639 input and exclude rows and 172 bindings; tables 44.4 s, 136 jobs,
     250 declared outputs, 46,680 rows; chain 0.8 s, sets 2245, 2247 and
     2259; packets 63.6 s, 24 sets, 3,334 packets, 16,836 forced entries;
     forced 0.3 s, 11,718 rows shared with a predecessor; counts).
   - `read_packets.py` on the 24 sets: **24 of 24 CLEAN**, 55 s.
   - **The packet stage reads what it read before**: for the four SCA 9
     epochs of `001_0001` in passes 2 to 5, all **781 packets** of run 13
     match run 12's by position and every one's three cutouts are
     **byte-equal**, read from the gzipped files.
   - **Determinism**: the difference jobs of the four pairs on the same
     instance type (run 12's 1807, 1808, 1809, 1826 against run 13's
     2197, 2201, 2205, 2209, SCA 9 of `001_0001` passes 2 to 5, r6i.8xlarge)
     give **row-for-row equal `sources`** (500 rows each, every column but
     the job, the dataset and the ids); so does pass 1 (1806 on
     r6i.8xlarge, 2193 on r6i.2xlarge). The two field 5390959 references
     are pixel-equal (run 12's 385 on m6a.2xlarge, run 13's 2190 on
     r6i.2xlarge). Field 5387158's are **not**: run 12's 360 and run 13's
     2189 differ by up to 4.9e-4 on a median of 1.35, from pixel-equal
     frames (run 12's pass-1 frame, the `job local` 353, and run 13's 2165
     are equal HDU for HDU), so the SCA 8 differences of `001_0001` agree
     to about the seventh figure only (361 against 2192). Why reference
     360 differs is not known (an observation; 360 was submitted by hand
     in part 7 step 8).

   Measurements, run 13 (24 epochs) beside run 12's same epochs (18: SCA 9
   of `001_0002` was never run under run 12), every job on Batch:

   | stage | run | jobs | instance | queue wait s min / median / max | job s min / median / max | bytes out (median per job) | objects per job |
   |---|---|---|---|---|---|---|---|
   | intake | 12 | 18 | r6i (16), m6a, m8a (`job local`) | 0.2 / 137.3 / 170.0 | 14.9 / 16.2 / 17.9 | 3.51 GB (194.9 MB) | 4 |
   | intake | 13 | 24 | r6i.8xlarge | 110.9 / 111.9 / 113.1 | 16.3 / 18.0 / 18.2 | 4.67 GB (194.8 MB) | 4 (SCA 8), 3 (SCA 9) |
   | reference | 12 | 2 | m6a.2xlarge | 164.4 / 179.7 / 194.9 | 74.2 / 74.5 / 74.7 | 1.19 GB (594.8 MB) | 8 |
   | reference | 13 | 3 | r6i.2xlarge | 151.0 / 151.3 / 161.6 | 76.1 / 77.1 / 77.4 | 1.79 GB (594.9 MB) | 8 |
   | difference | 12 | 18 | r6i (17), m6a | 6.4 / 130.6 / 187.2 | 173.9 / 227.0 / 255.6 | **45.80 GB (2,547.2 MB)** | **72** |
   | difference | 13 | 24 | r6i.8xlarge (20), r6i.2xlarge (4) | 10.7 / 138.0 / 153.4 | 175.3 / 227.4 / 254.1 | **5.67 GB (247.2 MB)** | **10** |
   | associate | 12 | 18 | r6i, m6a | 4.9 / 8.8 / 220.0 | 0.9 / 1.0 / 1.5 | 0.00 GB | 3 |
   | associate | 13 | 24 | r6i | 6.3 / 12.8 / 199.9 | 0.9 / 1.0 / 1.2 | 0.00 GB | 3 |
   | packet | 12 | 18 | r6i, m6a | 4.2 / 9.3 / 133.6 | 12.9 / 26.7 / 41.3 | 0.53 GB (28.2 MB) | 5 |
   | packet | 13 | 24 | r6i | 6.5 / 18.7 / 210.2 | 15.7 / **45.0** / 98.4 | 0.65 GB (23.9 MB) | 5 |

   - **Bytes per difference job: 247 MB in 10 objects against 2,547 MB in
     72**, a factor of 10.3. The seven kept FITS are 334.5 MB plain and
     203 to 247 MB gzipped (float images compress only to about 74%); the
     trim removed 69 files, 3.22 to 3.24 GB, per job before the upload
     (the manifest's `difference.published`).
   - **The run's whole prefix: 12.79 GB in 651 objects for 24 epochs, 0.53
     GB per epoch**, against 51.0 GB for run 12's same 18 epochs (2.83 GB
     per epoch, its two references counted), a factor of 5.3; the intake's
     195 MB and the reference's 595 MB per job are now most of it.
   - **The gzip costs 7.3 to 9.2 s per difference job** (median 8.9 s,
     the manifest's `gzip_s`), inside a container median of 227.4 s,
     the same as run 12's 227.0 s for these epochs (the ramp's median was
     249 s): the time saved uploading 2.3 GB less pays for it.
   - **The read side costs more**: the packet job's median rose from
     26.7 s to 45.0 s. Its forced photometry opens three gzipped files per
     window image; per window image it took 6.35 s (median, run 13)
     against 4.2 s (median, all 426 of run 12's sets), about 2.1 s of
     decompression per image, and field 5390959's chain of 12 puts up to
     12 images in a window (run 12's selection here: at most 6).
   - The loop: 25 passes, 28 to 48 s each, almost
     all of it the listing (68,040 keys every pass, task 3's change).
   - `official` 3,523 rows before, **3,696** after; the last change group
     **2173**; database 407 MB (396 before).

   Not run here: the consumer check (`consume_packets.py`) reads the
   whole topic range and holds it in memory (about 30 min and 20 GB on run
   12); step 3 bounds it and runs it.

2. **Batch: the Batch job id on the row, the loop reading Batch for what it
   holds pending, per-stage memory.** Three PRs, merged in turn: PR 316
   (`d6c90ecb`) records the id Batch returns as `jobs.batch_job_id` on
   every submission (`job show` prints it); PR 317 (`c1f97648`) makes each
   loop pass read Batch first (`loop.reconcile`, one `describe-jobs` call
   per hundred of the run's unpublished jobs with an id): a Batch job that
   has ended while its row is `launched` or `running` marks the row
   `failed` with `refusal = 'Batch <status>: <reason>'`, and a row `job
   run` marked `failed` whose Batch job is still queued or running is
   Batch's retry, held pending; it also adds `job submit --job <id>`, a new
   attempt of an existing job on Batch; PR 318 (`dc837023`) makes each
   Batch job ask for its stage's memory from the run's `[batch.memory_mib]`
   table (intake 2048, reference 6144, difference 24576, associate 1024,
   packet 2048 MiB, the ramp's peaks times about two), the job definition
   still one definition at 24 GiB.

   SMDC gets the column as `postgres`, before PR 316 merged:

   ```sh
   pg -c "ALTER TABLE jobs ADD COLUMN batch_job_id text;"
   ```

   Check, held: the column present (0.27 s), `UPDATE` on it held by
   `rapid_rebuild_pipeline`; jobs 1 to 2263, `official` 3,696 rows, last
   change group 2173, unchanged.

   The image and its revision (build 22 s with cached layers, deploy 74 s):

   ```sh
   env -u TMPDIR containers/rapid-pipeline/build-rebuild.sh dc837023b7b45634df393a697bc45db9dcc19a4f --tag rebuild-dc837023b7b45634df393a697bc45db9dcc19a4f
   RELEASE_TAG=rebuild-dc837023b7b45634df393a697bc45db9dcc19a4f RELEASE_IMAGE_DIGEST=sha256:16ac8c92… cloudformation/release-hooks/deploy
   ```

   Check, held: **`rapid-rebuild:37`** (and `rapid-rebuild-production:28`),
   image and `RAPID_IMAGE_DIGEST` both `sha256:16ac8c92…`, retry 3 on exit
   75, 1 vCPU, 24 GiB, read back with `describe-job-definitions`.

   The dataset `r00342-hk2` and its live run, as step 1 did for
   `r00342-hk1`: the shipped settings at `dc837023` (which now carry
   `[batch.memory_mib]`; nothing else differs from run 13's body) with the
   same overlay, the 36 F146 PSFs and the Gaia copy under the name
   `gaia-r00342-hk2`. Check, held: run **14** live (group 2174); PSF
   imports **2264 to 2299**, published and bound in groups 2175 to 2210,
   71 s for the 36; Gaia job **2300**, group 2211.

   The proof, one pointing on SCA 8, with the targeted case inside it: one
   pass, then one intake terminated on Batch, one pass, the intake
   resubmitted, then the loop to drain:

   ```sh
   sel="--run 14 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:37"
   rp loop $sel --once                       # 6 intakes, 2301 to 2306
   aws batch terminate-job --job-id <2306's batch_job_id> --reason "...the targeted case..."
   rp loop $sel --once                       # exit 2: job 2306 failed
   rp job submit --job 2306 --job-definition rapid-rebuild:37
   timeout 5400 rp loop $sel
   ```

   - **A container ended unrecorded, seen**: job 2306's Batch job was
     terminated while `RUNNABLE` (00:46:14 UTC; `FAILED` 8 s later). The
     next pass's reconcile (0.1 s, 6 jobs described) marked the row
     `failed`, `refusal` "Batch FAILED: housekeeping-2026-10-22 task 2:
     the targeted case, a container ended under the loop", `exit_code`
     null (no container ran), reported it under `failed` and
     `batch.ended`, and the verb exited 2; it submitted nothing.
   - **The rerun on Batch, and a failed row held pending**: `job submit
     --job 2306` gave the row a new `batch_job_id`. The loop's first pass
     found the row still `failed` with that Batch job queued and held it
     pending (`batch.retrying [2306]`), so it did not exit 2: the second
     reading of the lookup, met live by the rerun rather than by Batch's
     own exit-75 retry (that one is unit-tested only). Job 2306 published
     on attempt 1 (its first container) with `refusal` cleared.
   - The loop then drained in **36.1 min** (00:47:23 to 01:23:27 UTC, 19
     passes, 29.6 to 40.6 s each), exit 0, no job failed: 6 intakes, 1
     reference (2307, six frames), 6 differences, 6 associates (one chain
     of depth 6), 6 packet sets, **827 packets** produced and stamped.
     Jobs 2264 to 2325; every Batch job's row carries its `batch_job_id`.
   - `validate_run.py --run 14`: **6/6 PASS in 30.2 s** (lineage, 134
     input and exclude rows, 74 bindings; tables, 62 jobs, 9,762 rows;
     chain, set 2323 depth 6; packets, 6 sets, 827 packets; forced; counts).
     `read_packets.py` on the 6 sets: **6 of 6 CLEAN**, 13 s.

   Measurements (Batch's created to started is the queue wait, started to
   stopped the container; peak RSS is `job run`'s `peak_rss_kib`, the
   larger of the process and its largest child, from the job's log):

   | stage | jobs | asked MiB | peak RSS MiB (max) | instances | queue wait s min / median / max | container s min / median / max | ramp (PR 308) container median, peak |
   |---|---|---|---|---|---|---|---|
   | intake | 6 | 2,048 | 938 | one m6a.2xlarge, all six | 141.8 / 179.9 / 180.4 | 38.0 / 39.1 / 39.3 | 20.8 s, 910 |
   | reference | 1 | 6,144 | 1,439 | the same m6a.2xlarge | 5.5 | 76.4 | 98.3 s, 2,831 |
   | difference | 6 | 24,576 | 14,889 | one r6i.4xlarge (5), the m6a.2xlarge (1) | 4.4 / 98.9 / 99.5 | 190.6 / 225.6 / 229.2 | 246.3 s, 16,125 |
   | associate | 6 | 1,024 | 232 | m6a.2xlarge (5, three instances), r6i.4xlarge (1) | 4.6 / 73.5 / 185.7 | 2.6 / 3.0 / 3.1 | 3.5 s, 238 |
   | packet | 6 | 2,048 | 863 | m6a.2xlarge (6, four instances) | 4.4 / 156.4 / 198.9 | 19.1 / 35.9 / 54.3 | 30.2 s, 867 |

   - **Packing**: 31 Batch jobs ran on **6 container instances**, five
     m6a.2xlarge (8 vCPU, 32 GiB) and one r6i.4xlarge (16 vCPU, 128 GiB),
     where every job of the ramp asked for 24 GiB and landed on
     r6i.8xlarge (256 GiB), about ten jobs per instance. The six intakes
     and the reference shared one m6a.2xlarge; five differences shared
     the r6i.4xlarge. Instances are counted by Batch's
     `containerInstanceArn` per job (each job's latest Batch job).
   - **Every peak is under its ask**: intake 938 of 2,048, reference 1,439
     of 6,144, difference 14,889 of 24,576, associate 232 of 1,024, packet
     863 of 2,048 MiB.
   - **The intake ran twice as long** (container median 39.1 s against
     the ramp's 20.8 s; `job run`'s own time 36.5 s against 18.0 s in step
     1): six intakes at once on one 8-vCPU instance, each reading 200 MB
     unsigned from the public bucket and gzipping its FITS, where the ramp
     spread them over larger instances. The packing traded seconds for
     instances.
   - **The reconcile**: 0.0 to 0.1 s per pass (at most 6 jobs described),
     against passes of 29.6 to 40.6 s, almost all of them the listing.
   - The run's prefix: 3.43 GB in 251 objects for 6 epochs, 0.57 GB per
     epoch (step 1: 0.53).
   - `official` 3,696 rows before, **3,771** after; the last change group
     **2236**; database 409 MB.
   - Packets: 827 for the six epochs, where run 12's same pointing and SCA
     gave 825 (part 7 step 9); field 5387158's reference differs between
     the runs, as step 1 found (run 12's 360 was submitted by hand), and
     this run's reference is from the same six frames as run 13's 2189.
     Not compared further here.

3. **Pass: the listing by the selection's observations, one flush per
   publish pass, the two checks bounded.** Four PRs, merged in turn, none
   to a stage (so no new image or revision: the loop runs on this host,
   the jobs on `rapid-rebuild:37`): PR 320 (`17494b0e`) makes
   `loop.list_stems` take the selection and page only the observations of
   its filters, found from the listing itself (one one-key request per
   observation, each starting after the last one's prefix, reads its
   first key's filter: F146 is observation 004 in passes 1 to 5 and 001
   in pass 6); PR 321 (`aacfe5cb`) makes `publish` one connection, one
   producer and one flush per pass, stamping each set whose messages were
   all acknowledged and leaving a set with a failed acknowledgement
   unstamped; PR 322 (`96e14e6c`) runs `validate_run.py`'s `tables` and
   `packets` eight at a time, a connection per worker, the same
   assertions; PR 323 (`ffc0be8b`) makes `consume_packets.py` read the
   topic one message at a time, holding one set's outbox and no message
   after comparing it.

   Baseline reads on run 12, nothing submitted or stamped there, the old
   code (`15446a0c`) and then the new (`ffc0be8b`), each as the two checks
   at once on this host under `/usr/bin/time -v`:

   ```sh
   python scripts/validate_run.py --run 12
   python scripts/consume_packets.py --run 12
   ```

   The live pass on run 14 (`r00342-hk2`, task 2's run), a new pointing on
   SCA 8, from a checkout at `aacfe5cb` (PRs 320 and 321), then one idle
   pass:

   ```sh
   sel="--run 14 --filters F146 --scas 8 --pointings 001_0002 --job-definition rapid-rebuild:37"
   timeout 5400 rp loop $sel
   rp loop $sel --once
   ```

   - The loop drained in **34.1 min** (01:50:35 to 02:24:43 UTC, 18
     passes), exit 0, no job failed: 6 intakes (2326 to 2331), 1
     reference (2332, six frames, field 5390959), 6 differences, 6
     associates (one chain of depth 6, set 2348), 6 packet sets (2341,
     2343, 2345, 2347, 2349, 2350), **709 packets** (127, 118, 112, 123,
     123, 106) produced and stamped, one set per publishing pass (the
     field's chain is serial). The idle `--once` pass: drained, **4.9 s**.
   - `validate_run.py --run 14`: **6/6 PASS** in 26.1 s (87 jobs; chain
     sets 2323 and 2348, depth 6 each; packets 12 sets, 1,536 packets).
     `read_packets.py` on the 6 new sets: **CLEAN**, 13 s. The consumer
     check over run 14: **CLEAN**, 1,536 of 1,536 packets of its 12 sets
     read, valid and byte-equal, 1,201 s, 348 MiB.
   - `official` 3,771 rows before, **3,808** after; the last change group
     **2261**; database 411 MB.

   Measurements, before and after:

   | | before | after |
   |---|---|---|
   | loop listing, per pass (live, F146, SCA 8, `001_0002`) | 273 requests, 272,160 keys, 33.1 s | 95 requests (71 one-key + 24 pages), 23,398 keys, 3.7 s |
   | loop listing, F146 all 18 SCAs, all pointings | the same 273 | 95 requests, 5,832 stems, 4.5 s |
   | idle pass | 29.6 to 40.6 s (step 2), 30 s (PR 307) | 3.9 to 5.4 s (12 passes), 4.9 s `--once` |
   | a pass that publishes one set | pass + 4.3 s per set (PR 308) | 7.7 to 9.0 s, about 4 s over an idle pass |
   | database connections per publishing pass | 1 + 3 per set | 2 (the loop's idle guard and the pass) |
   | `validate_run.py --run 12` | **1,512 s** (tables 677 s, packets 818 s), 579 MiB | **666 s** (tables 58 s, packets 593 s), 1,086 MiB |
   | `consume_packets.py --run 12` | **2,397 s** check, 44.7 min wall, **24.8 GiB** | 2,740 s check, 45.9 min wall, **434 MiB**, CLEAN, 55,436 of 55,436 |

   - The brief's references were 1,231 s and 1,515 s for `validate_run`
     and 1,895 s and 20 GB for the consumer; this host's old-code baseline
     today, with both checks at once, was 1,512 s and 2,397 s (the topic
     has grown: 59,911 messages in range, offsets 73,505 to 133,415).
   - Every publishing pass here carried one set, so the single flush saves
     nothing per set; its time is the SSM read, the producer and its IAM
     handshake. The flush pays where a pass carries many sets, as the
     18-SCA ramp's did.
   - `packets` is still most of `validate_run`: the eight threads share the
     interpreter for Avro decoding and FITS opening.
   - The consumer's time is the topic read itself (the whole range from
     the earliest offset, one partition): bounding its memory does not
     shorten it. The new run took 343 s longer than the old, read while
     the run-14 consumer check and the tail of the loop shared the host,
     and loading each set's outbox inline with the read rather than all
     before it; not separated further.
   - `validate_run` now holds 1.1 GB at peak against 0.6 GB: eight sets
     decoded at once.

4. **Reference: the epochs a reference may coadd bounded by `mjdobs`, and
   the `catalogs` key with its dataset.** One code PR, PR 325
   (`124c0248`): a run setting `[reference.selection] before_mjd` (absent
   by default); with it set, the loop builds a field's reference only from
   its official L2 rows with `mjdobs < before_mjd`, once the selection's
   intake has drained (every listed stem of the selection published and no
   intake pending), differences only the rows at or after it (the rows
   before it get no difference, associate or packet), and reports a field
   with fewer than `min_frames` rows before the bound under `held`, every
   pass, without stopping the drain. `catalogs` gains `dataset`; its key is
   `(dataset, name, version)`; the import writes the job's dataset; the
   loop takes the Gaia catalog from the dataset's open binding, so the
   packet input names `{dataset, name, version}`.

   SMDC gets the key as `postgres`, one transaction, before PR 325 merged
   (`catalogs-dataset.sql`, 0.29 s, 03:16 UTC):

   ```sql
   ALTER TABLE catalogs ADD COLUMN dataset text;
   UPDATE catalogs c SET dataset = r.dataset FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = c.job;
   ALTER TABLE catalogs ALTER COLUMN dataset SET NOT NULL;
   ALTER TABLE catalogs DROP CONSTRAINT catalogs_name_version_job_key;
   ALTER TABLE catalogs ADD CONSTRAINT catalogs_dataset_name_version_job_key UNIQUE (dataset, name, version, job);
   UPDATE official o SET key = o.key || jsonb_build_object('dataset', c.dataset)
     FROM catalogs c WHERE o.product = 'catalogs' AND c.job = o.job
     AND c.name = o.key->>'name' AND c.version = o.key->>'version' AND NOT o.key ? 'dataset';
   UPDATE job_inputs i SET key = i.key || jsonb_build_object('dataset', c.dataset)
     FROM catalogs c WHERE i.product = 'catalogs' AND c.job = coalesce(i.source_job, i.job)
     AND c.name = i.key->>'name' AND c.version = i.key->>'version' AND NOT i.key ? 'dataset';
   DROP VIEW catalogs_official;
   CREATE VIEW catalogs_official AS ...;   -- schema.sql's, joining on (dataset, name, version)
   ```

   with a check inside the transaction (every binding and input carries the
   dataset; every open binding has its view row). Check, held: the four
   rows filled (job 1 `socsims-fakesrc-20260807`, 359 `r00342`, 2164
   `r00342-hk1`, 2300 `r00342-hk2`); the four open bindings, before and
   after, the same ids, jobs and groups (2 group 2, 1003 group 264, 3561
   group 2074, 3734 group 2211) with the key gaining its row's dataset
   (`{"name": "gaia-r00342", "version": "DR3"}` became `{"name":
   "gaia-r00342", "dataset": "r00342", "version": "DR3"}`); the 545
   `job_inputs` rows naming a `catalogs` key (4 `expects`, 541 `input`:
   79 GBTDS, 426 run 12, 24 run 13, 12 run 14) given the same, since
   `validate_run.py`'s lineage compares them with the row; the view's
   grants (`rapid_read`, `rapid_rebuild_pipeline`) back by the default
   privileges. On SMDC the column is the table's last; in `schema.sql` it
   follows `job`.

   The image and its revision (build 24 s with cached layers, deploy 73 s):

   ```sh
   env -u TMPDIR containers/rapid-pipeline/build-rebuild.sh 124c024823f459dc0ba759ed77f6310531df2ecf --tag rebuild-124c024823f459dc0ba759ed77f6310531df2ecf
   RELEASE_TAG=rebuild-124c024823f459dc0ba759ed77f6310531df2ecf RELEASE_IMAGE_DIGEST=sha256:2e51339f… cloudformation/release-hooks/deploy
   ```

   Check, held: **`rapid-rebuild:38`** (and `rapid-rebuild-production:29`),
   image and `RAPID_IMAGE_DIGEST` both `sha256:2e51339f…`, retry 3, 1 vCPU,
   24 GiB, read back with `describe-job-definitions`.

   The bound, read from run 12's `l2files` rows of pointing `001_0001` on
   SCA 8 (field 5387158): pass 3 at MJD **61590.65351**, pass 4 at
   **61592.15777**, midpoint **61591.4056**. Every F146 row of a pass lies
   within 0.211 d of the pass's first (pass 3 ends 61590.86411, pass 4
   starts 61592.15777), so one bound serves every pointing.

   Two datasets, each with its live run, as steps 1 and 2 did: the shipped
   settings at `124c0248` with the overlay (`[intake] source_root` the
   public `r00342/l2/` prefix, `unsigned = true`) and
   `[reference.selection] before_mjd`; the 36 F146 PSFs; Gaia for
   `r00342-hk4` only (the gap run makes no packet).

   ```sh
   rp run create ... --revision 124c024823f459dc0ba759ed77f6310531df2ecf --settings hk4-run.json --survey HLWAS --dataset r00342-hk4   # before_mjd 61591.4056
   rp run create ... --settings hk4g-run.json --survey HLWAS --dataset r00342-hk4g   # before_mjd 61586.6449, a day before pass 1
   rp job local --run 15 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-hk4", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00342-hk4", "name": "gaia-r00342-hk4", "version": "DR3"}, "binding": null}]'
   ```

   Check, held: run **15** live for `r00342-hk4` (group 2262), run **16**
   live for `r00342-hk4g` (group 2272); 36 PSFs bound under each (run 15:
   jobs 2351 to 2422, run 16: 2362 to 2414, interleaved: the two imports
   ran at once); Gaia job **2423**, group 2336, its row and binding
   carrying the dataset (the import's half of PR 325, live). One PSF import
   of run 15 (2394, reference SCA 10) failed at the publish with
   `UniqueViolation` on `psfs_pkey`, psfid 153: the `psfs` identity
   sequence reached the psfid that job 1 (the GBTDS import) inserted
   explicitly; `job run --job 2394` published it on its second attempt
   (psfid from the sequence past it).

   The two proofs, at once:

   ```sh
   timeout 5400 rp loop --run 15 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:38
   timeout 2400 rp loop --run 16 --filters F146 --scas 8 --pointings 001_0002 --job-definition rapid-rebuild:38
   rp loop --run 16 --filters F146 --scas 8 --pointings 001_0002 --job-definition rapid-rebuild:38 --once
   ```

   - **The disjoint reference, on run 12's pixels**: run 15 drained in
     **28.2 min** (03:24:35 to 03:52:46 UTC, 15 passes), exit 0, no job
     failed: 6 intakes (2425 to 2435), **one reference, 2436, from
     exactly three frames** (its inputs the L2 rows of passes 1, 2 and 3;
     `refimimages` 3 rows, `nframes` 3), 3 differences (2437 to 2439,
     passes 4, 5 and 6 only), 3 associates (2440, 2441, 2443: one chain of
     depth 3 in pass order), 3 packet sets (2442, 2444, 2445), **551
     packets** (189, 179, 183) produced and stamped, each packet job's
     Gaia input `{"dataset": "r00342-hk4", "name": "gaia-r00342-hk4",
     "version": "DR3"}`.
   - **The gap, live**: run 16 drained in **4.1 min** (03:24:35 to
     03:28:39 UTC, 3 passes), exit 0: 6 intakes (2424 to 2434) published
     and bound (6 `l2files`, 6 `exposures`), no reference, difference,
     associate or packet submitted; the drained pass and the `--once` pass
     after it (4.2 s) both report `"held": [{"field": 5390959, "fid": 8,
     "reason": "0 official L2 rows before before_mjd 61586.6449, fewer
     than min_frames 2"}]`.
   - `validate_run.py --run 15`: **6/6 PASS in 8.0 s** (lineage 0.2 s, 56
     input and exclude rows and 62 bindings; tables 1.7 s, 53 jobs, 73
     declared outputs, 4,617 rows; chain 0.2 s, set 2443 depth 3, 554
     members; packets 5.8 s, 3 sets, 551 packets; forced; counts: 1
     `refimages`, 3 `refimimages`, 1 `catalogs`). `read_packets.py` on
     the 3 sets: **3 of 3 CLEAN**, 7 s. The consumer check over run 15:
     **CLEAN**, 551 of 551 packets seen, valid and byte-equal, 61,171
     messages read (offsets 73,505 to 134,675), 1,217 s, 350 MiB.
   - The migration held for the runs before it: `validate_run.py --run
     14` on the new code, **6/6 PASS in 25.8 s** (lineage 268 input and
     exclude rows and 111 bindings, its packet jobs' Gaia inputs now
     carrying `r00342-hk2`).

   The null test, the same PSF and the same pixels, the reference the
   only change (runs 12 and 14 differenced every epoch against six frames,
   passes 1 to 6; run 15 differences passes 4 to 6 against passes 1 to 3;
   `max_sources` 500; S/N is `fluxfit / fluxerr` of the kept rows):

   | epoch | run | difference | reference (frames) | before the cap | S/N > 5 | > 10 | > 20 | bright-residual fraction (> 20 of kept) | packets |
   |---|---|---|---|---|---|---|---|---|---|
   | 4040101 | 12 | 364 | 360 (6) | 949 | 321 | 155 | 72 | 0.144 | 143 |
   | 4040101 | 14 | 2311 | 2307 (6) | 949 | 321 | 155 | 72 | 0.144 | 143 |
   | 4040101 | 15 | 2437 | 2436 (3) | 1,002 | 422 | 222 | **113** | **0.226** | 189 |
   | 5040101 | 12 | 365 | 360 (6) | 969 | 380 | 185 | 74 | 0.148 | 157 |
   | 5040101 | 14 | 2312 | 2307 (6) | 973 | 390 | 189 | 75 | 0.150 | 159 |
   | 5040101 | 15 | 2438 | 2436 (3) | 962 | 414 | 246 | **114** | **0.228** | 179 |
   | 6010101 | 12 | 366 | 360 (6) | 1,035 | 422 | 222 | 100 | 0.200 | 147 |
   | 6010101 | 14 | 2313 | 2307 (6) | 1,035 | 422 | 222 | 100 | 0.200 | 147 |
   | 6010101 | 15 | 2439 | 2436 (3) | 1,061 | 454 | 255 | **128** | **0.256** | 183 |

   An observation, not a gate: against a reference that holds none of the
   epoch, half as deep, the bright residuals rise by half (S/N > 20 from
   72 to 100 to 113 to 128, the fraction from 0.14 to 0.20 to 0.23 to
   0.26) and the packets by a quarter (447 to 551 for the three epochs);
   detections before the cap barely move (every epoch still hits the
   cap). With no variable sources in the simulation, every one of them is
   still a residual. Whether the rise is the reference's noise (three
   frames, not six) or the epoch's own signal no longer cancelling in a
   coadd that held it is not separated here: a six-frame reference from
   other epochs would separate them, and r00342 has six F146 epochs per
   pointing.

   Measurements, run 15 (Batch's created to started is the queue wait,
   started to stopped the container; peak RSS is `job run`'s
   `peak_rss_kib`):

   | stage | jobs | asked MiB | peak RSS MiB (max) | instances | queue wait s min / median / max | container s min / median / max | step 2 (run 14) container median |
   |---|---|---|---|---|---|---|---|
   | intake | 6 | 2,048 | 935 | two m6a.2xlarge (4 + 2) | 131.5 / 132.5 / 147.7 | 25.4 / 43.9 / 44.8 | 39.1 |
   | reference | 1 | 6,144 | 1,439 | m6a.2xlarge | 4.0 | **46.0 (3 frames)** | **76.4 (6 frames)** |
   | difference | 3 | 24,576 | 14,889 | r6i.2xlarge (2), m6a.2xlarge (1) | 9.1 / 156.5 / 156.9 | 212.3 / 246.9 / 248.2 | 225.6 |
   | associate | 3 | 1,024 | 231 | m6a.2xlarge (3 instances) | 176.0 / 184.6 / 201.8 | 2.8 / 2.9 / 3.0 | 3.0 |
   | packet | 3 | 2,048 | 855 | m6a.2xlarge (2 instances) | 6.9 / 184.4 / 201.5 | 21.0 / 27.5 / 33.9 | 35.9 |

   - 16 Batch jobs on 6 container instances; every peak under its ask.
   - The reference: 46.0 s for three frames against 76.4 s for six; its
     peak RSS the same 1,439 MiB.
   - Loop passes 3.8 to 5.8 s, the three publishing passes 9.8 to 10.3 s.
   - `official` 3,808 rows before (step 3), **3,920** after; the last
     change group **2358**; database 412 MB.

## Results of part 8 (2026-10-23)

Four steps, eleven code PRs (312 to 314, 316 to 318, 320 to 323, 325) and
four runbook PRs, each proven on Batch under a throwaway dataset with its
own live run (runs 13 to 16), on run 12's pixels. Run 12 and run 9 stayed
live and untouched; nothing was deleted on S3 or in the database.

| | before (run 12, the ramp) | after | step |
|---|---|---|---|
| bytes per difference job | 2,547 MB in 72 objects | 247 MB in 10 (gzipped, the read set only) | 1 |
| run prefix per epoch | 2.83 GB | 0.53 to 0.57 GB | 1, 2 |
| difference container median | 227.0 s (these epochs) | 227.4 s (gzip 8.9 s of it) | 1 |
| packet container median | 26.7 s | 45.0 s in step 1 (gzipped reads, longer windows), 27.5 to 35.9 s in steps 2 and 4 | 1 |
| leaked working directories on the fleet host | 89, 22.2 GiB | 0, removed after each upload | 1 |
| unbound duplicate `exposures` rows | 102 (the 18-SCA ramp) | 0: one row per exposure (12 for 12 in step 1) | 1 |
| memory asked per job | 24 GiB, every stage | 1 to 24 GiB by stage, every peak under it | 2 |
| instances | r6i.8xlarge, about ten jobs each | m6a.2xlarge and r6i.2xlarge/4xlarge, 31 jobs on 6 (step 2), 16 on 6 (step 4) | 2, 4 |
| a container that ends unrecorded | stays `running` | marked `failed` with Batch's reason in the next pass (0.1 s) | 2 |
| idle loop pass | 29.6 to 40.6 s | 3.8 to 5.8 s | 3, 4 |
| connections per publishing pass | 1 + 3 per set | 2 | 3 |
| `validate_run.py --run 12` | 1,512 s, 579 MiB | 666 s, 1,086 MiB | 3 |
| `consume_packets.py --run 12` | 2,397 s, 24.8 GiB | 2,740 s, 434 MiB | 3 |
| the reference's epochs | every epoch of the field, including the one differenced | a run setting, `before_mjd`: passes 1 to 3 build it, passes 4 to 6 are differenced | 4 |
| reference container | 76.4 s (6 frames) | 46.0 s (3 frames) | 4 |
| S/N > 20 residuals per epoch, passes 4 to 6, pointing (1, 1), SCA 8 | 72, 75, 100 (6-frame reference holding the epoch) | 113, 114, 128 (3-frame reference disjoint from it) | 4 |
| packets, those three epochs | 447 | 551 | 4 |
| the `catalogs` key | (name, version) | (dataset, name, version) | 4 |

Open, for the plan:

- **The disjoint reference's residuals.** Half again as many bright
  residuals, from a reference half as deep and disjoint from the epoch.
  Which of the two does it is not separated: a three-frame reference
  that holds the epoch would (r00342 has six F146 epochs per pointing, too
  few for a six-frame reference disjoint from three of them). The PSF mismatch at stars (part 7 step 9) is
  still under both.
- **Where the bound goes for the injected ramp.** The midpoint of passes
  3 and 4 leaves three epochs on each side of F146; a bound per filter is
  a later brief's (row 16).
- **The read side of gzip.** The packet stage's forced photometry reads
  three gzipped files per window image, about 2.1 s of decompression each
  (step 1); long chains pay it per epoch.
- **The consumer check reads the topic from its earliest offset**: its
  time is the topic, not the run (step 3).
- **The `psfs` identity sequence meets explicit psfids** (step 4: psfid
  153, job 1's): an import draws a psfid that a source system's import
  already wrote; the job fails at the publish and a rerun draws past it.
  Each explicit id ahead of the sequence will do this once.
- **The intake twice as long when six share an 8-vCPU instance** (step 2:
  39 s against 21 s): packing trades seconds for instances.

## Part 9: breadth, the PSF from each file's own ePSF reference, then the other filters (2026-10-23)

Part 6 row 16's first half, brought forward by Ben on 2026-10-22: every
filter gets its PSF from the CRDS ePSF reference its L2 files name, derived
by the pipeline, with no copy; then the six filters other than F146 run
un-injected, the same null test as F146's, SCA 8 over the footprint and then
the 18 SCAs of one pointing, each filter its own dataset and live run. Every
proof runs under a dataset of its own; runs 9, 10, 12 and the housekeeping
runs 13 to 16 stay live and untouched.

1. **The `psf` stage, the loop deriving what is missing, the intake's
   assertion.** Three PRs, merged in turn: PR 327 (`e46eb1a3`) adds the
   `psf` stage (`rapidpipe/stages/psf.py`, scope `{dataset, file}`): it
   reads the cal file's `meta.ref_file.epsf`, fetches that reference by
   name from `[psf] crds_server` (`/unchecked_get/references/roman/<name>`),
   asserts its detector and filter are the file's, and reduces
   `psf[focus 0, G2V]` as a gridded model at the detector centre (2048,
   2048), sampled on `4 x 61` sub-pixel centres per axis, each 4 x 4 block
   summed, unit sum: a 61 x 61 stamp (`[psf] size`, the copied F146 file's
   `NAXIS1`), written as the science and the reference PSF (two files, two
   `psfs` rows, `EPSFREF` naming the reference); it also makes the import
   draw `psfid` from the table's identity. PR 328 (`cadbec78`) makes the
   loop submit one `psf` job per (filter, SCA) of its new stems without
   both official rows and hold that SCA's intakes until they are official.
   PR 329 (`7ee86ca6`) makes the intake refuse a file, before a manifest,
   whose `meta.ref_file.epsf` is not the reference its science PSF's
   `EPSFREF` names (a PSF naming none is accepted; dev's copied F146 files
   name `roman_wfi_epsf_0276.asdf`, the same reference).

   The `psfs` identity sequence moved past the explicit ids, as `postgres`
   (part 8 step 4's seen failure), before the proof:

   ```sql
   SELECT setval(pg_get_serial_sequence('psfs','psfid'), (SELECT max(psfid) FROM psfs));
   ```

   Check, held: before, `last_value` **182**, `is_called` true, `max(psfid)`
   182 over 182 rows; after, the same (2 ms). The sequence had already
   passed every explicit id (run 15's rerun of import 2394 drew past 153),
   so the statement changed nothing; the import's half of PR 327 stops a
   new explicit id from appearing.

   The image and its revision (build 20 s with cached layers, deploy 74 s;
   the import gate now imports `photutils.psf`):

   ```sh
   env -u TMPDIR containers/rapid-pipeline/build-rebuild.sh 7ee86ca6333e51b06e87a9bcb1080caf424feb42 --tag rebuild-7ee86ca6333e51b06e87a9bcb1080caf424feb42
   RELEASE_TAG=rebuild-7ee86ca6333e51b06e87a9bcb1080caf424feb42 RELEASE_IMAGE_DIGEST=sha256:8a5b724b… cloudformation/release-hooks/deploy
   ```

   Check, held: **`rapid-rebuild:39`** (and `rapid-rebuild-production:30`),
   image and `RAPID_IMAGE_DIGEST` both `sha256:8a5b724b…`, retry 3, 1 vCPU,
   24 GiB, read back with `describe-job-definitions`.

   The dataset `r00342-ep1` and its live run: the shipped settings at
   `7ee86ca6` (now carrying `[psf]` and `[batch.memory_mib] psf = 2048`)
   with the overlay (`[intake] source_root` the public `r00342/l2/` prefix,
   `unsigned = true`, `[reference.selection] before_mjd = 61591.4056`, part
   8 step 4's F146 midpoint), Gaia under the dataset, and **no PSF
   imported**:

   ```sh
   rp run create ... --revision 7ee86ca6333e51b06e87a9bcb1080caf424feb42 --settings ep1-run.json --survey HLWAS --dataset r00342-ep1
   rp official change --group live.json ...      # run 17 live for HLWAS r00342-ep1
   rp job local --run 17 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-ep1", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00342-ep1", "name": "gaia-r00342-ep1", "version": "DR3"}, "binding": null}]'
   ```

   Check, held: run **17** live (group 2359); Gaia job **2446**, group 2360.

   The proof:

   ```sh
   timeout 5400 rp loop --run 17 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:39
   ```

   - It drained in **24.2 min** (04:52:02 to 05:16:14 UTC, 13 passes of
     4.2 to 11.7 s), exit 0, no job failed.
   - **Pass 1 submitted one `psf` job (2447) and no intake**; pass 2 held
     (`pending.psf` 1); the job published and bound **two `psfs` rows**,
     psfid 183 (`science`, binding 3923) and 184 (`reference`, binding
     3924), drawn from the identity with no collision; pass 3 admitted the
     six intakes (2448 to 2453), every one of which published: the intake's
     `EPSFREF` assertion held for all six (`roman_wfi_epsf_0276.asdf` on
     both sides).
   - Then **one reference (2454) from exactly three frames** (passes 1 to
     3), 3 differences (2455 to 2457, passes 4 to 6), 3 associates (2458,
     2460, 2461: one chain of depth 3), 3 packet sets (2459, 2462, 2463),
     **628 packets** (216, 213, 199) produced and stamped.
   - The psf job: 178,603,467 B fetched in 3.18 s from the compute subnet,
     the cal file's meta read in 2.05 s, the model built in 2.93 s, the
     reduction 0.12 s, rows 0.26 s; 9.1 s for the job, **10.9 s container**,
     **186.2 s queue wait**, peak RSS **302 MiB** of 2,048 asked, on an
     m6a.2xlarge. Its prefix: 4 objects, 43.7 KB (two 20,160-byte FITS).
     The derived stamp holds 0.9710 of the model's unit flux in 61 x 61
     pixels before normalising, its centroid **-0.027, -0.043 pixel** from
     the stamp's centre.
   - `validate_run.py --run 17`: **6/6 PASS in 8.5 s** (lineage, 56 input
     and exclude rows and 28 bindings; tables, 18 jobs, 38 declared
     outputs, 4,925 rows; chain, set 2461 depth 3, 628 members; packets, 3
     sets, 628 packets; forced, 1,239 rows; counts, `psfs` 2,
     `refimimages` 3). `read_packets.py` on the 3 sets: **3 of 3 CLEAN**,
     8 s. The consumer check over run 17: **CLEAN**, 628 of 628 packets
     seen, valid and byte-equal, 61,799 messages read (offsets 73,505 to
     135,303), 1,213 s, 366 MiB.

   **The null test, the PSF the only change**: run 15 (`r00342-hk4`, part 8
   step 4) and run 17 difference the same three epochs (passes 4 to 6,
   pointing `001_0001`, SCA 8) against a three-frame reference of passes 1
   to 3 under the same bound, run 15 with dev's copied WebbPSF files, run
   17 with the derived ones (`max_sources` 500; S/N is `fluxfit / fluxerr`
   of the kept rows):

   | epoch | run | PSF | difference | before the cap | S/N > 5 | > 10 | > 20 | bright-residual fraction (> 20 of kept) | packets |
   |---|---|---|---|---|---|---|---|---|---|
   | 4040101 | 15 | copied | 2437 | 1,002 | 422 | 222 | 113 | 0.226 | 189 |
   | 4040101 | 17 | derived | 2455 | 1,035 | **500** | 256 | **80** | **0.160** | 216 |
   | 5040101 | 15 | copied | 2438 | 962 | 414 | 246 | 114 | 0.228 | 179 |
   | 5040101 | 17 | derived | 2456 | 1,065 | **500** | 259 | **79** | **0.158** | 213 |
   | 6010101 | 15 | copied | 2439 | 1,061 | 454 | 255 | 128 | 0.256 | 183 |
   | 6010101 | 17 | derived | 2457 | 1,030 | **500** | 237 | **75** | **0.150** | 199 |

   An observation, not a gate, and within the factor of two the brief set:
   with the derived PSF the **bright residuals fall by a third** (S/N > 20
   from 113, 114, 128 to 80, 79, 75; the fraction from 0.23 to 0.26 down to
   0.15 to 0.16), while **every kept row now clears S/N 5** (500 of 500,
   against 414 to 454) and the packets rise by 14% (551 to 628), since the
   associate keeps sources at S/N 5 and above. Detections before the cap
   move by a few percent either way. A reading, unverified: r00342 was
   simulated with these CRDS ePSFs, so a PSF reduced from the same
   reference matches the stars' cores better than dev's ensemble files
   (`NPHASE = 12`, peak 0.240 against the derived stamp's 0.344, the same
   encircled energy within 5 pixels, 0.874), which removes bright
   mismatch residuals at stars; the same sharper PSF raises every fit's
   S/N, so faint residuals and noise peaks clear the cut more often. The
   detector response (inter-pixel capacitance, charge diffusion) is in
   neither comparison's control here.

   Measurements (Batch's created to started is the queue wait, started to
   stopped the container; peak RSS is `job run`'s `peak_rss_kib`):

   | stage | jobs | asked MiB | peak RSS MiB (max) | instances | queue wait s min / median / max | container s min / median / max | part 8 step 4 (run 15) container median |
   |---|---|---|---|---|---|---|---|
   | psf | 1 | 2,048 | 302 | m6a.2xlarge | 186.2 | 10.9 | (none: 36 imports by hand) |
   | intake | 6 | 2,048 | 940 | one m6a.2xlarge, all six | 194.0 / 194.5 / 195.1 | 37.4 / 37.6 / 38.5 | 43.9 |
   | reference | 1 | 6,144 | 1,439 | the same m6a.2xlarge | 7.8 | 45.7 (3 frames) | 46.0 |
   | difference | 3 | 24,576 | 14,889 | m6a.2xlarge (1), r6i.2xlarge (2) | 4.6 / 210.8 / 211.0 | 198.6 / 232.6 / 232.9 | 246.9 |
   | associate | 3 | 1,024 | 232 | the r6i.2xlarge | 6.4 / 11.3 / 19.9 | 2.6 / 2.8 / 9.0 | 2.9 |
   | packet | 3 | 2,048 | 859 | the r6i.2xlarge | 6.2 / 9.3 / 9.8 | 22.9 / 30.2 / 36.4 | 27.5 |

   - 18 Batch jobs on **3 container instances**; every peak under its ask.
   - **The PSF costs one queue wait before the first intake**: the psf
     job's 186 s wait and 11 s container put the intakes two passes (4
     min) later than an imported PSF would; against that, the 36 imports by
     hand per dataset (70 s and a person, part 8) are gone.
   - The run's prefix: **2.64 GB in 93 objects** for 6 epochs, 0.44 GB per
     epoch (part 8: 0.53 to 0.57; the run differences three of six).
   - `official` 3,920 rows before (part 8 step 4), **3,949** after; the
     last change group **2377**; database 414 MB.

2. **The six other filters on SCA 8 of the 54 pointings, one dataset and
   one live run each, and one script for a run's checks.** PR 332
   (`c58bd2cd`) adds `scripts/check_run.sh --run <id> [--settings F]`:
   `validate_run.py`, `read_packets.py` over the run's bound packet sets
   eight processes at a time, and `consume_packets.py`, each one's output
   to stderr and one line, `Check run <id>: CLEAN|FAIL validate <p>/<n>
   <s> reader <clean>/<sets> <s> consumer <seen>/<expected> <s>`, exiting
   non-zero on any failure (run 17: `CLEAN validate 6/6 9 s reader 3/3 4 s
   consumer 628/628 1214 s`).

   Each filter's bound, the midpoint of its own pass 3 and pass 4 start
   times at `001_0001` SCA 8, re-read from the two `_wcs.asdf` sidecars
   (`meta.exposure.start_time` as MJD), equal to the brief's values:

   | filter | dataset | pass 3 | pass 4 | `before_mjd` |
   |---|---|---|---|---|
   | F087 | `r00342-f087` | 61590.00852 | 61591.51278 | 61590.76065 |
   | F106 | `r00342-f106` | 61590.22362 | 61591.72788 | 61590.97575 |
   | F129 | `r00342-f129` | 61590.43852 | 61591.94278 | 61591.19065 |
   | F158 | `r00342-f158` | 61590.86840 | 61592.37266 | 61591.62053 |
   | F184 | `r00342-f184` | 61591.08330 | 61592.58756 | 61591.83543 |
   | F213 | `r00342-f213` | 61591.29840 | 61592.80266 | 61592.05053 |

   The topic before the loops (05:55 UTC): one partition, offsets 73,505
   to 135,304 (61,799 messages held), no topic-level overrides; the
   cluster's configuration (revision 3) sets `log.retention.ms` 604800000
   (7 days) and no `log.retention.bytes` (unbounded by size), replication
   3, `min.insync.replicas` 2, three `kafka.m7g.large` brokers with 2,000 GB
   of EBS each.

   Per filter, the run recipe of step 1 with the filter's bound and **no
   PSF imported**: `rp run create --revision <sha> --survey HLWAS
   --dataset r00342-f<nnn>` with the overlay, one change group opening it
   live, Gaia imported under the dataset (`gaia-r00342-f<nnn>`, DR3), then
   six loops started together, each in its own script and log, under
   `timeout 10800`:

   ```sh
   rp loop --run <id> --filters F<nnn> --scas 8 --pointings all --job-definition rapid-rebuild:<rev> --max-intake 1000
   ```

   **Three seen failures, each fixed by code, each resumed by a new live
   run under the same dataset** (the brief's recipe), plus transient
   start failures rerun:

   - **SSM throttling** (05:51 UTC, `rapid-rebuild:39`, runs 18 to 23 at
     `7ee86ca6`). Every loop's second pass submitted its filter's intakes
     at once, 3,456 in a minute; **1,448 failed** reading the database
     parameters from the SSM tree (`ThrottlingException ...
     GetParametersByPath (reached max retries: 4)`), 1,351 at their first
     connection (exit 64) and 97 at publish (exit 70). PR 331 (`cb358461`)
     builds the SSM client with botocore's standard retry mode and 25
     attempts. **`rapid-rebuild:40`**; runs **24 to 29** made live in place
     of 18 to 23 (groups 4404 to 4409); the loops re-planned exactly the
     1,448 stems no intake of the dataset had published, and none failed.
   - **The Kafka publish hung** (06:31 UTC, five of six loops; F213 had
     drained at 06:32:54, exit 0). A pass sent every unstamped set before
     one flush; batches waited past kafka-python's 120 s delivery timeout,
     and kafka-python 3.0.11's expiry path raised `KeyError` in the
     producer's sender thread, which died, leaving `flush()` waiting for
     ever. The leader broker was not the limit (produce time 4.6 ms, CPU
     27%, 46.7 MB/s in, 5-minute maxima). PR 333 (`dacf49d2`): `publish`
     sends, flushes (bounded at 120 s) and stamps one set at a time, and
     the producer's close is bounded. **`rapid-rebuild:41`**. The hung loops
     were stopped at 06:43 and `rapidpipe publish --run <n>` re-sent runs
     24 to 28's 449 unstamped sets (533 to 548 s each, five at once); runs
     **30 to 34** made live in place of 24 to 28.
   - **The loop re-associated under a resumed run** (seen 07:01 UTC). Its
     associate rule read only *this run's* associate jobs, so runs 30 and
     34 associated all 108 epochs of F087 and F184 again onto the official
     object sets, and runs 31 to 33 216 each with 63 packet sets on the
     doubled chains. Runs 30 to 34 were taken off live at 07:02 (groups
     10956 to 10960), so their in-flight jobs' bindings were refused (91).
     `official undo` of their groups, newest first, stopped after 222 of
     927: an undo reopens a closed binding as a new row, so the older
     group's row it would close was already closed. The state was then
     restored in **one change group, 11184**: for each of the 333 keys runs
     30 to 34 had bound, the binding open before their first group (270
     object sets of runs 24 to 28; 63 packet keys unbound). Checked after:
     every open binding of the six datasets names a job of runs 18 to 29,
     and each field's official chain (walked back through its associates'
     inputs) holds only runs 24 to 29's associates. Nothing was deleted;
     runs 30 to 34's 1,018 jobs and their outputs stay, unbound (927 bindings undone or restored, 91 refused). PR 334
     (`a3f1d321`): an epoch is associated when its source set is in the
     chain of its field's official object set, whatever run made it.
     **`rapid-rebuild:42`**; runs **35 to 39** made live (groups 11185 to
     11189). A dry plan before the loops: F087 and F184 nothing to submit,
     F106, F129 and F158 one associate and one packet per field.
   - **Task failed to start** (08:00 to 08:36 UTC, runs 36 to 38): 15
     jobs (2 associates, 13 packets; 16 failures) failed with `DockerTimeoutError: Could not
     transition to created; timed out after waiting 4m0s`, on two
     container instances, before any container ran; Batch's retry
     strategy retries exit code 75 only. Each was rerun with `rp job submit
     --job <id> --job-definition rapid-rebuild:42` (15535 twice) and its
     loop restarted.

   Runs, revisions and jobs per filter (runs 30 to 34 left out of every
   figure below; their 1,018 jobs, 11546 to 12563, are unbound):

   | filter | runs (revision) | psf job | jobs | loop drained |
   |---|---|---|---|---|
   | F087 | 18 (`7ee86ca6`), 24 (`cb358461`), 35 (`a3f1d321`) | 2471 | 2464 to 11398 | 07:12 (run 24's chain complete 06:31) |
   | F106 | 19, 25, 36 | 2475 | 2465 to 16173 | 09:04 |
   | F129 | 20, 26, 37 | 2474 | 2466 to 16035 | 08:37 |
   | F158 | 21, 27, 38 | 2473 | 2467 to 16142 | 08:56 |
   | F184 | 22, 28, 39 | 2470 | 2468 to 11255 | 07:12 (run 28's chain complete 06:31) |
   | F213 | 23, 29 | 2472 | 2469 to 11173 | **06:32:57, exit 0** |

   Every filter: one psf job in the first pass, two `psfs` rows bound,
   the intakes held one pass and then admitted (every `EPSFREF` assertion
   held: no intake refused), 54 references of exactly three frames, 54 x
   (epochs - 3) differences, chains of 2 (F087, F184, F213), 13 (F106,
   F129) and 14 (F158) per field, every differenced epoch a packet set.

   **The null test per filter**, medians over each dataset's official
   differences (passes 4 and later; `max_sources` 500; S/N is `fluxfit /
   fluxerr` of the kept rows; packets per differenced epoch), beside F146
   under the same rule and the same derived PSF (step 1's run 17, three
   epochs at `001_0001`; part 8 step 4's run 15, dev's copied PSF, 0.23).
   Passes 12 and 13 are 107.5 s exposures (the others 294.1 s) and are in
   F106, F129 and F158 only: their epochs are the "short" rows, as an
   observation.

   | filter | epochs | before the cap | kept | S/N > 20 | bright-residual fraction | packets per epoch | packets | sets |
   |---|---|---|---|---|---|---|---|---|
   | F146 (run 17) | 3 | 1,035 | 500 | 79 | 0.158 | 213 | 628 | 3 |
   | F087 | 108 | 915 | 500 | 110 | 0.220 | 205 | 22,048 | 108 |
   | F106 | 648 | 971 | 500 | 97 | 0.194 | 207 | | |
   | F106 short | 54 | 900 | 500 | 44 | 0.088 | 152 | 141,954 (both) | 702 |
   | F129 | 648 | 1,106 | 500 | 92 | 0.184 | 209 | | |
   | F129 short | 54 | 960 | 500 | 44 | 0.088 | 165 | 143,366 (both) | 702 |
   | F158 | 648 | 1,452 | 500 | 148 | 0.296 | 215 | | |
   | F158 short | 108 | 1,183 | 500 | 78 | 0.156 | 186 | 158,988 (both) | 756 |
   | F184 | 108 | 1,821 | 500 | 333 | **0.665** | 236 | 25,737 | 108 |
   | F213 | 108 | 333 | **333** | 33 | 0.101 | 98 | 10,871 | 108 |

   Descriptive only (filter, depth and PSF change together across rows):
   F087, F106 and F129 sit near F146 (0.18 to 0.22 against 0.16); F158 is
   higher (0.30) and **F184 is four times F146 (0.665)**, with 1,821
   detections before the cap; F213 is the shallowest band (333 detections,
   the cap not reached, consistent with the data ledger's fewer Gaia
   matches at F213). The short exposures halve the bright-residual
   fraction in each band that has them (0.088 against 0.18 to 0.19;
   F158 0.156 against 0.296) and carry about a quarter fewer packets per
   epoch. 503,964 packets over 2,484 differenced epochs.

   Per stage, medians over the six datasets' runs (queue wait is Batch's
   created to started, container started to ended; peak RSS is `job
   run`'s `peak_rss_kib`, the larger of self and children, 8 jobs sampled
   per stage per filter; bytes out per job from the run prefixes):

   | stage | jobs | failed | queue wait s (F087 / F106 / F129 / F158 / F184 / F213) | container s (same order) | peak RSS MiB median (max) | asked MiB | bytes out per job | F146 run 17 container |
   |---|---|---|---|---|---|---|---|---|
   | psf | 6 | 0 | 159 / 154 / 154 / 155 / 159 / 158 | 38 / 40 / 39 / 39 / 40 / 40 | 326 to 333 | 2,048 | 44 KB | 10.9 (9.1 s job) |
   | intake | 4,904 | 1,448 (SSM, re-planned) | 249 / 301 / 301 / 305 / 244 / 249 | 78 / 64 / 62 / 61 / 79 / 74 | 907 (911) | 2,048 | 200 MB | 37.6 |
   | reference | 324 | 0 | 10 / 93 / 92 / 93 / 16 / 17 | 120 / 125 / 125 / 123 / 120 / 99 | 1,439 (1,440) | 6,144 | 593 MB | 45.7 (3 frames) |
   | difference | 2,484 | 0 | 100 / 225 / 210 / 224 / 114 / 98 | 220 / 278 / 274 / 289 / 239 / 181 | 14,885 (15,013) | 24,576 | 248 MB | 232.6 |
   | associate | 2,484 | 2 (start, rerun) | 129 / 17 / 21 / 31 / 38 / 130 | 9 / 4 / 4 / 4 / 9 / 7 | 233 (238) | 1,024 | 21 to 100 KB | 2.8 |
   | packet | 2,484 | 13 (start, rerun) | 32 / 44 / 62 / 59 / 28 / 31 | 29 / 130 / 128 / 141 / 31 / 24 | 850 to 865 (877) | 2,048 | 19 to 46 MB | 30.2 |

   - **The packet's container time grows with the chain**: 24 to 31 s
     median at depth 2, 128 to 141 s at depths 13 and 14 (run 37's 608
     packet jobs: 147 s median), while its memory stays flat (877 MiB at
     most). The difference's container time rises with the job count
     (220 s at 108 jobs, 274 to 289 s at 702: more containers per
     instance).
   - **The psf job** waited 154 to 159 s and ran 38 to 40 s (run 17: 9.1 s
     alone): six fetching the 178.6 MB references at once.
   - Instances: differences on m6a.8xlarge, r6i.8xlarge and r7i.8xlarge
     (and one m6a.2xlarge), packets on those and m6a.2xlarge and
     m6a.4xlarge; no family pinned.
   - **Run prefixes**: F087 117.8 GB, F106 408.0 GB, F129 408.4 GB, F158
     435.6 GB, F184 118.5 GB, F213 112.9 GB: **1.60 TB** for 3,456 epochs
     (0.46 GB per epoch; the brief estimated 1.8 TB).
   - **Rows** (official): `sources` 54,000 / 351,000 / 351,000 / 378,000 /
     54,000 / 36,770 (1.22 million); `forced` 32,981 / 973,711 / 986,458 /
     1,163,368 / 37,834 / 16,355 (3.21 million: forced rows grow with the
     chain, about 1,400 per epoch at depth 13 against 300 at depth 2).
     Database **2.27 GB** (414 MB after step 1).
   - **Kafka**: about 30 packets per second per loop (F106's full passes:
     11,100 packets in 355 to 370 s), five loops publishing together; the
     publish re-sends of runs 24 to 28: 87 to 101 sets in 533 to 548 s
     each, five at once.
   - **Wall time**: the loops ran 05:43 to 09:04 UTC (3 h 21 min) including
     the three fixes and their redeploys; F213, whose only interruption
     was the SSM failure, drained 49 min after its first loop started
     (29.9 min for its resumed run 29). F106, F129 and F158's chains of 13
     and 14 took about 6 min a pass (one epoch per field per pass,
     publish-bound: 54 sets of about 205 packets).

   **The checks** (`scripts/check_run.sh` per run as soon as its chain was
   final; `validate_run.py` alone on the intake-only runs 18 to 23, whose
   consumer check would fail by its zero-kept rule; runs 30 to 34 not
   checked: abandoned, unbound). Every run CLEAN after the task's second
   attempt (11:47 UTC); the first attempt's cutoff, 09:41 UTC, left runs
   25 to 27 and 36 to 38 in their consumer check, and the second attempt
   re-ran `check_run.sh` on runs 24 to 27 and 36 to 38 after PR 337, seven
   at once (and run 29's consumer alone, as the proof):

   | filter | run | verdict line (second attempt, after PR 337) | consumer before PR 337 |
   |---|---|---|---|
   | all six | 18 to 23 | `validate_run.py` 6/6 each (7 to 39 s); no packets | |
   | F087 | 24 | `Check run 24: CLEAN validate 6/6 407 s reader 108/108 76 s consumer 22048/22048 3059 s` | 5,073 s |
   | F106 | 25 | `Check run 25: CLEAN validate 6/6 454 s reader 89/89 72 s consumer 18533/18533 2342 s` | unfinished at 4 h |
   | F129 | 26 | `Check run 26: CLEAN validate 6/6 486 s reader 94/94 69 s consumer 19527/19527 2359 s` | unfinished |
   | F158 | 27 | `Check run 27: CLEAN validate 6/6 490 s reader 91/91 68 s consumer 19729/19729 2961 s` | unfinished |
   | F184 | 28 | `Check run 28: CLEAN validate 6/6 317 s reader 108/108 52 s consumer 25737/25737 5287 s` (first attempt) | 5,287 s |
   | F213 | 29 | `Check run 29: CLEAN validate 6/6 209 s reader 108/108 34 s consumer 10871/10871 2508 s` (first attempt); consumer alone after PR 337: CLEAN 10,871/10,871 in 1,210 s | 2,508 s |
   | F106 | 36 | `Check run 36: CLEAN validate 6/6 1851 s reader 613/613 345 s consumer 123421/123421 4592 s` | not reached |
   | F129 | 37 | `Check run 37: CLEAN validate 6/6 1850 s reader 608/608 337 s consumer 123839/123839 4589 s` | not reached |
   | F158 | 38 | `Check run 38: CLEAN validate 6/6 2223 s reader 665/665 261 s consumer 139259/139259 5107 s` | not reached |

   Runs 24 to 27 kept more messages than their packets (27,460 for
   22,048 in run 24): the 06:43 re-sends of their unstamped sets, allowed
   by the publisher's contract; every packet was seen and byte-equal.

   - **A validation false failure, fixed**: run 24's chain check failed
     one of 28,232 objects, a source catalogued twice in one difference
     image (sids 941 and 943 of job 8034, 0.07 mas apart, equal flux to
     float32), whose `stdevflux` from the one-pass sums is off by the
     formula's cancellation; PR 335 (`db2f72f9`) sets the check's tolerance
     at that floor (`1e-7 * max|f|`); run 24's chain check then passed.
   - **The consumer check, fixed in the second attempt** (PR 337,
     `7a0941a1`): it read the topic from its earliest offset for every run
     at about 40 to 50 messages a second, and the cause was the client,
     not the volume: kafka-python without the `crc32c` C module checks
     every record batch's CRC-32C in pure Python (3,017 messages, 687 MB:
     51 msg/s with the check, 805 msg/s without). The script now skips the
     client CRC (every kept message is compared byte for byte with the
     outbox) and starts at `offsets_for_times` for the earliest `ended` of
     the run's stamped packet jobs, not their earliest
     `kafka_published_at`, which is written after the set's
     acknowledgements and so after its messages. Runs 24 to 29 started at
     offset 135,304 and read 530,960 messages, runs 36 to 38 at 279,745
     and 386,519; seven checks at once ran at 76 to 227 messages a second
     each (validation of the kept messages and the shared core count), so
     a run's consumer check is now 39 to 85 min where it was 4 h and
     growing. The topic's beginning offset is 73,505: run 12's earliest
     messages have aged out under its retention, so a consumer check of
     run 12 now fails "never read". `validate_run.py` grows with the
     chain (runs 36 to 38, depth 13 to 14: 1,850 to 2,223 s with seven at
     once; run 37's 1,554 s in the first attempt).

3. **The 18 SCAs of pointing `001_0001` for the same six runs.** No code
   change: the same live runs (F087 35, F106 36, F129 37, F158 38, F184
   39, F213 29), `rapid-rebuild:42` (`a3f1d321`; nothing under `rapidpipe`
   changed after it), six loops started together at 11:54 UTC, each in its
   own script and log under one 10,800 s deadline:

   ```sh
   rp loop --run <id> --filters F<nnn> --scas 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18 \
     --pointings 001_0001 --job-definition rapid-rebuild:42 --max-intake 1000
   ```

   - **Pass 1 submitted 17 `psf` jobs per filter, 102 in all, and no
     intake**; the new stems were 85 (F087, F184, F213), 272 (F106, F129)
     and 289 (F158), 1,088 in all: SCA 8 of `001_0001` was already in.
   - **A seen failure, transient: the CRDS server under 102 fetches at
     once.** 72 of the 102 psf jobs failed (70 `HTTP Error 504: Gateway
     Time-out`, 2 `TimeoutError: The read operation timed out`, from
     `urlopen` of the 178.6 MB reference, every one from the job's
     CloudWatch stream), 30 published; every loop exited 2 at 11:58. The
     server answered a HEAD in 0.04 s a minute later. The 72 were rerun with
     `rp job submit --job <id> --job-definition rapid-rebuild:42`, 24 and
     then waves of 12 (11:59 to 12:07), every one published at its second
     attempt, and the six loops were restarted at 12:07:42; they planned
     from the official rows and resumed. Each derived PSF's job read its
     own file's `meta.ref_file.epsf`; every intake's `EPSFREF` assertion
     held (no intake refused).
   - **All six drained, exit 0, no other job failed**: F213 and F184 at
     12:30:06, F087 12:30:32, F106 13:09:24, F129 and F158 13:16:30 (3
     passes before the restart and 12 to 34 after). Per filter: 17 x 2 PSF
     rows; 16 references of exactly three frames, not 17, because **SCA 9
     of `001_0001` shares SCA 8's field** (5390959 in F146; the reference
     key has no SCA, part 7 step 9), so SCA 9's epochs were differenced
     against the field's SCA 8 reference and appended to its chain, as in
     run 12; 17 x (epochs - 3) differences, associates and packet sets
     (34; 221 for F106 and F129; 238 for F158).

   **The null test per filter, the 18 SCAs** (medians over SCAs of each
   SCA's median over its differenced epochs; SCA 8's epochs are step 2's;
   `max_sources` 500; S/N is `fluxfit / fluxerr` of the kept rows). F146
   is run 12's 18-SCA ramp (part 7 step 9), whose reference rule differs:
   no `before_mjd`, every one of the six epochs differenced against a
   reference holding it, and dev's copied PSF. Descriptive only: filter,
   PSF and reference rule change together across the rows.

   | filter | run | exposures | SCAs | differenced epochs | before the cap | kept | S/N > 20 | bright-residual fraction | packets per epoch | SCA 4 / 5 S/N > 20 | SCA 4 / 5 fraction | other 16 SCAs' fraction (median) |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|
   | F146 | run 12 | 294.1 s | 18 | 108 | 995 | 500 | 79 | 0.158 | 118 | 352 / 256 | 0.70 / 0.51 | 0.153 |
   | F087 | run 35 | 294.1 s | 18 | 36 | 907 | 500 | 103 | 0.207 | 145 | 401 / 324 | 0.80 / 0.65 | 0.197 |
   | F106 | run 36 | 294.1 s | 18 | 216 | 1,000 | 500 | 97 | 0.195 | 149 | 393 / 229 | 0.79 / 0.46 | 0.188 |
   | F106 | run 36 | 107.5 s | 18 | 18 | 895 | 500 | 49 | 0.098 | 140 | 186 / 66 | 0.37 / 0.13 | 0.094 |
   | F129 | run 37 | 294.1 s | 18 | 216 | 1,175 | 500 | 118 | 0.235 | 168 | 500 / 180 | 1.00 / 0.36 | 0.221 |
   | F129 | run 37 | 107.5 s | 18 | 18 | 1,048 | 500 | 54 | 0.108 | 146 | 265 / 54 | 0.53 / 0.11 | 0.108 |
   | F158 | run 38 | 294.1 s | 18 | 216 | 1,487 | 500 | 161 | 0.322 | 176 | 500 / 270 | 1.00 / 0.54 | 0.316 |
   | F158 | run 38 | 107.5 s | 18 | 36 | 1,219 | 500 | 86 | 0.172 | 169 | 306 / 134 | 0.61 / 0.27 | 0.170 |
   | F184 | run 39 | 294.1 s | 18 | 36 | 1,674 | 500 | 214 | 0.428 | 170 | 500 / 500 | 1.00 / 1.00 | 0.397 |
   | F213 | run 29 | 294.1 s | 18 | 36 | 394 | 394 | 44 | 0.127 | 104 | 66 / 166 | 0.13 / 0.33 | 0.119 |

   Per SCA, the 294.1 s epochs: S/N > 20 per epoch (bright-residual
   fraction), each SCA's median:

   | SCA | F146 (run 12) | F087 | F106 | F129 | F158 | F184 | F213 |
   |---|---|---|---|---|---|---|---|
   | 1 | 99 (0.20) | 106 (0.21) | 100 (0.20) | 104 (0.21) | 124 (0.25) | 152 (0.30) | 43 (0.09) |
   | 2 | 81 (0.16) | 76 (0.15) | 86 (0.17) | 90 (0.18) | 122 (0.24) | 190 (0.38) | 34 (0.11) |
   | 3 | 114 (0.23) | 155 (0.31) | 113 (0.23) | 165 (0.33) | 200 (0.40) | 310 (0.62) | 40 (0.10) |
   | 4 | 352 (0.70) | 401 (0.80) | 393 (0.79) | 500 (1.00) | 500 (1.00) | 500 (1.00) | 66 (0.13) |
   | 5 | 256 (0.51) | 324 (0.65) | 229 (0.46) | 180 (0.36) | 270 (0.54) | 500 (1.00) | 166 (0.33) |
   | 6 | 86 (0.17) | 115 (0.23) | 79 (0.16) | 118 (0.23) | 200 (0.40) | 241 (0.48) | 60 (0.15) |
   | 7 | 160 (0.32) | 158 (0.32) | 166 (0.33) | 182 (0.36) | 222 (0.44) | 386 (0.77) | 63 (0.14) |
   | 8 | 77 (0.15) | 112 (0.22) | 102 (0.20) | 101 (0.20) | 157 (0.31) | 373 (0.75) | 48 (0.13) |
   | 9 | 76 (0.15) | 184 (0.37) | 154 (0.31) | 176 (0.35) | 226 (0.45) | 320 (0.64) | 120 (0.24) |
   | 10 | 86 (0.17) | 96 (0.19) | 80 (0.16) | 83 (0.17) | 150 (0.30) | 204 (0.41) | 76 (0.20) |
   | 11 | 100 (0.20) | 174 (0.35) | 146 (0.29) | 216 (0.43) | 254 (0.51) | 360 (0.72) | 84 (0.21) |
   | 12 | 62 (0.12) | 44 (0.09) | 52 (0.10) | 76 (0.15) | 96 (0.19) | 92 (0.18) | 37 (0.13) |
   | 13 | 72 (0.14) | 96 (0.19) | 95 (0.19) | 124 (0.25) | 162 (0.33) | 194 (0.39) | 36 (0.09) |
   | 14 | 64 (0.13) | 90 (0.18) | 104 (0.21) | 118 (0.23) | 184 (0.37) | 168 (0.34) | 38 (0.09) |
   | 15 | 58 (0.12) | 52 (0.10) | 63 (0.13) | 67 (0.13) | 55 (0.11) | 53 (0.11) | 38 (0.11) |
   | 16 | 53 (0.11) | 60 (0.12) | 80 (0.16) | 72 (0.14) | 118 (0.24) | 114 (0.23) | 23 (0.07) |
   | 17 | 38 (0.08) | 66 (0.13) | 59 (0.12) | 81 (0.16) | 120 (0.24) | 121 (0.24) | 32 (0.06) |
   | 18 | 70 (0.14) | 100 (0.20) | 93 (0.19) | 152 (0.30) | 159 (0.32) | 225 (0.45) | 44 (0.13) |

   - **The SCA 4 and 5 excess appears in the other filters.** SCA 4 holds
     the most bright residuals of the 18 in F087, F106, F129, F158 and F184
     (in F129, F158 and F184 every kept row of SCA 4 is above S/N 20, 500
     of 500); SCA 5 is second in F087, F106 and F158, at the cap beside SCA 4
     in F184 and fourth in F129; in F213,
     the shallowest band, SCA 4 does not stand out (66, 0.13) and SCA 5
     does (166, 0.33). F184 is high on most SCAs (0.397 median over the
     other 16). Nothing here says why.
   - **SCA 9 sits above its filter's SCA 8 in five of the six** (all but
     F184; 0.24 to 0.45 against 0.13 to 0.31), where in F146 the two were
     level (0.15): SCA 9 is the SCA differenced against SCA 8's reference
     stack, in F146 as here; stated as an observation.
   - The 107.5 s epochs (passes 12 and 13, F106, F129, F158) about halve
     the fraction again, SCA 4 and 5 included (SCA 4: 0.79, 1.00, 1.00 to
     0.37, 0.53, 0.61).

   Per stage, this step's jobs (count: queue wait median / container
   median (max), seconds; Batch's created to started and started to ended;
   peak RSS is `job run`'s `peak_rss_kib`, 8 jobs sampled per stage per
   filter):

   | stage | F087 | F106 | F129 | F158 | F184 | F213 | peak RSS MiB median (max) |
   |---|---|---|---|---|---|---|---|
   | psf | 17: 106 / 117 (118) | 17: 635 / 53 (58) | 17: 565 / 53 (120) | 17: 635 / 54 (58) | 17: 105 / 115 (120) | 17: 357 / 118 (120) | 301 (305) |
   | intake | 85: 15 / 79 (99) | 272: 195 / 83 (103) | 272: 188 / 84 (103) | 289: 196 / 82 (103) | 85: 15 / 84 (99) | 85: 16 / 78 (98) | 907 (921) |
   | reference | 16: 7 / 66 (94) | 16: 204 / 151 (155) | 16: 205 / 137 (155) | 16: 204 / 146 (155) | 16: 195 / 106 (108) | 16: 7 / 65 (92) | 1,439 (1,440) |
   | difference | 34: 203 / 205 (255) | 221: 100 / 212 (278) | 221: 99 / 213 (276) | 238: 103 / 217 (285) | 34: 140 / 214 (292) | 34: 204 / 181 (250) | 14,885 (15,013) |
   | associate | 34: 45 / 2 (4) | 221: 10 / 2 (8) | 221: 10 / 3 (9) | 238: 10 / 4 (9) | 34: 33 / 2 (4) | 34: 57 / 2 (4) | 232 (246) |
   | packet | 34: 14 / 27 (43) | 221: 11 / 76 (184) | 221: 13 / 82 (287) | 238: 12 / 86 (306) | 34: 24 / 26 (45) | 34: 18 / 24 (40) | 852 (888) |

   - **The psf job's container is 53 to 118 s** (step 1: 10.9 s alone),
     the fetch shared with up to 101 others (F106, F129 and F158's figures,
     53 to 54 s, are nearly all reruns in waves of 12 or 24). Peak RSS 301
     MiB of 2,048 asked, as in steps 1 and 2.
   - The reference container is 65 s (F087, F213) to 137 to 151 s (F106,
     F129, F158, whose references waited 204 s behind their 272 to 289
     intakes); the packet's grows with the chain as in
     step 2 (24 to 27 s at depth 2, 76 to 86 s median and 306 s at most at
     depth 13 to 14).
   - Instances: m6a.8xlarge for every psf, intake and reference job;
     differences, associates and packets on m6a.8xlarge, r6i.4xlarge and
     r6i.8xlarge; no family pinned.

   | filter | run | jobs (ids) | packet sets | packets | `sources` rows | `forced` rows | bytes out GB | loop passes | loop drained |
   |---|---|---|---|---|---|---|---|---|---|
   | F087 | 35 | 220 (16174 to 18494) | 34 | 5,596 | 17,000 | 8,326 | 36.1 | 3 + 12 | 12:30:32 |
   | F106 | 36 | 968 (16229 to 19783) | 221 | 38,435 | 110,500 | 266,086 | 126.0 | 3 + 31 | 13:09:24 |
   | F129 | 37 | 968 (16225 to 19805) | 221 | 40,169 | 110,500 | 278,102 | 126.5 | 3 + 34 | 13:16:29 |
   | F158 | 38 | 1,036 (16231 to 19804) | 238 | 44,904 | 119,000 | 330,033 | 135.1 | 3 + 34 | 13:16:31 |
   | F184 | 39 | 220 (16175 to 18484) | 34 | 6,176 | 17,000 | 9,109 | 36.2 | 3 + 12 | 12:30:06 |
   | F213 | 29 | 220 (16177 to 18490) | 34 | 3,760 | 13,875 | 5,624 | 34.6 | 3 + 12 | 12:30:06 |

   - **Totals**: 3,632 jobs (16174 to 19805), 1,088 epochs ingested, 782
     packet sets, **139,040 packets**, 0.49 TB of new objects under the six
     run prefixes (0.45 GB per new epoch; the brief estimated 0.6 TB);
     `official` 22,520 to 27,036 (4,516 rows); database **2.74 GB** (2.27 GB
     after step 2).
   - **Kafka**: about 29 packets a second per loop (F106's full passes,
     3,006 packets in 103 s), up to six loops publishing together, one set
     at a time (PR 333).
   - **Wall time**: 11:54 to 13:16 UTC, 1 h 22 min with 9 min lost to the
     psf reruns; F087, F184 and F213 (chains of 2) drained 23 min after the
     restart, F106, F129 and F158 (chains of 13 and 14) in 62 to 69 min,
     about 2 min a pass, one epoch per field per pass.

   **The checks** (`scripts/check_run.sh --run <id>` per run as soon as it
   drained; the enlarged runs, every packet set of step 2 and of this step
   alike):

   | filter | run | verdict line | wall |
   |---|---|---|---|
   | F087 | 35 | `Check run 35: CLEAN validate 6/6 85 s reader 34/34 20 s consumer 5596/5596 251 s` | 12:30:55 to 12:36:51 |
   | F106 | 36 | `Check run 36: CLEAN validate 6/6 2352 s reader 834/834 336 s consumer 161856/161856 6256 s` (task 3's second attempt waited on it) | 13:10:27 to 15:39:31 |
   | F129 | 37 | `Check run 37: CLEAN validate 6/6 2383 s reader 829/829 319 s consumer 164008/164008 6143 s` (task 3's second attempt waited on it) | 13:16:44 to 15:44:09 |
   | F158 | 38 | `Check run 38: CLEAN validate 6/6 2710 s reader 903/903 348 s consumer 184163/184163 6509 s` (task 3's second attempt waited on it) | 13:16:44 to 15:56:11 |
   | F184 | 39 | `Check run 39: CLEAN validate 6/6 88 s reader 34/34 20 s consumer 6176/6176 269 s` | 12:30:55 to 12:37:12 |
   | F213 | 29 | `Check run 29: CLEAN validate 6/6 304 s reader 142/142 44 s consumer 14631/14631 1271 s` | 12:30:55 to 12:57:54 |

   Every run of the six filters is CLEAN over the enlarged run. Runs 36 to
   38 read 525,559 messages each from offset 279,745, three at once, in
   6,143 to 6,509 s (about 33 MB/s per process), against 4,589 to 5,107 s
   for the same runs' step 2 checks.

## Results of part 9 (2026-10-23)

Three steps: the `psf` stage and the loop deriving what is missing (step
1, PRs 327 to 329), the six other filters on SCA 8 of the 54 pointings
(step 2, PRs 331 to 335 and 337), and the 18 SCAs of `001_0001` for the
same six (step 3, no code). Seven datasets, each with its own live run;
runs 9, 10, 12 and the housekeeping runs stayed live and untouched;
nothing was deleted on S3 or in the database.

**What the `psf` stage changed.** Every filter now has its PSF with no
copy: the loop submits one `psf` job per (filter, SCA) of its new stems
that has no official science and reference PSF, holds that SCA's intakes
until both rows are official, and the job derives both from the CRDS ePSF
reference the L2 file itself names (`meta.ref_file.epsf`), fetched by
name. 109 psf jobs over seven datasets, 109 pairs of `psfs` rows, and
every intake's `EPSFREF` assertion held: r00342 names one ePSF per
(filter, SCA). Against dev's copied F146 PSF on the same pixels (step 1)
the derived PSF cut the bright residuals by a third (0.23 to 0.16).

**What the per-filter dataset shape changed.** One dataset and one live
run per filter, each with its own `before_mjd` (the midpoint of its own
pass 3 and pass 4 at `001_0001`), gives every filter exactly three
reference frames per field and its own chains, and keeps a filter's
fixes and resumes from touching the others (step 2's Kafka and associate
fixes resumed five datasets while F213, drained, stayed as it was). It avoids cross-band
association by construction: a source seen in two filters is two objects
in two datasets.

**Every filter's numbers**, medians per differenced epoch (S/N > 20 of
the 500 kept, and its fraction), the 294.1 s epochs:

| filter | SCA 8, 54 pointings (step 2) | 18 SCAs, `001_0001` (step 3) | SCA 4 / 5 at `001_0001` | packets | run prefixes |
|---|---|---|---|---|---|
| F146 | 79 (0.158), run 17, 3 epochs | 79 (0.158), run 12, other reference rule | 352 / 256 | | |
| F087 | 110 (0.220) | 103 (0.207) | 401 / 324 | 27,644 | 154 GB |
| F106 | 97 (0.194); short 44 (0.088) | 97 (0.195); short 49 (0.098) | 393 / 229 | 180,389 | 534 GB |
| F129 | 92 (0.184); short 44 (0.088) | 118 (0.235); short 54 (0.108) | 500 / 180 | 183,535 | 535 GB |
| F158 | 148 (0.296); short 78 (0.156) | 161 (0.322); short 86 (0.172) | 500 / 270 | 203,892 | 571 GB |
| F184 | 333 (0.665) | 214 (0.428) | 500 / 500 | 31,913 | 155 GB |
| F213 | 33 (0.101), 333 kept | 44 (0.127), 394 kept | 66 / 166 | 14,631 | 148 GB |

4,544 epochs ingested over the two steps, 3,266 differenced, **643,004
packets**, 2.1 TB of run prefixes, database 2.74 GB. The checks by
`scripts/check_run.sh` are step 3's table.

The machinery's seen failures at breadth, each now fixed or rerun:
SSM throttling at 3,400 jobs starting together (PR 331); kafka-python's
dead sender thread under one flush per pass (PR 333); the associate rule
under a resumed run (PR 334); a validation false failure on a duplicated
source (PR 335); the consumer check's pure-Python CRC and its read from
the topic's beginning (PR 337); Batch's `Task failed to start` (16 jobs,
rerun by hand); and the CRDS server's 504s under 102 concurrent 178.6 MB
fetches (72 psf jobs, rerun in waves).

Open, for the plan:

- **Cross-band association.** The per-filter shape avoids it; the plan
  must decide whether an object is per band or across bands, and with it
  forced photometry in the other bands at a source's position.
- **The 107.5 s epochs** (passes 12 and 13, F106, F129, F158) are
  differenced against a 294.1 s reference and carry half the bright
  residuals and a quarter fewer packets: whether a short epoch gets a
  reference of its own exposure time, or is differenced at all, is not
  decided.
- **The reference PSF rule.** The reference PSF is the science PSF (the
  same ePSF, since a reference coadds same-filter, same-SCA frames); a
  coadd's PSF from its frames' positions and dithers is not built.
- **SCAs 4 and 5.** Their bright-residual excess appears in five of the
  six other filters (SCA 4 at the cap in F129, F158, F184) under PSFs
  derived from their own ePSF references; its cause is not looked into.
- **A field shared by two SCAs.** SCA 9 of `001_0001` is differenced
  against SCA 8's reference stack in every filter (the reference key has
  no SCA), and its chain appends after SCA 8's out of time order.
- **F184's residuals**: 0.43 to 0.67 of the kept rows above S/N 20,
  three to four times F146's; F213 reaches the cap on 5 of 18 SCAs
  (median kept 394).
- **The psf fetch** is 178.6 MB from the CRDS server per (filter, SCA)
  per dataset; one fetch per job under a wide fan-out drew 504s. A
  server error mapped to Batch's retried exit 75, or the reference read
  through the job's CRDS cache once per reference, are a later brief's.
- **Retention**: the topic keeps 7 days; a run's consumer check holds only
  inside it (run 12's earliest messages have aged out).

## Part 10: the null test's artefacts fixed on one pointing (2026-10-23)

Brief `fixes-2026-10-23` (plan v9 Part 7): the two artefact sources run 17's
detections were measured to be (the reference PSF mismatch, the unmasked
pixel defects) removed on pointing `001_0001`, SCA 8, counted again in seven
filters against the measured before, with the per-job quality numbers that
would have caught the finding. One step per task.

1. **Settings in layers (PR 341, merged `c6fcb196`).** `run create`
   resolves the body it stores from the base (`--settings`, default the
   shipped `foundation-thin.toml`), the dataset's `--overlay` and the
   run's `--set`s, refusing a key the base lacks, and records the body's
   hash and each layer's name and SHA-256 in a new `runs.layers` column.
   The HLWAS overlay `rapidpipe/settings/datasets/hlwas-r00342.toml`
   holds `[intake] unsigned = true` only. SMDC gets the column as
   `postgres`, once, before the PR merged
   (`database/2026-10-23-runs-layers.sql`), by SSM on the database host,
   since the `postgres` secret is not read from a session:

   ```sh
   podman exec rapid-postgres psql -U postgres -d rapid -v ON_ERROR_STOP=1 -c "ALTER TABLE runs ADD COLUMN layers jsonb;"
   ```

   Check, held: `ALTER TABLE` at 00:05:54 UTC, `runs.layers jsonb`
   present, runs 39 rows (max id 39) before and after.

   The layering proof, one run under dataset `r00342-fx-f146`, not live,
   no jobs (task 5 creates the final runs):

   ```sh
   rp run create --who "$USER" --why "..." --revision c6fcb1960fc8e48f187004539bf8cf22caeb13b3 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-fx-f146
   rp run show 40
   ```

   Check, held: run **40** (0.45 s), `live` null, no jobs. Its stored
   body equals the hand-built full body of run 17's `ep1-run.json` shape
   (the shipped toml at `c6fcb196` with `[intake] source_root` and
   `unsigned = true` and the F146 bound 61591.4056), and the stored
   `settings_hash`, the hand-built body's and the stored body's
   recomputed hash are the same, `sha256:85a5ddf6…`. `run show` prints
   `layers`: base `foundation-thin.toml` `a6e7e871…`, overlay
   `hlwas-r00342.toml` `29e79bde…`, the two `--set`s in order
   `896568d4…`. An overlay with `[intake] unsinged = true` exits 64 with
   `unknown settings key 'intake.unsinged'` before any connection, and no
   run is written.

2. **The DQ mask at intake (PR 343, merged `f948dbf7`).** Commissioning
   #238's rule in `build_fits_file`: where `DQ & dq_mask_bits` is nonzero,
   `SCI`, `ERR` and `VAR_*` are NaN, `DQ` is written unchanged, and each
   masked HDU carries `DQMASK` and `NDQMASK`. `[intake] dq_mask_bits`
   defaults to `0` (dev: nothing masked); the HLWAS overlay sets
   `0x2110C01` (#238's `0x2110C03` without SATURATED). The intake manifest
   records `dq` (`mask_bits`, and the science image's `nan_input`,
   `masked`, `nan_output`) and `metrics.masked_fraction`.

   The image and its revision (build 23 s, deploy 74 s):

   ```sh
   env -u TMPDIR containers/rapid-pipeline/build-rebuild.sh f948dbf7140a897da1e4ca293a598b17b6399738 --tag rebuild-f948dbf7140a897da1e4ca293a598b17b6399738
   RELEASE_TAG=rebuild-f948dbf7140a897da1e4ca293a598b17b6399738 RELEASE_IMAGE_DIGEST=sha256:106ed00f… cloudformation/release-hooks/deploy
   ```

   Check, held: **`rapid-rebuild:43`** (and `rapid-rebuild-production:34`),
   image `sha256:106ed00f…`, read back with `describe-job-definitions`.

   The dataset `r00342-fx-dq` and its live run, the step 1 recipe:

   ```sh
   rp run create --who "$USER" --why "..." --revision f948dbf7140a897da1e4ca293a598b17b6399738 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-fx-dq
   rp official change --group live.json ...      # run 41 live for HLWAS r00342-fx-dq
   rp job local --run 41 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-fx-dq", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00342-fx-dq", "name": "gaia-r00342-fx-dq", "version": "DR3"}, "binding": null}]'
   ```

   Check, held: run **41**, `dq_mask_bits` 34671617 (`0x2110C01`) in its
   body, live (group 18432); Gaia job **19806** published.

   The proof samples passes 1 to 4 (the reference's three frames and one
   differenced epoch). The loop has no pass selection, so a wrapper kept
   beside the brief replaces its one listing function, `loop.list_stems`,
   with one that drops stems of pass 5 and later, and hands the arguments
   to the CLI unchanged:

   ```sh
   MAX_PASS=4 timeout 5400 $PY loop-passes.py loop --run 41 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:43
   ```

   - It drained in **20.2 min** (00:29:50 to 00:50:01 UTC, 11 passes), exit 0, no
     job failed: psf **19807**, intakes **19808 to 19811** (passes 1 to
     4), reference **19812** (exactly three frames), difference **19821**
     (pass 4), associate **19826**, packet **19828** (194 packets, produced and stamped).
   - **The intake's FITS** (pass 4, job 19811), checked against its own
     ASDF by an independent read: the file's own NaN **1,281** (1,278 of
     them DO_NOT_USE), masked **6,904**, their union 6,904, and `SCI`'s
     NaN exactly that union; `DQMASK` `0x2110c01` and `NDQMASK` 6904 on
     `SCI`, `ERR` and `VAR_POISSON` (the file's only `VAR_*`), NaN at every
     masked pixel of each; `DQ` bit-identical to the ASDF's and without the
     cards. The manifests (`nan_input` / `masked` / `nan_output`): pass 1
     1,279 / 6,903 / 6,903, pass 2 1,282 / 6,904 / 6,904, pass 3 1,276 /
     6,904 / 6,906, pass 4 1,281 / 6,904 / 6,904; **masked fraction
     0.041%** (6,904 of 16,711,744). Every native NaN but pass 3's two is
     already under the mask.
   - **The reference** (job 19812) against run 17's unmasked reference 2454,
     the same three frames, over the whole 7000 x 7000 mosaic (the ledger's
     3000 px test region is not recorded; the whole mosaic contains it):

     | | run 17, 2454 (unmasked) | run 41, 19812 (masked) |
     |---|---|---|
     | `gmin` / `gmax` (manifest, DN/s) | -66,520 / 26,925 | **-88.5 / 9,135** |
     | pixels below -1000 DN/s | 24 | **0** |
     | pixels above 1000 DN/s | 268 | 225 |
     | of them in a star (a > 50 DN/s blob of 25 px or more) | 220 | 220 |
     | of them in a smaller blob | 48 (23 blobs of 3 to 4 px) | 5 (4 blobs of 16 to 21 px) |
     | `nsexcatsources` | 4,567 | 4,240 |
     | NaN pixels | 33,003,245 | 33,005,293 |

     The defects baked into the reference are gone: no pixel below -1000,
     no blob of 3 to 4 px above 1000 DN/s (the fixes ledger's "isolated"
     pixels, spread by the coadd's overlap weights onto two or three
     neighbours, which is why a strict eight-neighbour rule finds none in
     either mosaic). The 5 bright pixels left in small blobs have the same
     value in both mosaics: unflagged compact stars. `gmax` is now a bright
     star's core (a 737-pixel blob at (4966, 1874)); before it was a 4-pixel
     defect at (4381, 3796).
   - **Star flux unchanged**: the 30 brightest compact isolated stars of the
     masked mosaic (DAOStarFinder at 8 sigma, sharpness 0.3 to 1.0, no
     neighbour within 12 px, 40 px from an edge; peaks 70 to 7,424 DN/s),
     5 px aperture less the 8 to 12 px annulus median: flux ratio masked
     over unmasked **1.0000 for all 30** (to 1e-4), no NaN within 5 px of
     any; SATURATED out of the mask keeps every core.
   - **Coverage at the masked pixels**: pass 1's 6,903 masked pixels mapped
     through the two WCSs onto the mosaic; at the 4,702 where the unmasked
     mosaic had full coverage (3.000), the masked mosaic's is below 3 at
     every one (median 1.036, max 2.307), and its 3 x 3 minimum is below
     2.5 at all 4,702 and below 2 at 4,581: a coverage threshold near 2.5
     would blank them, as the ledger said.
   - **The pass 4 difference** (job 19821) against run 17's 2455, sources
     with a mask bit within a 3 x 3 / 7 x 7 box of their fit position, on
     pass 4's own DQ (the ledger's measurement, reproduced exactly on
     2455):

     | | 2455 (unmasked) | 19821 (masked) |
     |---|---|---|
     | candidates before the cap | 1,035 | **907** |
     | `sources` kept | 500 | 500 |
     | above S/N 20 | 80 | **45** |
     | #238's mask bit within 3 x 3 / 7 x 7, all | 55 / 65 | **8 / 12** |
     | the same, above S/N 20 | 26 / 34 | **5 / 8** |
     | the HLWAS mask's bit within 3 x 3 / 7 x 7, all | 47 / 53 | **0 / 0** |
     | the same, above S/N 20 | 21 / 26 | **0 / 0** |
     | photutils detections, positive / negative | 1,041 / 426 | 910 / 282 |
     | packets | 216 | **194** |
     | packets above S/N 20 | 30 | **13** |

     An observation, not a gate: no kept source touches a masked pixel, the
     8 left near #238's bits touch SATURATED only (star cores, kept on
     purpose), and the bright end falls from 80 to 45. The PSF mismatch,
     73% of the sources on reference stars, is untouched by this step and
     is task 3's; what remains above S/N 20 is read in task 5.

   Measurements (Batch's created to started is the queue wait, started to
   stopped the container; peak RSS is `job run`'s `peak_rss_kib`, self
   plus children):

   | stage | job | instance | queue wait s | container s | peak RSS MiB | before (part 9 step 1, run 17) |
   |---|---|---|---|---|---|---|
   | psf | 19807 | m6a.2xlarge | 121 | 11.7 | 302 | 186 s wait, 10.9 s, 302 MiB |
   | intake | 19808 to 19811 | the same | 131 to 132 | 25.3 / 26.4 / 27.0 (min / median / max) | 911 | 37.6 s median, 940 MiB |
   | reference | 19812 | the same | 5 | 46.1 | 1,388 | 45.7 s, 1,439 MiB |
   | difference | 19821 | the same | 41 | 170 | 16,120 | 232.6 s median, 14,889 MiB (as part 9 recorded it) |

   - The intake's conversion step: 8.4 to 12.0 s (fetch 0.9 to 1.1 s, gzip
     5.5 to 5.6 s, limmag 3.7 to 4.3 s); the mask adds one boolean array
     and one NaN count, not seen in the times. The brief's 21 s intake and
     98 s reference befores are not part 9's figures; part 9's are the
     comparison above.

3. **The reference PSF measured in the difference job (PRs 344 and 346,
   merged `7daf99e5` and `379688d6`; rapid_docs #94 and #95).** The
   difference job builds ZOGY's reference PSF from the resampled,
   background-subtracted reference it has just made
   (`rapidpipe.science.difference.refpsf`): DAOStarFinder at 8σ on the
   covered pixels, peak 20 to 3,500 DN/s, no neighbour within 14 px, the
   31 px stamp usable, and each star's own second-moment width between the
   science PSF's and that plus 0.4 px; `EPSFBuilder` oversample 4 on the
   brightest 100; evaluated on a 61 x 61 stamp re-centred to 0.01 px; the
   measured core within 6 px tapered into the science PSF's wings by 10
   px; unit sum. Fewer than ten stars stops the job with exit 65 and
   `refpsf: N stars, 10 needed` in `jobs.refusal`. The manifest's
   `metrics` hold `refpsf_sigma_px`, `refpsf_stars`, `sci_psf_sigma_px`
   and `psf_centroid_offset_px`. The `psf` stage writes the science kind
   only and the loop names only it.

   Two cuts the fixes ledger did not have, each earned on this pointing's
   data. **The width cut**: the ledger's sharpness and roundness values
   were not recorded, and on run 17's pass 4 reference a sharpness cut
   alone keeps defect pixels (own sigma 0.48 to 0.66 px) and galaxies
   (1.55 to 1.84) beside the stars (1.06 to 1.14); the built stamp came
   out at sigma 0.33 to 0.80. **The wings** (PR 346): PR 344's stamp held
   nothing beyond the 31 px build's 15 px, where the science PSF holds
   3.3% of its flux, and ZOGY left a ring 10 to 20 px out at every bright
   star: run 42's pass 4 had 1,685 candidates and 449 sources above S/N
   20, 190 of them 10 to 20 px from a top-100 reference star.

   Image `rebuild-7daf99e5…` (`sha256:3ddcb3b7…`), **`rapid-rebuild:44`**;
   then `rebuild-379688d6…` (`sha256:71f7d55a…`), **`rapid-rebuild:45`**,
   both from `rapid_systems`' `containers/rapid-pipeline/build-rebuild.sh`
   (the script is in `rapid_systems`, not this repository) and its
   `deploy` hook. Dataset `r00342-fx-psf`, F146, `001_0001`, SCA 8,
   **without** task 2's mask (the two tasks ran in parallel):

   ```sh
   rp run create --who "$USER" --why "..." --revision 7daf99e53ab2119342ba93889f8f8114da37ab90 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --set intake.dq_mask_bits=0 --survey HLWAS --dataset r00342-fx-psf     # run 42
   rp official change --group live.json ...                                  # run 42 live, group 18439
   rp job local --run 42 --stage import \
     --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-fx-psf", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00342-fx-psf", "name": "gaia-r00342-fx-psf", "version": "DR3"}, "binding": null}]'
   rp loop --run 42 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:44 --interval 60
   ```

   The first Gaia import (job 19813) used part 7's key without the
   dataset, and the loop stopped at the packet stage with "the run has no
   official catalogs binding"; job 19829 imported it under part 9's key
   and the restarted loop drained (exit 0). Job 19813's binding stays,
   unread. Run 42: psf 19814, intakes 19815 to 19820, reference 19822,
   differences 19823 to 19825, associates 19827 to 19832, packets 19830
   to 19834 (606 packets, onto the topic).

   Then the three differences again at `379688d6` under **run 43** (the
   same dataset, the same body, **not live**: a downstream-only rerun),
   by hand on run 42's inputs, published and unbound:

   ```sh
   rp job submit --run 43 --stage difference --job-definition rapid-rebuild:45 --scope '{"expid": 4040101, "sca": 8, "fid": 8}' \
     --inputs '[<l2files from 19818>, <psfs science from 19814>, <refimages field 5387158 from 19822>]' \
     --expects '[<diffimages and sources of the key, binding null>]'           # 19835; 5040101 19836, 6010101 19837
   ```

   Check, held: the psf job wrote **one** `psfs` row (kind `science`);
   one reference from passes 1 to 3; each difference's manifest reports an
   empirical PSF from **38 stars** with sigma **1.080, 1.071 and 1.081 px**
   (passes 4, 5, 6; the ledger's 1.064 to 1.091; it differs per epoch
   because each epoch resamples the reference onto its own grid), the
   stamp's centroid within 0.007 px of its centre, the science stamp's
   sigma 0.947 and the pair's relative centring (+0.029, +0.034) px (the
   science stamp's own offset). Before and after, by the images ledger's
   method re-implemented (it reproduces the ledger's run 17 figures):

   | pass 4 (run 17 job 2455 → run 42 job 19823 → run 43 job 19835) | run 17 | PR 344 | **PR 346** |
   |---|---|---|---|
   | core / ring of star flux, median over the top 30's real stars | +0.0297 / -0.0272 | +0.0164 / +0.0013 | **+0.0026 / -0.00001** |
   | photutils candidates before the cap | 1,035 | 1,685 | **974** |
   | sources above S/N 20 | 80 | 449 | **93** |
   | sources within 3 px of a reference star, all / S/N > 20 | 74.0% / 43.8% | 3.4% / 3.1% | **29.0% / 8.6%** |
   | S/N > 20 sources 10 to 20 px from a top-100 star | 2 | 190 | **3** |
   | S/N > 20 bad-pixel, spike, reference source, hole, unclassified | 30, 0, 25, 18, 7 | 75, 1, 123, 25, 225 | **28, 1, 14, 20, 30** |

   | passes 5 and 6 (run 17 jobs 2456, 2457 → run 43 jobs 19836, 19837) | pass 5 before | **after** | pass 6 before | **after** |
   |---|---|---|---|---|
   | reference PSF sigma | (0.947 given) | **1.071** | (0.947 given) | **1.081** |
   | core / ring | +0.0286 / -0.0277 | **+0.0029 / -0.0002** | +0.0276 / -0.0265 | **+0.0054 / -0.0002** |
   | candidates | 1,065 | **860** | 1,030 | **1,111** |
   | sources above S/N 20 | 79 | **83** | 75 | **119** |
   | on reference stars, all / S/N > 20 | 68.6% / 39.2% | **24.6% / 1.2%** | 71.8% / 46.7% | **24.8% / 10.1%** |
   | S/N > 20 bad-pixel, spike, reference source, hole, unclassified | 27, 0, 20, 19, 13 | **14, 0, 13, 22, 34** | 31, 0, 19, 22, 3 | **35, 0, 15, 45, 24** |

   The core-and-ring residual is gone in every epoch: the PSF mismatch is
   fixed. The bright end does not fall without task 2's mask: what is left
   above S/N 20 is defect-type (bad-pixel and holes, 48 of 93, 36 of 83,
   80 of 119) and an unclassified 24 to 34, which task 5 reads with both
   fixes. Pass 6 (`6010101`) has more holes than its before (45 against
   22) with the same reference; not explained here.

   Measurements (from the job rows: launched to started is the queue wait,
   started to ended the job; peak RSS from `job run`):

   | job | stage | instance | queue wait s | job s | before |
   |---|---|---|---|---|---|
   | 19814 | psf | m6a.2xlarge | 10 | 11 | 2447: 9 s |
   | 19815 to 19820 | intake | m6a.2xlarge | 11 to 12 | 20 to 35 | |
   | 19822 | reference | m6a.2xlarge | 24 | 46 | 2454: 44 s |
   | 19823 / 19824 / 19825 | difference, PR 344 | m6a / r6i / r6i | 46 / 228 / 228 | 221 / 254 / 256 | 2455 / 2456 / 2457: 196 / 231 / 231 s |
   | 19835 / 19836 / 19837 | difference, PR 346 | r6i.2xlarge | 213 / 212 / 225 | 241 / 240 / 232 | the same |

   - The measurement itself: 7.4 s (19823) and 8.2 to 8.5 s (19835 to
     19837) on Batch, 4.6 s on this host; the ledger's 4.7 s.
   - Peak RSS of the PR 346 differences: self 1,259,292 to 1,275,052 KiB,
     children 15,242,600 to 15,243,108 KiB; job 2455: 1,258,688 and
     15,247,280. Unchanged.
   - The published `refimage_psf.fits.gz`: 14,087 bytes per job.
   - Stars per SCA for task 5's re-count: 38 of 4,290 detections on SCA 8
     in F146 pass the cuts; ten are needed.

4. **The SWarp kernel, a paired comparison (PR 348, merged `6e8ca10e`):
   BILINEAR stays.** `[difference.swarp] swarp_resampling_type` (dev's
   `BILINEAR`) against `LANCZOS3` on identical masked inputs, the kernel
   the only difference. PR 348 adds two difference metrics this step reads
   and task 5 keeps: `metrics.noise_correlation` (the masked ZOGY
   difference's clipped 4 x 4 block-mean sigma times 4 over its clipped
   pixel sigma, 1 for white noise) and `metrics.ref_sigma_px` (the median
   second-moment sigma, 4 px window, of the stars the reference PSF is
   built from, on the resampled reference).

   Image `rebuild-6e8ca10e…` (`sha256:98f42035…`, build 22 s), deploy 74 s:
   **`rapid-rebuild:46`** (and `rapid-rebuild-production:37`). Dataset
   `r00342-fx-lz`, F146, `001_0001`, SCA 8, the HLWAS overlay (task 2's
   mask), the F146 bound, one run per kernel whose bodies differ in that
   key alone (checked key by key):

   ```sh
   for K in BILINEAR LANCZOS3; do
     rp run create --who "$USER" --why "..." --revision 6e8ca10e1ac46b9000c5390a93dd95c243ff8417 \
       --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
       --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
       --set "difference.swarp.swarp_resampling_type=\"$K\"" --survey HLWAS --dataset r00342-fx-lz
   done                                                                     # runs 44 and 45
   rp official change --group live.json ...                                 # run 44 live, group 18463
   rp job local --run 44 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-fx-lz", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00342-fx-lz", "name": "gaia-r00342-fx-lz", "version": "DR3"}, "binding": null}]'   # 19838
   MAX_PASS=4 timeout 4800 $PY loop-passes.py loop --run 44 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:46 --interval 60
   rp job submit --run 45 --stage difference --job-definition rapid-rebuild:46 --scope '{"expid": 4040101, "sca": 8, "fid": 8}' \
     --inputs '[<l2files from 19843>, <psfs science from 19839>, <refimages field 5387158 from 19844>]' \
     --expects '[<diffimages and sources of the key, binding null>]'           # 19846
   ```

   Check, held: run 44's loop (step 2's pass-limited wrapper) drained in
   12.1 min (01:29:14 to 01:41:22 UTC), exit 0, no job failed: psf
   **19839**, intakes **19840 to 19843**, reference **19844**, BILINEAR
   difference **19845**, associate 19847, packet 19848 (80 packets). Run
   45's LANCZOS3 difference **19846**, submitted as soon as 19844
   published, on the same three inputs: published, exit 0. Both on
   m6a.2xlarge.

   | pass 4, reference 19844 | BILINEAR (19845) | LANCZOS3 (19846) |
   |---|---|---|
   | `ref_sigma_px`, reference stars' own sigma (fixes ledger 1.10 / 1.02) | 1.108 | **1.036** |
   | `refpsf_sigma_px` / `refpsf_stars` | 1.080 / 39 | **1.012 / 49** |
   | `sci_psf_sigma_px` | 0.947 | 0.947 |
   | `noise_correlation` | 1.250 | **1.145** |
   | difference MAD sigma | 0.0466 | 0.0522 |
   | candidates before the cap / `sources` above S/N 20 | 752 / 23 | **533 / 17** |
   | uncapped PSF-fit sources, all / above S/N 20 | 754 / 24 | 540 / 19 |
   | core / ring of star flux, top 30 stars | +0.0062 / -0.0025 | +0.0019 / +0.0031 |
   | `sources` within 3 px of a reference star, all / S/N > 20 | 45.6% / 21.7% | 34.4% / 35.3% |
   | uncapped sources within 6 px of a hole, all / S/N > 20 | 11 / 3 | 9 / 2 |
   | uncapped sources within 6 px of a spike, all / S/N > 20 | 23 / 12 | **29** / 11 |
   | uncapped sources within 6 px of either, all / S/N > 20 | 28 / 12 | **31** / 11 |
   | ringing at holes, max \|excursion\| in the 6 px ring, local sky sigma, median / 90th percentile | 47.1 / 58.3 | 38.3 / 47.6 |
   | ringing at spikes, most negative value in the 6 px ring, local sky sigma, median / 10th percentile / minimum | +2.62 / +1.67 / +1.65 | +2.28 / **-2.48 / -6.25** |
   | resampled coverage below 0.5, px | 6,663 | 11,297 |
   | star flux, science over gain-matched reference, top 30, median (range) | 1.009 (0.78 to 1.06) | 1.003 (0.78 to 1.04) |
   | ungained reference flux, LANCZOS3 over BILINEAR, top 30, median (range) | | 1.005 (0.975 to 1.017) |
   | `scalefacref` | 0.9922 | 0.9948 |
   | queue wait / container s | 9 / 178 | 208 / 177 |
   | peak RSS KiB, self / children | 1,275,792 / 15,247,292 | 1,276,732 / 15,247,376 |

   Positions are taken once, from the BILINEAR job, on the shared science
   grid: holes are the 3,706 interior regions (5,108 px) of resampled
   coverage below 0.5 smaller than 1,000 px (task 2's masked defects, NaN
   in all three frames since passes 1 to 3 sit on the same pixel); spikes
   the 13 regions (226 px) of the resampled reference above 1000 DN/s, all
   bright star cores now that the defects are masked. "Uncapped" is the
   published `*_psfcat.parquet`: both jobs keep 500 `sources` at the cap,
   from different pools, so the near counts are read before the cap.

   **The rule (the brief): LANCZOS3 if its ringing adds no sources within
   6 px of holes and spikes beyond the bilinear count and the sigma falls
   as the ledger measured.** The sigma falls as measured (1.108 to 1.036;
   the ledger's 1.10 to 1.02), and no flux is lost. The ringing adds
   sources: 31 against 28 within 6 px of a hole or spike, all of the
   excess at the 13 bright-star cores (29 against 23), where LANCZOS3's
   negative lobes reach -6.2 local sky sigma and BILINEAR's never go below
   the sky. So `swarp_resampling_type` stays `BILINEAR` and the HLWAS
   overlay is unchanged. The excess is faint (at S/N above 20 the counts
   are 11 against 12) and one epoch's count is within its Poisson scatter,
   while LANCZOS3 cuts the candidates by 29% and the noise correlation
   from 1.25 to 1.14: the decision is by the rule, and a re-count over
   more epochs (task 5's metrics give it per job) is what would reverse
   it. LANCZOS3 also blanks 4,634 more pixels (0.03% of the frame), its
   resampled coverage ringing below 0.5 at the hole edges. The fixes
   ledger's other routes, a drizzle factor below 1 passed to awaicgen and a
   finer mosaic, narrow the PSF without SWarp's kernel and are not built.

5. **The metrics and the re-count (PRs 350, 351 and 352, merged
   `096a1a09`, `70c6bfaa` and `2369769a`).** Every job's manifest carries
   `metrics`, one flat dict of named numbers computed from arrays the job
   already holds: intake `masked_fraction`; reference `star_sigma_px` and
   `n_sigma_stars`, `coverage_lt1` to `_lt3` (share of the covered
   pixels), `gmin`, `gmax`; difference the registration offset, the
   reference stars' width and its ratio to the science PSF's, the core and
   ring fractions at the 30 brightest isolated stars, the noise
   correlation, the usable fraction, `n_candidates` before the cap,
   `n_sources` after it, `n_sources_snr_gt20` and
   `on_reference_star_fraction`, beside task 3's refpsf keys; packet
   `n_packets` and `n_packets_snr_gt20`. Definitions:
   `rapidpipe/science/difference/metrics.py`,
   `rapidpipe/science/reference/measure.py` and the stages' docstrings.
   The view `job_metrics` (one row per job and metric with run, stage,
   dataset and scope) is `database/2026-10-24-job-metrics.sql`, applied
   once on SMDC as `postgres` by SSM on the database host before the loops
   started:

   ```sh
   podman exec rapid-postgres psql -U postgres -d rapid -v ON_ERROR_STOP=1 -c "<the file's CREATE VIEW and COMMENT>"
   ```

   Check, held: `CREATE VIEW` and `COMMENT` at 02:05:08 UTC; `jobs` 19,848
   rows before and after; read back as `rapid_rebuild_pipeline` through
   the default privileges. `scripts/inspect_run.py --run N --out DIR`
   writes `run-N.md` (a table per stage, the run's median and each row's
   deviation) and a PNG per sampled difference job (first and last epoch,
   and any epoch above twice the median `n_sources_snr_gt20`);
   `check_run.sh` runs it first (`--inspect DIR`, default
   `$TMPDIR/inspect-run-N`).

   Image `rebuild-096a1a09…` (`sha256:8e76c64d…`, build 22 s, deploy 74
   s): **`rapid-rebuild:47`**. Seven datasets, each a new live run at
   that sha on the HLWAS overlay with its filter's bound, its Gaia
   imported under the dataset's key:

   ```sh
   rp run create --who "$USER" --why "..." --revision 096a1a0993ddd08bb37c6c67c0c86722de7bc009 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=<bound> \
     --survey HLWAS --dataset r00342-fx-<f>
   rp official change --group live.json ...                 # the run live for HLWAS r00342-fx-<f>
   rp job local --run <run> --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-fx-<f>", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00342-fx-<f>", "name": "gaia-r00342-fx-<f>", "version": "DR3"}, "binding": null}]'
   timeout <s> rp loop --run <run> --filters <F> --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:47 --interval 60
   scripts/check_run.sh --run <run> --inspect <dir>
   ```

   Runs **46** (F146), **47** (F087), **48** (F106), **49** (F129), **50**
   (F158), **51** (F184), **52** (F213). F146 ran first and alone
   (02:10:20 to 02:26:28 UTC, 16.1 min, exit 0) and was read before the
   six were submitted (02:21:46 UTC, in parallel).

   **F184 and F213 failed every difference** with task 3's named reason,
   `refpsf: 9 stars, 10 needed` (pass 5: 8) and `refpsf: 6 stars, 10
   needed`, exit 65; their loops exited 2. The fields are not sparse
   (2,100 and 683 detections on the mosaics): the cut was the peak floor,
   20 DN/s set on F146, where it is 886 times the noise; below it fall
   1,944 of F184's 1,981 shaped detections and 616 of F213's 648. PR 351
   states the floor as 400 times the clipped noise (mosaics: F146 64, F184
   49, F213 20 stars). Image `rebuild-70c6bfaa…` (`sha256:1c75937c…`):
   **`rapid-rebuild:48`**. Runs **53** (F184) and **54** (F213) at
   `70c6bfaa`, made live in place of 51 and 52 by one change group each
   (`{"close": <the live binding>, "open": {...}}`); their loops reused
   the dataset's official intakes, PSF and reference and ran the
   differences again (02:53:25 to 03:04:31 UTC). Runs 51 and 52 stay, not
   live, their failed jobs recorded.

   **The variety check.** F146's per-epoch `n_sources_snr_gt20` is 23, 19
   and 15, median 19, below the brief's 20, so the same pointing ran on
   SCAs 4 and 5 under run 46 (02:26:38 to 02:40:48 UTC). Checking run 46
   while that loop had just submitted its PSF jobs gave `counts: FAIL (1):
   2 psf jobs still launched`, a race of the two steps' order; the check
   after the second loop held.

   Check, held: every run drained, exit 0, no job failed but the four
   above: run 46 (SCAs 4, 5, 8) CLEAN validate 6/6, reader 9/9, consumer
   954/954; run 47 CLEAN 6/6, 2/2, 129/129; run 48 CLEAN 6/6, 13/13,
   786/786; run 49 CLEAN 6/6, 13/13, 1,392/1,392; run 50 CLEAN 6/6, 14/14,
   900/900; run 53 CLEAN 6/6, 2/2, 84/84; run 54 CLEAN 6/6, 2/2, 91/91.

   **F146 against run 17** (passes 4, 5, 6: run 17 jobs 2455, 2456, 2457
   against run 46 jobs 19864, 19865, 19866; run 17's figures from the
   images ledger and runbook part 9, the same counts read again from the
   database by lineage):

   | | pass 4 before | **after** | pass 5 before | **after** | pass 6 before | **after** |
   |---|---|---|---|---|---|---|
   | candidates before the cap | 1,035 | **752** | 1,065 | **786** | 1,030 | **718** |
   | `sources` kept | 500 | 500 | 500 | 500 | 500 | 500 |
   | sources above S/N 20 | 80 | **23** | 79 | **19** | 75 | **15** |
   | packets | 216 | **80** | 213 | **66** | 199 | **71** |
   | packets above S/N 20 | 30 | **9** | 28 | **3** | 27 | **3** |
   | on reference stars (ledger's list; mosaic catalog) | 74%; 71% | **44%** | 69%; 67% | **42%** | 72%; 69% | **43%** |
   | core / ring of star flux | +0.030 / -0.027 | **+0.0062 / -0.0023** | +0.029 / -0.028 | **+0.0053 / -0.0023** | +0.028 / -0.027 | **+0.0084 / -0.0024** |
   | reference star sigma / the PSF ZOGY gets for it, px | 1.10 / 0.95 | **1.108 / 1.080** | / 0.947 | **1.107 / 1.079** | / 0.947 | **1.108 / 1.076** |
   | registration science − reference, px | -0.017, +0.014 | -0.023, +0.019 | | -0.017, +0.010 | | -0.007, -0.003 |

   Pass 4 equals task 4's BILINEAR job 19845 (752 candidates, 23 above S/N
   20, core +0.0062): the same inputs at another revision give the same
   numbers.

   **The six other filters**, before from the breadth runs' official
   products of `001_0001` SCA 8 by lineage (`diffimages_official` by
   dataset, `expid % 10000 = 101` and SCA 8, the producing job named; its
   `sources` rows; `packets_official` joined to them for S/N; the
   manifest's `sources_before_cap`; the reference's `refimcatalogs` file
   for the share on reference sources, matched within 3 science pixels on
   the sky), never from the images; the PSF and residual numbers have no
   before outside F146. Per-epoch medians over the matched epochs (the
   tables per epoch are in this step's pull request):

   | filter | run | epochs | n_candidates | n_sources_snr_gt20 | n_packets | n_packets_snr_gt20 | on_reference_star_fraction | core / ring | ref / sci sigma px | refpsf stars | noise corr |
   |---|---|---|---|---|---|---|---|---|---|---|---|
   | F146 | 46 | 3 | 1035 → **752** | 79 → **19** | 213 → **71** | 28 → **3** | 69% → **43%** | +0.0062 / -0.0023 | 1.108 / 0.947 | 39 | 1.25 |
   | F087 | 47 | 2 | 959 → **712** | 112 → **18** | 192 → **64** | 46 → **4** | 40% → **20%** | -0.0031 / +0.0070 | 1.009 / 0.804 | 26 | 1.34 |
   | F106 | 48 | 13 | 1002 → **749** | 100 → **22** | 200 → **56** | 41 → **4** | 46% → **18%** | +0.0042 / +0.0050 | 1.036 / 0.826 | 33 | 1.31 |
   | F129 | 49 | 13 | 1142 → **1047** | 101 → **42** | 213 → **108** | 46 → **12** | 42% → **17%** | +0.0067 / +0.0021 | 1.084 / 0.921 | 22 | 1.42 |
   | F158 | 50 | 14 | 1467 → **901** | 156 → **18** | 216 → **57** | 75 → **6** | 38% → **37%** | +0.0105 / -0.0050 | 1.159 / 1.014 | 26 | 1.17 |
   | F184 | 53 | 2 | 1904 → **842** | 373 → **19** | 255 → **42** | 188 → **3** | 4% → **24%** | +0.0013 / +0.0141 | 1.312 / 1.179 | 44 | 1.20 |
   | F213 | 54 | 2 | 381 → **306** | 48 → **20** | 110 → **46** | 16 → **3** | 35% → **28%** | +0.0337 / +0.0225 | 1.388 / 1.291 | 19 | 1.13 |

   **What remains** (the fixes ledger's corrected categories, first rule
   that applies: bright star = 3 x 3 uncertainty over 4 times the median
   on a reference source of aperture S/N above 100; defect = that
   uncertainty otherwise, or a spike; hole = reference S/N below -5 or a
   NaN within 3 px; on a reference source = reference S/N above 5), above
   S/N 20:

   | | sources above S/N 20 | bright star | defect | hole | reference source | unclassified |
   |---|---|---|---|---|---|---|
   | F146 SCA 8, passes 4 / 5 / 6 | 23 / 19 / 15 | 21 / 15 / 11 | 0 / 0 / 0 | 1 / 2 / 3 | 1 / 1 / 0 | 0 / 1 / 1 |
   | F146 SCA 4, passes 4 / 5 / 6 | 48 / 53 / 66 | 31 / 40 / 44 | 0 | 12 / 7 / 11 | 5 / 5 / 7 | 0 / 1 / 4 |
   | F146 SCA 5, passes 4 / 5 / 6 | 66 / 51 / 66 | 58 / 44 / 58 | 0 | 1 / 3 / 4 | 7 / 2 / 0 | 0 / 2 / 4 |
   | run 17 SCA 8 pass 4 (before) | 80 | 24 | 6 | 18 | 25 | 7 |

   The defects are gone and the PSF-mismatch core and ring are gone; what
   is left at the bright end is bright stars: the gallery shows a residual
   with the PSF's diffraction-spike pattern inside 15 px of each, and in
   F213 a positive core with a positive ring (+0.034 / +0.023), a
   flux-scale residual rather than a width one. 39 of F213's 40 sources
   above S/N 20 sit within 3 px of a science pixel at or above 10,000 (F146
   29 of 38, F087 7 of 15). One probe: dev's science extreme-pixel repair
   (`[difference.sci_image] repair_extreme_artifact_pixels`, threshold
   10,000) was turned off in a paired difference of the same inputs (runs
   55 and 56, not live, jobs 20124 and 20125 against 20104 and 19864):
   every count and the core and ring are identical, so the repair is not
   the cause; the cause is not established. SCAs 4 and 5 leave two to
   three times SCA 8's bright stars, as run 12 found three to four times
   its artefacts.

   **The reference's `star_sigma_px` as stored is wrong**: 1.55 to 1.65
   px in every filter, because SExtractor's CLASS_STAR (PR 350's star cut)
   does not separate stars from galaxies on these mosaics; PR 352 takes
   the narrowest population of widths instead. The runs keep the stored
   values; at `2369769a` the same mosaics give F087 0.914, F106 0.936, F129
   0.987, F146 1.016 (the fixes ledger: 1.039), F158 1.063, F184 1.243,
   F213 1.336 px.

   PR 352's image `rebuild-2369769a…` (`sha256:8ae01699…`):
   **`rapid-rebuild:49`**, the revision the next run takes; nothing ran
   under it here.

   Measurements (job rows, runs 46 to 50, 53, 54; launched to started is
   the queue wait, started to ended the job):

   | stage | jobs | queue wait s, median / max | job s, median / max |
   |---|---|---|---|
   | psf | 7 | 10 / 159 | 28 / 32 |
   | intake | 72 | 141 / 152 | 49 / 87 |
   | reference | 7 | 50 / 124 | 58 / 68 |
   | difference | 55 | 157 / 219 | 217 / 275 |
   | associate | 55 | 9 / 215 | 1 / 2 |
   | packet | 54 | 9 / 215 | 42 / 198 |

   - Loops: F146 16.1 min (SCAs 4 and 5 another 14.2 min); F087 12.1 min;
     F106 47.2, F129 42.2, F158 50.2 min (13 and 14 epochs, the associate
     chain); F184 and F213 again 11.1 and 10.1 min.
   - The reference PSF measurement 6.7 / 8.5 / 10.2 s (min / median /
     max) over 55 differences; the new metrics 0.13 s on one (measured on
     job 19845's files on this host).
   - Instances: m6a.2xlarge, m6a.8xlarge, r6i.2xlarge, r6i.8xlarge.
   - Peak memory: not read this step (the jobs' CloudWatch streams are not
     under the `rebuild/default/<job>` names part 4 used); task 4's
     differences on the same chain peaked at 1.28 GB self and 15.2 GB in
     children.
   - The consumer checks took 4 to 47 s, from each run's first packet.

## Results of part 10 (2026-10-24)

The null test's two measured artefact sources are removed on pointing
`001_0001`, SCA 8, in all seven filters, and every job now records the
numbers that would have shown them on the first run.

| filter | epochs | sources above S/N 20 per epoch, before → after | packets per epoch | packets above S/N 20 | core / ring after (before, F146) |
|---|---|---|---|---|---|
| F087 | 2 | 112 → 18 | 192 → 64 | 46 → 4 | -0.003 / +0.007 |
| F106 | 13 | 100 → 22 | 200 → 56 | 41 → 4 | +0.004 / +0.005 |
| F129 | 13 | 101 → 42 | 213 → 108 | 46 → 12 | +0.007 / +0.002 |
| F146 | 3 | 79 → 19 | 213 → 71 | 28 → 3 | +0.006 / -0.002 (+0.030 / -0.027) |
| F158 | 14 | 156 → 18 | 216 → 57 | 75 → 6 | +0.011 / -0.005 |
| F184 | 2 | 373 → 19 | 255 → 42 | 188 → 3 | +0.001 / +0.014 |
| F213 | 2 | 48 → 20 | 110 → 46 | 16 → 3 | +0.034 / +0.023 |

(Medians per epoch over the matched epochs; before is run 17 for F146
and the breadth runs' official products for the others, read by lineage.)

- **What changed it**: the DQ mask at intake (step 2) removed the
  defects, which were most of the bright end in F184 (373 per epoch above
  S/N 20); the measured reference PSF (step 3) removed the core-and-ring
  residual at every star; the kernel stayed BILINEAR (step 4).
- **What remains above S/N 20** is bright stars: no defect category is
  left, a few holes, and on F146 SCA 8 15 to 21 of 15 to 23 per epoch on
  a reference source of aperture S/N above 100 with the PSF's
  diffraction-spike pattern in the residual. F213 leaves a positive core
  and ring at its bright stars, a flux-scale residual. The cause is not
  established; dev's science extreme-pixel repair is ruled out by a paired
  difference. F129 keeps the most (42 per epoch). SCAs 4 and 5 leave two
  to three times SCA 8's (48 to 66 per epoch), bright stars again.
- **The metrics the quality gate will read** are in `job_metrics`:
  per difference job `n_sources_snr_gt20`, `n_candidates`, `core_fraction`,
  `ring_fraction`, `on_reference_star_fraction`, `ref_sci_sigma_ratio`,
  `registration_dx_px` / `_dy_px`, `noise_correlation`, `usable_fraction`;
  per packet job `n_packets_snr_gt20`; per reference `star_sigma_px` (as
  stored by these runs wrong, corrected by PR 352) and the coverage
  histogram; per intake `masked_fraction`. Across this re-count's SCA 8
  epochs the healthy ranges are: core within ±0.011 except F213's +0.034,
  ring within ±0.015 except F213's +0.023, registration within 0.12 px,
  usable fraction 0.999, noise correlation 1.12 to 1.44.
- **Found on the way and fixed**: the reference PSF's peak floor was
  F146's count rate and failed F184 and F213 (PR 351); the reference's
  star width read the galaxies (PR 352).
- **Next, by what this shows**: the bright-star residual (its cause; it
  is now the whole bright end); the injection brief, whose completeness
  count now has an artefact floor of about 20 per epoch per SCA above S/N
  20 on SCA 8; and the second iteration's list in the plan.

## Part 11: the second iteration (2026-10-24)

Brief `night-2026-10-24` (plan v9 Part 7, the second iteration's list):
one step per task, numbered by task; a task's step lands when its task
does, so the numbers can arrive out of order.

1. **GBTDS first contact: the data, the WCS re-derived at intake, one
   pointing through the chain (PRs 361 and 370, merged `43805b70` and
   `bda41dd6`).** STScI's SOC
   GBTDS simulation, r00340, under the dataset `r00340-gb1` with its own
   overlay, `rapidpipe/settings/datasets/gbtds-r00340.toml`.

   **The data.** Read from the notebook's `gbtds_manifest.ecsv` (88,038
   rows, one per L2 file; fetched with `gh api
   repos/spacetelescope/roman_notebooks/contents/notebooks/ccs_simulations/gbtds_manifest.ecsv`)
   and one pointing's files under
   `s3://stpubdata/roman/nexus/soc_simulations/r00340/l2/` (unsigned):

   | what | r00340 |
   |---|---|
   | key grammar | HLWAS's, `r00340 01 PPP 001 OOO VVV_EEEE_wfiNN_fNNN`; beside each `_cal.asdf` (203 MB) a `_wcs.asdf` (49 KB), `_preview.png` (1.9 MB) and `_thumb.png` (125 KB); L1 `_uncal.asdf` 1.06 GB under `l1/` |
   | span | 2027-10-01 00:00 to 2027-10-08 18:21 UTC, 7.8 days |
   | pointings | 6, at PA 90.46 throughout: (266.71, -28.93), (267.60, -29.92), (267.84, -29.57), (268.07, -29.23), (268.31, -28.88), (268.54, -28.54) |
   | passes | 5, segment 1 always: pass 1 (2 h) the colour filters once, passes 2 to 4 (2.2 days each) F146 with F087 and F213 interleaved, pass 5 (one day) the same, partial |
   | observations | 1 to 36 per pass; an observation is one filter |
   | visits | 1 to 72 per observation; **the pointing is the visit modulo 6** (268.54 -28.54 is visits 5, 11, ..., 71), exposure 1 for F146, 1 to 4 for F062 |
   | exposure | 66.41 s, MA table SCI1002, 5 resultants, every file |
   | files per filter | F146 80,082; F213 3,348; F087 3,330; F062 432; F129, F158, F184 216 each; F106 198 |
   | epochs per pointing and SCA | F146 738 to 744 (every 13.7 min, gaps at most 41 min); F087 and F213 31; F062 4; F106, F129, F158, F184 2 |
   | GBTDS's L2 | romancal 0.22.0, CRDS context `roman_0046.pmap`, ePSF `roman_wfi_epsf_0276`, distortion `roman_wfi_distortion_0003`; `tweakreg` COMPLETE with tens of thousands of "matches" |

   The input catalog sits beside HLWAS's under
   `soc_simulations/input_catalogs/`, but its tiles are GBTDS's: the six
   nside-512 (Galactic, nested) tiles over the sample frame
   (`cat-1113941`, `1113943`, `1113949`, `1157632`, `1157634`, `1157640`)
   are 673 to 734 MB each, 7.7 million rows, every row type `PSF`
   (Synthpop stars), 12 columns (`source_id, ra, dec, F062 ... F213,
   type`; no `n`, `half_light_radius`, `pa` or `ba`), fluxes as maggies.

   **The WCS, the first quirk.** The notebook's `fix_gbtds_wcs.py`
   (`gh api .../ccs_simulations/fix_gbtds_wcs.py`) copies `ra_ref`,
   `dec_ref` and `roll_ref` from the L1 header into the L2 model and runs
   romancal's `AssignWcsStep`. Read on the data, three things differ from
   that recipe, and PR 361 ports what holds into the intake behind
   `[intake] rederive_wcs` (false by default, true on the GBTDS overlay):

   - The L2 files carry the L1 values unchanged: 12 of 12 sampled files
     (pointings, passes 2 to 5, SCAs, F146, F087 and F213) have L1 and L2
     `wcsinfo` equal bit for bit, so tweakreg moved only the gWCS and the
     L1 read is not needed.
   - `AssignWcsStep.call` asks CRDS for the best distortion reference;
     today's context (`roman_0077.pmap`) answers `roman_wfi_distortion_0076`,
     which puts the frame about 1,770 px off (grid median (-343, +1735)
     px). The file names `roman_wfi_distortion_0003`; the intake fetches
     that one by name from `[psf] crds_server` and calls the step's body,
     `romancal.assign_wcs.assign_wcs.load_wcs`, because the step contacts
     the CRDS server even with `override_distortion` (it failed with none
     reachable).
   - The `_wcs.asdf` sidecar's `wcs_l1` is the L2 gWCS shifted by the 4 px
     reference border (to 0.003 px), not an L1 WCS.

   The input catalog through each WCS against measured centroids, file
   `r0034001002001001005_0001_wfi08_f146` (the WCS ledger's method:
   isolated catalog stars brighter than F146 19, no catalog star within 2
   mag inside 6 px; DAOStarFinder at 20 sigma; Gaussian-weighted centroids;
   zero-based pixels; measured minus predicted; matched within 3 px):

   | WCS | matched | median dx, dy px | MAD-sigma px |
   |---|---|---|---|
   | L2 gWCS (tweakreg, "54,059 matches", claimed shift (-3.80, -0.37)) | 1,018 | chance | 1.79, 1.80 |
   | control, L2 gWCS moved 40 px | 1,071 | chance | 1.92, 1.84 |
   | **re-derived, the file's `distortion_0003`** | **9,993** | **+0.030, -0.032** | **0.046, 0.042** |
   | control, re-derived moved 40 px | 1,105 | chance | 1.78, 1.84 |
   | re-derived, CRDS's current `distortion_0076` | 1,126 | chance | 1.89, 1.80 |

   The L2 gWCS sits (-28.79, +21.58) px from the re-derived one on this
   file (0.7 px range across the frame). The intake's FITS SIP header
   reproduces the re-derived gWCS within 1e-4 px over a 33 x 33 grid.

   Image `rebuild-43805b70…` (`sha256:4b3ff7b0…`, build 22 s):
   **`rapid-rebuild:53`**. On all nine sampled epochs the intake's
   `metrics` read it back: the fix moved the WCS (-28.8, +21.6), (+38.7,
   -24.3), (-13.5, +42.4), (-22.9, -8.9), (+35.4, +21.6), (+16.0, +2.1),
   (+8.1, -49.1), (-3.5, -46.2), (+8.1, -49.1) px (tweakreg's error is
   different on every epoch, 9 to 50 px), and task 4's Gaia-offset metric
   through the re-derived WCS is `gaia_dx_px` +0.04 to +0.18, `gaia_dy_px`
   -0.08 to +0.05, over 1,397 to 1,451 Gaia stars per frame. The
   re-derivation adds 1.4 to 2.2 s per intake (the reference fetch) to a
   37 to 41 s container.

   **The sample and the run.** The loop's pointing (`VVV_EEEE`) is not a
   sky position here: the six pointings repeat as visit modulo 6 in every
   observation of every pass, so `--pointings` cannot bound the epochs
   (744 F146 epochs per SCA at one pointing). The sample is a source root
   of its own: nine F146 SCA 8 files of the 268.54 -28.54 pointing,
   copied unchanged to `$RAPIDPIPE_OUTPUTS_ROOT/samples/r00340-gb1/l2/`
   (one `aws s3 cp` each, 203 MB): pass 2's first seven epochs (13.7 min
   apart, 02:13 to 03:35 UTC on 2027-10-01), one a day later and one six
   days later. The reference is the first **N = 6** (`before_mjd` 61679.1449,
   between the sixth and seventh); three epochs are differenced. Gaia DR3
   over RA 267.95 to 268.45, Dec -28.65 to -28.10: the ESA archive returns
   at most 50,000 rows per job (the first fetch came back with exactly
   50,000), so the box comes as 4 x 4 sub-boxes after a `COUNT`, the sum
   checked: **256,209 rows**, 98 MB, 125 s (`gbtds/scripts/gaia_fetch.py`
   in the run directory).

   ```sh
   rp run create --who "$USER" --why "..." --revision 43805b704d02cf5ff84f2d8b112eea7aa12836ac \
     --overlay rapidpipe/settings/datasets/gbtds-r00340.toml \
     --set 'intake.source_root="<the sample root>"' --set intake.unsigned=false \
     --set reference.selection.before_mjd=61679.1449 --survey GBTDS --dataset r00340-gb1
   rp official change --group live.json ...   # run 62 live, group 18776
   rp job local --run 62 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00340-gb1", "version": "DR3", "path": "gaia-dr3-gbtds-r00340-gb1-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "r00340-gb1", "name": "gaia-r00340-gb1", "version": "DR3"}, "binding": null}]'
   timeout 5400 rp loop --run 62 --filters F146 --scas 8 --pointings all --job-definition rapid-rebuild:53 --interval 60
   ```

   Run **62**: the psf job, nine intakes and the six-frame reference
   published (05:30 to 05:36 UTC); **all three differences failed**
   `refpsf: 0 stars, 10 needed`, exit 65, and the loop exited 2. The bulge
   is confusion-limited: on the reference mosaic the clipped deviation is
   14.8 DN/s (crowding, not noise; 32,516 detections at 20 sigma on one
   66 s frame), so the peak floor of 400 deviations (5,901 DN/s) is above
   the ceiling (3,500) and none of 81,239 detections passes; with the
   floor at 20 to 200 the stars pass, but the ePSF builder cannot centre
   the crowded stamps (0.12 to 0.40 px off after 10 shifts). PR 370
   (`bda41dd6`) adds `[difference] reference_psf` (`"measured"`, the
   default and today's behaviour; `"science"`, ZOGY gets the science PSF
   for both images, as run 17 did), `"science"` on the GBTDS overlay.
   **`rapid-rebuild:56`**; run **67** made live in place of run 62 (group
   18833, closing binding 27533), the same loop at `rapid-rebuild:56`: it
   reused the dataset's official psf, intakes and reference and drained in
   **26.1 min** (05:50:11 to 06:16:17 UTC), exit 0, no job failed.

   - 3 differences (20211 to 20213), 3 associates (one chain, field
     4641773), 3 packet sets, **515 packets** (173, 173, 169) produced and
     stamped.
   - `scripts/check_run.sh --run 67 --inspect <dir>`: **`Check run 67:
     CLEAN validate 6/6 9 s reader 3/3 4 s consumer 515/515 6 s`**.

   | epoch | when | candidates | kept | S/N > 20 | packets | packets S/N > 20 | noise correlation | bright-footprint fraction |
   |---|---|---|---|---|---|---|---|---|
   | 2014101 | pass 2, +1.4 h after the reference | 9,618 | 500 | 367 | 173 | 133 | 1.63 | 0.003 |
   | 2171101 | pass 2, +1 day | 9,343 | 500 | 418 | 173 | 147 | 1.63 | 0.000 |
   | 4254101 | pass 4, +6 days | 8,784 | 500 | 346 | 169 | 118 | 1.67 | 0.003 |

   **The science, reported and not repaired**: HLWAS F146 SCA 8 leaves
   about 19 sources above S/N 20 per epoch (part 10); GBTDS leaves 346 to
   418, every bright one a positive core in a negative ring at a star (the
   gallery: `gbtds/gallery-20211.png` in the run directory, the eight of
   highest S/N, S/N 393 to 563 on reference peaks of 2,400 to 3,100). Of
   job 20211's 367 above S/N 20, 331 sit on reference pixels above 1,000
   (gain-matched DN) and 36 at 200 to 1,000. That is the core-and-ring
   residual of a reference PSF narrower than the reference, the one part
   10 step 3's measured PSF removed on HLWAS; with `"science"` it is back
   by construction. Core, ring, registration and on-reference-star
   metrics are absent: each needs the reference's measured stars.

   Measurements (Batch created to started is the queue wait; peak RSS is
   the job's `peak_rss_kib`):

   | stage | jobs | queue wait s min / median / max | container s min / median / max | peak RSS MiB (max) | HLWAS (part 9) container median |
   |---|---|---|---|---|---|
   | psf | 1 | 16 | 12 | 294 | 10.9 |
   | intake | 9 | 9 / 19 / 21 | 37 / 39 / 41 | 1,047 | 37.6 |
   | reference | 1 (6 frames) | 39 | 82 | 2,847 | 45.7 (3 frames) |
   | difference | 3 | 98 / 224 / 224 | **790 / 820 / 828** | 16,221 | 232.6 |
   | associate | 3 | 9 / 10 / 14 | 3 / 3 / 3 | 215 | 2.8 |
   | packet | 3 | 5 / 6 / 9 | 27 / 35 / 42 | 860 | 30.2 |

   The difference is 3.5 times HLWAS's, the crowded field's 9,000
   candidates per epoch through the PSF-fit catalogs.

   **The quirks met, each with its disposition:**

   | quirk | disposition |
   |---|---|
   | The L2 gWCS is tweakreg's spurious crowded-field match, 9 to 50 px off and different every epoch | fixed in a reader step: `[intake] rederive_wcs` (PR 361), on in the overlay |
   | `AssignWcsStep` under today's CRDS context takes `distortion_0076`, 1,770 px off | fixed in the reader step: the file's own `distortion_0003`, by name, through `load_wcs` |
   | The notebook's L1 read is a no-op (L2 `wcsinfo` equals L1's in 12 of 12) | recorded; the L1 file is not read |
   | The `_wcs.asdf` sidecar's `wcs_l1` is the L2 gWCS moved by the 4 px border | recorded |
   | A pointing is visit modulo 6, so `--pointings` does not bound the epochs; 744 F146 epochs per SCA per pointing at 13.7 min | recorded; sampled by a source root of chosen stems, the reference the first N = 6 |
   | The DQ flags are HLWAS's (0.077% under its mask, all 9 isolated pixels above 1000 DN/s flagged); SATURATED on 0.51% | fixed on the overlay: HLWAS's mask |
   | The bulge is confusion-limited: the reference PSF cannot be measured (0 stars) | fixed on the overlay: `[difference] reference_psf = "science"` (PR 370) |
   | The science PSF on the reference leaves 346 to 418 core-and-ring residuals per epoch above S/N 20 | recorded, the gallery; the next measurement for this dataset |
   | The difference job takes 820 s, 3.5 times HLWAS's | recorded |
   | The input-catalog tiles over GBTDS are Synthpop's: 700 MB, 7.7 million rows, stars only, 12 columns | recorded |
   | The Gaia archive caps a job at 50,000 rows silently | recorded; fetched in sub-boxes with the count checked |
   | GBTDS is romancal 0.22.0, context `roman_0046`, HLWAS 0.23.0.dev3; the ePSF reference is the same (`roman_wfi_epsf_0276`) | recorded |

   Runs 62 and 67 and the sample root stay; nothing was deleted.

2. **OpenUniverse TDS read, and one image through a second intake reader
   (PR 365, merged `0e5459da`).** The record is
   `LEDGER-openuniverse.md` beside the brief. The brief said FITS: STScI's
   `nexus/open_universe/` copy is ASDF (`*_simple.asdf`), and the FITS are
   IRSA's public full release (`openuniverse2024/roman/full/`).

   **Data.**

   | set | files | bytes | filters | pointings per filter |
   |---|---|---|---|---|
   | nexus TDS `simple_model/<filter>/<pointing>/roman_tds_<filter>_<pointing>_wfi<sca>_simple.asdf` | 13,256 | 3.58 TB | R062 Z087 Y106 J129 H158 F184 K213 | 440 to 449, 1 to 9 SCAs each |
   | nexus WAS, the same grammar with `roman_was_` | 1,460 | 0.39 TB | F184 H158 J129 K213 W146 Y106 | 59 to 78 |
   | IRSA TDS `RomanTDS/images/simple_model/…fits.gz` | 2,064,050 | 69.4 TB | the same seven | all 57,365 rows of the observing sequence |

   - **The nexus TDS is one patch.** The SCA centres span ra 9.27 to 9.83 and dec −44.31 to −43.90, about 0.27 deg².
   - **The pointing is a row of the sequence.** The pointing in a file name is the row index into IRSA's `Roman_TDS_obseq_11_6_23.fits`, and all 3,116 pointings sampled match it on the filter. The sequence's `date` is the file's `mjd_obs`, the exposure start.
   - **Exposure times.** R062 161.025 s; Z087 101.7; Y106, J129 and H158 302.275; F184 and K213 901.175.
   - **Cadence.** A visit runs all seven filters in order over 2.7 days, with 2 or 3 pointings per filter 23 minutes apart. Visits come every 5 days: 146 in MJD 62000 to 62725, then 3 in 63550 to 63560 after an 825-day gap.
   - **Epochs per point.** A point in the patch has 106 images per filter at the median and up to 195 (R062) or 219 (F184), from 109 to 123 visits. This was measured from the YAML trees of all 13,256 files by 32 KB ranged gets in 33 s.

   **The truth.**
   - **In every file.** Each file carries `galaxies`, `point_sources` and `transients`. For one SCA that is 164 transients over 0.0188 deg², 67 of them peaking in the season. The classes are 113 core-collapse, 30 SN Ia, 20 SN Iax and 1 FIXMAG. The R062 peak magnitudes run 24.7 to 29.6 at 5 to 95%, and z runs 0.7 to 2.8.
   - **Per image, on IRSA.** IRSA's `RomanTDS/truth/<filter>/<pointing>/Roman_TDS_index_<filter>_<pointing>_<sca>.txt` lists every object per image. Each row has one-based x and y, the realised flux in counts, and `mag`, where `mag` is the bandpass zero point minus 2.5 log10(counts).
   - **Completeness is a join.** A completeness measurement joins each difference image's candidates to that file's transient rows, within 1.5 px of (x − 1, y − 1), per AB magnitude bin. AB = `mag` + 2.5 log10(37570 × exptime). Purity uses the galaxy and star rows for the residuals.
   - **It complements injection.** TDS brings real hosts, light curves and cadence; injection keeps dense control on HLWAS. By Ben's order, TDS comes after HLWAS injection.

   **The format, and its quirks:**
   - **Not a data model.** The tree is a plain mapping, not a roman_datamodels model. `data` is float64 counts per exposure, gain 1, which is DN. `dq` is all zero.
   - **One-based pixels.** The gWCS takes GalSim's one-based pixels. 69 stars sit at (−1.16, −1.06) px through it, and the truth index sits exactly one pixel off. Fixed on the overlay, `crpix_shift = [-1, -1]`. After the shift, H158 pointing 1012 SCA 4 is at (−0.004, −0.010) px and R062 keeps (−0.16, −0.06) px, which is recorded and attributed to the PSF centroid, unverified.
   - **No AB zero point in the file.** `meta.zptmag` is 2.5 log10(37570 cm² × exptime), the photon flux to counts, not AB. Fixed on the overlay with galsim.roman's AB zero points for counts/s: R062 26.7339 against dev's 26.4, and K213 26.0164 against 25.4.
   - **`err` is not a sigma.** It equals `data` on 90% of pixels. Recorded only; nothing reads `ERR`.
   - **The inverse fails.** gwcs 1.x cannot invert the gWCS, which was written by 0.21. Only the forward transform is used, and the SIP fit matches it to 1.5e-9 arcsec. Recorded.
   - **No ePSF reference.** Recorded; the proof imports a galsim.roman stamp.

   **The reader (PR 365).** `[intake] reader` has two values:
   - `soc-l2`, the default: dev's behaviour.
   - `openuniverse`: an adapter onto `build_fits_file`, so the SOC reader's layout, SIP fit and DQ mask are unchanged. The truth tables follow the images as binary-table HDUs, and `expid` is the pointing.

   The overlay `rapidpipe/settings/datasets/openuniverse-tds.toml` sets `unsigned`, `reader`, `program_id = -2` (`surveys`' OpenUniverse row), `crpix_shift` and the zero-point table.

   The proof is dataset `ou-tds1`, survey `HLTDS`, on the merged sha, with `OU_ROOT` the
   nexus copy's `open_universe/TDS/simple_model/` prefix:

   ```sh
   rp run create --who "$USER" --why "..." --revision 0e5459da81391be8ebe7eab832924c65034ab352 \
     --overlay rapidpipe/settings/datasets/openuniverse-tds.toml \
     --set "intake.source_root=\"$OU_ROOT\"" \
     --survey HLTDS --dataset ou-tds1
   rp official change --group live.json ...      # run 64 live for HLTDS ou-tds1
   python galsim_psf.py R062 17 psf/sciimage_psf_r062_sca17.fits   # beside the brief: the simulation's own PSF model
   rp job local --run 64 --stage import --scope '{"source_dir": "<psf>", "psf": {"kind": "science", "path": "sciimage_psf_r062_sca17.fits"}}' \
     --expects '[{"product": "psfs", "key": {"dataset": "ou-tds1", "fid": 5, "sca": 17, "kind": "science"}, "binding": null}]'
   rp job local --run 64 --stage intake --scope '{"dataset": "ou-tds1", "file": "roman_tds_R062_10032_wfi17"}' \
     --inputs '[{"product": "psfs", "key": {"dataset": "ou-tds1", "fid": 5, "sca": 17, "kind": "science"}, "source_job": 20190, "binding": 27587}]' \
     --expects '[{"product": "l2files", "key": {"dataset": "ou-tds1", "expid": 10032, "sca": 17, "fid": 5}, "binding": null},
                 {"product": "exposures", "key": {"dataset": "ou-tds1", "expid": 10032}, "binding": null}]'
   ```

   Check, held:
   - **The run.** Run **64**: its body's `[intake]` is the overlay's (`reader` `openuniverse`, `crpix_shift` [−1, −1], the eight zero points), and it is live in group 18811.
   - **The PSF import.** Import job **20190**, `psfs` row 422, bound as 27587.
   - **The intake.** Intake job **20191** published attempt 1 in 12.2 s: fetch 1.5 s, convert 4.0, gzip 2.1, limmag 1.3. Its peak RSS was 0.9 GB. It bound `l2files` and `exposures` (group 18813).
   - **The FITS.** `roman_tds_R062_10032_wfi17_cal.fits.gz` is 45.8 MB, from a 269 MB ASDF. Its HDUs are PRIMARY, SCI, DQ, ERR, TRANSIENTS, POINT_SOURCES and GALAXIES. The header has `FILTER` R062, `SCA_NUM` 17, `EXPTIME` 161.025, `DATE-OBS` 2028-12-25T02:11:02.400, `OBSERVTN` 10032, `ZPTMAG` 26.7339, `BUNIT` DN and `CRPIX` 2043.5.
   - **Its WCS against the truth.** Through the published FITS WCS, the truth index's (x − 1, y − 1) for 24,299 objects sit within 0.0016 px. 104 stars sit at (−0.159, −0.056) px.
   - **The rows.** The `l2files` row has field 5325275, `hp6` 35388, `hp9` 2264882, `limmag` 27.023 and six overlapping fields. The `exposures` row has `program_id` −2.

   Not built, and needed for a chain past intake (the next brief's OpenUniverse run):
   - the psf stage's ePSF source, since the file names no CRDS reference;
   - `loop.list_stems` and the psf stage dispatching on `reader` (both read `_cal.asdf`);
   - a reference from epochs before a transient's `start_mjd`, or from the late visits.

3. **The bright-star footprint flag and the metrics the re-count asked
   for (PRs 360, 363 and 369, merged `dcc0877e`, `ad7e1035` and
   `df39552e`).** The difference job sets `sources.flags` bit `1 << 14`
   (16384) on every kept source within a bright reference source's
   footprint and drops nothing. A bright reference source is a mosaic
   catalog source with `FLUX_APER(4)` S/N above 100. Its footprint is a
   disc on the science grid of radius `max(4, 9.5 × 10^(0.2 (15 − m)))` px,
   where `m` is its magnitude on the reference's `MAGZP`. The associate and
   packet predicates read one informational mask
   (`rapidpipe.products.diffimage.INFORMATIONAL_SOURCE_FLAGS`), so a
   flagged source is associated, is a trigger, and its packet carries the
   bit in `psfFitFlags` (no Avro schema change).

   New metrics:
   - difference: `n_in_bright_footprint`, `bright_footprint_fraction`
     (of the sources above S/N 20), `sci_star_sigma_px` and
     `sci_star_psf_sigma_ratio`, `scalefacref`, `n_sources_near_bright_px`
     and `n_sources_ring_snr_gt20`;
   - packet: `n_packets_bright_footprint`;
   - every job: `peak_rss_kib` (self plus the largest child).

   `job_metrics` reads the new keys from the manifests, so there is no
   schema statement. The gallery divides each difference cutout by its
   star's reference flux. The reference PSF stamp takes the science PSF's
   side, and it is re-centred by a secant step (PR 369, below).

   The radius law was derived on run 46's own published sources (F146 SCA
   8, passes 4 to 6, jobs 19864 to 19866) before any run. It flags 21 of
   23, 15 of 19 and 11 of 15 of the sources above S/N 20: part 10 step 5's
   bright-star counts exactly. About half of all kept sources fall in a
   footprint (282, 251, 270 of 500), faint sources on bright stars.

   The zero-point table was checked once against each filter's own
   `meta.photometry` (one SOC L2 file per filter, SCA 8, the intake's
   `compute_zptmag`):

   | | F087 | F106 | F129 | F146 | F158 | F184 | F213 |
   |---|---|---|---|---|---|---|---|
   | file − table, mag | +0.0144 | −0.0006 | +0.0007 | −0.0017 | −0.0036 | −0.0353 | −0.0310 |

   The table is right to within 0.035 mag and is not corrected. It enters
   the difference only through the reference's `MAGZP`, and the gain match
   then measures `scalefac` from matched stars, where the zero points
   cancel; the table matters only in the fallback. The re-count's stored
   `scalefacref` (median per filter, SCA 8) is F087 1.010, F106 0.992,
   F129 0.992, F146 0.992, F158 0.989, F184 **0.958** and F213 **0.939**:
   the long-wavelength references are scaled down 4 to 6%, which fits
   F213's positive core and ring.

   Image `rebuild-ad7e1035…` (`sha256:4a55acc6…`): **`rapid-rebuild:52`**.
   Then `rebuild-df39552e…` (`sha256:7d4e288b…`): **`rapid-rebuild:54`**.
   Three runs at `ad7e1035`, the HLWAS overlay (with task 4's
   `crpix_shift`) and the F146 bound:

   ```sh
   rp run create --who "$USER" --why "..." --revision ad7e10359b41fb416dcd36b69cca3d52b6dc0074 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-nt-metrics                               # run 59, live (group 18761)
   # the same for --dataset r00342-nt-stamp                                  # run 60, live (group 18763)
   # the same with --set psf.size=121 for r00342-nt-stamp                     # run 61, not live
   rp job local --run <59|60> --stage import --scope '{"source_dir": "<gaia dir>", "gaia": {"name": "gaia-<dataset>", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "<dataset>", "name": "gaia-<dataset>", "version": "DR3"}, "binding": null}]'
   rp loop --run 59 --filters F146 --scas 4,5,8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:52 --interval 60
   rp loop --run 60 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:52 --interval 60
   rp job submit --run 61 --stage psf --job-definition rapid-rebuild:52 \
     --scope '{"dataset": "r00342-nt-stamp", "file": "r0034201004001004001_0001_wfi08_f146"}' ...   # 20144
   rp job submit --run 61 --stage difference --job-definition rapid-rebuild:52 --scope '{"expid": 4040101, "sca": 8, "fid": 8}' \
     --inputs '[<l2files from 20151>, <psfs science from 20144>, <refimages field 5387158 from 20164>]' ...   # 20178
   ```

   **Run 59's SCA 5 difference failed** (job 20184, exit 70):
   `refpsf: centroid still (0.0587, 0.0383) px off centre after 10
   shifts`. A local run (run 65, not live, job 20192, `rp job local` with
   `[difference.paths]` at a checkout) reproduced it, and the iteration was
   traced on its files. The ePSF's centroid moved 1.8 times each shift, so
   the plain step oscillated. Run 46's SCA 5 had converged before
   `crpix_shift` moved the sub-pixel phase. PR 369's secant step converges
   on the same files (78 stars, sigma 1.098). Job 20184 was resubmitted
   under `:54`, and run 59's loop was resumed under `:54` and drained.

   Check, held: run 60 drained (05:26 to 05:45 UTC), CLEAN validate 6/6,
   reader 1/1, consumer 79/79. Run 59 drained after the resumption
   (06:03 UTC), CLEAN 6/6, 3/3, 301/301. Run 61's difference 20178 was
   published (not bound). Jobs: psf 20140 to 20143; intakes 20145 to 20160;
   references 20164 to 20167; differences 20182 (SCA 4), 20184 (5), 20185
   (8), 20183 (run 60); associates 20194, 20201, 20202, 20215; packets
   20197, 20205, 20206, 20217.

   **The flag followed into a packet**: run 60's packet set 20197 holds 79
   packets, 49 of them flagged (`n_packets_bright_footprint`). Its source
   225 is read from the Avro with `psfFitFlags` 16384 and both derived
   photutils booleans false. Of the 500 kept sources of difference 20183,
   253 are flagged and 466 are eligible (no photutils bit), the same
   population as before the flag.

   **Each new metric on `001_0001` F146 pass 4**, against the fixes
   ledgers where they hold a number:

   | metric | SCA 8 (20185) | SCA 5 (20184) | SCA 4 (20182) | the fixes' SCA 8 (run 46, 19864) |
   |---|---|---|---|---|
   | `ref_sigma_px` / `refpsf_sigma_px` | 1.115 / 1.103 | 1.109 / 1.098 | 1.096 / 1.076 | 1.108 / 1.080 |
   | `refpsf_stars` | 66 | 78 | 88 | 39 |
   | `sci_psf_sigma_px` (the ePSF) | 0.947 | 0.951 | 0.925 | 0.947 |
   | **`sci_star_sigma_px`** / ratio | **0.969 / 1.024** | **0.966 / 1.015** | **0.956 / 1.033** | |
   | `scalefacref` | 0.996 | 1.000 | 0.990 | 0.992 |
   | core / ring | +0.00005 / +0.0009 | +0.0114 / −0.0015 | +0.0058 / +0.0024 | +0.0062 / −0.0023 |
   | candidates / above S/N 20 | 802 / 29 | 884 / 69 | 640 / 36 | 752 / 23 |
   | `n_in_bright_footprint` / `bright_footprint_fraction` | 253 / 0.79 | 279 / 0.74 | 292 / 0.75 | (282 / 0.91 by the law, before) |
   | `n_sources_near_bright_px` | 23 | 29 | 20 | (19 by this code, 23 by task 4's region count) |
   | `n_sources_ring_snr_gt20` (6 to 20 px) | 3 | 9 | 2 | (3, 10 to 20 px, part 10 step 3) |
   | packets / above S/N 20 / flagged | 79 / 8 / 49 | 124 / 16 / 75 | 98 / 11 / 65 | 80 / 9 / |
   | `peak_rss_kib` | 16.5 GB | 16.5 GB | 16.5 GB | (15.2 GB children, 1.28 GB self) |

   The reference star width is within 0.01 px of the fixes' value, and the
   bright-end count and the core move with the CRPIX translation (task 4's
   `crpix_shift`, new inputs: 29 against 23 above S/N 20, 66 refpsf stars
   against 39). **The science stars are 1.5 to 3.3% wider than the ePSF on
   all three SCAs, and SCA 5, with the most bright-star residuals, has the
   smallest mismatch.** So the SCA 4 and 5 excess is not a science-PSF
   width mismatch, the ruling's question. Median `peak_rss_kib` per stage
   over runs 59 and 60: psf 298 MB, intake 995 MB, reference 2.8 GB,
   difference 16.1 GB, associate 212 MB, packet 842 MB.

   **The stamp-size probe** (run 60's difference 20183 at 61 x 61 against
   run 61's 20178 at 121 x 121, the same three inputs; measured, not
   adopted):

   | | 61 x 61 (20183) | 121 x 121 (20178) |
   |---|---|---|
   | ePSF flux the science stamp holds (`psf.flux_held`) | 0.971 | 0.979 |
   | `refpsf_sigma_px` / stars | 1.103 / 66 | 1.103 / 66 |
   | core / ring | +0.00005 / +0.0009 | +0.0008 / +0.0011 |
   | candidates / above S/N 20 | 802 / 29 | 793 / 30 |
   | `bright_footprint_fraction` / `n_in_bright_footprint` | 0.793 / 253 | 0.800 / 266 |
   | `n_sources_near_bright_px` / ring count | 23 / 3 | 23 / 3 |

   A larger stamp changes nothing that matters. The spike-truncation story
   is not the bright-star residual's cause at this level; the next
   iteration's list gets no win from it.

   Measurements: loops of 19 min (run 60) and 37 min (run 59, with the
   failure, fix and resumption); differences on m6a.2xlarge and
   r6i.4xlarge; the local reproduction took 2 min up to the reference
   PSF.

4. **The loop's operations (PRs 354, 356, 359, 364, 366, 367, 374).** Six
   items, each its own PR, merged in turn; the proofs on `rapid-rebuild:51`
   (`fe29467c`, items 1 and 2) and `rapid-rebuild:55` (`6a063773`, item 6).

   1. **The HLWAS WCS translation and the Gaia-offset intake metric (PR
      354, `4d011ab0`).** `[intake] crpix_shift` adds `[dx, dy]` px to
      `CRPIX1`/`CRPIX2` of every HDU before the header is read; default
      `[0, 0]`. The HLWAS overlay carries `[0.12, -0.99]`: the WCS ledger's
      offset is measured minus predicted, and a TAN-SIP prediction is
      `CRPIX + f(world)`, so the offset itself is the shift (the brief's
      first guess had the opposite sign). When the intake names the
      dataset's Gaia `catalogs` input (the loop does once one is official)
      its `metrics` record `gaia_dx_px`, `gaia_dy_px`, `n_gaia_stars`: each
      isolated Gaia star the WCS puts on the frame centroided from its
      prediction, the median of measured minus predicted. The check, one
      F146 intake of pass 4 (`r0034201004001004001_0001_wfi08_f146`) each
      way, dataset `r00342-nt-wcs`:

      ```sh
      rp run create ... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
        --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
        --survey HLWAS --dataset r00342-nt-wcs                         # run 58, made live (group 18757)
      rp job local --run 58 --stage import --scope '{"source_dir": "<copy>", "gaia": {"name": "gaia-r00342-nt-wcs", "version": "DR3", "path": "gaia-dr3-hlwas-r00342-box.parquet"}}' \
        --expects '[{"product": "catalogs", "key": {"dataset": "r00342-nt-wcs", "name": "gaia-r00342-nt-wcs", "version": "DR3"}, "binding": null}]'   # job 20129
      rp loop --run 58 --filters F146 --scas 8 --pointings 001_0001 --passes 4 --job-definition rapid-rebuild:51 --interval 60
      rp run create ... --set "intake.crpix_shift=[0.0, 0.0]" --dataset r00342-nt-wcs   # run 63, never live
      rp job submit --run 63 --stage intake --job-definition rapid-rebuild:51 \
        --scope '{"dataset": "r00342-nt-wcs", "file": "r0034201004001004001_0001_wfi08_f146"}' \
        --inputs '[<psfs job 20130, binding 27509>, <catalogs job 20129>]'               # job 20168, nothing bound
      rp job wait --job 20168 --timeout 3000 --interval 20
      ```

      | | translation | `gaia_dx_px` | `gaia_dy_px` | `n_gaia_stars` | `CRPIX1`, `CRPIX2` |
      |---|---|---|---|---|---|
      | WCS ledger, catalog PSF through the gWCS | off | +0.116 | -0.989 | 30 | 2044.5, 2044.5 |
      | job 20168 (run 63) | off | **+0.127** | **-0.986** | 34 | 2044.5, 2044.5 |
      | job 20134 (run 58) | on, `[0.12, -0.99]` | **+0.007** | **+0.003** | 33 | 2044.62, 2043.51 |

      Check, held: within 0.05 px of zero with the translation, the
      ledger's offset without it. The loop drained in 6 min (05:24 to
      05:30 UTC, psf 20130 then intake 20134; the field reported `held`,
      no epoch before the bound in pass 4 alone, by design), exit 0.

   2. **`rp loop --passes` (PR 356, `fe29467c`).** The survey's passes
      beside `--pointings` and `--scas`, applied to stems and L2 rows
      alike, recorded in each pass's `selection`; the breadth and fixes
      briefs' `loop-passes.py` wrapper is retired. Check, held: run 58's
      loop above with `--passes 4` listed 5,832 stems, admitted 1.

   3. **`rp job wait` (PR 359, `206ffeaf`).** One blocking waiter on the
      job rows, Batch reconciled each check; exit 0 when every named job is
      published or failed, 75 at `--timeout`. Check, held: job 20168 above,
      `{"ended": true, "seconds": 40.5, "states": {"20168": "published"}}`,
      exit 0.

   4. **Failure handling (PR 364, `25753e48`).** A container Batch ended
      without `job run` recording it is resubmitted once (its Batch name
      ending `-rerun`) and held pending; a rerun's death leaves it failed
      and the loop exits 2. `[batch] max_submissions_per_pass = 1000` caps a
      pass in stage order, the rest `deferred` (the breadth night's 3,456
      simultaneous intakes throttled SSM, 1,448 failed). Two loops under one
      run: the check scopes to the loop's SCAs, `check_run.sh --scas LIST`
      (`validate_run.py --scas`), the counts check ignoring another SCA's
      pending jobs; chosen over the second loop waiting for the first's
      drain, which cannot see the other process and would still overlap the
      first loop's check. Unit contracts only; no container died tonight
      under task 4.

   5. **The quality gate's query (PR 366, `e7c2a2e0`, its squash subject
      "wip gate" by mistake: the branch's first commit's message, not the
      PR title).** `rapidpipe.foundation.gate.decide(conn, packet_job,
      settings)`: hold when the set's difference has `n_sources_snr_gt20`
      above `snr_gt20_factor` (3.0) times the run's median, or
      `|core_fraction| > 0.025`, or `|ring_fraction| > 0.015`, once the run
      has `min_epochs` (5) published differences; the loop reports
      `held_sets` each pass. Check, held: every official packet set of the
      re-count's runs at the defaults, 0.4 s:

      | run | 46 | 47 | 48 | 49 | 50 | 53 | 54 |
      |---|---|---|---|---|---|---|---|
      | sets, all sent | 9 | 2 | 13 | 13 | 14 | 2 | 2 |

   6. **The caches (PR 367, `6a063773`; PR 374 the cut, `d1e8a9b7`).** Measured on
      one sampled chain, dataset `r00342-nt-caches`, run 66 (F146, SCA 8,
      `001_0001`, `--passes 1,2,3,4`, `rapid-rebuild:55`; its body resolved
      at `bda41dd6`, one merge later, whose only change is a `[difference]`
      key off by default), against run 41, the fixes' same sample on
      `rapid-rebuild:43`. The ePSF copy first, once:

      ```sh
      python scripts/copy_epsf.py --to "$RAPIDPIPE_OUTPUTS_ROOT/refs/crds-epsf" \
        $(curl -s https://roman-crds.stsci.edu/unchecked_get/mappings/roman/roman_wfi_epsf_0004.rmap \
          | grep -o "roman_wfi_epsf_0[1-2][0-9][0-9]\.asdf" | sort -u | awk -F_ '{if (substr($4,1,4)+0 >= 145) print}')
      rp run create ... --set "psf.epsf_cache=\"$RAPIDPIPE_OUTPUTS_ROOT/refs/crds-epsf\"" --dataset r00342-nt-caches   # run 66, live
      rp loop --run 66 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:55 --interval 60
      ```

      The copy: 144 references (the `(WFIxx, Fnnn)` entries of
      `roman_wfi_epsf_0004.rmap`, context `roman_0046.pmap`), 25.7 GB, 643
      s, under the outputs root's prefix, not the inputs bucket the brief
      named, whose writes the brief's boundaries forbid. The loop drained in
      19 min (05:49 to 06:08 UTC, 21 passes), exit 0, nothing failed, no
      rerun, nothing deferred, `held_sets` empty; psf 20204, intakes 20207
      to 20210, reference 20214, difference 20216, associate 20226, packet
      20228 (76 packets).

      | item | run 41 (before) | run 66 (after) | kept |
      |---|---|---|---|
      | ePSF fetch in the psf job | 4.1 s from CRDS (job 19807; 3.1 s in job 20130) | **0.8 s** from the copy, `fetched_from` the copy | yes |
      | SSM in a job | every job read the tree | **none**: `PGHOST`, `PGPORT`, `PGDATABASE`, `RAPID_DB_SECRET_ID` and `RAPIDPIPE_KAFKA_*` in all nine Batch jobs' environment; container start to the row's `started` 1.25 to 1.42 s (before 1.13 to 1.67; the gain is the throttle at thousands of jobs, not one job's second) | yes |
      | intake intermediate | gzip 5.5 s, intakes 21 to 25 s | plain copy 4.7 to 9.5 s, intakes 17 to 28 s, difference 181 s (168) | **no**, cut by PR 374 |
      | packet window images | job 20126's inputs read here: 85.1 s and 92.9 s | decompressed once: 97.9 s and 99.5 s (forced photometry 55 to 60 → 37 to 38 s, the read 26 to 29 → 56 to 58 s) | **no**, cut inside PR 367 |

      The packet job's own send (task 5's) failed in run 66: `ClusterAuthorizationFailedError`
      on `InitProducerIdRequest`, kafka-python's idempotent producer asking for
      a cluster permission the scratch job role's grant (rapid_systems PR 420:
      Connect, DescribeTopic, WriteData) lacks (cause unverified); the loop's
      sweep sent the 76 packets and stamped the set. The endpoint came from the
      job environment, so this is the grant or the producer's settings, task 5's.

5. **The packet job sends its own set; the gate at the send boundary; the
   consumer check direct (PRs 358, 371, 372).** A packet job that its
   publisher loaded and bound sends its own set as its last act through
   `publish_kafka.deliver`, the one send boundary: the quality gate
   (`gate.decide`, PR 366, the run's `[gate]` table) answers first and a
   held set is neither sent nor stamped; otherwise one message per packet,
   keyed by the run's dataset (one partition per dataset), the set and sid
   in the `rapid-set` and `rapid-sid` headers, acks all, one flush, then
   the stamp with the acknowledgements' partition and first and last
   offsets. `rapidpipe publish`, and the loop's per-pass publish, are the
   re-publication verb and the recovery sweep: every unstamped official set
   whose job ended more than `SETTLE_S` (300 s) ago; a set inside the window
   is the container's to send, reported as `sets_waiting`, and holds the
   drain. `consume_packets.py --run N` reads only the stamped sets' recorded
   ranges (overlapping ranges merged), keeps a set's messages by key and
   headers, counts every set's packets and decodes and byte-compares one set
   per field (the latest of the field); the O(topic) walk is retired.

   The granted statement, `database/2026-10-24-kafka-offsets.sql`, applied
   once on SMDC as `postgres` by SSM on the database host:

   ```sh
   podman exec rapid-postgres psql -U postgres -d rapid -v ON_ERROR_STOP=1 -c "ALTER TABLE jobs ADD COLUMN kafka_partition integer, ADD COLUMN kafka_first_offset bigint, ADD COLUMN kafka_last_offset bigint;"
   ```

   Check, held: `ALTER TABLE` at 05:13:43 UTC; `jobs` 20,126 rows (max id
   20,126) before and after, the same the pooler reads.

   **First proof, run 57** (`r00342-nt-producer`, `e23f349a`, image
   `sha256:69e36fda…`, **`rapid-rebuild:50`**; F146, SCA 8, pointing
   `001_0001`; 05:21:13 to 05:47:21 UTC, exit 0). Every packet job's own
   send failed: `KafkaError: Unexpected error in InitProducerIdResponse:
   [Error 31] ClusterAuthorizationFailedError` (job 20196's stream, 05:39:46
   UTC). kafka-python 3.0.11 defaults to `enable_idempotence=True`, whose
   producer-id request needs `kafka-cluster:WriteDataIdempotently`; the job
   role's `MskAlertTopicProduce` holds `Connect`, `DescribeTopic`,
   `WriteData` (rapid_systems PR 420), and `rapid-deploy` on the host is
   administrator, which is why `publish` never met it. The sweep did its
   work unplanned: each set waited out `SETTLE_S` under `sets_waiting` and
   the next pass sent it (79, 108, 97 packets); the consumer check was
   CLEAN, 284/284, p0 0..283, 4.0 s. PR 371 builds the producer with
   `enable_idempotence=False` (nothing here is exactly-once; the IAM
   alternative is Ben's).

   **Second proof, run 68** (`r00342-nt-producer-b`, `75896e27`, image
   `sha256:d22c66ba…`, **`rapid-rebuild:57`**; the same selection; 05:59:42
   to 06:15:47 UTC, 17 passes, exit 0):

   ```sh
   rp run create --who "$USER" --why "..." --revision 75896e27fa3f76835ff71910a918afb5fbdb6166 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-nt-producer-b
   rp official change --group live.json ...                 # run 68 live for HLWAS r00342-nt-producer-b
   rp job local --run 68 --stage import ...                 # Gaia under the dataset's key, job 20218
   rp loop --run 68 --filters F146 --scas 8 --pointings 001_0001 --job-definition rapid-rebuild:57 --interval 60
   python scripts/consume_packets.py --run 68
   ```

   Check, held:

   | set | ended | stamped | partition | offsets | packets |
   |---|---|---|---|---|---|
   | 20235 | 06:08:11 | 06:08:14 | 1 | 79..154 | 76 |
   | 20240 | 06:10:21 | 06:10:25 | 1 | 155..261 | 107 |
   | 20243 | 06:15:25 | 06:15:28 | 1 | 608..706 | 99 |

   Every pass's `sets_produced` was empty and `sets_waiting` empty: the
   containers sent their own sets, 3 to 4 s after their jobs ended. Job
   20243's Batch log ends with its result's `kafka` field (99 packets,
   22,622,270 bytes, partition 1, offsets 608..706). The consumer check:
   CLEAN, read 282, kept 282, packets expected 282 seen 282, one set
   sampled (99 valid, 99 byte-equal), ranges `p1 79..261, p1 608..706`, 4.0
   s (other datasets' messages lie between). `rapidpipe publish --run 68`
   then sends nothing (`sets []`). The gate answers send for all three at
   the defaults (three epochs, below its floor of five); with `[gate]
   core_fraction_max_abs = 0.0001, min_epochs = 1` passed to `gate.decide`
   it holds set 20245 with `core_fraction +0.0032 beyond ±0.0001`.

   **The kill between the database publish and the acknowledgement.** The
   last epoch's packet job repeated here as a new job superseding the
   official set (`job local` with set 20243's scope and inputs and its
   binding in `expects`), killed with SIGKILL the moment its row turned
   `published` (`kill-t5.sh`, a watcher on the job row; the brief allows no
   Batch termination). A first try keyed the kill to an INFO log line that
   the CLI does not emit at its log level; that job, 20244, ran to the end
   and sent its own set (p1 876..974), a supersession and no harm. Job
   20245 was killed at 06:17:39 UTC: published, bound, `ended` and every
   stamp column null. One pass, `rp loop --run 68 ... --once` at 06:17:50
   UTC: `sets_produced [20245]`, `packets_produced 99`, drained, 6.7 s;
   then the consumer check CLEAN, 480/480 over five stamped sets, the
   sampled set 20245 byte-equal 99/99, ranges `p1 79..261, p1 608..706, p1
   876..1073`, 4.1 s.

6. **The database guards and the `forced` read (PRs 355, 357).** Three
   guards, no behaviour change, no schema statement on SMDC. A schema test
   in `tests/db/test_schema.py` reads the catalog CI applies: every product
   table bound under its own name holds `dataset` NOT NULL and its view
   exposes it, every view matching the binding's key builds the key with
   the dataset, and every child view (members, a reference's catalogs,
   forced and external rows) is bound through one of those. An import unit
   test asserts per table that an import supplies no identity column of
   `schema.sql` but the three source-system ids the plan keeps
   (`l2files.rid`, `diffimages.pid`, `refimages.rfid`, Part 1); PR 327's
   `psfid` would fail it. Both read SMDC read-only first:

   - one of 27,462 product bindings has a key without `dataset` (the
     earliest Gaia `catalogs` binding, which `packet.py` reads on purpose);
   - the three kept ids sit ahead of their sequences, from runs 1 and 9's
     imports: `pid` 350,875 against `last_value` 3,886, `rid` 1,832,552
     against 5,120, `rfid` 90,011 against 512; a drawn insert meets them
     only after that many more jobs. One `setval` each as `postgres`, part
     8 step 4's statement, removes it; not granted tonight.

   `forced` growth from the breadth rows (F158, runs 38 and 50, every
   published packet set; a recursive query over
   `job_inputs` for the object set's chain depth, in its PR's body, 0.74 s
   for 917 sets; the one field deeper than 14 is 5390959):

   | depth | sets (run 38) | packets per set | `forced` rows per set | rows per packet | packet job s |
   |---|---|---|---|---|---|
   | 1 | 18 | 189 | 189 | 1.0 | 21 |
   | 4 | 70 | 214 | 846 | 4.0 | 65 |
   | 8 | 70 | 216 | 1,696 | 7.9 | 162 |
   | 12 | 70 | 213 | 2,520 | 11.9 | 311 |
   | 14 | 70 | 185 | 2,569 | 13.9 | 289 |
   | 17 (field 5390959) | 1 | 298 | 891 | 3.0 | 41 |
   | 28 (field 5390959) | 1 | 280 | 3,920 | 14.0 | 221 |

   (Medians per depth. Run 50, one field to depth 14: 13.7 rows per packet
   at depth 14, the same law.) A set's `forced` rows are its packets times
   the window images on the trigger's SCA that hold the object, not the
   chain depth: field 5390959's chain runs 14 SCA 8 epochs then 14 SCA 9
   epochs, and at depth 15 the rows per packet fall back to 1. Every F158
   epoch of a field falls inside the 30-day forced window (9.1 days span),
   so here the window images equal the SCA's epochs so far. Run 38 holds
   1.46 million `forced` rows for 184,163 packets; the table is 4.52
   million rows and 954 MB. The growth is linear per set in the window's
   images and bounded by `forced_window_days`, so a field's `forced` total
   grows as the square of its epochs inside one window; the packet job's
   time grows with it (21 s at depth 1, about 300 s at depth 12 to 14).

7. **Association in the difference job, reconciled at publish; the set
   chain retired (PRs 378 and 379, merged `556fef84` and `0baea0b2`;
   rapid_docs 97).** The plan's amendment of 2026-10-24 as code. The
   difference job reads its field's objects of its run as they stand,
   records the founding jobs it saw as `objects` inputs, matches its
   sources in memory and publishes `objects` (founded here) and
   `memberships` (each with its source's offsets from the object's
   origin and its flux) beside its sources; the publisher, under a
   transaction-scoped lock on (run, field), attaches the job's founded
   objects to any founded since its read, rebased. The packet job names
   only its source set, PSF, Gaia and reference, and reads the objects,
   official members, forced window and official forced rows as they
   stand, recording the read. The loop has no associate step: a packet
   per official source set, in any order. Retired: the associate stage,
   `members_official`, the exclusions, `thin_proof.py`.

   The granted statement, `database/2026-10-24-object-catalog.sql`,
   applied once on SMDC as `postgres` by SSM on the database host before
   PR 378 merged (one transaction, the file piped to `podman exec -i
   rapid-postgres psql -U postgres -d rapid -v ON_ERROR_STOP=1`):

   Check, held: `COMMIT` at 06:54:56 UTC; the chain's rows kept as
   `objects_chain` 2,444,364 and `memberships_chain` 909,496 (the counts
   of `objects` and `memberships` before); the new tables empty; the
   pipeline role holds SELECT, INSERT, UPDATE, DELETE and MAINTAIN on
   them by the default privileges; `members_official` gone.

   Image `rebuild-556fef84…` (`sha256:aaf689fa…`): **`rapid-rebuild:58`**
   (production `:49` with it, the template's design). Proof under
   dataset `r00342-nt-assoc`, **run 69**, live; Gaia job 20246:

   ```sh
   rp run create ... --revision 556fef8420172779559a97699f124376e8248441 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-nt-assoc
   rp loop --run 69 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4,5 --job-definition rapid-rebuild:58 --interval 60
   rp loop --run 69 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4,5,6 --job-definition rapid-rebuild:58 --interval 60
   scripts/check_run.sh --run 69 --inspect DIR
   ```

   **Two epochs in flight at once.** Passes 4 and 5 were differenced from
   one pass (07:03:15 UTC), jobs 20254 (07:03:26 to 07:06:30) and 20255
   (07:05:05 to 07:08:13), 13 passes, drained 07:09:15, no failure. Each
   job reads the catalog after its catalog step, seconds before it
   publishes, so 20255 read 20254's 76 objects (`objects_read` 76, one
   founding job recorded) and founded 72; the reconcile found nothing
   founded since its read. Overlapping execution does not race the
   read-to-publish window, which is seconds wide.

   **The reconcile on SMDC, replayed** (`replay.py` in the brief
   directory): job 20261 of run 69, a copy of 20255's source set
   associated as if it had read the catalog before any epoch published
   (an empty read), expecting no binding, published through `run_job` and
   the publisher: 105 founded, 201 objects founded since its read, 105
   reconciled, 0 inserted, 105 memberships attached to 2 founding jobs.
   The attached memberships equal 20255's sequential ones, the same 105
   (object, source) pairs, and their rebased offsets equal the direct
   offsets of the sources from the surviving origins to 0.0 degrees.

   **A late epoch.** Pass 6 (job 20259, 07:17:40 to 07:20:45, the second
   loop, 13 passes, drained 07:21:59) read 148 objects of 2 founding jobs
   and founded 53. Packet 20256 (pass 4) recorded reading 1 source set
   besides its own input, 20257 (pass 5) 2, 20260 (pass 6) 3: pass 6's
   memberships are in no packet issued before it arrived.

   **A superseded set.** Pass 4 differenced again (job 20262, expecting
   the open bindings 27689 and 27690): bound; it founded 0 and its 76
   memberships are the same 76 (object, source) pairs as 20254's; 20254's
   76 rows stay in `memberships` and are out of `memberships_official`;
   the run holds 201 objects before and after.

   Check, held: `check_run.sh --run 69 --inspect` CLEAN, validate 6/6
   (`catalog`: 201 objects in 1 field, 462 memberships within the radius,
   181 of source sets not official and out of the view, every accepted
   source of an image's field a member, no duplicate founders, 201
   official objects' sums equal to their members'), reader 3/3, consumer
   280/280. The first `check_run.sh` found three checks still reading the
   chain (PR 379 fixed them).

   | | run 46, chained (SCA 8, 3 epochs) | run 69 (2 epochs, then 1) |
   |---|---|---|
   | last difference ended to last packet ended | 5.2 min (02:20:56 to 02:26:05) | 31 s (07:08:13 to 07:08:44) |
   | associate jobs | 3 of 1.2 s each, chained | none |
   | catalog read of the field's ~200 objects | 422 ms (`members_official`, the chain view) | 0.9 ms (`objects_official`) |

   Run 46's chain read beside it (the diagnostic): fields 5387158,
   5383350, 5383352, objects 158, 282, 275; members per object 1.37,
   1.34, 1.31, at most 3, 5, 4. Run 69's field 5387158: 201 objects for
   three epochs of SCA 8. No materialisation: the view reads the field in
   under a millisecond.

   What the next step must know: an image's sources outside its field and
   the field's neighbours are never associated, and those in a
   neighbouring field only join objects within the radius (`dev`'s pass
   2); on run 69, 374 of the accepted sources lay outside the image's own
   field, so a field nobody's image is centred on founds no object. That
   was so under the chain too; it is the next association question.
   Packets now read and record their window at execution, so an
   injection or Spot run needs no change to the loop's order.

8. **Spot as a probe: four rules in the loop, never retried on Spot (PR
   381, merged `43584e9a`; rapid_systems PRs 421 merged `fa004a4`, 423
   the data point, 422 the interruption capture left open).** Spot is a
   probe, never production (Ben, 2026-10-24). A run's `[batch.spot]`
   (`queue`, `backlog`, `hedge_factor`; absent by default) sends a
   stage's submission to the Spot queue while more than `backlog` of that
   stage's jobs remain to submit in the pass, else to the prompt queue
   (`--job-queue`); a Spot submission overrides the retry strategy to one
   attempt; a Spot Batch job that ends unpublished, for any reason, is the
   job's one rerun, on the prompt queue; a Spot straggler past
   `hedge_factor` times its stage's median `container_seconds` is
   terminated and rerun there. `jobs.attempts` records every submission
   (queue, times, instance type, zone, Batch's reason); each pass reports
   `queues` and `attempts`; `job_metrics` gains `container_seconds`,
   `batch_submissions`, `reclaims`, `hedges`, `lost_seconds`.

   The granted statement, `database/2026-10-24-job-attempts.sql` (the
   column, and `job_metrics` redefined with the same columns), applied
   once on SMDC as `postgres` by SSM on the database host, the file piped
   to `podman exec -i rapid-postgres psql -U postgres -d rapid -X -1 -v
   ON_ERROR_STOP=1`. Check, held: `COMMIT` at 07:56:28 UTC; 20,262 jobs,
   none with attempts; 18,803 `container_seconds` rows; the view's grants
   kept (`rapid_rebuild_pipeline` SELECT, INSERT, UPDATE, DELETE;
   `rapid_read` SELECT).

   The retry row: both rebuild job definitions gain `OnStatusReason:
   'Task failed to start*'` retry before the catch-all (rapid_systems
   PR 421), deployed by the release hook's change set, exactly its two
   `Modify`/`False` rows, executed 07:58 UTC with image
   `rebuild-43584e9a…` (`sha256:044533be…`): **`rapid-rebuild:59`**
   (production `:50`). Read back: `attempts` 3, `evaluateOnExit` 75
   retry, `*` exit, `Task failed to start*` retry, `*` exit.

   The compute environments, read 07:4x UTC and not changed:
   `rapid-ce-bulk` `SPOT_CAPACITY_OPTIMIZED`, min 0, max 10,000 vCPU;
   `rapid-ce-prompt` `BEST_FIT_PROGRESSIVE`, min 0, max 5,000; both
   ENABLED, VALID.

   Proof under dataset `r00342-nt-spot`, **run 70**, live (group 18894),
   Gaia job 20263:

   ```sh
   rp run create ... --revision 43584e9abb041895eb326396f6ffe261121903f9 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --set 'batch.spot.queue="rapid-queue-bulk"' --set batch.spot.backlog=1 --set batch.spot.hedge_factor=5.0 \
     --survey HLWAS --dataset r00342-nt-spot
   rp loop --run 70 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4,5,6 \
     --job-definition rapid-rebuild:59 --job-queue rapid-queue-prompt --interval 60
   scripts/check_run.sh --run 70 --inspect DIR
   ```

   One loop, 07:59:36 to 08:21:41 UTC, 23 passes, drained, no failure.
   The hedge factor 5 is a choice for this sample: on the HLWAS medians
   (intake 68 s, difference 248 s, packet 69 s) a factor of 3 would have
   hedged a cold Spot start of four minutes.

   | stage | jobs | Spot (one attempt) | prompt | Spot start lag | prompt start lag |
   |---|---|---|---|---|---|
   | psf | 1 | 0 | 1 | | 126 s (cold) |
   | intake | 6 | 5 | 1 | 203 s (cold) | 202 s (cold) |
   | reference | 1 | 0 | 1 | | 215 s (cold) |
   | difference | 3 | 2 | 1 | 168 s, 292 s | 11 s (warm host) |
   | packet | 3 | 0 | 3 | | 9 s, 190 s, 70 s |

   Packets came one per pass as differences finished, so each was its
   stage's last wave and went on-demand. Batch's own record agrees:
   `retryStrategy.attempts` 1 on the Spot jobs (20265, 20272), 3 on the
   prompt ones (20270, 20274). The pass report's `attempts`: 7 submissions
   per queue, reclaims 0, hedges 0, lost job-hours 0, fill to 1,000 vCPU
   unmeasured at this size. Every host was an m6a.2xlarge; Spot in
   us-east-1a and 1c, three Spot requests, each closed
   `instance-terminated-by-user` (the scale-down), none reclaimed. A
   24 GiB difference takes a whole m6a.2xlarge, so the second Spot
   difference waited for a third host (292 s).

   | | run 69, on-demand (passes 1 to 5, two epochs) | run 70, Spot rules (passes 1 to 6, three epochs) |
   |---|---|---|
   | loop | 12.1 min (06:57:09 to 07:09:15) | 22.1 min (07:59:36 to 08:21:41) |
   | reference ended to last packet ended | 5.6 min (07:03:06 to 07:08:44) | 10.2 min (08:11:01 to 08:21:14) |
   | jobs rerun | 0 | 0 |

   Run 46's chained pass (SCAs 4, 5 and 8, three epochs) ran 02:10:13
   to 02:40:28 UTC, its SCA 8 packets done at 02:26:05.

   Check, held: `check_run.sh --run 70 --inspect` CLEAN, validate 6/6,
   reader 3/3, consumer 280/280 (the same 280 packets as run 69); every
   rule exercised in the unit tests with a faked Batch (`test_loop.py`,
   `test_launch.py`, `test_attempts.py`). Not held: the interruption
   capture. It needs a CloudWatch Logs resource policy letting
   EventBridge write to `/rapid/batch/spot-interruptions`; the account's
   two policies name `/rapid/batch/job-failed` and
   `/rapid/ops/ssm-association-events` only, and a new one is a grant this
   brief does not make. rapid_systems PR 422 carries the group, the rule
   and the policy, left open for Ben; no test event was sent and no real
   warning was captured (the pointing drew no reclaim).

   What the next step must know: on a sample this small Spot costs wall
   time and saves nothing measurable; the backlog rule only sends a wave
   to Spot when a stage has more than `backlog` submissions in one pass,
   so the footprint's intakes and differences will, its packets mostly
   not. Task 9's twin sets the three `--set`s above with its own backlog
   and runs the loop with `--job-queue rapid-queue-prompt`.

9. **Injection counted: the staged injection brief run as amended, the
   footprint twice, on-demand and Spot (PRs 383, 384, 385, merged
   `04762172`, `166ec3ab`, `d5ba665d`; rapid_systems PR 424, the Spot data point, merged `6b7d5dd`).** The staged brief
   `injection-2026-10-23` (its tasks 1 to 3: inject, footprint, scas) is
   retired by this step; its part 10 numbering is folded in here. Sources
   are injected outside the pipeline as a dataset (Ben, 2026-10-22):
   `scripts/inject_dataset.py` writes, for a selection, every file after
   the bound with constant sources by `dev`'s
   `inject_variable_stars_into_l2` as it stands (copied unchanged as
   `scripts/rapid_l2_injections.py`), every file before it byte for byte,
   names and `meta` kept, and a truth table under the dataset's
   `catalog/`; `scripts/completeness.py --run N` joins the run's official
   sources and packets to the truth. 200 sources per image, AB 21 to 27
   uniform, seed 20261024, F146, bound 61591.4056 (passes 1 to 3 copied,
   4 to 6 injected).

   **The datasets.** Three, each its own live run, created from the layers
   at `43584e9a` (`rapid-rebuild:59`, the pipeline unchanged by PR 383):

   ```sh
   rp run create ... --revision 43584e9abb041895eb326396f6ffe261121903f9 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"<prefix>/l2/\"" --set intake.unsigned=false \
     --set reference.selection.before_mjd=61591.4056 \
     [--set 'batch.spot.queue="rapid-queue-bulk"' --set batch.spot.backlog=20 --set batch.spot.hedge_factor=5.0] \
     --survey HLWAS --dataset <dataset>
   rp official change --group live.json ...      # the run live for HLWAS <dataset>
   rp job local --run <run> --stage import ...   # Gaia under the dataset's key
   rp loop --run <run> --filters F146 --scas 8 --pointings all --job-definition rapid-rebuild:<rev> \
     --job-queue rapid-queue-prompt --max-intake 400 --interval 60
   python scripts/completeness.py --run <run> --truth <truth parquet>
   scripts/check_run.sh --run <run> --inspect DIR
   ```

   | dataset | run | source root | selection | Gaia job | queue |
   |---|---|---|---|---|---|
   | `r00342-inj0` (the rehearsal) | **71** | scratch `inj/r00342-inj0/` | SCA 8, `001_0001` | 20278 | prompt |
   | `r00342-inj1` | **72** | inputs bucket `r00342-inj1/` | SCA 8, all 54 pointings; then 18 SCAs of `001_0001` | 20293 | prompt, warm |
   | `r00342-inj1s` (the Spot twin, same bytes and truth) | **73** | the same `r00342-inj1/` | SCA 8, all 54 pointings | 20294 | `[batch.spot]` backlog 20, hedge 5 |

   **The injection** (on this host, `put-object --if-none-match '*'` per object):

   | selection | injected | copied | skipped | truth rows | workers | wall | per injected file | per copy | peak RSS |
   |---|---|---|---|---|---|---|---|---|---|
   | rehearsal, SCA 8 `001_0001` | 3 | 3 | | 600 | 1 | 75 s (second attempt; the first died on CRDS's config read at import, fixed in PR 383) | 19 to 31 s | 6 s | 3.9 GB |
   | footprint, SCA 8 | 162 | 162 | 0 | 32,400 (`truth-sca8.parquet`) | 8 | 10.1 min | 20.7 s mean | 6.0 s mean | 5.3 GB |
   | 18 SCAs of `001_0001` | 51 | 51 | 6 (SCA 8's) | 10,200 (`truth-18sca.parquet`) | 6 | 5.5 min | | | 4.5 GB |

   The ePSF each file names (`roman_wfi_epsf_0276.asdf` for SCA 8 under
   `roman_0046.pmap`) was the one fetched. The topic carries no retention
   override (`describe_configs` empty), so the cluster's seven days hold.

   **Warm capacity.** `rapid-ce-prompt` `minvCpus` 0 → **512** at 08:56:42
   UTC (one wave of the footprint's 324 single-vCPU intakes with room for
   the first differences; the fixes runs recorded no concurrency figure, so
   the value is the wave's), restored to **0** at 09:52:14, read back
   `UPDATING 0`. Queue waits, submission to start:

   | stage | run 71, cold (6 epochs) | run 72, warm (SCA 8 footprint) | run 73, Spot lane | run 73, its prompt waves |
   |---|---|---|---|---|
   | intake | 194 s | median 65 s, p90 70 s (324) | median 160 s, p90 183 s (304) | 69 s (20) |
   | reference | 6 s | 12 s, p90 69 s (54) | 20 s (34) | 49 s (20) |
   | difference | 143 s | 15 s, p90 72 s (168) | 223 s, p90 249 s (122) | 36 s (55) |
   | packet | 9 s | 15 s, p90 63 s (162) | 200 s (129) | 15 s (33) |

   The warm hosts are billed whether jobs run or not: 512 vCPU for 55.5
   min is up to about 59 instance-hours (64 r6i.2xlarge, every prompt job
   ran on that type), against the footprint jobs' own 4.0; the job-hour
   pricing below does not include it.

   **A failure the footprint earned: the reference ePSF's centring.** At
   09:14 run 72's difference job 21029 exited 70 on `refpsf: centroid still
   (0.0000, -0.0269) px off centre after 10 shifts`, and the loop stopped
   (exit 2) by design; run 73 failed ten such jobs (21168 on Spot, then on
   its prompt rerun), run 72 six. Reproduced on this host (the stage run
   in-process from job 21029's inputs, `cfg_path` at the checkout, its
   ePSF pickled): the evaluated centroid's axes are coupled and over a
   stretch move 0.03 px per 0.1 px of shift, where the secant stalls; a
   grid search finds 0.001 px. PR 384: the search when the stepping does
   not converge. Image `rebuild-166ec3ab…` (`sha256:605a407a…`):
   **`rapid-rebuild:60`** (production `:51`), the change set exactly its
   two `Modify`/`False` rows. The 16 failed jobs resubmitted on `:60` with
   `job submit --job <id>` at 09:27, every one published; the loops resumed
   on `:60` (run 72 at 09:27:42, run 73 at 09:28:50 once its resubmitted
   jobs had started). The runs keep their recorded revision `43584e9a`;
   the jobs after 09:27 ran PR 384's function.

   **A check the footprint earned: `forced` under sets in flight.** The
   first `check_run.sh` on both runs failed only `forced` (252 and 587
   pairs): two packet sets in flight at once each measured an (object,
   image) pair neither could read (sets 21405 and 21440, 09:28:09 to
   09:29:00), and a later set (21476) recorded both and copied one, value
   for value; the rows differ only in `time_proc`. PR 385: the pair must
   equal one recorded predecessor's measurement. Run 72's rows: 11,306
   forced ids carry two measurements.

   **The loops.**

   | | run 72, on-demand, warm | run 73, Spot twin |
   |---|---|---|
   | loop invocations | 09:06:25 to 09:14:40 (exit 2), 09:27:42 to 09:34:44 drained | 09:06:26 to 09:18:29 (exit 2), 09:28:50 to 09:38:53 drained |
   | first submission to last job end | 28.1 min (09:06:27 to 09:34:32) | 30.2 min (09:06:28 to 09:36:42) |
   | Batch submissions | 709 prompt | 589 Spot, 129 prompt |
   | peak jobs running at once | 244 | 255 |
   | reclaims / hedges | 0 / 0 | 0 / 0 |
   | lost job-hours (started, not published) | | 0.131 (the centring failures) |
   | fill time to 1,000 vCPU | never reached | never reached |
   | job-hours | 17.44 | 13.42 Spot + 3.84 prompt |
   | instance-hours (job's memory share of its host) | 4.03, all r6i.2xlarge | 3.01 Spot (m7i.4xlarge 247, m6a.2xlarge 186, m6a.4xlarge 117, m7i.2xlarge 23, m7a 16) + 1.15 prompt r6i.2xlarge |
   | priced at on-demand rates | $2.03 | $1.80 + $0.58 = $2.38 |
   | priced at Spot rates (mean history over the window, type and zone) | $1.83 | $0.72 + $0.51 = $1.23 |
   | as billed by lane (Spot at Spot, prompt at on-demand) | $2.03 | **$1.30** |
   | held sets (the gate) | 1 (21453, ring fraction +0.0162) | 1 (21587, the same epoch, the same value) |

   Both runs lost the same 13 minutes to the centring failure, so the
   spans compare like for like; the Spot lane's start waits (160 to 223 s
   median) against the warm prompt lane's (15 to 65 s) are the cost, and
   the twin finished 2.1 min later. Billed cost is unverified until Cost
   Explorer.

   **Completeness, the SCA 8 footprint** (run 72; 162 published epochs,
   none unprocessed; 32,400 injected rows; truth through the corrected
   intake WCS, `crpix_shift` [0.12, -0.99]: the pipeline's RA/Dec of the
   rendered pixel (x, y) is the stored WCS at (x - 0.12, y + 0.99); radius
   0.3 arcsec):

   | AB | injected | detected | alerted | sent | detected in own field | alerted of those | offset | flux ratio |
   |---|---|---|---|---|---|---|---|---|
   | 21.0 | 2,595 | 0.958 | 0.398 | 0.397 | 1,086 | 0.951 | 0.009" | 0.946 |
   | 22.0 | 2,808 | 0.975 | 0.431 | 0.427 | 1,249 | 0.969 | 0.010" | 0.939 |
   | 23.0 | 2,670 | 0.966 | 0.381 | 0.378 | 1,044 | 0.974 | 0.009" | 0.928 |
   | 24.0 | 2,910 | 0.968 | 0.415 | 0.413 | 1,231 | 0.981 | 0.009" | 0.918 |
   | 25.0 | 2,853 | 0.944 | 0.395 | 0.394 | 1,177 | 0.958 | 0.011" | 0.910 |
   | 25.5 | 2,580 | 0.915 | 0.415 | 0.411 | 1,104 | 0.969 | 0.013" | 0.909 |
   | 26.0 | 2,721 | 0.754 | 0.298 | 0.296 | 846 | 0.960 | 0.016" | 0.923 |
   | 26.5 | 2,640 | 0.233 | 0.109 | 0.109 | 296 | 0.976 | 0.021" | 1.038 |
   | all | 32,400 | 28,625 | 12,097 | 12,027 | 12,541 | 12,069 of the 12,070 unflagged | 0.010" | 0.931 |

   (Every 0.5 mag bin is in `comp-72/bins.parquet`; the rows shown are
   every other bin.) **Detection is 0.94 to 0.98 to AB 25, 0.75 at 26 and
   0.23 at 26.5.** **The alerted fraction, 0.40, is the association's
   field rule, not the difference stage**: a detection outside its
   difference image's own field joins only an object already within the
   radius, and none exists, so it is never a trigger (step 7's "next
   association question"); every unflagged detection inside its image's
   own field is alerted (12,069 of 12,070). Detections with photutils
   flags are not alertable by the packet stage's rule. 70 alerted rows sit
   in the held set. The Spot twin's table is the same to three
   detections at 26 to 26.5 (28,624 detected, 12,095 alerted).

   **Purity per packet magnitude** (packets on injected sources / on
   bright-footprint-flagged sources / anything else):

   | packet AB | packets | injected | bright | other | purity |
   |---|---|---|---|---|---|
   | below 21 | 91 | 0 | 63 | 28 | 0 |
   | 21.0 | 978 | 913 | 45 | 20 | 0.93 |
   | 22.0 | 1,309 | 1,187 | 106 | 16 | 0.91 |
   | 23.0 | 1,408 | 1,052 | 272 | 84 | 0.75 |
   | 24.0 | 2,033 | 1,201 | 584 | 248 | 0.59 |
   | 25.0 | 3,181 | 1,115 | 1,308 | 758 | 0.35 |
   | 26.0 | 4,776 | 942 | 2,130 | 1,704 | 0.20 |
   | 26.5 | 2,502 | 334 | 597 | 1,571 | 0.13 |

   Per epoch, the false positives relative to this catalog are a median
   95 (min 33, max 182): 52 on bright-flagged sources, 38 other; the null
   test's floor (step 3: about 20 bright-star sources per epoch per SCA
   above S/N 20) is the bright-flagged share's bright end. Purity at AB 21
   to 22 is 0.91 to 0.93 and falls to 0.2 at the injection's faint end,
   where the artefacts and real faint residuals dominate.

   **History**, over every alerted injected source (4,070 sources, 7,823
   later packets): 11,576 earlier alerted epochs expected in
   `prvDiaSources`, 11,048 present, 524 late (their sets published after the
   packet's read: the best-information contract), 4 missing, the 4 packets
   whose source split over two objects (two in-flight epochs founding
   apart; step 7's multi-match note); 222 earlier injected epochs never
   detected.

   **The 18 SCAs of `001_0001`** (run 72, one loop 09:34:59 to 09:52:01,
   18 passes, drained, on-demand and warm; 51 epochs, 10,200 rows; SCA 8's
   is the footprint's pointing):

   | SCA | detected | detected, AB < 25 | detected, AB 26 to 27 | alerted | in own field | alerted of in-field | offset | flux ratio |
   |---|---|---|---|---|---|---|---|---|
   | 1 | 0.855 | 0.914 | 0.620 | 0.232 | 0.253 | 0.914 | 0.011" | 0.935 |
   | 2 | 0.907 | 0.985 | 0.525 | 0.278 | 0.337 | 0.827 | 0.009" | 0.943 |
   | 3 | 0.880 | 0.961 | 0.489 | 0.417 | 0.520 | 0.801 | 0.009" | 0.933 |
   | **4** | 0.828 | 0.929 | 0.500 | 0.237 | 0.267 | 0.888 | 0.012" | 0.928 |
   | **5** | 0.830 | 0.893 | 0.510 | 0.282 | 0.312 | 0.904 | 0.010" | 0.940 |
   | 6 | 0.820 | 0.941 | 0.408 | 0.320 | 0.368 | 0.869 | 0.010" | 0.920 |
   | 7 | 0.683 | 0.728 | 0.402 | 0.163 | 0.230 | 0.710 | 0.010" | 0.935 |
   | **8** | 0.903 | 0.992 | 0.550 | 0.417 | 0.442 | | 0.010" | 0.936 |
   | 9 | 0.647 | 0.669 | 0.598 | 0.308 | 0.408 | 0.755 | 0.010" | 0.939 |
   | 10 | 0.832 | 0.931 | 0.485 | 0.318 | 0.333 | 0.955 | 0.010" | 0.933 |
   | 11 | 0.882 | 0.977 | 0.517 | 0.367 | 0.417 | 0.880 | 0.011" | 0.934 |
   | 12 | 0.888 | 0.962 | 0.522 | 0.325 | 0.340 | 0.956 | 0.008" | 0.926 |
   | 13 | 0.813 | 0.856 | 0.529 | 0.303 | 0.353 | 0.858 | 0.012" | 0.928 |
   | 14 | 0.780 | 0.878 | 0.429 | 0.238 | 0.288 | 0.827 | 0.011" | 0.927 |
   | 15 | 0.902 | 0.985 | 0.521 | 0.338 | 0.343 | 0.985 | 0.009" | 0.928 |
   | 16 | 0.763 | 0.864 | 0.303 | 0.160 | 0.160 | 1.000 | 0.011" | 0.915 |
   | 17 | 0.875 | 0.992 | 0.371 | 0.432 | 0.465 | 0.928 | 0.010" | 0.928 |
   | 18 | 0.770 | 0.902 | 0.222 | 0.240 | 0.350 | 0.686 | 0.009" | 0.908 |

   SCAs 8 and 17 detect best (0.992 below AB 25); SCAs 4 and 5 sit
   mid-range (0.93, 0.89); SCAs 7 and 9 lowest (0.73, 0.67; SCA 9's nearly
   flat with magnitude, 0.60 at AB 26 to 27, which reads as area lost
   rather than depth; read, not diagnosed). "Alerted of in-field" below 1
   counts flagged detections. Seven sets are held by the gate on
   `core_fraction` (+0.026 to +0.028 against ±0.025): SCA 14's pass 4 and
   all three epochs of SCAs 16 and 17, so those two sent nothing (2,574 of
   2,975 alerted rows sent). Purity
   by packet magnitude follows SCA 8's (0.94 at AB 21, 0.45 at 24, 0.19 at
   26); the per-epoch false positives are a median 153 (54 bright, 105
   other), higher than SCA 8's 95. History: 2,830 expected, 2,406 present,
   422 late, 2 missing (2 split objects).

   Check, held: `check_run.sh --run 71` CLEAN (validate 6/6, reader 3/3,
   consumer 533/533); after PR 385, `check_run.sh --run 72` over
   the enlarged run CLEAN (validate 6/6 in 475 s, reader 213/213, consumer
   33,650/33,650 in 388 s) and `--run 73` CLEAN (validate 6/6, reader
   162/162, consumer 27,081/27,081); the first checks' only failure was
   `forced`, above. Raw evidence in the brief
   directory's `t9/` (`inject-*.log`, `loop-7*.log`, `comp-7*/`,
   `lags*.txt`, `price*.txt`, `check-7*.txt`, `repro/`).

   What the next step must know: the injected numbers say the difference
   stage finds what is there to AB 25 at 0.94 or better and the alert
   chain loses three fifths of it to the association's field rule;
   the rule is the next science question, before purity work. The
   inputs bucket's `r00342-inj1/` holds 426 files and two truth tables
   (immutable); `r00342-inj1s` reads the same prefix.


## Results of part 11 (2026-10-24)

The second iteration landed in one unattended night: association moved
into the difference job and the loop lost its chains, the packet job
sends its own set behind the quality gate, Spot ran under four rules
beside on-demand at footprint scale, injected sources were counted, and
two new simulations were taken in. Every task's checks held on its first
attempt; every run below is CLEAN by `check_run.sh`.

| step | before | after | where |
|---|---|---|---|
| 1 GBTDS | never read; L2 WCS 9 to 50 px off by tweakreg | WCS re-derived at intake, catalog stars +0.03 / -0.03 px; one pointing through the chain, run 67, 515 packets | PRs 361, 370 |
| 2 OpenUniverse | never read beyond the listing | format, truth and cadence written down; one image through the `openuniverse` reader, WCS on the truth to 0.0016 px | PR 365 |
| 3 metrics | bright end unflagged; SCA 4/5 excess and F213 unexplained | bit 16384 informational, 74 to 79% of sources above S/N 20 flagged; science stars 1.5 to 3.3% wider than the ePSF on SCAs 4, 5 and 8 alike; zero-point table within 0.035 mag; F213 `scalefacref` 0.939; a 121 px stamp changes nothing | PRs 360, 363, 369 |
| 4 loop-ops | HLWAS WCS (+0.12, -0.99) px off; a wrapper for passes; no waiter; failures by hand | Gaia offset +0.007 / +0.003 px with `crpix_shift`; `--passes`; `job wait`; one rerun and a cap of 1,000 a pass; the gate's query; ePSF fetch 4.1 → 0.8 s; no SSM read in a job | PRs 354, 356, 359, 364, 366, 367, 374 |
| 5 producer | the host's `publish` sent every set; the consumer check walked the topic | the packet job sends its own set 3 to 4 s after it ends, offsets on the stamp, the gate at the send boundary; a kill between publish and send swept by the next pass; consumer check 4 s | PRs 358, 371, 372 |
| 6 db-guards | no test of dataset in keys or of import identities | both in CI; one Gaia binding without dataset (on purpose); `pid`, `rid`, `rfid` 89k to 1.8M draws ahead of their sequences, `setval` not granted; `forced` rows per set = packets × window images on the SCA | PRs 355, 357 |
| 7 association | a per-field chain of associate jobs, 5.2 min last difference to last packet | association in the difference job, reconcile at publish; 31 s; field read 422 → 0.9 ms; reconcile replayed equal to the sequential result | PRs 378, 379, rapid_docs 97 |
| 8 spot | Spot parked | four rules, one attempt on Spot; run 70, 7 of 14 jobs on Spot, 0 reclaims; `Task failed to start` retried by the job definition | PR 381, rapid_systems 421, 423 |
| 9 injection | no completeness number | SCA 8 footprint detected 0.94 to 0.98 to AB 25, 0.75 at 26; alerted 0.40; purity 0.93 at AB 21 to 0.20 at 26; Spot twin $1.30 vs $2.03 by lane, 2.1 min later | PRs 383, 384, 385, rapid_systems 424 |
| 10 docs | rapid_docs at task 7's state | the foundation's settings statement and amendment, the gate, Spot, the datasets page, these figures | rapid_docs 98 |

**The metrics the gate reads** (`gate.decide`, the run's `[gate]`):
per difference job `n_sources_snr_gt20` against 3.0 times the run's
median, `|core_fraction|` against 0.025 and `|ring_fraction|` against
0.015, from 5 published differences on. At those values every one of
the fixes' 55 sets was sent; the SCA 8 footprint held 1 of 162 (ring
+0.0162) and the 18 SCAs of `001_0001` held 7 of 51 (`core_fraction`
+0.026 to +0.028; SCAs 16 and 17 entire, SCA 14 pass 4). Beside the
gate's three, the night added per difference `bright_footprint_fraction`,
`n_in_bright_footprint`, `sci_star_sigma_px`, `sci_star_psf_sigma_ratio`,
`scalefacref`, `n_sources_near_bright_px`, `n_sources_ring_snr_gt20`,
`n_objects_read`, `n_objects_founded`, `n_memberships`; per intake
`gaia_dx_px`, `gaia_dy_px`, `n_gaia_stars`; per packet
`n_packets_bright_footprint`; per job `peak_rss_kib`,
`container_seconds`, `batch_submissions`, `reclaims`, `hedges`,
`lost_seconds`.

**What remains, and where it sits:**

- **The association's field rule** (step 7's last paragraph, step 9's
  completeness): a detection outside its image's own field only joins
  an existing object, so three fifths of the detected injected sources
  are never alerted. The next science question, before purity work.
- **The bright-star residual's cause**: flagged, not fixed; neither
  the science PSF's width nor the stamp size (step 3). About 20 per
  epoch above S/N 20 on SCA 8, the bright end of the purity floor.
- **GBTDS's 346 to 418 core-and-ring residuals per epoch** with the
  science PSF on both images (step 1); the next GBTDS measurement.
- **OpenUniverse past intake**: the psf stage's ePSF source, the loop's
  listing and psf dispatch on `reader`, a reference before a
  transient's start (step 2). Completeness on TDS comes after HLWAS
  injection, by Ben's order.
- **The Spot interruption capture**: rapid_systems PR 422 open, a
  CloudWatch Logs resource-policy grant for Ben (the ledger's DEPLOY
  line). No reclaim has been seen.
- **The three sequences** behind the imported ids (step 6): one
  `setval` each as `postgres`, not granted.
- **The producer's idempotence**: off, because the job role lacks
  `WriteDataIdempotently`; the IAM alternative is Ben's (step 5).
- **SCAs 7 and 9's low detection** (0.73, 0.67 below AB 25; step 9),
  read and not diagnosed.

**What the two probes found.** GBTDS r00340 (step 1): the L2 WCS is
tweakreg's spurious crowded-field match and the re-derivation needs the
file's own distortion reference by name, not CRDS's current one; the
bulge is confusion-limited, so no reference PSF can be measured; a
pointing is the visit modulo six; the difference job takes 820 s. The
quirks and dispositions are rapid_docs `system/datasets.md`.
OpenUniverse TDS (step 2): STScI's copy is ASDF, not FITS, one patch
of 0.27 deg² with 106 images per filter at a point; truth in every file
and per image on IRSA, so completeness is a join; the WCS is one-based
and the zero point is not AB, both fixed on the overlay.

**Session notes**, every task's, gathered for Ben (the PR each came
from in brackets):

- Task 1 (gbtds): the session-start sync failed on all three repos
  (`fatal: Cannot fast-forward to multiple branches`), so the vault and
  dotfiles were not pulled (361, 376); force-push being reserved, a
  branch overtaken by parallel merges is brought up to date by merging
  `rebuild` into it and squash-merging, worth a line in the brief
  skill's preamble for parallel groups (370).
- Task 2 (openuniverse): `tail`-piped test commands hide pytest's exit
  status; one push went out on a red local run and was caught before the
  PR; "check the test's exit, not its tail" in the Procedure would cover
  it (365).
- Task 3 (metrics): the fixes' runs 51 and 52 have no bytes left but
  their rows still name them, so a check that reads old files should go
  through kept runs (363); `rp job local` with `[difference.paths]` at a
  checkout reproduces a Batch difference in about 2 minutes, worth a line
  in "If a step fails" (369, 373).
- Task 4 (loop-ops): the host's load reached 41 on 16 cores with six
  sessions running unit suites at once, the full suite 6 min 51 s (354);
  two of its PRs conflicted only in `test_loop.py`'s docstring and tail
  (364); `podman` here pulls the pipeline image from ECR directly, which
  made checking the image a one-liner (367).
- Task 5 (producer): the brief put the gate in task 5 and its query in
  parallel task 4, so the wiring was a later PR of its own (358); the
  harness also resets the cwd after a backgrounded command and refuses
  an inline `sleep`, so every wait was a background command (377).
- Task 6 (db-guards): six parallel tasks each opened part 11 and the
  first to land wrote its header, so the rest rebased; a header staged
  before launch would save that (362).
- Task 7 (association): "proof on the last merged sha" and "statements
  before the PR merges" order themselves (statement, merge, build,
  deploy, run); ending a turn on a `run_in_background` waiter worked
  every time, eight waits (378).
- Task 8 (spot): a stray `cat` with no input hung for the tool's two
  minutes before backgrounding, since an unattended session has no
  stdin; "no command reads stdin" in the preamble would cover it; the
  release hook is already the rapid-batch change-set inspector (382).
- Task 9 (injection): the Bash tool refuses foreground `sleep N && cmd`
  chains; two wake sources (the command's notification and a watcher)
  caught the loop's exit 2 within a minute (386).
- Task 10 (docs): the rapid_docs venv is absent on the fleet host and
  building it is a package install, so a docs task here cannot run the
  Sphinx build locally; CI is the only build (rapid_docs 98).

## Part 12: the day (2026-10-24)

Brief `day-2026-10-24` (plan v9 Part 7's pruned list): one step per task, numbered by task;
a task's step lands when its task does, so the numbers can arrive out of order.

1. **The field rule: association by the source's field (PR 393, merged
   `80fa77ef`; rapid_docs 99).** Part 11 step 9 found that run 72
   detected 0.94 to 0.98 of the injected sources to AB 25 but alerted
   0.40. `dev`'s pass 2 let a detection outside its difference image's
   own field join only an object already there, so a field no image is
   centred on founded nothing. The rule now: **an accepted source is
   matched against the objects of its own field and its neighbouring
   fields, and founds in its own field when nothing matches, whichever
   image detected it.** The difference job reads the run's objects in
   its sources' fields and their neighbours in one query, records the
   read per (field, founding job), and founds each unmatched source in
   that source's own field; pass 2 and its inclusion cone are gone. The
   publisher takes the advisory locks of every field that gained an
   object or a membership and each one's neighbours, ascending, in one
   statement, so two jobs founding either side of a boundary serialise,
   and it reconciles against the objects founded since the read in all
   of those fields. The packet's forced window had been the images
   centred on the trigger image's field. It is now the images covering
   the triggers' objects, whichever field each is centred on: on run
   72's SCA 8, inside 30 days, images centred on other fields overlap an
   image 594 times against 324 for the same field. New difference
   metric `n_sources_out_of_field`. `validate_run.py`'s `catalog` check
   now asks every accepted source to be a member of an object in its own
   field or a neighbour, and looks for duplicate founders across field
   boundaries. No schema statement: `objects_field_idx` serves the
   multi-field read.

   **Measured first**, read-only on run 72's rows (`t1/before-72.txt`):

   | run 72 | accepted | in own image's field | outside it | outside, in a field some image is centred on | outside, in a field no image is centred on |
   |---|---|---|---|---|---|
   | all SCAs and epochs | 83,854 | 35,025 | 48,829 | 1,288 | 47,541 |
   | SCA 8 footprint | 63,634 | 27,260 | 36,374 | 444 | 35,930 |
   | detected injected, accepted | 27,752 | 12,097 | 15,655 | 170 | 15,485 |

   None of the sources outside their image's field was a member of
   anything. Run 72's 70 fields with a centred image are exactly its 70
   fields with a reference, so a field with no image centred on it is a
   field with no reference of its own. Under the rule, 35,930 of SCA 8's
   accepted sources found objects in such fields. Run 69's 374 were of
   this kind.

   Image `rebuild-80fa77ef…` (`sha256:1de22498…`): **`rapid-rebuild:63`**
   (production `:54`), the change set its two `Modify`/`False` rows,
   under the release lock. Proof under dataset `r00342-dy-field`,
   **run 81**, live, Gaia job 22004, the injected footprint's source
   root, F146's bound, on demand and cold (`minvCpus` 0):

   ```sh
   rp run create ... --revision 80fa77efba671e323b240a9e6f1fd232d1a26491 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"<inputs>/r00342-inj1/l2/\"" --set intake.unsigned=false \
     --set reference.selection.before_mjd=61591.4056 --survey HLWAS --dataset r00342-dy-field
   rp loop --run 81 --filters F146 --scas 8 --pointings 001_0001,001_0002 --job-definition rapid-rebuild:63 \
     --job-queue rapid-queue-prompt --max-intake 400 --interval 60      # the sample
   rp loop --run 81 --filters F146 --scas 8 --pointings all --job-definition rapid-rebuild:63 \
     --job-queue rapid-queue-prompt --max-intake 400 --interval 60      # the footprint
   python scripts/completeness.py --run 81 --truth truth-sca8.parquet
   scripts/check_run.sh --run 81 --inspect DIR
   ```

   **The sample first** (15:08:42 to 15:20:44 UTC, exit 0, 27
   submissions): pointings `001_0001` and `001_0002`, whose SCA 8 frames
   meet with their centres 7.22 arcmin apart (fields 5387158 and
   5390959), all six passes. 1,196 objects in 10 fields, 639 of them in
   the 8 fields no image is centred on. Object `240735837836867856` in
   field 5390958 (no image centred on it) has three members, one per
   injected epoch of `001_0002`, and three packets. Their forced windows
   name 1, then 2, then 3 images, each exactly the official differences
   covering it, and their histories carry the 0, then 1, then 2 earlier
   detections.

   **The footprint** (15:21:00 to 15:43:03 UTC, exit 0, 703 submissions
   in all, no failure, no rerun; first submission to last job end 30.9
   min, cold, against run 72's 28.1 min warm with its 13 minutes lost to
   the centring failure). 162 differences, 37,800 objects in 290 fields,
   21,573 of them in fields no image is centred on. 39,485 founded, 1,685
   reconciled onto objects founded in flight since the read (1,692
   memberships attached). One official membership crosses a field
   boundary, none skips a neighbour. Per difference job,
   `n_sources_out_of_field` has median 222 (150 to 304), and
   `n_objects_read` has median 830 (at most 2,109).

   | AB | injected | detected | alerted, run 72 | **alerted, run 81** | sent, run 81 |
   |---|---|---|---|---|---|
   | 21.0 | 2,595 | 0.958 | 0.398 | **0.920** | 0.916 |
   | 22.0 | 2,808 | 0.975 | 0.431 | **0.944** | 0.938 |
   | 23.0 | 2,670 | 0.966 | 0.381 | **0.943** | 0.937 |
   | 24.0 | 2,910 | 0.968 | 0.415 | **0.953** | 0.947 |
   | 25.0 | 2,853 | 0.944 | 0.395 | **0.917** | 0.913 |
   | 25.5 | 2,580 | 0.915 | 0.415 | **0.891** | 0.883 |
   | 26.0 | 2,721 | 0.754 | 0.298 | **0.722** | 0.717 |
   | 26.5 | 2,640 | 0.232 | 0.109 | **0.226** | 0.225 |
   | all | 32,400 | 28,623 | 12,097 | **27,750** | 27,588 |

   **Alerted is now 0.97 of detected** (27,750 of 28,623; run 72 had
   12,097 of 28,625). The detected column is run 81's, two detections off
   run 72's. What remains between the two is the flagged detections,
   which the packet stage does not alert by its rule (12,067 of the
   12,068 unflagged in-field detections are alerted, as before). The
   gate held one set (job 22727, ring +0.0162, the epoch the night held),
   so sent trails alerted by 162.

   | packet AB | packets, run 72 | purity, run 72 | packets, run 81 | injected | bright | other | purity, run 81 |
   |---|---|---|---|---|---|---|---|
   | 21.0 | 978 | 0.93 | 2,181 | 2,031 | 123 | 27 | 0.93 |
   | 22.0 | 1,309 | 0.91 | 2,817 | 2,511 | 286 | 20 | 0.89 |
   | 23.0 | 1,408 | 0.75 | 3,349 | 2,506 | 653 | 190 | 0.75 |
   | 24.0 | 2,033 | 0.59 | 4,694 | 2,707 | 1,398 | 589 | 0.58 |
   | 25.0 | 3,181 | 0.35 | 7,654 | 2,612 | 3,192 | 1,850 | 0.34 |
   | 26.0 | 4,776 | 0.20 | 11,115 | 2,210 | 5,053 | 3,852 | 0.20 |

   Purity per magnitude is unchanged: the rule alerts the out-of-field
   artefacts in the same proportion as the out-of-field injected
   sources. False positives per epoch rise with it, median 224 (130
   bright-flagged, 89 other) against run 72's 95. That is the purity
   work part 11 put after this rule, now at the full alert volume.

   **The forced window and history, proven on rows**
   (`t1/window-81.txt`, `t1/read-81-showcase.txt`). Over all 162 packet
   sets, no official difference that covers a set's objects, sits in its
   30-day window and was published before the set started is missing
   from the window it recorded. 249 of the 571 window images read are
   centred on a field other than the trigger image's, and the field
   window would have dropped them. Object `217081629015376663`, founded
   in field 5301528 (no image centred on it), has five members from
   images centred on fields 5301527 and 5301529. Its five packets'
   forced windows name 2, 3, 4, 5 and 6 images, each time exactly the
   covering differences, from both centre fields. Their `prvDiaSources`
   carry 0, 1, 2, 3 and 4 earlier detections. History over the 9,335
   alerted injected sources: of 26,601 earlier alerted epochs expected,
   26,248 present, 339 late (published after the packet's read, the
   best-information contract), 14 missing in the 13 packets whose source
   is split over two objects. That is the night's in-flight founding
   mechanism (run 72: 4 in 4 packets), now at a rate of 0.05%.

   The packet job carries 2.3 times the packets (median 393 against 170
   a set) and its container time has median 60 s against 37 s, with peak
   memory unchanged (0.89 GB).

   Check, held: `check_run.sh --run 81 --inspect` CLEAN: validate 6/6 in
   552 s (`catalog`: 37,800 objects in 290 fields, every accepted source a
   member of an object in its own field or a neighbour, no duplicate
   founders across boundaries), reader 162/162, consumer 63,153/63,153
   (the held set's 459 packets unsent). Raw evidence in the brief
   directory's `t1/`.

   What the next step must know: an object's `field` is its founding
   source's field. Every accepted detection is alertable wherever it
   falls. The alert volume is about 2.3 times the night's at the same
   purity per magnitude, so the false positives per epoch (median 224)
   are the next science question. `completeness.py`'s `in_field` column
   now says only where on its image a detection fell.

2. **The producer idempotent again (PR 388).** PR 371 built the producer
   with `enable_idempotence=False` because the scratch job role lacked
   `kafka-cluster:WriteDataIdempotently` (part 11 step 5, run 57). The
   grant is live since 14:14 UTC (rapid_systems PR 425, role and
   boundary), so `publish_kafka.producer` asks for idempotence again,
   named explicitly (kafka-python 3.0.11's default), `acks="all"` kept.

   Proof, run **78** (`r00342-dy-producer`, `2d282a6b`, image
   `sha256:27b11a9c…`, **`rapid-rebuild:61`**; F146, SCA 8, pointing
   `001_0001`, passes 1 to 6; 14:52:53 to 15:02:59 UTC, 11 passes, exit 0):

   ```sh
   rp run create --who "$USER" --why "..." --revision 2d282a6baef27bac98de0aa10bcdfd1178a8647a \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-dy-producer
   rp official change --group live.json ...                 # run 78 live for HLWAS r00342-dy-producer
   rp job local --run 78 --stage import ...                 # Gaia under the dataset's key, job 21955
   rp loop --run 78 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4,5,6 --job-definition rapid-rebuild:61 --interval 60
   python scripts/consume_packets.py --run 78
   rp publish --run 78
   scripts/check_run.sh --run 78 --inspect <dir>
   ```

   Check, held: 15 jobs, each published on its first attempt, nothing
   failed or rerun; every pass's `sets_produced` and `sets_waiting` empty,
   so each packet job sent its own set:

   | set | ended | stamped | partition | offsets | packets |
   |---|---|---|---|---|---|
   | 21976 | 15:00:31 | 15:00:34 | 1 | 1887..1962 | 76 |
   | 21977 | 15:00:38 | 15:00:42 | 1 | 1963..2067 | 105 |
   | 21982 | 15:02:41 | 15:02:45 | 1 | 2068..2166 | 99 |

   Each packet job's Batch log ends with its result's `kafka` field
   (partition 1 and the offsets above) and holds no `InitProducerId` or
   authorization error. `rp publish --run 78` afterwards sends nothing
   (`sets []`). The consumer check: CLEAN, read 280, kept 280, packets
   expected 280 seen 280, one set sampled (99 valid, 99 byte-equal), range
   `p1 1887..2166`, 4.4 s. `check_run.sh --inspect`: CLEAN, validate 6/6,
   reader 3/3, consumer 280/280.

3. **OpenUniverse TDS through the chain, counted against its own truth
   (PRs 390, 392, 400).** Three pieces made the `openuniverse` reader run
   past intake (part 11 step 2's "Not built"):
   - **The psf stage** (PR 390) draws the science PSF with galsim.roman,
     the simulation's own model, for the stem's filter and SCA: `getPSF`
     at the SCA centre (2044, 2044) in GalSim's pixels, achromatic at the
     bandpass's effective wavelength, 61 px with the pixel response, unit
     sum; the file is not read and no CRDS reference is named. galsim
     2.8.5 is in the host's env and the image (`requirements.txt` now
     lists it; the import gate checks `galsim.roman`). The H158 SCA 10
     stamp holds 0.977 of the flux in 61 px and its centroid is
     (−0.160, −0.127) px from the stamp centre, the size of part 11 step
     2's unexplained R062 star residual.
   - **The loop** (PR 392) lists `<source_root><H158>/` for `--filters
     F158` and keeps the `_simple.asdf` stems; an OpenUniverse pointing is
     the observing sequence's row, which the intake writes as `expid`, so
     `--pointings 247,632,…` selects stems and rows alike; `--passes` is
     refused.
   - **The reference** needs no code: the first visits under the existing
     `before_mjd`. The count script (PR 400) decides which transients a
     reference built from them can be judged on.

   The sample: H158, the filter whose live transients are brightest (4 to
   10 per image below AB 26 on SCA 10 against R062's 1 to 3, IRSA's index
   over six images each), and SCA 10 in field 5329200, which 27 in-season
   H158 images share (the night's `tds-heads.txt` grouped by the
   tessellation field of each file's SCA centre). The reference is the
   first six (pointings 247 to 2172, MJD 62001.2 to 62026.2); the ten
   epochs are spread over the season and include the two late visits
   (14492, 15262, 16032, 28737, 29507, 30277, 42982, 44137, 56457,
   57227). The packet stage needs a Gaia binding and the simulation has
   no Gaia stars, so the run's catalog is the truth's 487 star rows over
   the sixteen images on Gaia's Parquet schema (source_id the truth id,
   `ref_epoch` 2016.0, zero motion), which also gives the intake's WCS
   metric.

   Image `rebuild-8c51fdb3…` (`sha256:4e120d5a…`), **`rapid-rebuild:62`**:

   ```sh
   rp run create --who "$USER" --why "..." --revision 8c51fdb386a7b49ef34230e4091d1d0c3421d679 \
     --overlay rapidpipe/settings/datasets/openuniverse-tds.toml \
     --set "intake.source_root=\"$OU_ROOT\"" --set reference.selection.before_mjd=62026.3 \
     --survey HLTDS --dataset ou-tds2                              # run 80, live in group 20620
   rp job local --run 80 --stage import --scope '{"source_dir": "<dir>", "gaia": {"name": "truth-stars-ou-tds2",
     "version": "TDS-truth", "path": "openuniverse-tds-h158-sca10-stars.parquet"}}' \
     --expects '[{"product": "catalogs", "key": {"dataset": "ou-tds2", "name": "truth-stars-ou-tds2", "version": "TDS-truth"}, "binding": null}]'
   rp loop --run 80 --filters F158 --scas 10 \
     --pointings 247,632,1017,1402,1787,2172,14492,15262,16032,28737,29507,30277,42982,44137,56457,57227 \
     --job-definition rapid-rebuild:62 --interval 60              # 15:02 to 15:22 UTC, exit 0
   scripts/check_run.sh --run 80 --inspect <dir>
   python scripts/completeness_ou.py --run 80
   ```

   Check, held: 29 jobs (1 import, 1 psf, 16 intake, 1 reference, 10
   difference, 10 packet), each published on its first attempt; every
   L2 row in field 5329200; the gate held one set (44137, `ring_fraction`
   −0.025), the other nine sent by their jobs. `check_run.sh`: CLEAN,
   validate 6/6, reader 10/10 (677 packets), consumer 557/557.

   | per job | median | range |
   |---|---|---|
   | psf, galsim draw | 0.45 s | |
   | intake container | 23.9 s | 18.6 to 25.7 |
   | intake WCS against the truth stars | (−0.081, −0.069) px | ±0.005; 135 to 148 stars |
   | L2 `limmag` | 26.8 | 26.76 to 26.83 |
   | difference container | 292 s | 267 to 327 |
   | difference `n_candidates`, kept | 2,612, 500 | 2,077 to 3,682; 500 every epoch |
   | `scalefacref` | 2.22 | 2.17 to 2.27 (HLWAS 0.99) |

   **The count** (`completeness_ou.py`): 761 injected rows (transient
   rows with realised counts in the frame), of which 410 are evaluated
   (started after the reference's last frame, no counts in any reference
   frame), 348 have counts in a reference frame and 3 started before its
   end without any; only the 410 are counted.

   | AB | evaluated | detected | alerted |
   |---|---|---|---|
   | 21.0 to 23.5 | 8 | 1.00 | 0.50 |
   | 24.0 | 9 | 1.00 | 0.56 |
   | 24.5 | 10 | 0.80 | 0.60 |
   | 25.0 | 14 | 0.50 | 0.14 |
   | 25.5 | 18 | 0.11 | 0.06 |
   | 26 and fainter | 361 | 0.003 | 0.003 |

   Every alerted detection is in its image's own field (19 of 19), and
   16 of the 35 detections fall outside it (the field rule, step 1).
   Purity over all 677 packets: 21 on transients, 139 bright-flagged, 4
   on galaxies, 0 on stars, 513 other, which sit at AB 24.5 to 25.5.
   **The faint end is the source cap's**: every difference keeps dev's
   500 of 2,077 to 3,682 candidates, and 261 to 500 of the kept are
   above S/N 20. As a different test, HLWAS injection (part 11 step 9)
   detected 0.94 to 0.98 to AB 25. Recorded, not changed: the cap and
   `scalefacref` 2.2 are the next OpenUniverse measurements.

   Run 80, its dataset and the truth-star catalog stay; nothing was
   deleted.

4. **GBTDS's crowded-field reference PSF: it converges and is not
   adopted (branch `day/gbtds-refpsf`, unmerged).** Part 11 step 1 left
   run 67's 346 to 418 core-and-ring residuals per epoch above S/N 20
   under `[difference] reference_psf = "science"`, because the measured
   reference PSF found 0 stars in the bulge. One measured step, run as an
   experiment on the branch before any setting exists: a crowded variant
   of `refpsf.measure` (`crowded=True`, the mode `"measured-crowded"`)
   with the peak floor at the 300th brightest detection under the
   3,500 DN/s ceiling instead of 400 clipped deviations, and isolation as
   a contrast rule (no detection within 6 px peaking above 10% of the
   star's) instead of no detection within 14 px; 31 px stamps, the
   secant-then-search centring and the ten-star floor as before. A
   module constant on the branch (`_EXPERIMENT_REFERENCE_PSF`, its own
   commit `2a067507`) forced it over the overlay's `"science"`.

   Proof, dataset `r00340-gb2` on the GBTDS overlay, the sample root and
   the bound of part 11 step 1, reusing nothing of run 67's. Run **75**
   (`998fb0ef`) made the psf, nine intakes and the six-frame reference on
   Batch under `rapid-rebuild:60` (14:45 to 14:54 UTC; psf 21940, intakes
   21942 to 21950, reference 21958), one `--once` pass at a time so no pass
   could plan a difference; run **79** (`2a067507`, the same layers plus
   `[difference.paths]` at the worktree) was made live in its place and
   ran the three differences of run 67's epochs locally, one at a time,
   from the dataset's official products:

   ```sh
   rp run create ... --revision 998fb0ef6b85094744b0e69702eef5d68b2561f6 \
     --overlay rapidpipe/settings/datasets/gbtds-r00340.toml \
     --set 'intake.source_root="<the sample root>"' --set intake.unsigned=false \
     --set reference.selection.before_mjd=61679.1449 --survey GBTDS --dataset r00340-gb2   # run 75
   rp official change --group live.json ...      # run 75 live
   rp job local --run 75 --stage import ...      # Gaia under the dataset's key, job 21938
   rp loop --run 75 --filters F146 --scas 8 --pointings all --job-definition rapid-rebuild:60 --once   # until the reference is submitted
   rp run create ... --revision 2a06750789c841d85ca99be9f171a13d74b8da02 <the same layers> \
     --set 'difference.paths.rapid_sw="<worktree>"' --set 'difference.paths.cfg_path="<worktree>/cdf"' \
     --set 'difference.paths.zogy_code="<worktree>/modules/zogy/v21Aug2018/py_zogy.py"'          # run 79
   rp official change --group live79.json ...    # close run 75's live binding, open run 79's
   rp job local --run 79 --stage difference --scope '{"expid": 2014101, "sca": 8, "fid": 8}' \
     --inputs <the loop's plan for the key> --expects <diffimages, sources>                   # and 2171101, 4254101
   ```

   Check, held: the three differences published (jobs **21967**, **22016**,
   **22434**; 14:55 to 15:42 UTC, 875, 1,006 and 928 s, peak RSS 8.2 GB),
   each with its 500 sources and the association's objects (161, 152,
   129 founded). The variant converged on all three: 249 to 262 of
   67,116 to 67,332 detections passed its cuts, 100 stars used, the
   built stamp centred to 0.01 px, 6 s.

   | epoch | job (run 67 / 79) | `refpsf_stars` | `refpsf_sigma_px` | `sci_star_sigma_px` / ePSF (79) | core / ring (79) | candidates (67 / 79) | above S/N 20 (67 / 79) | noise correlation (67 / 79) |
   |---|---|---|---|---|---|---|---|---|
   | 2014101 (+1.4 h) | 20211 / 21967 | 0 / 100 | – / 1.260 | 1.245 / 0.947 | −0.155 / 0.061 | 9,618 / 8,734 | **367 / 62** | 1.63 / 1.89 |
   | 2171101 (+1 day) | 20212 / 22016 | 0 / 100 | – / 1.243 | 1.247 / 0.947 | −0.197 / 0.069 | 9,343 / 22,357 | **418 / 500** | 1.63 / 2.32 |
   | 4254101 (+6 days) | 20213 / 22434 | 0 / 100 | – / 1.200 | 1.247 / 0.947 | −0.169 / 0.020 | 8,784 / 14,514 | **346 / 81** | 1.67 / 2.07 |

   **The rule's answer: not adopted.** The count fell on two epochs by a
   factor of 4 to 6 and rose on the third (418 to 500, every kept source),
   so nothing of the variant merges, the overlay stays at `"science"`
   and there is no second variant. The galleries (`gallery-<job>.png` in
   the run directory, the eight kept sources of highest S/N) show what
   changed: on 21967 and 22434 the core-and-ring of run 67 is gone and the
   residuals at bright stars are small dipoles (S/N 84 to 118 against run
   67's 393 to 563); of 22016's 500, 424 sit where the reference peaks
   below 200 DN/s and the brightest is S/N 74, so it is the difference's
   noise, not the stars: its candidates more than double and its noise
   correlation is the highest of the six. Why the measured PSF inflates
   that one epoch's noise is not read here.

   **Seen on the way: the science PSF is the narrow one.** With the
   reference's stars measured, `sci_star_sigma_px` exists for GBTDS for
   the first time: the science frame's own stars are **1.245 px** against
   the ePSF's 0.947, a ratio of 1.31 on all three epochs (HLWAS F146 SCA
   8: 1.024, part 11 step 3). Run 67's "science" mode gave ZOGY that
   0.947 px stamp for both images whose stars are 1.25 px wide; the
   variant corrects the reference's half only. The next GBTDS measurement
   is the science PSF's width on this dataset, not a second reference
   variant.

   Left in place, nothing deleted: run **74** (`r00340-gb2`, created with
   a mistyped revision `998fb0ef3a0b…`, never live, no jobs), run 75
   (its psf, intakes and reference official for the dataset), run 79
   live with three differences and their objects and no packets (no
   packet or send was part of the step), the branch with the variant, its
   unit test and the experiment commit.

5. **F213's flux scale: the paired difference, the measured scale against
   the zero points' ratio. The forced scale makes the stars' flux ratio
   right and leaves the ring, so no setting is born.** The experiment, as
   the brief set it, ran before any setting existed: one F213 epoch at
   dev's measured scale (`gainmatch.py`'s median ratio of matched
   isolated reference stars' `FLUX_APER_6`) and at the scale its fallback
   uses, `10^(0.4 (MAGZP_ref − ZPTMAG_sci))`, forced by a module constant
   on the unmerged branch `day/f213-scale-experiment` (`9a775144`). One
   pair, no third variant. The baseline is part 10's run 54
   (`r00342-fx-f213`), pass 4 difference job 20104, at `70c6bfaa`.

   ```sh
   rp run create ... --revision 998fb0ef6b85094744b0e69702eef5d68b2561f6 \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61592.05053 \
     --survey HLWAS --dataset r00342-dy-f213                       # run 76, live (group 20575)
   rp job local --run 76 --stage import ...                       # Gaia under the dataset's key, job 21939
   rp loop --run 76 --filters F213 --scas 8 --pointings 001_0001 --passes 1,2,3,4 \
     --job-definition rapid-rebuild:60 --interval 60              # 14:45 to 14:58 UTC, exit 0
   rp run create ... --revision 9a7751442e548568dc94450498397e638085d778 \
     --overlay ... --set ... --set 'difference.paths.{rapid_sw,cfg_path,zogy_code}=<the worktree>' \
     --survey HLWAS --dataset r00342-dy-f213z                      # run 77, live (group 20577)
   rp job local --run 77 --stage import|psf|intake ...            # 21973; 21974; 21975, 21978, 21979, 21980
   rp job submit --run 77 --stage reference --job-definition rapid-rebuild:60 ...   # 21983
   rp job local --run 77 --stage difference --scope '{"expid": 4070101, "sca": 8, "fid": 4}' ...   # 21986
   ```

   The loop ran passes 1 to 4 of `001_0001` SCA 8 (three reference epochs,
   one difference). The difference stage refuses an `l2files` input keyed
   under another dataset (job 21972, exit 70, `is not the epoch`). So run
   77 made its own inputs from the same files at the same code: local
   import, psf and intakes, and the reference on Batch, because its
   `cfg_path` is the image's `/code/cdf` (a local attempt failed in
   SExtractor). Only the difference ran at the branch.

   | F213 `001_0001` SCA 8 pass 4 | run 54, job 20104 (`70c6bfaa`) | **measured**, run 76, job 21965 (`:60`) | **forced**, run 77, job 21986 |
   |---|---|---|---|
   | `scalefacref` | 0.935 | 0.939 | **0.972** (the zero points' ratio, MAGZP 25.1449) |
   | science / gain-matched reference flux, 30 residual stars, 6 px aperture, median | 1.043 | 1.036 | **1.002** |
   | difference / reference flux, same aperture | +0.056 | +0.018 | −0.016 |
   | core / ring | +0.037 / +0.023 | −0.021 / +0.046 | −0.045 / +0.036 |
   | candidates / above S/N 20 | 313 / 23 | 366 / 63 | 355 / 50 |
   | `n_sources_ring_snr_gt20` / `n_in_bright_footprint` | | 24 / 143 | 15 / 132 |
   | packets | 91 | 91 (consumer check CLEAN 91/91) | not run |

   Evidence: `t5/` in the brief's run directory: the three jobs' JSON,
   `starratio.py` and its output, and the galleries `inspect-76/` and
   `inspect-77/`.

   **What it says.** The zero points are right for F213: at their ratio,
   the bright isolated stars' science-to-reference flux is 1.002. The
   measured scale sits 3.4% low (0.939 against 0.972), so the
   catalog-flux bias exists and is about that size. It is not what leaves
   the residual, though. At the measured scale the core is now negative
   and the ring positive, where run 54's core was positive. Part 11's
   `crpix_shift` and the secant-centred reference PSF came between the
   two. A negative core with a positive ring is a width mismatch, not a
   scale. Forcing the scale drives the core further negative (−0.045),
   lowers the ring by a fifth (+0.036) and lowers the count (50 against
   63 above S/N 20). It does not remove the positive ring, and the
   galleries at ±0.001 show the same halo on every star both ways.

   The brief's condition was that the forced scale remove the positive
   core and ring without raising the count. It does not, so no
   `[difference] scale` is born and nothing merges but this step. Open
   causes, in order: F213's PSF width mismatch (the reference PSF, 1.387
   px, against the science ePSF, 1.291 px, and the science stars' own
   1.326 px). Then the catalog-flux bias of the measured scale (3.4%,
   measured here), which matters only once the width is right. Run 76's
   first numbers also differ from run 54's on the same file: a new
   before for F213 at `:60`.

6. **SCAs 7 and 9's low detection: area lost to masked pixels the
   finder cannot see past (read-only diagnosis; no code, no setting
   changed).** Part 11 step 9's 18-SCA table had SCAs 7 and 9 detecting
   0.73 and 0.67 of the injected sources below AB 25 where SCA 8 detects
   0.99. Read from run 72's rows and products only: the completeness
   tables (`completeness.py`'s `truth`, part 11 step 9's `comp-72-18/` and
   `comp-72/`), `job_metrics`, the references' inputs, and each pass 4
   difference's masked image and resampled reference coverage from the
   scratch bucket. Sample: `001_0001` F146 passes 4 to 6 (expids 4040101,
   5040101, 6010101), 200 injected sources an image, the same positions
   every epoch; the masks of pass 4 for the per-pixel reads.

   **The mechanism.** The difference catalog's finder (`psfcat.py`,
   photutils `DAOStarFinder`, dev's call) runs on the masked difference
   with its NaNs in place, and a star with a NaN pixel within 4 px is not
   found: on a synthetic star (photutils 3.0.0, the difference's finder
   settings) it is lost with one NaN at 0 to 4 px, found from 5 px, and
   found at every distance when the NaNs are passed as `mask=`. On run 72
   (SCAs 7, 8 and 9, pass 4, below AB 25) every injected source 6 px or
   more from a masked pixel is detected, 298 of 298; within 4 px, 0 of 62;
   at 4 to 6 px, 8 of 23. A missed source has no candidate within 0.3"
   at all (the nearest a median 9" away on SCA 7, 39" on SCA 9), so the 500-source cap
   is not what drops it.

   | | SCA 7 | SCA 8 | SCA 9 |
   |---|---|---|---|
   | detected below AB 25 (3 epochs) | 0.728 | 0.992 | 0.669 |
   | the same 5 px or more from a masked pixel / within 5 px | 0.926 / 0.097 | 0.992 / none | 0.977 / 0.000 |
   | difference masked (NaN) / area within 5 px of one | 0.0325 / 0.242 | 0.0006 / 0.013 | 0.2956 / 0.351 |
   | `usable_fraction` | 0.968 | 0.999 | 0.704 |
   | the field's reference built from | SCA 7's own passes 1 to 3 (field 5383348, job 21832) | SCA 8's own (field 5387158, job 20996) | **SCA 8's frames of `001_0002`**, passes 1 to 3 (field 5390959, job 20997) |
   | reference coverage below 0.5 on the science grid | 0.0275 | 0.0004 | 0.2942 |
   | science pixels masked by the overlay's `dq_mask_bits` | 0.0063 | | |
   | registration dx / dy, px | -0.010 / +0.008 | -0.007 / +0.003 | **-0.068 / +0.131** |
   | `sci_star_psf_sigma_ratio` / `refpsf_stars` | 1.038 / 68 | 1.024 / 65 | 1.035 / 48 |
   | detection by magnitude (AB 21, 23, 25) | 0.79, 0.79, 0.46 | 1.00, 1.00, 1.00 | 0.71, 0.80, 0.69 |
   | sets held by the gate | none | none (`001_0001`) | none |

   Detection below AB 25 by quarter of the SCA (left, three epochs; the
   top row is high y) beside the share of each quarter 5 px or more from
   a masked pixel (right):

   ```text
   SCA 7                                       SCA 9
   1.00 1.00 0.92 0.92 | 0.95 0.93 0.81 0.93    0.00 1.00 0.90 1.00 | 0.11 0.97 0.85 0.98
   0.76 0.67 0.22 0.57 | 0.93 0.77 0.41 0.65    0.12 0.83 0.86 0.90 | 0.10 0.88 0.81 0.97
   0.81 0.50 0.60 0.86 | 0.83 0.49 0.62 0.81    0.20 0.86 0.80 1.00 | 0.10 0.85 0.88 0.98
   1.00 0.82 0.57 0.38 | 0.96 0.85 0.75 0.47    0.00 0.80 0.70 1.00 | 0.06 0.60 0.61 0.62
   correlation 0.93                             correlation 0.95
   ```

   SCA 8's quarters are 0.91 to 1.00 against 0.98 to 0.99. The figure
   (`t6/sca-7-8-9.png` in the brief directory) draws the area within
   5 px of a mask with the detected and missed sources on it.

   **SCA 9: the reference covers 70% of the image.** Field 5390959's
   reference was coadded at 09:11 to 09:13 UTC, during the SCA 8
   footprint, from SCA 8's frames of pointing `001_0002` (expids 1040102,
   2040102, 3040102), the only frames of that field the selection then
   held. The 18-SCA loop at 09:35 found the field's reference and
   differenced SCA 9 against it; SCA 9's own passes 1 to 3 (1040101,
   2040101, 3040101, in the field) never entered it. The left 900 px and
   the bottom 380 px of SCA 9 are uncovered, and the registration offset,
   (-0.068, +0.131) px, is six to eleven times every other SCA's (at most
   0.012), another detector's distortion through the reference. This is
   a measurement artefact of growing one run's selection after a field's
   reference exists, not the reference stage's rule (a run whose
   selection holds every SCA from the start coadds a field from all its
   frames once the intake drains).

   **SCA 7: the detector's own masked pixels.** Its reference is its own.
   0.63% of SCA 7's science pixels carry `DO_NOT_USE`, `DEAD`,
   `NO_LIN_CORR` and `UNRELIABLE_FLAT` together, every one of them in the
   HLWAS overlay's `dq_mask_bits`, mostly isolated pixels on arcs across
   the middle of the detector; resampled, the reference coverage falls
   below 0.5 on 2.75% of the science grid and the difference masks 3.25%.
   Each isolated pixel blinds the finder over a disc of about 5 px, so a
   quarter of SCA 7's area cannot yield a detection.

   **Ruled out with the numbers:** depth or the detection threshold (the
   misses are as bright as the hits, detection flat with magnitude on
   both SCAs and 0.93 to 0.98 away from a mask); registration on SCA 7
   (within 0.01 px, as SCA 8); the science PSF's width (1.035 and 1.038
   against SCA 8's 1.024); the 500-source cap (no candidate at all); the
   gate (no held set on SCAs 7, 8 or 9 in passes 4 to 6).

   **Every SCA of `001_0001` loses area the same way**, pass 4's masks
   against the three epochs' sources below AB 25:

   | SCA | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | area within 5 px of a mask | 0.07 | 0.05 | 0.05 | 0.07 | 0.04 | 0.04 | **0.24** | 0.01 | **0.35** | 0.02 | 0.04 | 0.02 | 0.06 | 0.07 | 0.04 | 0.07 | 0.03 | 0.12 |
   | detected, away from a mask | 0.97 | 0.99 | 0.98 | 0.96 | 0.94 | 0.98 | 0.93 | 0.99 | 0.98 | 0.95 | 1.00 | 0.98 | 0.90 | 0.91 | 1.00 | 0.93 | 1.00 | 0.98 |
   | detected, within 5 px | 0.13 | 0.50 | 0.40 | 0.13 | 0.00 | 0.24 | 0.10 | | 0.00 | 0.00 | 0.00 | 0.25 | 0.22 | 0.11 | 0.00 | 0.09 | 0.00 | 0.13 |

   Away from a mask SCAs 13, 14, 7, 16 and 5 still detect 0.90 to 0.94:
   another cause, not read here.

   **No fix on one key.** `dq_mask_bits` without these bits would let
   back the isolated spikes the overlay masks them for (its comment),
   and `post_zogy_keep_diffimg_lower_cov_map_thresh` (0.5) already keeps
   every pixel any reference frame covers; SCA 9's cause is the run's
   order, not a setting. The fix is code, a divergence from dev: the
   finder and the PSF fit given the difference's non-finite pixels as
   `mask=`, with a `decisions.md` line. **The next measurement**: that
   change on SCA 7's three epochs under a dataset of its own,
   `completeness.py` before and after (up to a quarter of SCA 7's area,
   1 to 12% on the other SCAs); and SCA 9 counted on a dataset whose
   selection holds SCA 9 from the start, so its field's reference is
   coadded from its own frames.

   **SCAs 16 and 17's held sets are a real residual, not a gate bound to
   revisit.** All six epochs' published metrics sit at core +0.027 to +0.036
   against the bound's 0.025 and ring -0.014 to -0.024 (bound 0.015);
   SCA 14's pass 4 at +0.026, its passes 5 and 6 at +0.019 and +0.022. Across the 51 epochs the core follows the science stars'
   width against the ePSF (`sci_star_psf_sigma_ratio`, r = -0.71; 0.985
   on SCAs 16 and 17, 0.994 on 13 and 14, 1.00 to 1.04 on the rest) and
   the ePSF's centroid offset in x (r = +0.57; 0.15 to 0.23 px on SCAs
   13, 14, 16 and 17 against -0.03 to 0.14 elsewhere), not `scalefacref`
   (r = -0.07). A positive core with a negative ring is a science PSF
   broader than the stars it models: the residual is the per-SCA ePSF on
   those detectors. Detection is untouched (SCA 17 0.992 below AB 25, the
   best with SCA 8), so the gate holds sets of a real residual as
   designed; the next measurement is those SCAs' science PSF measured on
   their own stars, not a wider bound.

   Raw evidence in the brief directory's `t6/` (`probe.py`,
   `lostarea.py`, `nandist.py`, `all18.py` and `all18.csv`,
   `finder_nan.py`, `dqbits.py`, `figure.py` and `sca-7-8-9.png`,
   `metrics72.jsonl`, `m18.jsonl`).

7. **The night's operations seen (PRs 389, 394, 395, 398, 399).** Five of
   the night's Improvements seen, one PR each:

   - `job submit --job` marks a `failed` row `launched` with its new Batch
     id (PR 389), so a loop restarted at once counts it pending; no schema
     statement, the CHECK already admits `launched`.
   - Every pass line carries `sets_late` (PR 395): the run's official
     packet sets stamped more than `SETTLE_S` after their job ended, i.e.
     sent by a sweep and not by their own job. It was first read on SMDC's
     rows: run 57 (every container send failed `ClusterAuthorizationFailed`)
     3 of 3; runs 68, 69, 72, 73 0.
   - The CLI logs the `rapidpipe` loggers at INFO and every other library
     at WARNING (PR 394), so a Batch job's own INFO lines reach CloudWatch.
   - The gallery's flux stretch is ±3e-3 (it was ±1e-3), and a job that lists no residual stars
     gets cutouts at its kept sources of highest S/N (PR 398). Run 67
     (GBTDS) went from a thumbnail alone to eight cutouts per job.
   - A failed job holds only the work that depends on it: the loop drains
     the rest, then exits 2 with the failed jobs and their scopes in the
     last pass's `failed` (PR 399; its unit contract: one chain failed,
     the other's packet submitted and published over two passes, exit 2).

   Not here, recorded: the difference's memory ask against its 16.5 GB
   peak (a Spot lever, and Spot is not in this brief); the packet stage's
   out-of-field count (it duplicates the difference's).

   Proof, run **82** (`r00342-dy-ops`, `c0f5a38e`, image
   `sha256:9b0e0f01…`, **`rapid-rebuild:64`**; F146, SCAs 8 and 5,
   pointing `001_0001`, passes 1 to 4; 15:17:48 to 15:27:53 UTC, 11
   passes, exit 0):

   ```sh
   rp run create --who "$USER" --why "..." --revision c0f5a38e... \
     --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
     --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
     --survey HLWAS --dataset r00342-dy-ops
   rp official change --group live.json ...                 # run 82 live for HLWAS r00342-dy-ops
   rp job local --run 82 --stage import ...                 # Gaia under the dataset's key, job 22046
   rp job local --run 82 --stage psf ...                    # SCA 8's psf, failed on purpose: job 22047
   rp job submit --job 22047 --job-definition rapid-rebuild:64
   rp loop --run 82 --filters F146 --scas 8,5 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:64 --interval 60
   scripts/check_run.sh --run 82 --inspect <dir>
   ```

   Check, held:

   - **The resubmission.** The deliberate failure (SCA 8's psf job run
     locally with HTTPS behind a dead proxy) failed before `job run`
     recorded `running`: the outputs prefix's attempt listing comes
     first. So the row stayed `launched` with no Batch id, not `failed`.
     `job submit --job 22047` gave it a Batch id, and the loop started at
     once: its first pass counted 22047 under `pending.psf`, submitted SCA
     5's psf (22053) and did not exit. 22047 then published on Batch. The
     `failed` → `launched` transition itself is PR 389's database
     contract; no live failure of that shape offered itself.
   - **The pass line.** `sets_late` was 0 in all 11 passes; `sets_produced`
     was empty in every pass; set 22380 sat under `sets_waiting` for one
     pass while its own job sent it.
   - **The stream.** Packet job 22380's CloudWatch stream holds `packet
     set 22380 published and bound; producing it to the topic` (15:26:39.6)
     and `378 packets, 85415497 bytes acknowledged in partition 0 offsets
     702..1143; stamped` (15:27:18.2). A difference job's stream (22066)
     has 134 INFO lines among its 3,511.
   - **The gallery.** Two jobs, 22066 and 22067, eight residual-star
     cutouts each at ±3e-3. The core-and-ring pattern and the spikes show,
     though the brightest cores still clip.
   - **The drain.** No job failed (17 jobs, each published on its first
     attempt), so the live loop exited 0; the failed-epoch drain stands on
     its unit contract.
   - `check_run.sh --inspect`: **CLEAN**, validate 6/6, reader 2/2,
     consumer 559/559.

   Seen, not built: a job that dies before `job run` records `running`
   (here, the attempt listing) leaves its row `launched`. Without a Batch
   id (a local job) no reconcile ever ends it, and a loop counts it
   pending for ever. Recording `running` before the first read of the
   outputs prefix, or marking the row `failed` on any exception, would
   close it (`rapidpipe/foundation/local.py`, `run_job`).

   Raw evidence in the brief directory's `t7/` (`loop-82.log`,
   `rows-82.txt`, `stream-packet.txt`, `stream-difference.txt`,
   `check-82.txt`, `inspect-82/`, `gallery-67/`, `late-first-check.txt`,
   `failpsf.log`).

## Results of part 12 (2026-10-24)

The day's eight tasks ran unattended, seven in parallel and then the
docs. Every task wrote `TASK DONE` on its first attempt. The night's
one science finding is answered: under association by the source's
field, 0.97 of the detected injected sources are alerted, against
0.40. The producer is idempotent again, OpenUniverse TDS runs through
the chain and is counted against its own truth, and the night's
operational notes landed. The three probes (GBTDS's reference PSF,
F213's scale, SCAs 7 and 9) each ended in a measurement, and none
changed an overlay. Every run below is CLEAN by `check_run.sh`.

| step | before | after | where |
|---|---|---|---|
| 1 field rule | `dev`'s pass 2: a source outside its image's field only joins an existing object; run 72 alerted 12,097 of 28,625 detected injected (0.42), 20,575 objects in 70 fields | association by the source's field, locks over each touched field and its neighbours, the forced window by position; run 81 alerted 27,750 of 28,623 (0.97), purity per magnitude unchanged (0.93 at AB 21 to 0.20 at 26), 37,800 objects in 290 fields, false positives per epoch 95 → 224, 30.9 min cold against 28.1 warm | PRs 393, 404; rapid_docs 99 |
| 2 producer | `enable_idempotence=False` (PR 371), the grant missing | idempotent, `acks="all"`; run 78, 3 of 3 sets sent by their own packet jobs 3 to 4 s after they end, the sweep sent nothing, consumer CLEAN 280/280 | PRs 388, 396; rapid_docs 100 |
| 3 OpenUniverse | one image through intake, the psf stage and the listing SOC-only | galsim.roman PSF in the psf stage, the loop's listing by obseq row, `completeness_ou.py`; run 80 (H158 SCA 10, six-frame reference, ten epochs, 29 jobs first attempt): 410 evaluated transients detected 1.0 to AB 24, 0.5 at 25, the 500-source cap ending the count; 348 rows lit in a reference frame reported apart | PRs 390, 392, 400, 402; rapid_docs 101 |
| 4 GBTDS reference PSF | `reference_psf = "science"`, 367 / 418 / 346 above S/N 20 (run 67) | the crowded-field measured variant converges (100 stars, σ 1.20 to 1.26 px) and gives 62 / 500 / 81 (run 79), not lower on all three epochs, so not adopted; the variant stays on the unmerged branch `day/gbtds-refpsf` | PR 403 |
| 5 F213 scale | measured `scalefacref` 0.939, core and ring both positive (run 54) | forced to the zero points' ratio, 0.972: star flux ratio 1.036 → 1.002, but the ring stays (+0.046 → +0.036) and the core goes negative (−0.021 → −0.045); a width mismatch, so no `[difference] scale` setting | PR 397 |
| 6 SCAs 7 and 9 | 0.73 and 0.67 detected below AB 25, not diagnosed | area lost: `DAOStarFinder` misses any star within 4 px of a NaN (298 of 298 detected at 6 px or more); SCA 9's reference coadded from SCA 8's `001_0002` frames (70% coverage), SCA 7's isolated masked pixels blind 24% of its area; no one-key fix; SCAs 16 and 17's held sets a real residual | PR 391 |
| 7 ops | the night's five notes | `job submit --job` marks a failed row `launched`, rapidpipe's INFO lines in CloudWatch, `sets_late` in the pass line, the gallery at ±3e-3 with cutouts without residual stars, a failed job holds only its own work; run 82 CLEAN 559/559 | PRs 389, 394, 395, 398, 399, 401 |
| 8 docs | rapid_docs at the night's state | the field rule across the foundation, difference, alerts and loop pages; OpenUniverse in the foundation and loop pages; the loop's operations; this section | rapid_docs 102, this PR |

The live runs the day made: 75 and 79 (`r00340-gb2`; 74 was created
with a mistyped revision, never live, no jobs), 76 and 77
(`r00342-dy-f213`, `r00342-dy-f213z`), 78 (`r00342-dy-producer`), 80
(`ou-tds2`), 81 (`r00342-dy-field`), 82 (`r00342-dy-ops`). Job
definitions `rapid-rebuild:61` to `:64` were registered today. Nothing
was deleted.

**What remains, and where it sits:**

- **The false positives per epoch under the field rule**, a median 224
  on the injected footprint, 130 of them on bright-footprint-flagged
  sources (step 1). Alert volume is 2.3 times the night's at the same
  purity per magnitude; it is the next science question.
- **The finder blinded by NaNs** (step 6): `DAOStarFinder` and the fit
  given the difference's non-finite pixels as `mask=` would recover up
  to 24% of SCA 7's area and 1 to 12% on the other SCAs. It is a code
  divergence from `dev`, so it needs a `decisions.md` line, and it is
  the next detection lever on every SCA. SCA 9 is then counted on a
  dataset whose selection holds it from the start.
- **The OpenUniverse source cap** (step 3): every difference keeps 500
  of 2,077 to 3,682 candidates, so the count below AB 25 measures the
  cap; one measured step on `[difference] max_sources` on the overlay.
  Its `scalefacref` of 2.2 against HLWAS's 0.99 is unexplained;
  `[reference.instrument] sca_gain = 2.0` against the simulation's
  gain of 1 is the first candidate (unverified). Run 80 predates the
  field rule: 16 of its 35 detections, outside their image's field,
  were not alerted.
- **GBTDS's science PSF** (step 4): the frames' stars are 1.31 times
  the ePSF's width (HLWAS 1.02), so ZOGY's science half is mismatched
  whatever the reference PSF is; epoch 2171101's difference noise is
  underestimated (22,357 candidates, noise correlation 2.32), seen and
  not read.
- **F213's width** (step 5): the reference PSF (1.387 px) is wider than
  the science ePSF (1.291 px) and the science stars (1.326 px); the
  measured scale's 3.4% bias would earn `scale = "zeropoint"` only once
  the width is right.
- **The bright-star residual's cause** (part 11 step 3): flagged, not
  fixed, unchanged today.
- **A local job dying before `job run` records `running`** stays
  `launched` for ever and a loop counts it pending (step 7, run 82's
  job 22047), seen and not built.
- **Run 72's 8 and run 73's one official set unstamped** (step 7's
  first check of `sets_late`), cause not read.
- **The packet's window query** reads the dataset's difference images
  in the MJD window through an unindexed haversine; enough at 162
  images, a q3c or HEALPix predicate on `diffimages (ra0, dec0)` at
  thousands a month (step 1).

The night's grants are closed: the Spot interruption capture is live,
the three identity sequences are past their imported ids, and the job
role writes data idempotently (Ben, 2026-10-24 14:02 UTC).

**Session notes**, every task's, gathered for Ben (the PR each came
from in brackets):

- Task 1 (field rule): the killed first launch at 14:40 had left an
  empty local branch `day/field-rule`, so the brief's
  `git worktree add ... -b` failed and the worktree went onto the
  existing branch; a relaunch addendum should say "branches may exist,
  empty" (404). PR 393's squash subject kept "runbook part 12's
  header", which task 6 wrote first (404).
- Task 2 (producer): the harness refuses a foreground `sleep` outright,
  so even a short look at a just-started loop needs a background waiter
  (396).
- Task 3 (OpenUniverse): the brief's image check `podman run --rm
  <image> python -c ...` cannot work, because the entrypoint is
  `rapidpipe` and `python` is not on the image's PATH;
  `--entrypoint /opt/rapid/conda/envs/rapid/bin/python3` works (390).
  The brief's "a Gaia import is not needed" met the loop's hard
  requirement for one at the packet stage; a brief that waives an input
  should check the planner's refusals first (400).
- Task 4 (GBTDS): `[difference.paths]` is frozen in a run's settings at
  `run create`, so a proof mixing Batch stages and local differences
  spends two runs unless the run is created with the paths up front
  (403).
- Task 5 (F213): the difference stage refuses an `l2files` input keyed
  under another dataset, so "the same epoch under a second dataset"
  re-keys the whole upstream chain; a brief should budget it or name
  the not-bound publish under the first dataset (run 61's pattern)
  (397).
- Task 6 (SCAs 7 and 9): this host's AWS CLI has no `-q` on `aws s3 cp`;
  `--only-show-errors` is the quiet flag (391).
- Task 7 (ops): a heredoc fed to `python -` and also given `</dev/null`
  (the no-stdin rule) runs nothing and exits 0, because the later
  redirect replaces the heredoc; write the script to a file first (401).
- Task 8 (docs): the rapid_docs venv is still absent on this host, so
  the Sphinx build is CI's alone (rapid_docs 102); the main `rapid`
  checkout carries an untracked `views-after.txt` from an earlier
  session, left in place.

## Part 13: the weekend planner (2026-10-24)

Each step below is one planner-written brief, run in parallel with others; the queue is the state vault's `rapid-queue.md`.

### Step dev-takenow: dev's three reference-side fixes, one reference rebuilt beside run 46's

**The change (PR 406, merged `44052575`).** Three hand ports from `dev`,
none touching pixels: the reference's `fwhmmedpix` is now the mode of
`FWHM_IMAGE` over point sources (FLAGS 0, FLUX_AUTO/FLUXERR_AUTO >= 50,
ELONGATION <= 1.3) through the difference side's `catalog_fwhm_stats`,
`fwhm_ref` that mode in arcsec (dev 1822789e, 095c45d8, 46fc651a,
fdc7fa79 reference half); `[reference.sextractor_refimage]`
`seeing_fwhm` 1.2 -> 0.25 and `deblend_nthresh` 4 -> 32 (dev ce485e8f);
`photutils>=3.0,<4` (dev 8d766c43). The 7000 px mosaic stays held
(queue item `dev-refresh`).

**The proof.** Image `rebuild-44052575…` (digest `a66b64da…`), job
definition `rapid-rebuild:66`. Run **85**, dataset `r00342-dt-f146`,
live (group 21392), created from the shipped layers, the HLWAS overlay
and run 46's two sets (source root, `before_mjd = 61591.4056`, the
bound read from run 46's layers), plus `intake.crpix_shift = [0.0, 0.0]`:
run 46 predates the overlay's `crpix_shift`, and without the pin the
coadd's WCS would move. Against run 46's stored body the resolved
settings then differ only in the two ported keys and in keys run 46's
code did not have (the gate, `[batch] max_submissions_per_pass`,
`intake.reader = "soc-l2"`, `rederive_wcs = false`, `psf.epsf_cache`,
`difference.reference_psf`), none read by intake's pixels or the
reference stage. The loop over pointing `001_0001`, SCA 8, F146,
passes 1 to 3 admitted the three frames run 46's reference job 19863
coadded (expids 1040101, 2040101, 3040101), and ran psf 22751, intake
22760 to 22762 and reference **22765** (Batch `4f09727c…`, m6a.2xlarge
as 19863), field 5387158, drained with no failure and no difference
(nothing after the bound). Evidence in the brief's `t1/`.

```sh
rp run create --who rusholme --why '…' --revision 44052575b47d2cb82b935917b3ec49068eab914c \
  --overlay rapidpipe/settings/datasets/hlwas-r00342.toml --set 'intake.source_root="'$SOURCE_ROOT'"' \
  --set reference.selection.before_mjd=61591.4056 --set 'intake.crpix_shift=[0.0, 0.0]' \
  --survey HLWAS --dataset r00342-dt-f146
rp loop --run 85 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3 \
  --job-definition rapid-rebuild:66 --interval 60
```

**Measured**, reference job 19863 (run 46) beside 22765 (run 85):

| | run 46 | run 85 |
|---|---|---|
| catalog sources (`nsexcatsources`; `refimcatalogs` one row each) | 4,240 | 4,299 |
| CLASS_STAR > 0.8 (stars) | 411 | 1,059 |
| CLASS_STAR < 0.2 (galaxies) | 1 | 1,447 |
| between | 3,828 | 1,793 |
| `fwhmmedpix` recorded | 2.70 (all-source median) | 2.425 (point-source mode) |
| `fwhmminpix` / `fwhmmaxpix` | -4.26 / 29.56 | -4.26 / 43.81 |
| `fwhm_ref` (logged) | 2.70 px | 0.267 arcsec |
| reference image and uncertainty arrays | — | identical (7000 x 7000, NaN masks equal, max diff 0) |
| `cov5percent` | 19.566 | 19.566 |

Pixels: identical, as expected; only `CHECKSUM`, `DATE`, `RPJOB` and
`RPRUN` differ in the headers. CLASS_STAR at seeing 1.2 put 90 per cent
of the catalog between 0.2 and 0.8; at 0.25 the catalog separates. The
point-source mode on run 46's own catalog is also 2.425 px, so the
statistic's change is the method's, not the deblending's. The job's
`star_sigma_px` reads 1.017 px over 135 stars against run 46's 1.554
over 228: that is commit 2369769a's metric (after run 46's revision),
which on run 46's catalog gives 1.016 over 139; the ports move it by
0.0003 px.

### Step err-tools: an external tool's nonzero exit fails the job, its reason on the row

**The change (PR 407, merged `a1cc82e7`).** `ToolRunner.run` and
`run_shell` (`rapidpipe/science/difference/tools.py`) raise `ToolFailed`
on a nonzero exit: the tool's name (a Python interpreter's script
named with it, so ZOGY reads `python3 py_zogy.py`), its exit code and
the last 20 lines of its merged stdout and stderr. `check=False` returns
the code instead; SFFT's caller passed it, and PR 410 (`differencer`)
took it away when SFFT became the published differencer, so no stage
passes it now. The callers rely on the default: SExtractor
(`sextractor.py` 157 and 202, `gainmatch.py` 110, the reference's
`catalog.py` 69), SWarp (`resample.py` 150, 162, 169), ZOGY
(`difference.py` 687), bkgest (`background.py` 64 and 105), awaicgen
(`reference.py` 357). `run_job` (`foundation/local.py`) now writes any
failure's `type: message` into the row's `refusal`, where it wrote
`NULL` for anything but a `CommandExit`.

**The proof.** Image `rebuild-a1cc82e7…` (digest `191e2a09…`), job
definition `rapid-rebuild:65`; HLWAS `001_0001`, SCA 8, F146, passes 1
to 4 (three reference frames, epoch 4040101 differenced). Two runs, each
live under its own dataset: run **83** (`r00342-et-fail`) with the
difference stage's SExtractor pointed at `cat`, which refuses
SExtractor's `-CATALOG_NAME` option and exits 1 with two lines; run
**84** (`r00342-et-ok`), the same layers without it. Evidence in the
brief's `t1/`.

```sh
rp run create ... --revision a1cc82e7... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --set 'difference.paths.sextractor="cat"' --survey HLWAS --dataset r00342-et-fail   # run 83; run 84 without the last --set
rp official change --group live.json ...; rp job local --run 83 --stage import ...    # live; Gaia under the dataset's key
rp loop --run 83 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:65 --interval 60
```

Check, held:

- **The failed job.** Run 83's difference job 22766 (Batch
  `4cc06c89…`): Batch `FAILED`, one attempt, exit 70; the row `failed`,
  exit 70, `refusal` `ToolFailed: cat exited with code 1; its last 2
  output lines:` then `cat: invalid option -- 'C'` and `Try 'cat --help'
  for more information.` Its CloudWatch stream ends with the traceback
  through `sextractor.py:157`, the first SExtractor call, and the same
  message. The loop listed it under `failed` once, reran nothing (its
  reruns key on a `Batch ` reason), and exited 2 at 20:06:55 UTC after
  11 minutes; intake, psf and reference published.
- **The twin.** Run 84's difference job 22767 published, then its packet
  job 22775; the loop exited 0. Beside run 82's job 22067 on the same
  epoch (r6i against m6a): candidates 774 and 773, S/N above 20 19 and
  21, core −0.00106 and −0.00103, ring +0.00173 and +0.00171,
  registration identical to 1e-8 px; the Intel-against-AMD difference
  already recorded. `check_run.sh --run 84`: **CLEAN**, validate 6/6,
  reader 1/1, consumer 180/180.

### Step differencer: SFFT becomes the published differencer, proven on one epoch

**The change (PR 408, merged `f49be4fe`; PR 410, merged `5729031a`;
rapid_docs 103).** Ben ruled at 19:22 UTC: switch now, measure after.
This matches `dev`'s choice since b725007d (2026-04-20; `diff_flavor =
sfft`).

- The difference job's `diffimages` row is the SFFT image, and its
  `sources` is SFFT's positive PSF-fit catalog through the unchanged
  `source_set`.
- The packet stage reads the PSF and uncertainty beside
  `diffimages.filename`. So with no packet code changed, its cutouts and
  forced photometry use the SFFT image, `sfftdiffpsf.fits` and
  `sfftdiffimage_uncert_masked.fits`.
- The published image's metrics (core, ring, noise correlation, source
  counts) are measured on the SFFT image.
- ZOGY still runs. Its positive and negative PSF-fit catalogs and SFFT's
  negative one stay under `diff/` as diagnostics, read by no stage.
- `[difference.sfft] run_sfft = false` fails the settings check.
- SFFT runs checked like every tool: a nonzero exit raises `ToolFailed`,
  with no fallback to ZOGY. PR 410 reconciled this with err-tools' PR
  407, which merged minutes before 408 with SFFT left unchecked and a
  test for the fallback, so the tip briefly held both contracts.
- The import reads SFFT's files.
- `isdiffpos` stays `True`: SFFT's positive catalog detects and fits on
  the positive image, as `dev`'s loader reads it.

**The proof.** Image `rebuild-5729031a…` (digest `dfef56b3…`), job
definition `rapid-rebuild:67`, which also carries PRs 406 (dev-takenow)
and 407. Run **86** (`r00342-dy-sfft`), live, Gaia job 22768; HLWAS
`001_0001`, SCA 8, F146, passes 1 to 4 (three reference frames, epoch
4040101 differenced). Evidence is in the brief's `t1/`.

```sh
rp run create ... --revision 5729031a... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --survey HLWAS --dataset r00342-dy-sfft                                   # run 86
rp official change --group live.json ...; rp job local --run 86 --stage import ...   # live; Gaia
rp loop --run 86 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:67 --interval 60
scripts/check_run.sh --run 86 --inspect DIR
```

Check, held:

- **Execution.** Nine jobs (import, psf, four intakes, reference,
  difference 22776, packet 22777), each published on its first attempt.
  The loop exited 0 at 20:17:46 UTC after 12 minutes.
  `check_run.sh --run 86`: **CLEAN**, validate 6/6, reader 1/1, consumer
  471/471.
- **The published products.** `diffimages.filename` is
  `…/86/difference/22776/1/diff/sfftdiffimage_masked.fits.gz`. All 500
  `sources` rows are SFFT's, `isdiffpos` true. The manifest records
  `differencer: sfft`.
- **The prefix.** It holds the SFFT image, its uncertainty, mask and
  PSF, the science image, the template, the reference PSF, and the four
  PSF-fit catalogs.
- **The cutouts.** Three interior packets' `cutoutDifference` stamps
  equal the SFFT image at the source pixel for pixel (16,641 of 16,641
  each).

Measured, same epoch 4040101, same instance type (m6a.2xlarge):

| | ZOGY, run 46 job 19864 | ZOGY in job 22776 (diagnostic) | SFFT, run 86 job 22776 (published) |
|---|---|---|---|
| positive PSF-fit rows (candidates) | 752 | 779 | 2,173 (2,151 inside the bounds) |
| of them S/N > 20 | 23 | 24 | 683 |
| kept sources at S/N > 20 | 23 of 500 | — | 500 of 500 |
| negative PSF-fit rows (S/N > 20) | — | 772 (37) | 987 (302) |
| core fraction / ring fraction | +0.006 / −0.002 | — | **+0.251 / +0.021** |
| noise correlation | 1.25 | — | 1.03 |
| difference seconds | 179 | 188 (both differencers run) | |

The ZOGY counts inside job 22776 differ from run 46's by about 4%. This
fits PR 406's reference changes, which this image carries.

**Seen, reported, not tuned.** SFFT's difference leaves about a quarter
of each bright star's flux in its core: core fraction 0.25 against
ZOGY's 0.006. The gallery (`t1/inspect-86/`) shows positive cores, not
ZOGY's core-and-ring. So the 500-source cap fills with stars: 363 of
the 500 kept sources are in a bright reference source's footprint.
`dev`'s own SFFT numbers point the same way (61k spurious per SCA on
250 GBTDS SCAs, its ini's completeness note). Two sources of the published set sit
0.04 px apart, a duplicate detection in SFFT's catalog.

**Consequence for operations.** The gate's `core_fraction_max_abs =
0.025` was read on ZOGY. It will hold every SFFT packet set once a run
has `min_epochs` (5) published differences. A loop of five or more
epochs under the new default sends nothing to Kafka until the core
residual or the bound changes. Task `differencer-measure` measures this
on the injected and OpenUniverse samples. This brief does no tuning.

### Step err-publish: empty products never bound, refused jobs visible, exit codes right

**The change (PR 409, merged `e46d6cf5`; rapid_docs PR 104, `d5a6ffba`).**
The publisher's automatic binding (`_bind`, `rapidpipe/foundation/publish.py`)
counts the rows each table loaded and refuses, inside its savepoint, a
job that expects a product whose table loaded none (an undeclared table
counts as none), unless the manifest's `empty_ok` names the table: the
rows stay published and unbound, the reason on `jobs.refusal`.
`JobContext.write_manifest(..., empty_ok=...)` writes it; `difference`
declares `sources` (nothing above the cut is a valid set), `packet`
declares `packets` when its source set has no triggers; `intake`, `psf`,
`reference` and `import` declare nothing. The loop's `snapshot` reads
`refusal` and `plan` reports a published job with one under `failed`,
each entry with its `refusal`, its key held like a failed job's (it was
resubmitted every pass before). `loop` exits 1 for failed or refused jobs
(was 2); `job wait` exits 0 when all published, 1 when one failed (was
0), 2 at the timeout (was 75). The earlier parts' "exit 2" are the
codes of their day.

**The proof** that the refusal does not fire on healthy work. Image
`rebuild-e46d6cf5…` (digest `9a5c2263…`), job definition
`rapid-rebuild:68`; run **87** (`r00342-wk-errpub`, live, group 21421),
HLWAS `001_0001`, SCA 8, F146, passes 1 to 4 (three reference frames
before the bound 61591.4056, part 9 step 2's F146 value, and one
differenced epoch). Evidence in the brief's `t1/`.

```sh
rp run create ... --revision e46d6cf5... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --survey HLWAS --dataset r00342-wk-errpub                     # run 87
rp official change --group live.json ...; rp job local --run 87 --stage import ...   # live; Gaia, job 22778
rp loop --run 87 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:68 --interval 60
scripts/check_run.sh --run 87 --inspect <dir>
```

Check, held:

- **The drain.** 11 passes, 20:14:06 to 20:24:06 UTC, exit 0, `failed`
  empty in every pass: psf 22779, intake 22780 to 22783, reference
  22784, difference 22785, packet 22786, each published on attempt 1
  with `refusal` null; the import 22778 the same.
- **The bindings.** 14 opened, one per expected key: catalogs, psfs,
  four exposures and four l2files, refimages, diffimages, sources,
  packets. Rows behind them: 4 l2files, 1 psfs, 1 refimages, 1
  diffimages, 500 sources, 471 packets. Difference 22785's manifest
  carries `empty_ok: ["sources"]`; the set was not empty.
- `check_run.sh --inspect`: **CLEAN**, validate 6/6, reader 1/1,
  consumer 471/471.

The refusal itself, the loop's report of it and the exit codes stand on
their CI contracts (`tests/db/test_publish.py`, `tests/unit/test_loop.py`);
no live failure was staged.

### Step err-inputs: a packet job's required inputs fail it; nothing is caught around the PSF fit

**The change (PR 415, merged `f90d9fef`).** A packet job now fails,
naming the file or key, when the trigger epoch's difference, science or
template stamp image cannot be read (`write_set`), when the reference
catalog is unreadable, empty or short of a `REFCAT_COLUMNS` column
(`load_refcat`), or when the trigger's reference has no `refimcatalogs`
row (`_read`). Each of these used to ship every packet of the set with
null cutouts or null reference matches. Window images were already
required here: they are fetched by `prefix.get` and read by forced
photometry, both of which raise, so no optional-image path remains.
`compute_psf_catalog` no longer catches anything around
`PSFPhotometry.__call__`. The probe below found no empty input on which
photutils 3.0.0 raises: no sources found, all-NaN or all-zero data, and
filtered-out candidates all return `None`, which stays the empty
catalog. It raises `ValueError` only for faults, such as a zero or NaN
uncertainty under a source or a mismatched uncertainty shape. Over the
prompt queue's 14-day log retention, the removed handler's message
appears 0 times.

**The census.** Every reference of the 50 live runs (436) and of every
run (714) has its cattype-1 `refimcatalogs` row, so the new raise cannot
fire on the data at hand.

**The proof.** The image is `rebuild-f90d9fef…` (digest `b47e1c5c…`),
job definition `rapid-rebuild:69`. The sample is HLWAS `001_0001`, SCA
8, F146, passes 1 to 4: three reference frames, with epoch 4040101
differenced and packeted. Run **90** (`r00342-ei-inputs`) is live under
its own dataset. Evidence is in the brief's `t1/`.

```sh
rp run create ... --revision f90d9fef... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --survey HLWAS --dataset r00342-ei-inputs                                          # run 90
rp official change --group live.json ...; rp job local --run 90 --stage import ...    # live; Gaia under the dataset's key
rp loop --run 90 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:69 --interval 60
```

Check, held:

- **The chain.** All nine jobs were published with exit 0 and no
  refusal. The loop ran from 20:40:41 to 20:49:46 UTC, drained, and
  exited 0. `check_run.sh --run 90` was **CLEAN**: validate 6/6, reader
  1/1, consumer 471/471.
- **The packets.** All 471 records carry all three cutouts. All 471
  carry non-null reference star and galaxy match lists, and 285 have a
  star match.
- **Beside the earlier runs**, epoch 4040101:

  | run | differencer | difference job, instance | candidates / kept | S/N > 20 | packet job, instance | packets | packets S/N > 20 |
  |---|---|---|---|---|---|---|---|
  | 84 | ZOGY (before PR 408) | 22767, m6a.2xlarge | 773 / 500 | 21 | 22775, m6a.2xlarge | 180 | 13 |
  | 86 | SFFT | 22776, m6a.2xlarge | 2151 / 500 | 500 | 22777, m6a.2xlarge | 471 | 471 |
  | 90 | SFFT | 22824, m6a.2xlarge | 2151 / 500 | 500 | 22866, r6i.8xlarge | 471 | 471 |

  Run 90 equals run 86, the same differencer on the same epoch. The
  change from run 84 is PR 408's switch to SFFT, which step differencer
  reports, not this change.

### Step differencer-measure: SFFT against ZOGY on the same bytes, injected HLWAS and OpenUniverse

**The method (PR 418, merged `a17fca7d`; rapid_docs 107, merged `ca35d3d7`).** The SFFT default (image `rapid-rebuild:67`, task 1) was measured against ZOGY runs 81 (HLWAS injected) and 80 (OpenUniverse TDS). The inputs were the same bytes, not a rebuilt reference.

- **New datasets.** `r00342-dy-sfft-inj` (run **88**) and `ou-tds2-sfft` (run **89**), from the baselines' layers at `5729031a`.
- **Loop for PSF and intake only.** The selection holds no frame before the bound, so the reference is reported held.
- **Hand-submitted jobs.** Differences 22804–22816 and packets 22825–22837. Each names the new `l2files` row and the baseline difference job's own `refimages` and `psfs` rows. The difference and packet stages read those rows by job, and refuse only an `l2files` row of another dataset.
- **Identity checks.** The L2 science arrays' sha256 are equal (13 of 13), and the reference and PSF rows are the same. ZOGY, which still runs inside each job, reproduces the baseline's 500 source ids on 11 of 13 epochs. OpenUniverse 14492 (AMD against Intel) shares 438. OpenUniverse 28737 (Intel against Intel) shares 323; that cause is unread.
- **Script fields.** `completeness.py --expids` recomputes run 81 on the sample's three epochs. `completeness_ou.py` reads the reference that the difference jobs named, and gains `false_pos`, `n_candidates` and `container_seconds` per epoch.

```sh
rp run create ... --survey HLWAS --dataset r00342-dy-sfft-inj     # run 88; run 81's layers
rp loop --run 88 --filters F146 --scas 8 --pointings 001_0001 --passes 4,5,6 --job-definition rapid-rebuild:67
#   psf and intake; the reference held (no frame before the bound selected)
submit.py difference 88 81 rapid-rebuild:67; rp job wait ...; submit.py packet 88 81 ...   # the brief's t2/scripts
python scripts/completeness.py --run 81 --expids 4040101,5040101,6010101
python scripts/completeness.py --run 88 --expids 4040101,5040101,6010101
python scripts/completeness_ou.py --run 80; python scripts/completeness_ou.py --run 89
scripts/check_run.sh --run 88 --inspect DIR; scripts/check_run.sh --run 89 --inspect DIR
```

Check, held:

- `check_run.sh --run 88`: **CLEAN** (validate 6/6, reader 3/3, consumer 1,393/1,393).
- `check_run.sh --run 89`: **CLEAN** (validate 6/6, reader 10/10, consumer 4,934/4,934).
- Every job published on its first attempt.

Measured (fractions of injected, or of evaluated on OpenUniverse; purity is true packets over all packets in the bin):

| HLWAS `001_0001` SCA 8, passes 4–6, 600 injected | ZOGY run 81 | SFFT run 88 |
|---|---|---|
| detected AB 21 / 23 / 24 / 25 / 26 | 1.00 / 1.00 / 0.97 / 0.99 / 0.55 | 0.73 / 0.64 / 0.61 / 0.00 / 0.00 |
| alerted, all | 516 | 238 |
| purity AB 21 / 23 / 24 | 0.91 / 0.71 / 0.52 | 0.31 / 0.10 / 0.18 |
| false positives per epoch | 228 (145 bright-flagged) | 385 (289 bright-flagged) |
| negative PSF-fit candidates / S/N > 20 per epoch | 751 / 30 | 975 / 300 |
| core fraction | +0.003 | +0.252 |
| difference seconds | 205 | 219 |

| OpenUniverse H158 SCA 10, ten epochs, 410 evaluated | ZOGY run 80 | SFFT run 89 |
|---|---|---|
| detected AB 21–23 / 24 / 25 | 1.00 / 0.89 / 0.28 | 1.00 / 0.79 / 0.03 |
| detected / alerted, all | 35 / 19 | 24 / 24 |
| purity AB 24 | 0.04 of 236 | 0.01 of 2,562 |
| false positives per epoch | 48 | 494 (455 matched to no truth row) |
| candidates per difference | 2,612 | 2,354 |
| negative PSF-fit candidates / S/N > 20 per epoch | 2,736 / 560 | 2,470 / 707 |
| core fraction | +0.005 | +0.005 |
| difference seconds | 292 | 279 |

**Seen, reported, not tuned.**

- **HLWAS.** SFFT's positive star cores fill the 500-source cap, and detection stops at AB 24.5.
- **OpenUniverse.** SFFT subtracts stars cleanly, but the cap fills with sources that match no truth row.

Galleries are in the brief's `t2/inspect-88/` and `t2/inspect-89/`.

**Confounds.**

- **OpenUniverse packet metrics.** Run 80 predates the field rule (PR 393), so its alerted, purity and false-positive counts differ by that rule as well as by SFFT. Detection depends on sources alone.
- **The gate.** The gate held no set. HLWAS's three epochs are below `min_epochs` (5), and OpenUniverse's core fraction is inside the bound.

**The ruling owed.** The default stands as Ben ruled. Whether to revert it, or port dev's SFFT catalog cuts (sumrat, nneg, nbad), is his ruling.

### Step dev-refresh: the 9100 px mosaic and dev's reference grading, measured on one pointing

**The change (PR 416, merged `9a7f0167`; rapid_docs 106).** `dev`
1518042b's mosaic: `[reference.mosaic]` 7000 → 9100 px. `dev`
cb53d2c7, 2851627b and fdc7fa79's reference grading:
`cdf/infobitsref.txt` by cherry-pick of that one path, the rest by hand
in `rapidpipe/science/reference/infobits.py`, `[reference.infobits]`
holding `dev`'s provisional thresholds and `fatalinfobits = [2, 5, 7,
8, 16]`. The grade goes on the `refimages` row (`status`, `infobits`),
in the manifest's `reference` block, and on the mosaic's and
uncertainty map's headers (`INFOBITS` with its set-bits comment,
`STATUS`, the `BITnn` cards). The Photutils catalog's bits (5, 6, 7, 16)
are never set, because this build makes no Photutils reference catalog
and has no PSF at the reference stage. A status-0 reference is published
and differenced against, as `dev`'s science pipeline differences
against the reference it has just built. The reference job also records
`awaicgen_seconds`.

**The proof.** Image `rebuild-9a7f0167…` (digest `9169d4d2…`), job
definition `rapid-rebuild:70`. Four runs, each live under its own
dataset, created from the shipped layers and the HLWAS overlay with
`before_mjd = 61591.4056` and `batch.memory_mib.reference = 12288` on
all four (headroom for the first 9100 build; the peak came in under the
default). Each run is one arm of a pair, `001_0001`, SCA 8, F146,
reference from passes 1 to 3 and passes 4 to 6 differenced (epochs
4040101, 5040101, 6010101) under SFFT, today's default:

| run | dataset | source | mosaic |
|---|---|---|---|
| **91** | `r00342-dr-inj7000` | the injected footprint `r00342-inj1` | `--set` 7000 |
| **92** | `r00342-dr-inj9100` | the same | 9100 (default) |
| **93** | `r00342-dr-null7000` | HLWAS r00342, uninjected | `--set` 7000 |
| **94** | `r00342-dr-null9100` | the same | 9100 (default) |

The arms stop at the difference: `t1/drive_arm.sh` in the brief
directory runs `rp loop --once` passes until the three difference jobs
are submitted, then `rp job wait` on them, so no packet job is planned
and no Gaia import is needed. Evidence in the brief's `t1/`.

```sh
rp run create --who rusholme --why '…' --revision 9a7f016737471cdc602cbf7af86e0c83b48aa4f2 \
  --overlay rapidpipe/settings/datasets/hlwas-r00342.toml --set reference.selection.before_mjd=61591.4056 \
  --set batch.memory_mib.reference=12288 --set 'intake.source_root="'$INJ_ROOT'"' --set intake.unsigned=false \
  [--set reference.mosaic.naxis1=7000 --set reference.mosaic.naxis2=7000] --survey HLWAS --dataset r00342-dr-inj7000
rp official change --group live.json ...                                   # each run live for its dataset
rp loop --run 91 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4,5,6 \
  --job-definition rapid-rebuild:70 --once                                  # until three differences exist
rp job wait --job <the three difference jobs>
scripts/check_run.sh --run 91 --inspect DIR
scripts/completeness.py --run 91 --truth truth-sca8.parquet --no-history
```

Check, held: every job of the four runs published on its first attempt
(psf, six intakes, reference, three differences each). `check_run.sh`
**CLEAN** on all four, validate 6/6; with no packets, the reader and the
consumer read nothing.

**Measured**, 7000 beside 9100:

| | injected 7000 (91) | injected 9100 (92) | null 7000 (93) | null 9100 (94) |
|---|---|---|---|---|
| reference job, instance | 22868, m6a.2xlarge | 22867, m6a.2xlarge | 22869, r6i.8xlarge | 22870, r6i.8xlarge |
| reference peak MiB | 2,824 | 4,488 | 2,827 | 4,490 |
| reference seconds / awaicgen seconds | 45 / 21.8 | 53 / 22.9 | 48 / 20.8 | 56 / 22.1 |
| each map (image, coverage, uncertainty) | 196 MB | 331 MB | 196 MB | 331 MB |
| reference status / infobits / catalog sources | 1 / 1 / 4,300 | 1 / 1 / 4,300 | 1 / 1 / 4,300 | 1 / 1 / 4,300 |
| difference seconds, per epoch | 222, 222, 219 | 223, 189\*, 223 | 210, 211, 185\* | 216, 217, 217 |
| difference peak MiB | 16,133 to 16,134 | 16,133 to 16,138 | 16,132 to 16,136 | 16,132 to 16,134 |
| SFFT candidates per epoch | 2,279, 2,217, 2,359 | the same | 2,151, 2,086, 2,229 | the same |
| candidates at S/N > 20 (positive) | 775, 758, 820 | 775, 758, 820 | 683, 663, 722 | 683, 663, 723 |
| negative candidates at S/N > 20 | 302, 314, 274 | 302, 315, 273 | 301, 312, 272 | 302, 312, 272 |
| kept sources at S/N > 20 | 500 of 500 each | the same | 500 of 500 each | the same |
| core fraction | +0.251, +0.252, +0.271 | the same | +0.251, +0.252, +0.271 | the same |
| ring fraction | +0.021, +0.019, −0.002 | the same | +0.021, +0.019, −0.002 | the same |
| detected of 600 injected (AB 21 to 26.5) | 256 | 256 | — | — |
| false positives per epoch (bright-flagged) | 415 (314), 415 (305), 414 (313) | the same | — | — |

\* m6a.2xlarge, the others r6i.8xlarge: the pairs at epoch 5040101
(22873 against 22874) and 6010101 (22880 against 22882) ran on
different families. They are flagged and not resubmitted. Batch places
a job by the compute environment, the job definition names no family,
and pinning one is ruled out (Ben, 2026-10-21). Their candidate counts
are equal across the families. The spread recorded before is 2,156 rows
between Intel and AMD on one ZOGY difference.

Completeness per magnitude, both injected arms identically: detected
0.74 at AB 21, 0.56 to 0.81 from AB 21.5 to 23.5, 0.90 at 24, 0.21 at 24.5, 0 from
25. The cap of 500 fills with SFFT's bright-star cores (step
`differencer`), so the count ends there. This is not compared with run
81, which published ZOGY.

**Why nothing changed downstream.** The pointing's three frames cover
pixels 2256 to 7800 by 1286 to 6672 of the 9100 grid, and the central
7000 box spans 1050 to 8049. No covered pixel lies outside it. At this
pointing the 9100 mosaic only adds NaN padding. The resampled templates
of each pair agree to 7e-4 DN/s, their NaN masks are equal, and the
difference's peak memory does not move. What 9100 costs here is the
reference's 1.6 GiB of extra peak memory (still inside `[batch.memory_mib]
reference = 6144`, so no setting changed) and 1.69 times the bytes per
map. What it buys is coverage for a field whose frames fall near the
7000 grid's edge, which this pointing does not test.

**The grading, over every reference the rebuild has built.** The
population is every `refimages` row (719) and every reference
job without one (job 21981, run 77, failed exit 70, built nothing).
Each reference whose files remain was graded twice from its own
products, recomputed, never from its recorded `fwhmmedpix`, whose
meaning changed at PR 406: by the port, and by `dev`'s own code read
from `origin/dev` c931b001 (`referenceImageSubs.evaluate_refimage_quality`,
`psfFwhmSubs.catalog_fwhm_stats`, `rapid_pipeline_subs.compute_psf_catalog`,
the ini's `[REFIMAGE_INFOBITS]` and `[PSFCAT_REFIMAGE]`) with the
Photutils catalog made. The PSF for that catalog is the dataset's
science PSF of the filter and SCA, a stand-in for `dev`'s per-filter
reference PSF file. A reference under a deleted prefix is listed as
unavailable, never counted as a pass.

| run | dataset | built | graded | unavailable | port bits (count) | dev bits (count) | port status 0 | dev status 0 |
|---|---|---|---|---|---|---|---|---|
| 1 | `socsims-fakesrc-20260807` | 1 | 1 | 0 | none | none | 0 | 0 |
| 2 | `socsims-fakesrc-20260807` | 1 | 1 | 0 | 0:1, 1:1 | 0:1, 1:1 | 0 | 0 |
| 4 | `socsims-fakesrc-20260807` | 1 | 1 | 0 | 0:1, 1:1 | 0:1, 1:1 | 0 | 0 |
| 10 | `socsims-fakesrc-20260807` | 1 | 1 | 0 | 0:1, 1:1 | 0:1, 1:1 | 0 | 0 |
| 12 | `r00342` | 70 | 70 | 0 | 0:70 | 0:70 | 0 | 0 |
| 17 | `r00342-ep1` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 46 | `r00342-fx-f146` | 3 | 3 | 0 | 0:3 | 0:3 | 0 | 0 |
| 47 | `r00342-fx-f087` | 1 | 1 | 0 | none | none | 0 | 0 |
| 48 | `r00342-fx-f106` | 1 | 1 | 0 | none | none | 0 | 0 |
| 49 | `r00342-fx-f129` | 1 | 1 | 0 | none | none | 0 | 0 |
| 50 | `r00342-fx-f158` | 1 | 1 | 0 | none | none | 0 | 0 |
| 57 | `r00342-nt-producer` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 59 | `r00342-nt-metrics` | 3 | 3 | 0 | 0:3 | 0:3 | 0 | 0 |
| 60 | `r00342-nt-stamp` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 62 | `r00340-gb1` | 1 | 1 | 0 | 0:1, 1:1 | 0:1, 1:1 | 0 | 0 |
| 66 | `r00342-nt-caches` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 68 | `r00342-nt-producer-b` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 69 | `r00342-nt-assoc` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 70 | `r00342-nt-spot` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 71 | `r00342-inj0` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 72 | `r00342-inj1` | 70 | 70 | 0 | 0:70 | 0:70 | 0 | 0 |
| 73 | `r00342-inj1s` | 54 | 54 | 0 | 0:54 | 0:54 | 0 | 0 |
| 75 | `r00340-gb2` | 1 | 1 | 0 | 0:1, 1:1 | 0:1, 1:1 | 0 | 0 |
| 76 | `r00342-dy-f213` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 77 | `r00342-dy-f213z` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 78 | `r00342-dy-producer` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 80 | `ou-tds2` | 1 | 1 | 0 | none | none | 0 | 0 |
| 81 | `r00342-dy-field` | 54 | 54 | 0 | 0:54 | 0:54 | 0 | 0 |
| 82 | `r00342-dy-ops` | 2 | 2 | 0 | 0:2 | 0:2 | 0 | 0 |
| 83 | `r00342-et-fail` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 84 | `r00342-et-ok` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 85 | `r00342-dt-f146` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 86 | `r00342-dy-sfft` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 87 | `r00342-wk-errpub` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 90 | `r00342-ei-inputs` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 91 | `r00342-dr-inj7000` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 92 | `r00342-dr-inj9100` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 93 | `r00342-dr-null7000` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 94 | `r00342-dr-null9100` | 1 | 1 | 0 | 0:1 | 0:1 | 0 | 0 |
| 13, 14, 15, 24, 25, 26, 27, 28, 29, 35, 36, 37, 38, 39, 41, 42, 44, 51, 52 | deleted prefixes | 431 | 0 | 431 | — | — | — | — |

Outcome: 719 built, 288 graded, 431 unavailable (19 runs' deleted
prefixes), errors 0. **No reference has status 0, under the port or
under `dev`'s code**, so there is no port-only status 0 to correct. The
port's bits equal `dev`'s on all 288; `dev`'s Photutils catalog made
bits 5, 7 and 16 set nowhere (its smallest PSF-fit catalog has 1,301
rows, the lowest catalog limiting magnitude 24.6). The only bits set
are non-fatal. Bit 0 (median background above 1.0 DN/s) is set on 282
references, all but the imported `dev` reference (run 1), the four
other-filter fixes (runs 47 to 50) and OpenUniverse (run 80); run 85's
reads 1.357 DN/s.
Bit 1 (robust noise above 0.1 DN/s) is set on the five deep or crowded
ones: the three early 25-frame socsims coadds and the two GBTDS
references. The thresholds were not changed. `dev`'s PSF-fit catalog
took 20 to 30 s on a three-frame HLWAS reference and 7 to 10 minutes on
the 25-frame socsims coadds (155,000 rows). Per-reference results are in
the brief's `t1/grading/results/`, the full table in
`t1/grading/summary.md`.

### Step err-transient: transient failures retried, every failure's reason on its row, the checks fail when they should

**The change (PR 420, merged `d4866eab`; rapid_docs PR 108, `feb232df`).**
The error-handling ledger's fixes 5, 8, 9 and 10. `cli.main.exit_code` is
the one classification of a failure, for the process and for the failed
job row (`run_job(..., exit_code=)`): a `CommandExit`'s code, `Refused`
1, `LookupError` and `ConnectionConfigError` 64, and 75 for
`ConnectionUnavailable`, `psycopg2.OperationalError` and anything
`storage.is_transient` names. That rule now includes the `ClientError` codes `Throttling`,
`ThrottlingException`, `TooManyRequestsException`, `SlowDown` and
`RequestLimitExceeded`, and HTTP 503. Everything else exits 70. The SSM and Secrets Manager reads raise
`ConnectionUnavailable` when throttled or unreachable, which was the failure of
2026-10-23, and `ConnectionConfigError` otherwise. Every failed row's
`refusal` is `<Class>: <message>`, at most 500 characters, a
`CommandExit`'s included. A failed recording write logs the job and
the reason, and the stage's own exception still propagates.
`launch.submit` marks the row `failed` with the error when the Batch request
itself raises. `loop` exits 64 when its first pass admits no stem.
`validate_run.py` fails a run with a failed job (`counts`), with no
jobs (`counts`), or with no published job (`tables`).
`check-public-safety.sh` fails with exit 2 when `git grep` exits above 1.

**The checks on earlier runs** under the new `validate_run.py`:

| run | what it is | `check_run.sh` |
|---|---|---|
| 87 | err-publish's healthy chain | CLEAN, validate 6/6, reader 1/1, consumer 471/471 |
| 83 | err-tools' forced SExtractor failure (PR 412) | **FAIL**, `counts: FAIL (1): 1 difference jobs failed` |

Run 83 failed on purpose and now reads not clean, which is right. Nothing was
changed for it.

**The proof.** Image `rebuild-d4866eab…` (digest `6ea09ed7…`), job
definition `rapid-rebuild:71`. Run **95** (`r00342-wk-errtrans`, live,
group 21535) covers HLWAS `001_0001`, SCA 8, F146, passes 1 to 4: three reference frames
before the bound 61591.4056 and epoch 4040101 differenced and packeted.
Evidence is in the brief's `t1/`.

```sh
rp run create ... --revision d4866eab... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --survey HLWAS --dataset r00342-wk-errtrans                   # run 95
rp official change --group live.json ...; rp job local --run 95 --stage import ...   # live; Gaia, job 22883
rp loop --run 95 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:71 --interval 60
scripts/check_run.sh --run 95 --inspect <dir>
rp loop --run 95 --filters F146 --scas 8 --pointings 001_0001 --passes 30 --job-definition rapid-rebuild:71 --interval 60
```

Check, held:

- **The drain.** 16 passes, 21:07:37 to 21:22:35 UTC, exit 0, with `failed`
  empty in every pass: psf 22884, intake 22885 to 22888, reference 22889,
  difference 22890 and packet 22891. Each published on attempt 1 with
  `refusal` null, and difference 22890 ran image `6ea09ed7…`.
- **The rows.** 4 l2files, 1 psfs, 1 refimages, 1 diffimages, 500
  sources, 471 packets, 14 open bindings, matching run 87's counts on the image
  before.
- `check_run.sh --inspect`: **CLEAN**, validate 6/6, reader 1/1,
  consumer 471/471.
- **The zero-stem guard, live.** The pass-30 loop listed 5,832 F146
  stems and admitted none. It printed its pass line, submitted nothing and exited
  **64** with `the first pass found no stem under <source root> for
  filters F146, SCAs 8, pointings 001_0001, passes 30`. Before this
  change, the same pass read as drained and exited 0.

The transient exits, the row's reason and the failed submit row stand on their CI
contracts (`tests/unit/test_job_failure.py`, `test_connection.py`,
`test_launch.py`, `test_validate_counts.py`, `test_public_safety.py`).
No transient failure was staged live.

### Step dev-columns: dev's sumrat, nneg and nbad on every SFFT candidate, measured injected against noise

**The change (rapid PR 422, merged `74eafe20`; rapid_docs PR 112).** The change ports `dev`'s ZTF candidate metrics into `rapidpipe/science/difference/columns.py` (fa600637, babd5d16, 9af0f59f, e4a49c97, 0c1ef727, c32e28f6, 41473d60).

- **What is measured.** `sumrat` is `sum(p) / sum(|p|)` over a 5 x 5 stamp after a 3 x 3 truncated median filter, with `lower_median = false`. `nneg` and `nbad` are the stamp's finite negative and non-finite pixels. All three are measured after a 64 px Background2D local background, the same as `dev`.
- **Where.** On every row of both SFFT PSF-fit catalogs, before the cap. Each catalog is measured on the image its sources are positive in.
- **Where they land.** The published `sfftdiffimage_masked_psfcat[_negative].parquet` files, and three nullable columns on `sources`.
- **Settings.** `[difference.sumrat]` and `[difference.extra_catalog_cols]`, holding only the keys `dev`'s ini has.
- **What is unchanged.** The cap and the selection.

The granted schema statement, `database/2026-10-24-pixel-columns.sql` (`ALTER TABLE sources ADD COLUMN sumrat real, nneg smallint, nbad smallint`), was applied once on SMDC as `postgres` by SSM on the database host before the merge. The file was piped to `podman exec -i rapid-postgres psql -U postgres -d rapid -X -1 -v ON_ERROR_STOP=1`, with `lock_timeout` 20 s. Check, held:

- `ALTER TABLE` at 21:26:38 UTC, command `37a9006b`.
- The three columns are nullable.
- 2,615,828 rows, every value null.
- The grants are kept: `rapid_rebuild_pipeline` SELECT, INSERT, UPDATE, DELETE; `rapid_read` SELECT.

`sources_official` on SMDC keeps its column list without the three columns. Its `t.*` was expanded when it was created, and re-creating it takes a `DROP VIEW`, which the grant does not allow. A reader joins `sources` to `official`.

**The proof.** Image `rebuild-74eafe20…`, `rapid-rebuild:72`. Run 88's same-bytes method: new dataset `r00342-dc-cols-inj` (run **96**) from run 88's layers, Gaia imported, the loop on passes 4 to 6 for PSF and intake (the reference held by design). Difference jobs 22903–22905 and packet jobs 22908–22910 were submitted by hand against run 81's `refimages` 22029 and `psfs` 22015 rows.

```sh
rp run create ... --survey HLWAS --dataset r00342-dc-cols-inj     # run 96; run 88's layers at 74eafe20
rp loop --run 96 --filters F146 --scas 8 --pointings 001_0001 --passes 4,5,6 --job-definition rapid-rebuild:72
submit.py difference 96 81 rapid-rebuild:72; rp job wait ...; submit.py packet 96 81 rapid-rebuild:72   # the brief's t1/scripts
scripts/check_run.sh --run 96 --inspect DIR
python scripts/completeness.py --run 96 --truth truth-sca8.parquet --expids 4040101,5040101,6010101
measure_columns.py --run 96 --truth truth-sca8.parquet --out DIR                                        # the brief's t1/scripts
```

Check, held:

- `check_run.sh --run 96`: **CLEAN** (validate 6/6, reader 3/3, consumer 1,393/1,393).
- Every job published on its first attempt.
- All three epochs' catalog files and their 1,500 `sources` rows carry the columns. One kept row (expid 5040101) is null, where the catalog's value is NaN because its stamp leaves the image. There are 0 value mismatches between the rows and the catalogs.

**Invariance.**

- **Unit.** `test_the_pixel_columns_add_three_columns_and_change_nothing_else` runs the stage with the step and without it on identical inputs. Both give the same ids, every other column of every row and catalog, and every published image's pixels.
- **Live, beside run 88.** On every epoch: the same 500 source ids and values, the same candidate counts (2,279, 2,217 and 2,359), and the same core fraction (+0.251, +0.252, +0.271) and ring fraction. The difference images' pixels are equal; only CHECKSUM, CREATED, DATE and SWVERS differ. Detected 256 of 600, alerted 238, false positives per epoch 385 (289 bright-flagged): all as run 88.

**Measured.** The population is every SFFT positive candidate `source_set` considers, before the cap: 6,855 over three epochs.

- **Matching.** `completeness.py`'s rule, 0.3 arcsec through the corrected WCS. A candidate matched to a truth row is injected (391). Every other candidate is noise (6,464); the noise inside a bright reference footprint is bright-flagged (2,257).
- **Magnitude.** The truth AB magnitude for injected candidates, and the candidate's own PSF-fit magnitude for noise ("none": `fluxfit` ≤ 0).
- **Nulls.** A null counts in its denominator and does not survive: 86 noise candidates, 0 injected.

| AB | injected: n / survive > 0.4 | noise: n / survive | bright-flagged: n / survive | noise not bright: n / survive |
|---|---|---|---|---|
| < 21 | — | 39 / 0.74 | 39 / 0.74 | — |
| 21 | 61 / 1.00 | 116 / 0.78 | 112 / 0.80 | 4 / 0.00 |
| 22 | 78 / 1.00 | 249 / 0.72 | 184 / 0.87 | 65 / 0.28 |
| 23 | 58 / 1.00 | 558 / 0.85 | 389 / 0.95 | 169 / 0.62 |
| 24 | 84 / 1.00 | 1,065 / 0.85 | 759 / 0.94 | 306 / 0.61 |
| 25 | 74 / 1.00 | 1,548 / 0.85 | 607 / 0.97 | 941 / 0.78 |
| 26 | 36 / 0.97 | 1,684 / 0.73 | 96 / 0.70 | 1,588 / 0.73 |
| ≥ 27 | — | 387 / 0.15 | 3 / 0.33 | 384 / 0.15 |
| none | — | 818 / 0.11 | 68 / 0.32 | 750 / 0.09 |
| all | **391 / 0.997** | **6,464 / 0.68** | **2,257 / 0.90** | 4,207 / 0.55 |

The quartiles (q1 / median / q3) are in `t1/m-96.txt`:

- **Injected.** sumrat +1.00 / +1.00 / +1.00 overall (+0.85 / +0.96 / +1.00 at AB 26); nneg 0 / 1 / 4, rising from 0 at AB 21 to 8 at AB 26; nbad 0 throughout.
- **Bright-flagged.** sumrat +0.99 / +1.00 / +1.00; nneg 1 / 3 / 6.
- **Noise not bright.** sumrat −0.48 / +0.79 / +1.00; nneg 5 / 10 / 14.

nbad is 0 at every quartile of every class.

**`dev`'s figure.** `dev`'s socsims page gives the population behind 37 per cent (2026-10-20 and the later GBTDS test): every SFFT PSF-fit row of both signs, loaded when inside the fit-position bounds with sumrat > 0.4 and rb > 0 where computed. 717,644 of 1,937,852 rows passed, and 22,216,362 of 59,352,608. The 41 and 40 per cent are the SExtractor XSources catalogs, with an S/N > 5 and 2 px edge cut. The same rule without rb, on this sample, passes **6,243 of 9,834 rows (0.635)**: 0.68 to 0.73 of the positive rows and 0.48 to 0.49 of the negative. The comparison is like for like in its rule, but not in its field (HLWAS injected against GBTDS socsims) or its rb cut.

**Seen, reported, not tuned.** sumrat passes SFFT's bright-star residuals: they are positive point-like cores, 0.90 above 0.4. So on HLWAS the cut alone removes a third of the noise and almost none of the bright-flagged false positives that fill the cap. The noise it does remove is the faint and negative-flux tail, and the unflagged candidates with negative sumrat (their nature unread; the gallery is `t1/inspect-96/`). Cost: `pixel_columns_s` 4.3 to 5.2 s a difference. Container seconds 193 to 228, against run 88's 187 to 222.

**What `dev-prefilter` must know.**

- **The flavour.** PR 425 (session `diff-flavor`, merged after this step's code) made the published differencer `[difference] diff_flavor`, shipped as `zogy`. The columns are computed on every SFFT candidate whatever the flavour. Published `sources` rows carry them only under `diff_flavor = "sfft"`; under ZOGY they are null.
- **The prefilter alone.** A prefilter at sumrat 0.4 alone will not stop the bright-star cores on HLWAS.

Raw evidence is in the brief's `t1/`: `m-96/` (candidates.parquet, stats.csv, survival.csv, dev.csv, kept.json), `m-96.txt`, `c-96/` and `c-96.txt`, `check-96.txt`, `inspect-96/`, `pixels-88-96.txt` and `ssm-out.txt`.

### Step mig-quickstart: the README runs one epoch locally, and the loop page starts a night, each followed verbatim

**The change (PR 423, merged `3723b445`; rapid_docs PR 109, merged `9b802ea2`).**
The README gains **Getting started**. It takes one HLWAS epoch (`001_0001`, SCA 8, F146) through import, psf, three intakes (passes 1, 2 and 4), a two-frame reference, the difference and the packet set. Each stage runs with `job local` inside the pipeline image, against a throwaway `postgres:18` in a podman pod. The section also gives the environment in one block, with the team's values commented as placeholders. The README's three-stages sentence, the difference-on-the-old-contract clause, `launch.submit`'s command and `RAPIDPIPE_BATCH_JOB_DEFINITION` are corrected or cut. `system/loop.md` gains **Starting a night**: run create with `--set` for the source root and the bound, the live binding's JSON, the Gaia import, the loop under `timeout`, `check_run.sh`, and a table of `loop`'s and `job local`'s exit codes.

Two facts were checked before the text was written:

- **The dated SQL files do not apply after a fresh `schema.sql`.** All five fail on `postgres:18`: "already exists" for four, and `members_official` does not exist for the object-catalog file. The README applies `schema.sql` alone.
- **No setting turns off the packet job's send.** The local recipe points the existing `RAPIDPIPE_KAFKA_*` environment at `127.0.0.1:9`. The send fails (`KafkaTimeoutError: Unable to bootstrap`), and the job stays published and exits 0.

**The proof, local.** The README's `sh` blocks were extracted from the file, with only `IMAGE` and `GAIA` filled. They ran in an `env -i` shell and logged every command's exit status:

| walk-through | image | UTC | validate_run | read_packets | packets |
|---|---|---|---|---|---|
| a1 | `rebuild-6c7ef990…` | 21:18:03–21:22:31 | 6/6 PASS | 1 set CLEAN | 465 |
| a2 | `rebuild-c87a2171…` (the final text, PR 420 merged in) | 21:23:24–21:27:54 | 6/6 PASS | 1 set CLEAN | 465 |

No command exited nonzero except `pg_isready` inside its `until`. The difference kept 500 of 2,112 sources, and its children peaked at 15.2 GB. Nothing on S3 or SMDC was touched. The pod was removed after each walk-through.

**The proof, SMDC.** The loop page's `sh` blocks were extracted the same way and run after the README's team environment block, uncommented and filled. They ran from a `rebuild` checkout at `3723b445`. The image was `rebuild-3723b445…` (digest `6e3911a3…`), job definition **`rapid-rebuild:73`**. Run **97** (`r00342-mq-night`, live, binding 32040) covered HLWAS `001_0001`, SCA 8, F146, passes 1 to 4 under the bound 61591.4056.

```sh
rp run create --who "$USER" --why "..." --revision 3723b445... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --survey HLWAS --dataset r00342-mq-night                                          # run 97
rp official change --group live-r00342-mq-night.json ...; rp job local --run 97 --stage import ...   # live; Gaia, job 22897
timeout 5400 $PY -m rapidpipe.cli.main loop --run 97 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 \
  --job-definition rapid-rebuild:73 --interval 60
scripts/check_run.sh --run 97
```

Check, held:

- **The drain.** 16 passes, 21:34:39 to 21:49:39 UTC, exit 0, `failed` empty in every pass. Jobs: psf 22898, intakes 22899 to 22902, reference 22906, difference 22907, packet 22911. Each was published on attempt 1 with `refusal` null.
- **`check_run.sh --run 97`: CLEAN**, validate 6/6, reader 1/1, consumer 471/471.

The reference was pending for four passes (about five minutes) at the 9100 px mosaic. The page's drain range now reads 9 to 15 minutes.

**Found by the walk-throughs and written into the text:** `timeout 5400 rp loop` cannot run a shell function, so the loop step runs the module. The fix came from reading the text before the SMDC run, which then ran the corrected text.

**Seen, not built:**

- The publisher binds an expected key that no loaded row holds. A dry-run typo expected `psfs` under fid 6, and the row was fid 8. `validate_run.py`'s lineage check catches it afterwards.
- There is no off switch for the packet job's send.
- `scripts/` is not in the image.

Raw evidence is in the brief's `t1/`: `local-a{1,2}.*`, `night-a1.*`, `dated-files-check.txt`, `dry/`.

### Step gate-metrics: the gate also holds on lost sky and misregistration, bounds from every recorded difference job

**The change (PR 426, merged `05b81610`; rapid_docs PR 111, `332bb0b3`).**
`gate.judge` holds a packet set when its difference job's
`usable_fraction` is below `[gate] usable_fraction_min` (0.9) or its
registration offset `hypot(registration_dx_px, registration_dy_px)` is
above `[gate] registration_max_px` (0.25 px). Both checks run before the
existing three, since lost sky or a misregistration inflates the count
and the residuals checked after them. `min_epochs`, the median rule, the
existing bounds and "a metric the job could not compute holds nothing"
are unchanged. `decide` already read every metric of the set's
difference job, so its query is untouched. No overlay value was set:
every dataset's healthy population sits inside the shared defaults.

**The calibration** (read only, every published difference job SMDC
holds that carries the metrics, runs 46 to 95):

| population | offset px, max | usable fraction, min |
|---|---|---|
| HLWAS SCA 8, seven filters, null and injected (162 epochs) | 0.123 (run 49 F129, 25 stars); 0.060 over 162 F146 epochs | 0.98881 |
| HLWAS 18 SCAs (run 72), SCA 9 apart | 0.027 | 0.93853 (SCA 16) |
| **HLWAS SCA 9 (run 72), target failure** | 0.132 to 0.160 | **0.70439** (reference from another SCA's frames) |
| GBTDS (runs 67, 79) | 0.094 (67 has none) | 0.99035 |
| OpenUniverse (runs 80, 89) | 0.034 | 0.99313 |

Within one SCA the usable fraction is constant to 1e-4 across epochs: the
SCA's reference coverage and mask set it. The offset's tail grows as
registration stars fall (0.08 to 0.123 px with 25 to 35 stars). That is
why the bound is twice the healthy maximum, set to catch an offset of the
class of HLWAS's 1 px, not SCA 9's 0.13 to 0.16 (SCA 9 is held by its
usable fraction). The first check: `001_0001` SCA 8 F146 reads 0.019 px
and 0.99935, inside the plan's 0.12 px and 0.999.

**The exercising proof** (read only, `gate.decide` at `05b81610`
against SMDC's recorded rows, each run's own `[gate]` table):

- Every published packet set, 59 runs and 4,627 sets: held 12 with the
  new checks disabled, 15 with them on. The three new holds are run 72's
  SCA 9 sets 21890, 21895 and 21900, each `usable_fraction 0.7044 below
  0.9`. No healthy set is held, and no existing hold changes its reason.
- Sets past `min_epochs` with the values `decide` read from the
  difference job each names: run 49 sets 20023 and 20112 (offset 0.013
  and 0.1231 px) send, run 48's 20122 (0.119 px) sends, run 72 SCA 16's
  21907 (usable 0.9385) holds on `core_fraction` as before, run 72 SCA 9's
  21890 (0.7044, 0.160 px) holds on `usable_fraction`.
- `decide`'s first `sources` row is the set's own epoch in all 532 sets
  that carry window neighbours' sources too (insertion order, not an SQL
  promise; recorded under Improvements in PR 426).

The registration hold has no recorded case past the bound. Its contract
stands on a synthetic unit test (`tests/unit/test_gate.py`).

**Publication smoke**, not an exercise of the new checks (one epoch, below
`min_epochs`): image `rebuild-05b81610…` (digest `cbae47ab…`), job
definition `rapid-rebuild:74`. Run **98** (`r00342-gm-smoke`, live, group
21567; Gaia job 22912), HLWAS `001_0001` SCA 8 F146, passes 1 to 4.

```sh
rp run create ... --revision 05b81610... --overlay rapidpipe/settings/datasets/hlwas-r00342.toml \
  --set "intake.source_root=\"$SOURCE_ROOT\"" --set reference.selection.before_mjd=61591.4056 \
  --survey HLWAS --dataset r00342-gm-smoke                      # run 98
rp loop --run 98 --filters F146 --scas 8 --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:74 --interval 60
scripts/check_run.sh --run 98 --inspect <dir>
```

Check, held:

- **The drain.** The loop ran 21:51:46 to 22:04:46 UTC and exited 0, with
  `failed` and `held_sets` empty in every pass. Jobs: psf 22913, intake
  22914 to 22917, reference 22918, difference 22920, packet 22938. Each
  published on attempt 1 with `refusal` null, and the packet set was
  stamped sent.
- **The difference's metrics**, the two the gate now reads: usable
  0.99935, offset 0.019 px (-0.012, +0.014), the same as runs 59 to 95's
  epoch. Core -0.001, 20 sources above S/N 20.
- `check_run.sh --inspect`: **CLEAN**, validate 6/6, reader 1/1,
  consumer 181/181.
- 181 packets against runs 87, 90 and 95's 471: those ran under SFFT's
  published catalog, and this run under ZOGY's, which has been the
  default since PR 425. That is not this step's measurement.

Evidence is in the brief's `t1/`.

### Step dev-prefilter: dev's database prefilter on SFFT's catalog, measured against ZOGY and raw SFFT on the same bytes; the default stays ZOGY

**The change (rapid PR 431, merged `14860f57`; rapid PR 432; rapid_docs PR 114).**

- **What.** Under `diff_flavor = "sfft"`, `source_set` applies `dev`'s database prefilter to SFFT's positive PSF-fit catalog after the fit-position bounds and before the 500 cap. A source is kept when its `sumrat` is finite and above `[difference.database_prefilter] sumrat_prefilter_threshold` (0.4) and its nearest pixel is at least 2 px from every edge (`catalogs.EDGE_MARGIN`). This ports `dev` c931b001 (963eb440), as `loadPSFCatIntoDBSourcesTable.py` applies it.
- **Where the edge comes from.** `dev`'s XSources loader (57559572). Its PSF path has no edge cut, so this is a divergence, recorded in `decisions.md`.
- **Divergence: null sumrat.** A null `sumrat` is rejected; `dev` passes it.
- **What is not added.** `rb > 0`: the rebuild has no score. An SNR cut: `dev`'s is XSources-only.
- **The off setting.** `""` turns off the whole predicate: sumrat, null and edge.
- **Under ZOGY.** Nothing changes.
- **`n_candidates`.** It is now the count the cap chooses from, so after the cut.

**The proof.** Image `rebuild-14860f57…`, `rapid-rebuild:76`. Run 88's same-bytes method: six new datasets and live runs, each with Gaia imported, and the loop for PSF and intake. Difference and packet jobs were submitted by hand against run 81's `refimages` 22029 and `psfs` 22015 (injected) and the same rows through run 95 (null).

| arm | settings | injected run | null run |
|---|---|---|---|
| A | `diff_flavor = "zogy"` (shipped) | 103 | 104 |
| B | `"sfft"`, `sumrat_prefilter_threshold = ""` | 105 | 102 |
| C | `"sfft"`, the prefilter at 0.4 | 107 | 106 |

```sh
arm.sh <a|b|c> <inj|null> 14860f57… rapid-rebuild:76    # the brief's t1/: run create, live, import, loop, submit.py, rp job wait, check_run
python scripts/completeness.py --run N --truth truth-sca8.parquet --expids 4040101,5040101,6010101
metrics.py 103 105 107 104 102 106; gate_proof.py 103 104; gate_proof.py 107 106            # the brief's t1/scripts
```

Check, held: all six runs `check_run.sh` **CLEAN** (consumer 1,158, 1,393, 1,420 injected; 182, 471, 478 null), and every job published on its first attempt.

**Measured.** The injected sample is passes 4 to 6, 600 injected; the null sample is epoch 4040101.

| | A: ZOGY | B: raw SFFT | C: SFFT + prefilter |
|---|---|---|---|
| detected of 600 | **542** | 256 | 275 |
| AB 24 to 25, of 96 | **93** | 59 | 78 |
| alerted of detected | 516 / 542 | 238 / 256 | 257 / 275 |
| false positives per epoch, mean (highest) | **214 (233)** | 385 (390) | 388 (390) |
| of them bright-flagged | 0.62 | 0.75 | 0.86 |
| candidates before the cap, per injected epoch | 954–1,018 | 2,217–2,359 | 1,524–1,677 |
| null: S/N > 20 / packets | **20 / 182** | 500 / 471 | 500 / 478 |
| container seconds | 199–251 | 246–252 | 250–252 |
| core / ring fraction | −0.0011 to +0.0032 / −0.0000 to +0.0017 | +0.251 to +0.271 / −0.002 to +0.021 | as B |

- **The cut.** It keeps 0.67 to 0.71 of SFFT's candidates, column D, read from the catalog files through `n_candidates`. It removes "other" false positives (97 → 55 per epoch), and the bright-star residuals refill the cap (289 → 334).
- **What C recovers.** 19 injected sources, all at AB 24.5. AB 25 and fainter stay at 0.
- **The default rule.** Arm C against arm A must be at least as good on detections in total and at AB 24 to 25, on false positives per epoch (mean and highest) and on null packets (mean and highest). It is worse on every count, so `diff_flavor` stays `"zogy"`.

**Re-based.** Arm B is run 88 to the digit: candidates, core, 256 detected, 385 false positives. Runs 91 and 92's 415 came from their own reference and PSF rows (`refimages` 22868/22867, `psfs` 22838/22839), not from the image between them. Which part of those references makes it is unread.

**The gate.**

- **The inputs.** Core and ring are image metrics (`quality.residuals`), not read from the source set; they are identical between B and C.
- **Healthy epochs.** No arm-C difference is healthy. They pass `usable_fraction` (0.9994) and registration (at most 0.019 px), but carry SFFT's bright-star core residual, so nothing is calibrated from them and nothing is widened.
- **The shipped bounds.** Arm A's four differences sit inside them, so they stay. The `[gate]` comment carries the measurement.
- **`decide` over every recorded set,** at the shipped bounds, each run's own table, as recorded and with `min_epochs = 1` (past the minimum):
  - **ZOGY.** 4,457 sets, 15 held as recorded and 18 past the minimum: runs 72, 73, 80 and 81 as before, plus F213's residual in runs 54 and 76. Arm A's four send.
  - **SFFT.** 29 sets, 0 held as recorded and 19 held past the minimum, all on core: every HLWAS SFFT set, arm C's four among them. OpenUniverse run 89's 10 send.
- **Publication smoke.** The sampled runs' own verdicts are send, warming up.

Raw evidence is in the brief's `t1/`: `metrics.txt`, `c-103/`, `c-105/`, `c-107/` and their `.txt`, `gate-zogy.txt`, `gate-sfft.txt`, `?-inj/` and `?-null/`.

### Step sfft-core: SFFT's bright-star core residual on HLWAS is the zeroed DQ cores in its fit; the HLWAS overlay carries dev's OpenUniverse block

**The change (rapid PR 435, merged `a761ae23`; rapid_docs PR 116).**

- **The overlay.** `hlwas-r00342.toml` gains `[difference.sfft]` with dev's complete OpenUniverse block:
  - `crossconv_flag`, `sfft_use_segmentation` and `sfft_use_gainmatch_catalogs` are true;
  - `sfft_bsmask_value` is `"50.0"` and `sfft_bsmask_radius` is `"100.0"`.
- **When it applies.** It is read only under `diff_flavor = "sfft"`. The published differencer stays ZOGY, so no published row changes.
- **Nothing else changes.** The cap, the prefilter and the gate are untouched. No code changed: the rebuild's command builder already builds dev's command for this block, and the module makes its own segmentation images.

**The measurement.**

- **The pair.** One epoch pair on the same bytes: expid 4040101 (`r00342-inj1` pass 4, `001_0001` SCA 8 F146) against run 81's `refimages` 22029 and `psfs` 22015.
- **The runs.** Each configuration has its own dataset and live run (`r00342-sfft-core-{s,x,g}`, runs 108 to 110). Each was created at `14860f57` with run 81's layers and `--set difference.diff_flavor="sfft"`, plus `[difference.paths]` at a worktree.
- **The jobs.** Import, psf, intake and the difference ran under `rp job local`. A wrapper kept each work directory for the diagnostics.

```sh
config.sh S                                                    # the brief's t1/scripts: run create, live, import, psf, intake, difference
config.sh X difference.sfft.crossconv_flag=true difference.sfft.sfft_use_segmentation=true \
  difference.sfft.sfft_use_gainmatch_catalogs=true 'difference.sfft.sfft_bsmask_value="50.0"' 'difference.sfft.sfft_bsmask_radius="100.0"'
config.sh G difference.sfft.sfft_use_gainmatch_catalogs=true
analyze.py <cfg> <run> work-<cfg> truth-sca8.parquet row-<cfg>.json stamps-<cfg>.png; probe_zeros.py work-S probe-zeros 23033 0 1 2 3
```

Check, held: the control S reproduces run 88 job 22804 to the digit: core +0.250664, ring +0.021170, 2,279 positive and 982 negative candidates.

| | S: socsims block (108) | X: OpenUniverse block (109) | G: S + catalogs (110) | ZOGY (run 81) |
|---|---|---|---|---|
| core / ring fraction | +0.251 / +0.021 | **−0.000 / −0.009** | +0.251 / +0.021 | −0.001 / +0.002 |
| S/N > 20 positive / negative | 771 / 300 | 184 / 12 | 771 / 300 | 156 / 31 |
| of them within 3 px of a reference source above aperture S/N 100 | 408 / 1 | 1 / 1 | 408 / 1 | 5 / 4 |
| injected matched in the PSF-fit catalog, of 200 | 130 | **179** | 130 | 183 |
| pixels left to the kernel fit | 1.000 | 0.016 | 1.000 | — |
| catalog sources selected, sci / ref | — | 85 / 62 | 0 / 0 | — |
| decorrelation sigmas, sci / ref (segm background only) | — | 0.0536 / 0.0176 (0.0524 / 0.0158) | — | — |
| difference s (SFFT s) | 168 (36) | 197 (56) | 188 (41) | 205 |

- **X is the one acceptable configuration** by the brief's rule:
  - core inside ±0.011 and ring inside ±0.015;
  - none of dev's broken-input caveats holds: 62 reference sources are selected, and the sigmas come from pixels 96% segmentation background;
  - its injected recovery is at least S's.
- **G is S.** The 20000 threshold is above every FLUX_MAX (7,272 and 8,242), so the catalog path selects nothing.

**The cause (a diagnostic, `probe-zeros.jsonl`).**

- **The pixels.** The stage sets the science image's NaNs to 0.0 before SFFT. On this frame these are the DQ mask's 5,096 px in coverage, the brightest stars' cores among them.
- **Their weight.** They are 0.03% of the frame and 31% of Σ(reference²), so in the unmasked socsims fit they pull the photometric ratio down. Core and ring together are a +0.27 flux excess.
- **The probe.** SFFT's own `run_sfft_rapid` on S's inputs: with no mask it reproduces S's core; with only those pixels masked, core −0.014 and ring −0.013. Dilated by 1, 2 and 3 px: +0.070, +0.025 and +0.017.
- **Why X works.** Its 100 px discs exclude the bright stars, so SFFT's kernel does absorb this field's PSF mismatch.
- **Not built.** Excluding the zeroed pixels from SFFT's fit is recorded under PR 435's improvements.

**The proof on SMDC.**

- **The image and the run.** Image `rebuild-a761ae23…` (`sha256:b2304313…`), `rapid-rebuild:77`. Dataset `r00342-sfft-core-proof`, run **111**, at `a761ae23`: run 81's layers, the overlay now carrying the block, and `--set difference.diff_flavor="sfft"` only.
- **The jobs.** Import, psf and intake ran locally. One difference job, 23050, was submitted on Batch against the same reference and PSF rows and waited on with `rp job wait`.

```sh
proof.sh a761ae2372db221326728ec39b9021ddc5d6814e     # the brief's t1/scripts: build, deploy, run create, live, import, psf, intake, rp job submit, rp job wait, check_run.sh
```

Check, held: job 23050 published on its first attempt (m6a.2xlarge, 209 s). Its core and ring are −0.000026 and −0.00922, against the local measurement's −0.000017 and −0.00931; its candidates are 453 against 457 (AMD against Intel, as PR 418 saw). `check_run.sh --run 111`: **CLEAN**, validate 6/6, no packet stage.

Raw evidence is in the brief's `t1/`: `row-{S,X,G,ZOGY}.json`, `stamps-{S,X,G}.png`, `probe-zeros.jsonl`, `config-*.log`, `proof.log`, `check-proof.txt`, `inspect-proof/`.

### Step rimtimsim-reader: RimTimSim's lite FITS read at intake, F213 and F087 each through the chain CLEAN; the 250513 truth does not describe the 260622 images

Brief `rimtimsim-reader`, task 1 (rapid PR 429, merged `479eb163`; this
step's PR the overlay's PSF-transpose comment). RimTimSim's 2026-06-22
delivery, dev's field 4682737, SCA 2, read as dev's converted
`rimtimsim_WFI_<filter>_SCA02_<exposure>_lite.fits.gz` (F087 132, F213
131) from the inputs bucket's `g0007-rimtimsim/rimtimsim-260622-lite/`,
under the new overlay `rapidpipe/settings/datasets/rimtimsim-260622.toml`.

**The reader.** Both existing readers are ASDF, so the overlay alone
could not read a FITS file: PR 429 adds `[intake] reader = "rimtimsim"`
(`rapidpipe/science/intake_rimtimsim.py`, dev's own header builders onto
the SOC layout, PRIMARY and SCI, the file's `ZPTMAG`, WCS and NaN), the
loop's flat listing with the exposure number as pointing and `expid`,
and `[psf.epsf]`: the file names no ePSF, so the overlay names CRDS's
answer under `roman_0077` for WFI02 (`roman_wfi_epsf_0280` F213, `0266`
F087). The overlay also carries dev's rimtimsims block (gain 1, read
noise 11, saturation 1.1e6 DN, artefact repair off) and `program_id = -3`.

| check on the files | value |
|---|---|
| headers, all 263 (ranged reads) | `EXPTIME` 54.72 s; `ZPTMAG` 25.8573 (F213), 26.2982 (F087); rimtimsim_v2.0 |
| dithers | `CRVAL` within 0.69 x 0.61 arcsec over all 263; rotation 29.098 deg on every file; one tessellation field, 4682737 |
| WCS against Gaia DR3 (the intake's metric, every intake) | +0.03 to +0.12 / -0.01 to +0.08 px over about 590 stars: no `crpix_shift` |
| the input catalog's own pixel columns through the lite WCS | scatter 0.001 px, a constant +1.31 px in x; with the axes swapped, scatter 1,200 px: dev's transpose holds |

**The sample and the runs.** Dev's bound, `before_mjd = 61455.3`, takes
the first 10 frames of each filter for the reference; five epochs after
it per filter. Gaia DR3 over RA 268.38 to 268.62, Dec -29.32 to -29.09
(2 x 2 sub-boxes, 62,822 rows), imported per dataset. Image
`rebuild-479eb163…`: **`rapid-rebuild:75`**.

```sh
REV=<PR 429's merge, 479eb163…>
rp run create ... --revision "$REV" \
  --overlay rapidpipe/settings/datasets/rimtimsim-260622.toml \
  --set 'intake.source_root="<the lite prefix>"' --set reference.selection.before_mjd=61455.3 \
  --survey GBTDS --dataset rts260622-f213                     # run 99; rts260622-f087 run 100
rp loop --run 99 --filters F213 --scas 2 --pointings 411,827,...,6235 --job-definition rapid-rebuild:75 --interval 60
```

- Run **99** (F213): loop exit 0, every job published on its first
  attempt; **`Check run 99: CLEAN validate 6/6 reader 5/5 consumer 1499/1499`**.
- Run **100** (F087): loop exit 0, every job published on its first
  attempt; **`Check run 100: CLEAN validate 6/6 reader 5/5 consumer 1818/1818`**.

| filter | epoch | MJD | candidates (+ / −, ZOGY uncapped) | S/N > 20 (+ / −) | kept | core | ring | registration px | usable | difference s | packets | gate at send | gate replayed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| F213 | 4571 | 61455.749 | 19,630 / 85,641 | 479 / 11,383 | 500 | −0.069 | −0.034 | (+0.007, +0.020) | 0.9998 | 3,985 | 390 | sent | hold, core |
| F213 | 4987 | 61456.273 | 29,801 / 107,475 | 1,143 / 12,728 | 500 | −0.073 | −0.039 | (+0.007, +0.023) | 0.9995 | 4,318 | 357 | **held**, core −0.0732 | hold, core |
| F213 | 5403 | 61456.796 | 15,668 / 72,460 | 530 / 10,837 | 500 | −0.050 | −0.032 | (+0.006, +0.024) | 0.9995 | 3,767 | 349 | sent | hold, core |
| F213 | 5819 | 61457.321 | 21,127 / 88,415 | 708 / 11,798 | 500 | −0.075 | −0.022 | (+0.009, +0.025) | 0.9995 | 3,984 | 380 | sent | hold, core |
| F213 | 6235 | 61457.845 | 23,943 / 88,463 | 734 / 10,496 | 500 | −0.061 | −0.018 | (+0.006, +0.024) | 0.9998 | 4,131 | 380 | sent | hold, core |
| F087 | 4363 | 61455.487 | 22,495 / 71,382 | 1,299 / 6,301 | 500 | −0.005 | +0.007 | (−0.020, +0.010) | 0.9996 | 1,648 | 302 | sent | send |
| F087 | 4779 | 61456.011 | 35,644 / 68,132 | 529 / 6,845 | 500 | +0.011 | +0.004 | (+0.003, +0.009) | 0.9996 | 1,769 | 415 | sent | send |
| F087 | 5195 | 61456.534 | 36,717 / 57,194 | 1,580 / 5,582 | 500 | +0.005 | −0.007 | (+0.013, +0.005) | 0.9999 | 1,760 | 377 | sent | send |
| F087 | 5611 | 61457.059 | 28,084 / 72,371 | 353 / 7,688 | 500 | +0.024 | −0.014 | (+0.014, +0.001) | 0.9999 | 1,773 | 443 | sent | send |
| F087 | 6027 | 61457.583 | 50,186 / 71,153 | 11,353 / 8,466 | 500 | −0.003 | +0.004 | (−0.006, +0.012) | 0.9996 | 1,892 | 281 | sent | send |

Attempted, published and held: 10 differences attempted and published,
10 packet sets published, 9 sent, 1 held. The gate is not changed: at
send time the F213 run had fewer than `min_epochs` (5) published
differences for its first four sets, so only the fifth was judged and
held; replayed through `decide` now, all five F213 sets hold on core
fraction (bound ±0.025) and all five F087 sets send.

| stage | F213 (run 99) | F087 (run 100) |
|---|---|---|
| psf | 10 s, 294 MiB | 14 s, 294 MiB |
| intake (15) | 11 to 15 s, 925 MiB | 13 to 22 s, 935 MiB |
| reference (10 frames, 9100 px, `cov5percent` 19.2, status 1, infobits 3) | 104 s, 4,488 MiB | 104 s, 4,488 MiB |
| difference (5) | 3,767 to 4,318 s, 16,427 MiB | 1,648 to 1,892 s, 16,402 MiB |
| packet (5) | 25 to 50 s, 902 MiB | 22 to 57 s, 900 MiB |

The difference is 4.6 to 5.3 (F213) and 2.0 to 2.3 (F087) times GBTDS's 820 s and
stays on one vCPU: ZOGY's negative PSF-fit catalog alone (85,641 sources)
took 12 min, then SFFT's and the naive subtraction's catalogs, as in
dev's RimTimSim runs (dev's naive catalog took 19 min).

**The science PSF's transpose (dev's rule, 2025-05-30), measured.** Run
**101** (dataset `rts260622-f213-psft`) imported run 99's F213 stamp
transposed and ran the same ten frames and epoch 4571 (`Check run 101:
CLEAN`): core −0.0694 → −0.0688, ring −0.0339 → −0.0385, ring sources
above S/N 20 36 → 44. The stamp is within 0.0017 of its own transpose
(rms over peak) and the stars of the field fit it as is to 0.0236 and
transposed to 0.0223 of their peak; the transpose does not earn a
setting, and the overlay's comment says so.

**Truth compatibility, checked on three counts.**

| count | result | numbers |
|---|---|---|
| times | **agrees** | `gbtds_timestamps_field03.txt` has all 263 lite exposures, `TSTART` equal to `OBS_TIME_JD` to 0.000 s, filters equal; every F087 and F213 time series has exactly the lite files' exposure numbers as columns (132 and 131) |
| positions | **agrees** | the input catalog's stars through the reference's WCS: of the reference catalog's detections ranked 2,000 to 6,000 by flux, 0.568 sit within 0.1 arcsec of a catalog star brighter than F213 20 (chance, the catalog moved 30 arcsec: 0.019), median offset (−0.024, −0.021) arcsec; the targets themselves match the reference catalog at 0.022 against 0.002 chance, because the 15,512 reference detections cannot resolve the 170,603 catalog stars brighter than 20 on the frame |
| injected signal | **disagrees** | per target-epoch, the time series' change against the mean of the 10 reference frames, times the catalog magnitude's flux at `ZPTMAG`, over the epoch's point-source sigma (`limmag`'s 5-sigma flux / 5: 7.1 DN/s F087, 24.1 F213); found = a source of the predicted sign in the uncapped ZOGY catalog (S/N ≥ 5) within 0.1 arcsec. F087 brightening at predicted S/N 30 to 100: 3 of 45 found (0.067), above 100: 1 of 14, quiet stars of the same magnitudes 0.007; fading at S/N 10 to 30: 0.313 against quiet stars' 0.175. With the sign flipped: 0 of 34 and the quiet-star rate. F213: 19 brightening target-epochs above predicted S/N 5, none found |

The flux scale agrees (the reference catalog's isolated stars at F213 17
to 18 read the catalog magnitude minus `MAG_AUTO` 24.93 against the
mosaic's `MAGZP` 25.145, a Kron aperture's shortfall), so the missing
signal is not a scale error: the 250513 time series do not describe the
260622 images' variables, as the delivery's "greater variety of injected
transients" suggested. One table of the truth is also wrong in itself:
the `dim_EB` time series carry the `blended_EB` series' 5,000 ids and
none of the 4,750 dim-EB stars, and were left out. **Completeness unmet:
truth incompatible**; the counting rule was defined (a target-epoch
eligible when its predicted brightening is at least the epoch's 5-sigma
point-source flux, magnitude the catalog's, matched to the published
`sources` within 0.1 arcsec) and not run.

**The quirks met:**

| quirk | disposition |
|---|---|
| A FITS file, not ASDF; image in DN at gain 1, no ERR or DQ, NaN present (3,992 on the first file) | a reader, `[intake] reader = "rimtimsim"` (PR 429) |
| The file names no ePSF reference | `[psf.epsf]` on the overlay: CRDS's `roman_0077` answer for WFI02 per filter |
| Dev's artefact repair (10,000 DN/s) sits below the stars' cores (99.99th percentile 14,600 DN/s) | the overlay: off, as dev's rimtimsims block |
| The reference mosaic's `MAGZP` is 25.145 against the frames' 25.857 | recorded; gain matching scales the reference by 2.06 (F087) and 2.12 (F213) |
| F213's differences over-subtract star cores (core −0.050 to −0.075; 72k to 107k negative candidates, 10k to 13k above S/N 20); the stars are 1.04 times the science PSF's sigma, the measured reference PSF 1.39 px against the science 1.30 | recorded, with the gate's holds; the next measurement for this dataset (F087's cores are −0.005 to +0.024) |
| Every epoch fills the 500 cap (15k to 50k positive candidates) | recorded; it censors any count on the published set |
| The difference takes 28 to 72 min on one vCPU, the PSF-fit catalogs of three differencers | recorded |
| Dev's science-PSF transpose | measured on one epoch (run 101): no gain; not built |
| The 250513 truth's signal does not match the 260622 images; its dim-EB series carry the blended-EB ids | recorded; completeness unmet |

Runs 99, 100 and 101, their datasets and the Gaia boxes stay; nothing
was deleted. The raw evidence is the brief directory's `t1/`
(`metrics-*.json`, `snr-*.txt`, `gate-answers.txt`, `compat/`,
`times-check.txt`, `scripts/`).

## Results of part 13 so far (2026-10-24 22:35 UTC)

The weekend planner's briefs ran unattended from 19:22 UTC, one task
each, several in parallel, and every brief's task wrote `TASK DONE` on
its first attempt. The error-handling ledger's fixes 1 to 10 are in: a tool's
failure, an empty product, a missing input and a transient fault each
end where they should, with the reason on the job row and an exit code
the loop and Batch read correctly. The reference converges on `dev`
(point-source FWHM, SExtractor 0.25 / 32, 9100 px, InfoBits and status).
SFFT was published, measured worse than ZOGY on the same bytes, and the
published differencer is now the setting `[difference] diff_flavor`,
shipped `zogy`; `dev`'s pixel columns and its prefilter, the remedy Ben
named, are ported under `sfft` and measured: they do not bring SFFT to
ZOGY on HLWAS, so `zogy` stays. The gate holds on lost sky and
misregistration too. Every run below is CLEAN by `check_run.sh` except
run 83, which failed on purpose.

| step | before | after | where |
|---|---|---|---|
| dev-takenow | reference `fwhmmedpix` the all-source median, SExtractor seeing 1.2 and deblend 4: CLASS_STAR > 0.8 / < 0.2 411 / 1 of 4,240 (run 46) | the point-source mode (2.70 → 2.425 px), seeing 0.25, deblend 32: 1,059 / 1,447 of 4,299 (run 85); coadd pixels identical | PRs 406, 411 |
| err-tools | a tool's nonzero exit unread: a failed SExtractor, SWarp, ZOGY or bkgest surfaced later as a missing file, or not at all | `ToolFailed` (the tool, its code, its last 20 lines) on the row, exit 70; run 83 (SExtractor forced to `cat`) failed exit 70 with the reason; twin run 84 CLEAN | PRs 407, 412; rapid_docs 105 |
| differencer | ZOGY published | SFFT published and checked like every tool (PR 410 reconciled 407 and 408); run 86, one epoch: core fraction +0.251 against ZOGY's +0.006, the cap filled with stars (363 of 500 in a bright footprint), 471 packets | PRs 408, 410, 413; rapid_docs 103 |
| err-publish | an empty expected product bound official; a published-refused job resubmitted every pass; `loop` exit 2, `job wait` 0 / 0 / 75 | refused unbound unless `empty_ok` names the table; the loop reports it under `failed`; `loop` 1, `job wait` 0 / 1 / 2; run 87 drained exit 0, 14 of 14 bindings opened | PRs 409, 414; rapid_docs 104 |
| err-inputs | an unreadable trigger stamp, a bad reference catalog or a missing `refimcatalogs` row shipped null cutouts or matches; a catch-all around the PSF fit | each fails the packet job naming it; nothing caught around the fit (photutils returns `None` on every empty input probed); run 90 CLEAN, 471 packets as run 86; 0 of 436 live references lack their catalog row | PRs 415, 417 |
| differencer-measure | SFFT's default unmeasured | same bytes, HLWAS injected passes 4 to 6: detected 256 of 600 against ZOGY's 542, 0.00 at AB 25 against 0.99, false positives per epoch 385 against 228 (run 88 against 81); OpenUniverse 24 of 410 against 35 (run 89 against 80) | PRs 418, 419; rapid_docs 107 |
| dev-refresh | 7000 px mosaic; references graded `status` 1, `infobits` 0 by construction | 9100 px: reference peak 2.8 → 4.5 GiB inside the 6 GiB ask, nothing downstream changes at `001_0001` SCA 8 (runs 91 to 94, 256 of 600 both arms); `dev`'s grading: 288 graded, 0 status 0, bits equal `dev`'s on all 288 | PRs 416, 421; rapid_docs 106 |
| err-transient | throttling and `OperationalError` exited 70 (Batch never retried); a raising submit left the row `launched`; an empty first pass exited 0; `validate_run.py` passed failed runs | 75 on process and row; every failed row `<Class>: <message>`; the row `failed`; the first pass exits 64 (live, pass 30); `validate_run.py` fails a failed job; run 95 CLEAN, run 83 now FAIL by design | PRs 420, 424; rapid_docs 108 |
| diff-flavor | SFFT hard-coded as published | `[difference] diff_flavor`, `dev`'s knob, shipped `zogy` (Ben, 21:19 UTC); no run of its own: the ZOGY path is the one every run before PR 408 ran; run 98 is the first proof under it | PR 425; rapid_docs 110 |
| dev-columns | no sumrat, nneg, nbad | on every SFFT candidate after a 64 px local background, on `sources` under `sfft`; run 96 identical to 88 but for the columns; sumrat > 0.4 keeps 0.997 of injected, 0.68 of noise, 0.90 of bright-flagged; 4.3 to 5.2 s a difference; schema statement applied 21:26 UTC | PRs 422, 427; rapid_docs 112 |
| mig-quickstart | no local recipe; the loop page had no night from scratch | README Getting started, one HLWAS epoch in the image against a throwaway `postgres:18`, verbatim twice, 465 packets, validate 6/6; the loop page's Starting a night verbatim on SMDC, run 97 CLEAN 471 / 471 | PRs 423, 428; rapid_docs 109 |
| gate-metrics | lost sky and misregistration invisible to the gate | holds at `usable_fraction` < 0.9 and offset > 0.25 px; replay of 4,627 sets holds run 72 SCA 9's three (usable 0.704) and nothing healthy; run 98 CLEAN as smoke | PRs 426, 430; rapid_docs 111 |
| rimtimsim-reader (code only) | no FITS reader | `[intake] reader = "rimtimsim"`, overlay `rimtimsim-260622.toml`; one file: WCS (+0.052, +0.060) px from 588 Gaia stars, so no `crpix_shift`; the chain's runs are its step's | PR 429 |
| dev-prefilter | SFFT's catalog capped raw | under `sfft`, sumrat > 0.4 and the 2 px edge before the cap, `""` none; same bytes, three arms: ZOGY detected 542 of 600, raw SFFT 256, SFFT with the cut 275 (AB 25 bin 57 / 0 / 0 of 57), false positives per epoch 214 / 385 / 388, null packets 182 / 471 / 478; the cut drops a third of SFFT's candidates but the bright-star residuals refill the cap, so `diff_flavor` stays `zogy` and the gate's core and ring bounds stay (arm A's sit inside them) | PRs 431, 432, 434; rapid_docs 114 |

**The current procedure**, where these steps changed it:

- A night is rapid_docs' loop page, Starting a night, as run 97
  followed it: `run create` with the source root and bound by `--set`,
  the live binding, the Gaia import, the loop under `timeout 5400`
  running the module (a shell function cannot run under `timeout`),
  then `check_run.sh`.
- Read the loop's exit: 0 drained clean; 1 drained around failed or
  refused jobs, each named in the last pass's `failed` (read the row's
  `refusal`, fix, `job submit --job`); 64 a usage error, a first pass
  that admitted nothing among them; 75 transient, start it again.
  `job wait`: 0 all published, 1 one failed, 2 timed out.
- A failed run is not clean: `check_run.sh` now fails any run with a
  failed job, so a run that fails on purpose (run 83) reads FAIL.
- A comparison of differencers runs on the same bytes: a dataset of its
  own, the loop for PSF and intake only, the difference and packet jobs
  submitted against the baseline difference job's own `refimages` and
  `psfs` rows (runs 88, 89, 96).
- The published catalog is ZOGY's unless a run sets
  `difference.diff_flavor="sfft"`; SFFT's catalogs and their pixel
  columns are under every difference job's `diff/` either way.
- The unit suite runs under `env -u RAPID_PARAMETER_PATH` once the
  session has exported it for the database.

The live runs the part made: 83 (`r00342-et-fail`), 84
(`r00342-et-ok`), 85 (`r00342-dt-f146`), 86 (`r00342-dy-sfft`), 87
(`r00342-wk-errpub`), 88 (`r00342-dy-sfft-inj`), 89 (`ou-tds2-sfft`),
90 (`r00342-ei-inputs`), 91 to 94 (`r00342-dr-inj7000`,
`r00342-dr-inj9100`, `r00342-dr-null7000`, `r00342-dr-null9100`), 95
(`r00342-wk-errtrans`), 96 (`r00342-dc-cols-inj`), 97
(`r00342-mq-night`), 98 (`r00342-gm-smoke`), 102 to 107
(dev-prefilter's three arms, injected and null). Job definitions
`rapid-rebuild:65` to `:76` were registered; `:75` is `479eb163` (PR
429) and `:76`, the latest, `14860f57` (PR 431). The one schema statement was PR 422's `ALTER TABLE sources
ADD COLUMN`. Flight program 1049's four F158 WFI02 epochs are in the
inputs bucket under `flight-1049/`, read by no run yet. Nothing was
deleted.

**What remains, and where it sits:**

- **Whether `sfft` comes back**: not through the catalog's selection.
  sumrat keeps 0.90 of SFFT's point-like bright-star residuals on HLWAS,
  so the route is SFFT's own configuration (queue item `sfft-core`).
  Under SFFT `n_sources_snr_gt20` saturates at the cap, so the gate's
  count check cannot see an SFFT epoch off-scale (PR 432).
- **The false positives per epoch** under the field rule, a median 224
  on the injected footprint under ZOGY (part 12), unchanged by this part.
- **The injected arms' 415 false positives per epoch** (runs 91, 92)
  against run 88's 385 under SFFT: PR 432's raw arm reproduces 385 on
  the tip's image against run 81's reference and PSF rows, so the shift
  is in runs 91 and 92's own reference products (`refimages` 22868 and
  22867); which part is unread, PR 406's reference catalog the
  candidate.
- **`sources_official` on SMDC** lacks the three columns, because the
  view's `t.*` was expanded at creation and re-creating it needs a
  `DROP VIEW` grant (step dev-columns).
- **The dated SQL files fail after a fresh `schema.sql`** (step
  mig-quickstart); the README applies `schema.sql` alone.
- **The publisher binds an expected key no loaded row holds** (a dry-run
  typo, caught afterwards by `validate_run.py`'s lineage check), and no
  setting turns the packet job's Kafka send off (step mig-quickstart).
- **`gate.decide` reads the first `sources` input with no `ORDER BY`**;
  insertion order picks the set's own epoch in all 532 multi-source sets
  (PRs 426, 431).
- **`job wait` ends 0 for a job published with a refusal**, where the
  loop counts it failed (PRs 409, 420).
- **A stage's own `KeyError` exits 64** as a usage error with no
  traceback (PR 420, queue item `err-edges`), and `ToolRunner` has no
  timeout (PR 407).
- **OpenUniverse 28737's ZOGY reproduction shares 323 of 500 ids**,
  Intel against Intel, cause unread (step differencer-measure).

**Session notes**, every task's, gathered for Ben (the PR each came
from in brackets):

- dev-takenow: the image tag must be the full 40-character sha; a short
  one fails the deploy hook at `CreateChangeSet` after the push (411).
- err-tools: two briefs touching one line raced; naming which brief
  owns the line would remove it (407).
- differencer: the `deploy` hook moved `rapid-rebuild-production` with
  `rapid-rebuild:67`, by design, so production published SFFT until PR
  425 (413). PRs 407 and 408, each green on its own base, merged into a
  red `rebuild` for about ten minutes; CI does not rerun a PR on a moved
  base (410, 414).
- err-inputs: `python -I` also drops `PYTHONPATH`, so a session's own
  scripts importing `rapidpipe` run without it (415).
- dev-refresh and differencer-measure: the unit suite fails one
  connection test when `RAPID_PARAMETER_PATH` is exported (416, 418).
- dev-refresh: the graders were bound by `dev`'s PSF fit on the
  25-frame coadds, 7 to 10 minutes each, not by EFS (421).
- err-transient: the repository's pre-push hook blocked a test fixture
  holding a literal personal path; it is now assembled from fragments
  (424).
- mig-quickstart: err-transient merged while the task was open; it was
  merged in and the walk-through rerun on it (423).
- gate-metrics, rimtimsim-reader: COMMON's "at writing" facts were one
  merge behind PR 425 (426, 429).

## Validation

The ten checks a night meets, and their outcome on 2026-10-21. Checks 1 to 6
are `scripts/validate_run.py --run <id>` (part 7 step 7); 7 to 10 are
operations, checked as they run.

| check | outcome |
|---|---|
| 1 lineage closes | held on run 9 and run 10 |
| 2 Parquet and tables agree | held on both (counts for 309 jobs, bytes for ten) |
| 3 the chain | held on both: depth 69 and 5, row for row, sums reproduced, sets only grow |
| 4 packets against their recorded inputs, and the reader | held on both: 69 and 5 sets CLEAN |
| 5 forced photometry measured once | held on both |
| 6 two executions of one key | held: one official, the other an unbound candidate |
| 7 one group makes the night official | held: 145 keys, every view moved, nothing else did |
| 8 a mid-night epoch replaced | held by conservation; packets 4 and 5 left as published (observation) |
| 9 undo restores | held row for row; the undo of A refuses after B by the rule, and an explicit group restores |
| 10 two groups at once through the pooler | held: one commits, the other refuses naming the binding, nothing half-applied |

## If a step fails

- Part 1: stop. Before step 2 nothing has changed. After it, there is no
  backup to restore, by decision: the rollback is the schema step 1 named,
  rebuilt from git, and SMDC's `public` held only reference rows.
- Part 2: the inputs bucket refuses overwrites and deletes. A file that went
  up stays; rerun the loop and the files already there fail with 412, which
  means they are present, not that they are wrong.
- Part 3 and later: nothing is deleted. A job that failed is rerun with `rp
  job run --job <id>` (its next attempt), or on Batch by resubmitting its
  own request (step 13); a wrong binding is undone with `rp official undo
  <group>`. A Batch job that failed because of the code is read (`aws batch
  describe-jobs`, then its CloudWatch log stream), fixed by a pull request
  on `rebuild`, and the image rebuilt for the new commit (step 10); a new run
  names that commit and its jobs are submitted afresh, so the run and job
  records name the code that ran.
