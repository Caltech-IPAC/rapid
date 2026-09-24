"""The alerts stage's :class:`rapidpipe.selftest.runner.StageFixture`.

``prepare`` synthesises a 200x200 float32 ZOGY difference image with a TAN
WCS, a small SExtractor reference catalog, an input-set manifest naming
both and the three result sets, and the fake database's seed: six sources
in the source set (one flagged, one whose merges row points at an object
missing from the association set, two on one object with history, two
singletons), two earlier detections of that object (one inside the
previous-detection window, one outside), the association set's merges and
objects, and the statistics set's rows. ``check`` decodes the container
with fastavro against the packaged schema and checks the alerts, the
summary, the outbox rows the fake database recorded, the registered
manifest and the ``nalertpackets`` update.

The fake database's seed and state files live in ``work_dir``, named by
:data:`rapidpipe.selftest.support.fakealertsdb.SEED_ENV` and ``STATE_ENV``.
The same stage against PostgreSQL is ``tests/db/test_alerts.py``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import fastavro
from astropy.io import fits

from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.science.alerts.assemble import load_schema
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakealertsdb import (
    ASSOCIATION_SET,
    DIFFERENCE_INSTANCE,
    REFERENCE_CATALOG_INSTANCE,
    SEED_ENV,
    SOURCE_SET,
    STATE_ENV,
    STATISTICS_SET,
    UNIT_ID,
    build_alerts_input_set,
    pixel_to_sky,
    source_row,
    write_difference_image,
    write_reference_catalog,
)

FAKE_DATABASE = "rapidpipe.selftest.support.fakealertsdb:fake_database"
DATABASE_ENV = "RAPIDPIPE_ALERTS_DATABASE"
OTHER_ASSOCIATION_SET = "01J8Y6QZ3M00000000000ASSC2"
OTHER_SOURCE_SET = "01J8Y6QZ3M00000000000SRCS0"


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    spec = expected["inputs"]
    image = spec["image"]
    inputs = work / "inputs"
    diff_path = inputs / "diff" / "zogy_diffimage_masked.fits"
    wcs = write_difference_image(diff_path, image["naxis"], image["crval"],
                                 image["pixel_scale_arcsec"],
                                 [(s["x"], s["y"]) for s in spec["sources"]])
    refcat_path = inputs / "ref" / "refimage_sexcat.txt"
    refcat_rows = []
    for r in spec["reference_catalog"]:
        ra, dec = pixel_to_sky(wcs, r["x"], r["y"])
        refcat_rows.append([r["number"], f"{ra:.9f}", f"{dec:.9f}", 0, r["class_star"],
                            20.5, 0.02, 1.1, 2.5, 1.2, 1.8, 3.5])
    write_reference_catalog(refcat_path, refcat_rows)
    build_alerts_input_set(inputs, difference=diff_path, reference_catalog=refcat_path,
                           result_sets=[SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET])

    mjd = spec["mjdobs"]
    positions: dict[int, tuple[float, float]] = {}
    sources = []
    for s in spec["sources"]:
        ra, dec = pixel_to_sky(wcs, s["x"], s["y"])
        positions[s["sid"]] = (ra, dec)
        sources.append(source_row(s["sid"], s["x"], s["y"], ra, dec, result_set=SOURCE_SET,
                                  pid=spec["pid"], mjdobs=mjd, flags=s.get("flags", 0),
                                  isdiffpos=s.get("isdiffpos", True)))
    for h in spec["history"]:
        ra, dec = pixel_to_sky(wcs, h["x"], h["y"])
        positions[h["sid"]] = (ra, dec)
        sources.append(source_row(h["sid"], h["x"], h["y"], ra, dec, result_set=OTHER_SOURCE_SET,
                                  pid=h["pid"], mjdobs=mjd - h["days_before"], expid=h["expid"]))
    astroobjects = [{"aid": int(aid), "ra0": positions[sid][0], "dec0": positions[sid][1],
                     "result_set": ASSOCIATION_SET} for aid, sid in spec["astroobjects"].items()]
    astroobjects += [{"aid": int(aid), "ra0": positions[sid][0], "dec0": positions[sid][1],
                      "result_set": OTHER_ASSOCIATION_SET}
                     for aid, sid in spec["astroobjects_in_another_set"].items()]
    seed = {
        "product_instances": {
            SOURCE_SET: {"kind": "source-set", "complete": True,
                         "key": {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"}},
            ASSOCIATION_SET: {"kind": "association-set", "complete": True, "key": {"field": "5321"}},
            STATISTICS_SET: {"kind": "statistics-set", "complete": True,
                             "key": {"membership": ASSOCIATION_SET}},
        },
        "diffimages": {DIFFERENCE_INSTANCE: {"pid": spec["pid"]}},
        "sources": sources,
        "filters": spec["filters"],
        "exposures": spec["exposures"],
        "merges": [{"aid": aid, "sid": sid, "result_set": ASSOCIATION_SET}
                   for aid, sid in spec["merges"]],
        "astroobjects": astroobjects,
        "astroobjectsmeta": [{**m, "result_set": STATISTICS_SET} for m in spec["astroobjectsmeta"]],
    }
    seed_path = work / "db-seed.json"
    seed_path.write_text(json.dumps(seed, indent=2))
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("alerts") / "settings.toml").read_text())
    extra_env = {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(seed_path),
                 STATE_ENV: str(work / "db-state.json")}
    return inputs, overlay, extra_env


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
           context: CheckContext) -> None:
    spec = expected["expected"]
    mjd = expected["inputs"]["mjdobs"]
    want_sids = spec["alerts"]

    checks.check(manifest.inputs.products == {"difference-image": DIFFERENCE_INSTANCE,
                                              "reference-catalog": REFERENCE_CATALOG_INSTANCE},
                 f"inputs.products names the difference image and reference catalog: "
                 f"{manifest.inputs.products}")
    checks.check(list(manifest.inputs.result_sets) == [SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET],
                 "inputs.result_sets names the three sets")
    kinds = sorted(e.kind for e in manifest.outputs)
    checks.check(kinds == ["alert-container", "alert-set"],
                 f"outputs are an alert-container and an alert-set: {kinds}")
    by_kind = {e.kind: e for e in manifest.outputs}
    container = by_kind.get("alert-container")
    alert_set = by_kind.get("alert-set")
    if container is None or alert_set is None:
        return
    checks.check(sorted(m.role for m in container.members) == ["container", "summary"],
                 "the alert-container has two members, container and summary")
    checks.check(container.key == {"difference": DIFFERENCE_INSTANCE, "schema_version": "00.04"},
                 f"alert-container key: {container.key}")
    checks.check(alert_set.is_result_set() and alert_set.registration.get("row_count") == len(want_sids),
                 f"alert-set is a result set of {len(want_sids)} rows: {alert_set.registration}")
    reg = container.registration
    checks.check(reg.get("alert_count") == len(want_sids)
                 and reg.get("dropped_count") == len(spec["dropped"])
                 and reg.get("source_set") == SOURCE_SET
                 and reg.get("association_set") == ASSOCIATION_SET
                 and reg.get("statistics_set") == STATISTICS_SET
                 and reg.get("difference") == DIFFERENCE_INSTANCE
                 and reg.get("schema_version") == "00.04",
                 f"alert-container registration block: {reg}")

    members = {m.role: context.outputs / m.path for m in container.members}
    with members["container"].open("rb") as fh:
        raw = fh.read()
    reader = fastavro.reader(io.BytesIO(raw), reader_schema=load_schema())
    checks.check(reader.codec == "deflate", f"container codec deflate, got {reader.codec}")
    alerts = list(reader)
    sids = [a["diaSourceId"] for a in alerts]
    checks.check(sids == want_sids, f"alerts decoded against the packaged schema: {sids}")
    for alert in alerts:
        sid = str(alert["diaSourceId"])
        label = f"alert {sid}"
        checks.check(alert["schemaVersion"] == "00.04", f"{label} schemaVersion")
        checks.check(alert["diaSource"]["diaSourceId"] == int(sid)
                     and alert["diaSource"]["pid"] == expected["inputs"]["pid"],
                     f"{label} diaSource carries the seeded sid and pid")
        checks.check(alert["diaObject"]["diaObjectId"] == spec["objects"][sid],
                     f"{label} object {spec['objects'][sid]}")
        prv = alert["prvDiaSources"] or []
        checks.check(len(prv) == spec["prv_counts"][sid],
                     f"{label} prvDiaSources: expected {spec['prv_counts'][sid]}, got {len(prv)}")
        checks.check(alert["diaObject"]["nDiaSources"] == spec["n_dia_sources"][sid],
                     f"{label} nDiaSources: expected {spec['n_dia_sources'][sid]}, "
                     f"got {alert['diaObject']['nDiaSources']}")
        first = mjd - spec["first_seen_days_before"][sid]
        checks.check(abs(alert["diaObject"]["firstDiaSourceMjd"] - first) < 1e-9,
                     f"{label} firstDiaSourceMjd {first}")
        checks.check(len(alert["refStarMatches"] or []) == spec["ref_star_matches"][sid]
                     and len(alert["refGalaxyMatches"] or []) == spec["ref_galaxy_matches"][sid]
                     and alert["refStarMatches"] is not None,
                     f"{label} reference-catalog matches")
        checks.check(alert["ssMatches"] is None and alert["nedMatches"] is None,
                     f"{label} KONA and NED off: ssMatches, nedMatches null")
        checks.check(alert["cutoutScience"] is None and alert["cutoutReference"] is None,
                     f"{label} science and reference cutouts null in this port")
        stamp = alert["cutoutDifference"]
        checks.check(bool(stamp), f"{label} cutoutDifference non-empty")
        if stamp:
            with fits.open(io.BytesIO(stamp)) as hdus:
                shape = hdus[0].data.shape
                has_wcs = "CRPIX1" in hdus[0].header and "CTYPE1" in hdus[0].header
            side = spec["stamp_side"]
            checks.check(shape == (side, side), f"{label} cutout is {side}x{side}: {shape}")
            checks.check(has_wcs, f"{label} cutout carries the parent WCS")

    summary = json.loads(members["summary"].read_text())
    dropped = {str(d["sid"]): d["reason"] for d in summary.get("dropped", [])}
    checks.check(dropped == spec["dropped"],
                 f"summary dropped: expected {spec['dropped']}, got {dropped}")
    checks.check(summary.get("n_alerts") == len(want_sids) and summary.get("n_flagged") == 1
                 and summary.get("n_failed") == 1, "summary counts (dev's BatchStats)")

    state_path = context.work_dir / "db-state.json"
    if not state_path.exists():
        checks.check(False, "the fake database was committed")
        return
    state = json.loads(state_path.read_text())
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")
    outbox = sorted(state["outbox"], key=lambda r: r["record_index"])
    checks.check(len(outbox) == len(want_sids), f"{len(want_sids)} outbox rows, got {len(outbox)}")
    checks.check([r["record_index"] for r in outbox] == list(range(len(want_sids))),
                 "outbox record_index is 0..N-1")
    checks.check([r["candidate"] for r in outbox] == want_sids, "outbox candidates in record order")
    for r in outbox:
        label = f"outbox row {r['record_index']}"
        checks.check(is_valid_ulid(r["id"]), f"{label} id is a ULID")
        checks.check((r["run"], r["attempt"]) == (manifest.run, manifest.attempt),
                     f"{label} run and attempt are the invocation's")
        checks.check((r["instance"], r["result_set"]) == (container.instance, alert_set.instance),
                     f"{label} names the container and the alert set")
        checks.check(r["alert_name"] is None and r["schema_version"] == "00.04",
                     f"{label} alert_name null, schema 00.04")
        checks.check(r["object"] == spec["objects"][str(r["candidate"])], f"{label} object")
        offset, length = r["block_offset"], r["block_length"]
        header_end = outbox[0]["block_offset"]
        one = list(fastavro.reader(io.BytesIO(raw[:header_end] + raw[offset:offset + length])))
        checks.check(len(one) == 1 and one[0]["diaSourceId"] == r["candidate"],
                     f"{label} byte range decodes to its own alert")
    registered = state["registered"]
    checks.check(len(registered) == 1, f"one register_manifest call, got {len(registered)}")
    if registered:
        outputs = {o["kind"]: o for o in registered[0]["manifest"]["outputs"]}
        checks.check(set(outputs) == {"alert-container", "alert-set"}
                     and len(outputs["alert-container"]["members"]) == 2
                     and outputs["alert-set"]["members"] == []
                     and outputs["alert-set"].get("row_count") == len(want_sids),
                     "registered: alert-container (2 members) and alert-set (row_count)")
        checks.check(registered[0]["manifest"]["inputs"]["result_sets"]
                     == [SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET],
                     "registered dependencies name the three sets")
    checks.check(state["nalertpackets"] == [{"instance": DIFFERENCE_INSTANCE, "run": manifest.run,
                                             "value": 1}],
                 f"nalertpackets set on the run's difference row: {state['nalertpackets']}")


FIXTURE = StageFixture(
    stage="alerts",
    module="rapidpipe.stages.alerts",
    unit_kind="detector-image",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
