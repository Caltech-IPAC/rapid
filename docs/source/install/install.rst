Download the source code
####################################################

.. code-block::

   cd /source/code/location
   git clone https://github.com/Caltech-IPAC/rapid


The C code in this git repo must be built, in order to run the RAPID
pipeline.  Depending on whether the build is on a Mac laptop, a
Linux machine, or inside a Docker container on a Linux machine,
there are separate build scripts referred to below.

The build commands below can be repeated safely as the build scripts
remove prior build/install files before proceeding.

Building C code on Mac laptop
####################################################


The script to build on a Mac laptop the C software system for the RAPID pipeline is

.. code-block::

   /source/code/location/rapid/c/builds/build_laptop.csh

This script is has been tested on a Mac laptop running macOS Montery.
  
1. Prerequisites for the atlas-library build in the build script (you may need to install brew on your Mac laptop):

.. code-block::

   brew install gfortran
   brew install autoconf
   brew install automake
   brew install libtool

2. Modify the following line in the build script to configure the environment within the script, setting the absolute path of the rapid git repo:

.. code-block::

   setenv RAPID_SW /source/code/location/rapid

3. Modify the following line in the build script to configure the PATH
   environment variable within the script, ensuring that all paths to
   commands like ``make``, ``gcc``, ``ls``, ``rm``, ``gfortran``, ``autoconf``, ``automake``, ``libtool``, etc. are accessible:

.. code-block::

   setenv PATH /bin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/opt/X11/bin

4. Run the build script:

.. code-block::
   
   cd /source/code/location/rapid/c/builds
   ./build_laptop.csh >& build_laptop.out &

The script may take some time to finish as building the atlas library
(perhaps 12 hours or more), which is needed by sextractor, is part of the process.

The binary executables, libraries, and include files are
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

.. warning::
    This build script worked on a Mac laptop running macOS Monterey with a 2.9 GHz Dual-Core Intel Core i5 processor.
    It has not been fully tested for the new processor chips, like the Apple M3 Max.

Building C code on Linux machine
####################################################

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

The binary executables, libraries, and include files are
installed under the following paths:

.. code-block::
   
   /source/code/location/rapid/c/bin
   /source/code/location/rapid/c/lib
   /source/code/location/rapid/c/include
   /source/code/location/rapid/c/common/fftw/lib
   /source/code/location/rapid/c/common/fftw/include

Building C code on EC2 instance inside Docker container
####################################################

The script to build inside a Docker container the C software system for the RAPID pipeline is

.. code-block::

   /source/code/location/rapid/c/builds/build_inside_container.sh

This script has preconfigured RAPID_SW and PATH environment
variables.  The former is tied directly to how the docker container is
launched, as shown in the instructions below, and the latter is tied
to how the infrastructure software in 
RAPID project's Docker image has been pre-installed.

0. Install ``docker`` and create Docker image if not already done;
   otherwise, skip to next step:

.. toctree::
   :maxdepth: 3
   
   docker.rst
   docker_image.rst

1. Ssh into the EC2 instance, and launch the Docker container with the
   following commands:

.. code-block::

   ssh -i ~/.ssh/MyKey.pem ubuntu@ubuntu@ec2-34-219-130-182.us-west-2.compute.amazonaws.com
   sudo docker run -it -v /source/code/location/rapid:/code rapid:1.0 bash

In this case, the rapid:1.0 Docker image is run.

The C-code-build location is embedded in the source-code location, as
documented below.  The source-code location is
mapped from a location outside the container to inside the container
in the ``docker run -v`` command option.
Therefore, the C-code build only needs to be done once, and this will
be persisted even after exiting the container.

2. Run the build script inside the container:

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

Here are listings:

.. code-block::

   # ls /code/c/bin
   fitsverify  fpack  funpack  generateSmoothLampPattern  gsl-config  gsl-histogram  gsl-randist  hdrupdate  imcopy  imheaders  ldactoasc	makeTestFitsFile  sex  swarp  verifyHduSums
   # ls /code/c/lib
   libcfitsio.a  libcfitsio.so  libcfitsio.so.10  libcfitsio.so.10.4.3.1  libgsl.a  libgsl.la  libgsl.so  libgsl.so.23  libgsl.so.23.1.0  libgslcblas.a  libgslcblas.la  libgslcblas.so  libgslcblas.so.0	libgslcblas.so.0.0.0  libnan.a	libnan.so  libnumericalrecipes.a  libnumericalrecipes.so  pkgconfig
   # ls /code/c/common/fftw/lib
   cmake  libfftw3f.a  libfftw3f.la  libfftw3f_threads.a  libfftw3f_threads.la  pkgconfig
   # ls /code/c/common/fftw/include
   fftw3.f  fftw3.f03  fftw3.h  fftw3l.f03  fftw3q.f03

The binary executatables and libraries therein cannot be executed
outside the container even though they are visible outside.
