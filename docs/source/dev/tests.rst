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

===============   ========================================================================================================================
Date              Software modification
===============   ========================================================================================================================
5/14/2025         Added new capabilith to execute SFFT with the ``--crossconv`` flag
5/30/2025         Feed ZOGY computed astrometric uncertainties computed from gain-matching, instead of fixed value 0.01 pixels
5/30/2025         Shift the gain-matched reference image that is fed to ZOGY by subpixel x and y offsets computed from gain-matching
6/20/2025         Output new naive image-differencing product (``naive_diffimage_masked.fits``)
7/10/2025         Output new star finder catalog (``diffimage_masked_psfcat_finder.txt``)
7/17/2025         Input correct FWHMs when generating gain-matching SExtractor catalogs
7/17/2025         Switched around "cconv" versus "dconv" SFFT filenames in ``crossconv_flag`` logic
7/18/2025         Added new capability of fake-source injection
===============   ========================================================================================================================


Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
