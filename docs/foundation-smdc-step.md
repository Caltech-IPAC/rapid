# The attended SMDC step of the foundation (milestones 1 to 4)

Milestones 1 to 3 of the foundation plan (v5, 2026-10-20) ran against a
throwaway PostgreSQL 18 and `file://` prefixes on one host. This runbook takes
the same thin chain onto SMDC, then gives it its first Batch-produced
difference images (Part 4, milestone 4). It is attended: every step is one
command run by a person, followed by its check, and nothing goes on to the
next step until the check holds. Four parts, in order:

1. **Schema**: the SMDC `rapid` database's `public` schema becomes `database/schema.sql`.
2. **Inputs**: the field's two epochs are copied to the inputs bucket under `foundation/`.
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

Account-specific names are not in this repository. Where a step needs one it
uses a shell variable, set once from rapid_systems (`docs/architecture.md`
and `AGENTS.md` there):

```sh
export INPUTS_BUCKET=...     # the GBTDS simulation inputs bucket
export PRODUCTS_BUCKET=...   # the pipeline products bucket
export SCRATCH_BUCKET=...    # the scratch bucket the rapid-rebuild job role writes
export AWS_PROFILE=...       # an administrator profile for this account
export AWS_REGION=us-east-1
export DATA=...              # the field's directory, verified with its SHA256SUMS
export RAPID=...             # a checkout of rebuild at the commit the image was built from
export PY=...                # the rapid environment's python3
export PYTHONPATH=$RAPID
D=socsims-fakesrc-20260807
rp() { $PY -m rapidpipe.cli.main "$@"; }
```

## Before part 1

- The rapid_systems pull request for the per-commit image is merged, and its
  two commands have run (its body names them): the `rapid-batch` template
  deployed, then the image for `$RAPID`'s commit built and a `rapid-rebuild`
  revision registered for it. Check: `aws batch describe-job-definitions
  --job-definition-name rapid-rebuild --status ACTIVE --query
  'sort_by(jobDefinitions,&revision)[-1].containerProperties.environment'`
  shows `RAPID_RELEASE_IDENTITY` equal to `rebuild-<the commit>`.
- No Batch job is running or queued in either queue. Check: `aws batch
  list-jobs --job-queue rapid-queue-prompt --job-status RUNNING` and the
  same for `RUNNABLE` and for `rapid-queue-bulk` are empty.
- The scheduled processing-date loop is off or between dates; it writes
  through the old schema and would fail against the new one. Check: its
  `operations.tsv` row in rapid_systems and its last run time.

## Part 1: the schema

Run on the database host as root, with `PGPASSFILE=/etc/rapid/pg-admin.pgpass`
exported, exactly as rapid_systems' operator runbook § 8 does. `pg()` below is
the database's own socket, which the pooler fence does not affect:

```sh
pg() { podman exec -i --user postgres rapid-postgres psql -d rapid -v ON_ERROR_STOP=1 "$@"; }
```

1. **The rollback point.** A full backup on the stanza.

   ```sh
   podman exec --user postgres rapid-postgres pgbackrest --stanza=rapid-db-main --type=full backup
   ```

   Check: `podman exec --user postgres rapid-postgres pgbackrest --stanza=rapid-db-main info`
   lists a full backup finished in the last few minutes, and `... check` exits 0.

2. **Fence the pooler** for database `rapid`. Open one admin session to the
   pooler console and keep it open until step 6; issue `DISABLE` and later
   `ENABLE` over it (operator runbook § 8: a fresh admin connection may not
   authenticate while the pool is fenced).

   ```sh
   psql -h 127.0.0.1 -p 6432 -U postgres -d pgbouncer
   pgbouncer=# DISABLE rapid;
   pgbouncer=# KILL rapid;
   ```

   `DISABLE` refuses new clients for `rapid`; `KILL` drops the pool's client
   and server connections. Check, in the same session: `SHOW DATABASES;` shows
   `rapid` with `disabled` 1, `SHOW POOLS;` shows no clients or servers for
   it, and `pg -Atc "SELECT count(*) FROM pg_stat_activity WHERE datname =
   'rapid' AND usename <> 'postgres'"` is 0.

3. **Replace `public`.** Everything else in the database stays: `ops`,
   `pgbouncer` and its `pgbouncer_auth` lookup, and every role (roles are
   cluster objects).

   ```sh
   pg -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public' && pg < schema.sql
   ```

   (`schema.sql` is `$RAPID/database/schema.sql`, copied to the host.)
   Check: `pg -Atc "SELECT count(*) FROM runs"` is 0, `pg -Atc "SELECT
   q3c_version()"` answers (no "q3c is not available" notice appeared), and
   `pg -Atc "SELECT nspname FROM pg_namespace WHERE nspname IN ('ops',
   'pgbouncer')"` returns both.

4. **Grants.** The pipeline's role reads and writes every table and sequence,
   analyses the tables the publisher loads, and gets the same on anything
   created later; the database-level plumbing grants are reapplied.

   ```sh
   pg <<'SQL'
   GRANT USAGE ON SCHEMA public TO rapid_rebuild_pipeline;
   GRANT SELECT, INSERT, UPDATE, DELETE, MAINTAIN ON ALL TABLES IN SCHEMA public TO rapid_rebuild_pipeline;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO rapid_rebuild_pipeline;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE, MAINTAIN ON TABLES TO rapid_rebuild_pipeline;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO rapid_rebuild_pipeline;
   SQL
   ```

   Then rapid_systems' `cloudformation/apply-db-migrations.sh` (its plumbing
   files are re-runnable and restore `USAGE` on `public` for `rapid_read`).
   Check: `pg -Atc "SELECT count(*) FROM information_schema.role_table_grants
   WHERE grantee = 'rapid_rebuild_pipeline' AND privilege_type = 'INSERT'"`
   equals `pg -Atc "SELECT count(*) FROM pg_tables WHERE schemaname =
   'public'"`.

5. **Unfence**, over the same admin session: `ENABLE rapid;`. Check: `SHOW
   DATABASES;` shows `disabled` 0, then close the session.

6. **The pipeline's path works.** From the host holding the field's files,
   with the job definition's parameter tree naming the pooler and the
   service secret (`rapidpipe.db.connection`):

   ```sh
   export RAPID_PARAMETER_PATH=/rapid/rebuild; unset PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
   rp run show 1
   ```

   Check: exit 64 with "run 1 does not exist" (connected, empty schema). Exit
   75 or a configuration error means the path does not work; stop.

## Part 2: the inputs

7. **Copy the field's two epochs** to the inputs bucket. The L2 files and the
   PSF are already there under `g0005-fits-20260821/` and `g0006-psf-f146/`;
   this adds the difference and reference job outputs and the Gaia cone, as
   the record of what the baseline imported.

   ```sh
   aws s3 cp --recursive --only-show-errors "$DATA/" "s3://$INPUTS_BUCKET/foundation/field-4658155-20260821/"
   ```

   Check: `aws s3 ls --recursive "s3://$INPUTS_BUCKET/foundation/field-4658155-20260821/" | wc -l`
   equals `find "$DATA" -type f | wc -l`. The import below still reads `$DATA`
   on this host (its scope's `source_dir` is a local directory).

## Part 3: the baseline

The same jobs, in the same order, as milestones 1 to 3 (PRs 279, 280, 281),
with the outputs root under the products bucket. On the fresh schema their ids
are fixed by the order: run 1; import jobs 1 (reference, PSF, Gaia), 2 (epoch
1, expid 84613) and 3 (epoch 2, expid 84624); associate jobs 4 and 5; packet
jobs 6 and 7. The open binding ids follow: 2 to 4 for job 1, 5 to 8 for job 2,
9 to 12 for job 3, 13 for job 4 (closed by job 5's 14), 15 and 16 for the
packet sets. Each check confirms the id a command printed; a different id
means an earlier step published something extra, and the commands below that
name ids must then be read against `rp run show 1`.

```sh
export RAPIDPIPE_OUTPUTS_ROOT="s3://$PRODUCTS_BUCKET/foundation"
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

8. **The run, live.**

   ```sh
   rp run create --who "$USER" --why "milestone 4 baseline: the thin chain of field 4658155 on SMDC" \
     --revision "$(git -C "$RAPID" rev-parse HEAD)" --settings "$RAPID/rapidpipe/settings/foundation-thin.toml" \
     --survey GBTDS --dataset $D \
   && echo '[{"open": {"survey": "GBTDS", "dataset": "'$D'", "run": 1}}]' > live.json \
   && rp official change --group live.json --who "$USER" --why "first live run on SMDC for GBTDS $D"
   ```

   Check: prints 1 then 1; `rp run show 1` shows the live binding.

9. **Import** the shared products, then each epoch.

   ```sh
   rp job local --run 1 --stage import --scope "$SCOPE_SHARED" --expects '[{"product": "refimages", "key": {"dataset": "'$D'", "field": 4658155, "fid": 8}, "binding": null}, {"product": "psfs", "key": {"dataset": "'$D'", "fid": 8, "sca": 8}, "binding": null}, {"product": "catalogs", "key": {"name": "gaia", "version": "DR3"}, "binding": null}]' \
   && rp job local --run 1 --stage import --scope "$(epoch 84613 1832705 350875 r0034001004001029016_0001_wfi08_f146_cal_lite.fits.gz jid143931)" --expects "$(expects 84613)" --inputs "[$PSF]" \
   && rp job local --run 1 --stage import --scope "$(epoch 84624 1832552 351878 r0034001004001029022_0001_wfi08_f146_cal_lite.fits.gz jid144961)" --expects "$(expects 84624)" --inputs "[$PSF]"
   ```

   Check: three results with `"published": true`, jobs 1, 2, 3, each with a
   `group`; `rp job show 2` lists four open bindings and its manifest prefix
   under `s3://$PRODUCTS_BUCKET/foundation/1/import/2/1/`. The row counts are
   PR 279's: 2 exposures, 2 L2 files, 2 difference images, 1 reference, 1 PSF,
   1 Gaia catalogue, 500 sources per epoch.

10. **Associate** epoch 1 (no predecessor), then epoch 2 on it.

    ```sh
    rp job local --run 1 --stage associate --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84613)"', "source_job": 2}]' \
      --expects '[{"product": "objects", "key": '"$OKEY"', "binding": null}]' \
    && rp job local --run 1 --stage associate --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84624)"', "source_job": 3}, {"product": "objects", "key": '"$OKEY"', "source_job": 4}]' \
      --expects '[{"product": "objects", "key": '"$OKEY"', "binding": 13}]'
    ```

    Check: jobs 4 and 5 published; `rp job show 5` shows the field's object
    binding open on job 5 and job 4's closed (Part 3 step 1's observation);
    the object and membership counts equal PR 280's.

11. **Packet** epoch 1, then epoch 2 with epoch 1's set as its predecessor.

    ```sh
    rp job local --run 1 --stage packet --scope '{"expid": 84613, "sca": 8, "fid": 8}' \
      --inputs "[$SHARED_INPUTS, {\"product\": \"objects\", \"key\": $OKEY, \"source_job\": 5}, $(image 84613 2), {\"product\": \"sources\", \"key\": $(k 84613), \"source_job\": 2}]" \
      --expects '[{"product": "packets", "key": '"$(k 84613)"', "binding": null}]' \
    && rp job local --run 1 --stage packet --scope '{"expid": 84624, "sca": 8, "fid": 8}' \
      --inputs "[$SHARED_INPUTS, {\"product\": \"objects\", \"key\": $OKEY, \"source_job\": 5}, $(image 84613 2), $(image 84624 3), {\"product\": \"sources\", \"key\": $(k 84624), \"source_job\": 3}, {\"product\": \"packets\", \"key\": $(k 84613), \"source_job\": 6}]" \
      --expects '[{"product": "packets", "key": '"$(k 84624)"', "binding": null}]'
    ```

    Check: jobs 6 and 7 published and bound; `$PY $RAPID/scripts/read_packets.py
    --job 6` (and `--job 7`) exits 0, the reader reading the Avro back from
    the bucket. The thin chain is on SMDC.

## Part 4: Batch, then the change group

Blocked until the `reference` and `difference` ports named at the top are
merged and their commit's image and `rapid-rebuild` revision are registered
(rerun "Before part 1"'s first check for that commit). The scope and inputs
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

12. **The Batch run**, not live, so its products publish unbound.

    ```sh
    rp run create --who "$USER" --why "milestone 4: reference and difference on Batch for field 4658155" \
      --revision <the port's commit> --settings "$RAPID/rapidpipe/settings/foundation-thin.toml" \
      --survey GBTDS --dataset $D
    ```

    Check: prints the run id, `$B` below (2 on the fresh schema); `rp run show $B`
    shows no live binding.

13. **Reference** on Batch.

    ```sh
    rp job submit --run $B --stage reference --scope "$REF_SCOPE" --inputs "$REF_INPUTS" --job-definition $DEF
    ```

    Check: prints `{"job", "batch_job"}`; `aws batch describe-jobs --jobs <batch_job>`
    reaches `SUCCEEDED`; `rp job show <job>` is `published` with its prefix
    under `s3://$SCRATCH_BUCKET/rapidpipe/foundation/$B/reference/`.

14. **Difference** on Batch, one job per epoch, each naming the reference job.

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

15. **The chain downstream**, under run `$B` on this host, exactly as steps
    10 and 11 but with the Batch jobs' source sets (`<d1>`, `<d2>`, the
    difference job ids of step 14) and the new object set:

    ```sh
    export RAPIDPIPE_OUTPUTS_ROOT="s3://$SCRATCH_BUCKET/rapidpipe/foundation"
    rp job local --run $B --stage associate --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84613)"', "source_job": <d1>}]' \
    && rp job local --run $B --stage associate --scope '{"field": 4658155}' \
      --inputs '[{"product": "sources", "key": '"$(k 84624)"', "source_job": <d2>}, {"product": "objects", "key": '"$OKEY"', "source_job": <a1>}]'
    ```

    then the two packet jobs of step 11 with `objects` = `<a2>`, the
    `diffimages` inputs from `<d1>` and `<d2>`, the `l2files` inputs still from
    jobs 2 and 3, and epoch 2's `packets` predecessor = the new epoch-1 packet
    job `<p1>`. No `--expects`: the run is not live and nothing binds yet.
    Check: four jobs published, none bound; `read_packets.py --job <p1>` and
    `--job <p2>` exit 0.

16. **One change group** replaces the imported difference images, source
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

17. **Undo** restores the imported bindings.

    ```sh
    rp official undo $G --who "$USER" --why "milestone 4: undo restores the imported products"
    ```

    Check: the same four views return jobs 2, 3, 5, 6 and 7 again, the
    bindings opened by `$G` are closed, and nothing was deleted (the Batch
    run's rows are still in their tables). Milestone 4 is done. Whether to
    reapply `$G` (`rp official undo` of the undo's group) is a decision for
    after the comparison, not part of this runbook.

## If a step fails

- Parts 1 and 2: stop. Before step 3 nothing has changed; after it, the
  rollback point is step 1's backup, restored as rapid_systems' operator
  runbook § 8 "Rollback" describes, with the pooler still fenced.
- Part 3 and later: nothing is deleted. A job that failed is rerun with `rp
  job run --job <id>` (its next attempt); a wrong binding is undone with `rp
  official undo <group>`.
