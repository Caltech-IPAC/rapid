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
HLWAS simulation from ASDF intake to Kafka, was run the next night. Seven parts, in order:

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

## Validation

The ten checks a night meets, and their outcome on 2026-10-21:

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
