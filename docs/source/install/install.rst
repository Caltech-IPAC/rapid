Installing RAPID Pipeline
####################################################

Download the source code
************************************

.. code-block::

   cd /source-code/location
   git clone https://github.com/Caltech-IPAC/rapid


The C code in this git repo must be built, in order to run the RAPID
pipeline.  Depending on whether the build is on a Mac laptop, a
Linux machine, or inside a Docker container on a Linux machine,
there are separate build scripts referred to below.

The build commands below can be repeated safely as the build scripts
remove prior build/install files before proceeding.

A build can take as little as 15 minutes, with most of that time spent on the GSL and
FFTW libraries.

Building C code on Mac laptop
************************************

The script to build on a Mac laptop the C software system for the RAPID pipeline is

.. code-block::

   /source-code/location/rapid/c/builds/build_laptop.csh

1. Prerequisites for the build script (you may need to install brew on your Mac laptop):

.. code-block::

   brew install gfortran
   brew install autoconf
   brew install automake
   brew install libtool
   brew install openblas

2. Modify the following line in the build script to configure the environment within the script,
   setting the absolute path of the rapid git repo:

.. code-block::

   setenv RAPID_SW /source-code/location/rapid

3. Modify the following line in the build script to configure the PATH
   environment variable within the script, ensuring that all paths to
   commands like ``make``, ``gcc``, ``ls``, ``rm``, ``gfortran``, ``autoconf``, ``automake``, ``libtool``, etc. are accessible:

.. code-block::

   setenv PATH /opt/homebrew/bin:/bin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/opt/X11/bin

   You may also have to make the following symlink if the build script complains
   that it cannot find libtoolize:

.. code-block::

   sudo ln -s /opt/homebrew/bin/glibtoolize /opt/homebrew/bin/libtoolize

4. Run the build script:

.. code-block::

   cd /source-code/location/rapid/c/builds
   ./build_laptop.csh >& build_laptop.out &

The script may take some time to finish (minutes or hours depending on the Mac laptop).

The binary executables, libraries, and include files are
installed under the following paths:

.. code-block::

   /source-code/location/rapid/c/bin
   /source-code/location/rapid/c/lib
   /source-code/location/rapid/c/include
   /source-code/location/rapid/c/atlas/lib
   /source-code/location/rapid/c/atlas/include
   /source-code/location/rapid/c/common/fftw/lib
   /source-code/location/rapid/c/common/fftw/include


   To run a binary executable, the run-time environment must be set up with the library path,
   as follows:

.. code-block::

   export DYLD_LIBRARY_PATH=/Users/laher/git/rapid/c/lib

.. warning::

    ``SExtractor`` is built from the source code in this build script.  If it fails,
    an alternate, easier method is to simply

    .. code-block::

        brew install sex

.. note::
    This build script worked successfully on a Mac laptop running macOS Monterey
    with a 2.9 GHz Dual-Core Intel Core i5 processor in a previous revision where
    the ``atlas`` library was required (commit 6ff4b9a2c8f796695bd9a6f7230defd85fbd32d7).
    It was recently tested on a Mac laptop with M3 Max chip running macOS Sequoia 15.6.1,
    and all binary executables were successfully built. (The atlas library failed to build, but the
    current revision of this build script uses the ``openblas`` library instead.  The build
    commands for the ``atlas`` library are retained in the script because it may work on some
    laptops, and it is good to keep options open.).


Building C code on Linux machine
************************************

The script to build on a Linux machine the C software system for the RAPID pipeline is

.. code-block::

   /source-code/location/rapid/c/builds/build.csh

It is assumed the atlas library is located in

.. code-block::

   /usr/lib64/atlas

Furthermore, it is assumed gfortran is in the PATH.

1. Modify the following line in the build script to configure the environment within the script, setting the absolute path of the rapid git repo:

.. code-block::

   setenv RAPID_SW /source-code/location/rapid

2. Run the build script:

.. code-block::

   cd /source-code/location/rapid/c/builds
   ./build.csh >& build.out &

The binary executables, libraries, and include files are
installed under the following paths:

.. code-block::

   /source-code/location/rapid/c/bin
   /source-code/location/rapid/c/lib
   /source-code/location/rapid/c/include
   /source-code/location/rapid/c/common/fftw/lib
   /source-code/location/rapid/c/common/fftw/include

Building C code on EC2 instance inside Docker container
************************************

The script to build inside a Docker container the C software system for the RAPID pipeline is

.. code-block::

   /source-code/location/rapid/c/builds/build_inside_container.sh

This script has preconfigured RAPID_SW and PATH environment
variables.  The former is tied directly to how the docker container is
launched, as shown in the instructions below, and the latter is tied
to how the infrastructure software in
RAPID project's Docker image has been pre-installed.

1. Install ``docker`` and create Docker image if not already done
   (otherwise, skip to step 2):

   * How to :doc:`install Docker on EC2 instance </install/docker>`

   * How to :doc:`create Docker image </install/docker_image>`

2. Ssh into the EC2 instance, and launch the Docker container with the
   following commands:

.. code-block::

   ssh -i ~/.ssh/MyKey.pem ubuntu@ubuntu@ec2-34-219-130-182.us-west-2.compute.amazonaws.com
   sudo docker run -it -v /source-code/location/rapid:/code rapid:1.0 bash

In this case, the rapid:1.0 Docker image is run.

The C-code-build location is embedded in the source-code location, as
documented below.  The source-code location is
mapped from a location outside the container to inside the container
in the ``docker run -v`` command option.
Therefore, the C-code build only needs to be done once, and this will
be persisted even after exiting the container.

3. Run the build script inside the container:

.. code-block::

   cd /code/c/builds
   ./build_inside_container.sh >& build_inside_container.out &

   tail -f build_inside_container.out

The binary executables, libraries, and include files are
installed under the following paths inside the container:

.. code-block::

   /code/c/bin
   /code/c/lib
   /code/c/include
   /code/c/common/fftw/lib
   /code/c/common/fftw/include
   /code/c/common/wcstools/wcstools-3.9.7/bin
   /code/c/common/wcstools/wcstools-3.9.7/libwcs

Here are listings:

.. code-block::

   # ls /code/c/bin
   HPXcvt	awaicgen  fitshdr  fitsverify  fpack  funpack  generateSmoothLampPattern  gsl-config  gsl-histogram  gsl-randist  hdrupdate  imcopy  imheaders	ldactoasc  makeTestFitsFile  sex  sundazel  swarp  tofits  verifyHduSums  wcsware
   # ls /code/c/lib
   libcfitsio.a   libcfitsio.so.10        libgsl.a   libgsl.so	libgsl.so.23.1.0  libgslcblas.la  libgslcblas.so.0	libnan.a   libnumericalrecipes.a   libwcs-8.2.2.a  libwcs.so	libwcs.so.8.2.2
   libcfitsio.so  libcfitsio.so.10.4.3.1  libgsl.la  libgsl.so.23	libgslcblas.a	  libgslcblas.so  libgslcblas.so.0.0.0	libnan.so  libnumericalrecipes.so  libwcs.a	   libwcs.so.8	pkgconfig
   ls /code/c/include/
   cfitsio  gsl  nan  numericalrecipes  wcslib  wcslib-8.2.2
   # ls /code/c/common/fftw/lib
   cmake  libfftw3f.a  libfftw3f.la  libfftw3f_threads.a  libfftw3f_threads.la  pkgconfig
   # ls /code/c/common/fftw/include
   fftw3.f  fftw3.f03  fftw3.h  fftw3l.f03  fftw3q.f03

The binary executatables and libraries therein cannot be executed
outside the container even though they are visible outside.

The wcslib library located in /code/c/lib and /code/c/include is that
of Mark M. R. Calabretta (`URL <https://www.atnf.csiro.au/people/mcalabre/WCS/>`_).

The WCS tools of Jessica Mink also has a libwcs.a (located in /code/c/common/wcstools/wcstools-3.9.7/libwcs), which may be a
different version (`URL <http://tdc-www.harvard.edu/wcstools/>`_).

To run a binary executable, you must first set LD_LIBRARY_PATH.  Here
is an example of running ``awaicgen`` without command-line options to
get its online tutorial:

.. code-block::

   export LD_LIBRARY_PATH=/code/c/lib
   /code/c/bin/awaicgen

.. include:: awaicgen_tutorial.txt
   :literal:
