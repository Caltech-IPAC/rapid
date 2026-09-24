# The alerts stage's fixture

The stage-contract fixture for `alerts` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-alerts
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.alerts` there as a subprocess with fresh run and
attempt ids (the invocation Batch uses), and checks the exit code, the
manifest, the Avro container, the summary and what the fake database
recorded. It exits 0 when every check passes. `rapidpipe selftest --stage
alerts` runs the same fixture inside the pipeline image.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `settings.toml` (the overlay: ZOGY, 129x129 stamps,
reference-catalog match on, KONA, NED and Kafka off) and `expected.json`
(the synthetic image, the database seed, the expected products) -- lives
once, under the packaged `rapidpipe/selftest/fixtures/alerts/`. This
directory keeps no copy of its own.

### Inputs

Not committed as files: `rapidpipe/selftest/alerts.py` writes them from
`expected.json`'s `inputs` at prepare time (no FITS is packaged):

- a 200x200 float32 ZOGY difference image with a TAN WCS centred on
  (269.45, -28.77) at 0.11"/pixel, with a Gaussian bump at each source;
- a two-row SExtractor reference catalog: a star (CLASS_STAR 0.9) beside
  sources 103 and 104, a galaxy (0.1) beside source 105;
- `inputs/`: an input-set manifest naming the difference image, the
  reference catalog, the source set, two association sets (the image spans
  two fields) and the statistics set describing each;
- `inputs-empty/`: the same image, naming a registered source set with no
  rows and the first association and statistics sets.

The overlay sets 17x17 stamps, so several alerts share an Avro block.

### Database seed

The fake database (`rapidpipe/selftest/support/fakealertsdb.py`, selected by
`RAPIDPIPE_ALERTS_DATABASE`) holds the sets' `product_instances` rows, the
difference instance's `diffimages` pid (4242), and these rows:

| sid | In | Fate |
|---|---|---|
| 101 | the source set, flags 4 | dropped: flagged |
| 102 | the source set; merges to aid 9002, whose astroobjects row is only in an unnamed association set | dropped: orphan |
| 103 | the source set; aid 9001 in the first association set | alert; previous detections 104 and 51 |
| 104 | the source set; aid 9001 | alert; previous detections 103 and 51 |
| 105 | the source set, 5 pixels from the edge; aid 9003 | alert; stamp partly filled with 0.0 |
| 106 | the source set, negative; aid 9004 in the second association set | alert; nDiaSources 7 from the second statistics set |
| 107 | another source set, same image | never read |
| 51 | another source set, 30 days earlier; aid 9001 | history, inside the 365.25-day window |
| 50 | another source set, 400 days earlier; aid 9001 | history, outside the window |

Contaminating rows the stage must not read: merges and objects in an
unnamed association set (including a merges row for 103), and an unnamed
statistics set with nsources 99 for aids 9001 and 9003. The fake writes the
outbox rows, the `register_manifest` call and the `nalertpackets` update to
`db-state.json` on commit, and a later invocation restores them from it.

## Expected products

| Check | Tolerance | Why |
|---|---|---|
| exit code, two outputs (`alert-container` with members `container` and `summary`, `alert-set`), keys and registration blocks | exact | the manifest shape |
| the container decodes with fastavro against the packaged schema to alerts 103, 104, 105, 106, codec deflate | exact | `dev`'s container; 107 must not appear |
| per alert: object, previous-detection count, nDiaSources from its own statistics set, firstDiaSourceMjd, reference matches, KONA and NED null, science and reference cutouts null | exact (MJD to 1e-9) | `dev`'s assembly over the seed |
| cutoutDifference decodes as a 17x17 FITS image carrying the parent WCS | exact | `dev`'s stamp geometry at the overlay's size |
| the summary's dropped list: 101 flagged, 102 orphan | exact | `dev`'s BatchStats, extended |
| outbox rows: one per alert, record_ordinal 0..3, record_index 0.. within each block, at least one block with two or more alerts, each block locator decoding to its own alert, one time_processed_mjd equal to the alerts' timeProcessedMjd | exact | the outbox locator |
| one commit, one `register_manifest` call naming the five sets, `nalertpackets` set on the difference instance for this run | exact | one transaction |
| rerun of the same attempt with the local container and summary deleted: exit 0, the same instances, byte-identical files, no new rows, no commit | exact | recovery after an uncertain commit |
| the empty input set as a new attempt: exit 0, a zero-record container, an alert set of zero rows registered complete, no outbox rows | exact | zero alertable sources |

The same stage against PostgreSQL is `tests/db/test_alerts.py`.
