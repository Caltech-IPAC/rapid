RAPID Pipeline Testing
####################################################

Overview
************************************

The tests described below are organized by processing date.

On any given date will be a test involving either OpenUniverse or RimTimSim simulated data.

One or more separate PostgreSQL databases have been set up for each different simulated dataset:

===============      ===================      =====================================================================
Dataset                Database name            Comment
===============      ===================      =====================================================================
OpenUniverse           rapidopsdb               Observation range of reference images earlier than test (normal)
OpenUniverse           specialdb                Observation range of reference images later than test (special)
RimTimSim              rimtimsimdb              Observation range of reference images earlier than test (normal)
===============      ===================      =====================================================================

Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
