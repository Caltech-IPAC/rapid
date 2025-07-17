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
===============      ===================      =====================================================================


Pipeline Improvements Pertinent To Testing Timeline
******************************************************

===============   =========================================================================================
Date              Software modification
===============   =========================================================================================
7/17/2025         Input correct FWHMs when generating gain-matching SExtractor catalogs
7/17/2025         Switched around "cconv" versus "dconv" SFFT filenames in ``crossconv_flag`` logic
===============   =========================================================================================


Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
