#! /bin/csh

#
# Script to build the C software system for RAPID.
#
# Russ Laher (2/14/24)

#--------Configure build environment--------------------------
setenv RAPID_SW /users/rlaher/git/rapid
# A well-defined path is essential.
setenv PATH /usr/local/bin:/usr/bin:/usr/sbin

#--------Remove old and make new delivery directories--------------------------
cd ${RAPID_SW}/c
rm -rf bin
rm -rf lib
rm -rf include
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

#--------No need to build atlas library (already at /usr/lib64/atlas)---------------------

#--------Build sextractor binary---------------------
echo " "     
echo "--->Building sextractor, version 2.25.0 ..."
cd ${RAPID_SW}/c/common/sextractor
rm -rf sextractor-2.25.0
tar -xvf sextractor-2.25.0.tar
cd sextractor-2.25.0
./autogen.sh
./configure CPPFLAGS=-I${RAPID_SW}/c/common/fftw/include LDFLAGS=-L${RAPID_SW}/c/common/fftw/lib --enable-static --prefix=${RAPID_SW}/c --with-fftw-libdir=${RAPID_SW}/c/common/fftw/lib --with-fftw-incdir=${RAPID_SW}/c/common/fftw/include --with-atlas-libdir=/usr/lib64/atlas --with-atlas-incdir=/usr/include --enable-threads=8
make
make install 
echo " "     
echo "--->Finished building sextractor."
