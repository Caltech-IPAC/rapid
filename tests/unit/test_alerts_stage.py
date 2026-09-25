"""Tests for rapidpipe.stages.alerts with the fake database: every boundary and exit code.

The input set and seed are the packaged fixture's (``rapidpipe.selftest.alerts``);
here the stage runs in process with ``open_database`` monkeypatched, so each
test can change one thing. PostgreSQL-backed behaviour is tests/db/test_alerts.py.
"""

from __future__ import annotations

import contextlib
import io
import json

import fastavro
import pytest

import rapidpipe.stages.alerts as alerts
from rapidpipe.products.alertcontainer import (
    AlertContainerRegistrationError,
    validate_alert_container_entry,
    validate_alert_set_entry,
)
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.alerts import (
    ASSOCIATION_SET_2,
    RESULT_SETS,
    STATISTICS_SET_2,
    UNNAMED_ASSOCIATION_SET,
    UNNAMED_STATISTICS_SET,
    _prepare,
)
from rapidpipe.selftest.runner import load_expected
from rapidpipe.selftest.support.fakealertsdb import (
    ASSOCIATION_SET,
    DIFFERENCE_INSTANCE,
    SOURCE_SET,
    STATISTICS_SET,
    FakeAlertsDatabase,
)
from rapidpipe.stages.contract import ExitCode
from rapidpipe.stages.register import _reject_unknown_kinds

RUN = "01J8Y6QZ3M0000000000000RUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"


@pytest.fixture()
def prepared(tmp_path):
    inputs, overlay, env = _prepare(tmp_path, load_expected("alerts"), True)
    seed = json.loads((tmp_path / "db-seed.json").read_text())
    return inputs, overlay, seed


def _run(tmp_path, monkeypatch, inputs, seed, *, overlay_text="", db=None, attempt=ATTEMPT,
         outputs_name="outputs", extra=()):
    database = db if db is not None else FakeAlertsDatabase(seed)
    monkeypatch.setattr(alerts, "open_database", lambda: contextlib.nullcontext(database))
    argv = ["--run", RUN, "--unit", "e20260821001234/SCA07", "--attempt", attempt,
            "--inputs", str(inputs), "--outputs", str(tmp_path / outputs_name), *extra]
    if overlay_text:
        (tmp_path / "overlay.toml").write_text(overlay_text)
        argv += ["--settings", str(tmp_path / "overlay.toml")]
    return alerts.main(argv), tmp_path / outputs_name, database


def _manifest_inputs(inputs):
    return json.loads((inputs / "manifest.json").read_text())


def _rewrite(inputs, manifest):
    (inputs / "manifest.json").write_text(json.dumps(manifest))


def test_declaration():
    alerts.DECLARATION.validate()
    assert alerts.DECLARATION.consumes == ("difference-image", "reference-catalog", "source-set",
                                           "association-set", "statistics-set")
    assert alerts.DECLARATION.produces == ("alert-container", "alert-set")
    assert alerts.DECLARATION.database_access == "read-write"
    assert alerts.DECLARATION.resource_defaults == {"vcpus": 1, "memory_mib": 8192}


def test_success_writes_container_outbox_and_both_outputs(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == 0
    manifest = Manifest.read(outputs / "manifest.json")
    by_kind = {e.kind: e for e in manifest.outputs}
    container, alert_set = by_kind["alert-container"], by_kind["alert-set"]
    validate_alert_container_entry(container.to_dict())
    validate_alert_set_entry(alert_set.to_dict())
    assert container.registration["alert_count"] == 4
    assert container.registration["dropped_count"] == 2
    assert list(manifest.inputs.result_sets) == RESULT_SETS
    assert container.registration["association_sets"] == [ASSOCIATION_SET, ASSOCIATION_SET_2]
    assert container.registration["statistics_sets"] == [STATISTICS_SET, STATISTICS_SET_2]
    assert container.registration["association_set"] == ASSOCIATION_SET
    assert container.registration["statistics_set"] == STATISTICS_SET
    raw = (outputs / container.primary).read_bytes()
    assert [r["diaSourceId"] for r in fastavro.reader(io.BytesIO(raw))] == [103, 104, 105, 106]
    assert [r["record_ordinal"] for r in db.outbox] == [0, 1, 2, 3]
    # default 129x129 stamps: each alert fills an Avro block of its own
    assert [r["record_index"] for r in db.outbox] == [0, 0, 0, 0]
    assert len({r["block_offset"] for r in db.outbox}) == 4
    assert {r["result_set"] for r in db.outbox} == {alert_set.instance}
    assert db.commits == 1
    assert db.nalertpackets == [{"instance": DIFFERENCE_INSTANCE, "run": RUN, "value": 1}]
    # register can replay this manifest: both kinds are known to it
    _reject_unknown_kinds(manifest.outputs)


def test_kafka_on_exits_64(tmp_path, monkeypatch, prepared, capsys):
    inputs, _, seed = prepared
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed,
                           overlay_text="[publish]\nkafka = true\n")
    assert rc == int(ExitCode.USAGE)
    captured = capsys.readouterr()
    assert alerts.KAFKA_REFUSAL in captured.out + captured.err
    assert not (outputs / "manifest.json").exists() and db.commits == 0


@pytest.mark.parametrize("overlay", [
    "[alerts]\nunknown_key = 1\n",
    "[alerts]\ndiff_flavor = \"naive\"\n",
    "[alerts]\nstamp_half_width = 0\n",
    "[archive]\ncodec = \"snappy\"\n",
    "[alerts]\nkona_file = \"/nonexistent/kona.json\"\n",
])
def test_bad_settings_exit_64(tmp_path, monkeypatch, prepared, overlay):
    inputs, _, seed = prepared
    rc, _, _ = _run(tmp_path, monkeypatch, inputs, seed, overlay_text=overlay)
    assert rc == int(ExitCode.USAGE)


def test_not_an_input_set_exits_65(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    manifest = _manifest_inputs(inputs)
    manifest["stage"] = "difference"
    _rewrite(inputs, manifest)
    rc, _, _ = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_differencer_other_than_diff_flavor_exits_65(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    rc, _, _ = _run(tmp_path, monkeypatch, inputs, seed,
                    overlay_text="[alerts]\ndiff_flavor = \"sfft\"\n")
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_member_checksum_mismatch_exits_65(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    manifest = _manifest_inputs(inputs)
    manifest["outputs"][0]["members"][0]["sha256"] = "sha256:" + "1" * 64
    _rewrite(inputs, manifest)
    rc, _, _ = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == int(ExitCode.INPUT_REJECTED)


@pytest.mark.parametrize("change", ["unknown-kind", "unregistered", "incomplete", "other-difference",
                                    "no-association-set", "two-source-sets",
                                    "statistics-of-an-unnamed-set", "two-statistics-for-one-set"])
def test_result_set_problems_exit_65(tmp_path, monkeypatch, prepared, change):
    inputs, _, seed = prepared
    instances = seed["product_instances"]
    manifest = _manifest_inputs(inputs)
    if change == "unknown-kind":
        instances[STATISTICS_SET]["kind"] = "pruned-set"
    elif change == "unregistered":
        del instances[STATISTICS_SET]
    elif change == "incomplete":
        instances[ASSOCIATION_SET]["complete"] = False
    elif change == "other-difference":
        instances[SOURCE_SET]["key"]["difference"] = "01J8Y6QZ3M00000000000OTHER"
    elif change == "no-association-set":
        manifest["inputs"]["result_sets"] = [SOURCE_SET]
    elif change == "statistics-of-an-unnamed-set":
        instances[STATISTICS_SET_2]["key"] = {"membership": UNNAMED_ASSOCIATION_SET}
    elif change == "two-statistics-for-one-set":
        manifest["inputs"]["result_sets"].append(UNNAMED_STATISTICS_SET)
    elif change == "two-source-sets":
        instances["01J8Y6QZ3M00000000000SRCS9"] = dict(instances[SOURCE_SET])
        manifest["inputs"]["result_sets"].append("01J8Y6QZ3M00000000000SRCS9")
    _rewrite(inputs, manifest)
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert db.commits == 0 and db.outbox == []


def test_unregistered_difference_instance_exits_65(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    seed["diffimages"] = {}
    rc, _, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert db.commits == 0


def test_statistics_set_is_optional(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    manifest = _manifest_inputs(inputs)
    manifest["inputs"]["result_sets"] = [SOURCE_SET, ASSOCIATION_SET, ASSOCIATION_SET_2]
    _rewrite(inputs, manifest)
    rc, outputs, _ = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == 0
    container = next(e for e in Manifest.read(outputs / "manifest.json").outputs
                     if e.kind == "alert-container")
    assert container.registration["statistics_sets"] == []
    assert container.registration["statistics_set"] is None
    raw = (outputs / container.primary).read_bytes()
    objects = {r["diaSourceId"]: r["diaObject"] for r in fastavro.reader(io.BytesIO(raw))}
    # no statistics: sigmas null, nDiaSources the merges count (dev's _stats_sql)
    assert objects[103]["raSigma"] is None and objects[103]["nDiaSources"] == 4


def test_zero_alertable_sources_still_give_a_complete_empty_set(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    for row in seed["sources"]:
        if row["result_set"] == SOURCE_SET:
            row["flags"] = 8
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == 0
    by_kind = {e.kind: e for e in Manifest.read(outputs / "manifest.json").outputs}
    assert by_kind["alert-container"].registration["alert_count"] == 0
    assert by_kind["alert-container"].registration["dropped_count"] == 6
    assert by_kind["alert-set"].registration["row_count"] == 0
    raw = (outputs / by_kind["alert-container"].primary).read_bytes()
    assert list(fastavro.reader(io.BytesIO(raw))) == []
    assert db.outbox == [] and db.commits == 1
    registered = {o["kind"]: o for o in db.registered[0]["manifest"]["outputs"]}
    assert registered["alert-set"]["row_count"] == 0
    assert db.nalertpackets and db.nalertpackets[0]["value"] == 1


def test_rerun_of_the_same_attempt_reuses_what_it_committed(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    db = FakeAlertsDatabase(seed)
    rc, outputs, _ = _run(tmp_path, monkeypatch, inputs, seed, db=db)
    assert rc == 0
    first = {e.kind: e for e in Manifest.read(outputs / "manifest.json").outputs}
    rc, outputs, _ = _run(tmp_path, monkeypatch, inputs, seed, db=db)
    assert rc == 0
    second = {e.kind: e for e in Manifest.read(outputs / "manifest.json").outputs}
    assert second == first
    assert len(db.outbox) == 4 and len(db.registered) == 1 and db.commits == 1


def test_rerun_regenerates_lost_outputs_byte_for_byte(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    db = FakeAlertsDatabase(seed)
    rc, outputs, _ = _run(tmp_path, monkeypatch, inputs, seed, db=db)
    assert rc == 0
    container = next(e for e in Manifest.read(outputs / "manifest.json").outputs
                     if e.kind == "alert-container")
    before = {m.path: (outputs / m.path).read_bytes() for m in container.members}
    for path in before:
        (outputs / path).unlink()
    rc, _, _ = _run(tmp_path, monkeypatch, inputs, seed, db=db)
    assert rc == 0
    assert {p: (outputs / p).read_bytes() for p in before} == before
    assert db.commits == 1


def test_rerun_that_cannot_reproduce_the_registered_bytes_exits_70(tmp_path, monkeypatch,
                                                                   prepared):
    inputs, _, seed = prepared
    db = FakeAlertsDatabase(seed)
    rc, outputs, _ = _run(tmp_path, monkeypatch, inputs, seed, db=db)
    assert rc == 0
    container = next(e for e in Manifest.read(outputs / "manifest.json").outputs
                     if e.kind == "alert-container")
    for member in db.members[container.instance]:
        member["sha256"] = "sha256:" + "f" * 64
    rc, _, _ = _run(tmp_path, monkeypatch, inputs, seed, db=db)
    assert rc == int(ExitCode.STAGE_ERROR)


def test_dry_run_writes_nothing(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed, extra=("--dry-run",))
    assert rc == 0
    assert not (outputs / "manifest.json").exists() and db.commits == 0


def test_alert_container_validation_refuses_a_missing_summary():
    entry = {"kind": "alert-container", "key": {"difference": "x", "schema_version": "00.04"},
             "primary": "a.avro",
             "members": [{"role": "container", "path": "a.avro"}],
             "registration": {"alert_count": 0, "dropped_count": 0, "schema_version": "00.04",
                              "difference": "x", "source_set": "s", "association_sets": ["a"],
                              "statistics_sets": [], "association_set": "a",
                              "statistics_set": None}}
    with pytest.raises(AlertContainerRegistrationError, match="roles"):
        validate_alert_container_entry(entry)
    entry["members"].append({"role": "summary", "path": "a.json"})
    assert validate_alert_container_entry(entry)["alert_count"] == 0
    entry["registration"]["alert_count"] = -1
    with pytest.raises(AlertContainerRegistrationError):
        validate_alert_container_entry(entry)


BASE_SET = "01J8Y6QZ3M00000000000ASSC0"


def _extend_from_a_base(seed):
    """Make the first association set extend a base: as crossmatch leaves them.

    The base holds object 9001 (and a stale copy of 9003), and the merges of
    9001's earlier detections 50 and 51; the extending set holds only the new
    detections' merges rows (103, 104 on 9001, 102, 105) and the objects it
    made itself (9003).
    """
    instances = seed["product_instances"]
    instances[BASE_SET] = {"kind": "association-set", "complete": True,
                           "key": {"field": 5321, "base": None}}
    instances[ASSOCIATION_SET]["key"] = {"field": 5321, "base": BASE_SET}
    for row in seed["merges"]:
        if row["result_set"] == ASSOCIATION_SET and row["sid"] in (50, 51):
            row["result_set"] = BASE_SET
    for row in list(seed["astroobjects"]):
        if row["result_set"] == ASSOCIATION_SET and row["aid"] == 9001:
            row["result_set"] = BASE_SET
        if row["result_set"] == ASSOCIATION_SET and row["aid"] == 9003:
            seed["astroobjects"].append({**row, "result_set": BASE_SET, "ra0": 0.0, "dec0": 0.0})


def test_objects_and_history_resolve_across_the_association_sets_lineage(
        tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    _extend_from_a_base(seed)
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == 0
    manifest = Manifest.read(outputs / "manifest.json")
    # the base is read, so it is an input
    assert list(manifest.inputs.result_sets) == RESULT_SETS + [BASE_SET]
    container = next(e for e in manifest.outputs if e.kind == "alert-container")
    raw = (outputs / container.primary).read_bytes()
    alerts_by_sid = {a["diaSourceId"]: a for a in fastavro.reader(io.BytesIO(raw))}
    # 103 and 104 are new detections of 9001, whose object row is in the base
    assert sorted(alerts_by_sid) == [103, 104, 105, 106]
    assert alerts_by_sid[103]["diaObject"]["diaObjectId"] == 9001
    assert len(alerts_by_sid[103]["prvDiaSources"]) == 2      # 104, and 51 from the base
    # 9003 is in both sets: the extending (newest) set's row wins
    assert alerts_by_sid[105]["diaObject"]["ra0"] != 0.0
    summary = json.loads((outputs / next(m.path for m in container.members
                                          if m.role == "summary")).read_text())
    assert {d["sid"]: d["reason"] for d in summary["dropped"]} == {101: "flagged", 102: "orphan"}


def test_a_broken_lineage_exits_65(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    seed["product_instances"][ASSOCIATION_SET]["key"] = {"field": 5321,
                                                         "base": "01J8Y6QZ3M00000000000GONE0"}
    rc, _, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert db.commits == 0


def test_an_unregistered_input_product_is_read_but_not_a_dependency(tmp_path, monkeypatch,
                                                                    prepared):
    """A dev reference catalog has no instance row: no dependency edge, a note instead."""
    from rapidpipe.selftest.support.fakealertsdb import REFERENCE_CATALOG_INSTANCE
    inputs, _, seed = prepared
    del seed["product_instances"][REFERENCE_CATALOG_INSTANCE]
    rc, outputs, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == 0
    manifest = Manifest.read(outputs / "manifest.json")
    assert manifest.inputs.products == {"difference-image": DIFFERENCE_INSTANCE}
    assert db.registered[0]["manifest"]["inputs"]["products"] == {
        "difference-image": DIFFERENCE_INSTANCE}
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"]["unregistered_inputs"] == {"reference-catalog":
                                                      REFERENCE_CATALOG_INSTANCE}
    # it was still read: the reference-catalog matches are there
    container = next(e for e in manifest.outputs if e.kind == "alert-container")
    raw = (outputs / container.primary).read_bytes()
    assert all(a["refStarMatches"] is not None for a in fastavro.reader(io.BytesIO(raw)))


def test_an_association_set_without_an_integer_field_exits_65(tmp_path, monkeypatch, prepared):
    inputs, _, seed = prepared
    seed["product_instances"][ASSOCIATION_SET]["key"] = {"field": "5321; DROP", "base": None}
    rc, _, db = _run(tmp_path, monkeypatch, inputs, seed)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert db.commits == 0
