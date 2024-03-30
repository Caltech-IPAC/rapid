Installing RAPID Pipeline
************************************

Download the source code
====================

.. code-block::
   
  git clone https://github.com/Caltech-IPAC/rapid


The C code in this git repo must be built, in order to run the RAPID pipeline.


Building C code on laptop
========================


The script to build the C software system for the RAPID pipeline is
located in

  rapid/c/builds/build_laptop.csh

1. Prerequisite for atlas-library build: brew install gfortran

2. Modify the following line in the build script to configure build environment, setting the absolute path of the rapid git repo.  E.g.,

  setenv RAPID_SW /Users/laher/Documents/rapid/git/rapid

3. Run the build script.

  cd /Users/laher/Documents/rapid/git/rapid/c/builds
  ./build_laptop.csh >& build_laptop.out &

The script may take some time to run as building the atlas library,
which is needed by sextractor, is part of the process.


Building C code on Linux machine
========================
