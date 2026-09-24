"""Database-backed tests for `register` recording the difference stage's manifest.

An l2 image is admitted and registered for real (tests/db/test_register_l2.py's
helpers), the difference stage runs with fake tools on an input set whose
l2 entry names that admitted instance, and `register` runs for real against
the difference manifest. Covers the reference resolved both ways -- by its
instance (20260923-02-refimages-instance.sql) and, for a reference
registered by `dev`, by the legacy rfid the manifest carries -- and the
column sources the products page's difference-image field list fixes.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import pytest

import rapidpipe.stages.difference as difference
import rapidpipe.stages.finalize as finalize
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest, register_unit_id
from rapidpipe.runs import repository as repo
from rapidpipe.science.spatial import healpix_indexes
from rapidpipe.stages.contract import ExitCode
from tests.unit.fakedifftools import (
    CDF_DIR,
    FakePsfCatalog,
    FakeToolRunner,
    build_input_set,
    fake_sip_to_pv,
)

from .test_register_l2 import _run_admit, _run_register
from .test_repository import _make_run, _make_unit


def _seed_ids(cur):
    cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
    (fid,) = cur.fetchone()
    cur.execute("SELECT svid FROM swversions ORDER BY svid LIMIT 1")
    (svid,) = cur.fetchone()
    return fid, svid


def _legacy_refimage(cur) -> int:
    """A `refimages` row as `dev` writes one: no run, attempt or instance."""
    fid, svid = _seed_ids(cur)
    cur.execute(
        """
        INSERT INTO refimages (field, hp6, hp9, fid, ppid, version, vbest, svid, infobits)
        VALUES (5321, 1, 1, %s, 12, 1, 1, %s, 0) RETURNING rfid
        """,
        (fid, svid))
    return cur.fetchone()[0]


def _instance_refimage(conn) -> tuple[int, str]:
    """A `refimages` row carrying a registered reference instance."""
    run_id = _make_run(conn)
    _make_unit(conn, run_id, stage="reference", unit_id="field-5321")
    attempt_id = repo.allocate_attempt(conn, run_id, "reference", "field-5321")
    instance = new_ulid()
    repo.register_manifest(conn, {
        "run": run_id, "stage": "reference", "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{
            "kind": "reference-image", "format_version": "1", "instance": instance,
            "key": {"field": "5321", "filter": "F184", "recipe": "awaicgen", "version": "1"},
            "primary": "ref/image.fits",
            "members": [{"role": "image", "path": "ref/image.fits", "bytes": 1,
                         "sha256": "sha256:" + "a" * 64}],
            "registration": {},
        }],
    }, registering_attempt_id=attempt_id)
    with conn.cursor() as cur:
        fid, svid = _seed_ids(cur)
        cur.execute(
            """
            INSERT INTO refimages (field, hp6, hp9, fid, ppid, version, vbest, svid,
                                   infobits, run, attempt, instance)
            VALUES (5321, 1, 1, %s, 12, 1, 0, %s, 0, %s, %s, %s) RETURNING rfid
            """,
            (fid, svid, run_id, attempt_id, instance))
        return cur.fetchone()[0], instance


def _run_difference(conn, tmp_path, monkeypatch, *, l2_instance, reference_instance=None,
                    rfid=None, overlay=""):
    """Run the difference stage (fake tools) in a real run; return (run, outputs)."""
    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=FakeToolRunner(), sip_to_pv=fake_sip_to_pv, psf_catalog=FakePsfCatalog()))

    inputs = tmp_path / "diff-inputs"
    build_input_set(inputs, rfid=rfid)
    manifest_path = inputs / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][0]["instance"] = l2_instance
    if reference_instance is not None:
        manifest["outputs"][1]["instance"] = reference_instance
    manifest_path.write_text(json.dumps(manifest))

    run_id = _make_run(conn, selected_stages=["difference", "register"])
    unit_id = "e20260821001234/SCA07"
    _make_unit(conn, run_id, stage="difference", unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "difference", unit_id)
    settings = tmp_path / "overlay.toml"
    settings.write_text(f'[paths]\ncfg_path = "{CDF_DIR}"\n' + overlay)
    outputs = tmp_path / "diff-outputs"
    rc = difference.main([
        "--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
        "--inputs", str(inputs), "--outputs", str(outputs), "--settings", str(settings)])
    assert rc == int(ExitCode.SUCCESS)
    return run_id, outputs


def _admitted_l2(conn, tmp_path, monkeypatch):
    run_id, admit_outputs = _run_admit(conn, tmp_path, name="l2")
    admit_manifest = Manifest.read(admit_outputs / "manifest.json")
    rc, _ = _run_register(conn, monkeypatch, admit_outputs, run_id=run_id,
                          unit_id=register_unit_id(admit_manifest),
                          tmp_path=tmp_path, name="l2")
    assert rc == int(ExitCode.SUCCESS)
    return admit_manifest.outputs[0].instance


def _register_difference(conn, monkeypatch, outputs, run_id, tmp_path, name="reg"):
    """Register the difference manifest at ``outputs``.

    The registering unit id is derived from that manifest
    (``register_unit_id``, ruling: "a register unit is identified by what
    it registers" -- <producing stage>/<producing unit id>), the same
    derivation the CLI now performs at submission time
    (rapidpipe.cli.main._resolve_register_unit_id) rather than a
    hand-keyed name. Two calls against the *same* ``outputs`` manifest
    (e.g. replay tests) therefore collide on the same unit id by design --
    a distinct ``name`` no longer buys a distinct unit for the same
    producer, matching the "no occurrence counter" rule; a caller that
    wants a second, fresh unit must pass a different producer manifest.
    """
    difference_manifest = Manifest.read(outputs / "manifest.json")
    return _run_register(conn, monkeypatch, outputs, run_id=run_id,
                         unit_id=register_unit_id(difference_manifest),
                         tmp_path=tmp_path, name=name)


def _diffimages_row(cur, instance):
    cur.execute(
        """
        SELECT pid, rid, expid, sca, ppid, version, vbest, rfid, field, hp6, hp9, fid, jd,
               ra0, dec0, ra1, dec4, infobitssci, infobitsref, filename, checksum, status,
               svid, run, attempt, instance
        FROM diffimages WHERE instance = %s
        """,
        (instance,))
    columns = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(columns, row)) if row is not None else None


def _diffimmeta_row(cur, pid):
    cur.execute(
        """
        SELECT nsexcatsources, scalefacref, dxrmsfin, dyrmsfin, dxmedianfin, dymedianfin,
               field, hp6, hp9, fid, sca, source_counts, run, instance
        FROM diffimmeta WHERE pid = %s
        """,
        (pid,))
    columns = [d[0] for d in cur.description]
    return dict(zip(columns, cur.fetchone()))


def test_register_writes_diffimages_and_diffimmeta_for_a_legacy_reference(
        conn, tmp_path, monkeypatch):
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=l2_instance, rfid=rfid)
    manifest = Manifest.read(outputs / "manifest.json")
    entry = next(e for e in manifest.outputs if e.kind == "difference-image")
    registration = entry.registration

    rc, registering_attempt = _register_difference(conn, monkeypatch, outputs, run_id, tmp_path)
    assert rc == int(ExitCode.SUCCESS)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT rid, expid, sca, field, fid, mjdobs FROM l2files WHERE instance = %s",
            (l2_instance,))
        rid, expid, sca, field, fid, mjdobs = cur.fetchone()
        row = _diffimages_row(cur, entry.instance)
        assert row is not None
        hp6, hp9 = healpix_indexes(registration["centre"]["ra"], registration["centre"]["dec"])
        assert (row["rid"], row["expid"], row["sca"], row["field"], row["fid"]) == (
            rid, expid, sca, field, fid)
        assert row["jd"] == pytest.approx(mjdobs + 2400000.5)
        assert row["ppid"] == 15
        assert row["rfid"] == rfid
        assert (row["version"], row["vbest"], row["status"]) == (1, 0, 0)
        assert (row["hp6"], row["hp9"]) == (hp6, hp9)
        assert row["ra0"] == pytest.approx(registration["centre"]["ra"])
        assert row["ra1"] == pytest.approx(registration["corners"][0][0])
        assert row["dec4"] == pytest.approx(registration["corners"][3][1])
        assert row["infobitssci"] == registration["catalog_outcome_bits"]
        assert row["infobitsref"] == registration["infobits_reference"]
        assert row["checksum"] == registration["md5"]
        assert row["filename"] == f"{outputs}/{entry.primary}"
        assert (row["run"], row["attempt"], row["instance"]) == (
            run_id, registering_attempt, entry.instance)
        cur.execute("SELECT cvstag FROM swversions WHERE svid = %s", (row["svid"],))
        assert cur.fetchone()[0] == "abc123"   # _make_run's code_revision

        meta = _diffimmeta_row(cur, row["pid"])
        counts = registration["source_counts"]
        assert meta["nsexcatsources"] == counts["sextractor"]["positive"]
        assert meta["source_counts"] == counts
        residual = registration["registration_residual"]
        assert meta["dxrmsfin"] == pytest.approx(residual["x_rms"])
        assert meta["dyrmsfin"] == pytest.approx(residual["y_rms"])
        assert meta["scalefacref"] == pytest.approx(registration["reference_scale_factor"])
        assert (meta["field"], meta["hp6"], meta["hp9"], meta["fid"], meta["sca"]) == (
            field, hp6, hp9, fid, sca)
        assert (meta["run"], meta["instance"]) == (run_id, entry.instance)

        # Source catalogs: an instance row each, no legacy rows.
        cur.execute(
            "SELECT count(*) FROM product_instances WHERE kind = 'source-catalog' AND run = %s",
            (run_id,))
        assert cur.fetchone()[0] == 4


def test_register_resolves_rfid_from_the_reference_instance(conn, tmp_path, monkeypatch):
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    rfid, reference_instance = _instance_refimage(conn)
    run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=l2_instance,
        reference_instance=reference_instance)
    entry = next(e for e in Manifest.read(outputs / "manifest.json").outputs
                 if e.kind == "difference-image")
    rc, _ = _register_difference(conn, monkeypatch, outputs, run_id, tmp_path)
    assert rc == int(ExitCode.SUCCESS)
    with conn.cursor() as cur:
        assert _diffimages_row(cur, entry.instance)["rfid"] == rfid
        cur.execute(
            "SELECT count(*) FROM dependencies WHERE consumer_instance = %s "
            "AND producer_instance = %s", (entry.instance, reference_instance))
        assert cur.fetchone()[0] == 1


def test_replaying_the_manifest_writes_nothing_new(conn, tmp_path, monkeypatch):
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=l2_instance, rfid=rfid)
    assert _register_difference(conn, monkeypatch, outputs, run_id, tmp_path, "first")[0] == 0
    assert _register_difference(conn, monkeypatch, outputs, run_id, tmp_path, "second")[0] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM diffimages WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1


def test_unregistered_l2_instance_exits_65(conn, tmp_path, monkeypatch):
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    # An l2 instance that was never registered: the dependency edge's
    # foreign key or the l2files lookup refuses it.
    run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=new_ulid(), rfid=rfid)
    rc, _ = _register_difference(conn, monkeypatch, outputs, run_id, tmp_path)
    assert rc in (int(ExitCode.INPUT_REJECTED), int(ExitCode.STAGE_ERROR))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM diffimages WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 0


def test_register_writes_an_sfft_instance_under_its_pipelines_row(conn, tmp_path, monkeypatch):
    # database/migrations/20260924-01-pipelines-sfft.sql (lead ruling 2026-09-24:
    # ppid 16, priority 6) replaces the earlier refusal -- an SFFT instance used
    # to be rejected here for having no `pipelines` row to register under.
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=l2_instance, rfid=rfid,
        overlay="[sfft]\nregister_sfft = true\n")
    manifest = Manifest.read(outputs / "manifest.json")
    sfft_entry = next(e for e in manifest.outputs
                       if e.kind == "difference-image" and e.key["differencer"] == "sfft")
    rc, _ = _register_difference(conn, monkeypatch, outputs, run_id, tmp_path)
    assert rc == int(ExitCode.SUCCESS)
    with conn.cursor() as cur:
        row = _diffimages_row(cur, sfft_entry.instance)
        assert row is not None
        assert row["ppid"] == 16


def _run_finalize(conn, tmp_path, run_id, diff_outputs):
    """Run finalize on ``diff_outputs`` in ``run_id``; return its outputs directory."""
    unit_id = "e20260821001234/SCA07"
    _make_unit(conn, run_id, stage="finalize", unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "finalize", unit_id)
    outputs = tmp_path / "fin-outputs"
    rc = finalize.main([
        "--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
        "--inputs", str(diff_outputs), "--outputs", str(outputs)])
    assert rc == int(ExitCode.SUCCESS)
    return outputs


def test_register_records_the_finalized_instance(conn, tmp_path, monkeypatch):
    # Chain difference -> register(raw) -> finalize -> register(finalized)
    # (supervisor, 2026-09-24, amended): the raw instance is registered
    # first, so the dependency edges finalize's inputs.products names
    # resolve; the finalized instance gets its own diffimages row, the next
    # version for (rid, ppid) within the run.
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, diff_outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=l2_instance, rfid=rfid)
    rc, _ = _register_difference(conn, monkeypatch, diff_outputs, run_id, tmp_path)
    assert rc == int(ExitCode.SUCCESS)
    source = next(e for e in Manifest.read(diff_outputs / "manifest.json").outputs
                  if e.kind == "difference-image")

    outputs = _run_finalize(conn, tmp_path, run_id, diff_outputs)
    manifest = Manifest.read(outputs / "manifest.json")
    entry = next(e for e in manifest.outputs if e.kind == "difference-image")
    assert entry.registration["finalized_from"] == source.instance

    rc, _ = _register_difference(conn, monkeypatch, outputs, run_id, tmp_path, name="fin")
    assert rc == int(ExitCode.SUCCESS)
    with conn.cursor() as cur:
        row = _diffimages_row(cur, entry.instance)
        assert row is not None
        assert row["checksum"] == entry.registration["md5"] != source.registration["md5"]
        assert row["filename"] == f"{outputs}/{entry.primary}"
        assert (row["ppid"], row["rfid"]) == (15, rfid)
        raw = _diffimages_row(cur, source.instance)
        assert (raw["version"], row["version"]) == (1, 2)
        assert raw["rid"] == row["rid"]
        cur.execute(
            "SELECT count(*) FROM product_instances WHERE kind = 'source-catalog' "
            "AND producing_stage = 'finalize' AND run = %s", (run_id,))
        assert cur.fetchone()[0] == sum(1 for e in manifest.outputs if e.kind == "source-catalog")
        cur.execute(
            "SELECT count(*) FROM dependencies WHERE consumer_instance = %s "
            "AND producer_instance = %s", (entry.instance, source.instance))
        assert cur.fetchone()[0] == 1


# ======================================================================
# Two registers in one run: derived ids are distinct per producer
# ======================================================================

def test_two_registers_in_one_run_get_distinct_derived_ids(conn, tmp_path, monkeypatch):
    """A run that registers after both `admit` and `difference` (the
    diffchain shape: admit, register, difference, register) gets two
    distinct `register` units, each named for its own producer -- no
    hand-keyed ``<unit>/difference`` suffix and no occurrence counter
    (Ben, 2026-09-23 ruling: "a register unit is identified by what it
    registers"; id = <producing stage>/<producing unit id>).
    """
    admit_run_id, admit_outputs = _run_admit(conn, tmp_path, name="twice")
    admit_manifest = Manifest.read(admit_outputs / "manifest.json")
    admit_register_id = register_unit_id(admit_manifest)
    rc, admit_registering_attempt = _run_register(
        conn, monkeypatch, admit_outputs, run_id=admit_run_id,
        unit_id=admit_register_id, tmp_path=tmp_path, name="twice-admit-reg")
    assert rc == int(ExitCode.SUCCESS)
    l2_instance = admit_manifest.outputs[0].instance

    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    difference_run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=l2_instance, rfid=rfid)
    difference_manifest = Manifest.read(outputs / "manifest.json")
    difference_register_id = register_unit_id(difference_manifest)
    rc, difference_registering_attempt = _register_difference(
        conn, monkeypatch, outputs, difference_run_id, tmp_path, "twice-diff-reg")
    assert rc == int(ExitCode.SUCCESS)

    assert admit_register_id == f"admit/{admit_manifest.unit.id}"
    assert difference_register_id == f"difference/{difference_manifest.unit.id}"
    assert admit_register_id != difference_register_id

    # Neither register_main nor _run_register calls record_attempt_result/
    # select_attempt (that is run_stage_locally's/submit_unit's job, an
    # end-to-end concern covered by tests/db/test_local_runner.py) -- this
    # test is only about the two units table rows this run now carries,
    # each keyed by its own derived id.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run, stage, unit_id FROM units "
            "WHERE stage = 'register' AND unit_id IN (%s, %s) ORDER BY unit_id",
            (admit_register_id, difference_register_id))
        rows = {row[2]: row for row in cur.fetchall()}

    assert set(rows) == {admit_register_id, difference_register_id}
    assert rows[admit_register_id][0] == admit_run_id
    assert rows[difference_register_id][0] == difference_run_id
    assert admit_registering_attempt != difference_registering_attempt
