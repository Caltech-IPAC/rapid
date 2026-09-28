# Database

The `rapid` PostgreSQL database schema, its migrations, the tool that
applies them, and the sky-tessellation helpers the pipeline imports.

- [`migrations/`](migrations/README.md): the schema, as versioned SQL
  applied in filename order. See that README for the rules and how to run
  the applier locally.
- [`apply-migrations.sh`](apply-migrations.sh): the applier, run by CI
  (`db-migrations.yml`, `cli-behaviour.yml`) against a fresh PostgreSQL
  with Q3C on every push and pull request, and by the release migrate
  hook, which reads it and `migrations/` from the tagged git tree.
- `modules/utils/roman_tessellation.py` and
  `modules/utils/roman_tessellation_db.py`: the closed-form Roman sky
  tessellation (`RomanTessellationClosedForm`), imported by
  `rapidpipe.science.spatial` and `rapidpipe.science.load.catalogs` and
  shipped in the pipeline image. No database access.

`dev`'s `schema/`, `scripts/` (including `buildDatabase.sh`), `sims/`,
`config/` and `modules/utils/rapid_db.py` are not carried on this branch:
`migrations/` supersedes the schema, and nothing here imports the rest.
