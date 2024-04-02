Download the source code
====================

.. code-block::

   cd /source/code/location
   git clone https://github.com/Caltech-IPAC/rapid


The C code in this git repo must be built, in order to run the RAPID
pipeline.  Depending on whether the build is on a Mac laptop or a
Linux machine, there are separate build scripts referred to below.


Building C code on Mac laptop
========================


The script to build on a Mac laptop the C software system for the RAPID pipeline is

.. code-block::

   /source/code/location/rapid/c/builds/build_laptop.csh

This script is has been tested on a Mac laptop running macOS Montery.
  
1. Prerequisite for the atlas-library build in the build script:

.. code-block::

   brew install gfortran

2. Modify the following line in the build script to configure the environment within the script, setting the absolute path of the rapid git repo:

.. code-block::

   setenv RAPID_SW /source/code/location/rapid

3. Run the build script:

.. code-block::
   
   cd /source/code/location/rapid/c/builds
   ./build_laptop.csh >& build_laptop.out &

The script may take some time to finish as building the atlas library
(perhaps 12 hours or more), which is needed by sextractor, is part of the process.

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
  
.. warning::
    The sextractor configure script made by autogen.sh in the build
    script initially did
    not work on the Mac laptop used to test the build script.  To fix
    the problem, a hacked version of the sextractor configure script
    is copied into the sextractor build directory and rerun as part of
    the build process.

    Users may wish to comment out this portion in the sextractor
    section of the build script in order to experiment with whether
    the problem is indeed experienced particularly on their Mac laptop.

  
Building C code on Linux machine
========================

The script to build on a Linux machine the C software system for the RAPID pipeline is

.. code-block::

   /source/code/location/rapid/c/builds/build.csh

It is assumed the atlas library is located in

.. code-block::

   /usr/lib64/atlas

Furthermore, it is assumed gfortran is in the PATH.
  
1. Modify the following line in the build script to configure the environment within the script, setting the absolute path of the rapid git repo:

.. code-block::

   setenv RAPID_SW /source/code/location/rapid

2. Run the build script:

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
