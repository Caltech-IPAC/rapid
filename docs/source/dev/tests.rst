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
8/15/2025         Changes 8/17/25 test (Big Run).  Made correction to uncertainty-image formula.
8/15/2025         New PSF-fit catalog for SFFT difference image.
8/15/2025         Changed [FAKE_SOURCES] num_injections = 100, mag_min = 21.0, mag_max = 28.0.
8/15/2025         Changed [PSFCAT_DIFFIMAGE] fwhm = 2.0.
8/15/2025         Changed [SEXTRACTOR_DIFFIMAGE] FILTER_THRESH = 3.0, DEBLEND_NTHRESH = 32, WEIGHT_TYPE = "NONE,MAP_RMS", FILTER = "N" (last two parameters are overrided in code for ZOGY and SFFT SExtractor catalogs).
8/15/2025         Fed ZOGY dxrmsfin = 0.0, dyrmsfin = 0.0.
9/8/2025          Modified to not limit the precision of (ra, dec) in PSF-fit catalogs.
9/16/2025         Added code to generate naive-difference-image PSF-fit catalogs.
9/16/2025         Added code to generate SExtractor catalogs and PSF-fit catalogs for negative difference images (ZOGY, SFFT, naive).
9/17/2025         Modified to feed sca_gain * exptime_sciimage as gain to method pipeline.differenceImageSubs.compute_diffimage_uncertainty.
9/17/2025         Fixed bug: x and y subpixels offsets were swapped (adversely affected inputs to ZOGY, SFFT, and naive image-differencing).
9/25/2025         Added new method normalize_image to normalize science-image PSFs (required by ZOGY).
10/29/2025        Set min_separation = 1.0 pixel for PhotUtils catalog generation.
11/19/2025        Upgraded to SExtractor 2.28.2.
12/4/2025         Explicitly cast data and uncertainty images as ndarrays when passed to PhotUtils methods (not sure whether this actually caused any problems).
12/22/2025        Adjusted awaicgen_num_threads = 2 to match the number of VCPUs in the AWS Batch machines used by the RAPID pipeline.
===============   ===============================================================================================================================================================================================================================


Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
