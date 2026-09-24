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
`expected.json`'s `inputs`:

- a 200x200 float32 ZOGY difference image with a TAN WCS centred on
  (269.45, -28.77) at 0.11"/pixel, with a Gaussian bump at each source;
- a two-row SExtractor reference catalog: a star (CLASS_STAR 0.9) beside
  sources 103 and 104, a galaxy (0.1) beside source 105;
- an input-set manifest (stage `input-set`) naming the difference image,
  the reference catalog, and the source, association and statistics sets.

### Database seed

The fake database (`rapidpipe/selftest/support/fakealertsdb.py`, selected by
`RAPIDPIPE_ALERTS_DATABASE`) holds the three sets' `product_instances` rows,
the difference instance's `diffimages` pid (4242), and these rows:

| sid | In | Fate |
|---|---|---|
| 101 | the source set, flags 4 | dropped: flagged |
| 102 | the source set; merges to aid 9002, whose astroobjects row is in another association set | dropped: orphan |
| 103 | the source set; aid 9001 | alert; previous detections 104 and 51 |
| 104 | the source set; aid 9001 | alert; previous detections 103 and 51 |
| 105 | the source set, 5 pixels from the edge; aid 9003 | alert; stamp partly filled with 0.0 |
| 106 | the source set, negative; aid 9004 | alert; no statistics row, so nDiaSources is the merges count |
| 51 | another source set, 30 days earlier; aid 9001 | history, inside the 365.25-day window |
| 50 | another source set, 400 days earlier; aid 9001 | history, outside the window |

The statistics set carries rows for aids 9001 (nsources 4) and 9003
(nsources 1). The fake writes the outbox rows, the `register_manifest` call
and the `nalertpackets` update to `db-state.json` on commit.

## Expected products

| Check | Tolerance | Why |
|---|---|---|
| exit code, two outputs (`alert-container` with members `container` and `summary`, `alert-set`), keys and registration blocks | exact | the manifest shape |
| the container decodes with fastavro against the packaged schema to alerts 103, 104, 105, 106, codec deflate | exact | `dev`'s container |
| per alert: object, previous-detection count, nDiaSources, firstDiaSourceMjd, reference matches, KONA and NED null, science and reference cutouts null | exact (MJD to 1e-9) | `dev`'s assembly over the seed |
| cutoutDifference decodes as a 129x129 FITS image carrying the parent WCS | exact | `dev`'s stamp geometry |
| the summary's dropped list: 101 flagged, 102 orphan | exact | `dev`'s BatchStats, extended |
| outbox rows: one per alert, record_index 0..3, run and attempt the invocation's, byte range decoding to its own alert | exact | the outbox |
| one commit, one `register_manifest` call naming the three sets, `nalertpackets` set on the difference instance for this run | exact | one transaction |

The same stage against PostgreSQL is `tests/db/test_alerts.py`.
