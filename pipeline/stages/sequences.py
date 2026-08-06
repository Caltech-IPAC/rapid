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
    JOB_TYPE_POST_PROCESS,
    JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_SCIENCE,
    RouteError,
)

from pipeline.stages import post_process, reference_image, science

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

POST_PROCESS_SEQUENCE = (
    ("stamp_reference_image", post_process.stamp_reference_image),
    ("stamp_difference_image", post_process.stamp_difference_image),
    ("upload_products", post_process.upload_products),
)

SEQUENCES = {
    JOB_TYPE_SCIENCE: SCIENCE_SEQUENCE,
    JOB_TYPE_REFERENCE_IMAGE: REFERENCE_IMAGE_SEQUENCE,
    JOB_TYPE_POST_PROCESS: POST_PROCESS_SEQUENCE,
}


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
