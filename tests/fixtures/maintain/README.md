# The maintain stage's fixture

The stage-contract fixture for `maintain` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-maintain
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.maintain` there as a subprocess with fresh run
and attempt ids (the invocation Batch uses), and checks the exit code, the
manifest and the fake database's recorded CLUSTER. It exits 0 when every
check passes.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `expected.json` (the unit's table, the input
manifest's source-set instance, which tables the fake database is seeded
to already have, and the expected products) -- lives once, under the
packaged `rapidpipe/selftest/fixtures/maintain/`: the same copy `rapidpipe
selftest --stage maintain` reads inside the pipeline image. This
directory keeps no copy of its own, so there is nothing here to fall out
of sync with it. `maintain` declares no settings, so unlike `difference`'s
and `load`'s fixtures there is no packaged `settings.toml`: the fixture
writes an empty settings overlay itself.

### Inputs

Not committed as files: `run_fixture.py` writes them with
`rapidpipe.selftest.support.fakemaintaindb.build_maintain_input_set` from
the packaged `expected.json`'s parameters (`inputs.table`,
`inputs.source_set_instance`). The manifest is shaped like a `load`
completion manifest -- stage `load`, unit kind `detector-image`, one
`source-set` output entry whose `registration.table` names the unit's
child table -- the simpler of the two shapes `maintain` accepts (the
other being a stage input-set manifest composed from several `load`
attempts for the same date and detector).

### Database seed

The fake database (`rapidpipe.selftest.support.fakemaintaindb.FakeMaintainDatabase`,
selected by `RAPIDPIPE_MAINTAIN_DATABASE`) is seeded with the child
tables it already holds (`inputs.existing_tables`), and writes what the
stage did -- which table(s) it CLUSTERed and ANALYZEd, and how many
commits -- to `db-state.json` on commit. The same stage against
PostgreSQL (`cluster_sources_child_table`, `pg_index.indisclustered`) is
tested in `tests/db/test_maintain.py`.

## Expected products and tolerances

| Check | Tolerance | Why |
|---|---|---|
| exit code, no outputs, `inputs.result_sets` names the source-set instance | exact | `maintain` writes no result set of its own |
| execution record `notes.table` and `notes.clustered` | exact | what the stage recorded it did |
| the fake database's `clustered` list and commit count | exact | the CLUSTER/ANALYZE ran once, on the right table |

Provenance is checked for shape only: the manifest's run, unit and
attempt are the invocation's, and the execution record carries a SHA-256
settings hash (an empty settings dict still hashes to one).
