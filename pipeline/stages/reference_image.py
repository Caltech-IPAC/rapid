"""
File:    reference_image.py

The reference-image construction pipeline's stages.

Extracted from `awsBatchSubmitJobs_runSingleReferenceImagePipeline.py` (734
lines). The science here is a coadd and two catalogues over it, and every call
below is that file's call.

The operational skeleton it shed was almost the whole of the difference. That
script's `terminating_exitcode` was assigned the literal `0` at line 720 and
never set from any error condition — so an upload failure (three sites,
logged and dropped), an `ls` failure, or a missing product all reported
success, and the only nonzero exits were nine bare `exit(64)` calls in the
environment-variable preamble. Under the runtime a stage that fails raises,
and the outcome is authored from what actually happened.

Also gone: the `chdir` into a coadd subdirectory followed by three
`shutil.move` calls back up — a dance the cwd-relative filenames required. The
per-attempt workdir makes the products absolutely named where they are built.
"""

import os

import numpy as np

import modules.utils.rapid_pipeline_subs as util
import pipeline.referenceImageSubs as rfis
from pipeline.mosaic_geometry import resolve_awaicgen_geometry
from pipeline.runtime import science_config
from pipeline.runtime.errors import InputError
from pipeline.stages.publishing import (publish_products, split_s3_uri,
                                        verify_downloaded_input)

# Fail-loud and per call, not a module-scope `os.environ.get(..., "/code")`:
# see the same pair in `pipeline/stages/science.py`.
SOFTWARE_ROOT = science_config.software_root
CFG_PATH = science_config.config_directory


def download_reference_psf(context) -> None:
    """Fetch the reference-image PSF. (Monolith stage S4, lines 235-247.)"""
    psf_uri = context.fact("psf_uri")
    psf, _subdirs, downloaded = util.download_file_from_s3_bucket(
        context.s3, psf_uri,
        outputfile=context.scratch(os.path.basename(psf_uri)))
    if not downloaded:
        # The monolith called `exit(64)` here (line 243), which the wrapper
        # then reported as 64 with no record of why.
        raise InputError(
            f"the reference-image PSF at {psf_uri} could not be downloaded",
            uri=psf_uri)
    context.produce("reference_psf", psf)


def build_reference_image(context) -> None:
    """Coadd the input frames. (Monolith stage S5b-1, lines 305-353.)"""
    awaicgen = context.science_section("awaicgen")
    instrument = context.science_section("instrument")
    fake_sources = context.science_section("fake_sources")

    # The coadd's mosaic extent and centre. The launcher computed these and
    # substituted them into `[AWAICGEN]` before dispatch, so the four keys
    # exist in the master `.ini` only as `to_be_filled_by_script` and the W4B
    # migration had no values to carry. `resolve_awaicgen_geometry` is that
    # computation ported verbatim; the centre comes from the `tile_position`
    # fact because it varies per field, the extent from release content
    # because it does not. See `pipeline/mosaic_geometry.py`.
    resolve_awaicgen_geometry(awaicgen,
                              context.science_section("ref_image"),
                              context.fact("tile_position"))

    coadd_inputs_uri = context.fact("coadd_inputs_uri")
    # The bucket comes from the URI, not from `s3/inputs-bucket`. The URI
    # already names where the object is, and taking the bucket from the
    # parameter instead asserted that the coadd-input list must live in the
    # STAGED-INPUT bucket — two different kinds of data with two different
    # lifecycles (upstream bytes that are sealed create-once, versus a list
    # this submission just authored) forced into one location by a string
    # split. Worse, the split was silent when it disagreed: with a URI in
    # any other bucket, `split(f"{job_bucket}/", 1)[-1]` returns the whole
    # URI unchanged and the download is attempted with an `s3://...` string
    # as the KEY.
    coadd_inputs_bucket, coadd_inputs_object = split_s3_uri(coadd_inputs_uri)
    coadd_inputs_local = context.scratch(os.path.basename(coadd_inputs_uri))

    # Fetch the coadd-input list here and check it against the checksum the
    # manifest cited, BEFORE any coadding happens (review finding #9).
    # `generateReferenceImage` downloads this same object to this same path as
    # its first act, so the fetch is not new work being added — it is the same
    # fetch, moved to where its bytes can still be refused. Verified after the
    # coadd it would only tell us the reference image we just built was made
    # from the wrong frames.
    context.s3.download_file(coadd_inputs_bucket, coadd_inputs_object,
                             coadd_inputs_local)
    verify_downloaded_input(
        context, "coadd-input list", coadd_inputs_local,
        context.optional_fact("coadd_inputs_checksum"))

    generated = rfis.generateReferenceImage(
        context.s3,
        coadd_inputs_bucket,
        coadd_inputs_object,
        coadd_inputs_local,
        context.unit.key,
        context.job_type,
        awaicgen,
        context.science_value("ref_image", "max_n_images_to_coadd"),
        float(instrument["sca_gain"]),
        float(instrument["sca_readout_noise"]),
        context.parameter("s3/products-bucket"),
        True,
        context.science_value("fake_sources", "inject_fake_sources_flag"),
        fake_sources,
        SOFTWARE_ROOT(),
        context.optional_fact("reference_overlapping_fields", []),
        # Any diagnostic input upload keys under THIS attempt, never the
        # legacy jid path a retry would overwrite (#18).
        upload_key_prefix=context.product_prefix(),
    )

    (infobits_refimage, checksum_refimage,
     mosaic_image_file, mosaic_cov_map_file, mosaic_uncert_image_file,
     _obj_image, _obj_cov, _obj_uncert,
     nframes, refimage_input_filenames,
     jdstart, jdend, zprefimg, total_refimage_exptime) = generated

    context.produce("reference_image", mosaic_image_file)
    context.produce("reference_cov_map", mosaic_cov_map_file)
    context.produce("reference_uncert_image", mosaic_uncert_image_file)
    context.produce("reference_input_filenames", refimage_input_filenames)
    context.produce("reference_nframes", nframes)
    context.produce("reference_jdstart", jdstart)
    context.produce("reference_jdend", jdend)
    context.produce("reference_zeropoint", zprefimg)
    context.produce("reference_total_exptime", total_refimage_exptime)

    context.record(reference_image_infobits=infobits_refimage,
                   reference_image_checksum=checksum_refimage,
                   reference_nframes=nframes,
                   reference_jdstart=jdstart,
                   reference_jdend=jdend,
                   reference_zeropoint=zprefimg,
                   reference_total_exptime=total_refimage_exptime)


def coverage_and_uncertainty_statistics(context) -> None:
    """Coverage fraction and clipped statistics. (Stages S5b-2, S5b-3.)"""
    cov_map = context.product("reference_cov_map")
    uncert_image = context.product("reference_uncert_image")

    cov5percent = rfis.compute_cov5percent(cov_map)

    n_sigma = 3.0
    hdu_index = 0
    stats_cov = util.fits_data_statistics_with_clipping(
        cov_map, n_sigma, hdu_index)
    stats_unc = util.fits_data_statistics_with_clipping(
        uncert_image, n_sigma, hdu_index)

    # `gmed` is the clipped median the statistics helper actually returns
    # (`modules/utils/rapid_pipeline_subs.py:295`); the monolith read it under
    # that name at lines 476 and 482. There is no `clippedmed` key.
    context.produce("cov5percent", cov5percent)
    context.record(reference_cov5percent=cov5percent,
                   reference_medncov=stats_cov["gmed"],
                   reference_medpixunc=stats_unc["gmed"])


def sextractor_catalog(context) -> None:
    """SExtractor over the coadd. (Monolith stage S5b-4, lines 378-391.)"""
    sextractor_refimage = context.science_section("sextractor_refimage")

    (checksum, catalog, _obj) = rfis.generateSExtractorReferenceImageCatalog(
        context.s3,
        context.parameter("s3/products-bucket"),
        context.unit.key,
        context.job_type,
        context.product("reference_image"),
        context.product("reference_uncert_image"),
        sextractor_refimage,
        True)

    context.produce("reference_sexcat", catalog)
    context.record(reference_sexcat_checksum=checksum)


def psf_catalog(context) -> None:
    """PhotUtils PSF catalogue. (Monolith stage S5b-5, lines 394-415.)"""
    psfcat_refimage = context.science_section("psfcat_refimage")

    result = rfis.generatePhotUtilsReferenceImageCatalog(
        context.s3,
        context.parameter("s3/products-bucket"),
        context.unit.key,
        context.job_type,
        context.product("reference_image"),
        context.product("reference_uncert_image"),
        context.product("reference_psf"),
        psfcat_refimage,
        True)

    (flag_psf_refimage_catalog, checksum_psf_refimage_catalog,
     _checksum_finder, filename_psf_catalog, _filename_finder,
     obj_psf_catalog, _obj_finder, uploaded, _uploaded_finder) = result

    context.produce("reference_psfcat", filename_psf_catalog)
    context.record(reference_psfcat_ok=bool(flag_psf_refimage_catalog),
                   reference_psfcat_checksum=checksum_psf_refimage_catalog,
                   reference_psfcat_object=obj_psf_catalog,
                   reference_psfcat_uploaded=bool(uploaded))


def image_statistics(context) -> None:
    """Clipped statistics over the coadd. (Stages S5b-6, S5b-7, 423-454.)"""
    sextractor_refimage = context.science_section("sextractor_refimage")
    saturation_level_refimage = float(
        sextractor_refimage["sextractor_satur_level"])

    # Stopgap: the saturation level is a per-exposure value and the coadd is in
    # rate units. Carried over verbatim (monolith lines 423-428, with its TODO).
    saturation_level_refimage_rate = saturation_level_refimage / 60.0

    stats = util.fits_data_statistics_with_clipping(
        context.product("reference_image"), 3.0, 0,
        saturation_level_refimage_rate)

    # The key names are the statistics helper's own
    # (`modules/utils/rapid_pipeline_subs.py:291-301`), read here under exactly
    # the names the monolith read them under at lines 448-454: `gsigma` is what
    # it called `datascale`, `gdatamin`/`gdatamax` what it called `gmin`/`gmax`,
    # and `satcount`/`nancount` what it called `npixsat`/`npixnan`. The
    # provenance field names below keep the monolith's vocabulary; only the
    # lookups are corrected.
    context.record(
        reference_avg=stats["clippedavg"],
        reference_std=stats["clippedstd"],
        reference_noutliers=stats["noutliers"],
        reference_gmed=stats["gmed"],
        reference_datascale=stats["gsigma"],
        reference_gmin=stats["gdatamin"],
        reference_gmax=stats["gdatamax"],
        reference_npixsat=stats["satcount"],
        reference_npixnan=stats["nancount"],
    )


def measure_fwhm(context) -> None:
    """FWHM from the catalogue. (Monolith stage S6, lines 457-485.)"""
    paramsfile = CFG_PATH() + "/rapidSexParamsRefImage.inp"
    vals = util.parse_ascii_text_sextractor_catalog(
        context.product("reference_sexcat"), paramsfile, ["FWHM_IMAGE"])

    fwhm_vals = np.array([float(val[0]) for val in vals])
    fwhm_min = float(np.nanmin(fwhm_vals)) if len(fwhm_vals) else float("nan")
    fwhm_max = float(np.nanmax(fwhm_vals)) if len(fwhm_vals) else float("nan")
    fwhm_med = float(np.nanmedian(fwhm_vals)) if len(fwhm_vals) else float("nan")

    context.record(reference_sexcat_sources=len(vals),
                   fwhm_ref_minpix=fwhm_min,
                   fwhm_ref_maxpix=fwhm_max,
                   fwhm_ref_medpix=fwhm_med)


def add_header_keywords(context) -> None:
    """Stamp provenance into both images' headers. (Stage S9, lines 568-595.)"""
    for product in ("reference_image", "reference_uncert_image"):
        rfis.addKeywordsToReferenceImageHeader(
            context.product(product),
            context.fact("field"),
            context.fact("fid"),
            context.fact("filter_name"),
            context.product("cov5percent"),
            context.product("reference_nframes"),
            context.product("reference_input_filenames"),
            context.product("reference_jdstart"),
            context.product("reference_jdend"),
            context.product("reference_zeropoint"),
            context.product("reference_total_exptime"))


def upload_products(context) -> None:
    """Upload the coadd and its companions. (Stages S9 upload block, S13.)

    The monolith's three upload sites each caught `ClientError`, printed it,
    and carried on — so a job whose products never reached the bucket still
    reported success and still wrote a product `.ini` claiming they were there.

    TWO defects this docstring used to describe away (review finding #18).

    The key was `job_type/unit`, carrying neither run nor attempt identity, so
    a reference retry or a reprocessing run OVERWROTE the previous attempt's
    objects and left earlier records citing keys whose bytes had changed. It is
    now `context.product_prefix()`, the same run/attempt-scoped builder the
    science path uses — the one place product keys are built.

    And `upload_files_to_s3_bucket` did NOT raise: it returned a boolean nobody
    read, so exactly the swallow this docstring claimed was fixed was still
    there, one layer down. `publish_products` raises, and records each object's
    URI and checksum so the terminal record names what was actually published.
    """
    bucket = context.parameter("s3/products-bucket")

    published = publish_products(context, bucket, context.publishable(),
                                 product_type="reference")

    context.record(product_bucket=bucket,
                   product_prefix=context.product_prefix(),
                   products_uploaded=len(published))
    context.logger.info("uploaded %d products to s3://%s/%s",
                        len(published), bucket, context.product_prefix())
