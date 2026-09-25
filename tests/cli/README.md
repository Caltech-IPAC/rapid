# tests/cli

Black-box behavioural tests of the `rapidpipe` CLI: argv in, exit code,
stdout/stderr and database state out. `test_help.py` needs no database;
`test_run_lifecycle.py` drives `rapidpipe run create/list/show/submit/
reconcile/finish/promote/rollback/delete/pin/unpin/cancel` against a real
PostgreSQL, with AWS Batch and S3 faked.

Run locally against a throwaway PostgreSQL with
`database/apply-migrations.sh` already applied (needs the Q3C extension;
`.github/workflows/cli-behaviour.yml` installs it into the CI service
container):

```
PGHOST=... PGPORT=5432 PGDATABASE=rapid PGUSER=... PGPASSWORD=... \
    .venv/bin/python -m pytest tests/cli -q
```

Without `PGHOST` set, the database-backed tests skip cleanly (exit 0);
`test_help.py` still runs.

Pass 2 adds `run start`/`status`/`inputs`/`compare`/`expire` coverage
(`test_run_start.py`, `test_run_inputs.py`,
`test_run_status_compare_expire.py`), plus `stage list`/`stage describe`
and the two forms of a stage's own `-h` in `test_help.py`. `run start`'s
own wait loop is driven by monkeypatching `rapidpipe.cli.runctl._reconcile`
to give any attempt of the run still unresolved a SUCCEEDED status and a
valid manifest before the real reconcile runs, standing in for Batch
finishing a job between polls without a side thread. `run expire`'s
`--now` only widens its candidate query; the actual deletion gate
re-checks `expires_at < now()` under the row lock using the database's
own clock (`runs.repository.mark_run_deleting`), so a scratch run under
test is backdated directly rather than through `--now` alone.
