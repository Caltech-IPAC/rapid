# Attempt-record migration — and where it has to live to be applied

This directory holds `011-attempt-records.sql`, the attempt-record DDL
(design/observability.md § Attempt record; decisions.md § Attempt-record
schema, ADOPTED 2026-08-03, both amendments folded in).

**It has not been applied, and it cannot be applied from here.** That is a
boundary problem, not an oversight — read this before doing anything with the
file.

## The situation

The versioned migration stream and its runner are in the **infrastructure
repo**, `rapid_systems`, not in this one:

- `rapid_systems/cloudformation/db-migrations/000-…010-*.sql`
- `rapid_systems/cloudformation/apply-db-migrations.sh`

The applier enumerates `db-migrations/0*.sql` **inside its own repo**, uploads
the pending ones to S3, and applies them over SSM, recording each filename in
the `schema_migrations` table. A migration file sitting in the pipeline repo is
invisible to it. Committing the DDL here does not schedule it for application;
only a commit in `rapid_systems` does.

This matches the design, which says the DDL "ships as a versioned migration in
the **infrastructure repository's** migration stream" — the pipeline repo was
never its home.

## Live state, verified 2026-08-04 (read-only SSM query)

    000-schema-migrations.sql        2026-07-19
    001-group-roles.sql              2026-07-19
    …
    010-pipeline-service-role.sql    2026-08-03

`attempts`, `attempt_stages`, `milestones`, and
`attempt_record_schema_versions` do **not** exist in the live database. So the
amendments' landing rule is satisfied as written: they fold directly into this
migration because nothing has been deployed to migrate from.

## What has to happen

Move this file — unchanged — to
`rapid_systems/cloudformation/db-migrations/011-attempt-records.sql`, commit it
there, and run `./apply-db-migrations.sh`. The number is free: the stream ends
at 010.

Do not renumber it into a second stream in this repo. Two streams against one
database, each with its own idea of what has been applied, is a worse problem
than the one it would solve.

## Validation already done

The DDL was applied and probed against a throwaway local PostgreSQL cluster
(not the live database): all four relations create, and 12 probe rows that the
§ 4 lifecycle matrix says are impossible were each rejected by the intended
constraint — including the amendment-1 cases (a `started` row missing
provenance or `scheduler_job_id`; a `terminal_without_start` row carrying
`product_disposition`; a `missing_or_contradictory` row missing
`reconciliation_sources`). Three valid rows inserted cleanly, among them the
scheduler-`SUCCEEDED`-with-application-`failure` combination the taxonomy
exists to make representable.

The emission code in `observability/` writes against exactly this shape and is
unit-tested with the database boundary stubbed, so it is ready the moment the
migration lands.
