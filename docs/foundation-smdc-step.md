# The SMDC step of the foundation (milestones 1 to 4)

Milestones 1 to 3 of the foundation plan (v5, 2026-10-20) ran against a
throwaway PostgreSQL 18 and `file://` prefixes on one host. This runbook takes
the same thin chain onto SMDC, then gives it its first Batch-produced
difference images (part 4, milestone 4). Every step is one command followed by
its check, and nothing goes on to the next step until the check holds. Parts 1
to 3 were run unattended on 2026-10-21 from a fleet host in the same security
group as the database's pooler, and are written here as run, with their
results; part 4 followed the same day, run the same way. Four parts, in order:

1. **Schema**: the SMDC `rapid` database's `public` schema becomes `database/schema.sql`.
2. **Inputs**: the field's directory is copied to the inputs bucket under `foundation/`.
3. **Baseline**: the same import, associate and packet runs as milestones 1 to 3, on SMDC,
   so the database holds the thin chain before Batch touches it.
4. **Batch**: `reference` and `difference` on Batch under a run for the field's L2 inputs,
   the chain downstream of them, also on Batch, then one change group that replaces the
   imported reference, difference images, sources, objects and packets together, and its undo.

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
   22 tables and 15 views.)

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
SCOPE_SHARED='{"source_dir": "'$DATA'", "reference": {"rfid": 90011, "dir": "jid129279"}, "psf": {"psfid": 153, "path": "psf/sciimage_psf_f146_sca08.fits"}, "gaia": {"name": "gaia", "version": "DR3", "path": "gaia/gaia-dr3-field-4658155-r0.12deg.parquet"}}'
k() { echo '{"dataset": "'$D'", "expid": '$1', "sca": 8, "fid": 8}'; }
OKEY='{"dataset": "'$D'", "survey": "GBTDS", "field": 4658155}'
epoch() { # expid rid pid l2 difference-directory
  echo '{"source_dir": "'$DATA'", "epochs": [{"expid": '$1', "rid": '$2', "pid": '$3', "rfid": 90011, "l2": "l2/'$4'", "difference": "'$5'"}]}'; }
expects() { # expid
  echo '[{"product": "exposures", "key": {"dataset": "'$D'", "expid": '$1'}, "binding": null}, {"product": "l2files", "key": '"$(k $1)"', "binding": null}, {"product": "diffimages", "key": '"$(k $1)"', "binding": null}, {"product": "sources", "key": '"$(k $1)"', "binding": null}]'; }
PSF='{"product": "psfs", "key": {"dataset": "'$D'", "fid": 8, "sca": 8}, "source_job": 1}'
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
   rp job local --run 1 --stage import --scope "$SCOPE_SHARED" --expects '[{"product": "refimages", "key": {"dataset": "'$D'", "field": 4658155, "fid": 8}, "binding": null}, {"product": "psfs", "key": {"dataset": "'$D'", "fid": 8, "sca": 8}, "binding": null}, {"product": "catalogs", "key": {"name": "gaia", "version": "DR3"}, "binding": null}]' \
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
PK='{"dataset": "'$D'", "fid": 8, "sca": 8}'
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
    (PR 288's import job 9; the stage tells the two apart by `PSFKIND`).

    ```sh
    din() { # expid l2-job
      echo '[{"product": "l2files", "key": '"$(k $1)"', "source_job": '$2'}, {"product": "refimages", "key": '"$RKEY"', "source_job": <r>},
             {"product": "psfs", "key": '"$PK"', "source_job": 1}, {"product": "psfs", "key": '"$PK"', "source_job": 9}]'; }
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
