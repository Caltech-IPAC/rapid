# The statistics stage's fixture

The stage-contract fixture for `statistics` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-statistics
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.statistics` there as a subprocess with fresh
run and attempt ids (the invocation Batch uses), and checks the exit code,
the manifest, the fake database's registered statistics set and the rows
it received. It exits 0 when every check passes.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `expected.json` (the input association set, its
base, their source sets, each object's sources, and the expected
products) and `settings.toml` (the overlay) -- lives once, under the
packaged `rapidpipe/selftest/fixtures/statistics/`: the same copy
`rapidpipe selftest --stage statistics` reads inside the pipeline image.

### Inputs

Not committed as files: the fixture writes them with
`rapidpipe.selftest.support.fakestatisticsdb.build_statistics_input_set`.
The manifest is shaped like a `crossmatch` completion manifest: stage
`crossmatch`, unit kind `field`, unit id the rtid, one `association-set`
entry whose key names the field, a base association set and the source
set it read.

### Database seed

The fake database (`rapidpipe.selftest.support.fakestatisticsdb.FakeStatisticsDatabase`,
selected by `RAPIDPIPE_STATISTICS_DATABASE`) is seeded by
`seed_from_fixture` from `expected.json`'s `inputs`:

- two association sets, the input and its base (base plus delta, step 1
  ruling R3), each naming one source set in its own `sources` child table;
- four objects: 101 has three sources spanning the RA 0/360 wrap, one
  under the base and two under the input set, to exercise `dev`'s
  mean-vector method; 102 has a single source; 103 has one source in each
  set; 104 has two sources under the input set only;
- one association set and source set outside the chain (object 105),
  whose rows must not be read.

Positions are synthetic and not inside the field's tile; the fake
database does not check geometry. The same stage against PostgreSQL is
`tests/db/test_statistics.py`.

## Expected products and tolerances

| Check | Tolerance | Why |
|---|---|---|
| exit code; one `statistics-set` entry, no members; key `{membership: <association set>}`; `inputs.result_sets` | exact | the manifest shape the products page fixes |
| `registration.table`, `row_count` (4), `objects_in_set` (4) | exact | literal counts |
| the chain read and one SELECT per source set | exact | the membership rule (R3, R7) |
| per-object `nsources` | exact | literal |
| per-object `meanra`, `stdevra`, `meandec`, `stdevdec`, `meanflux`, `stdevflux` | 1e-9 absolute | recomputed in the check with the ported `compute_radec_statistics` and `numpy` from the object's own sources |
| object 101's mean RA is at the wrap (within 1e-3 degrees of 0/360) | exact bound | a naive arithmetic mean would give about 120 degrees |
| object 102's standard deviations are 0.0 | exact | one source gives 0.0, not NaN |

Provenance is checked for shape only: the manifest's run, unit and
attempt are the invocation's, and the execution record carries a SHA-256
settings hash.
