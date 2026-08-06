"""
File:    post_process.py

The post-process pipeline's stages.

Extracted from `awsBatchSubmitJobs_runSinglePostProcPipeline.py` (394 lines).
Its job is to stamp database identities into the FITS headers of products the
science pipeline already made, and to re-upload them — so it runs after
registration has assigned those identities.

Two operational defects went with the skeleton:

**The bucket scan is gone.** The monolith listed the whole product prefix and
compared every key against two expected filenames (lines 206-332), doing a
`Bucket.objects.filter` walk to answer a question the manifest answers
directly. It also printed the difference-image filename on every iteration of
that loop. The manifest names the two objects; they are fetched by name.

**A missing product no longer reports success.** When neither `if` matched, the
monolith left `rfid` and `pid` as the string `"None"` in its output `.ini`,
uploaded that, and exited 0 — the product it was asked to stamp simply did not
get stamped, and nothing recorded it. Here an absent input raises `InputError`
and the attempt is classified `input_missing`.

The `db` import the monolith carried was only ever `compute_checksum`, an
MD5-of-file helper with no database in it. It is called directly.
"""

import os

import modules.utils.rapid_pipeline_subs as util
from database.modules.utils.rapid_db import compute_checksum
from pipeline.runtime.errors import InputError
from pipeline.stages.publishing import publish_products
from submission.routes import JOB_TYPE_SCIENCE, ppid_for

# S3OBJPRF — THE HEADER THAT POINTED AT THE WRONG PREFIX (round-3 finding #7).
#
# Both stamped headers carried `f"{ctx.job_type}/{ctx.unit.key}"`, the OLD
# product-key shape, while the upload has been run- and attempt-scoped since
# review finding #18 closed the overwrite hole: `product_prefix()` is
# `job_type/run_id/unit/attempt-N`. The two disagreed, so every stamped product
# announced in its own header an S3 prefix that nothing had ever been written
# to — and the header is exactly what someone reads to find the object again.
# Both now call `context.product_prefix()`, the one place product keys are
# built, so the header names the prefix the bytes actually land under and stays
# correct if that shape changes again.

# `compute_checksum` signals failure by returning one of these instead of a
# digest — the monolith checked for them at line 1668 of the science script and
# not at all here. Named rather than repeated as bare integers.
CHECKSUM_ERROR_CODES = (65, 66, 68)

# The PPID stamped into both products' headers. The monolith read a single
# `ppid` from `[SCI_IMAGE]` of the job's .ini
# (`5664024^:pipeline/...PostProcPipeline.py:158`, value 15 in
# `...launchSingleSciencePipeline.ini:95`) and stamped that one value into the
# reference image's header at line 238 and the difference image's at line 297.
# It is the identifier of the pipeline that *produced the difference image* —
# the science pipeline — which is why the same value goes into both.
#
# The first extraction read a `reference_image_ppid` manifest fact instead, with
# different per-header defaults (12 and 15): the difference image was labelled
# as produced by pipeline 12, the dedicated reference-image pipeline, whenever
# the fact was present. `ppid_for` is the route matrix's own map and needs no
# manifest fact — the value is a property of the pipeline, not of the unit.
SCIENCE_PPID = ppid_for(JOB_TYPE_SCIENCE)


def stamp_reference_image(context) -> None:
    """Stamp identities into the reference image. (Stage S6a, lines 215-269.)"""
    _stamp(context,
           uri_fact="reference_image_uri",
           product="reference_image",
           keywords=["RFID", "S3BUCKN", "S3OBJPRF", "RFFILEN", "INFOBITS",
                     "RFIMVER", "PPID", "DATE"],
           values=lambda ctx, filename: [
               ctx.fact("reference_image_id"),
               ctx.parameter("s3/products-bucket"),
               ctx.product_prefix(),
               os.path.basename(filename),
               ctx.optional_fact("reference_image_infobits", 0),
               ctx.optional_fact("reference_image_version", 1),
               SCIENCE_PPID,
               ctx.started_at.isoformat(),
           ],
           checksum_fact="reference_image_checksum")


def stamp_difference_image(context) -> None:
    """Stamp identities into the difference image. (Stage S6b, lines 272-332.)"""
    _stamp(context,
           uri_fact="difference_image_uri",
           product="difference_image",
           keywords=["PID", "S3BUCKN", "S3OBJPRF", "DIFFILEN", "INFOBITS",
                     "DIFIMVER", "PPID", "RID", "EXPID", "FID", "FIELD",
                     "DATE"],
           values=lambda ctx, filename: [
               ctx.fact("pid"),
               ctx.parameter("s3/products-bucket"),
               ctx.product_prefix(),
               os.path.basename(filename),
               ctx.optional_fact("infobits", 0),
               ctx.optional_fact("difference_image_version", 1),
               SCIENCE_PPID,
               ctx.fact("rid"),
               ctx.fact("expid"),
               ctx.fact("fid"),
               ctx.fact("field"),
               ctx.started_at.isoformat(),
           ],
           checksum_fact="difference_image_checksum")


def _stamp(context, uri_fact, product, keywords, values, checksum_fact) -> None:
    """Download one product, stamp its header, checksum it.

    The shared body of the monolith's two `if` blocks, which differed only in
    the keyword list and the values behind it.
    """
    uri = context.fact(uri_fact)
    filename, _subdirs, downloaded = util.download_file_from_s3_bucket(
        context.s3, uri, outputfile=context.scratch(os.path.basename(uri)))
    if not downloaded:
        raise InputError(
            f"the product to post-process is not in the bucket: {uri}. The "
            f"post-process job stamps identities into products the science "
            f"pipeline already made; there is nothing here to stamp.",
            uri=uri)

    util.addKeywordsToFITSHeader(
        filename, keywords, values(context, filename), 0, filename)

    checksum = compute_checksum(filename)
    if checksum in CHECKSUM_ERROR_CODES:
        raise InputError(
            f"could not checksum {filename} after stamping its header "
            f"(compute_checksum returned {checksum})", path=filename)

    context.produce(product, filename)
    context.record(**{checksum_fact: checksum})


def upload_products(context) -> None:
    """Re-upload the stamped products. (Stages S6a/S6b uploads, S9.)

    THE SWALLOWED UPLOAD, AND THE EMPTY PUBLICATION (round-3 finding #7). This
    stage called `util.upload_files_to_s3_bucket` and discarded its return —
    and that legacy helper does not raise: it returns a boolean nobody read,
    prints on failure, and `continue`s straight over a file it could not find.
    So an upload that wrote nothing at all left this stage returning normally.

    It was the second half that made the failure invisible rather than merely
    unreported. Nothing here ever called `publish_products` or
    `context.publish`, so `context.published_products` stayed `{}`;
    `build_terminal_record`'s `if products:` guard was false and the record
    carried NO products key whatever; and the entrypoint still closed the
    attempt `(success, published)`. A post-process attempt could therefore
    upload nothing, publish nothing, record nothing, and close as a success
    that published products — the exact combination the disposition vocabulary
    exists to keep unrepresentable.

    `publish_products` is now the caller, the same helper the science and
    reference paths use, so all three answer "what is a published product"
    identically: run- and attempt-scoped keys, an upload that RAISES on
    failure, conditional create so a key is never written over, and one
    recorded entry per object carrying the immutable URI and the checksum of
    exactly the bytes uploaded. `context.publishable()` selects what to send
    rather than the ad-hoc on-disk filter this stage had grown its own copy of.
    """
    bucket = context.parameter("s3/products-bucket")

    entries = context.publishable()
    if not entries:
        raise InputError(
            "no stamped products exist to upload; post-process stamps "
            "identities into products the science pipeline already made, so "
            "an empty set means nothing was downloaded or nothing was stamped")

    published = publish_products(context, bucket, entries,
                                 product_type="post-processed")

    context.record(product_bucket=bucket,
                   product_prefix=context.product_prefix(),
                   products_uploaded=len(published))
    context.logger.info("published %d stamped products to s3://%s/%s",
                        len(published), bucket, context.product_prefix())
