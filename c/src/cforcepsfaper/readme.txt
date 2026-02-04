10/17/22

export DYLD_LIBRARY_PATH=${ZTF_SW}/ztf/lib:/Users/laher/Files/laher/rlaher/git/ext/lib
export CFITSIO=/Users/laher/Files/laher/rlaher/git/ext

make clean
make
echo $?

cforcepsfaper

cforcepsfaper, v. 1.30, by Russ Laher

Usage:
-i <input list-of-images file>
-a <input list-of-alert-positions file>
-o <output lightcurve-data file>
-t <number of processing threads> (default = 1)
[-r (switch to read upsampled PSFs from ztf*rebinpsf.fits in current directory)]
[-v (verbose switch)]


Test case #1 commands:

make clean
make

time cforcepsfaper -i diffimglist.txt -a xy.txt -o lightcurve_c.dat -t 1 -v > cforcepsfaper.out
echo $?

real	0m0.174s
user	0m0.007s
sys	0m0.012s


Test case #1 details:

mkdir data
cd data

scp ztfrl@ztfadmin:/ztf/archive/sci/2022/1014/464190/ztf_20221014464190_000619_zg_c16_o_q4_scimrefdiffimg.fits.fz .
scp ztfrl@ztfadmin:/ztf/archive/sci/2022/1014/486528/ztf_20221014486528_000619_zr_c16_o_q4_scimrefdiffimg.fits.fz .
scp ztfrl@ztfadmin:/ztf/archive/sci/2022/1015/490532/ztf_20221015490532_000619_zi_c16_o_q4_scimrefdiffimg.fits.fz .

scp ztfrl@ztfadmin:/ztf/archive/sci/2022/1014/464190/ztf_20221014464190_000619_zg_c16_o_q4_diffimgpsf.fits .
scp ztfrl@ztfadmin:/ztf/archive/sci/2022/1014/486528/ztf_20221014486528_000619_zr_c16_o_q4_diffimgpsf.fits .
scp ztfrl@ztfadmin:/ztf/archive/sci/2022/1015/490532/ztf_20221015490532_000619_zi_c16_o_q4_diffimgpsf.fits .


Withi bilinear interpolation of the PSF...

mami:cforcepsfaper laher$ cat cforcepsfaper_thread_0.out
tnum, startc, endc = 0, 0, 0
c, k, xpos, ypos, orignbadpixels, origdmin, origdsum = 0, 0, 2750.268260, 651.705985, 0, -32.756161, 21141.570203
difpsffilename=data/ztf_20221014464190_000619_zg_c16_o_q4_diffimgpsf.fits
retfrominterp = 0
c, k, badpixels, dsum = 0, 0, 0, 528539.255071
retfromrecenter = 0
badpixels, badpixfrac, maxbadpixfrac = 0, 0.000000, 0.500000
c, k, psfsum = 0, 0, 0.002119
c, k, n, sigmadiff, median, pct50upsamp = 0, 0, 556, 10.957888, -0.945980, -0.037839
psffitphotom: c, k, forcediffimflux, forcediffimfluxunc, forcediffimfluxsnr, forcediffimfluxchisq, exitstatuseph[0] = 0, 0, 950.373931, 50.066528, 18.982222, 1.023106, 0

psffitphotom: c, k, forcediffimapflux, forcediffimapfluxunc, forcediffimapfluxsnr, forcediffimapfluxchisq = 0, 0, 1040.303310, 93.989037, 11.068347, 1.061616

c, k, xpos, ypos, orignbadpixels, origdmin, origdsum = 0, 1, 2759.206806, 653.561487, 0, -36.096172, 23020.669188
difpsffilename=data/ztf_20221014486528_000619_zr_c16_o_q4_diffimgpsf.fits
retfrominterp = 0
c, k, badpixels, dsum = 0, 1, 0, 575516.729710
retfromrecenter = 0
badpixels, badpixfrac, maxbadpixfrac = 0, 0.000000, 0.500000
c, k, psfsum = 0, 1, 0.002118
c, k, n, sigmadiff, median, pct50upsamp = 0, 1, 556, 9.034595, -0.045642, -0.001826
psffitphotom: c, k, forcediffimflux, forcediffimfluxunc, forcediffimfluxsnr, forcediffimfluxchisq, exitstatuseph[0] = 0, 1, 690.993776, 41.465430, 16.664334, 1.106360, 0

psffitphotom: c, k, forcediffimapflux, forcediffimapfluxunc, forcediffimapfluxsnr, forcediffimapfluxchisq = 0, 1, 689.685751, 77.830093, 8.861428, 1.065453

c, k, xpos, ypos, orignbadpixels, origdmin, origdsum = 0, 2, 2770.402705, 663.894070, 0, -39.409721, 24277.785848
difpsffilename=data/ztf_20221015490532_000619_zi_c16_o_q4_diffimgpsf.fits
retfrominterp = 0
c, k, badpixels, dsum = 0, 2, 0, 606944.646189
retfromrecenter = 0
badpixels, badpixfrac, maxbadpixfrac = 0, 0.000000, 0.500000
c, k, psfsum = 0, 2, 0.002304
c, k, n, sigmadiff, median, pct50upsamp = 0, 2, 556, 10.127603, -0.609133, -0.024365
psffitphotom: c, k, forcediffimflux, forcediffimfluxunc, forcediffimfluxsnr, forcediffimfluxchisq, exitstatuseph[0] = 0, 2, 220.749381, 42.849476, 5.151741, 0.984220, 0

psffitphotom: c, k, forcediffimapflux, forcediffimapfluxunc, forcediffimapfluxsnr, forcediffimapfluxchisq = 0, 2, 168.510679, 84.738040, 1.988607, 1.042107



 my $simcalpsffluxcor = 0.89897;
 my $simcalapfluxcor = 0.99331;

perl -e '$simcalpsffluxcor = 0.89897; $psfflux = $simcalpsffluxcor * 950.373931; print "psfflux = $psfflux\n";'

psfflux = 854.35765275107 (with bilinear interpolation)
-----------------------------------------------------------------------------------
Cf. 898.923592746838 from forcedphotometry_trim.pl (with Gaussian-basis-function interpolation)
898.923592746838 52.8057824757423 18.9363423932866 1.07120687474915
-----------------------------------------------------------------------------------

perl -e '$simcalapfluxcor = 0.99331; $apflux = $simcalapfluxcor * 1040.303310; print "apflux = $apflux\n";'

apflux = 1033.3436808561 (with bilinear interpolation)
-----------------------------------------------------------------------------------
Cf. 1030.82541724765 from forcedphotometry_trim.pl (with Gaussian-basis-function interpolation)
1030.82541724765 93.8166840267379 11.0616581315695 1.05902923117402
-----------------------------------------------------------------------------------






