"""Database-backed tests for supersession by slot (supervisor step 5a,
2026-09-26): the migration ``20260926-02-product-slots.sql``, the
per-kind derivation (R3, amended by R14/R16/R18), promotion by slot
(R4), the frozen plan (R5), the association-set ancestor rule (R6),
old-release compatibility, and rollback (R9).

Skips cleanly if PGHOST is unset (see conftest.py). Reuses the
run/unit/attempt helpers from test_repository.py and the legacy
FK-parent (l2files/refimages/diffimages) helpers from
test_difference_columns.py, which every kind repository._VBEST_TABLES
maps needs a matching ``dev`` row for before it can be promoted.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.checks.policy import Policy
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo

from .test_difference_columns import (
    _existing_seed_ids,
    _make_exposure,
    _make_l2file,
    _make_refimage,
)
from .test_repository import _make_run, _make_unit, _succeed_and_select

# ======================================================================
# Generic registration helper: one instance of any kind, through the
# public register_manifest, with a fresh run/unit/attempt chain (or an
# existing run_id, for several instances in one run).
# ======================================================================

def _register(
    conn, kind, key, *, run_id=None, instance_id=None, input_products=None,
    stage="test-stage", member_bytes=100, member_sha256="sha256:" + "0" * 64,
):
    if run_id is None:
        run_id = _make_run(conn)
    unit_id = new_ulid()
    stage_name, unit_id = _make_unit(conn, run_id, stage=stage, unit_id=unit_id)
    attempt_id = _succeed_and_select(conn, run_id, stage_name, unit_id)
    instance_id = instance_id or new_ulid()
    manifest = {
        "run": run_id,
        "unit": {"kind": "detector-image", "id": unit_id},
        "stage": stage_name,
        "attempt": attempt_id,
        "inputs": {
            "manifest": "s3://example/manifest.json",
            "products": input_products or {},
            "result_sets": [],
        },
        "outputs": [
            {
                "kind": kind,
                "format_version": "1",
                "instance": instance_id,
                "key": key,
                "primary": f"s3://example/{kind}/{instance_id}.fits",
                "members": [
                    {"role": "primary", "path": f"s3://example/{kind}/{instance_id}.fits",
                     "bytes": member_bytes, "sha256": member_sha256},
                ],
            },
        ],
    }
    repo.register_manifest(conn, manifest, registering_attempt_id=attempt_id)
    return run_id, instance_id


def _row(conn, instance_id):
    """(kind, logical_key, slot, identity, custody) of one instance."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, logical_key, slot, identity, custody "
            "FROM product_instances WHERE id = %s",
            (instance_id,))
        return cur.fetchone()


def _slot_identity(conn, instance_id):
    _kind, _key, slot, identity, _custody = _row(conn, instance_id)
    return slot, identity


def _set_custody(conn, instance_id, custody):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE product_instances SET custody = %s WHERE id = %s",
            (custody, instance_id))


def _clear_slot(conn, instance_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE product_instances SET slot = NULL WHERE id = %s", (instance_id,))


#: A policy with no checks at all: every kind's `checks_for_kind` is
#: empty, so promote_run's check-policy gate never looks for a `checks`
#: row (this step's tests are not about check policy; a real check like
#: `difference-image-statistics` would need a populated `diffimmeta` row
#: this file's synthetic diffimages fixtures do not create).
_NO_CHECKS_POLICY = Policy(
    name="wpb-slot-tests", version="1", approval="trial", approved_by="brusholme",
    auto_promote=False, checks=())


# ======================================================================
# Fixtures needing a dev-table row: l2-image, reference-image,
# difference-image (repository._VBEST_TABLES) so promote()/promote_run
# can maintain vbest without refusing "has no {table} row".
# ======================================================================

#: exposures.dateobs is UNIQUE and derived from the `field` argument to
#: `_make_exposure` (now() + field seconds, now() frozen for the whole
#: test transaction); this counter keeps every exposure this file
#: creates at a distinct `field`, independent of the JSON key's own
#: "exposure" value, which two rows in the same (kind, slot) legitimately
#: share (predicate 5, 6).
_exposure_field_seq = [0]


def _next_exposure_field():
    _exposure_field_seq[0] += 1
    return 900_000 + _exposure_field_seq[0]


def _register_l2(conn, exposure, detector, version=1, *, run_id=None, instance_id=None):
    run_id_out, instance_id_out = _register(
        conn, "l2-image", {"exposure": exposure, "detector": detector, "version": version},
        run_id=run_id, instance_id=instance_id)
    with conn.cursor() as cur:
        fid, sca, _ppid, _svid = _existing_seed_ids(cur)
        field = _next_exposure_field()
        expid = _make_exposure(cur, fid, field=field)
        _make_l2file(cur, expid, sca, fid, version=version, field=field)
        cur.execute(
            "UPDATE l2files SET run = %s, attempt = %s, instance = %s "
            "WHERE expid = %s AND sca = %s AND version = %s",
            (run_id_out, _selected_attempt(conn, run_id_out), instance_id_out,
             expid, sca, version))
    return run_id_out, instance_id_out


def _selected_attempt(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT selected_attempt FROM units WHERE run = %s "
            "ORDER BY created DESC LIMIT 1", (run_id,))
        return cur.fetchone()[0]


def _register_reference(conn, field, filt, recipe="coadd-v1", version=1, *,
                         run_id=None, instance_id=None):
    run_id_out, instance_id_out = _register(
        conn, "reference-image",
        {"field": field, "filter": filt, "recipe": recipe, "version": version},
        run_id=run_id, instance_id=instance_id)
    with conn.cursor() as cur:
        fid, _sca, ppid, svid = _existing_seed_ids(cur)
        rfid = _make_refimage(cur, fid, ppid, svid, field=field, version=version)
        cur.execute(
            "UPDATE refimages SET run = %s, attempt = %s, instance = %s WHERE rfid = %s",
            (run_id_out, _selected_attempt(conn, run_id_out), instance_id_out, rfid))
    return run_id_out, instance_id_out


_DIFFIMAGE_COLUMNS = (
    "rid, expid, sca, ppid, version, vbest, rfid, field, hp6, hp9, fid, "
    "ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4, "
    "infobitssci, infobitsref, filename, svid"
)


def _register_difference(conn, l2_instance, reference_instance, differencer="zogy",
                          settings_hash="sha256:" + "s" * 8, *, run_id=None,
                          instance_id=None, ppid=None):
    l2_slot, _ = _slot_identity(conn, l2_instance)
    exposure = l2_slot["exposure"]
    run_id_out, instance_id_out = _register(
        conn, "difference-image",
        {"l2": l2_instance, "reference": reference_instance,
         "differencer": differencer, "settings_hash": settings_hash},
        run_id=run_id, instance_id=instance_id)
    with conn.cursor() as cur:
        cur.execute("SELECT rid, expid, sca, fid, version FROM l2files WHERE instance = %s",
                    (l2_instance,))
        rid, expid, sca, fid, l2version = cur.fetchone()
        cur.execute("SELECT rfid, svid FROM refimages WHERE instance = %s",
                    (reference_instance,))
        rfid, svid = cur.fetchone()
        if ppid is None:
            cur.execute("SELECT ppid FROM pipelines ORDER BY ppid LIMIT 1")
            (ppid,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO diffimages ({_DIFFIMAGE_COLUMNS}, run, attempt, instance)
            VALUES (
                %(rid)s, %(expid)s, %(sca)s, %(ppid)s, %(version)s, 1,
                %(rfid)s, %(field)s, 1, 1, %(fid)s,
                10.0, 20.0, 9.9, 19.9, 10.1, 19.9, 10.1, 20.1, 9.9, 20.1,
                0, 0, %(filename)s, %(svid)s, %(run)s, %(attempt)s, %(instance)s
            )
            """,
            {"rid": rid, "expid": expid, "sca": sca, "version": l2version,
             "rfid": rfid, "field": exposure, "fid": fid, "svid": svid,
             "filename": f"s3://example/diff/{instance_id_out}.fits",
             "run": run_id_out, "attempt": _selected_attempt(conn, run_id_out),
             "instance": instance_id_out, "ppid": ppid},
        )
    return run_id_out, instance_id_out


# ======================================================================
# Predicate 1: migration objects exist.
# ======================================================================

def test_migration_objects_exist(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'product_instances' AND column_name IN ('slot', 'identity')")
        assert {r[0] for r in cur.fetchall()} == {"slot", "identity"}

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'promotion_changes' AND column_name = 'slot'")
        assert cur.fetchone() == ("slot",)

        cur.execute("SELECT to_regclass('slot_backfill_log')")
        assert cur.fetchone()[0] == "slot_backfill_log"

        cur.execute("SELECT to_regprocedure('product_identity_fill()')")
        assert cur.fetchone()[0] is not None

        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'product_instances' "
                    "AND indexname = 'product_instances_current_slot_uq'")
        assert cur.fetchone() is not None

        # The pre-existing (kind, logical_key) index still exists (additive rule).
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'product_instances' "
                    "AND indexname = 'product_instances_current_key_uq'")
        assert cur.fetchone() is not None


# ======================================================================
# Predicate 2: idempotence.
# ======================================================================

def test_migration_file_applies_twice_without_error_or_row_change(conn):
    with open("database/migrations/20260926-02-product-slots.sql") as fh:
        sql_text = fh.read()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM product_instances")
        (before_count,) = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM product_instances WHERE slot IS NOT NULL")
        (before_slotted,) = cur.fetchone()

        # A second application, inside this test's own transaction: no
        # exception, and no row's slot/identity/custody changes.
        cur.execute(sql_text)

        cur.execute("SELECT count(*) FROM product_instances")
        (after_count,) = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM product_instances WHERE slot IS NOT NULL")
        (after_slotted,) = cur.fetchone()

    assert after_count == before_count
    assert after_slotted == before_slotted


# ======================================================================
# Predicate 3: derivation per kind.
# ======================================================================

def test_derivation_per_kind_exact_json(conn):
    _run, l2 = _register_l2(conn, 34001002, 1, version=3)
    slot, identity = _slot_identity(conn, l2)
    assert slot == {"exposure": 34001002, "detector": 1}
    assert identity == {"exposure": 34001002, "detector": 1, "version": 3}

    _run, ref = _register_reference(conn, 1000, "F184", recipe="coadd-v1", version=2)
    slot, identity = _slot_identity(conn, ref)
    assert slot == {"field": 1000, "filter": "F184"}
    assert identity == {"field": 1000, "filter": "F184", "recipe": "coadd-v1", "version": 2}

    _run, refcat = _register(
        conn, "reference-catalog", {"reference": ref, "catalog_type": "gaia"})
    slot, identity = _slot_identity(conn, refcat)
    assert slot == {"field": 1000, "filter": "F184", "catalog_type": "gaia"}
    assert identity == {"field": 1000, "filter": "F184", "recipe": "coadd-v1",
                        "version": 2, "catalog_type": "gaia"}

    _run, diff = _register_difference(conn, l2, ref, differencer="zogy",
                                       settings_hash="sha256:diff1")
    slot, identity = _slot_identity(conn, diff)
    assert slot == {"exposure": 34001002, "detector": 1, "differencer": "zogy"}
    assert identity == {
        "exposure": 34001002, "detector": 1, "version": 3, "differencer": "zogy",
        "reference": {"field": 1000, "filter": "F184", "recipe": "coadd-v1", "version": 2},
        "settings_hash": "sha256:diff1",
    }

    _run, srccat = _register(
        conn, "source-catalog",
        {"difference": diff, "catalog_type": "sextractor", "sign": "positive"})
    slot, identity = _slot_identity(conn, srccat)
    assert slot == {"exposure": 34001002, "detector": 1, "differencer": "zogy",
                    "catalog_type": "sextractor", "sign": "positive"}
    assert identity == {**_slot_identity(conn, diff)[1],
                        "catalog_type": "sextractor", "sign": "positive"}

    _run, srcset = _register(
        conn, "source-set", {"difference": diff, "catalog_type": "sextractor"})
    slot, identity = _slot_identity(conn, srcset)
    assert slot == {"exposure": 34001002, "detector": 1, "differencer": "zogy",
                    "catalog_type": "sextractor"}
    assert identity == {**_slot_identity(conn, diff)[1], "catalog_type": "sextractor"}

    _run, alertc = _register(
        conn, "alert-container", {"difference": diff, "schema_version": "4.2"})
    slot, identity = _slot_identity(conn, alertc)
    assert slot == {"exposure": 34001002, "detector": 1, "differencer": "zogy"}
    assert identity == {**_slot_identity(conn, diff)[1], "schema_version": "4.2"}

    _run, alerts = _register(
        conn, "alert-set", {"difference": diff, "schema_version": "4.2"})
    slot, identity = _slot_identity(conn, alerts)
    assert slot == {"exposure": 34001002, "detector": 1, "differencer": "zogy"}
    assert identity == {**_slot_identity(conn, diff)[1], "schema_version": "4.2"}

    _run, assoc0 = _register(
        conn, "association-set",
        {"field": 1000, "base": None, "source_sets": [srcset], "settings_hash": "sha256:a0"})
    slot, identity = _slot_identity(conn, assoc0)
    assert slot == {"field": 1000}
    assert identity["field"] == 1000
    assert identity["settings_hash"] == "sha256:a0"
    assert identity["source_sets"] == [_slot_identity(conn, srcset)[1]]
    assert identity["base"] is None

    _run, assoc1 = _register(
        conn, "association-set",
        {"field": 1000, "base": assoc0, "source_sets": [srcset],
         "settings_hash": "sha256:a1"})
    slot, identity = _slot_identity(conn, assoc1)
    assert slot == {"field": 1000}
    assert identity["settings_hash"] == "sha256:a1"
    assert isinstance(identity["base"], str) and len(identity["base"]) == 64

    _run, pruned = _register(
        conn, "pruned-set", {"base": assoc1, "settings_hash": "sha256:p1"})
    slot, identity = _slot_identity(conn, pruned)
    assert slot == {"field": 1000}
    assert identity == {"association": _slot_identity(conn, assoc1)[1],
                        "settings_hash": "sha256:p1"}

    _run, stats = _register(conn, "statistics-set", {"membership": assoc1})
    slot, identity = _slot_identity(conn, stats)
    assert slot == {"field": 1000, "membership_kind": "association-set"}
    assert identity == {"membership": _slot_identity(conn, assoc1)[1],
                        "membership_kind": "association-set"}

    export_key = {"field": 1000, "export_type": "full", "digest": "sha256:exp1"}
    _run, export = _register(conn, "catalog-export", export_key)
    slot, identity = _slot_identity(conn, export)
    assert slot == {"export_type": "full", "field": 1000}
    assert identity == export_key


def test_derivation_association_set_equal_deltas_different_bases_differ(conn):
    """R14: two association sets with the same field/settings_hash/source_sets
    but different bases get different identities (the base hash chain)."""
    _run, _srcset = _register(
        conn, "source-set", {"difference": new_ulid(), "catalog_type": "sextractor"})
    # _srcset's producer difference doesn't exist, so it itself is unresolved
    # (slot/identity NULL) -- fine, we only need *an* instance id to hold as a
    # (constant) source_sets element; what varies below is base.
    _run, base_a = _register(
        conn, "association-set",
        {"field": 2000, "base": None, "source_sets": [], "settings_hash": "sha256:base-a"})
    _run, base_b = _register(
        conn, "association-set",
        {"field": 2000, "base": None, "source_sets": [], "settings_hash": "sha256:base-b"})

    _run, over_a = _register(
        conn, "association-set",
        {"field": 2000, "base": base_a, "source_sets": [], "settings_hash": "sha256:same"})
    _run, over_b = _register(
        conn, "association-set",
        {"field": 2000, "base": base_b, "source_sets": [], "settings_hash": "sha256:same"})

    _, identity_a = _slot_identity(conn, over_a)
    _, identity_b = _slot_identity(conn, over_b)
    assert identity_a != identity_b
    assert identity_a["base"] != identity_b["base"]
    assert identity_a["settings_hash"] == identity_b["settings_hash"] == "sha256:same"


def test_missing_producer_registers_unresolved_then_resolves(conn):
    missing_l2_id = new_ulid()
    _run, ref = _register_reference(conn, 3000, "F129")
    _run, diff = _register(
        conn, "difference-image",
        {"l2": missing_l2_id, "reference": ref, "differencer": "zogy",
         "settings_hash": "sha256:pending"})
    slot, identity = _slot_identity(conn, diff)
    assert slot is None
    assert identity is None

    with conn.cursor() as cur:
        report = repo.fill_identity(cur)
    by_kind = {kind: (converted, unresolved, dup) for kind, converted, unresolved, dup in report}
    assert by_kind.get("difference-image", (0, 0, 0))[1] >= 1  # unresolved

    # Register the missing producer; register_manifest's own internal
    # fill_identity call (R2) then resolves the waiting difference-image.
    _register_l2(conn, 34009009, 1, version=1, instance_id=missing_l2_id)

    slot, identity = _slot_identity(conn, diff)
    assert slot is not None
    assert slot["differencer"] == "zogy"


# ======================================================================
# Predicate 4: old-release (rebuild-v0.8) compatibility.
# ======================================================================

def test_old_release_v08_insert_then_fill_then_promote_by_slot(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = _succeed_and_select(conn, run_id, stage, unit_id)
    instance_id = new_ulid()
    key = {"exposure": 60001, "detector": 2, "version": 1}

    with conn.cursor() as cur:
        # Exact INSERT text of rebuild-v0.8:rapidpipe/runs/repository.py,
        # `_register_one_output`, lines 1050-1054: the column list predates
        # `slot`/`identity`, which this migration adds nullable, so the
        # v0.8 text runs unmodified against the migrated schema and leaves
        # both NULL.
        cur.execute(
            """
            INSERT INTO product_instances (
                id, kind, logical_key, run, producing_stage, producing_attempt,
                registering_attempt, custody, format_version, primary_location,
                manifest_ref
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                instance_id, "l2-image", json.dumps(key), run_id, stage,
                attempt_id, attempt_id, "candidate",
                "1", f"s3://example/l2/{instance_id}.fits", "",
            ),
        )

    slot, identity = _slot_identity(conn, instance_id)
    assert slot is None
    assert identity is None

    with conn.cursor() as cur:
        repo.fill_identity(cur)
    slot, identity = _slot_identity(conn, instance_id)
    assert slot == {"exposure": 60001, "detector": 2}
    assert identity == {"exposure": 60001, "detector": 2, "version": 1}

    with conn.cursor() as cur:
        fid, sca, _ppid, _svid = _existing_seed_ids(cur)
        expid = _make_exposure(cur, fid, field=60001)
        _make_l2file(cur, expid, sca, fid, version=1, field=60001)
        cur.execute(
            "UPDATE l2files SET run = %s, attempt = %s, instance = %s "
            "WHERE expid = %s AND sca = %s AND version = %s",
            (run_id, attempt_id, instance_id, expid, sca, 1))

    promotion_id = repo.promote_run(
        conn, run_id, who="brusholme", reason="old-image promotion",
        allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)
    assert promotion_id
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        (custody,) = cur.fetchone()
        cur.execute("SELECT slot FROM promotion_changes WHERE promotion = %s", (promotion_id,))
        (recorded_slot,) = cur.fetchone()
    assert custody == "current"
    assert recorded_slot == {"exposure": 60001, "detector": 2}


# ======================================================================
# Predicate 5: duplicate currents (R12).
# ======================================================================

def test_duplicate_currents_withheld_and_logged(conn):
    _run_a, a = _register_l2(conn, 50001, 1, version=1)
    _run_b, b = _register_l2(conn, 50001, 1, version=2)
    _clear_slot(conn, a)
    _clear_slot(conn, b)
    _set_custody(conn, a, "current")
    _set_custody(conn, b, "current")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM slot_backfill_log")
        (before_log,) = cur.fetchone()
        report = repo.fill_identity(cur)
        cur.execute("SELECT count(*) FROM slot_backfill_log")
        (after_log,) = cur.fetchone()

    by_kind = {kind: (converted, unresolved, dup) for kind, converted, unresolved, dup in report}
    assert by_kind["l2-image"][2] >= 2  # duplicate_current
    assert after_log > before_log

    # Calling it again is stable: still withheld, no error (the unique
    # index would refuse a real UPDATE that gave both the same slot).
    with conn.cursor() as cur:
        repo.fill_identity(cur)
    slot_a, _ = _slot_identity(conn, a)
    slot_b, _ = _slot_identity(conn, b)
    assert slot_a is None
    assert slot_b is None


# ======================================================================
# Predicate 6: supersession by slot (R4).
# ======================================================================

def test_supersession_by_slot(conn):
    _run, l2 = _register_l2(conn, 70001, 3, version=1)
    _run, ref = _register_reference(conn, 4000, "F184")
    run1, diff1 = _register_difference(conn, l2, ref, differencer="zogy",
                                        settings_hash="sha256:run1")
    repo.promote_run(conn, run1, who="brusholme", reason="run1", allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (diff1,))
        assert cur.fetchone()[0] == "current"

    run2, diff2 = _register_difference(conn, l2, ref, differencer="zogy",
                                        settings_hash="sha256:run2")
    promo2 = repo.promote_run(conn, run2, who="brusholme", reason="run2",
                               allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)

    with conn.cursor() as cur:
        cur.execute("SELECT id, custody FROM product_instances WHERE id IN (%s, %s)",
                    (diff1, diff2))
        rows = dict(cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM product_instances WHERE kind = 'difference-image' "
            "AND custody = 'current' AND slot = %s",
            (json.dumps({"exposure": 70001, "detector": 3, "differencer": "zogy"}),))
        (current_count,) = cur.fetchone()
        cur.execute(
            "SELECT slot, before_instance, after_instance FROM promotion_changes "
            "WHERE promotion = %s", (promo2,))
        slot, before, after = cur.fetchone()

    assert rows[diff1] == "candidate"
    assert rows[diff2] == "current"
    assert current_count == 1
    assert slot == {"exposure": 70001, "detector": 3, "differencer": "zogy"}
    assert before == diff1
    assert after == diff2


def test_promote_run_refuses_two_candidates_in_one_slot(conn):
    _run, l2 = _register_l2(conn, 70101, 5, version=1)
    _run, ref = _register_reference(conn, 4100, "F184")
    run_id, _diff_a = _register_difference(conn, l2, ref, settings_hash="sha256:a", ppid=15)
    _register_difference(conn, l2, ref, settings_hash="sha256:b", run_id=run_id, ppid=17)
    with pytest.raises(repo.PromotionRefused, match="more than one candidate"):
        repo.promote_run(conn, run_id, who="brusholme", reason="dup", allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)


def test_promote_run_refuses_a_candidate_with_no_slot(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    _succeed_and_select(conn, run_id, stage, unit_id)
    instance_id = new_ulid()
    # An unknown kind never derives a slot (R3: "An unknown kind: NULL").
    _run_out, instance_id = _register(
        conn, "unknown-kind-for-test", {"anything": "here"},
        run_id=run_id, instance_id=instance_id)
    with pytest.raises(repo.PromotionRefused, match="has no slot"):
        repo.promote_run(conn, run_id, who="brusholme", reason="no-slot",
                          kinds=["unknown-kind-for-test"], allow_unreleased=True)


# ======================================================================
# Predicate 7 (library form): frozen plan and StalePlan (R5).
# ======================================================================

def test_promotion_plan_matches_and_stale_plan_refuses(conn):
    _run, l2 = _register_l2(conn, 70201, 6, version=1)
    _run, ref = _register_reference(conn, 4200, "F184")
    run1, diff1 = _register_difference(conn, l2, ref, settings_hash="sha256:plan1")
    repo.promote_run(conn, run1, who="brusholme", reason="seed", allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)

    run2, diff2 = _register_difference(conn, l2, ref, settings_hash="sha256:plan2")
    plan = repo.promotion_plan(conn, run2)
    assert plan == [{
        "kind": "difference-image",
        "slot": {"exposure": 70201, "detector": 6, "differencer": "zogy"},
        "before": diff1, "after": diff2,
    }]

    # Nothing moved: applying the frozen plan succeeds.
    promotion_id = repo.promote_run(
        conn, run2, who="brusholme", reason="apply plan", allow_unreleased=True, check_policy=_NO_CHECKS_POLICY, plan=plan)
    assert promotion_id
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (diff2,))
        assert cur.fetchone()[0] == "current"

    # A fresh plan for a THIRD run; an intervening promotion into that
    # plan's slot (rolling diff2 back out is enough to move the slot's
    # current-selection away from what the plan recorded) makes it stale.
    run3, diff3 = _register_difference(conn, l2, ref, settings_hash="sha256:plan3")
    plan3 = repo.promotion_plan(conn, run3)
    assert plan3[0]["before"] == diff2

    repo.promote(
        conn, who="brusholme", reason="intervene",
        changes=[("difference-image",
                  {"slot": {"exposure": 70201, "detector": 6, "differencer": "zogy"}},
                  diff2, None)],
        allow_unreleased=True)

    with pytest.raises(repo.StalePlan, match="stale plan"):
        repo.promote_run(conn, run3, who="brusholme", reason="apply stale",
                          allow_unreleased=True, check_policy=_NO_CHECKS_POLICY, plan=plan3)
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (diff3,))
        assert cur.fetchone()[0] == "candidate"  # nothing written


# ======================================================================
# Predicate 8: association-set ancestor rule (R6, R13).
# ======================================================================

def test_association_ancestor_rule(conn):
    _run, srcset = _register(
        conn, "source-set", {"difference": new_ulid(), "catalog_type": "sextractor"})
    _run, base = _register(
        conn, "association-set",
        {"field": 8000, "base": None, "source_sets": [srcset], "settings_hash": "sha256:base"})
    repo.promote(
        conn, who="brusholme", reason="seed base",
        changes=[("association-set", {"slot": {"field": 8000}}, None, base)],
        allow_unreleased=True)

    # An ordinary next-date extension: base = the current instance. Promoted.
    _run, child = _register(
        conn, "association-set",
        {"field": 8000, "base": base, "source_sets": [srcset], "settings_hash": "sha256:child"})
    repo.promote(
        conn, who="brusholme", reason="extend",
        changes=[("association-set", {"slot": {"field": 8000}}, base, child)],
        allow_unreleased=True)
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (child,))
        assert cur.fetchone()[0] == "current"

    # A chain switch attempt: unrelated base (not descended from `child`).
    _run, unrelated = _register(
        conn, "association-set",
        {"field": 8000, "base": None, "source_sets": [srcset], "settings_hash": "sha256:switch"})
    with pytest.raises(repo.PromotionRefused, match="chain switch is not implemented"):
        repo.promote(
            conn, who="brusholme", reason="switch",
            changes=[("association-set", {"slot": {"field": 8000}}, child, unrelated)],
            allow_unreleased=True)
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (child,))
        assert cur.fetchone()[0] == "current"  # refused as a whole; nothing changed


# ======================================================================
# Predicate 9: rollback (R9, R13).
# ======================================================================

def test_rollback_legacy_promotion_recorded_without_a_slot(conn):
    """(a): a promotion recorded BEFORE the migration, both instances'
    slot stripped to NULL (legacy state); rollback_promotion restores the
    earlier selection exactly, selecting by logical_key since the
    recorded change's slot is NULL."""
    _run, srcset = _register(
        conn, "source-set", {"difference": new_ulid(), "catalog_type": "sextractor"})
    # Pre-migration, promotion matched (kind, logical_key) exactly: a
    # superseding instance necessarily carried the SAME logical_key as the
    # one it replaced (the old unique index was on (kind, logical_key)).
    shared_key = {"field": 9000, "base": None, "source_sets": [srcset],
                  "settings_hash": "sha256:shared"}
    _run, old = _register(conn, "association-set", shared_key)
    _run, new = _register(conn, "association-set", shared_key)
    _clear_slot(conn, old)
    _clear_slot(conn, new)
    _set_custody(conn, old, "candidate")
    _set_custody(conn, new, "current")

    with conn.cursor() as cur:
        promotion_id = new_ulid()
        cur.execute(
            "INSERT INTO promotions (id, who, reason) VALUES (%s, %s, %s)",
            (promotion_id, "brusholme", "legacy promotion, recorded by hand"))
        cur.execute(
            "INSERT INTO promotion_changes "
            "(id, promotion, kind, logical_key, slot, before_instance, after_instance) "
            "VALUES (%s, %s, %s, %s, NULL, %s, %s)",
            (new_ulid(), promotion_id, "association-set", json.dumps(shared_key), old, new))

    reversing_id = repo.rollback_promotion(
        conn, promotion_id, who="brusholme", reason="undo the legacy promotion")
    assert reversing_id

    with conn.cursor() as cur:
        cur.execute("SELECT id, custody FROM product_instances WHERE id IN (%s, %s)",
                    (old, new))
        rows = dict(cur.fetchall())
        cur.execute(
            "SELECT before_instance, after_instance FROM promotion_changes "
            "WHERE promotion = %s", (reversing_id,))
        rev_before, rev_after = cur.fetchone()
    assert rows[old] == "current"
    assert rows[new] == "candidate"
    assert rev_before == new
    assert rev_after == old


def test_rollback_slot_promotion_exact_inverse_and_refused_when_moved(conn):
    """(b): a slot promotion rolls back the same way; (c): refused when a
    later promotion moved the slot."""
    _run, l2 = _register_l2(conn, 70301, 7, version=1)
    _run, ref = _register_reference(conn, 4300, "F184")
    run1, diff1 = _register_difference(conn, l2, ref, settings_hash="sha256:roll1")
    repo.promote_run(conn, run1, who="brusholme", reason="seed", allow_unreleased=True,
                      check_policy=_NO_CHECKS_POLICY)

    run2, diff2 = _register_difference(conn, l2, ref, settings_hash="sha256:roll2")
    promo2 = repo.promote_run(conn, run2, who="brusholme", reason="reprocess",
                               allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)

    reversing_id = repo.rollback_promotion(
        conn, promo2, who="brusholme", reason="undo reprocess")
    with conn.cursor() as cur:
        cur.execute("SELECT id, custody FROM product_instances WHERE id IN (%s, %s)",
                    (diff1, diff2))
        rows = dict(cur.fetchall())
    assert rows[diff1] == "current"
    assert rows[diff2] == "candidate"

    # Roll promo2 forward again (reprocess is current once more), then a
    # THIRD run supersedes it; rolling back promo2 is now refused because
    # its recorded after-selection (diff2) is no longer current.
    repo.rollback_promotion(conn, reversing_id, who="brusholme", reason="redo")
    run3, _diff3 = _register_difference(conn, l2, ref, settings_hash="sha256:roll3")
    repo.promote_run(conn, run3, who="brusholme", reason="supersede again",
                      allow_unreleased=True, check_policy=_NO_CHECKS_POLICY)

    with pytest.raises(repo.PromotionRefused):
        repo.rollback_promotion(conn, promo2, who="brusholme", reason="stale rollback")


# ======================================================================
# Predicate 11: catalog-counts-vs-reference finds its reference by slot
# (R8, R16).
# ======================================================================

def _register_result_set(conn, kind, key, *, row_count=None, run_id=None, instance_id=None):
    """Register a database result set (no members, a ``row_count``), the
    shape ``catalog-counts-vs-reference`` reads."""
    if run_id is None:
        run_id = _make_run(conn)
    unit_id = new_ulid()
    stage_name, unit_id = _make_unit(conn, run_id, stage="test-stage", unit_id=unit_id)
    attempt_id = _succeed_and_select(conn, run_id, stage_name, unit_id)
    instance_id = instance_id or new_ulid()
    manifest = {
        "run": run_id,
        "unit": {"kind": "detector-image", "id": unit_id},
        "stage": stage_name,
        "attempt": attempt_id,
        "inputs": {"manifest": "s3://example/manifest.json", "products": {}, "result_sets": []},
        "outputs": [
            {"kind": kind, "format_version": "1", "instance": instance_id, "key": key,
             "primary": None, "members": [], "row_count": row_count},
        ],
    }
    repo.register_manifest(conn, manifest, registering_attempt_id=attempt_id)
    return run_id, instance_id


def test_catalog_counts_vs_reference_finds_reference_by_slot(conn):
    from rapidpipe.checks.builtin import catalog_counts_vs_reference

    _run, l2 = _register_l2(conn, 80001, 9, version=1)
    _run, ref = _register_reference(conn, 5000, "F184")
    run_a, diff_a = _register_difference(conn, l2, ref, settings_hash="sha256:chk-a")
    run_b, diff_b = _register_difference(conn, l2, ref, settings_hash="sha256:chk-b")

    _run_a2, ref_srcset = _register_result_set(
        conn, "source-set", {"difference": diff_a, "catalog_type": "sextractor"},
        row_count=100, run_id=run_a)
    _set_custody(conn, ref_srcset, "current")

    _run_b2, cand_srcset = _register_result_set(
        conn, "source-set", {"difference": diff_b, "catalog_type": "sextractor"},
        row_count=104, run_id=run_b)

    params = {"tolerance": 0.1, "missing_reference": "fail", "reference_run": None}
    result = catalog_counts_vs_reference(conn, cand_srcset, params)
    assert result.outcome == "passed"
    assert result.detail["identity"] == {
        "exposure": "80001", "detector": "9", "catalog_type": "sextractor"}
    assert result.detail["reference"]["instance"] == ref_srcset
    assert result.detail["measurements"]["reference_row_count"] == 100

    # Withhold the reference's slot (as R12/duplicate-current would): it
    # is not a reference lookup match any more, reported as missing, the
    # same as a broken link is today.
    _clear_slot(conn, ref_srcset)
    result2 = catalog_counts_vs_reference(conn, cand_srcset, params)
    assert result2.outcome == "failed"
    assert "no reference" in result2.detail["reason"]


def test_catalog_counts_vs_reference_candidate_with_no_slot_fails(conn):
    from rapidpipe.checks.builtin import catalog_counts_vs_reference

    _run, cand = _register_result_set(
        conn, "source-set", {"difference": new_ulid(), "catalog_type": "sextractor"},
        row_count=10)
    # The producer difference-image doesn't exist, so the candidate's own
    # slot is NULL (R3): the check refuses to guess an identity for it.
    params = {"tolerance": 0.1, "missing_reference": "pass", "reference_run": None}
    result = catalog_counts_vs_reference(conn, cand, params)
    assert result.outcome == "failed"
    assert "identity" in result.detail["failing"]


# ======================================================================
# R20: a duplicate-current set is withheld together at any depth.
# ======================================================================

def _clear_slot_and_identity(conn, instance_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE product_instances SET slot = NULL, identity = NULL WHERE id = %s",
            (instance_id,))


def test_two_depth_duplicate_current_withholds_together(conn):
    """R20 (supervisor ledger, after Rehearsal 2): the fill derives to a
    fixpoint first and only then withholds every member of a
    duplicate-current set together, whatever its dependency depth. Two
    current association-sets share one field slot (the second's base is
    the first, the loop's ordinary next-date shape); each has a current
    pruned-set and a current statistics-set built on it. Against
    713421f2 (R18, before amendment 4) the two pruned-sets and the two
    statistics-sets resolve their slot one pass apart, because the
    second association-set's own identity (needed by its descendants'
    derivation) is itself one pass behind the first's -- so the
    depth-1 descendant that happens to resolve first keeps a slot
    nobody else contests YET, and only its sibling is later caught as
    the duplicate. R20 requires the full fixpoint be reached first, so
    all three (kind, slot) collisions -- association-set, pruned-set,
    statistics-set -- are counted duplicate_current in pairs, and all
    six rows end up with slot NULL."""
    _run, base_a = _register(
        conn, "association-set",
        {"field": 91000, "base": None, "source_sets": [], "settings_hash": "sha256:r20-a"})
    _run, base_b = _register(
        conn, "association-set",
        {"field": 91000, "base": base_a, "source_sets": [], "settings_hash": "sha256:r20-b"})
    _run, pruned_a = _register(
        conn, "pruned-set", {"base": base_a, "settings_hash": "sha256:r20-pa"})
    _run, pruned_b = _register(
        conn, "pruned-set", {"base": base_b, "settings_hash": "sha256:r20-pb"})
    _run, stats_a = _register(conn, "statistics-set", {"membership": base_a})
    _run, stats_b = _register(conn, "statistics-set", {"membership": base_b})

    six = [base_a, base_b, pruned_a, pruned_b, stats_a, stats_b]
    for instance_id in six:
        _clear_slot_and_identity(conn, instance_id)
        _set_custody(conn, instance_id, "current")

    with conn.cursor() as cur:
        report = repo.fill_identity(cur)
    by_kind = {kind: (converted, unresolved, dup) for kind, converted, unresolved, dup in report}
    assert by_kind.get("association-set", (0, 0, 0))[2] == 2
    assert by_kind.get("pruned-set", (0, 0, 0))[2] == 2
    assert by_kind.get("statistics-set", (0, 0, 0))[2] == 2

    with conn.cursor() as cur:
        cur.execute("SELECT id, slot FROM product_instances WHERE id = ANY(%s)", (six,))
        rows = dict(cur.fetchall())
    for instance_id in six:
        assert rows[instance_id] is None, f"{instance_id} kept a slot; not withheld with its peer"
