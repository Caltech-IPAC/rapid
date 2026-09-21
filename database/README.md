# Database

The `rapid` PostgreSQL database schema, its migrations, and the tool that
applies them.

The schema is now defined by versioned SQL in [`migrations/`](migrations/README.md),
applied by [`apply-migrations.sh`](apply-migrations.sh) and tested in CI
against a fresh PostgreSQL with Q3C on every push and pull request. See
that directory's README for the rules and how to run the applier locally.

`scripts/buildDatabase.sh`'s schema-loading section (the block that runs
`psql -f` against the files under `schema/`) is superseded by
`migrations/`: new schema changes are added there, not to `schema/`. The
script and `schema/` are kept for now as the historical record of how the
original database was built and are not deleted by the pull request that
introduced `migrations/`.
