# The SMDC step of the foundation (milestones 1 to 4)

Milestones 1 to 3 of the foundation plan (v5, 2026-10-20) ran against a
throwaway PostgreSQL 18 and `file://` prefixes on one host. This runbook takes
the same thin chain onto SMDC, then gives it its first Batch-produced
difference images (part 4, milestone 4). Every step is one command followed by
its check, and nothing goes on to the next step until the check holds. Parts 1
to 3 were run unattended on 2026-10-21 from a fleet host in the same security
group as the database's pooler, and are written here as run, with their
results. Four parts, in order:

1. **Schema**: the SMDC `rapid` database's `public` schema becomes `database/schema.sql`.
2. **Inputs**: the field's directory is copied to the inputs bucket under `foundation/`.
3. **Baseline**: the same import, associate and packet runs as milestones 1 to 3, on SMDC,
   so the database holds the thin chain before Batch touches it.
4. **Batch**: `reference` and `difference` on Batch under a run for the field's L2 inputs,
   the chain downstream of them, then one change group that replaces the imported
   difference images, sources, objects and packets together, and its undo.

What precedes part 4, and is not in this runbook: the `reference` and
`difference` stages ported to the `run(job)` contract
(`rapidpipe/foundation/local.py`), and the difference stage refreshed from
`dev` first (the plan's risk line: 102 commits of science drift). Until both
are merged on `rebuild`, part 4 is blocked; parts 1 to 3 are not.

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

Part 4 also needs, before it starts: the image for the commit that carries the
ports, and a `rapid-rebuild` revision registered for it. Check: `aws batch
describe-job-definitions --job-definition-name rapid-rebuild --status ACTIVE
--query 'sort_by(jobDefinitions,&revision)[-1].containerProperties.environment'`
shows `RAPID_RELEASE_IDENTITY` equal to `rebuild-<the commit>`.

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

Blocked until the `reference` and `difference` ports named at the top are
merged and their commit's image and `rapid-rebuild` revision are registered
(the check under "Before part 1" for that commit). The scope and inputs
of those two stages are the port's to define; the commands below take them
from the port's pull request body as `$REF_SCOPE`, `$REF_INPUTS`,
`$DIFF_SCOPE_<expid>` and `$DIFF_INPUTS_<expid>`.

The Batch jobs run under the scratch job role, which reads the inputs bucket,
writes the scratch bucket and lists it only under `rapidpipe*` (rapid_systems
`rapid-scratch-identities.yaml`), and cannot read the products bucket. So the
Batch run's outputs root is under `s3://$SCRATCH_BUCKET/rapidpipe/`, and the
stages it runs name only inputs-bucket files and their own outputs.

```sh
export RAPIDPIPE_OUTPUTS_ROOT="s3://$SCRATCH_BUCKET/rapidpipe/foundation"
export RAPIDPIPE_BATCH_JOB_QUEUE=rapid-queue-prompt
DEF=rapid-rebuild:<the revision registered for the port's commit>
```

10. **The Batch run**, not live, so its products publish unbound.

    ```sh
    rp run create --who "$USER" --why "milestone 4: reference and difference on Batch for field 4658155" \
      --revision <the port's commit> --settings "$RAPID/rapidpipe/settings/foundation-thin.toml" \
      --survey GBTDS --dataset $D
    ```

    Check: prints the run id, `$B` below (2 on the fresh schema); `rp run show $B`
    shows no live binding.

11. **Reference** on Batch.

    ```sh
    rp job submit --run $B --stage reference --scope "$REF_SCOPE" --inputs "$REF_INPUTS" --job-definition $DEF
    ```

    Check: prints `{"job", "batch_job"}`; `aws batch describe-jobs --jobs <batch_job>`
    reaches `SUCCEEDED`; `rp job show <job>` is `published` with its prefix
    under `s3://$SCRATCH_BUCKET/rapidpipe/foundation/$B/reference/`.

12. **Difference** on Batch, one job per epoch, each naming the reference job.

    ```sh
    rp job submit --run $B --stage difference --scope "$DIFF_SCOPE_84613" --inputs "$DIFF_INPUTS_84613" --job-definition $DEF \
    && rp job submit --run $B --stage difference --scope "$DIFF_SCOPE_84624" --inputs "$DIFF_INPUTS_84624" --job-definition $DEF
    ```

    Check: both Batch jobs `SUCCEEDED`, both jobs `published`, each with one
    `diffimages` row and its `sources` set (`rp job show`). A Batch job that
    failed is retried as the same job, not a new one (`rp job submit` always
    records a new job): resubmit the failed Batch job's own request with
    `aws batch submit-job`, the same job definition, queue and container
    overrides (`aws batch describe-jobs --jobs <batch_job>` shows them); its
    command `job run --job <id>` takes the next attempt.

13. **The chain downstream**, under run `$B` on this host, exactly as steps
    8 and 9 but with the Batch jobs' source sets (`<d1>`, `<d2>`, the
    difference job ids of step 12) and the new object set:

    ```sh
    export RAPIDPIPE_OUTPUTS_ROOT="s3://$SCRATCH_BUCKET/rapidpipe/foundation"
    rp job local --run $B --stage associate --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84613)"', "source_job": <d1>}]' \
    && rp job local --run $B --stage associate --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84624)"', "source_job": <d2>}, {"product": "objects", "key": '"$OKEY"', "source_job": <a1>}]'
    ```

    then the two packet jobs of step 9 with `objects` = `<a2>`, the
    `diffimages` inputs from `<d1>` and `<d2>`, the `l2files` inputs still from
    jobs 2 and 3, and epoch 2's `packets` predecessor = the new epoch-1 packet
    job `<p1>`. No `--expects`: the run is not live and nothing binds yet.
    Check: four jobs published, none bound; `read_packets.py --job <p1>` and
    `--job <p2>` exit 0.

14. **One change group** replaces the imported difference images, source
    sets, object set and packet sets with the Batch-produced ones, in one
    transaction:

    ```sh
    cat > replace.json <<EOF
    [{"close": 5,  "open": {"product": "diffimages", "key": $(k 84613), "job": <d1>}},
     {"close": 8,  "open": {"product": "sources",    "key": $(k 84613), "job": <d1>}},
     {"close": 9,  "open": {"product": "diffimages", "key": $(k 84624), "job": <d2>}},
     {"close": 12, "open": {"product": "sources",    "key": $(k 84624), "job": <d2>}},
     {"close": 14, "open": {"product": "objects",    "key": $OKEY,      "job": <a2>}},
     {"close": 15, "open": {"product": "packets",    "key": $(k 84613), "job": <p1>}},
     {"close": 16, "open": {"product": "packets",    "key": $(k 84624), "job": <p2>}}]
    EOF
    rp official change --group replace.json --who "$USER" --why "milestone 4: Batch difference products replace the imported ones"
    ```

    (The binding ids are part 3's; if `rp run show 1` showed different ones,
    use those.) Check: prints the group id `$G`; `diffimages_official`,
    `sources_official`, `objects_official` and `packets_official` return the
    Batch run's jobs for the field, and every other official view is
    unchanged (exposures, L2 files, the reference and PSF, the Gaia catalogue
    still from run 1). Part 4 milestone 4's first half.

15. **Undo** restores the imported bindings.

    ```sh
    rp official undo $G --who "$USER" --why "milestone 4: undo restores the imported products"
    ```

    Check: the same four views return jobs 2, 3, 5, 6 and 7 again, the
    bindings opened by `$G` are closed, and nothing was deleted (the Batch
    run's rows are still in their tables). Milestone 4 is done. Whether to
    reapply `$G` (`rp official undo` of the undo's group) is a decision for
    after the comparison, not part of this runbook.

## If a step fails

- Part 1: stop. Before step 2 nothing has changed. After it, there is no
  backup to restore, by decision: the rollback is the schema step 1 named,
  rebuilt from git, and SMDC's `public` held only reference rows.
- Part 2: the inputs bucket refuses overwrites and deletes. A file that went
  up stays; rerun the loop and the files already there fail with 412, which
  means they are present, not that they are wrong.
- Part 3 and later: nothing is deleted. A job that failed is rerun with `rp
  job run --job <id>` (its next attempt); a wrong binding is undone with `rp
  official undo <group>`.
