"""
File:    sequences.py

What each job type runs, in order.

The entrypoint dispatches on the manifest's job type; this is the table it
dispatches through. One sequence per job type, each an ordered list of
`(stage name, callable)` — the stage name is what appears in the attempt's
stage records and in the bundle's per-stage logs, so it is part of the
observable contract and not a comment.

**Why a table and not a function per job type.** The sequence is data the
entrypoint iterates, which means the set of stages an attempt was going to run
is knowable before any of them runs — the runtime logs it at startup, and a
reader of the attempt record can see which stage a failure stopped at without
reconstructing control flow. A hand-written driver per job type would put that
back inside the code.

**Registration is not here.** The registration job type dispatches to the
existing W3-converted registration path unchanged; it is not a stage sequence
and gets no entry. The entrypoint routes it directly.
"""

from submission.routes import (
    JOB_TYPE_ALERT_PRODUCTION,
    JOB_TYPE_CATALOG_LOAD,
    JOB_TYPE_CROSSMATCH,
    JOB_TYPE_MERGE_CURRENCY,
    JOB_TYPE_MERGE_DEDUP,
    JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_SCIENCE,
    JOB_TYPE_SOURCE_CURRENCY,
    JOB_TYPE_STATISTICS,
    RouteError,
)

from pipeline.stages import alert_production, post_db, reference_image, science

# The science (prompt differencing) sequence. Two catalogue variants always
# (ZOGY positive and negative); SFFT and the naive difference add theirs when
# the release enables them, and record a skipped stage when it does not.
SCIENCE_SEQUENCE = (
    ("download_inputs", science.download_inputs),
    ("gunzip_science_image", science.gunzip_science_image),
    ("resolve_reference_image", science.resolve_reference_image),
    ("measure_reference_fwhm", science.measure_reference_fwhm),
    # `science_image_statistics` must stay ahead of `inject_fake_sources`: the
    # clipped average it produces feeds the uncertainty model in
    # `reformat_science_image`, and the monolith computed it over the
    # *pre-injection* image (lines 788-798, injection opening at 806). Moving
    # it after injection changes the noise term for injection-enabled runs.
    ("science_image_statistics", science.science_image_statistics),
    ("inject_fake_sources", science.inject_fake_sources),
    ("reformat_science_image", science.reformat_science_image),
    ("science_image_catalog", science.science_image_catalog),
    ("resample_reference_image", science.resample_reference_image),
    ("normalize_science_psf", science.normalize_science_psf),
    ("subtract_background", science.subtract_background),
    ("gain_match", science.gain_match),
    ("prepare_zogy_inputs", science.prepare_zogy_inputs),
    ("run_zogy", science.run_zogy),
    ("postprocess_zogy", science.postprocess_zogy),
    ("catalog_zogy", science.catalog_zogy),
    ("run_sfft", science.run_sfft),
    ("catalog_sfft", science.catalog_sfft),
    ("naive_difference", science.naive_difference),
    ("upload_products", science.upload_products),
)

REFERENCE_IMAGE_SEQUENCE = (
    ("download_reference_psf", reference_image.download_reference_psf),
    ("build_reference_image", reference_image.build_reference_image),
    ("coverage_and_uncertainty_statistics",
     reference_image.coverage_and_uncertainty_statistics),
    ("sextractor_catalog", reference_image.sextractor_catalog),
    ("psf_catalog", reference_image.psf_catalog),
    ("image_statistics", reference_image.image_statistics),
    ("measure_fwhm", reference_image.measure_fwhm),
    ("add_header_keywords", reference_image.add_header_keywords),
    ("upload_products", reference_image.upload_products),
)

SEQUENCES = {
    JOB_TYPE_SCIENCE: SCIENCE_SEQUENCE,
    JOB_TYPE_REFERENCE_IMAGE: REFERENCE_IMAGE_SEQUENCE,
    # The post-DB science chain (step-3 conversion). These six produce
    # database state rather than S3 products: each declares an empty product
    # set, its terminal record is a pure disposition record, and its effect
    # rides in the attempt record's own fields. They are sequences like any
    # other because the entrypoint's dispatch, stage spans and termination
    # protocol are what give them the account of themselves the four
    # orchestrator subprocesses never had.
    JOB_TYPE_CATALOG_LOAD: post_db.CATALOG_LOAD_SEQUENCE,
    JOB_TYPE_CROSSMATCH: post_db.CROSSMATCH_SEQUENCE,
    JOB_TYPE_STATISTICS: post_db.STATISTICS_SEQUENCE,
    JOB_TYPE_MERGE_CURRENCY: post_db.MERGE_CURRENCY_SEQUENCE,
    JOB_TYPE_SOURCE_CURRENCY: post_db.SOURCE_CURRENCY_SEQUENCE,
    JOB_TYPE_MERGE_DEDUP: post_db.MERGE_DEDUP_SEQUENCE,
    # The alert-production trigger (step-4 conversion): the prompt-queue job
    # type that wires the complete-but-unwired alerts path to the real
    # producer.
    JOB_TYPE_ALERT_PRODUCTION: alert_production.ALERT_PRODUCTION_SEQUENCE,
}


#: The job types whose stages settle a database effect through the
#: claim/confirm protocol (`RAPIDDB.claim_alert_emission` /
#: `confirm_alert_emission`) and report it as the `effect_outcome` stage
#: fact — ruling R1, effect-lifecycle completion boundary. Distinct from
#: `submission.subjects.is_product_producing`'s split: that boundary is
#: product-producing (science, reference-image) vs database-effect
#: (everything else), and every one of the six post-DB job types in
#: `SEQUENCES` above is database-effect WITHOUT being claim/confirm-based —
#: they report `rows_written`/`rows_removed` through `record_effect` and
#: close a plain `success`/`none` pair, which `observability.registration
#: .decide` already SKIPs correctly. This narrower set is what
#: `pipeline.entrypoints.job._execute`'s fail-closed guard checks: a
#: successful attempt whose job type is IN this set but produced no
#: `effect_outcome` fact is a classified failure (a stage that returned
#: without recording its effect outcome, silently), never a silent
#: `success`+`none`.
#:
#: A SET, not a per-stage remember-to-check: the guard reads this once at
#: the derivation site rather than trusting each of an arbitrary number of
#: future effect stages to enforce it on itself — the whole point of a
#: CENTRAL fail-closed guard.
EFFECT_CLASS_JOB_TYPES = frozenset({JOB_TYPE_ALERT_PRODUCTION})


def sequence_for(job_type: str) -> tuple:
    """The stage sequence for one job type.

    Raises
    ------
    RouteError
        If no sequence exists. The entrypoint has already validated the job
        type against the route matrix by the time it calls this, so reaching
        here means a job type is routable but has no payload — which is a
        code/vocabulary mismatch, not a submission error, and must not be
        defaulted through.
    """
    if job_type not in SEQUENCES:
        raise RouteError(
            f"job type {job_type!r} has no stage sequence; sequences exist "
            f"for: {', '.join(sorted(SEQUENCES))}. Registration dispatches to "
            f"the registration path rather than to a stage sequence.")
    return SEQUENCES[job_type]
