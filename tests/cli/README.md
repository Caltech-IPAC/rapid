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

Pass 2 adds `run start`/`status`/`inputs`/`compare`/`expire` coverage.
