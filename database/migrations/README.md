# Database migrations live in the infrastructure repo

Schema changes ship through the versioned migration stream in
`rapid_systems/cloudformation/db-migrations/`, applied by
`rapid_systems/cloudformation/apply-db-migrations.sh` (S3-staged, applied
over SSM, recorded in the `schema_migrations` table). This repo carries no
migration files: a `.sql` file added here is invisible to the applier, and
a second stream against the same database would be worse than the problem
it solved.

The attempt-record emission code in `observability/` writes against the
schema created by `011-attempt-records.sql` in that stream, with the
database boundary stubbed in its unit tests.
