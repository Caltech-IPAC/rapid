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

  `objects`, `memberships`, `packets` and `forced` are split by field, as
  `dev`'s `astroobjects` and `merges` are, and so are `xobjects` and
  `xmemberships`, `dev`'s `xastroobjects` and `xmerges`: the xsources'
  catalog, which the SFFT difference job's cross-match writes: one child per Roman
  tessellation field, `<parent>_<field>` (for example `objects_1234`),
  each row filed under its object's field, so an object's row, its
  memberships, its packet rows and its forced photometry share one
  field's children. Each carries a `field` column and a child `CHECK
  (field = <field>)`; the parents hold no rows. The publisher makes a child
  through `create_field_child(parent, field)`, owned and granted like
  `create_catalog_child`, loads it by `COPY` and records the fields each
  job's rows went to in `job_fields`. In SQL, `<parent>_in(fields)` returns
  the rows in those fields' children, `objects_in(fields, dataset)` and
  `xobjects_in(fields, dataset)` the dataset's rows of them, of every run
  (the dataset's filter inside each child's query, so the readers hold no
  other dataset's rows; a difference job matches every run's objects of its
  dataset), the packet
  stage's `packet_origin_rows`, `packet_membership_rows` and
  `packet_reuse_rows` the rows its three reads keep, of every run (each read's
  filter inside each child's query), `<parent>_of(jobs)` the rows those
  jobs wrote (through `job_fields`), and `objects_official_in(fields)`
  `objects_official`'s rows of those fields, each from the children only.

  Nothing in this repository reads a parent (or its `_official` view): a
  query there visits every child. The pipeline, the operator scripts and
  `queries.sql` read the children their task needs. A source set's child
  is named by its difference image's L2 `mjdobs` and SCA (its own `jd`
  when it has no L2 row): `source_set_child(parent, job)`, which
  `rapidpipe.foundation.children` uses too. In SQL, `sources_of(jobs)`
  and `xsources_of(jobs)` return the rows of the source sets `jobs`
  names, from their children only: `FROM sources_of(ARRAY[...]) s` where
  a query would read `FROM sources s`, and likewise
  `FROM memberships_in(ARRAY[...]) m` for `FROM memberships m`. The
  publisher analyses the children
  it loaded, not the parents. `tests/unit/test_no_parent_reads.py` fails
  on a parent read in `rapidpipe`, `scripts` or `queries.sql`. A query by
  hand on a parent is still correct, and the children's `CHECK`
  constraints let a filter on `mjdobs`, `sca` or `field` skip the others.
- [`schema.svg`](schema.svg): the schema's entity diagram, every table and
  column with its declared foreign keys (solid) and the references the code
  follows by convention (dashed); a parent of child tables (`sources`,
  `xsources`, `objects`, `memberships`, `xobjects`, `xmemberships`,
  `packets`, `forced`) is marked with its children's names. `scripts/schema_diagram.py` draws it from
  `schema.sql` through `schema.dot` with Graphviz; a change to `schema.sql`
  reruns it (`tests/unit/test_schema_diagram.py` fails on a stale
  `schema.dot`).
- The dated files (`database/<date>-<name>.sql`), the rule for incremental
  change, as each file's header says: `schema.sql` builds a fresh database
  whole. A dated file is the one granted statement that brought SMDC's
  existing database to it, applied once and recorded in its runbook step
  (`docs/foundation-smdc-step.md`). A dated file is never applied after a
  fresh `schema.sql`; it fails there. The 2026-11-04 files apply in this
  order, which is not their names' order: `2026-11-04-a-retire-runs-1-68.sql`,
  `2026-11-04-sources-children.sql`, `2026-11-04-xsources.sql`,
  `2026-11-04-field-children.sql`, `2026-11-04-xobjects.sql`. SMDC's
  database was recreated from `schema.sql` on 2026-11-14, so the files before
  that date are history; since then the database is brought forward by dated
  files alone, its rows kept (Ben, 2026-11-20), applied in their names' order:
  `2026-11-17-survey-programs.sql`, `2026-11-18-refimmeta.sql`,
  `2026-11-20-exposure-ids.sql` (the last after the other two; it records
  every exposure already stored under the id it has and refuses, changing
  nothing, where it cannot), then `2026-11-20-run-stopped.sql` (three
  nullable columns on `runs`, which `rapidpipe run stop` writes), then
  `2026-11-23-pixel-area-map.sql` (one nullable column on `l2files`,
  which intake writes), then `2026-11-24-xsources-position-errors.sql`
  (six nullable columns on `xsources` and, by inheritance, every child,
  which the difference stage writes).

  Applying one is one command, run by whoever merges the pull request that
  adds it, from their own workstation under its instance role, with no
  profile and no database login of their own: in a rapid_systems checkout,

  ```
  cloudformation/apply-dated-sql.sh <path to the dated file>
  ```

  It pipes the file to `psql -X -v ON_ERROR_STOP=1` as `postgres` on the
  database host, prints psql's output and exits 0 only when the file applied
  (rapid_systems' operator runbook, "Apply a dated schema file", gives the
  checks around it). "As postgres" in a file's header names the role the
  statements run as, not a person. A file is one transaction, so a failure
  leaves the database as it was. Nothing records which files a database has
  taken but the runbook page, so a second application is the file's own
  behaviour: one that only adds missing rows (`2026-11-17-survey-programs.sql`)
  adds none; one that adds a table or a column fails at that statement and
  changes nothing. A person's own database login cannot apply one: it may not
  create in `public`, and a table it owned would lack the grants the
  pipeline's roles get from `postgres`'s default privileges.
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
