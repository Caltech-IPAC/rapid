"""Database-backed tests for ``rapidpipe.runs.binding.bind_input_set``:
the one primitive both composers use to
admit, bind and write an input set. Local (non-S3) storage is enough --
``rapidpipe.cli.runctl._Storage``'s local-path branch needs no S3 client
-- so this suite calls the primitive directly against a real PostgreSQL,
one level below the CLI-level black-box suite in
``tests/cli/test_run_inputs.py``.

Every test wraps ``conn`` in ``tests/db/test_bind_inputs.py``'s
``_NoCommit`` around the call to ``bind_input_set`` itself (not around
its setup, which never commits on its own -- the repository functions
open no transactions of their own), so ``bind_input_set``'s own
``conn.commit()`` never actually commits to the live database: everything
stays inside the ``conn`` fixture's one never-committed, rolled-back-at-
teardown transaction (``tests/db/conftest.py``).
"""

from __future__ import annotations

import pytest

from rapidpipe.cli.runctl import _Storage
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Inputs, Manifest, Member, OutputEntry, Unit
from rapidpipe.runs import repository as repo
from rapidpipe.runs.binding import bind_input_set
from rapidpipe.runs.inputs import InputsRefused
from tests.db.test_bind_inputs import _NoCommit
from tests.db.test_repository import _full_chain_to_current_candidate, _make_run

pytestmark = pytest.mark.real_input_manifest


def _bind(conn, storage, **kwargs):
    """``bind_input_set`` through the commit-absorbing wrapper (see the
    module docstring), so the test's whole transaction still rolls back
    at teardown despite ``bind_input_set``'s own ``conn.commit()``."""
    return bind_input_set(_NoCommit(conn), storage, **kwargs)


def _manifest_naming(run_id: str, unit_id: str, instance_ids: list[str]) -> Manifest:
    """A minimal, valid :class:`Manifest` whose only inputs are
    ``instance_ids`` (one output entry each) -- enough for
    ``bind_input_set``'s ``compose`` seam, since only
    ``rapidpipe.runs.inputs.manifest_instances`` reads it."""
    return Manifest(
        run=run_id, unit=Unit(kind="detector-image", id=unit_id), stage="input-set",
        attempt=new_ulid(), execution_record="exec/input-set.json",
        inputs=Inputs(manifest="x"),
        outputs=tuple(
            OutputEntry(
                kind="test-product", format_version="1", instance=instance_id,
                key={"k": instance_id}, primary=f"{instance_id}.fits",
                members=(Member(role="image", path=f"{instance_id}.fits", bytes=1,
                                sha256="sha256:" + "0" * 64),))
            for instance_id in instance_ids),
    )


def _reused_manifest(conn, tmp_path):
    """A producer's registered instance, composed, bound and written once
    at ``dest`` for a first consumer unit ("U"). Returns ``(consumer,
    dest, instance, manifest_path, written_text, written_mtime)``."""
    producer = _make_run(conn, kind="scratch")
    _, _, _, instance = _full_chain_to_current_candidate(
        conn, producer, logical_key={"unit": "e001/SCA01", "v": f"bind-{new_ulid()}"})
    consumer = _make_run(conn, kind="scratch")
    dest = str(tmp_path / "inputs" / "difference" / "U")

    def compose():
        return _manifest_naming(consumer, "U", [instance])

    result = _bind(
        conn, _Storage(), run_id=consumer, stage="difference", unit_kind="detector-image",
        unit_id="U", dest=dest, compose=compose, reuse_existing=True)
    assert result.reused is False
    assert result.bound == (instance,)

    manifest_path = tmp_path / "inputs" / "difference" / "U" / "manifest.json"
    assert manifest_path.is_file()
    return consumer, dest, instance, manifest_path, manifest_path.read_text(), manifest_path.stat().st_mtime


def _bound(conn, run_id: str, stage: str, unit_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ui.producer_instance FROM unit_inputs ui JOIN units u ON u.id = ui.unit "
            "WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s ORDER BY 1",
            (run_id, stage, unit_id))
        return [row[0] for row in cur.fetchall()]


def test_bind_input_set_reuse_rebinds_the_existing_manifests_ids_to_the_consumer(conn, tmp_path):
    consumer, dest, instance, manifest_path, written, mtime = _reused_manifest(conn, tmp_path)

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM unit_inputs WHERE unit IN (SELECT id FROM units WHERE run = %s "
            "AND stage = %s AND unit_id = %s)", (consumer, "difference", "U"))
    assert _bound(conn, consumer, "difference", "U") == []

    def compose_must_not_run():
        raise AssertionError("must not compose")

    result = _bind(
        conn, _Storage(), run_id=consumer, stage="difference", unit_kind="detector-image",
        unit_id="U", dest=dest, compose=compose_must_not_run, reuse_existing=True)
    assert result.reused is True
    assert _bound(conn, consumer, "difference", "U") == [instance]
    assert manifest_path.read_text() == written
    assert manifest_path.stat().st_mtime == mtime

    # A third call: still exactly one row, the manifest still untouched.
    result3 = _bind(
        conn, _Storage(), run_id=consumer, stage="difference", unit_kind="detector-image",
        unit_id="U", dest=dest, compose=compose_must_not_run, reuse_existing=True)
    assert result3.reused is True
    assert _bound(conn, consumer, "difference", "U") == [instance]
    assert manifest_path.read_text() == written
    assert manifest_path.stat().st_mtime == mtime


def test_bind_input_set_reuse_binds_a_second_consumer_unit_to_the_same_manifest(conn, tmp_path):
    consumer, dest, instance, manifest_path, written, mtime = _reused_manifest(conn, tmp_path)

    def compose_must_not_run():
        raise AssertionError("must not compose")

    result = _bind(
        conn, _Storage(), run_id=consumer, stage="difference", unit_kind="detector-image",
        unit_id="U2", dest=dest, compose=compose_must_not_run, reuse_existing=True)
    assert result.reused is True
    assert _bound(conn, consumer, "difference", "U2") == [instance]
    # The first unit's own binding is untouched, and the manifest was
    # never rewritten for the second consumer either.
    assert _bound(conn, consumer, "difference", "U") == [instance]
    assert manifest_path.read_text() == written
    assert manifest_path.stat().st_mtime == mtime


def test_bind_input_set_refuses_a_deleting_producer_and_writes_nothing(conn, tmp_path):
    producer = _make_run(conn, kind="scratch")
    _, _, _, instance = _full_chain_to_current_candidate(
        conn, producer, logical_key={"unit": "e001/SCA01", "v": "bind-primitive-deleting"})
    repo.mark_run_deleting(conn, producer, requested_by="brusholme")

    consumer = _make_run(conn, kind="scratch")
    dest = str(tmp_path / "inputs" / "difference" / "U")

    def compose():
        return _manifest_naming(consumer, "U", [instance])

    with pytest.raises(InputsRefused):
        _bind(
            conn, _Storage(), run_id=consumer, stage="difference", unit_kind="detector-image",
            unit_id="U", dest=dest, compose=compose, reuse_existing=True)

    manifest_path = tmp_path / "inputs" / "difference" / "U" / "manifest.json"
    assert not manifest_path.exists()
    assert _bound(conn, consumer, "difference", "U") == []
