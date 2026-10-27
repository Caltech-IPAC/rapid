.. _testing:

RAPID Pipeline Testing
####################################################

Overview
************************************

The tests described below are organized by processing date.

On any given date will be a test involving either OpenUniverse or RimTimSim simulated data.


Databases Used For Testing
******************************************************

One or more separate PostgreSQL databases have been set up for each different simulated dataset or
particularly distinct testing scenario:

===============      ===================      =====================================================================
Dataset                Database name            Comment
===============      ===================      =====================================================================
OpenUniverse           rapidopsdb               Observation range of reference images earlier than test (normal)
OpenUniverse           specialdb                Observation range of reference images later than test (special)
RimTimSim              rimtimsimdb              Observation range of reference images earlier than test (normal)
OpenUniverse           fakesourcesdb            For fake-source injection (copy of specialdb)
RimTimSim              rimtimsims2db            For fake-source injection (emptied copy of fakesourcesdb)
SOC GBTDS Sims         socsimsdb                For fake-source injection (emptied copy of rimtimsims2db)
RimTimSim              rimtimsims3db            New sims delivered on 6/22/26 (emptied copy of socsimsdb)
===============      ===================      =====================================================================


Pipeline Improvements Pertinent To Testing Timeline
******************************************************

===============   ===============================================================================================================================================================================================================================
Date              Software modification
===============   ===============================================================================================================================================================================================================================
5/14/2025         Added new capability to execute SFFT with the ``--crossconv`` flag
5/30/2025         Feed ZOGY computed astrometric uncertainties computed from gain-matching, instead of fixed value 0.01 pixels
5/30/2025         Shift the gain-matched reference image that is fed to ZOGY by subpixel x and y offsets computed from gain-matching
6/20/2025         Output new naive-difference-image product (``naive_diffimage_masked.fits``)
7/10/2025         Output new star finder catalog (``diffimage_masked_psfcat_finder.txt``)
7/17/2025         Input correct FWHMs when generating gain-matching SExtractor catalogs
7/17/2025         Switched around "cconv" versus "dconv" SFFT filenames in ``crossconv_flag`` logic
7/18/2025         Added new capability of fake-source injection
7/23/2025         Added computation of naive-difference-image SExtractor catalog (``naive_diffimage_masked.txt``)
8/15/2025         Changes for 8/17/25 test (Big Run).  Made correction to uncertainty-image formula.
8/15/2025         New PSF-fit catalog for SFFT difference image.
8/15/2025         Changed ``[FAKE_SOURCES] num_injections = 100, mag_min = 21.0, mag_max = 28.0``.
8/15/2025         Changed ``[PSFCAT_DIFFIMAGE] fwhm = 2.0``.
8/15/2025         Changed ``[SEXTRACTOR_DIFFIMAGE] FILTER_THRESH = 3.0, DEBLEND_NTHRESH = 32, WEIGHT_TYPE = "NONE,MAP_RMS", FILTER = "N"`` (last two parameters are overrided in code for ZOGY and SFFT SExtractor catalogs).
8/15/2025         Fed ZOGY ``dxrmsfin = 0.0, dyrmsfin = 0.0``.
9/8/2025          Modified to not limit the precision of (ra, dec) in PSF-fit catalogs.
9/16/2025         Added code to generate naive-difference-image PSF-fit catalogs.
9/16/2025         Added code to generate SExtractor catalogs and PSF-fit catalogs for negative difference images (ZOGY, SFFT, naive).
9/17/2025         Modified to feed ``sca_gain * exptime_sciimage`` as gain to method ``pipeline.differenceImageSubs.compute_diffimage_uncertainty``.
9/17/2025         Fixed bug: x and y subpixels offsets were swapped (adversely affected inputs to ZOGY, SFFT, and naive image-differencing).
9/25/2025         Added new method normalize_image to normalize science-image PSFs (required by ZOGY).
10/10/2025        Added source matching within/without field boundaries to populate Sources, Merges, and AstroObjects database tables.
10/11/2025        Added methods to compute statistics for AstroObjects database tables.
10/29/2025        Set ``min_separation = 0.0`` pixels for PhotUtils catalog generation.
11/19/2025        Upgraded to SExtractor 2.28.2.
11/25/2025        Modified ``awaicgen`` for execution on Mac laptop (compiler is more strict than Linux).
12/4/2025         Explicitly cast data and uncertainty images as ndarrays when passed to PhotUtils methods (not sure whether this actually caused any problems).
12/8/2025         Fixed call to ``romanisim.psf.make_one_psf`` method after interface changed.
12/17/2025        New SFFT python module that works on rimtimsim images.
12/22/2025        Adjusted ``awaicgen_num_threads = 2`` to match the number of VCPUs in the AWS Batch machines used by the RAPID pipeline.
1/14/2026         Modified science pipeline to output catalogs in parquet format.
1/24/2026         Added methods to delete not-best records in Sources and Merges database tables.
1/30/2026         Developed code to generate sources and lightcurves HATS catalogs.
1/31/2026         Various miscellaneous improvements such as modifications to run RAPID science pipeline on Mac laptop.
2/3/2026          Created forced-photometry backend and added ``cforcepsfaper`` C module.
2/4/2026          Reduced-chi2 in PhotUtils catalogs and Sources database table.
2/11/2026         Scaled reference-image inputs so that reference image has fixed zero point = 17 mag.
2/12/2026         Modified to generate PhotUtils catalog for reference image.
3/24/2026         Completed upgrade/debugging fake-source injection for variable sources with fixed sky positions.
4/7/2026          Modified SFFT code to output a difference-image PSF.
4/9/2026          Changes to how the uncertainty images are calculated (for science image and refimage inputs).
4/13/2026         Replaces hard-wired value 1750.0 with ``saturation_value_rate_sciimage`` for processing rimtimsims.
4/16/2026         Modified crossMatchSources.py to only cross-match sources with ``flags = 0``.
4/17/2026         Modified crossMatchSources.py to cross-match using AstroObjects ``(meanra,meandec)`` instead of ``(ra0,dec0)``.
4/17/2026         Modified crossMatchSources.py to update AstroObjects ``(meanra,meandec)`` record for each lightcurve data point added.
4/20/2026         Modified to cross-match all sources in one observation at a time for all SCAs in ascending time order.
4/20/2026         Modified to load into RAPID operations database the SFFT-difference-image PhotUtils catalogs, instead of ZOGY.
4/21/2026         Modified to replace NaNs, if any, in SFFT difference image with zeros.
4/21/2026         Modified to replace NaNs, if any, in difference-image uncertainty images with ``std_dif_img``.
4/21/2026         Increased ``[SCI_IMAGE] saturation_level`` from 100000 to 1100000 for rimtimsims.
4/22/2026         Modified SFFT command for rimtimsims to use the brute-force masking options (``--bsmaskvalue 20000.0 --bsmaskradius 30.0``).
4/22/2026         In the latest version of PhotUtils, output column name ``npixfit`` has been changed to ``n_pixels_fit``, and output column name ``npix`` has been changed to ``n_pixels``.
4/23/2026         Modified to use ``filename_sfftdiffpsf`` for SFFT-difference-image PSF-fit catalog generation, instead of ``filename_refimage_psf`` as before.
4/24/2026         Changed ``[SOURCE_MATCHING] match_radius`` to 0.00001528 degrees (half a Roman WFI pixel).  Reran cross-matching for the 4/23/2026 test.
4/28/2026         Modified SFFT code to refactor bright star masking in ``bkg_mask`` to use binary_dilation.
4/28/2026         Modified SFFT code to replace the slow per-pixel distance loop with ``scipy.ndimage.binary_dilation`` and a precomputed circular footprint.
4/28/2026         Modified SFFT code so that when a SExtractor catalog is provided, a second pass after catalog masking to catch any remaining bright pixels above ``bsmask_value``.
4/29/2026         Modified SFFT code to fix logic path issues, and set ``sat_value`` and ``bsmask_value`` defaults to 1e6 to disable masking unless explicitly set.
5/12/2026         Modified to scale the reference-image uncertainty map by the gain-matching scale factor (prior to this, gain-matching was only applied to the reference image).
5/12/2026         Moved the block of code that uploads intermediate products to just before ZOGY execution (this facilitates running ZOGY offline from S3-bucket downloaded inputs).
5/19/2026         Modified to feed ZOGY scaled std_ref_img by scalefacref  (gain-matching correction).
5/27/2026         Modified gain-matching to fall back on scalefac = 10**(0.4*(magzpref - magzpsci)) if unable to find a sufficient number of matches.
6/2/2026          Updated L2Files database table for 5th-order SIP distortion.
6/23/2026         Upgraded awaicgen version from 5.2 to 5.4 (fixed bug in handling SIP distortion).
7/8/2026          Modified to replace NaNs in difference-image inputs with zeros.
8/5/2026          Made correction to ``data_unc`` formula (line 64 of ``pipeline/differenceImageSubs.py``).
8/5/2026          Modified to feed ``dfis.compute_diffimage_uncertainty method`` background-subtracted ``filename_bkg_subbed_science_image`` instead of ``reformatted_science_image_filename`` (with background).
8/5/2026          Set ``min_separation = 1.0`` pixels for PhotUtils catalog generation.
10/14/2026        * Changed reference-image size from 7Kx7K to 9.1Kx9.1K.
                  * New ``ProcReqs`` database table that reserves a unique ``reqid`` for each VPO processing episode.
                  * Req ID included in S3 pathnames (stored in ``ProcReqs`` database table, and referenced in DB via ``Jobs.reqid`` column).
                  * Extra columns added to SExtractor and PhotUtils catalogs.
                  * VPO loads both Sources and XSources child database tables.
                  * Sources and XSources child database tables have extra columns.
                  * Pipeline now computes real-bogus scores (``rb``).
                  * DB load scripts have prefiltering on ``sumrat`` (> 0.4) and ``rb`` (> 0.0 at this time).
                    Prefilter: reject xsources with ``sumrat`` or ``rb`` at or below its threshold, where computed
                    (do not reject if no value is available to threshold).
                  * Alert generation has been incorporated into the RAPID pipeline!
                  * VPO upgrades to resubmit failed AWS Batch jobs due to infrastructure errors.
                  * VPO upgrade to execute the RAPID pipeline in an open loop automatically.
                  * Reference-image infobits, as defined by the ZTF project, are now being computed and set by the RAPID pipeline.
10/15/2026        * New ``[SEXTRACTOR_DIFFIMAGE_SFFT]`` and ``[SEXTRACTOR_DIFFIMAGE_NAIVE]`` sections override ``[SEXTRACTOR_DIFFIMAGE]`` for the SFFT and naive difference-image SExtractor catalogs:
                    7x7 Gaussian filter with FWHM 2.0 pixels, ``DETECT_MINAREA = 2``, ``DETECT_THRESH = ANALYSIS_THRESH = 2.5`` (ZOGY unchanged; SFFT section not applied with ``--crossconv``) (PR #247).
                  * Reference-image background now subtracted with the same ``bkgest`` command as the science image (``[BKGEST] subtract_from_reference = True``),
                    instead of by SWarp (``[SWARP] swarp_SUBTRACT_BACK = N`` for the reference image) (PR #256).
                  * SExtractor ``SEEING_FWHM`` changed from 1.2 to 0.25 arcsec (science-image, reference-image, difference-image and gain-matching catalogs), and ``DEBLEND_NTHRESH``
                    from 4 to 32 (science-image, reference-image and gain-matching catalogs);
                    ``[GAINMATCH] min_class_star_thresh`` changed from 0.5 to 0.0 (the ``CLASS_STAR`` cut had left gain matching on its MAGZP-based fallback) (PR #257).
                  * PhotUtils-style columns in the SExtractor difference-image catalogs now use a 5x5 PSF-fit box (``[EXTRA_CATALOG_COLS] photutils_cols_fit_shape``), instead of 17x17 (PR #258).
                  * ``sumrat``, ``nneg`` and ``nbad`` now measured after subtracting a local background (``[EXTRA_CATALOG_COLS] pixel_cols_bkg_box = 64``, ``pixel_cols_bkg_filter_size = 3``) (PR #258).
                  * XSources database prefilter adds an SNR cut, ``FLUX_APER_1 / FLUXERR_APER_1 > 5.0`` (``[DATABASE_PREFILTER] snr_prefilter_threshold``),
                    and the XSources loader rejects sources within 2 pixels of the image edge (PR #258).
10/23/2026        * Change #1 made to complete 10/21/2026 socsims test: Select the not-best [x]sids on the database server in ``pruneNotBest[X]Merges.py``, instead of client-side selection,
                    in order to eliminate queries larger than 1 GB (PostgreSQL has a 1 GB limit).
                  * Change #2 made to complete 10/21/2026 socsims test: Optimized container run command:
                    ``docker run -it --entrypoint bash --name russ-test-jobsubmit  --shm-size=64g -e TMPDIR=/dev/shm
                    -v /home/ubuntu/work/test_20261021:/work -e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 -e MKL_NUM_THREADS=1
                    -e NUMEXPR_NUM_THREADS=1 public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest``
10/26/2026        * Only the SFFT difference image is registered in the DiffImages database table, from the new ``[SFFT]`` section of the product config file (replacing ``[ZOGY]``);
                    ``[SFFT] run_sfft`` must be True, and a job that does not run SFFT or whose SFFT fails exits with code 65 (replacing exit code 4) (PR #478).
                  * New ``[ZOGY] run_zogy`` flag (default True) skips the ZOGY products; naive products are skipped with ``[NAIVE_DIFFIMAGE] naive_diffimage_flag = False`` (PR #478).
                  * New ``[VPO_SETTINGS] load_sources_tables`` flag (default True): False skips loading the Sources, Merges, AstroObjects and AstroObjectsMeta tables, and alert production;
                    the [X]tables are always loaded (PR #479).
10/27/2026        * The VPO now deletes the not-best (``vbest = 0``) records of the Sources and XSources child tables (new stages 18 and 19, ``pruneNotBestSources.py`` and new ``pruneNotBestXSources.py``),
                    after pruning the Merges/XMerges records; alert production is now stage 20 (PR #504).
===============   ===============================================================================================================================================================================================================================


Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
   socsim_tests.rst
