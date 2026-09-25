"""Database-backed tests for `register` recording the reference stage's manifest.

Constituent l2 images are admitted and registered for real
(tests/db/test_register_l2.py's helpers); the reference manifest is
written by hand in the shape rulings R5/R6 fix (the `reference` stage is
WP-A's), then `register` runs for real against it. Covers `dev`'s
``addRefImage`` version allocation, ``vbest`` 0, the run columns,
`refimmeta`, one `refimimages` row per constituent, `refimcatalogs` with
columns copied from the `refimages` row, replay, an unregistered
constituent, and a difference manifest that names the new reference
instance resolving ``diffimages.rfid`` through it.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest, register_unit_id
from rapidpipe.runs import cleanup
from rapidpipe.runs import repository as repo
from rapidpipe.science.spatial import healpix_indexes
from rapidpipe.stages.contract import ExitCode
from tests.unit.test_refimage_products import (
    reference_catalog_entry,
    reference_image_entry,
)

from .test_register_difference import _diffimages_row, _register_difference, _run_difference
from .test_register_l2 import _run_admit, _run_register
from .test_repository import _make_run

FIELD = 4711398


def _admitted_l2s(conn, tmp_path, monkeypatch, n=2):
    """Admit and register ``n`` l2 images, each in its own run; return
    their instance ids and the filter name of their fid."""
    instances = []
    for i in range(n):
        run_id, admit_outputs = _run_admit(conn, tmp_path, name=f"l2-{i}")
        manifest = Manifest.read(admit_outputs / "manifest.json")
        rc, _ = _run_register(conn, monkeypatch, admit_outputs, run_id=run_id,
                              unit_id=register_unit_id(manifest), tmp_path=tmp_path,
                              name=f"l2-{i}")
        assert rc == int(ExitCode.SUCCESS)
        instances.append(manifest.outputs[0].instance)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT f.filter FROM l2files l JOIN filters f ON f.fid = l.fid "
            "WHERE l.instance = %s", (instances[0],))
        (filter_,) = cur.fetchone()
    return instances, filter_


def _reference_manifest(conn, tmp_path, name, constituents, filter_, *, field=FIELD,
                        catalogs=("sextractor",), npucatsources=None):
    """Write a reference manifest under a real reference attempt; return
    (run, outputs dir, reference instance, {catalog_type: catalog instance})."""
    run_id = _make_run(conn, selected_stages=["reference", "register"])
    unit_id = f"{field}/{filter_}/{name}"
    repo.add_unit(conn, run_id, "reference", "field", unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "reference", unit_id)
    reference = new_ulid()
    image = reference_image_entry(instance=reference, constituents=constituents, field=field,
                                  filter_=filter_, npucatsources=npucatsources,
                                  version=name.ljust(16, "0")[:16])
    catalog_instances = {}
    outputs = [image]
    for catalog_type in catalogs:
        catalog_instances[catalog_type] = new_ulid()
        outputs.append(reference_catalog_entry(
            instance=catalog_instances[catalog_type], reference=reference,
            catalog_type=catalog_type, path=f"ref/{catalog_type}.txt"))
    out_dir = tmp_path / name / "reference-outputs"
    out_dir.mkdir(parents=True)
    # Catalogs first: register orders the image before them.
    outputs = outputs[1:] + outputs[:1]
    (out_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "field", "id": unit_id},
        "stage": "reference", "attempt": attempt_id,
        "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "input-set/manifest.json", "products": {}, "result_sets": []},
        "outputs": outputs,
    }))
    return run_id, out_dir, reference, catalog_instances


def _register(conn, monkeypatch, outputs, run_id, tmp_path, name):
    manifest = Manifest.read(outputs / "manifest.json")
    return _run_register(conn, monkeypatch, outputs, run_id=run_id,
                         unit_id=register_unit_id(manifest), tmp_path=tmp_path, name=name)


def _row(cur, sql, params):
    cur.execute(sql, params)
    columns = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(columns, row)) if row is not None else None


def _refimages_row(cur, instance):
    return _row(cur, "SELECT * FROM refimages WHERE instance = %s", (instance,))


def test_register_writes_refimages_refimmeta_refimimages_refimcatalogs(
        conn, tmp_path, monkeypatch):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch)
    run_id, outputs, reference, catalogs = _reference_manifest(
        conn, tmp_path, "one", constituents, filter_)
    rc, registering_attempt = _register(conn, monkeypatch, outputs, run_id, tmp_path, "one")
    assert rc == int(ExitCode.SUCCESS)

    block = reference_image_entry(constituents=constituents, filter_=filter_)["registration"]
    hp6, hp9 = healpix_indexes(block["ra_center"], block["dec_center"])
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters WHERE filter = %s", (filter_,))
        (fid,) = cur.fetchone()
        ref = _refimages_row(cur, reference)
        assert ref is not None
        assert (ref["field"], ref["hp6"], ref["hp9"], ref["fid"], ref["ppid"]) == (
            FIELD, hp6, hp9, fid, 12)
        assert (ref["vbest"], ref["status"], ref["infobits"]) == (0, 1, 0)
        assert ref["checksum"] == block["md5"]
        assert ref["filename"] == f"{outputs}/ref/awaicgen_output_mosaic_image.fits"
        # attempt is the PRODUCING (reference) attempt, not register's.
        producing_attempt = Manifest.read(outputs / "manifest.json").attempt
        assert producing_attempt != registering_attempt
        assert (ref["run"], ref["attempt"], ref["instance"]) == (
            run_id, producing_attempt, reference)
        cur.execute("SELECT max(svid) FROM swversions")
        assert ref["svid"] == cur.fetchone()[0]   # addRefImage: the latest swversions row

        meta = _row(cur, "SELECT * FROM refimmeta WHERE rfid = %s", (ref["rfid"],))
        assert (meta["field"], meta["hp6"], meta["hp9"], meta["fid"]) == (FIELD, hp6, hp9, fid)
        assert meta["nframes"] == len(constituents)
        assert meta["mjdobsmin"] == pytest.approx(block["mjdobs_min"])
        assert meta["mjdobsmax"] == pytest.approx(block["mjdobs_max"])
        for name in ("clmean", "clstddev", "gmedian", "datascale", "gmin", "gmax",
                     "cov5percent", "medncov", "medpixunc", "fwhmmedpix", "fwhmminpix",
                     "fwhmmaxpix"):
            assert meta[name] == pytest.approx(block[name], rel=1e-6), name
        assert (meta["npixnan"], meta["clnoutliers"]) == (block["npixnan"], block["clnoutliers"])
        assert meta["nsxcatsources"] == block["nsxcatsources"]
        assert meta["npucatsources"] is None   # no Photutils catalog: null, never 0

        cur.execute(
            "SELECT l.instance FROM refimimages r JOIN l2files l ON l.rid = r.rid "
            "WHERE r.rfid = %s", (ref["rfid"],))
        assert sorted(r[0] for r in cur.fetchall()) == sorted(constituents)

        cat = _row(cur, "SELECT * FROM refimcatalogs WHERE rfid = %s", (ref["rfid"],))
        assert (cat["ppid"], cat["cattype"], cat["status"]) == (12, 1, 1)
        assert (cat["field"], cat["hp6"], cat["hp9"], cat["fid"]) == (FIELD, hp6, hp9, fid)
        assert cat["filename"] == f"{outputs}/ref/sextractor.txt"
        assert cat["checksum"] == block["md5"]

        cur.execute(
            "SELECT kind FROM product_instances WHERE id IN (%s, %s)",
            (reference, catalogs["sextractor"]))
        assert sorted(r[0] for r in cur.fetchall()) == ["reference-catalog", "reference-image"]


def test_npucatsources_is_written_when_measured_and_psf_catalog_is_cattype_2(
        conn, tmp_path, monkeypatch):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    run_id, outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "psf", constituents, filter_, catalogs=("sextractor", "psf"),
        npucatsources=4000)
    assert _register(conn, monkeypatch, outputs, run_id, tmp_path, "psf")[0] == 0
    with conn.cursor() as cur:
        rfid = _refimages_row(cur, reference)["rfid"]
        cur.execute("SELECT npucatsources FROM refimmeta WHERE rfid = %s", (rfid,))
        assert cur.fetchone()[0] == 4000
        cur.execute("SELECT cattype FROM refimcatalogs WHERE rfid = %s ORDER BY cattype",
                    (rfid,))
        assert [r[0] for r in cur.fetchall()] == [1, 2]


def test_a_second_reference_takes_the_next_version(conn, tmp_path, monkeypatch):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    first_run, first_outputs, first, _ = _reference_manifest(
        conn, tmp_path, "a", constituents, filter_)
    assert _register(conn, monkeypatch, first_outputs, first_run, tmp_path, "a")[0] == 0
    second_run, second_outputs, second, _ = _reference_manifest(
        conn, tmp_path, "b", constituents, filter_)
    assert _register(conn, monkeypatch, second_outputs, second_run, tmp_path, "b")[0] == 0
    with conn.cursor() as cur:
        one, two = _refimages_row(cur, first), _refimages_row(cur, second)
        assert two["version"] == one["version"] + 1
        assert (one["vbest"], two["vbest"]) == (0, 0)


def test_replay_writes_nothing_new(conn, tmp_path, monkeypatch):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch)
    run_id, outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "r", constituents, filter_)
    assert _register(conn, monkeypatch, outputs, run_id, tmp_path, "r1")[0] == 0
    with conn.cursor() as cur:
        rfid = _refimages_row(cur, reference)["rfid"]

    def ctids():
        # An UPDATE writes a new tuple version, so an unchanged ctid means
        # the row was not rewritten (registerRefImCatalog/-Meta upsert).
        with conn.cursor() as cur:
            found = {}
            for table in ("refimages", "refimmeta", "refimcatalogs", "refimimages"):
                cur.execute(f"SELECT array_agg(ctid::text ORDER BY ctid) FROM {table} "
                            "WHERE rfid = %s", (rfid,))
                found[table] = cur.fetchone()[0]
            return found

    before = ctids()
    # A second attempt of the same register unit (a retry replaying the manifest).
    assert _replay_as_new_attempt(conn, outputs, run_id, tmp_path, "r2") == 0
    assert ctids() == before
    with conn.cursor() as cur:
        for table in ("refimages", "refimmeta", "refimcatalogs"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE rfid = %s", (rfid,))
            assert cur.fetchone()[0] == 1, table
        cur.execute("SELECT count(*) FROM refimimages WHERE rfid = %s", (rfid,))
        assert cur.fetchone()[0] == len(constituents)
        cur.execute("SELECT count(*) FROM refimages WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1


def test_a_constituent_without_an_l2files_row_exits_65(conn, tmp_path, monkeypatch):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    missing = new_ulid()
    run_id, outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "m", constituents + [missing], filter_)
    rc, _ = _register(conn, monkeypatch, outputs, run_id, tmp_path, "m")
    assert rc == int(ExitCode.INPUT_REJECTED)
    with conn.cursor() as cur:
        assert _refimages_row(cur, reference) is None


def test_an_unknown_filter_exits_65(conn, tmp_path, monkeypatch):
    constituents, _ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    run_id, outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "f", constituents, "F999")
    assert _register(conn, monkeypatch, outputs, run_id, tmp_path, "f")[0] == int(
        ExitCode.INPUT_REJECTED)


def test_the_roman_filter_spelling_finds_the_rapid_row(conn, tmp_path, monkeypatch):
    constituents, _ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    run_id, outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "w", constituents, "F146")   # the filters table says W146
    assert _register(conn, monkeypatch, outputs, run_id, tmp_path, "w")[0] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters WHERE filter = 'W146'")
        (w146,) = cur.fetchone()
        assert _refimages_row(cur, reference)["fid"] == w146


def _replay_as_new_attempt(conn, outputs, run_id, tmp_path, name):
    import rapidpipe.stages.register as register_module

    from .test_register_l2 import _register_argv
    manifest = Manifest.read(outputs / "manifest.json")
    attempt = repo.allocate_attempt(conn, run_id, "register", register_unit_id(manifest))
    return register_module.main(_register_argv(
        outputs, tmp_path / name, run_id=run_id, unit_id=register_unit_id(manifest),
        attempt_id=attempt))


@pytest.mark.parametrize("edit", [
    lambda outputs: outputs[-1]["registration"].update(clmean=0.5),
    lambda outputs: outputs[-1]["registration"].update(npucatsources=7),
    lambda outputs: outputs[-1]["registration"].update(md5="0" * 32),
    lambda outputs: outputs[0]["registration"].update(md5="1" * 32),   # the catalog
])
def test_a_replay_with_different_content_exits_65(conn, tmp_path, monkeypatch, edit):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    run_id, outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "x", constituents, filter_)
    assert _register(conn, monkeypatch, outputs, run_id, tmp_path, "x1")[0] == 0
    path = outputs / "manifest.json"
    manifest = json.loads(path.read_text())
    edit(manifest["outputs"])   # [catalog, image]: _reference_manifest's order
    path.write_text(json.dumps(manifest))
    assert _replay_as_new_attempt(conn, outputs, run_id, tmp_path, "x2") == int(
        ExitCode.INPUT_REJECTED)


def test_a_catalog_naming_an_unregistered_reference_exits_65(conn, tmp_path, monkeypatch):
    run_id = _make_run(conn, selected_stages=["reference", "register"])
    repo.add_unit(conn, run_id, "reference", "field", "cat-only")
    attempt_id = repo.allocate_attempt(conn, run_id, "reference", "cat-only")
    out_dir = tmp_path / "cat-only"
    out_dir.mkdir()
    (out_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1", "run": run_id, "unit": {"kind": "field", "id": "cat-only"},
        "stage": "reference", "attempt": attempt_id, "execution_record": "exec/x.json",
        "inputs": {"manifest": "m", "products": {}, "result_sets": []},
        "outputs": [reference_catalog_entry(instance=new_ulid(), reference=new_ulid())],
    }))
    assert _register(conn, monkeypatch, out_dir, run_id, tmp_path, "cat-only")[0] == int(
        ExitCode.INPUT_REJECTED)


def test_a_difference_naming_the_new_reference_resolves_its_rfid(conn, tmp_path, monkeypatch):
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    ref_run, ref_outputs, reference, _ = _reference_manifest(
        conn, tmp_path, "d", constituents, filter_)
    assert _register(conn, monkeypatch, ref_outputs, ref_run, tmp_path, "d")[0] == 0
    with conn.cursor() as cur:
        rfid = _refimages_row(cur, reference)["rfid"]

    # The difference input set names the new reference instance, rfid null.
    run_id, outputs = _run_difference(
        conn, tmp_path, monkeypatch, l2_instance=constituents[0],
        reference_instance=reference, rfid=None)
    entry = next(e for e in Manifest.read(outputs / "manifest.json").outputs
                 if e.kind == "difference-image")
    assert entry.registration["reference_rfid"] is None
    assert _register_difference(conn, monkeypatch, outputs, run_id, tmp_path)[0] == 0
    with conn.cursor() as cur:
        assert _diffimages_row(cur, entry.instance)["rfid"] == rfid


def test_deleting_a_scratch_run_removes_its_reference_rows(conn, tmp_path, monkeypatch):
    """cleanup: refimimages/refimcatalogs/refimmeta go with their rfid's run
    (supervisor step 8, ruling R7); before this, they blocked deletion."""
    constituents, filter_ = _admitted_l2s(conn, tmp_path, monkeypatch, n=1)
    run_id = _make_run(conn, kind="scratch", selected_stages=["reference", "register"])
    repo.add_unit(conn, run_id, "reference", "field", "del")
    attempt_id = repo.allocate_attempt(conn, run_id, "reference", "del")
    reference = new_ulid()
    out_dir = tmp_path / "del"
    out_dir.mkdir()
    (out_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1", "run": run_id, "unit": {"kind": "field", "id": "del"},
        "stage": "reference", "attempt": attempt_id, "execution_record": "exec/x.json",
        "inputs": {"manifest": "m", "products": {}, "result_sets": []},
        "outputs": [reference_image_entry(instance=reference, constituents=constituents,
                                          filter_=filter_),
                    reference_catalog_entry(instance=new_ulid(), reference=reference)],
    }))
    assert _register(conn, monkeypatch, out_dir, run_id, tmp_path, "del")[0] == 0
    with conn.cursor() as cur:
        rfid = _refimages_row(cur, reference)["rfid"]
        # Resolve every attempt so the deletion fence holds.
        cur.execute("SELECT id FROM attempts WHERE run = %s", (run_id,))
        attempts = [r[0] for r in cur.fetchall()]
    for attempt in attempts:
        repo.record_attempt_result(
            conn, attempt, exit_code=0, disposition="succeeded", output_location=str(out_dir),
            execution_record={"source_revision": "abc123", "schema_version": "1",
                              "settings_hash": "sha256:xyz"},
            scheduler_job_id=None)

    assert cleanup._blocking_references(conn, run_id) == []
    report = cleanup.delete_run(conn, run_id, "brusholme")
    assert report.rows_deleted["refimimages"] == len(constituents)
    assert (report.rows_deleted["refimmeta"], report.rows_deleted["refimcatalogs"],
            report.rows_deleted["refimages"]) == (1, 1, 1)
    with conn.cursor() as cur:
        for table in ("refimages", "refimmeta", "refimcatalogs", "refimimages"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE rfid = %s", (rfid,))
            assert cur.fetchone()[0] == 0, table
        # The constituents' l2files rows, another run's, are untouched.
        cur.execute("SELECT count(*) FROM l2files WHERE instance = %s", (constituents[0],))
        assert cur.fetchone()[0] == 1
        # Tombstones kept: the run and its instance rows stay, marked deleted.
        cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] == "deleted"
        cur.execute("SELECT kind, deletion_state FROM product_instances WHERE run = %s "
                    "ORDER BY kind", (run_id,))
        assert cur.fetchall() == [("reference-catalog", "deleted"),
                                  ("reference-image", "deleted")]


def test_a_reference_of_another_run_naming_this_runs_l2_blocks_deletion(
        conn, tmp_path, monkeypatch):
    run_id, admit_outputs = _run_admit(conn, tmp_path, name="own")
    manifest = Manifest.read(admit_outputs / "manifest.json")
    assert _run_register(conn, monkeypatch, admit_outputs, run_id=run_id,
                         unit_id=register_unit_id(manifest), tmp_path=tmp_path,
                         name="own")[0] == 0
    l2 = manifest.outputs[0].instance
    with conn.cursor() as cur:
        cur.execute("SELECT f.filter FROM l2files l JOIN filters f ON f.fid = l.fid "
                    "WHERE l.instance = %s", (l2,))
        (filter_,) = cur.fetchone()
    ref_run, ref_outputs, _, _ = _reference_manifest(conn, tmp_path, "other", [l2], filter_)
    assert _register(conn, monkeypatch, ref_outputs, ref_run, tmp_path, "other")[0] == 0
    blocking = cleanup._blocking_references(conn, run_id)
    assert any("refimimages" in b and "l2files" in b for b in blocking), blocking
