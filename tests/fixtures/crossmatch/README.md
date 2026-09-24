# The crossmatch stage's fixture

The stage-contract fixture for `crossmatch` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-crossmatch
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.crossmatch` there as a subprocess with fresh run
and attempt ids (the invocation Batch uses), and checks the exit code, the
manifest and the fake database's tables. It exits 0 when every check passes.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `expected.json` and the `settings.toml` overlay --
lives once, under the packaged `rapidpipe/selftest/fixtures/crossmatch/`:
the same copy `rapidpipe selftest --stage crossmatch` reads inside the
pipeline image.

### Inputs

Not committed as files: `rapidpipe.selftest.crossmatch` writes an input-set
manifest (stage `crossmatch`, unit kind `field`) naming two `source-set`
entries, and seeds the fake database with their rows.

- **Field 4662268**, an interior tile with eight neighbours
  (`field_neighbours`), which holds `load`'s own fixture position
  (269.45, -28.77). Its north edge is dec -28.758562; field 4658172 is its
  north neighbour.
- **Two source sets in one child table** (`sources_20270101_3`), one
  exposure each. The set with expid 2002 has the earlier `mjdobs`, so
  ascending-MJD order (`dev`'s) and ascending-expid order disagree.
- **Ten sources.** Expid 2002: 101, 102, 103 and 110 become objects; 104 has
  `flags = 4` and is ignored; 105 sits at 102's position in the same
  exposure, so it matches nothing yet and makes the same `aid`, which ON
  CONFLICT keeps once. Expid 1001: 106 and 107 lie within `match_radius` of
  101 and 103 and join their objects; 108 becomes an object; 109 lies in the
  north neighbour, 8e-6 degrees from 110, and joins 110's object in pass 2.

### Database seed

The fake database (`rapidpipe.selftest.support.fakecrossmatchdb.FakeCrossmatchDatabase`,
selected by `RAPIDPIPE_CROSSMATCH_DATABASE`) holds the two source sets'
registry entries and their rows. `q3c_join` and `q3c_radial_query` are exact
angular-separation tests. It writes its tables, locks, CLUSTER calls and
registrations to `db-state.json` on commit. The same stage against
PostgreSQL 18 with Q3C is `tests/db/test_crossmatch.py`.

## Expected products and tolerances

| Check | Tolerance | Why |
|---|---|---|
| exit code, one `association-set` entry, its key, `inputs.result_sets` | exact | the stage's contract |
| `row_counts`: 5 objects, 9 merges rows (8 in pass 1, 1 in pass 2), 6 new-object lines | exact | the scenario above |
| objects' `aid`, `ra0`, `dec0` | exact | recomputed with `radec_index` from the named sources |
| merges pairs | exact | named as (the object's first source, the source) |
| every row's run columns, one lock, one CLUSTER, one commit | exact | ruling R3, R13, R5 |
| each source's `field` | exact | recomputed with `tessellation_field` |

Provenance is checked for shape only: the manifest's run, unit and attempt
are the invocation's, and the execution record carries a SHA-256 settings
hash.
