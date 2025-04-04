#! /bin/csh

#
# Script to build the C software system for RAPID.
#
# Russ Laher (2/14/24)
#
# Prerequisite for atlas-library build: brew install gfortran
#

#--------Configure build environment--------------------------
setenv RAPID_SW /Users/laher/git/rapid
# A well-defined path is essential.
setenv PATH /bin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/opt/X11/bin

#--------Remove old and make new delivery directories--------------------------
cd ${RAPID_SW}/c
rm -rf bin
rm -rf lib
rm -rf include
rm -rf atlas
mkdir bin
mkdir lib
mkdir include
mkdir -p include/cfitsio
mkdir -p include/nan
mkdir -p include/numericalrecipes

#--------Build GSL library---------------------
echo " "
echo "--->Building GSL 2.5 library ..."
cd ${RAPID_SW}/c/common/gsl
rm -rf gsl-2.5
tar -xvf gsl-2.5.tar
cd gsl-2.5
./configure --prefix=${RAPID_SW}/c
make
make check
make install
echo " "
echo "--->Finished building GSL 2.5 library."

#--------Build cfitsio library-------------------------------
echo " "
echo "--->Building CFITSIO library, vsn 4.3.1 ..."
cd ${RAPID_SW}/c/common/cfitsio/
rm -rf cfitsio-4.3.1
tar -xvf cfitsio-4.3.1.tar
cd cfitsio-4.3.1
./configure --prefix=${RAPID_SW}/c/common/cfitsio/cfitsio-4.3.1
make shared
make install
make imcopy
make fpack
make funpack
echo " "
echo "--->Done with CFITSIO-library make install ..."
mkdir -p ${RAPID_SW}/c/include/cfitsio
cp ${RAPID_SW}/c/common/cfitsio/cfitsio-4.3.1/lib/libcfits* ${RAPID_SW}/c/lib
cp ${RAPID_SW}/c/common/cfitsio/cfitsio-4.3.1/include/*.h ${RAPID_SW}/c/include/cfitsio
cp ${RAPID_SW}/c/common/cfitsio/cfitsio-4.3.1/imcopy ${RAPID_SW}/c/bin
cp ${RAPID_SW}/c/common/cfitsio/cfitsio-4.3.1/fpack ${RAPID_SW}/c/bin
cp ${RAPID_SW}/c/common/cfitsio/cfitsio-4.3.1/funpack ${RAPID_SW}/c/bin
echo " "
echo "--->Finished building CFITSIO library."

#--------Build wcslib library---------------------
echo " "
echo "--->Building wcslib library, version 8.2.2 ..."
cd ${RAPID_SW}/c/common/wcslib
rm -rf wcslib-8.2.2
tar -xvf wcslib-8.2.2.tar
cd wcslib-8.2.2
./configure --prefix=${RAPID_SW}/c  --with-cfitsiolib=${RAPID_SW}/c/lib --with-cfitsioinc=${RAPID_SW}/c/include/cfitsio
make
make install
echo " "
echo "--->Finished building wcslib library."

#--------Build wcstools---------------------
echo " "
echo "--->Building wcstools, version 3.9.7 ..."
cd ${RAPID_SW}/c/common/wcstools
rm -rf wcstools-3.9.7
tar -xvf wcstools-3.9.7.tar
cd wcstools-3.9.7
make all
echo " "
echo "--->Finished building wcstools."

#--------Build awaicgen binary---------------------
echo " "
echo "--->Building awaicgen, version 5.2 ..."
cd ${RAPID_SW}/c/common/awaicgen
rm -rf awaicgen-5.2
tar -xvf awaicgen-5.2.tar
cd ${RAPID_SW}/c/common/awaicgen/awaicgen-5.2/libtwoplane
make
cd ${RAPID_SW}/c/common/awaicgen/awaicgen-5.2
make
cp -p awaicgen ${RAPID_SW}/c/bin
echo " "
echo "--->Finished building awaicgen."

#--------Build swarp binary---------------------
echo " "
echo "--->Building swarp, version 2.41.5 ..."
cd ${RAPID_SW}/c/common/swarp
rm -rf swarp-2.41.5
tar -xvf swarp-2.41.5.tar
cd swarp-2.41.5
./autogen.sh
./configure --prefix=${RAPID_SW}/c --enable-threads=8  --with-cfitsio-libdir=${RAPID_SW}/c/lib --with-cfitsio-incdir=${RAPID_SW}/c/include/cfitsio
make
make install
echo " "
echo "--->Finished building swarp."

#--------Build fitsverify module-------------------
echo " "
echo "--->Building fitsverify module ..."
cd ${RAPID_SW}/c/common/fitsverify
rm -rf fitsverify-4.22
tar xvf fitsverify-4.22.tar
cp Makefile fitsverify-4.22
cd fitsverify-4.22
make
echo " "
echo "--->Finished building fitsverify module."

#--------Build nan library-------------------
echo " "
echo "--->Building nan library ..."
cd ${RAPID_SW}/c/common/nan
make clean
make
echo " "
echo "--->Finished building nan library."

#--------Build numericalrecipes library-------------------
echo " "
echo "--->Building numericalrecipes library ..."
cd ${RAPID_SW}/c/common/numericalrecipes
make clean
make
echo " "
echo "--->Finished building numericalrecipes library."

#--------Build verifyHduSums module-------------------
echo " "
echo "--->Building verifyHduSums module ..."
cd ${RAPID_SW}/c/src/verifyHduSums
make clean
make
echo " "
echo "--->Finished building verifyHduSums module."

#--------Build imheaders module-------------------
echo " "
echo "--->Building imheaders module ..."
cd ${RAPID_SW}/c/src/imheaders
make clean
make
echo " "
echo "--->Finished building imheaders module."

#--------Build hdrupdate module-------------------
echo " "
echo "--->Building hdrupdate module ..."
cd ${RAPID_SW}/c/src/hdrupdate
make clean
make
echo " "
echo "--->Finished building hdrupdate module."

#--------Build generateSmoothLampPattern module-------------------
echo " "
echo "--->Building generateSmoothLampPattern module ..."
cd ${RAPID_SW}/c/src/generateSmoothLampPattern
make clean
make
echo " "
echo "--->Finished building generateSmoothLampPattern module."

#--------Build makeTestFitsFile module-------------------
echo " "
echo "--->Building makeTestFitsFile module ..."
cd ${RAPID_SW}/c/src/makeTestFitsFile
make clean
make
echo " "
echo "--->Finished building makeTestFitsFile module."

#--------Build fftw library-------------------
echo " "
echo "--->Building fftw library ..."
cd ${RAPID_SW}/c/common/fftw
rm -rf bin lib include share
rm -rf fftw-3.3.10
tar -xvf fftw-3.3.10.tar
cd fftw-3.3.10
./configure --prefix=$RAPID_SW/c/common/fftw CC=gcc CCC=g++ CFLAGS=-O2 --disable-fortran --build=x86_64-linux --enable-threads --enable-float
make
make check
make install
echo " "
echo "--->Finished building fftw library."

#--------Build atlas library---------------------
echo " "
echo "--->Building sextractor, version 3.10.3 ..."
cd ${RAPID_SW}/c/common/atlas
rm -rf ATLAS3.10.3
bunzip2 -c atlas3.10.3.tar.bz2 | tar xfm -
mv ATLAS ATLAS3.10.3
cd ATLAS3.10.3
mkdir build
cd build
../configure -b 64 --prefix=$RAPID_SW/c/atlas --with-netlib-lapack-tarfile=$RAPID_SW/c/common/atlas/lapack-3.12.0.tar.gz
make build
make check
make ptcheck
make time
make install
echo " "
echo "--->Finished building atlas library."

#--------Build sextractor binary---------------------
echo " "
echo "--->Building sextractor, version 2.25.0 ..."
cd ${RAPID_SW}/c/common/sextractor
rm -rf sextractor-2.25.0
tar -xvf sextractor-2.25.0.tar
cd sextractor-2.25.0
./autogen.sh
./configure CPPFLAGS=-I${RAPID_SW}/c/common/fftw/include:${RAPID_SW}/c/atlas/include LDFLAGS=-L${RAPID_SW}/c/common/fftw/lib:${RAPID_SW}/c/atlas/lib --enable-static --prefix=${RAPID_SW}/c --with-fftw-libdir=${RAPID_SW}/c/common/fftw/lib --with-fftw-incdir=${RAPID_SW}/c/common/fftw/include --with-atlas-libdir=${RAPID_SW}/c/atlas/lib --with-atlas-incdir=${RAPID_SW}/c/atlas/include --enable-threads=4
cp -p ../configure_hacked configure
./configure CPPFLAGS=-I${RAPID_SW}/c/common/fftw/include:${RAPID_SW}/c/atlas/include LDFLAGS=-L${RAPID_SW}/c/common/fftw/lib:${RAPID_SW}/c/atlas/lib --enable-static --prefix=${RAPID_SW}/c --with-fftw-libdir=${RAPID_SW}/c/common/fftw/lib --with-fftw-incdir=${RAPID_SW}/c/common/fftw/include --with-atlas-libdir=${RAPID_SW}/c/atlas/lib --with-atlas-incdir=${RAPID_SW}/c/atlas/include --enable-threads=4
make
make install
echo " "
echo "--->Finished building sextractor."

#--------Build computeOverlapArea module-------------------
echo " "
echo "--->Building computeOverlapArea module ..."
cd ${RAPID_SW}/c/src/computeOverlapArea
make clean
make
echo " "
echo "--->Finished building computeOverlapArea module."
