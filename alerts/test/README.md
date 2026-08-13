# `alerts` tests

Automated tests and benchmarking tools for the alert-production package
(`alerts/`, the parent directory).

## Requirements

- **Python 3.11** (the environment where `fitsio`, `numpy` 2, `astropy`,
  `fastavro`, and `pytest` are installed). Invoke it explicitly as
  `python3.11` — the default `python3` may be a different interpreter
  without these packages.
- The commands below are written to be run from the package root,
  `alerts/`. (Imports resolve from any directory — `conftest.py` and the
  hand-run tools put the repo root on `sys.path` themselves — only the
  relative `test/...` paths in the examples assume this cwd.)

```bash
cd alerts
python3.11 -m pytest test/ -q
```

That runs the whole suite. The live-database tests (`test_live_db.py`)
**skip** unless the database environment is set (see below) — a skipped
run is normal and not a failure.

## What each file is

### Automated tests (`pytest`)

| File | Covers |
|------|--------|
| `test_schema.py` | Schema-registry consistency, alert assembly semantics, stub/nullable enforcement, Avro round-trip. Uses a hand-rolled provider — no DB, no files. |
| `test_clips.py` | Cutout clips: the 0-based/1-based indexing regression, the WCS/position-consistency invariant, edge padding, header whitelist, multi-HDU loading. |
| `test_provider.py` | `DatabaseProvider` behavior over a fake DB + synthetic job directory: `resolve_pid`, flavor selection, the cutout-failure degradation ladder, batch/single byte-identity, and `--save` archive round-trip. |
| `test_benchmark.py` | The benchmark harness itself: the timing/memory/size JSONL is well-formed and `TimedProvider` is transparent. |
| `test_benchmark_forced_phot.py` | Offline pieces of the forced-photometry cost benchmark: footprint geometry (incl. RA wrap), FP stdout/lightcurve parsing, run selection, the cost fit, and the report path. |
| `test_ss_match.py` | Solar-system (KONA) association: sep/PA geometry vs astropy, radius/nearest-3 selection, the `--kona-file` loader, and the three ssMatches states end-to-end over the fake chip. |
| `test_ref_match.py` | Reference-catalog cross-match: SExtractor-catalog parsing, the star/galaxy partition and its subset→row index mapping, sep/PA geometry vs the KONA matcher, radius/nearest-3 selection, batch ≡ single, and the three refStarMatches/refGalaxyMatches states end-to-end over the fake chip (pid → rfid → refimcatalogs routing). |
| `test_live_db.py` | Live-database integration: the pixel-convention sentinel, the end-to-end production round trip (real alert → Avro → decode, cutouts FITS-verified), the `--kona-file` wiring against a real alert, and the reference-catalog match against the real mosaic catalog. **Skips** without DB access (see below). |

### Support modules (imported by the tests, not run directly)

| File | Role |
|------|------|
| `conftest.py` | Shared fixtures: the fake database (`FakeDB`/`ChipData`), a synthetic on-disk job directory of FITS products, and a realistic TPV WCS header. The whole suite hangs off these. |
| `wcs_eval.py` | A minimal forward-TPV WCS evaluator (`astropy.wcs` is unavailable in the container; `fitsio` has no transforms). |

### Tools (run by hand, not part of `pytest`)

| File | Role |
|------|------|
| `benchmark.py` | Timing + memory + output-size benchmark of batch production against the live database. |
| `benchmark_forced_phot.py` | Forced-photometry cost benchmark: decision data for alert-time FP vs storing FP products (see below). |
| `avro_producer.py`, `avro_consumer.py` | Standalone Kafka publish/consume smoke scripts (require a running broker and `confluent_kafka`). Resolve the schema via `schema/latest.txt` through `produce.load_schema()`. |
| `gen_sample_alert.py` | Regenerates the **synthetic** `schema/<ver>/sample_data/alert.json` from the registry (run after schema changes). For a real alert use `python -m alerts.cli <sid> --save file.avro`. |
| `inspect_alert.py` | Decodes a produced alert archive and sanity-checks fields and cutout WCS by hand: `python test/inspect_alert.py file.avro`. |

## Running subsets

```bash
# one file
python3.11 -m pytest test/test_clips.py -q

# one test, showing prints/logs (-s disables output capture)
python3.11 -m pytest test/test_provider.py::test_resolve_pid_picks_newest_best_campaign -sv

# everything matching a keyword
python3.11 -m pytest test/ -k cutout -q
```

## Live-database tests

`test_live_db.py` needs the RAPID database environment variables
(`DBSERVER`, `DBPORT`, `DBNAME`, `DBUSER`, `DBPASS`) and AWS credentials
for the product bucket. Without them — or when the database is
unreachable (VPN down, security group) — the tests emit a visible
`UserWarning` and skip; they never fail for lack of access.

```bash
# with the environment set (and VPN/DB reachable):
python3.11 -m pytest test/test_live_db.py -sv
```

The `test_database_reachable` canary owns the "not run" warning, so the
other live tests skip quietly and point to it.

## Benchmarking (not a `pytest` target)

`benchmark.py` runs batch production against the live database and writes
one JSON Lines file per run (timing per source, peak/bracketing memory,
and — with `--save` — the produced Avro archive's size). It needs the
same DB/AWS environment as the live tests. The summary is both printed to
the console and saved to the `-o` file.

```bash
# benchmark one exposure + SCA, also writing the (compressed) alert archive
python3.11 test/benchmark.py --exposure 80982 --sca 18 \
    --save alerts.avro -o timing.jsonl

# benchmark a specific processing ID, timing only (no archive)
python3.11 test/benchmark.py --pid 338173 -o timing.jsonl

# store the archive uncompressed (default is deflate, the MAST format)
python3.11 test/benchmark.py --pid 338173 --save alerts.avro --no-compress

# re-print the summary of one or more existing runs, side by side
python3.11 test/benchmark.py report timing.jsonl other_machine.jsonl
```

The embedded `meta` record (architecture, CPU, cores, library versions,
git SHA) makes runs from different machines directly comparable with
`report`.

## Forced-photometry cost benchmark (not a `pytest` target)

`benchmark_forced_phot.py` gathers the data for an architecture decision:
run forced photometry at alert time, or store FP products and refresh
them (and, if stored, how often full re-runs are affordable). Two tiers,
one JSONL output:

- **survey** (default; needs only the DB environment): walks the real
  alert path for one chip and, per alert-triggering object, counts the
  difference-image epochs covering its position — the work an alert-time
  FP request would redo — alongside its detection history. This is the
  per-chip "varying source histories" distribution.
- **measure** (`--run`; pipeline container + AWS + DB): actually executes
  `pipeline/forcedPhotometryForField.py` for batches of surveyed
  positions (request CSVs written with `reqid = aid`) at several batch
  sizes, recording wall time, the backend's own phase timings, staged
  bytes, and lightcurve row counts. Because the FP backend works per
  *field*, batch-size variation separates the fixed per-job staging cost
  from the marginal per-position cost — the two numbers the decision
  turns on. Scratch directories are kept for inspection.

```bash
# survey only: history/epoch distribution for one chip
python3.11 test/benchmark_forced_phot.py --pid 338173 -o fp.jsonl

# survey + real FP runs at batch sizes 1, 4 and 16 (expensive: each run
# downloads every difference image overlapping the field)
python3.11 test/benchmark_forced_phot.py --pid 338173 --run \
    --batches 1,4,16 -o fp.jsonl

# summarize, including the derived decision aid (per-alert latency,
# per-chip batched cost, full re-run cost per field)
python3.11 test/benchmark_forced_phot.py report fp.jsonl
```
