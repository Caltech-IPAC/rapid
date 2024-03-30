Installing RAPID Pipeline
************************************

Download the source code
====================

.. code-block::

   cd /source/code/location
   git clone https://github.com/Caltech-IPAC/rapid


The C code in this git repo must be built, in order to run the RAPID pipeline.


Building C code on laptop
========================


The script to build on a laptop the C software system for the RAPID pipeline is

  /source/code/location/rapid/c/builds/build_laptop.csh

This script is has been tested on a Mac laptop running macOS Montery.
  
1. Prerequisite for atlas-library build: brew install gfortran

2. Modify the following line in the build script to configure build environment, setting the absolute path of the rapid git repo.  E.g.,

  setenv RAPID_SW /source/code/location/rapid

3. Run the build script.

.. code-block::
   
   cd /source/code/location/rapid/c/builds
   ./build_laptop.csh >& build_laptop.out &

The script may take some time to finish as building the atlas library,
which is needed by sextractor, is part of the process.

The binary executables, libraries, and include file are
installed under the following paths:

.. code-block::
   
   /source/code/location/rapid/c/bin
   /source/code/location/rapid/c/lib
   /source/code/location/rapid/c/include
   /source/code/location/rapid/c/atlas/lib
   /source/code/location/rapid/c/atlas/include
   /source/code/location/rapid/c/common/fftw/lib
   /source/code/location/rapid/c/common/fftw/include
  

  
Building C code on Linux machine
========================

The script to build on a Linux machine the C software system for the RAPID pipeline is

  /source/code/location/rapid/c/builds/build.csh

It is assumed the atlas library is located in

  /usr/lib64/atlas

Furthermore, it is assumed gfortran is in the PATH.
  
1. Modify the following line in the build script to configure build environment, setting the absolute path of the rapid git repo.  E.g.,

  setenv RAPID_SW /source/code/location/rapid

2. Run the build script.

.. code-block::
   
   cd /source/code/location/rapid/c/builds
   ./build.csh >& build.out &

The binary executables, libraries, and include file are
installed under the following paths:

.. code-block::
   
   /source/code/location/rapid/c/bin
   /source/code/location/rapid/c/lib
   /source/code/location/rapid/c/include
   /source/code/location/rapid/c/common/fftw/lib
   /source/code/location/rapid/c/common/fftw/include
