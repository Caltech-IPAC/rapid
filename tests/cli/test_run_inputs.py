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

from .conftest import FAKE_BUCKET
from .test_run_lifecycle import _create_run, _kv, _submit


def _output_entry(kind: str, instance: str, files: dict[str, bytes], *, primary: str | None = None):
    members = [
        {"role": "image", "path": path, "bytes": len(data), "sha256": "sha256:" + "0" * 64}
        for path, data in files.items()]
    return {
        "kind": kind, "format_version": "1", "instance": instance,
        "key": {"k": instance}, "members": members,
        "primary": primary or members[0]["path"], "registration": {},
    }


def _manifest(*, run_id, stage, unit_id, attempt_id, outputs):
    return {
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "detector-image", "id": unit_id},
        "stage": stage, "attempt": attempt_id,
        "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": []},
        "outputs": outputs,
    }


#: The template input-set's own entries: an old l2-image (superseded by
#: admit's, below), a reference-image, and a psf entry with two members.
_TEMPLATE_ENTRIES = (
    ("l2-image", "OLDL2", {"l2/old.fits": b"old"}),
    ("reference-image", "REF1", {"ref/image.fits": b"reference!"}),
    ("psf", "PSF1", {"psf/a.fits": b"psf-a", "psf/b.fits": b"psf-b"}),
)


def _seed_template(fake_s3, bucket: str, prefix: str) -> str:
    outputs = []
    for kind, instance, files in _TEMPLATE_ENTRIES:
        outputs.append(_output_entry(kind, instance, files))
        for path, data in files.items():
            fake_s3.seed(bucket, f"{prefix}/{path}", data)
    manifest = _manifest(
        run_id="REFRUN", stage="input-set", unit_id="U", attempt_id="REFATT", outputs=outputs)
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
