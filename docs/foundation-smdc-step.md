# The SMDC step of the foundation (milestones 1 to 6)

Milestones 1 to 3 of the foundation plan (v5, 2026-10-20) ran against a
throwaway PostgreSQL 18 and `file://` prefixes on one host. This runbook takes
the same thin chain onto SMDC, then gives it its first Batch-produced
difference images (part 4, milestone 4). Every step is one command followed by
its check, and nothing goes on to the next step until the check holds. Parts 1
to 3 were run unattended on 2026-10-21 from a fleet host in the same security
group as the database's pooler, and are written here as run, with their
results; parts 4 and 5 followed the same day, run the same way, and part 6 is
the night of the same field written by the tasks that ran it. Six parts, in order:

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
