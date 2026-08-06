"""
File:    science.py

The science (prompt differencing) pipeline's stages.

Extracted from `awsBatchSubmitJobs_runSingleSciencePipeline.py` (2,961 lines,
one flat `__main__` block). The stage bodies below are that file's bodies: the
same helper calls, in the same order, with the same arguments. Read either one
beside the other and the science is the same.

What extraction forced, and nothing more:

**The six SExtractor blocks and six PSF-catalogue blocks are parameterized.**
They were ~110 lines each, near-identical, differing only in input filenames,
an output key prefix, an infobit, and — for the naive-difference pair — whether
the parquet/join step runs at all. That is ~1,000 of the 2,961 lines. They
collapse to `_sextractor_on_difference_image` and `_psf_catalog_for_difference_
image` with those four things as arguments. This is the one place the
extraction is not a literal move, and it is a move of duplication rather than
of logic: each call site passes exactly what its block hardcoded.

**The SExtractor save/revert protocol is gone, replaced by a copy.** The
monolith mutated one live `ConfigParser` section across six stages and reverted
two keys after each, using values captured once, 200 lines earlier. A missed
revert would have silently changed a later catalogue's weighting. Each call
here starts from `context.science_section(...)`, which returns a copy — so
there is nothing to revert and nothing to miss.

**`dxrmsfin`/`dyrmsfin` are still zeroed before ZOGY.** The monolith measured
them in the gain-match stage, then overwrote both with 0.0 immediately before
building the ZOGY command (a dated recommendation, lines 1218-1220), and the
zeroed values are what reached the product config. Preserved exactly, including
which value reaches the record — this is a science decision, not an operational
one, and W5's remit stops at the operational skeleton. The measured values are
recorded in provenance alongside, so the substitution is visible rather than
buried.

**The `>= 64` machinery and `run_sfft_was_successful` are gone.** The monolith's
whole failure vocabulary was one flag set at one call site, mapped to
`terminating_exitcode = 4`, then discarded by a `>= 64` test that could never
fire — and flattened to 64 by the shell wrapper regardless. An SFFT failure
here raises `ToolError`; the runtime records `tool_failure` and the termination
protocol authors the outcome.
"""

import os
import sys

import numpy as np

import modules.utils.rapid_pipeline_subs as util
import pipeline.differenceImageSubs as dfis
import pipeline.referenceImageSubs as rfis
from pipeline.runtime.errors import InputError
from pipeline.runtime.process import run_shell, run_tool

# The release-content tree inside the image. The monolith hardcoded "/code" as
# `rapid_sw` and "/code/cdf" as `cfg_path` at module scope; they are the
# installed software root and the auxiliary-file directory beside it, and the
# Containerfile now sets them as ENV so this module reads rather than assumes.
SOFTWARE_ROOT = os.environ.get("RAPID_SW", "/code")
CFG_PATH = os.environ.get("RAPID_CFG", os.path.join(SOFTWARE_ROOT, "cdf"))

# The interpreter for the bundled tool scripts (ZOGY, SFFT, the fake-source
# injector). The monolith hardcoded '/usr/bin/python3.11' at three sites; the
# co-design's "Also reconsidered" inventory calls for unified interpreter
# invocation, and `sys.executable` is that — the child runs under exactly the
# interpreter the parent is running under, whatever the image calls it.
#
# The name matters here. `/usr/bin/python3.11` in the current image is a
# symlink the Containerfile creates to /opt/rapid/conda/envs/rapid/bin/python,
# which is **Python 3.14** — the "3.11" is a legacy name kept only because the
# deleted shell wrappers hardcoded it. Naming a version that is not the
# version is how a future image bump silently runs a tool on a different
# interpreter than the pipeline; `sys.executable` cannot drift that way.
PYTHON = sys.executable

# Infobits set when a PSF-catalogue variant fails. One bit per variant, exactly
# as the monolith assigned them (2**0 at line 1471 through 2**5 at line 2770).
PSFCAT_INFOBIT = {
    "zogy_positive": 2 ** 0,
    "zogy_negative": 2 ** 1,
    "sfft_positive": 2 ** 2,
    "sfft_negative": 2 ** 3,
    "naive_positive": 2 ** 4,
    "naive_negative": 2 ** 5,
}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def download_inputs(context) -> None:
    """Fetch the science image and both PSFs. (Monolith stages E, F.)

    The monolith downloaded into the container's cwd; here everything lands in
    the per-attempt scratch tree, which is what makes two array children on one
    host unable to collide — the old cwd-relative naming was safe only because
    one container ran one unit.
    """
    science_image_uri = context.fact("science_image_uri")

    gz_name, _subdirs, _ = util.download_file_from_s3_bucket(
        context.s3, science_image_uri,
        outputfile=context.scratch(os.path.basename(science_image_uri)))
    context.produce("science_image_gz", gz_name)

    psf_uri = context.fact("psf_uri")
    sci_psf, _subdirs, _ = util.download_file_from_s3_bucket(
        context.s3, psf_uri,
        outputfile=context.scratch(os.path.basename(psf_uri)))
    context.produce("science_psf", sci_psf)

    # The reference-image PSF. The monolith built this URI from an .ini
    # directory plus a filename template carrying an "FID" placeholder that the
    # launcher substituted. The manifest now names the object outright — the
    # placeholder was a per-invocation fact wearing configuration's clothes.
    ref_psf_uri = context.fact("reference_image_uri").replace(
        "image.fits", "psf.fits")
    ref_psf, _subdirs, _ = util.download_file_from_s3_bucket(
        context.s3, ref_psf_uri,
        outputfile=context.scratch(os.path.basename(ref_psf_uri)))
    context.produce("reference_psf", ref_psf)

    context.logger.info("inputs: science=%s sci_psf=%s ref_psf=%s",
                        gz_name, sci_psf, ref_psf)


def gunzip_science_image(context) -> None:
    """Decompress the science image. (Monolith stage J, lines 775-785.)"""
    science_image_filename_gz = context.product("science_image_gz")
    science_image_filename = science_image_filename_gz.replace(".fits.gz", ".fits")

    if os.path.exists(science_image_filename):
        context.logger.info("the file %r exists", science_image_filename)
    else:
        context.logger.info("the file %r does not exist", science_image_filename)
        run_tool(["gunzip", "-f", science_image_filename_gz],
                 capture_path=context.workdir.tool_capture_path("gunzip"),
                 logger=context.logger)

    context.produce("science_image", science_image_filename)


def resolve_reference_image(context) -> None:
    """Fetch a prebuilt reference image, or build one. (Stages G1/G2.)

    The monolith's `rfid is not None` fork, and the reason `products` exists:
    the download branch leaves a dozen names undefined that the build branch
    defines, and the monolith coped with an `if rfid is None:` guard 200 lines
    downstream. Here the later stages ask for what they need by name and get a
    message naming the missing product instead of a `NameError`.
    """
    awaicgen = context.science_section("awaicgen")

    if context.optional_fact("reference_image_id") is not None:
        _download_reference_image(context, awaicgen)
    else:
        _build_reference_image(context, awaicgen)


def _download_reference_image(context, awaicgen) -> None:
    """The `rfid is not None` branch. (Monolith lines 352-399.)"""
    reference_uri = context.fact("reference_image_uri")

    image_file, _subdirs, _ = util.download_file_from_s3_bucket(
        context.s3, reference_uri,
        outputfile=context.scratch(awaicgen["awaicgen_output_mosaic_image_file"]))
    context.produce("reference_image", image_file)

    for key, product in (("awaicgen_output_mosaic_cov_map_file", "reference_cov_map"),
                         ("awaicgen_output_mosaic_uncert_image_file",
                          "reference_uncert_image")):
        uri = reference_uri.replace(
            os.path.basename(reference_uri), awaicgen[key])
        downloaded, _subdirs, _ = util.download_file_from_s3_bucket(
            context.s3, uri, outputfile=context.scratch(awaicgen[key]))
        context.produce(product, downloaded)

    sexcat_uri = reference_uri.replace("image.fits", "refimsexcat.txt")
    sexcat, _subdirs, _ = util.download_file_from_s3_bucket(
        context.s3, sexcat_uri,
        outputfile=context.scratch(os.path.basename(sexcat_uri)))
    context.produce("reference_sexcat", sexcat)

    context.record(reference_image_infobits=context.optional_fact(
        "reference_image_infobits", 0))


def _build_reference_image(context, awaicgen) -> None:
    """The `rfid is None` branch. (Monolith lines 402-575.)

    The monolith made a subdirectory, `chdir`-ed into it, ran the coadd, then
    `shutil.move`-d three products back up — because every filename in play was
    cwd-relative. The per-attempt workdir removes the need: the products are
    named absolutely and there is nothing to move.
    """
    instrument = context.science_section("instrument")
    fake_sources = context.science_section("fake_sources")

    # The coadd-input list is a per-invocation fact: the manifest names the
    # object holding it, where the monolith took two environment variables
    # (REFIMAGEINPUTSFILENAME / REFIMAGEINPUTSOBJNAME) set by the launcher.
    coadd_inputs_uri = context.fact("coadd_inputs_uri")
    job_bucket = context.parameter("s3/inputs-bucket")
    coadd_inputs_object = coadd_inputs_uri.split(f"{job_bucket}/", 1)[-1]
    coadd_inputs_local = context.scratch(os.path.basename(coadd_inputs_uri))

    generated = rfis.generateReferenceImage(
        context.s3,
        job_bucket,
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
        SOFTWARE_ROOT,
        context.optional_fact("reference_overlapping_fields", []),
    )

    (infobits_refimage, checksum_refimage,
     mosaic_image_file, mosaic_cov_map_file, mosaic_uncert_image_file,
     _obj_image, _obj_cov, _obj_uncert,
     nframes, refimage_input_filenames,
     jdstart, jdend, zprefimg, total_refimage_exptime) = generated

    context.produce("reference_image", mosaic_image_file)
    context.produce("reference_cov_map", mosaic_cov_map_file)
    context.produce("reference_uncert_image", mosaic_uncert_image_file)

    cov5percent = rfis.compute_cov5percent(mosaic_cov_map_file)

    n_sigma = 3.0
    hdu_index = 0
    stats_cov = util.fits_data_statistics_with_clipping(
        mosaic_cov_map_file, n_sigma, hdu_index)
    stats_unc = util.fits_data_statistics_with_clipping(
        mosaic_uncert_image_file, n_sigma, hdu_index)

    sextractor_refimage = context.science_section("sextractor_refimage")
    (checksum_sex_refimage_catalog, filename_sex_refimage_catalog,
     _obj_sexcat) = rfis.generateSExtractorReferenceImageCatalog(
        context.s3, context.parameter("s3/products-bucket"), context.unit.key,
        context.job_type, mosaic_image_file, mosaic_uncert_image_file,
        sextractor_refimage, True)
    context.produce("reference_sexcat", filename_sex_refimage_catalog)

    # The PhotUtils reference-image catalogue (monolith lines 508-529). The
    # first extraction dropped this call, so a science job that had to build
    # its own reference produced no reference PSF/finder catalogues at all —
    # while the dedicated reference-image pipeline, which runs the same coadd,
    # did produce them. The reference PSF is an input to this job type, so it
    # is available here by the same name the download branch produces.
    psfcat_refimage = context.science_section("psfcat_refimage")
    refimage_psfcat = rfis.generatePhotUtilsReferenceImageCatalog(
        context.s3, context.parameter("s3/products-bucket"), context.unit.key,
        context.job_type, mosaic_image_file, mosaic_uncert_image_file,
        context.product("reference_psf"), psfcat_refimage, True)

    (flag_psf_refimage_catalog, checksum_psf_refimage_catalog,
     checksum_psf_finder_refimage_catalog, filename_psf_refimage_catalog,
     filename_psf_finder_refimage_catalog, _obj_psfcat, _obj_finder,
     _uploaded_psfcat, _uploaded_finder) = refimage_psfcat

    context.produce("reference_psfcat", filename_psf_refimage_catalog)
    context.produce("reference_psfcat_finder",
                    filename_psf_finder_refimage_catalog)

    saturation_level_refimage = float(
        sextractor_refimage["sextractor_satur_level"])
    # The science monolith divided by the science image's exposure time here
    # (line 549), where the dedicated reference-image pipeline divides by a
    # literal 60.0 (its line 428). Both carry the same TODO calling the
    # stopgap incorrect; each is reproduced against its own authority rather
    # than unified, because unifying them would be a science change.
    saturation_level_refimage_rate = (saturation_level_refimage
                                      / float(context.fact("exptime")))

    stats_ref = util.fits_data_statistics_with_clipping(
        mosaic_image_file, n_sigma, hdu_index, saturation_level_refimage_rate)
    context.produce("saturation_level_refimage", saturation_level_refimage_rate)

    context.record(
        reference_image_infobits=infobits_refimage,
        reference_image_checksum=checksum_refimage,
        reference_sexcat_checksum=checksum_sex_refimage_catalog,
        reference_psfcat_ok=bool(flag_psf_refimage_catalog),
        reference_psfcat_checksum=checksum_psf_refimage_catalog,
        reference_psfcat_finder_checksum=checksum_psf_finder_refimage_catalog,
        reference_nframes=nframes,
        reference_cov5percent=cov5percent,
        reference_medncov=stats_cov["gmed"],
        reference_medpixunc=stats_unc["gmed"],
        reference_jdstart=jdstart,
        reference_jdend=jdend,
        reference_zeropoint=zprefimg,
        reference_total_exptime=total_refimage_exptime,
        reference_input_frames=len(refimage_input_filenames or []),
        reference_avg=stats_ref["clippedavg"],
        reference_std=stats_ref["clippedstd"],
    )


def measure_reference_fwhm(context) -> None:
    """FWHM from the reference-image catalogue. (Monolith stage H, 581-617.)"""
    paramsfile = CFG_PATH + "/rapidSexParamsRefImage.inp"
    vals = util.parse_ascii_text_sextractor_catalog(
        context.product("reference_sexcat"), paramsfile, ["FWHM_IMAGE"])

    fwhm_vals = np.array([float(val[0]) for val in vals])
    fwhm_ref = np.nanmedian(fwhm_vals)
    if np.isnan(fwhm_ref) or fwhm_ref < 0.0:
        fwhm_ref = 2.0

    context.produce("fwhm_ref", float(fwhm_ref))
    context.record(reference_sexcat_sources=len(vals),
                   fwhm_ref=float(fwhm_ref))
    context.logger.info("fwhm_ref = %s (from %d sources)", fwhm_ref, len(vals))


# ---------------------------------------------------------------------------
# Science-image preparation
# ---------------------------------------------------------------------------

def science_image_statistics(context) -> None:
    """Clipped statistics for image resizing. (Monolith stage K, 788-798.)

    Its own stage, and it must stay ahead of `inject_fake_sources` in the
    sequence. The monolith computed `avg_sci_img` here — at line 798, eight
    lines *before* the injection block opens at 806 — and passed that
    pre-injection value into the reformat at line 912, after injection had
    rebound `science_image_filename` to the injected image. Injected pixels
    therefore never entered this average.

    The first extraction folded this into `reformat_science_image`, which runs
    after injection, so the average was taken over the injected image and the
    uncertainty model shifted for injection-enabled runs only. Splitting the
    stage out is what makes the monolith's ordering expressible: a sequence
    position is visible, an inline computation's position is not.

    `hdu_index` is 1 here — the science image at this point is the raw
    downloaded FITS, whose data live in the first extension, not the PRIMARY.
    It is the reformat that moves them to PRIMARY.
    """
    saturation_level_sciimage = context.science_value(
        "sci_image", "saturation_level")

    stats_sci_img = util.fits_data_statistics_with_clipping(
        context.product("science_image"), 3.0, 1, saturation_level_sciimage)

    context.produce("avg_sci_img", stats_sci_img["clippedavg"])
    context.record(sci_image_avg_preinjection=stats_sci_img["clippedavg"],
                   sci_image_std_preinjection=stats_sci_img["clippedstd"])


def inject_fake_sources(context) -> None:
    """Optional fake-source injection. (Monolith stage L, lines 806-893.)

    Skipped unless the release enables it. The stage returns `SKIPPED` in that
    case rather than being absent from the sequence, so the attempt's stage
    records show it was considered — the monolith's `if` left no trace either
    way.
    """
    from pipeline.runtime.stages import SKIPPED

    if not context.science_value("fake_sources", "inject_fake_sources_flag"):
        return SKIPPED

    fake_sources = context.science_section("fake_sources")
    science_image_filename = context.product("science_image")

    file_content = ""
    for overlapping_field in context.optional_fact("overlapping_fields", []):
        injection_catalog_filename = \
            f"injection_catalog_rtid{overlapping_field}.json"
        uri = (f"s3://{context.parameter('s3/inputs-bucket')}"
               f"/injection_catalogs/{injection_catalog_filename}")
        downloaded_name, _subdirs, downloaded = \
            util.download_file_from_s3_bucket(
                context.s3, uri,
                outputfile=context.scratch(injection_catalog_filename))
        if downloaded:
            file_content += f"{downloaded_name}\n"
        else:
            context.logger.warning(
                "injection catalog is missing (%s); omitting",
                injection_catalog_filename)

    injection_catalog_list_filename = context.scratch(
        "injection_catalog_list_sciimg.csv")
    with open(injection_catalog_list_filename, "w") as handle:
        handle.write(file_content)

    fake_sources_code = SOFTWARE_ROOT + "/modules/fake_src/rapid_source_injections.py"
    run_tool([PYTHON, fake_sources_code,
              "--sci_ext", str(fake_sources["sci_ext"]),
              "--num_injections", str(fake_sources["num_injections"]),
              "--mag_min", str(fake_sources["mag_min"]),
              "--mag_max", str(fake_sources["mag_max"]),
              "--injections_by_field_flag",
              "--field_catalogs_input_filename", injection_catalog_list_filename,
              science_image_filename],
             capture_path=context.workdir.tool_capture_path("fake_sources"),
             logger=context.logger)

    filename_image_with_fake_sources = science_image_filename.replace(
        ".fits", "_inject.fits")
    filename_injection_catalog = science_image_filename.replace(
        ".fits", "_inject.txt")

    context.produce("injection_catalog", filename_injection_catalog)
    context.record(
        fake_sources_injected=int(fake_sources["num_injections"]),
        fake_sources_mag_min=float(fake_sources["mag_min"]),
        fake_sources_mag_max=float(fake_sources["mag_max"]),
    )

    # Propagate the science image with fake sources through the pipeline. In
    # the monolith this was a bare rebinding of `science_image_filename` at
    # line 886 — the single most consequential invisible assignment in the
    # file. `produce` logs the replacement.
    context.produce("science_image", filename_image_with_fake_sources)
    return None


def reformat_science_image(context) -> None:
    """Reformat and build the uncertainty image. (Monolith stage M, 899-914.)

    `avg_sci_img` comes from `science_image_statistics`, which ran before
    injection — the monolith's ordering, and the reason that stage is separate.
    Everything else here is stage M's body.
    """
    instrument = context.science_section("instrument")
    science_image_filename = context.product("science_image")
    avg_sci_img = context.product("avg_sci_img")

    reformatted = science_image_filename.replace(".fits", "_reformatted.fits")
    reformatted_unc = science_image_filename.replace(
        ".fits", "_reformatted_unc.fits")

    dfis.reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model(
        science_image_filename,
        float(instrument["sca_gain"]),
        float(instrument["sca_readout_noise"]),
        avg_sci_img,
        reformatted,
        reformatted_unc)

    context.produce("science_image_reformatted", reformatted)
    context.produce("science_uncert_image", reformatted_unc)


def science_image_catalog(context) -> None:
    """SExtractor on the science image, and its FWHM. (Stages N, O, 920-965.)"""
    reformatted = context.product("science_image_reformatted")
    filename_sciimage_catalog = reformatted.replace(".fits", "_secat.txt")

    util.generateScienceImageCatalog(
        reformatted,
        context.product("science_uncert_image"),
        CFG_PATH,
        context.science_section("sextractor_sciimage"),
        filename_sciimage_catalog)

    paramsfile = CFG_PATH + "/rapidSexParamsSciImage.inp"
    vals = util.parse_ascii_text_sextractor_catalog(
        filename_sciimage_catalog, paramsfile, ["FWHM_IMAGE"])

    fwhm_vals = np.array([float(val[0]) for val in vals])
    fwhm_sci = np.nanmedian(fwhm_vals)
    if np.isnan(fwhm_sci) or fwhm_sci < 0.0:
        fwhm_sci = 2.0

    context.produce("science_sexcat", filename_sciimage_catalog)
    context.produce("fwhm_sci", float(fwhm_sci))
    context.record(science_sexcat_sources=len(vals), fwhm_sci=float(fwhm_sci))
    context.logger.info("fwhm_sci = %s (from %d sources)", fwhm_sci, len(vals))


def resample_reference_image(context) -> None:
    """SWarp the reference image into the science frame. (Stage P, 968-998.)"""
    hdu_index_for_science_image_data = 0
    hdu_index_for_reference_image_data = 0
    # The reference image was made by awaicgen, so it carries no geometric
    # distortion and needs no sip->pv conversion. True only when the reference
    # is a single Roman SCA image.
    pv_convert_flag_for_reference_image_data = False

    resampled = util.resample_reference_image_to_science_image_with_pv_distortion(
        context.product("science_image_reformatted"),
        hdu_index_for_science_image_data,
        context.product("reference_image"),
        context.product("reference_cov_map"),
        context.product("reference_uncert_image"),
        hdu_index_for_reference_image_data,
        pv_convert_flag_for_reference_image_data,
        context.science_section("swarp"))

    (sci_fits_file_with_pv, ref_fits_file_with_pv,
     ref_cov_fits_file_with_pv, ref_uncert_fits_file_with_pv,
     output_resampled_reference_image, output_resampled_reference_cov_map,
     output_resampled_reference_uncert_image) = resampled

    context.produce("science_image_pv", sci_fits_file_with_pv)
    context.produce("reference_image_pv", ref_fits_file_with_pv)
    context.produce("reference_cov_pv", ref_cov_fits_file_with_pv)
    context.produce("reference_uncert_pv", ref_uncert_fits_file_with_pv)
    context.produce("resampled_reference_image", output_resampled_reference_image)
    context.produce("resampled_reference_cov_map",
                    output_resampled_reference_cov_map)
    context.produce("resampled_reference_uncert_image",
                    output_resampled_reference_uncert_image)
    context.produce("pv_convert_flag", pv_convert_flag_for_reference_image_data)


def normalize_science_psf(context) -> None:
    """Normalize the science PSF; the reference PSF already is. (Stage Q.)"""
    filename_sciimage_psf = context.product("science_psf")
    normalized = filename_sciimage_psf.replace(".fits", "_normalized.fits")
    util.normalize_image(filename_sciimage_psf, 0, normalized)
    context.produce("science_psf_normalized", normalized)


def subtract_background(context) -> None:
    """bkgest on the science image. (Monolith stage R, lines 1009-1043.)

    The resampled reference image already has its background subtracted, by
    virtue of having been swarped.
    """
    bkgest = context.science_section("bkgest")
    bkgest_code = SOFTWARE_ROOT + "/c/bin/bkgest"
    bkgest_include_dir = SOFTWARE_ROOT + "/c/include"

    filename_bkg_subbed_science_image = context.scratch(
        bkgest["filename_bkg_subbed_science_image"])
    filename_global_clippedmean_sciimage_tbl = context.scratch(
        bkgest["filename_global_clippedmean_sciimage_tbl"])

    run_tool([bkgest_code,
              "-i", context.product("science_image_pv"),
              "-f", str(bkgest["output_image_type"]),
              "-c", str(bkgest["clippedmean_calc_type"]),
              "-g", str(bkgest["local_clippedmean_grid_spacing"]),
              "-w", str(bkgest["local_clippedmean_input_window"]),
              "-a", bkgest_include_dir,
              "-ot", filename_global_clippedmean_sciimage_tbl,
              "-o2", filename_bkg_subbed_science_image],
             capture_path=context.workdir.tool_capture_path("bkgest"),
             logger=context.logger)

    context.produce("science_image_bkg_subbed",
                    filename_bkg_subbed_science_image)
    context.produce("science_clippedmean_table",
                    filename_global_clippedmean_sciimage_tbl)


def gain_match(context) -> None:
    """Gain-match science and reference. (Monolith stage S, lines 1046-1093.)

    To apply, multiply the reference image by `scalefacref`.
    """
    bkg_subbed = context.product("science_image_bkg_subbed")
    resampled_ref = context.product("resampled_reference_image")

    filename_scigainmatchsexcat_catalog = bkg_subbed.replace(
        ".fits", "_scigainmatchsexcat.txt")
    filename_refgainmatchsexcat_catalog = resampled_ref.replace(
        ".fits", "_refgainmatchsexcat.txt")

    scalefac, dxrmsfin, dyrmsfin, dxmedianfin, dymedianfin = \
        dfis.gainMatchScienceAndReferenceImages(
            context.s3,
            context.parameter("s3/products-bucket"),
            context.unit.key,
            context.job_type,
            bkg_subbed,
            context.product("science_uncert_image"),
            filename_scigainmatchsexcat_catalog,
            resampled_ref,
            context.product("resampled_reference_uncert_image"),
            filename_refgainmatchsexcat_catalog,
            context.science_section("awaicgen"),
            context.science_section("gainmatch"),
            context.science_section("sextractor_gainmatch"),
            context.product("fwhm_sci"),
            context.product("fwhm_ref"),
            context.science_value("zogy", "astrometric_uncert_x"),
            context.science_value("zogy", "astrometric_uncert_y"),
            True)

    scalefacref = 1. / scalefac

    gainmatched = resampled_ref.replace(".fits", "_gainmatched.fits")
    util.scale_image_data(resampled_ref, scalefacref, gainmatched)

    resampled_ref_unc = context.product("resampled_reference_uncert_image")
    gainmatched_unc = resampled_ref_unc.replace(".fits", "_gainmatched.fits")
    util.scale_image_data(resampled_ref_unc, scalefacref, gainmatched_unc)

    context.produce("science_gainmatch_sexcat",
                    filename_scigainmatchsexcat_catalog)
    context.produce("reference_gainmatch_sexcat",
                    filename_refgainmatchsexcat_catalog)
    context.produce("gainmatched_reference_image", gainmatched)
    context.produce("gainmatched_reference_uncert_image", gainmatched_unc)
    context.produce("scalefacref", scalefacref)
    context.produce("dxmedianfin", dxmedianfin)
    context.produce("dymedianfin", dymedianfin)
    context.record(gainmatch_scalefac=scalefac,
                   gainmatch_dxrms_measured=dxrmsfin,
                   gainmatch_dyrms_measured=dyrmsfin,
                   gainmatch_dxmedian=dxmedianfin,
                   gainmatch_dymedian=dymedianfin)


def prepare_zogy_inputs(context) -> None:
    """NaN handling, statistics, offsets, PSF transpose. (Stage T, 1096-1152.)"""
    bkg_subbed = context.product("science_image_bkg_subbed")
    gainmatched = context.product("gainmatched_reference_image")

    nan_indices_sciimage = util.replace_nans_with_value(bkg_subbed, 0.0)
    nan_indices_refimage = util.replace_nans_with_value(gainmatched, 0.0)

    n_sigma = 3.0
    hdu_index = 0
    stats_sci_img = util.fits_data_statistics_with_clipping(
        context.product("science_image_reformatted"), n_sigma, hdu_index,
        context.science_value("sci_image", "saturation_level"))
    # The **undivided** reference-image saturation level, read straight from
    # release content exactly as the monolith read it once at line 304 and used
    # it here at line 1117. The first extraction reached for the
    # `saturation_level_refimage` product instead — which is the *rate*, the
    # saturation level already divided by an exposure time — so ZOGY's input
    # reference statistics clipped at a threshold some tens of times too low,
    # and only when the reference had been built inline (the download branch
    # never produces that product, so the lookup fell through to a
    # `ref_image.saturation_level` key that does not exist in release content).
    saturation_level_refimage = float(
        context.science_section("sextractor_refimage")["sextractor_satur_level"])
    stats_ref_img = util.fits_data_statistics_with_clipping(
        context.product("resampled_reference_image"), n_sigma, hdu_index,
        saturation_level_refimage)

    std_sci_img = stats_sci_img["clippedstd"]
    std_ref_img = stats_ref_img["clippedstd"]

    util.replace_nans_with_value(context.product("science_uncert_image"),
                                 std_sci_img)
    util.replace_nans_with_value(
        context.product("gainmatched_reference_uncert_image"), std_ref_img)

    util.apply_subpixel_orthogonal_offsets(
        gainmatched, context.product("dxmedianfin"),
        context.product("dymedianfin"))

    # Transpose the science-image PSF for rimtimsim data.
    if "rimtimsim" in context.product("science_image"):
        util.transpose_image_data(context.product("science_psf_normalized"))

    context.produce("nan_indices_sciimage", nan_indices_sciimage)
    context.produce("nan_indices_refimage", nan_indices_refimage)
    context.produce("std_sci_img", std_sci_img)
    context.produce("std_ref_img", std_ref_img)
    context.record(zogy_input_avg_sci=stats_sci_img["clippedavg"],
                   zogy_input_std_sci=std_sci_img,
                   zogy_input_avg_ref=stats_ref_img["clippedavg"],
                   zogy_input_std_ref=std_ref_img)


# ---------------------------------------------------------------------------
# Difference imaging
# ---------------------------------------------------------------------------

def run_zogy(context) -> None:
    """ZOGY difference imaging. (Monolith stage V, lines 1196-1248.)

    The image data in the science image and its pv-distortion twin are the
    same; only the header representation of the distortion differs, and ZOGY
    reads only the data.
    """
    zogy = context.science_section("zogy")
    zogy_code = SOFTWARE_ROOT + "/modules/zogy/v21Aug2018/py_zogy.py"

    filename_diffimage = context.scratch(zogy["zogy_output_diffimage_file"])
    filename_diffpsf = context.scratch(zogy["zogy_output_diffpsf_file"])
    filename_scorrimage = context.scratch(zogy["zogy_output_scorrimage_file"])

    # 2025-08-15 Jacob's recommendation for the next Big Run: the measured
    # astrometric RMS is not passed to ZOGY. Preserved verbatim from monolith
    # lines 1218-1220 — a science decision, outside W5's remit. The measured
    # values are in provenance (gain_match records them) so the substitution
    # is visible rather than silent.
    dxrmsfin = 0.0
    dyrmsfin = 0.0

    scalefacref = context.product("scalefacref")
    std_ref_img = context.product("std_ref_img")

    run_tool([PYTHON, zogy_code,
              context.product("science_image_bkg_subbed"),
              context.product("gainmatched_reference_image"),
              context.product("science_psf_normalized"),
              context.product("reference_psf"),
              context.product("science_uncert_image"),
              context.product("gainmatched_reference_uncert_image"),
              str(context.product("std_sci_img")),
              str(std_ref_img * scalefacref),
              str(dxrmsfin),
              str(dyrmsfin),
              filename_diffimage,
              filename_diffpsf,
              filename_scorrimage],
             capture_path=context.workdir.tool_capture_path("zogy"),
             logger=context.logger)

    context.produce("zogy_diffimage", filename_diffimage)
    context.produce("zogy_diffpsf", filename_diffpsf)
    context.produce("zogy_scorrimage", filename_scorrimage)
    context.produce("diffimage_infobits", 0)
    context.record(zogy_dxrms_used=dxrmsfin, zogy_dyrms_used=dyrmsfin)


def postprocess_zogy(context) -> None:
    """Mask, restore NaNs, build negatives and the uncertainty image.

    Monolith stages W and X, lines 1251-1338.
    """
    diffimage = context.product("zogy_diffimage")
    scorrimage = context.product("zogy_scorrimage")
    cov_map = context.product("resampled_reference_cov_map")
    threshold = context.science_value(
        "zogy", "post_zogy_keep_diffimg_lower_cov_map_thresh")

    # Record the fake-source injection in the difference image's header.
    if context.has_product("injection_catalog"):
        fake_sources = context.science_section("fake_sources")
        util.addHistoryLinesToFITSHeader(
            diffimage, ["Fake sources were injected into science image."], 0)
        util.addKeywordsToFITSHeader(
            diffimage, ["NINJECT", "MNMAGINJ", "MXMAGINJ"],
            [fake_sources["num_injections"], fake_sources["mag_min"],
             fake_sources["mag_max"]], 0, diffimage)

    masked = diffimage.replace(".fits", "_masked.fits")
    dfis.mask_difference_image_with_resampled_reference_cov_map(
        diffimage, cov_map, masked, threshold)

    scorr_masked = scorrimage.replace(".fits", "_masked.fits")
    dfis.mask_difference_image_with_resampled_reference_cov_map(
        scorrimage, cov_map, scorr_masked, threshold)

    # Restore the NaNs that were zeroed before ZOGY ran, from both images.
    nan_sci = context.product("nan_indices_sciimage")
    nan_ref = context.product("nan_indices_refimage")
    for target in (masked, scorr_masked):
        if nan_sci:
            util.restore_nans(target, nan_sci)
        if nan_ref:
            util.restore_nans(target, nan_ref)

    negative = masked.replace(".fits", "_negative.fits")
    util.scale_image_data(masked, -1.0, negative)
    scorr_negative = scorr_masked.replace(".fits", "_negative.fits")
    util.scale_image_data(scorr_masked, -1.0, scorr_negative)

    # The uncertainty image doubles as SExtractor's WEIGHT_IMAGE.
    unc_masked = masked.replace("masked.fits", "uncert_masked.fits")
    instrument = context.science_section("instrument")
    dfis.compute_diffimage_uncertainty(
        float(instrument["sca_gain"]) * context.fact("exptime"),
        context.product("science_image_reformatted"),
        context.product("gainmatched_reference_image"),
        cov_map,
        threshold,
        masked,
        unc_masked)
    weight_image = unc_masked

    context.produce("zogy_diffimage_masked", masked)
    context.produce("zogy_scorrimage_masked", scorr_masked)
    context.produce("zogy_diffimage_masked_negative", negative)
    context.produce("zogy_scorrimage_masked_negative", scorr_negative)
    context.produce("zogy_diffimage_unc_masked", unc_masked)
    context.produce("weight_image", weight_image)


def run_sfft(context) -> None:
    """SFFT difference imaging. (Monolith stage AF1, lines 1809-1916.)

    Skipped unless the release enables it.

    **The venv.** The monolith activated `/sfft_env` in a shell, ran SFFT, and
    deactivated — the pipeline's only genuine shell case. Whether that venv is
    still needed is W5's recorded investigation (see the ledger and
    `docs/source/dev/sfft_environment.rst`): where SFFT's dependency set
    coexists with the image environment it is invoked directly through
    `run_tool`, and the shell path is used only when a real conflict forces it.
    The choice is made by the image, not by this code: `RAPID_SFFT_VENV` is set
    in the Containerfile if and only if a separate interpreter exists.

    Either way the invocation is checked. The monolith's
    `execute_command_in_shell` returned an exit code that mapped to
    `terminating_exitcode = 4`, which the `>= 64` test then discarded and the
    shell wrapper flattened to 64 — an SFFT failure could not fail a job.
    `run_shell` and `run_tool` both raise `ToolError`.
    """
    from pipeline.runtime.stages import SKIPPED

    if not context.science_value("sfft", "run_sfft"):
        return SKIPPED

    sfft_code = SOFTWARE_ROOT + "/modules/sfft/sfft_rapid_rimtimsim.py"
    science_image = context.product("science_image")

    crossconv_flag = context.science_value("sfft", "crossconv_flag")
    if "rimtimsim" in science_image:
        # rimtimsim data: cross-convolution is off regardless of the release
        # setting (monolith lines 1832-1834).
        crossconv_flag = False

    argv = _sfft_argv(context, sfft_code, science_image, crossconv_flag)

    venv = os.environ.get("RAPID_SFFT_VENV")
    capture = context.workdir.tool_capture_path("sfft")
    if venv:
        # The dependency sets genuinely conflict; the venv stays and is
        # entered through the one checked shell variant. Arguments are quoted:
        # the monolith's `' '.join(sfft_cmd)` would break on any filename
        # containing a shell metacharacter.
        import shlex
        command = (f"source {shlex.quote(venv)}/bin/activate && "
                   + " ".join(shlex.quote(part) for part in argv)
                   + " && deactivate")
        run_shell(command, capture_path=capture, logger=context.logger)
    else:
        run_tool(argv, capture_path=capture, logger=context.logger)

    # SFFT's output filenames are a function of the cross-convolution flag, and
    # the tool writes them beside its science-image argument
    # (`modules/sfft/sfft_rapid_rimtimsim.py:206-217`) — which is in scratch,
    # so they land there. The monolith named them at lines 1837-1846; the same
    # four names, derived the same way.
    if crossconv_flag:
        diffimage = context.scratch("sfftdiffimage_dconv_masked.fits")
        soln = context.scratch("sfftsoln_cconv.fits")
        diffpsf = context.scratch("sfftdiffpsf_dconv.fits")
        context.produce("sfft_cconv_diffimage",
                        context.scratch("sfftdiffimage_cconv_masked.fits"))
    else:
        diffimage = context.scratch("sfftdiffimage_masked.fits")
        soln = context.scratch("sfftsoln.fits")
        diffpsf = context.scratch("sfftdiffpsf.fits")

    context.produce("sfft_diffimage", diffimage)
    context.produce("sfft_soln", soln)
    context.produce("sfft_diffpsf", diffpsf)
    context.produce("sfft_crossconv_flag", crossconv_flag)


def _sfft_argv(context, sfft_code, science_image, crossconv_flag) -> list:
    """Build SFFT's argument vector. (Monolith lines 1849-1892.)

    Every token here is checked against the tool's own parser
    (`modules/sfft/sfft_rapid_rimtimsim.py:312-328`). The first extraction
    invented a vocabulary — six positional inputs and `--sci_star_list`,
    `--ref_star_list`, `--crossconv_flag` — that the parser rejects with status
    2, so every SFFT-enabled science job failed in argparse before SFFT ran.

    The real shape: two positionals (science, reference), and the branch is on
    whether the science image filename begins with "r" — the rimtimsim case,
    which passes **no** catalogues and masks bright sources hard (20000.0 over
    30 pixels), where the OpenUniverse case passes the gain-match catalogues as
    `--scicat`/`--refcat` and masks gently (50.0 over 100 pixels). The first
    extraction had this branch inverted as well as misnamed.

    `--crossconv` is `action="store_true"`, so it is present or absent — never
    given a value — and it brings `--refpsf`, `--scisegm` and `--refsegm` with
    it. `--scipsf` is passed unconditionally.
    """
    filename_scifile = context.product("science_image_bkg_subbed")
    filename_reffile = context.product("gainmatched_reference_image")

    if os.path.basename(science_image).startswith("r"):
        argv = [PYTHON, sfft_code,
                filename_scifile,
                filename_reffile,
                "--bsmaskvalue", "20000.0",
                "--bsmaskradius", "30.0"]
    else:
        argv = [PYTHON, sfft_code,
                filename_scifile,
                filename_reffile,
                "--scicat", context.product("science_gainmatch_sexcat"),
                "--refcat", context.product("reference_gainmatch_sexcat"),
                "--bsmaskvalue", "50.0",
                "--bsmaskradius", "100.0"]

    # If crossconv is off, the SFFT difference-image PSF is just the science
    # image's PSF — which is why --scipsf is unconditional (monolith 1880-1883).
    argv += ["--scipsf", context.product("science_psf_normalized")]

    if crossconv_flag:
        argv += ["--crossconv",
                 "--refpsf", context.product("reference_psf"),
                 "--scisegm", context.scratch("sfftscisegm.fits"),
                 "--refsegm", context.scratch("sfftrefsegm.fits")]

    return argv


# ---------------------------------------------------------------------------
# The parameterized catalogue templates
# ---------------------------------------------------------------------------

def sextractor_on_difference_image(context, variant: str, image: str,
                                   detection_image: str,
                                   weight_image: str | None = None,
                                   override_weighting: bool = True) -> dict:
    """SExtractor on one difference image. (Six monolith blocks, ~50 lines each.)

    The blocks at 1341-1387, 1390-1436, 2027-2077, 2080-2130, 2542-2573 and
    2576-2607 were identical but for the two input filenames, the output
    catalogue name, and — for the naive pair — whether the weighting overrides
    are applied at all. Those are the arguments.

    Detection runs on the Scorr (S/N) match-filter image and photometry on the
    difference image, which is why both are passed.

    The dict is a copy from release content, so the monolith's manual
    save/revert of `WEIGHT_TYPE` and `FILTER` after every block is unnecessary:
    nothing here outlives the call.
    """
    sextractor = context.science_section("sextractor_diffimage")
    paramsfile = CFG_PATH + "/rapidSexParamsDiffImage.inp"
    catalog = image.replace(".fits", ".txt")

    sextractor["sextractor_detection_image"] = detection_image
    sextractor["sextractor_input_image"] = image
    if override_weighting:
        # The naive-difference blocks (monolith 2542-2607) deliberately do not
        # override these two, and did not revert them either.
        sextractor["sextractor_weight_type"] = "NONE,MAP_RMS"
        sextractor["sextractor_filter"] = "N"
    # Each difference-image variant weights with its own uncertainty image;
    # `weight_image` is the monolith's `filename_weight_image`, which it
    # reassigned per variant (1329 for ZOGY, 1984 for SFFT, 2539 for naive).
    # Defaulting to the ZOGY product keeps the ZOGY call sites unchanged.
    sextractor["sextractor_weight_image"] = (
        weight_image if weight_image is not None
        else context.product("weight_image"))
    sextractor["sextractor_parameters_name"] = paramsfile
    sextractor["sextractor_filter_name"] = \
        CFG_PATH + "/rapidSexDiffImageFilter.conv"
    sextractor["sextractor_starnnw_name"] = \
        CFG_PATH + "/rapidSexDiffImageStarGalaxyClassifier.nnw"
    sextractor["sextractor_catalog_name"] = catalog

    run_tool(util.build_sextractor_command_line_args(sextractor),
             capture_path=context.workdir.tool_capture_path(
                 f"sextractor_{variant}"),
             logger=context.logger)

    vals = util.parse_ascii_text_sextractor_catalog(
        catalog, paramsfile, ["XWIN_IMAGE", "YWIN_IMAGE", "FLUX_APER_6"])

    context.produce(f"sexcat_{variant}", catalog)
    context.record(**{f"sexcat_sources_{variant}": len(vals)})
    context.logger.info("sexcat sources (%s) = %d", variant, len(vals))
    return {"catalog": catalog, "values": vals}


def psf_catalog_for_difference_image(context, variant: str, image: str,
                                     uncert_image: str, psf: str,
                                     output_prefix: str,
                                     negative: bool = False,
                                     with_parquet: bool = True) -> None:
    """PSF-fit catalogue for one difference image, via photutils.

    Six monolith blocks (~110 lines each) at 1439-1548, 1551-1660, 2158-2269,
    2272-2383, 2638-2734 and 2737-2833, differing in inputs, the output key
    prefix, whether the variant is the negative one, the infobit set on
    failure, and — for the naive pair — the absence of the parquet/join step.
    No background subtraction is done.

    **The output schema is the monolith's, and it is not just `phot`.** Each
    block computed sky coordinates for the fitted pixel positions and added
    `ra`/`dec` columns (1500-1509), wrote the photometry table with
    `ascii.write`, wrote `psfphot.finder_results` as a *second* catalogue
    (1519-1523), and — for the ZOGY and SFFT variants — inner-joined photometry
    with finder results on `id` before writing the parquet (1526-1537). The
    first extraction wrote `phot` alone in a different ascii format, dropped
    the finder catalogue entirely, and parqueted the unjoined table: downstream
    readers lost both the sky coordinates and every finder column.

    **The negative variants get their own filenames.** The monolith derived
    them by `.replace(".txt", "_negative.txt")` on the same configured name
    (1551-1562, 2286-2288, 2751-2753). The first extraction passed the same
    `output_prefix` for both signs, so both resolved to one configured
    filename and the negative invocation overwrote the positive catalogue —
    then uploaded negative bytes under both product names.

    The monolith wrapped everything after `compute_psf_catalog` in a bare
    `except Exception` that printed and continued. That is preserved in effect
    but not in form: a failure to build the catalogue's columns sets the
    variant's infobit, which reaches the terminal record, rather than printing
    into a log nobody reads.

    On failure of `compute_psf_catalog` itself the monolith OR-ed a per-variant
    infobit into `output_diffimage_file_infobits` and continued rather than
    failing the job. Preserved: a PSF-catalogue failure is a product-quality
    fact carried in infobits, not a job outcome.
    """
    from astropy.io import ascii
    from astropy.table import join

    psfcat = context.science_section("psfcat_diffimage")

    n_clip_sigma = float(psfcat["n_clip_sigma"])
    n_thresh_sigma = float(psfcat["n_thresh_sigma"])
    fwhm = float(psfcat["fwhm"])
    fit_shape_str = str(psfcat["fit_shape"])
    fit_shape = tuple(int(x) for x in fit_shape_str
                      .replace("(", "").replace(")", "")
                      .replace(" ", "").split(","))
    aperture_radius = float(psfcat["aperture_radius"])

    catalog_name = psfcat[f"output_{output_prefix}_psfcat_filename"]
    finder_name = psfcat[f"output_{output_prefix}_psfcat_finder_filename"]
    residual_name = psfcat[f"output_{output_prefix}_psfcat_residual_filename"]
    if negative:
        catalog_name = catalog_name.replace(".txt", "_negative.txt")
        finder_name = finder_name.replace(".txt", "_negative.txt")
        residual_name = residual_name.replace(".fits", "_negative.fits")

    output_psfcat_filename = context.scratch(catalog_name)
    output_psfcat_finder_filename = context.scratch(finder_name)
    output_psfcat_residual_filename = context.scratch(residual_name)

    psfcat_flag, phot, psfphot = util.compute_psf_catalog(
        n_clip_sigma, n_thresh_sigma, fwhm, fit_shape, aperture_radius,
        image, uncert_image, psf, output_psfcat_residual_filename)

    if not psfcat_flag:
        _set_psfcat_infobit(context, variant)
        return

    try:
        # Sky coordinates for the fitted pixel positions, from the reformatted
        # science image's WCS (monolith 1500-1509). Both `ra` and `dec` become
        # catalogue columns; without them the parquet carries pixel coordinates
        # alone and nothing downstream can position a candidate on the sky.
        ra, dec = util.computeSkyCoordsFromPixelCoords(
            context.product("science_image_reformatted"),
            list(phot["x_fit"]), list(phot["y_fit"]))
        phot.add_column(ra, name="ra")
        phot.add_column(dec, name="dec")

        ascii.write(phot, output_psfcat_filename, overwrite=True)
        context.produce(f"psfcat_{variant}", output_psfcat_filename)

        ascii.write(psfphot.finder_results, output_psfcat_finder_filename,
                    overwrite=True)
        context.produce(f"psfcat_finder_{variant}",
                        output_psfcat_finder_filename)

        if with_parquet:
            # The naive-difference variants stop at the two ascii writes
            # (monolith 2686-2726, 2755-2833); only the ZOGY and SFFT variants
            # join and write parquet.
            joined = join(phot, psfphot.finder_results, keys="id",
                          join_type="inner")
            parquet = output_psfcat_filename.replace(".txt", ".parquet")
            joined.to_pandas().to_parquet(parquet, engine="pyarrow")
            context.produce(f"psfcat_parquet_{variant}", parquet)
            context.record(**{f"psfcat_joined_rows_{variant}": len(joined)})

        context.record(**{f"psfcat_sources_{variant}": len(phot)})
    except Exception as exc:
        # The monolith's bare `except Exception: print(...)` (1539-1540 and its
        # five twins). The job continues, but the failure is now a recorded
        # product-quality fact rather than a line of stdout.
        context.logger.warning(
            "PSF-fit catalog columns failed for %s: %s", variant, exc)
        _set_psfcat_infobit(context, variant)


def _set_psfcat_infobit(context, variant: str) -> None:
    """OR one variant's failure bit into the difference-image infobits."""
    infobits = context.products.get("diffimage_infobits", 0)
    context.produce("diffimage_infobits", infobits | PSFCAT_INFOBIT[variant])
    context.logger.warning(
        "PSF catalog failed for %s; infobit %d set", variant,
        PSFCAT_INFOBIT[variant])


# ---------------------------------------------------------------------------
# Difference-image variant drivers
# ---------------------------------------------------------------------------

def catalog_zogy(context) -> None:
    """Both SExtractor catalogues and both PSF catalogues for ZOGY."""
    sextractor_on_difference_image(
        context, "zogy_positive",
        context.product("zogy_diffimage_masked"),
        context.product("zogy_scorrimage_masked"))
    sextractor_on_difference_image(
        context, "zogy_negative",
        context.product("zogy_diffimage_masked_negative"),
        context.product("zogy_scorrimage_masked_negative"))

    psf_catalog_for_difference_image(
        context, "zogy_positive",
        context.product("zogy_diffimage_masked"),
        context.product("zogy_diffimage_unc_masked"),
        context.product("zogy_diffpsf"), "zogy")
    psf_catalog_for_difference_image(
        context, "zogy_negative",
        context.product("zogy_diffimage_masked_negative"),
        context.product("zogy_diffimage_unc_masked"),
        context.product("zogy_diffpsf"), "zogy", negative=True)


def catalog_sfft(context) -> None:
    """The SFFT variants' catalogues, when SFFT ran. (Monolith 1921-2431.)

    Every input here is SFFT's own. The first extraction detected on ZOGY's
    Scorr image and fitted with ZOGY's uncertainty image and ZOGY's difference
    PSF, so the SFFT candidates, their weights and their fitted fluxes were
    ZOGY's answers wearing SFFT's product names. The monolith built an SFFT
    uncertainty image (1974-1984), used the SFFT difference PSF (2171), and
    detected on the cross-convolved image when crossconv was on and the SFFT
    difference image when it was off (2031-2034, 2084-2087).
    """
    from pipeline.runtime.stages import SKIPPED

    if not context.has_product("sfft_diffimage"):
        return SKIPPED

    sfft_image = context.product("sfft_diffimage")
    crossconv_flag = context.product("sfft_crossconv_flag")

    # Record the fake-source injection in SFFT's difference-image headers, as
    # the monolith did for both the deconvolved and cross-convolved images
    # (1926-1954), before anything reads them.
    if context.has_product("injection_catalog"):
        fake_sources = context.science_section("fake_sources")
        targets = [sfft_image]
        if crossconv_flag:
            targets.append(context.product("sfft_cconv_diffimage"))
        for target in targets:
            util.addHistoryLinesToFITSHeader(
                target, ["Fake sources were injected into science image."], 0)
            util.addKeywordsToFITSHeader(
                target, ["NINJECT", "MNMAGINJ", "MXMAGINJ"],
                [fake_sources["num_injections"], fake_sources["mag_min"],
                 fake_sources["mag_max"]], 0, target)

    # Replace NaNs in the SFFT difference image with zeros (monolith 1957-1959).
    util.replace_nans_with_value(sfft_image, 0.0)

    sfft_negative = sfft_image.replace(".fits", "_negative.fits")
    util.scale_image_data(sfft_image, -1.0, sfft_negative)
    context.produce("sfft_diffimage_negative", sfft_negative)

    if crossconv_flag:
        cconv = context.product("sfft_cconv_diffimage")
        cconv_negative = cconv.replace(".fits", "_negative.fits")
        util.scale_image_data(cconv, -1.0, cconv_negative)
        context.produce("sfft_cconv_diffimage_negative", cconv_negative)
        detection_positive = cconv
        detection_negative = cconv_negative
    else:
        detection_positive = sfft_image
        detection_negative = sfft_negative

    # SFFT's own uncertainty image, which is also its SExtractor weight image
    # (monolith 1972-1984). ZOGY's was a different image over a different
    # difference.
    sfft_unc_masked = context.scratch("sfftdiffimage_uncert_masked.fits")
    instrument = context.science_section("instrument")
    dfis.compute_diffimage_uncertainty(
        float(instrument["sca_gain"]) * context.fact("exptime"),
        context.product("science_image_reformatted"),
        context.product("gainmatched_reference_image"),
        context.product("resampled_reference_cov_map"),
        context.science_value("zogy",
                              "post_zogy_keep_diffimg_lower_cov_map_thresh"),
        sfft_image,
        sfft_unc_masked)
    context.produce("sfft_diffimage_unc_masked", sfft_unc_masked)

    sfft_diffpsf = context.product("sfft_diffpsf")

    for variant, image, detection, is_negative in (
            ("sfft_positive", sfft_image, detection_positive, False),
            ("sfft_negative", sfft_negative, detection_negative, True)):
        sextractor_on_difference_image(
            context, variant, image, detection,
            weight_image=sfft_unc_masked)
        psf_catalog_for_difference_image(
            context, variant, image, sfft_unc_masked, sfft_diffpsf, "sfft",
            negative=is_negative)
    return None


def naive_difference(context) -> None:
    """The naive difference image and its catalogues. (Stage AG, 2437-2870.)

    The monolith's ordering, restored. The first extraction skipped the
    coverage-map masking entirely — differencing straight into the catalogues —
    and then detected on ZOGY's Scorr image, weighted with ZOGY's uncertainty
    image, and fitted with ZOGY's difference PSF. The monolith masked with the
    resampled reference coverage map (2469-2476), built a naive uncertainty
    image (2527-2539), detected on the naive image itself (2547-2548,
    2581-2582), and fitted with the **reference** image's PSF (2651, 2750 —
    both carrying its `# TODO`).
    """
    from pipeline.runtime.stages import SKIPPED

    if not context.science_value("naive_diffimage", "naive_diffimage_flag"):
        return SKIPPED

    # The unmasked difference first; the configured name belongs to the masked
    # product (monolith 2441, 2471).
    naive_raw = context.scratch("naive_diffimage.fits")
    util.compute_naive_difference_image(
        context.product("science_image_bkg_subbed"),
        context.product("gainmatched_reference_image"),
        naive_raw)

    # Record the fake-source injection in the header (monolith 2448-2466).
    if context.has_product("injection_catalog"):
        fake_sources = context.science_section("fake_sources")
        util.addHistoryLinesToFITSHeader(
            naive_raw, ["Fake sources were injected into science image."], 0)
        util.addKeywordsToFITSHeader(
            naive_raw, ["NINJECT", "MNMAGINJ", "MXMAGINJ"],
            [fake_sources["num_injections"], fake_sources["mag_min"],
             fake_sources["mag_max"]], 0, naive_raw)

    threshold = context.science_value(
        "zogy", "post_zogy_keep_diffimg_lower_cov_map_thresh")
    cov_map = context.product("resampled_reference_cov_map")

    naive = context.scratch(context.science_value(
        "naive_diffimage", "naive_output_diffimage_file"))
    dfis.mask_difference_image_with_resampled_reference_cov_map(
        naive_raw, cov_map, naive, threshold)

    nan_sci = context.product("nan_indices_sciimage")
    nan_ref = context.product("nan_indices_refimage")
    if nan_sci:
        util.restore_nans(naive, nan_sci)
    if nan_ref:
        util.restore_nans(naive, nan_ref)

    naive_negative = naive.replace(".fits", "_negative.fits")
    util.scale_image_data(naive, -1.0, naive_negative)

    context.produce("naive_diffimage", naive)
    context.produce("naive_diffimage_negative", naive_negative)

    # The naive difference's own uncertainty image, and its weight image.
    naive_unc_masked = naive.replace("masked.fits", "uncert_masked.fits")
    instrument = context.science_section("instrument")
    dfis.compute_diffimage_uncertainty(
        float(instrument["sca_gain"]) * context.fact("exptime"),
        context.product("science_image_reformatted"),
        context.product("gainmatched_reference_image"),
        cov_map,
        threshold,
        naive,
        naive_unc_masked)
    context.produce("naive_diffimage_unc_masked", naive_unc_masked)

    reference_psf = context.product("reference_psf")

    for variant, image, is_negative in (("naive_positive", naive, False),
                                        ("naive_negative", naive_negative,
                                         True)):
        # Detection runs on the naive image itself, not on a Scorr image —
        # the naive difference has none. The monolith also left WEIGHT_TYPE
        # and FILTER at their release-content values for this pair alone.
        sextractor_on_difference_image(
            context, variant, image, image,
            weight_image=naive_unc_masked,
            override_weighting=False)
        psf_catalog_for_difference_image(
            context, variant, image, naive_unc_masked, reference_psf, "naive",
            negative=is_negative, with_parquet=False)
    return None


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def upload_products(context) -> None:
    """Upload the science products. (Monolith stages U, AD, AF3, AG2, AI.)

    The monolith interleaved six upload blocks through the pipeline, each
    reassigning `product_s3_bucket = product_s3_bucket_base` first (nine times,
    always to the same value). One upload at the end, of everything produced,
    replaces them — and what is uploaded is `products`, so a stage that skipped
    contributes nothing rather than raising a `NameError` on its filename.

    No product `.ini` is written. The attempt's terminal record carries what
    the `.ini` carried, keyed by attempt identity and immutable, and
    registration reads reconciled records rather than parsing a config file.
    """
    bucket = context.parameter("s3/products-bucket")
    prefix = f"{context.job_type}/{context.unit.key}"

    uploadable = [(name, value) for name, value in sorted(context.products.items())
                  if isinstance(value, str) and os.path.isfile(value)]
    if not uploadable:
        raise InputError(
            "no product files exist to upload; every stage either skipped or "
            "produced nothing on disk")

    filenames = [value for _name, value in uploadable]
    objectnames = [f"{prefix}/{os.path.basename(value)}"
                   for _name, value in uploadable]

    util.upload_files_to_s3_bucket(context.s3, bucket, filenames, objectnames)

    context.record(product_bucket=bucket, product_prefix=prefix,
                   products_uploaded=len(filenames),
                   diffimage_infobits=context.products.get(
                       "diffimage_infobits", 0))
    context.logger.info("uploaded %d products to s3://%s/%s",
                        len(filenames), bucket, prefix)
