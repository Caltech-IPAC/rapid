# Database

The `rapid` PostgreSQL database schema and the sky-tessellation helpers
the pipeline imports.

- [`schema.sql`](schema.sql): the foundation schema (foundation plan v5,
  Part 1): the scientific tables with a job on every row, `runs`, `jobs`,
  `job_inputs`, `official` and the `<table>_official` views. It is applied
  to an empty PostgreSQL 18 database in one run, by CI
  (`foundation.yml`) and by hand:

  ```
  psql -v ON_ERROR_STOP=1 -f database/schema.sql
  ```

  It creates the Q3C extension and its indexes when Q3C is installed and
  says so in a NOTICE when it is not, as on a developer's container. There
  is no migration machinery until the first change after real data; from
  then on changes are incremental SQL: `schema.sql` carries every change
  for a fresh database, and a dated file beside it
  (`2026-10-23-runs-layers.sql`, the first; `2026-10-24-job-metrics.sql`, the `job_metrics` view; `2026-10-24-kafka-offsets.sql`, a packet set's acknowledgement coordinates; `2026-10-24-object-catalog.sql`, the object catalog that replaced the set chain; `2026-10-24-job-attempts.sql`, a job's Batch submissions and the per-job numbers `job_metrics` reads from them) brings an existing one to it,
  applied once.
- [`queries.sql`](queries.sql): the read queries the official views
  answer (foundation plan v5, Part 2): the plan's eight (as eleven
  statements: packet history three ways, lineage up and down) and the
  team's twelve spatial and status shapes, each one named statement with
  its documented columns, psycopg2 `%(name)s` parameters.
  `tests/db/test_queries.py` runs every one and checks the spatial ones
  reach their q3c or GIN index.
- `modules/utils/roman_tessellation.py` and
  `modules/utils/roman_tessellation_db.py`: the closed-form Roman sky
  tessellation (`RomanTessellationClosedForm`), imported by
  `rapidpipe.science.spatial` and `rapidpipe.science.load.catalogs` and
  shipped in the pipeline image. No database access.

`dev`'s `schema/`, `scripts/` (including `buildDatabase.sh`), `sims/`,
`config/` and `modules/utils/rapid_db.py` are not carried on this branch:
`schema.sql` supersedes the schema, and nothing here imports the rest.
