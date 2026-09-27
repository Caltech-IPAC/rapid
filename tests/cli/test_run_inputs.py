"""Behavioural, black-box tests of ``rapidpipe run inputs``: argv in,
exit code / stdout / stderr, FakeS3 state and database state out, against
a real PostgreSQL with Batch and S3 faked (see
``tests/cli/test_run_lifecycle.py``'s module docstring for the shared
setup and registration correction this module reuses).

Admit's own completed unit is driven through the real CLI
(``run submit`` + ``run reconcile``), like ``test_run_lifecycle.py``, but
with its own manifest seeding helper (:func:`_submit_and_complete_with_l2`)
rather than that module's ``_submit_and_complete``: this suite needs
admit's manifest to actually carry an ``l2-image`` output entry with real
bytes at a real (fake) S3 location, for ``compose_inputs`` to copy.
"""

from __future__ import annotations

import json

from rapidpipe.cli import runctl
from rapidpipe.db.ids import new_ulid
from rapidpipe.exitcodes import ExitCode
from tests.db.test_repository import _register_simple_instance

from .conftest import FAKE_BUCKET
from .test_loop import _bound
from .test_run_lifecycle import _create_run, _kv, _submit, _submit_and_complete


def _output_entry(kind: str, instance: str, files: dict[str, bytes], *, primary: str | None = None):
    members = [
        {"role": "image", "path": path, "bytes": len(data), "sha256": "sha256:" + "0" * 64}
        for path, data in files.items()]
    return {
        "kind": kind, "format_version": "1", "instance": instance,
        "key": {"k": instance}, "members": members,
        "primary": primary or members[0]["path"], "registration": {},
    }


def _manifest(*, run_id, stage, unit_id, attempt_id, outputs, result_sets=()):
    return {
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "detector-image", "id": unit_id},
        "stage": stage, "attempt": attempt_id,
        "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": list(result_sets)},
        "outputs": outputs,
    }


#: The template input-set's own entries: an old l2-image (superseded by
#: admit's, below), a reference-image, and a psf entry with two members.
_TEMPLATE_ENTRIES = (
    ("l2-image", "OLDL2", {"l2/old.fits": b"old"}),
    ("reference-image", "REF1", {"ref/image.fits": b"reference!"}),
    ("psf", "PSF1", {"psf/a.fits": b"psf-a", "psf/b.fits": b"psf-b"}),
)


def _seed_template(fake_s3, bucket: str, prefix: str, *, result_sets: tuple[str, ...] = (),
                   entries=_TEMPLATE_ENTRIES) -> str:
    outputs = []
    for kind, instance, files in entries:
        outputs.append(_output_entry(kind, instance, files))
        for path, data in files.items():
            fake_s3.seed(bucket, f"{prefix}/{path}", data)
    manifest = _manifest(
        run_id="REFRUN", stage="input-set", unit_id="U", attempt_id="REFATT", outputs=outputs,
        result_sets=result_sets)
    fake_s3.seed(bucket, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    return f"s3://{bucket}/{prefix}"


def _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, *, unit_id,
                                  l2_data=b"science-bytes"):
    """Submit and complete an ``admit`` attempt whose manifest carries one
    ``l2-image`` output entry with real bytes at the attempt's own (fake)
    S3 output location. Returns ``(attempt_id, output_location)``."""
    submitted = _submit(cli, run_id, "admit", unit_id)
    assert submitted.rc == 0, submitted.err
    attempt_id = _kv(submitted.out, "attempt")
    job_id = _kv(submitted.out, "job")
    output_location = _kv(submitted.out, "outputs")
    bucket = FAKE_BUCKET
    prefix = output_location[len(f"s3://{bucket}/"):]

    fake_s3.seed(bucket, f"{prefix}/deep/dir/science.fits", l2_data)
    manifest = _manifest(
        run_id=run_id, stage="admit", unit_id=unit_id, attempt_id=attempt_id,
        outputs=[_output_entry("l2-image", "L2NEW", {"deep/dir/science.fits": l2_data})])
    fake_s3.seed(bucket, f"{prefix}/manifest.json", json.dumps(manifest).encode())

    fake_batch.set_status(job_id, "SUCCEEDED")
    reconciled = cli("run", "reconcile", run_id)
    assert reconciled.rc == 0, reconciled.err
    return attempt_id, output_location


def _register_producer_instance(cli, db, fake_batch, fake_s3, *, kind, instance_id, purpose):
    """Register ``instance_id`` as a real, registered ``product_instances``
    row of kind ``kind``, produced by a fresh scratch run and attempt of
    its own -- the way tests/cli/test_loop.py's ``world`` fixture registers
    the difference template's entries under a producer run of its own --
    so :func:`rapidpipe.runs.inputs.bind_registered_inputs` treats
    ``instance_id`` as registered when a template or result-set list names
    it. No stage process runs (module docstring): registration is done the
    same way ``_register_candidate``/``_register_simple_instance`` do,
    directly, not through a submitted attempt's own manifest."""
    producer_run = _create_run(cli, db, kind="scratch", stages="difference", purpose=purpose)
    attempt_id, _job_id, _output = _submit_and_complete(
        cli, fake_batch, fake_s3, producer_run, unit_id="P", stage="difference")
    _register_simple_instance(
        db.connection, producer_run, "difference", attempt_id,
        instance_id=instance_id, kind=kind, logical_key={"unit": "P", "instance": instance_id})
    return producer_run


def _template_with_registered_entry(cli, db, fake_batch, fake_s3, *, purpose):
    """:data:`_TEMPLATE_ENTRIES`, but with its reference-image entry's
    instance id swapped for a fresh ULID that is actually registered as a
    ``product_instances`` row (``product_instances.id`` is a ``rapid_ulid``
    domain: the literal ``"REF1"`` the rest of this module uses is fine as
    a bare S3 manifest field, elsewhere never registered, but fails that
    domain's check constraint). Returns ``(entries, ref_instance_id)``."""
    ref_instance_id = new_ulid()
    entries = (
        ("l2-image", "OLDL2", {"l2/old.fits": b"old"}),
        ("reference-image", ref_instance_id, {"ref/image.fits": b"reference!"}),
        ("psf", "PSF1", {"psf/a.fits": b"psf-a", "psf/b.fits": b"psf-b"}),
    )
    _register_producer_instance(cli, db, fake_batch, fake_s3, kind="reference-image",
                                instance_id=ref_instance_id, purpose=purpose)
    return entries, ref_instance_id


# ======================================================================
# Composing: template entries plus the producer's l2-image, under l2/.
# ======================================================================

def test_inputs_composes_template_entries_and_the_producers_l2_output(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference", purpose="inputs-1")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-U1")
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    result = cli("run", "inputs", run_id, "difference", "--unit", "U",
                 "--from-stage", "admit", "--template", template_loc)
    assert result.rc == 0, result.err
    expected_dest = f"s3://{FAKE_BUCKET}/scratch/runs/{run_id}/inputs/difference/U"
    assert result.out.strip() == f"inputs={expected_dest}"

    dest_prefix = expected_dest[len(f"s3://{FAKE_BUCKET}/"):]
    assert (FAKE_BUCKET, f"{dest_prefix}/l2/science.fits") in fake_s3._objects
    assert fake_s3._objects[(FAKE_BUCKET, f"{dest_prefix}/l2/science.fits")] == b"science-bytes"
    assert (FAKE_BUCKET, f"{dest_prefix}/ref/image.fits") in fake_s3._objects
    assert (FAKE_BUCKET, f"{dest_prefix}/psf/a.fits") in fake_s3._objects
    assert (FAKE_BUCKET, f"{dest_prefix}/psf/b.fits") in fake_s3._objects
    # The template's own l2-image entry (and its object) is not copied:
    # the producer's l2-image entry replaces it entirely.
    assert (FAKE_BUCKET, f"{dest_prefix}/l2/old.fits") not in fake_s3._objects

    manifest = json.loads(fake_s3._objects[(FAKE_BUCKET, f"{dest_prefix}/manifest.json")])
    kinds = {o["kind"]: o for o in manifest["outputs"]}
    assert set(kinds) == {"l2-image", "reference-image", "psf"}
    assert kinds["l2-image"]["instance"] == "L2NEW"
    assert kinds["l2-image"]["members"] == [
        {"role": "image", "path": "l2/science.fits", "bytes": len(b"science-bytes"),
         "sha256": "sha256:" + "0" * 64}]


def test_inputs_second_call_refuses_to_overwrite(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference", purpose="inputs-2")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-U2")
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    first = cli("run", "inputs", run_id, "difference", "--unit", "U",
               "--from-stage", "admit", "--template", template_loc)
    assert first.rc == 0, first.err

    second = cli("run", "inputs", run_id, "difference", "--unit", "U",
                "--from-stage", "admit", "--template", template_loc)
    assert second.rc == 64
    assert "refusing to overwrite" in second.err


def test_inputs_dest_outside_the_scratch_inputs_root_is_refused(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference", purpose="inputs-3")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-U3")
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    result = cli("run", "inputs", run_id, "difference", "--unit", "U",
                 "--from-stage", "admit", "--template", template_loc,
                 "--dest", f"s3://{FAKE_BUCKET}/elsewhere/")
    assert result.rc == 64
    assert "is not under" in result.err


def test_inputs_default_dest_is_the_scratch_root_for_a_production_run(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="production", stages="admit,difference",
                         purpose="inputs-4")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-U4")
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    result = cli("run", "inputs", run_id, "difference", "--unit", "U",
                 "--from-stage", "admit", "--template", template_loc)
    assert result.rc == 0, result.err
    assert result.out.strip() == (
        f"inputs=s3://{FAKE_BUCKET}/scratch/runs/{run_id}/inputs/difference/U")


def test_inputs_on_a_finished_run_is_refused_by_the_admission_fence(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference", purpose="inputs-5")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-U5")
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    finished = cli("run", "finish", run_id)
    assert finished.rc == 0, finished.err

    result = cli("run", "inputs", run_id, "difference", "--unit", "U",
                 "--from-stage", "admit", "--template", template_loc)
    assert result.rc == 64


# ======================================================================
# run delete removes a scratch run's composed input set too.
# ======================================================================

def test_delete_removes_composed_input_set_objects_too(
        cli, db, fake_batch, fake_s3, fake_versioned_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference", purpose="inputs-6")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-U6")
    attempt_id, output_location = _submit_and_complete_with_l2(
        cli, fake_batch, fake_s3, run_id, unit_id="U")

    composed = cli("run", "inputs", run_id, "difference", "--unit", "U",
                   "--from-stage", "admit", "--template", template_loc)
    assert composed.rc == 0, composed.err
    dest_prefix = composed.out.strip()[len("inputs=") + len(f"s3://{FAKE_BUCKET}/"):]

    # A real S3 bucket serves both the compose seam (storage.s3_client,
    # FakeS3 here) and the cleanup seam (runs.cleanup._default_s3_client,
    # FakeVersionedS3 here) -- this suite fakes them as two independent
    # stores, so the composed objects (and the attempt's own manifest)
    # are mirrored into the versioned one by hand before delete runs.
    for bucket, key in fake_s3._objects:
        if bucket == FAKE_BUCKET and key.startswith(dest_prefix + "/"):
            fake_versioned_s3.seed(bucket, key, versions=1)
    output_prefix = output_location[len(f"s3://{FAKE_BUCKET}/"):]
    fake_versioned_s3.seed(FAKE_BUCKET, f"{output_prefix}/manifest.json", versions=1)
    fake_versioned_s3.seed(FAKE_BUCKET, f"{output_prefix}/deep/dir/science.fits", versions=1)

    before = fake_versioned_s3.remaining(FAKE_BUCKET, dest_prefix + "/")
    assert before, "nothing seeded under the composed inputs prefix"

    result = cli("run", "delete", run_id)
    assert result.rc == 0, result.err

    after = fake_versioned_s3.remaining(FAKE_BUCKET, dest_prefix + "/")
    assert after == []


# ======================================================================
# Binding (supervisor step 2): the primitive's guarantees through the CLI.
# ======================================================================

def test_inputs_binds_a_templates_registered_result_set_and_registered_output_entries(
        cli, db, fake_batch, fake_s3, batch_env):
    """A template's result set is bound: registered ids (an
    ``inputs.result_sets`` entry and a template output entry) end up in
    the consumer unit's ``unit_inputs``; an unregistered result-set id
    does not; the composed manifest's ``inputs.result_sets`` is carried
    over unchanged."""
    result_set_id = new_ulid()
    unregistered_id = new_ulid()
    entries, ref_instance_id = _template_with_registered_entry(
        cli, db, fake_batch, fake_s3, purpose="inputs-ref1-producer")
    _register_producer_instance(cli, db, fake_batch, fake_s3, kind="source-set",
                                instance_id=result_set_id, purpose="inputs-resultset-producer")

    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference",
                         purpose="inputs-resultsets")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-resultsets",
                                  result_sets=(result_set_id, unregistered_id), entries=entries)
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    result = cli("run", "inputs", run_id, "difference", "--unit", "U",
                 "--from-stage", "admit", "--template", template_loc)
    assert result.rc == 0, result.err

    bound = _bound(db, run_id, "difference", "U")
    assert bound == {result_set_id, ref_instance_id}

    expected_dest = f"s3://{FAKE_BUCKET}/scratch/runs/{run_id}/inputs/difference/U"
    dest_prefix = expected_dest[len(f"s3://{FAKE_BUCKET}/"):]
    manifest = json.loads(fake_s3._objects[(FAKE_BUCKET, f"{dest_prefix}/manifest.json")])
    assert manifest["inputs"]["result_sets"] == [result_set_id, unregistered_id]


def test_inputs_commits_bindings_before_the_manifest_write_and_recovers_after_a_write_failure(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    """The bindings are committed before the manifest is written (a
    manifest on storage means its bindings were committed): a write
    failure leaves the ``unit_inputs`` rows in place and no manifest at
    dest; a retry composes again and writes the manifest, binding nothing
    new (each (unit, instance) bound exactly once)."""
    entries, _ref_instance_id = _template_with_registered_entry(
        cli, db, fake_batch, fake_s3, purpose="inputs-commit-ref1-producer")
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference",
                         purpose="inputs-commit-before-publish")
    template_loc = _seed_template(fake_s3, FAKE_BUCKET, "templates/difference-commit",
                                  entries=entries)
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    real_write_manifest = runctl._Storage.write_manifest
    calls = {"n": 0}

    def flaky_write_manifest(self, manifest, location):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk went away")
        return real_write_manifest(self, manifest, location)

    monkeypatch.setattr(runctl._Storage, "write_manifest", flaky_write_manifest)

    first = cli("run", "inputs", run_id, "difference", "--unit", "U",
               "--from-stage", "admit", "--template", template_loc)
    # The OSError from write_manifest matches none of _with_connection's
    # named handlers (InputsRefused / RunModelError&co / batch-shaped), so
    # it re-raises past compose_inputs to main()'s unclassified-error
    # boundary (rapidpipe/cli/main.py's top-level "except Exception"),
    # which exits STAGE_ERROR (70): an observed, not a chosen, code.
    assert first.rc == int(ExitCode.STAGE_ERROR), (first.rc, first.err)

    expected_dest = f"s3://{FAKE_BUCKET}/scratch/runs/{run_id}/inputs/difference/U"
    dest_prefix = expected_dest[len(f"s3://{FAKE_BUCKET}/"):]
    assert (FAKE_BUCKET, f"{dest_prefix}/manifest.json") not in fake_s3._objects

    bound_after_failure = _bound(db, run_id, "difference", "U")
    assert bound_after_failure, "the bindings should have committed before the failed write"

    second = cli("run", "inputs", run_id, "difference", "--unit", "U",
                "--from-stage", "admit", "--template", template_loc)
    assert second.rc == 0, second.err
    assert (FAKE_BUCKET, f"{dest_prefix}/manifest.json") in fake_s3._objects

    with db.cursor() as cur:
        cur.execute(
            "SELECT ui.producer_instance, count(*) FROM unit_inputs ui "
            "JOIN units u ON u.id = ui.unit "
            "WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s "
            "GROUP BY ui.producer_instance", (run_id, "difference", "U"))
        rows = cur.fetchall()
    assert {row[0] for row in rows} == bound_after_failure
    assert all(count == 1 for _, count in rows)


def test_inputs_admission_before_copying_and_rollback_after_a_compose_failure(
        cli, db, fake_batch, fake_s3, batch_env):
    """R13: a copied member's size disagreeing with its manifest fails
    compose after admission (``add_unit``'s insert is uncommitted) but
    before any binding or manifest write, so ``_with_connection``'s
    rollback undoes the consumer unit too, leaving nothing behind. A run
    that admits no new work (finished) is refused by the same admission
    fence before compose ever runs, so nothing is copied into dest."""
    run_id = _create_run(cli, db, kind="scratch", stages="admit,difference",
                         purpose="inputs-r13-mismatch")
    bucket = FAKE_BUCKET
    prefix = "templates/difference-r13-mismatch"
    ref_data = b"reference!"
    bad_entry = _output_entry("reference-image", "REFBAD", {"ref/image.fits": ref_data})
    bad_entry["members"][0]["bytes"] = len(ref_data) + 5  # disagrees with the seeded object
    fake_s3.seed(bucket, f"{prefix}/ref/image.fits", ref_data)
    manifest = _manifest(
        run_id="REFRUN", stage="input-set", unit_id="U", attempt_id="REFATT",
        outputs=[bad_entry])
    fake_s3.seed(bucket, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    template_loc = f"s3://{bucket}/{prefix}"
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id, unit_id="U")

    result = cli("run", "inputs", run_id, "difference", "--unit", "U",
                 "--from-stage", "admit", "--template", template_loc)
    assert result.rc == int(ExitCode.FAILURE), (result.rc, result.err)
    assert "its manifest says" in result.err

    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, "difference", "U"))
        assert cur.fetchone() == (0,)
        cur.execute(
            "SELECT count(*) FROM unit_inputs ui JOIN units u ON u.id = ui.unit "
            "WHERE u.run = %s", (run_id,))
        assert cur.fetchone() == (0,)
    dest_prefix = f"scratch/runs/{run_id}/inputs/difference/U"
    assert (bucket, f"{dest_prefix}/manifest.json") not in fake_s3._objects

    # A finished run's admission fence refuses before compose ever runs.
    run_id2 = _create_run(cli, db, kind="scratch", stages="admit,difference",
                          purpose="inputs-r13-finished")
    template_loc2 = _seed_template(fake_s3, bucket, "templates/difference-r13-finished")
    _submit_and_complete_with_l2(cli, fake_batch, fake_s3, run_id2, unit_id="U")
    finished = cli("run", "finish", run_id2)
    assert finished.rc == 0, finished.err

    result2 = cli("run", "inputs", run_id2, "difference", "--unit", "U",
                  "--from-stage", "admit", "--template", template_loc2)
    assert result2.rc == int(ExitCode.USAGE)
    assert "admits no new work" in result2.err
    dest_prefix2 = f"scratch/runs/{run_id2}/inputs/difference/U"
    assert not any(key.startswith(dest_prefix2 + "/")
                  for (b, key) in fake_s3._objects if b == bucket)
