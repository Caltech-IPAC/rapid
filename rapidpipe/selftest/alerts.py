"""The alerts stage's :class:`rapidpipe.selftest.runner.StageFixture`.

``prepare`` synthesises a 200x200 float32 ZOGY difference image with a TAN
WCS (at prepare time; no FITS is packaged), a small SExtractor reference
catalog, two input-set manifests and the fake database's seed:

- ``inputs/`` names the source set, two association sets (the image spans
  two fields) and the statistics set describing each. The source set holds
  six sources: one flagged, one whose merges row points at an object
  missing from its association set, two on one object with history, and two
  singletons, one in each association set. Two earlier detections of the
  shared object sit in another source set, one inside the previous-detection
  window and one outside.
- ``inputs-empty/`` names a registered source set with no rows.
- Contaminating rows the stage must never read: a source on the same image
  in another source set, merges and objects in an association set the input
  set does not name, and statistics in a statistics set it does not name.

The stage runs three times. The runner's invocation is checked in full:
the container decoded with fastavro against the packaged schema, several
alerts in one Avro block, each outbox row's block locator decoding to its
own alert, the summary, the registered manifest and ``nalertpackets``.
``check`` then deletes the local container and summary and reruns the same
attempt; it must regenerate identical bytes and reuse the committed rows,
writing nothing. Finally the empty input set runs as a new attempt; it gives
a zero-record container and an empty complete alert set.

The fake database's seed and state files live in ``work_dir``, named by
:data:`rapidpipe.selftest.support.fakealertsdb.SEED_ENV` and ``STATE_ENV``;
the state carries what one invocation committed into the next. The same
stage against PostgreSQL is ``tests/db/test_alerts.py``.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import fastavro
from astropy.io import fits

from rapidpipe.db.ids import is_valid_ulid, new_ulid
from rapidpipe.products.manifest import Manifest, hash_file
from rapidpipe.science.alerts.assemble import load_schema
from rapidpipe.selftest.runner import (
    CheckContext,
    Checks,
    StageFixture,
    fixture_dir,
    run_stage_subprocess,
)
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

MODULE = "rapidpipe.stages.alerts"
FAKE_DATABASE = "rapidpipe.selftest.support.fakealertsdb:fake_database"
DATABASE_ENV = "RAPIDPIPE_ALERTS_DATABASE"
ASSOCIATION_SET_2 = "01J8Y6QZ3M00000000000ASSC2"
STATISTICS_SET_2 = "01J8Y6QZ3M00000000000STAT2"
UNNAMED_ASSOCIATION_SET = "01J8Y6QZ3M00000000000ASSC9"
UNNAMED_STATISTICS_SET = "01J8Y6QZ3M00000000000STAT9"
OTHER_SOURCE_SET = "01J8Y6QZ3M00000000000SRCS0"
EMPTY_SOURCE_SET = "01J8Y6QZ3M00000000000SRCSE"
RESULT_SETS = [SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET, ASSOCIATION_SET_2, STATISTICS_SET_2]
EMPTY_RESULT_SETS = [EMPTY_SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET]

_SETS = {"association": ASSOCIATION_SET, "association_2": ASSOCIATION_SET_2,
         "unnamed_association": UNNAMED_ASSOCIATION_SET, "statistics": STATISTICS_SET,
         "statistics_2": STATISTICS_SET_2, "unnamed_statistics": UNNAMED_STATISTICS_SET}
#: The field whose per-field tables (merges_<f>, astroobjects_<f>,
#: astroobjectsmeta_<f>) hold each set's rows.
_FIELDS = {"association": 5321, "association_2": 5322, "unnamed_association": 5321,
           "statistics": 5321, "statistics_2": 5322, "unnamed_statistics": 5321}


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
                           result_sets=RESULT_SETS)
    empty = work / "inputs-empty"
    empty_diff = empty / "diff" / diff_path.name
    empty_diff.parent.mkdir(parents=True)
    empty_diff.write_bytes(diff_path.read_bytes())
    build_alerts_input_set(empty, difference=empty_diff, reference_catalog=None,
                           result_sets=EMPTY_RESULT_SETS)

    mjd = spec["mjdobs"]
    positions: dict[int, tuple[float, float]] = {}
    sources = []
    for s in spec["sources"]:
        ra, dec = pixel_to_sky(wcs, s["x"], s["y"])
        positions[s["sid"]] = (ra, dec)
        sources.append(source_row(s["sid"], s["x"], s["y"], ra, dec, result_set=SOURCE_SET,
                                  pid=spec["pid"], mjdobs=mjd, flags=s.get("flags", 0),
                                  isdiffpos=s.get("isdiffpos", True)))
    for s in spec["contaminating_sources"]:
        ra, dec = pixel_to_sky(wcs, s["x"], s["y"])
        positions[s["sid"]] = (ra, dec)
        sources.append(source_row(s["sid"], s["x"], s["y"], ra, dec, result_set=OTHER_SOURCE_SET,
                                  pid=spec["pid"], mjdobs=mjd))
    for h in spec["history"]:
        ra, dec = pixel_to_sky(wcs, h["x"], h["y"])
        positions[h["sid"]] = (ra, dec)
        sources.append(source_row(h["sid"], h["x"], h["y"], ra, dec, result_set=OTHER_SOURCE_SET,
                                  pid=h["pid"], mjdobs=mjd - h["days_before"], expid=h["expid"]))
    difference_key = {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"}
    seed = {
        "product_instances": {
            SOURCE_SET: {"kind": "source-set", "complete": True, "key": difference_key},
            EMPTY_SOURCE_SET: {"kind": "source-set", "complete": True, "key": difference_key},
            OTHER_SOURCE_SET: {"kind": "source-set", "complete": True,
                               "key": {"difference": "01J8Y6QZ3M00000000000D1F0",
                                       "catalog_type": "photutils"}},
            ASSOCIATION_SET: {"kind": "association-set", "complete": True,
                              "key": {"field": 5321, "base": None}},
            ASSOCIATION_SET_2: {"kind": "association-set", "complete": True,
                                "key": {"field": 5322, "base": None}},
            UNNAMED_ASSOCIATION_SET: {"kind": "association-set", "complete": True,
                                      "key": {"field": 5321, "base": None}},
            DIFFERENCE_INSTANCE: {"kind": "difference-image", "complete": None,
                                  "key": {"differencer": "zogy"}},
            REFERENCE_CATALOG_INSTANCE: {"kind": "reference-catalog", "complete": None,
                                         "key": {"catalog_type": "sextractor"}},
            STATISTICS_SET: {"kind": "statistics-set", "complete": True,
                             "key": {"membership": ASSOCIATION_SET}},
            STATISTICS_SET_2: {"kind": "statistics-set", "complete": True,
                               "key": {"membership": ASSOCIATION_SET_2}},
            UNNAMED_STATISTICS_SET: {"kind": "statistics-set", "complete": True,
                                     "key": {"membership": ASSOCIATION_SET}},
        },
        "diffimages": {DIFFERENCE_INSTANCE: {"pid": spec["pid"]}},
        "sources": sources,
        "filters": spec["filters"],
        "exposures": spec["exposures"],
        "merges": [{"aid": aid, "sid": sid, "result_set": _SETS[name], "field": _FIELDS[name]}
                   for name, pairs in spec["merges"].items() for aid, sid in pairs],
        "astroobjects": [{"aid": int(aid), "ra0": positions[sid][0], "dec0": positions[sid][1],
                          "result_set": _SETS[name], "field": _FIELDS[name]}
                         for name, objects in spec["astroobjects"].items()
                         for aid, sid in objects.items()],
        "astroobjectsmeta": [{**m, "result_set": _SETS[name], "field": _FIELDS[name]}
                             for name, rows in spec["astroobjectsmeta"].items() for m in rows],
    }
    seed_path = work / "db-seed.json"
    seed_path.write_text(json.dumps(seed, indent=2))
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("alerts") / "settings.toml").read_text())
    return inputs, overlay, _env(work)


def _env(work: Path) -> dict[str, str]:
    return {DATABASE_ENV: FAKE_DATABASE, SEED_ENV: str(work / "db-seed.json"),
            STATE_ENV: str(work / "db-state.json")}


def _state(work: Path) -> dict[str, Any] | None:
    path = work / "db-state.json"
    return json.loads(path.read_text()) if path.exists() else None


def _run_again(context: CheckContext, run_id: str, attempt_id: str, inputs: Path,
               outputs: Path) -> int:
    from rapidpipe.selftest import REPO_ROOT
    return run_stage_subprocess(sys.executable, MODULE, inputs, str(outputs),
                                context.work_dir / "settings.toml", run_id, attempt_id, UNIT_ID,
                                _env(context.work_dir), REPO_ROOT)


def _check_alerts(checks: Checks, container, raw: bytes, spec: dict[str, Any],
                  expected: dict[str, Any]) -> None:
    mjd = expected["inputs"]["mjdobs"]
    reader = fastavro.reader(io.BytesIO(raw), reader_schema=load_schema())
    checks.check(reader.codec == "deflate", f"container codec deflate, got {reader.codec}")
    alerts = list(reader)
    sids = [a["diaSourceId"] for a in alerts]
    checks.check(sids == spec["alerts"],
                 f"alerts decoded against the packaged schema: {sids} (107 is in another "
                 "source set and must not appear)")
    for alert in alerts:
        sid = str(alert["diaSourceId"])
        label = f"alert {sid}"
        checks.check(alert["schemaVersion"] == "00.04", f"{label} schemaVersion")
        checks.check(alert["diaSource"]["diaSourceId"] == int(sid)
                     and alert["diaSource"]["pid"] == expected["inputs"]["pid"],
                     f"{label} diaSource carries the seeded sid and pid")
        checks.check(alert["diaObject"]["diaObjectId"] == spec["objects"][sid],
                     f"{label} object {spec['objects'][sid]}, got "
                     f"{alert['diaObject']['diaObjectId']}")
        prv = alert["prvDiaSources"] or []
        checks.check(len(prv) == spec["prv_counts"][sid],
                     f"{label} prvDiaSources: expected {spec['prv_counts'][sid]}, got {len(prv)}")
        checks.check(alert["diaObject"]["nDiaSources"] == spec["n_dia_sources"][sid],
                     f"{label} nDiaSources from its own statistics set: expected "
                     f"{spec['n_dia_sources'][sid]}, got {alert['diaObject']['nDiaSources']}")
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


def _check_outbox(checks: Checks, manifest: Manifest, container, alert_set, raw: bytes,
                  outbox: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    want_sids = spec["alerts"]
    checks.check(len(outbox) == len(want_sids), f"{len(want_sids)} outbox rows, got {len(outbox)}")
    checks.check([r["record_ordinal"] for r in outbox] == list(range(len(want_sids))),
                 "outbox record_ordinal is 0..N-1")
    checks.check([r["candidate"] for r in outbox] == want_sids, "outbox candidates in record order")
    blocks: dict[int, list[dict]] = {}
    for r in outbox:
        blocks.setdefault(r["block_offset"], []).append(r)
    checks.check(max((len(b) for b in blocks.values()), default=0)
                 >= spec["min_records_in_largest_block"],
                 f"several alerts share one Avro block: {[len(b) for b in blocks.values()]}")
    checks.check(all([r["record_index"] for r in b] == list(range(len(b)))
                     for b in blocks.values()), "record_index is 0.. within each block")
    checks.check(len({r["time_processed_mjd"] for r in outbox}) == 1,
                 "one time_processed_mjd for the container")
    header_end = min(blocks) if blocks else 0
    for r in outbox:
        label = f"outbox row {r['record_ordinal']}"
        checks.check(is_valid_ulid(r["id"]), f"{label} id is a ULID")
        checks.check((r["run"], r["attempt"]) == (manifest.run, manifest.attempt),
                     f"{label} run and attempt are the invocation's")
        checks.check((r["instance"], r["result_set"]) == (container.instance, alert_set.instance),
                     f"{label} names the container and the alert set")
        checks.check(r["alert_name"] is None and r["schema_version"] == "00.04",
                     f"{label} alert_name null, schema 00.04")
        checks.check(r["object"] == spec["objects"][str(r["candidate"])], f"{label} object")
        offset, length = r["block_offset"], r["block_length"]
        block = list(fastavro.reader(io.BytesIO(raw[:header_end] + raw[offset:offset + length])))
        checks.check(r["record_index"] < len(block)
                     and block[r["record_index"]]["diaSourceId"] == r["candidate"],
                     f"{label} block locator decodes to its own alert")
        checks.check(block and block[0]["diaSource"]["timeProcessedMjd"] == r["time_processed_mjd"],
                     f"{label} time_processed_mjd is the alerts' timeProcessedMjd")


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
           context: CheckContext) -> None:
    spec = expected["expected"]
    want_sids = spec["alerts"]

    checks.check(manifest.inputs.products == {"difference-image": DIFFERENCE_INSTANCE,
                                              "reference-catalog": REFERENCE_CATALOG_INSTANCE},
                 f"inputs.products names the difference image and reference catalog: "
                 f"{manifest.inputs.products}")
    checks.check(list(manifest.inputs.result_sets) == RESULT_SETS,
                 "inputs.result_sets names the five sets")
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
                 and reg.get("association_sets") == [ASSOCIATION_SET, ASSOCIATION_SET_2]
                 and reg.get("statistics_sets") == [STATISTICS_SET, STATISTICS_SET_2]
                 and reg.get("association_set") == ASSOCIATION_SET
                 and reg.get("statistics_set") == STATISTICS_SET
                 and reg.get("difference") == DIFFERENCE_INSTANCE
                 and reg.get("schema_version") == "00.04",
                 f"alert-container registration block: {reg}")

    members = {m.role: context.outputs / m.path for m in container.members}
    raw = members["container"].read_bytes()
    _check_alerts(checks, container, raw, spec, expected)

    summary = json.loads(members["summary"].read_text())
    dropped = {str(d["sid"]): d["reason"] for d in summary.get("dropped", [])}
    checks.check(dropped == spec["dropped"],
                 f"summary dropped: expected {spec['dropped']}, got {dropped}")
    checks.check(summary.get("n_alerts") == len(want_sids) and summary.get("n_flagged") == 1
                 and summary.get("n_failed") == 1, "summary counts (dev's BatchStats)")

    state = _state(context.work_dir)
    if state is None:
        checks.check(False, "the fake database was committed")
        return
    checks.check(state["commits"] == 1, f"one commit, got {state['commits']}")
    outbox = sorted(state["outbox"], key=lambda r: r["record_ordinal"])
    _check_outbox(checks, manifest, container, alert_set, raw, outbox, spec)
    registered = state["registered"]
    checks.check(len(registered) == 1, f"one register_manifest call, got {len(registered)}")
    if registered:
        outputs = {o["kind"]: o for o in registered[0]["manifest"]["outputs"]}
        checks.check(set(outputs) == {"alert-container", "alert-set"}
                     and len(outputs["alert-container"]["members"]) == 2
                     and outputs["alert-set"]["members"] == []
                     and outputs["alert-set"].get("row_count") == len(want_sids),
                     "registered: alert-container (2 members) and alert-set (row_count)")
        checks.check(registered[0]["manifest"]["inputs"]["result_sets"] == RESULT_SETS,
                     "registered dependencies name the five sets")
    checks.check(state["nalertpackets"] == [{"instance": DIFFERENCE_INSTANCE, "run": manifest.run,
                                             "value": 1}],
                 f"nalertpackets set on the run's difference row: {state['nalertpackets']}")

    # A rerun of the same attempt after its commit, the local outputs lost:
    # identical bytes regenerated, the committed rows reused, nothing written.
    digests = {role: hash_file(path) for role, path in members.items()}
    for path in members.values():
        path.unlink()
    rc = _run_again(context, manifest.run, manifest.attempt, context.inputs, context.outputs)
    checks.check(rc == 0, f"rerun of the same attempt exits 0, got {rc}")
    rerun = Manifest.read(context.outputs / "manifest.json") if rc == 0 else None
    checks.check(rerun is not None and {e.kind: e.instance for e in rerun.outputs}
                 == {"alert-container": container.instance, "alert-set": alert_set.instance},
                 "the rerun reuses the committed instances")
    checks.check(all(path.exists() and hash_file(path) == digests[role]
                     for role, path in members.items()),
                 "the rerun regenerates byte-identical container and summary")
    after = _state(context.work_dir) or {}
    checks.check(after.get("commits") == 1 and after.get("outbox") == state["outbox"]
                 and len(after.get("registered", [])) == 1,
                 "the rerun writes no rows and does not commit")

    # The empty input set, a new attempt: a zero-record container and an
    # empty complete alert set, still registered and still counted.
    empty_outputs = context.work_dir / "outputs-empty"
    empty_attempt = new_ulid()
    rc = _run_again(context, manifest.run, empty_attempt, context.work_dir / "inputs-empty",
                    empty_outputs)
    checks.check(rc == 0, f"the empty input set exits 0, got {rc}")
    if rc != 0:
        return
    empty = {e.kind: e for e in Manifest.read(empty_outputs / "manifest.json").outputs}
    checks.check(set(empty) == {"alert-container", "alert-set"},
                 "the empty input set still gives both outputs")
    empty_container = empty["alert-container"]
    raw = (empty_outputs / empty_container.primary).read_bytes()
    checks.check(list(fastavro.reader(io.BytesIO(raw))) == [],
                 "the empty input set's container decodes to zero records")
    checks.check(empty_container.registration.get("alert_count") == 0
                 and empty["alert-set"].registration.get("row_count") == 0,
                 "zero alerts, an alert set of zero rows")
    final = _state(context.work_dir) or {}
    checks.check(final.get("commits") == 2
                 and not [r for r in final.get("outbox", []) if r["attempt"] == empty_attempt],
                 "the empty run commits once and writes no outbox rows")
    registered_set = final.get("product_instances", {}).get(empty["alert-set"].instance, {})
    checks.check(registered_set.get("complete") is True and registered_set.get("row_count") == 0,
                 f"the empty alert set is registered complete: {registered_set}")


FIXTURE = StageFixture(
    stage="alerts",
    module=MODULE,
    unit_kind="detector-image",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
