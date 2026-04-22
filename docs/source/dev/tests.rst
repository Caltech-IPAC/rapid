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
10/29/2025        Set ``min_separation = 1.0`` pixel for PhotUtils catalog generation.
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
4/7/2026          Modified SFFT code to output a difference-image PSF
4/9/2026          Changes to how the uncertainty images are calculated (for science image and refimage inputs).
4/13/2026         Replaces hard-wired value 1750.0 with saturation_value_rate_sciimage for processing rimtimsims.
4/16/2026         Modified crossMatchSources.py to only cross-match sources with flags = 0.
4/17/2026         Modified crossMatchSources.py to cross-match using AstroObjects (meanra,meandec) instead of (ra0,dec0).
4/17/2026         Modified crossMatchSources.py to update AstroObjects (meanra,meandec) for each lightcurve data point added.
4/20/2026         Modified to cross-match all sources in one observation at a time for all SCAs in ascending time order.
4/20/2026         Modified to load into RAPID operations database the SFFT-difference-image PhotUtils catalogs, instead of ZOGY.
4/21/2026         Modified to replace NaNs, if any, in SFFT difference image with zeros.
4/21/2026         Modified to replace NaNs, if any, in difference-image uncertainty images with std_dif_img.
4/21/2026         Increased [SCI_IMAGE] saturation level from 100000 to 1100000 for rimtimsims.
4/22/2026         Modified SFFT command for rimtimsims to use the brute-force masking options.
4/22/2026         In the latest version of PhotUtils, output column name ``npixfit`` has been changed to ``n_pixels_fit``.
===============   ===============================================================================================================================================================================================================================


Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
