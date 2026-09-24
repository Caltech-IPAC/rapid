# The load stage's fixture

The stage-contract fixture for `load` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-load
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.load` there as a subprocess with fresh run and
attempt ids (the invocation Batch uses), and checks the exit code, the
manifest and the rows the stage loaded. It exits 0 when every check passes.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `settings.toml` (the settings overlay: a 64x64
detector, so the fit-position bounds are `[-0.5, 64.5]`) and
`expected.json` (the input catalog rows, the database seed, the expected
products, the tolerance) -- lives once, under the packaged
`rapidpipe/selftest/fixtures/load/`: the same copy `rapidpipe selftest
--stage load` reads inside the pipeline image. This directory keeps no
copy of its own, so there is nothing here to fall out of sync with it.

### Inputs

Not committed as files: `run_fixture.py` writes them with
`tests/unit/fakeloaddb.build_load_input_set` from the packaged
`expected.json`'s parameters (`inputs.catalogs`). They are a difference
attempt's output location as `load` reads it:

- a completion manifest of stage `difference`, naming one ZOGY
  `difference-image` instance and its four `source-catalog` entries;
- the two SExtractor catalogs (placeholders: `load` never reads them, as
  `dev` never loads them);
- the two Photutils catalogs, positive and negative, each a PSF-fit
  catalog and a finder catalog written with `astropy.io.ascii.write` and
  `dev`'s column names. They are synthetic and small, chosen to cross each
  of `dev`'s rules once:

| Sign | id | x_fit, y_fit | Fate |
|---|---|---|---|
| positive | 1 | 10, 12 | loaded |
| positive | 2 | -0.6, 12 | rejected: x below -0.5 |
| positive | 3 | 64.6, 12 | rejected: x above 64 + 1 - 0.5 |
| positive | 4 | NaN, 12 | rejected: NaN fails the comparison |
| positive | 5 | 64.5, -0.5 | loaded: both bounds inclusive |
| positive | 6 | 20, 20 | dropped by the inner join: no finder row |
| negative | 1 | 30, 30 | loaded |
| negative | 2 | 5, 64.5 | loaded |

### Database seed

The fake database (`tests/unit/fakeloaddb.FakeLoadDatabase`, selected by
`RAPIDPIPE_LOAD_DATABASE`) is seeded with the difference instance's
`diffimages`/`l2files` values (`inputs.difference_row`: pid 4242, expid
1234, SCA 7, fid 3, MJD 61273.125, observed 2026-08-21), and writes what the
stage did -- tables made, rows COPY received, the source set registered --
to `db-state.json` on commit. The same stage against PostgreSQL (the
child-table functions, COPY, the `result_sets` row, provenance) is tested in
`tests/db/test_load.py`.

## Expected products and tolerances

| Check | Tolerance | Why |
|---|---|---|
| exit code, one `source-set` output, its key, `row_count`, `rows_by_sign`, table name | exact | integers and names |
| rows in COPY order (positive then negative), ids and signs | exact | `dev`'s order |
| every column of positive row 1 and the spatial and fit columns of two others, as COPY text | exact | the CSV is text; `dev` formats with `%s` |
| `pid`, `expid`, `fid`, `sca`, `mjdobs` on every row = the seed | exact | copied, never re-derived |
| `run`, `attempt`, `result_set` on every row = the invocation and the new instance | exact | the run model |
| `hp6`, `hp9`, `field` on every row | exact | recomputed independently: `healpy.ang2pix` at NSIDE 64 and 512 nested, and the tessellation looked up row by row as `dev` does |
| RA, Dec of the loaded rows | 1e-12 deg | round-tripped through the text catalog |

Provenance is checked for shape only: the manifest's run, unit and attempt
are the invocation's, the source-set instance is a ULID, and the execution
record carries a SHA-256 settings hash.
