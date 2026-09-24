# The prune stage's fixture

The stage-contract fixture for `prune` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-prune
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.prune` there as a subprocess with fresh run and
attempt ids (the invocation Batch uses), and checks the exit code, the
manifest and the fake database's recorded `prunedmerges` rows. It exits 0
when every check passes.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `settings.toml` (the settings overlay: the packaged
defaults, spelled out) and `expected.json` (the crossmatch association
set, its source sets and their sources, the seeded `diffimages` rows, the
seeded `merges` rows, and the expected exclusion) -- lives once, under the
packaged `rapidpipe/selftest/fixtures/prune/`: the same copy `rapidpipe
selftest --stage prune` reads inside the pipeline image. This directory
keeps no copy of its own, so there is nothing here to fall out of sync
with it.

### Inputs

Not committed as files: `run_fixture.py` writes them with
`rapidpipe.selftest.support.fakeprunedb.build_prune_input_set` from the
packaged `expected.json`'s parameters (`inputs.field`,
`inputs.association`). The manifest is shaped like `crossmatch`'s own
completion manifest -- stage `crossmatch`, unit kind `field`, one
`association-set` output entry naming the field, its base (`null`, a
root set here) and its source sets.

### Database seed

The fake database (`rapidpipe.selftest.support.fakeprunedb.FakePruneDatabase`,
selected by `RAPIDPIPE_PRUNE_DATABASE`) is seeded with the association
set's chain, the sources of its source sets (`sid`, `pid`), the
`diffimages` rows those `pid`s name (`vbest`, `run`), and the `merges`
rows of the field. It writes what the stage did -- the pruned set
registered and the `prunedmerges` rows inserted -- to `db-state.json` on
commit. The same stage against PostgreSQL (the child tables, the
not-best exclusion for real, `prunedmerges`, provenance) is tested in
`tests/db/test_prune.py`.

Three difference images, over five `merges_<field>` pairs (ruling R6, the
run-model form of `dev`'s not-best rule):

| pid | `vbest` | `run` | Fate |
|---|---|---|---|
| 101 | 1 | (none) | best (promoted current): its one source's pairs kept |
| 102 | 0 | another run | not best: its source's two pairs excluded |
| 103 | 0 | **this run** | best under R6's own-run clause: its source's pair kept |

A seeded `diffimages.run` of `"__OWN_RUN__"` is replaced with the
invocation's own `--run` value at fixture run time (read from `sys.argv`
inside the subprocess) -- the packaged `expected.json` is written before
`run_fixture.py` mints that id, so it cannot be spelled out literally.

## Expected products and tolerances

| Check | Tolerance | Why |
|---|---|---|
| exit code, one `pruned-set` output, its key names the base association set | exact | integers and instance ids |
| registration `row_count`, `base_row_count`, `table`, `rule` | exact | the exclusion count, the base set's own row count, `"prunedmerges"`, `"not-best"` |
| `inputs.result_sets` names the crossmatch association instance | exact | the one result set `prune` reads |
| the fake database's registered pruned set and its `prunedmerges` rows, one commit | exact | the excluded (aid, sid) pairs, matching the table above |

Provenance is checked for shape only: the manifest's run, unit and
attempt are the invocation's, the pruned-set instance is a ULID, and the
execution record carries a SHA-256 settings hash.
