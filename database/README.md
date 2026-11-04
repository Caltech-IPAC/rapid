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
  says so in a NOTICE when it is not, as on a developer's container.

  `sources` is split by UT observation date and SCA, as `dev`'s is: its
  rows live in child tables that inherit it, one per UT day of `mjdobs`
  and SCA, `sources_<yyyymmdd>_<sca>` (for example `sources_20261104_8`),
  and the parent holds none (`sources_holds_no_rows`, a `CHECK (false)`
  its children do not inherit). Each child's `CHECK` holds it to its date
  and SCA. The publisher makes a child when a set first needs it, through
  `create_catalog_child(parent, mjdobs, sca)`, a `SECURITY DEFINER`
  function owned by the schema's owner, since the pipeline's role holds
  table grants only, and loads it by bulk `COPY`; its `EXECUTE` is
  granted to that role alone (`docs/foundation-smdc-step.md`).

  `xsources`, `dev`'s table of the SFFT difference image's SExtractor
  sources (positive and negative, with `dev`'s extra columns), is split
  and loaded the same way, `xsources_<yyyymmdd>_<sca>`. Its rows belong to
  the difference job's source set and are official when the set is
  (`xsources_official` joins the set's `sources` binding).
- [`schema.svg`](schema.svg): the schema's entity diagram, every table and
  column with its declared foreign keys (solid) and the references the code
  follows by convention (dashed). `scripts/schema_diagram.py` draws it from
  `schema.sql` through `schema.dot` with Graphviz; a change to `schema.sql`
  reruns it (`tests/unit/test_schema_diagram.py` fails on a stale
  `schema.dot`).
- The dated files (`database/<date>-<name>.sql`), the rule for incremental
  change, as each file's header says: `schema.sql` builds a fresh database
  whole. A dated file is the one granted statement that brought SMDC's
  existing database to it, applied once and recorded in its runbook step
  (`docs/foundation-smdc-step.md`). A dated file is never applied after a
  fresh `schema.sql`; it fails there.
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
