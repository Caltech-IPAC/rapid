"""Fix round 1 — N1: active manifests are READ, and their contents protected.

The shipped package listed manifest-body expansion in its docstrings and never
did it: `collect_references` returned `submissions.manifest_uri` and stopped,
so every input a manifest named — `science_image_uri`, `reference_image_uri`,
`psf_uri`, `coadd_inputs_uri` — looked unreferenced. The gap was masked by a
test that HAND-INJECTED the reference it then asserted on, which is exactly the
shape of test that proves nothing about the real path.

These tests exercise the real expansion, with a reader that CAN REFUSE.
"""

import json

import pytest

from pipeline.gc import reference_sql
from pipeline.gc.reference_sql import (ManifestUnreadable,
                                       expand_manifest_bodies)

MANIFEST_URI = "s3://roman-rapid-products/submissions/batch-1/manifest.json"

#: A manifest body in the real shape: units nested under the manifest, each
#: carrying the typed payload's URI components.
BODY = {
    "batch_id": "run-1",
    "units": [
        {"payload": {"grain": "exposure_sca", "exposure": 90000, "sca": 1,
                     "science_image_uri": "s3://b/science/90000_1.fits",
                     "psf_uri": "s3://b/psf/90000_1.fits",
                     "reference_image_uri": "s3://b/ref/field-7.fits"}},
        {"payload": {"grain": "field_filter",
                     "coadd_inputs_uri": "s3://b/submissions/run-1/"
                                         "coadd-inputs/u/inputs.csv"}},
    ],
}


def test_every_uri_field_in_a_manifest_body_becomes_a_reference():
    """N1: the four enumerated URI fields, expanded from a real body.

    Enumerated from the payload dataclasses rather than a partial list:
    `science_image_uri` (`payloads.py:308`), `psf_uri` (`:332`),
    `reference_image_uri` (`:341`), `coadd_inputs_uri` (`:433`).
    """
    found = expand_manifest_bodies({MANIFEST_URI}, lambda uri: BODY)
    assert found == {
        "s3://b/science/90000_1.fits",
        "s3://b/psf/90000_1.fits",
        "s3://b/ref/field-7.fits",
        "s3://b/submissions/run-1/coadd-inputs/u/inputs.csv",
    }


def test_the_coadd_input_csv_is_protected_by_its_manifest():
    """A coadd-input CSV carries no row in ANY product table.

    `submission/gathering.py:983` writes it; nothing records it. It reaches
    the reference set only through the manifest that cites it, which is why
    expansion is not optional.
    """
    found = expand_manifest_bodies({MANIFEST_URI}, lambda uri: BODY)
    assert "s3://b/submissions/run-1/coadd-inputs/u/inputs.csv" in found


def test_the_cross_attempt_reference_image_is_protected():
    """`reference_image_uri` is cited by MANY later science manifests.

    A reference image published by one attempt is an input to work that has
    nothing to do with that attempt, so attempt-scoped reasoning alone would
    delete live inputs. Expansion is what stops it.
    """
    found = expand_manifest_bodies({MANIFEST_URI}, lambda uri: BODY)
    assert "s3://b/ref/field-7.fits" in found


def test_an_unknown_uri_field_is_still_treated_as_a_reference():
    """Coded to the SHAPE, not to a list.

    A payload gaining a new URI component must not silently drop out of the
    reference set between the day it ships and the day someone remembers to
    update a constant here.
    """
    body = {"units": [{"payload": {"cutout_uri": "s3://b/new/thing.fits"}}]}
    found = expand_manifest_bodies({MANIFEST_URI}, lambda uri: body)
    assert found == {"s3://b/new/thing.fits"}


def test_an_unreadable_manifest_refuses_the_whole_plan():
    """THE PLAN, NOT THE OBJECT — and nothing is deleted in that run.

    "Its referenced objects" cannot be identified without reading it, so the
    fallback cannot be per-object. Guessing which objects an unreadable
    manifest covers is exactly the guess this design refuses to make.
    """
    def refusing_reader(uri):
        raise IOError("access denied")

    with pytest.raises(ManifestUnreadable) as caught:
        expand_manifest_bodies({MANIFEST_URI}, refusing_reader)
    assert "REFUSED" in str(caught.value)
    assert MANIFEST_URI in str(caught.value)


def test_a_manifest_expanding_to_nothing_refuses_the_plan():
    """A reader returning None is unreadable, not "protects no objects"."""
    with pytest.raises(ManifestUnreadable):
        expand_manifest_bodies({MANIFEST_URI}, lambda uri: None)


@pytest.mark.contract
def test_collect_references_refuses_when_manifests_exist_but_no_reader_does():
    """A missing reader gets the same answer as an unreadable manifest.

    Not silently skipped: computing a plan without expanding active manifests
    would leave every input they name looking unreferenced, which is the
    condition under which this GC deletes live data.

    **THE FIXTURE MAKES ITS OWN ACTIVE MANIFEST** rather than skipping when
    the database happens to have none. A first revision skipped, and the
    recorded acceptance run's zero-skip gate caught it — a criterion that
    skips proves nothing, which is exactly why that gate exists.
    """
    from pipeline.contract import fixture
    conn = fixture.connect()
    try:
        if not fixture.has_table(conn, "submissions"):
            pytest.skip("DRAFT 044 is not applied (submissions absent)")
        execute = fixture.executor(conn)
        run_id = "gc-manifest-%s" % fixture.RUN_TAG

        # An active manifest: a `submissions` row with a manifest_uri and NO
        # resolvable children, which the ACTIVE_MANIFESTS_SQL predicate treats
        # as active precisely because unattributable children are retained.
        with conn.cursor() as cur:
            # Every NOT NULL column, read from 044 rather than guessed: the
            # table requires job_type, job_name, job_queue, job_definition,
            # manifest_checksum, manifest_uri, array_size and state.
            cur.execute(
                "INSERT INTO submissions"
                " (run_id, job_type, job_name, job_queue, job_definition,"
                "  manifest_checksum, manifest_uri, array_size, state)"
                " VALUES (%s, 'science', %s, 'q', 'jd', %s, %s, 1,"
                "         'prepared')",
                (run_id, "job-" + run_id, "sha256:" + ("a" * 64),
                 MANIFEST_URI))
        conn.commit()
        try:
            rows = execute(reference_sql.ACTIVE_MANIFESTS_SQL, [])
            assert rows, "the fixture manifest is not active"

            with pytest.raises(ManifestUnreadable):
                reference_sql.collect_references(execute,
                                                 manifest_reader=None)
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM submissions WHERE run_id = %s",
                            (run_id,))
            conn.commit()
    finally:
        conn.close()


@pytest.mark.contract
def test_collect_references_expands_bodies_when_a_reader_is_supplied():
    """The real SQL surface plus the real expansion, together.

    This is the assertion the hand-injected fixture could not make: the
    reference set that comes back contains what the MANIFESTS name, not only
    what the tables record.
    """
    from pipeline.contract import fixture
    conn = fixture.connect()
    try:
        if not fixture.has_table(conn, "submissions"):
            pytest.skip("DRAFT 044 is not applied")
        execute = fixture.executor(conn)
        rows = execute(reference_sql.ACTIVE_MANIFESTS_SQL, [])

        seen = []

        def reader(uri):
            seen.append(uri)
            return BODY

        references, consulted, _absent = reference_sql.collect_references(
            execute, manifest_reader=reader)

        assert "submissions" in consulted
        if rows:
            assert seen, "active manifests were not read"
            assert "submissions:bodies" in consulted
            assert "s3://b/ref/field-7.fits" in references
    finally:
        conn.close()
