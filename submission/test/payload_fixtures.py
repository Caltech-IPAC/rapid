"""Valid unit payloads for tests, one helper per job type.

**WHY THIS EXISTS.** D4 made the imaging payloads validate their required
facts at construction, which is the point of rule 11 — but it also means a
science or reference-image unit can no longer be built from `exposure` and
`sca` alone, and roughly a hundred test fixtures did exactly that. Repeating
the eleven required facts at every one of those sites would be a hundred
places for the required set to drift from what `ImagingPayload` actually
declares.

So the fixtures live here, beside the tests, and they are DELIBERATELY not
in `submission/payloads.py`: production code must never have a
build-me-a-plausible-unit helper, because the whole value of the validation
is that production cannot construct a unit whose facts nobody resolved.

Every value is obviously synthetic. A fixture that looked like real survey
data would invite someone to read meaning into a number this module made up.
"""

from submission import payloads
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE)

#: The facts `submission.gathering.science_facts` always resolves, and which
#: both imaging payloads therefore require. Kept as one mapping because the
#: two job types share the resolver — the same reason `ImagingPayload` is one
#: class.
IMAGING_FACTS = {
    "rid": 101,
    "fid": 2,
    "field": 4242,
    "rtid": 4242,
    "expid": 90001,
    "mjdobs": 60000.5,
    "exptime": 100.0,
    "infobits": 0,
    "science_image_uri": "s3://test-bucket/science.fits",
    "sky_position": {"ra0": 10.0, "dec0": 20.0},
    "tile_position": {"ra0": 10.0, "dec0": 20.0},
}

#: What a reference-image build needs on top of the imaging facts.
COADD_FACTS = {
    "coadd_inputs_uri": "s3://test-bucket/coadd-inputs.csv",
    "coadd_inputs_checksum": "f" * 64,
    "coadd_input_identities": ((90001, 3, 0), (90002, 4, 0)),
}


def science_payload(exposure=90001, sca=3, **overrides):
    """A valid `SciencePayload`. Overrides win over the defaults."""
    facts = dict(IMAGING_FACTS, **overrides)
    return payloads.build(JOB_TYPE_SCIENCE, exposure=exposure, sca=sca,
                          **facts)


def reference_payload(exposure=90002, sca=4, **overrides):
    """A valid `ReferenceImagePayload`, coadd inputs included."""
    facts = dict(IMAGING_FACTS, **COADD_FACTS)
    facts.update(overrides)
    return payloads.build(JOB_TYPE_REFERENCE_IMAGE, exposure=exposure,
                          sca=sca, **facts)


def alert_payload(exposure=90003, sca=5, **overrides):
    """A valid `AlertProductionPayload`."""
    facts = {"promoted_attempt_id": 77, "release_identity": "test-release",
             "difference_image_pid": 1234}
    facts.update(overrides)
    return payloads.build(JOB_TYPE_ALERT_PRODUCTION, exposure=exposure,
                          sca=sca, **facts)


def catalog_load_payload(proc_date="20260812", sca=6, **overrides):
    facts = {"target_table": f"sources_{proc_date}_{sca}"}
    facts.update(overrides)
    return payloads.build(JOB_TYPE_CATALOG_LOAD, proc_date=proc_date, sca=sca,
                          **facts)


def crossmatch_payload(proc_date="20260812", field=4242, **overrides):
    facts = {"target_tables": (f"astroobjects_{field}", f"merges_{field}")}
    facts.update(overrides)
    return payloads.build(JOB_TYPE_CROSSMATCH, proc_date=proc_date,
                          field=field, **facts)


def field_payload(job_type, field=4242, **overrides):
    """A valid field-grained payload for any of the four sweep job types."""
    facts = {"target_table": f"astroobjects_{field}"}
    facts.update(overrides)
    return payloads.build(job_type, field=field, **facts)


def science_unit(exposure=90001, sca=3, **overrides):
    """A whole `ProcessingUnit` around a science payload."""
    from submission.manifest import ProcessingUnit

    return ProcessingUnit(payload=science_payload(exposure, sca, **overrides))


def reference_unit(exposure=90002, sca=4, **overrides):
    from submission.manifest import ProcessingUnit

    return ProcessingUnit(
        payload=reference_payload(exposure, sca, **overrides))
