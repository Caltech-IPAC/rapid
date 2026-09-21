# Database migrations

Versioned SQL that defines the `rapid` database schema, applied in
filename order by `database/apply-migrations.sh`. Per
`rapid_docs/system/specification.md`'s Repositories section, the schema,
its migrations and the applier all live in this public repo, and a code
change and its schema change ship together in one pull request, tested
together by CI (`.github/workflows/db-migrations.yml`).

## Rules

- **Numbered files**: `NNN-short-name.sql`, three digits, filename order.
- **One change per file** — don't bundle unrelated schema changes.
- **Never edit a file once it has been applied anywhere.** The applier
  records each applied file's sha256 and refuses to proceed if a recorded
  file's hash no longer matches disk. A correction is a new, later-numbered
  file, not an edit in place.
- **`000-baseline.sql` is the floor**: the team's schema as of the rebuild,
  taken from `database/schema/`. Every later migration assumes it applied.
  See its own header for what was stripped and why (nothing
  account-specific may be committed to this public repo).
- **A schema change and its code change are one pull request.** CI applies
  the full stream to a fresh PostgreSQL and runs the test suite against it.
- `database/scripts/buildDatabase.sh` and `database/schema/*.sql` are
  superseded by this directory but not deleted by the pull request that
  introduces it.

## Running the applier locally

Point it at any PostgreSQL 16+ with Q3C, via the standard `PG*` variables:

```
export PGHOST=localhost PGPORT=5432 PGDATABASE=rapid PGUSER=postgres PGPASSWORD=...
database/apply-migrations.sh                    # apply
database/apply-migrations.sh --dry-run           # list what would apply
database/apply-migrations.sh --status            # list applied/pending
database/apply-migrations.sh /some/other/dir     # apply a different directory
```

Only those five `PG*` variables are read — no account-specific
configuration, no secret store lookups. Re-running is a no-op: already
applied files are skipped by filename and matching sha256. A file whose
content changed since it was recorded applied makes the applier exit
non-zero, naming the file.

## Adding a migration

1. Write `database/migrations/NNN-short-name.sql`, one past the highest
   existing number.
2. Test it locally against a throwaway PostgreSQL with Q3C.
3. Include its code change, if any, in the same pull request.
4. CI applies the whole stream from empty, re-applies to confirm a no-op,
   and checks that a modified already-applied file is refused.
