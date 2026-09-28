Installing RAPID Pipeline
####################################################

Download the source code
************************************

.. code-block::

   cd /source-code/location
   git clone https://github.com/Caltech-IPAC/rapid


Build the C code in this git repo before running the RAPID pipeline.
Use the script below for a Mac laptop, a Linux machine, or a Docker
container on a Linux machine.

The build commands are safe to repeat: each script removes prior
build/install files before proceeding.

A build can take as little as 15 minutes, mostly spent on the GSL and
FFTW libraries.

Building C code on Mac laptop
************************************

The Mac laptop build script is:

.. code-block::

   /source-code/location/rapid/c/builds/build_laptop.csh

1. Install the prerequisites (you may need to install brew first):

.. code-block::

   brew install gfortran
   brew install autoconf
   brew install automake
   brew install libtool
   brew install openblas

2. Set the absolute path of the rapid git repo in the build script:

.. code-block::

   setenv RAPID_SW /source-code/location/rapid

3. Set PATH in the build script so commands such as ``make``, ``gcc``,
   ``ls``, ``rm``, ``gfortran``, ``autoconf``, ``automake`` and ``libtool``
   are accessible:

.. code-block::

   setenv PATH /opt/homebrew/bin:/bin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/opt/X11/bin

If the build script cannot find libtoolize, you may also need this symlink:

.. code-block::

   sudo ln -s /opt/homebrew/bin/glibtoolize /opt/homebrew/bin/libtoolize

4. Run the build script. It may take minutes or hours, depending on the
   Mac laptop:

.. code-block::

   cd /source-code/location/rapid/c/builds
   ./build_laptop.csh >& build_laptop.out &

The script installs binary executables, libraries and include files under:

.. code-block::

   /source-code/location/rapid/c/bin
   /source-code/location/rapid/c/lib
   /source-code/location/rapid/c/include
   /source-code/location/rapid/c/atlas/lib
   /source-code/location/rapid/c/atlas/include
   /source-code/location/rapid/c/common/fftw/lib
   /source-code/location/rapid/c/common/fftw/include


Before running a binary executable, set the run-time library path:

.. code-block::

   export DYLD_LIBRARY_PATH=/source-code/location/rapid/c/lib

.. warning::

    The script builds ``SExtractor`` from source. If that build fails,
    an easier alternative is:

    .. code-block::

        brew install sex

.. note::
    A previous revision requiring ``atlas`` (commit
    6ff4b9a2c8f796695bd9a6f7230defd85fbd32d7) worked on a Mac laptop
    running macOS Monterey with a 2.9 GHz Dual-Core Intel Core i5 processor.
    A recent test on a Mac laptop with an M3 Max chip running macOS Sequoia
    15.6.1 built all binary executables successfully. The atlas library
    failed to build, but the current script uses ``openblas`` instead.
    The ``atlas`` build commands remain as an option for laptops where
    the library may build successfully.


Building C code on Linux machine
************************************

The Linux build script is:

.. code-block::

   /source-code/location/rapid/c/builds/build.csh

The script assumes gfortran is in PATH and the atlas library is in:

.. code-block::

   /usr/lib64/atlas

1. Set the absolute path of the rapid git repo in the build script:

.. code-block::

   setenv RAPID_SW /source-code/location/rapid

2. Run the build script:

.. code-block::

   cd /source-code/location/rapid/c/builds
   ./build.csh >& build.out &

The script installs binary executables, libraries and include files under:

.. code-block::

   /source-code/location/rapid/c/bin
   /source-code/location/rapid/c/lib
   /source-code/location/rapid/c/include
   /source-code/location/rapid/c/common/fftw/lib
   /source-code/location/rapid/c/common/fftw/include

Building C code on EC2 instance inside Docker container
************************************

The Docker container build script is:

.. code-block::

   /source-code/location/rapid/c/builds/build_inside_container.sh

The script preconfigures RAPID_SW for the container launch shown below
and PATH for the infrastructure software pre-installed in the RAPID
project's Docker image.

1. Install ``docker`` and create the Docker image if not already done
   (otherwise, skip to step 2):

   * How to :doc:`install Docker on EC2 instance </install/docker>`

   * How to :doc:`create Docker image </install/docker_image>`

2. Ssh into the EC2 instance and launch the rapid:1.0 Docker image:

.. code-block::

   ssh -i ~/.ssh/MyKey.pem ubuntu@ubuntu@ec2-34-219-130-182.us-west-2.compute.amazonaws.com
   sudo docker run -it -v /source-code/location/rapid:/code rapid:1.0 bash

The C-code-build location is within the source-code location, as shown
below. The ``docker run -v`` option maps that location from outside the
container to inside it, so the build needs to run only once and persists
after the container exits. The binary executables and libraries are
visible outside the container but cannot be executed there.

3. Run the build script inside the container:

.. code-block::

   cd /code/c/builds
   ./build_inside_container.sh >& build_inside_container.out &

   tail -f build_inside_container.out

The script installs binary executables, libraries and include files under
these paths inside the container:

.. code-block::

   /code/c/bin
   /code/c/lib
   /code/c/include
   /code/c/common/fftw/lib
   /code/c/common/fftw/include
   /code/c/common/wcstools/wcstools-3.9.7/bin
   /code/c/common/wcstools/wcstools-3.9.7/libwcs

Directory listings:

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

The wcslib library in /code/c/lib and /code/c/include is from
Mark M. R. Calabretta (`URL <https://www.atnf.csiro.au/people/mcalabre/WCS/>`_).

Jessica Mink's WCS tools also provide libwcs.a, in
/code/c/common/wcstools/wcstools-3.9.7/libwcs, which may be a different
version (`URL <http://tdc-www.harvard.edu/wcstools/>`_).

Set LD_LIBRARY_PATH before running a binary executable. This example
runs ``awaicgen`` without command-line options to get its online tutorial:

.. code-block::

   export LD_LIBRARY_PATH=/code/c/lib
   /code/c/bin/awaicgen

.. include:: awaicgen_tutorial.txt
   :literal:
