RAPID Pipeline Testing
####################################################

Overview
************************************

The tests described below are organized by processing date.

On any given date will be a test involving either OpenUniverse or RimTimSim simulated data.

A separate PostgreSQL database has been set up for each different simulated dataset:

===============      ===================
Dataset                Database name
===============      ===================
OpenUniverse           rapidopsdb
RimTimSim              rimtimsimdb
===============      ===================

Tests
************************************

.. toctree::
   :maxdepth: 2

   openuniv_tests.rst
   rimtimsim_tests.rst
