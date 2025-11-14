PhotUtils DAOStarFinder Parameter Study
####################################################


Overview
************************************

The analysis described below is for the purpose of understanding
the effects of varying PhotUtils DAOStarFinder input parameters.
This is a systematic study with 1000 independent samples as input.
These results can be compared with the three SExtractor input configurations that are documented below.


Input Difference Image
************************************

One thousand sets of ZOGY difference-image products are used.
Here is an example of how to download the input files needed for a single sample:

.. code-block::

    aws s3 cp s3://rapid-product-files/20250927/jid90828/bkg_subbed_science_image.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/diffimage_masked.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/scorrimage_masked.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/diffimage_uncert_masked.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/diffpsf.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/Roman_TDS_simple_model_Y106_124_5_lite_inject.txt .

The Python script ``scripts/download_files.py`` was used to do the bulk downloading (see next section).

Analysis Software
************************************

The following Python scripts are used to download the input data,
regenerate the catalogs for the various input configurations,
and make plots (offline, on a laptop):

.. code-block::

    scripts/download_files.py
    scripts/generate_sexcat.py
    scripts/generate_psfcat.py
    scripts/plot_detections.py
    scripts/generate_sexcats_with_custom_config.py
    scripts/generate_psfcats_for_many_cases.py
    scripts/breakdown_by_filter_cats_for_many_cases.py


SExtractor Baseline for Comparison
************************************

Three SExtractor configurations were tested.

The first SExtractor configuration below is similar to ZTF.  The others were determined by Alice Ciobanu and Lynn Yan in experiments with OpenUniverse simulated images.

===============      ===================      ===================      ======================      ==================================================================
Configuraton              ZTF                      AL1                      A2                     Description
===============      ===================      ===================      ======================      ==================================================================
DEBLEND_NTHRESH           4                        4                        4                      Number of deblending sub-thresholds
DEBLEND_MINCONT           0.005                    0.005                    0.005                  Minimum contrast parameter for deblending
DETECT_MINAREA            1                        4                        5                      Minimum number of pixels above threshold
DETECT_THRESH             5.0                      2.5                      2.5                    Detection threshold in **absolute DN** if weight image not used
ANALYSIS_THRESH           5.0                      2.5                      2.5                    Analysis threshold in number of sigmas
WEIGHT_TYPE            "NONE,MAP_RMS"           "NONE,MAP_RMS"          "BACKGROUND,MAP_RMS"       Do not use weight image for detection
FILTER                 "N"                      "N"                     "N"                        Do not apply filter for detection
===============      ===================      ===================      ======================      ==================================================================

The ZOGY scorr image is used for detection, and the difference image for analysis.

Fake sources were injected into the input image before ZOGY.  100 fake sources were injected.
In matching within 1.5 pixels for the SExtractor ZTF baseline,
there were on average 65.30 matches between extracted source positions and fake source positions.

Statistical results over all filters or WFI bands:

================================= ======================== ======================== ======================== ===============================================================================
Statistic                         ZTF                      AL1                      A2                       Description
================================= ======================== ======================== ======================== ===============================================================================
sample_size                       1000                     1000                     1000                     Number of ZOGY difference-image cases studied
avg_numpy_nsources_sexcat         1590.26                  2799.98                  1835.53                  Average number of SExtractor sources analyzed
std_numpy_nsources_sexcat         584.70                   1531.22                  942.60                   Standard deviation of corresponding average
margin_of_error_nsources_sexcat   36.24                    94.91                    58.42                    Uncertainty of corresponding average (95% confidence level)
avg_numpy_ns_true                 65.30                    63.42                    61.51                    Average number of catalog matches with fake-source positions (1.5-pixel radius)
std_numpy_ns_true=                9.29                     10.20                    11.34                    Standard deviation of corresponding average
margin_of_error_ns_true           0.58                     0.63                     0.70                     Uncertainty of corresponding average (95% confidence level)
================================= ======================== ======================== ======================== ===============================================================================


.. note::
    The ``XWIN_IMAGE, YWIN_IMAGE`` pixel coordinates are one-based indices, while the pixel coordinates
    of the fake-source truth list and PhotUtils PSF-fit catalog are zero-based indices.


PhotUtils DAOStarFinder Input-Parameter Variation
************************************

In all ten cases below, the input threshold is 5 times the clipped standard deviation
of the ZOGY difference image (multiplied by a Gaussian correction factor to account for the data clipping)::

    threshold = 0.2488752235542349 DN/s for the aforementioned single sample

This is the same threshold sigma that was used in the 9/27/2025 test.

Case #1 defines the parameters that were used in the 9/27/2025 test.

Statistical results covering all filters or WFI bands, for sample size = 1000.  The same inputs were used as for the above SExtractor ZTF baseline.

===== ==== ======= ======= ======= ======= ======= =============================== ==========================================================
Cases fwhm sharplo sharphi roundlo roundhi min_sep num_sources (std,unc)           num_matches_with_fake_sources (std,unc)
===== ==== ======= ======= ======= ======= ======= =============================== ==========================================================
1     2.0  0.2     1.0     -1.0    1.0     0.0     1715.98 (821.23,50.90)          52.62 (14.80,0.92)
2     2.0  -1.0    10.0    -1.0    1.0     0.0     2307.33 (1030.44,63.87)         70.09 (7.32,0.45)
3     2.0  -1.0    10.0    -1.0    1.0     1.0     2567.87 (1153.88,71.52)         70.72 (7.31,0.45)
4     2.0  -1.0    10.0    -1.0    1.0     2.0     2307.33 (1030.48,63.87)         70.10 (7.32,0.45)
5     2.0  -1.0    1.0     -1.0    1.0     1.0     1924.32 (909.99,56.40)          53.40 (14.68,0.91)
6     2.0  -1.0    10.0    -2.0    2.0     1.0     3874.28 (1469.20,91.06)         74.34 (6.76,0.42)
7     1.4  0.2     1.0     -1.0    1.0     0.0     2178.58 (998.78,61.91)          67.52 (8.34,0.52)
8     1.4  -1.0    10.0    -1.0    1.0     0.0     2510.98 (1123.97,69.66)         70.12 (7.35,0.46)
9     1.0  0.2     1.0     -1.0    1.0     0.0     2357.50 (1066.53,66.10)         67.86 (7.72,0.48)
10    1.0  -1.0    10.0    -1.0    1.0     0.0     2516.48 (1131.74,70.15)         68.16 (7.72,0.48)
===== ==== ======= ======= ======= ======= ======= =============================== ==========================================================

The average results are each given with corresponding standard deviation and uncertainty (95% confidence level) in parentheses.

Case #6 gave the largest number of PhotoUtils PSF-fit catalog sources and also
the largest number of fake-source matches (74.34) within 1.5 pixels.


Results Broken Down By Filter
************************************

.. code-block::

    Statistical results for filter = F184:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [220, 220, 220, 220, 220, 220, 220, 220, 220, 220, 220, 220, 220]
    avg_numpy_nsources_cat=[2172.35909091 4527.5        2982.64090909 2715.89090909 3404.25909091
     3783.74090909 3404.2        3013.14090909 5073.45909091 3288.19090909
     3647.29090909 3438.59545455 3609.10909091]
    std_numpy_nsources_cat=[260.25220837 363.85890747 262.60487629 327.49009271 393.60512187
     452.68622603 393.445579   365.86723021 671.45297213 395.07161596
     436.17822349 423.83808548 440.46165545]
    margin_of_error_nsources_cat=[34.39055261 48.08147057 34.70144161 43.27557999 52.01222973 59.8193943
     51.99114723 48.34685671 88.72792629 52.20601691 57.63797446 56.00730947
     58.20400075]
    avg_numpy_nmatches_cat=[71.62727273 70.50909091 70.03181818 69.74090909 72.80454545 73.45
     72.80454545 70.38181818 75.91363636 72.49545455 72.95       71.26363636
     71.45      ]
    std_numpy_nmatches_cat=[5.71021269 5.98823916 5.94167763 5.51371506 5.42242627 5.37775299
     5.42242627 5.44890618 5.41435763 5.42678352 5.42320351 5.30724594
     5.33106249]
    margin_of_error_nmatches_cat=[0.75456562 0.79130492 0.78515214 0.7285998  0.71653661 0.71063334
     0.71653661 0.72003575 0.7154704  0.71711239 0.71663932 0.70131632
     0.70446351]

    Statistical results for filter = H158:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [285, 285, 285, 285, 285, 285, 285, 285, 285, 285, 285, 285, 285]
    avg_numpy_nsources_cat=[1884.65964912 2848.89473684 1878.39649123 2113.52280702 2925.27719298
     3274.44912281 2925.43157895 2381.24561404 4839.01403509 2750.74385965
     3220.4877193  3033.68421053 3259.84561404]
    std_numpy_nsources_cat=[266.13916677 319.22262397 274.52421487 332.20325158 423.47062742
     489.44168877 423.64494367 375.5226384  850.92492906 416.37773127
     479.84106728 468.82938796 505.91983696]
    margin_of_error_nsources_cat=[30.89885841 37.06186796 31.87236568 38.56892376 49.16510078 56.82436607
     49.18533896 43.59832103 98.79270765 48.34161284 55.70973027 54.43127012
     58.73748534]
    avg_numpy_nmatches_cat=[71.14385965 69.78596491 68.44561404 59.10175439 73.74736842 74.32631579
     73.75087719 59.81403509 77.36140351 72.21052632 73.60350877 71.51578947
     71.80701754]
    std_numpy_nmatches_cat=[5.11451384 5.36981291 5.42198482 5.70258455 5.09757787 5.11435977
     5.10015902 5.67021886 4.88383493 5.16172432 5.06248552 5.35844274
     5.37452926]
    margin_of_error_nmatches_cat=[0.593797   0.62343732 0.6294945  0.66207223 0.59183073 0.59377911
     0.5921304  0.65831457 0.56701509 0.59927816 0.58775649 0.62211724
     0.62398489]

    Statistical results for filter = J129:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [127, 127, 127, 127, 127, 127, 127, 127, 127, 127, 127, 127, 127]
    avg_numpy_nsources_cat=[1721.37795276 1937.09448819 1338.13385827 1638.48818898 2401.31496063
     2656.59055118 2401.21259843 1845.25984252 4262.52755906 2208.92125984
     2670.66141732 2481.81102362 2710.64566929]
    std_numpy_nsources_cat=[215.31993881 251.39479987 215.03419842 206.44191824 245.60796176
     281.97733629 245.44196453 227.84748361 615.08976141 248.85588931
     277.89300667 271.09735394 294.88982402]
    margin_of_error_nsources_cat=[ 37.44884766  43.72305517  37.39915116  35.90476567  42.71659743
      49.04202727  42.68772688  39.62766176 106.97756512  43.28148308
      48.331673    47.14976033  51.28779136]
    avg_numpy_nmatches_cat=[64.55905512 62.94488189 60.71653543 42.34645669 72.73228346 73.32283465
     72.73228346 43.1023622  76.86614173 68.88976378 72.53543307 70.35433071
     70.83464567]
    std_numpy_nmatches_cat=[6.08425559 6.62054436 6.68751056 5.43012587 5.75407944 5.78061094
     5.75407944 5.45682358 5.75943212 5.87162984 5.88042967 6.20962874
     6.18578989]
    margin_of_error_nmatches_cat=[1.05818514 1.15145749 1.16310438 0.94441768 1.00076029 1.0053747
     1.00076029 0.94906099 1.00169124 1.02120488 1.02273536 1.07999028
     1.07584418]

    Statistical results for filter = K213:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [85, 85, 85, 85, 85, 85, 85, 85, 85, 85, 85, 85, 85]
    avg_numpy_nsources_cat=[1057.36470588 5393.21176471 3156.15294118 1285.85882353 1426.30588235
     1596.76470588 1426.32941176 1448.71764706 2109.75294118 1350.2
     1440.65882353 1305.22352941 1368.65882353]
    std_numpy_nsources_cat=[148.25612552 313.73762339 205.53290882 217.52123104 249.30340709
     294.40486917 249.35301639 259.11235863 426.30703947 233.31875697
     257.11560929 235.77793024 253.74000371]
    margin_of_error_nsources_cat=[31.5180438  66.69806135 43.69462105 46.24324063 52.99987219 62.58807539
     53.01041872 55.08517533 90.62940162 49.60166587 54.6606827  50.12446606
     53.94305647]
    avg_numpy_nmatches_cat=[63.55294118 62.52941176 62.25882353 61.30588235 62.38823529 63.07058824
     62.38823529 62.05882353 65.65882353 61.84705882 62.24705882 58.84705882
     59.08235294]
    std_numpy_nmatches_cat=[5.62453843 5.12798145 5.31181356 5.30487795 5.2110609  5.27098213
     5.2110609  5.29228727 5.2592841  5.42869475 5.31790733 4.98351608
     4.91866018]
    margin_of_error_nmatches_cat=[1.19573102 1.09016706 1.12924826 1.12777381 1.10782907 1.12056783
     1.10782907 1.12509713 1.11808093 1.15409625 1.13054375 1.0594549
     1.04566706]

    Statistical results for filter = R062:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [80, 80, 80, 80, 80, 80, 80, 80, 80, 80, 80, 80, 80]
    avg_numpy_nsources_cat=[ 753.2375  920.3375  650.7     499.1     747.55    827.5875  747.5125
      573.3    1834.1125  695.3625  801.25    752.65    787.125 ]
    std_numpy_nsources_cat=[141.78480558 189.2972493  153.72917745  74.99943333 105.41796574
     119.02591039 105.32568463  88.50612973 391.44412097  99.59872536
     115.04004737 108.86873977 114.84700421]
    margin_of_error_nsources_cat=[31.06996542 41.4815887  33.68739131 16.43497546 23.10073027 26.08270262
     23.08050828 19.39476081 85.77897506 21.825533   25.20926187 23.85691447
     25.16695942]
    avg_numpy_nmatches_cat=[52.6375 49.625  45.8625 31.2875 63.1375 63.8    63.1375 32.3    69.9375
     58.1    63.     60.0875 60.35  ]
    std_numpy_nmatches_cat=[6.97359977 7.67361551 7.72454489 5.17733945 6.82045407 6.81432315
     6.82045407 5.17058991 5.93578923 6.5852107  6.72681202 6.53871882
     6.47900455]
    margin_of_error_nmatches_cat=[1.52815743 1.68155514 1.69271553 1.13453453 1.4945979  1.4932544
     1.4945979  1.13305547 1.30073716 1.44304792 1.47407768 1.43285994
     1.41977447]

    Statistical results for filter = Y106:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [81, 81, 81, 81, 81, 81, 81, 81, 81, 81, 81, 81, 81]
    avg_numpy_nsources_cat=[1591.96296296 1572.96296296 1122.37037037 1252.97530864 1860.35802469
     2037.60493827 1860.2962963  1401.7037037  3640.16049383 1723.60493827
     2060.38271605 1944.87654321 2097.2962963 ]
    std_numpy_nsources_cat=[210.93416236 220.73471282 195.20615531 162.75164563 204.2508932
     226.85148669 204.42236864 177.52485094 560.69028128 204.71091807
     230.21211732 221.85694327 239.87082143]
    margin_of_error_nsources_cat=[ 45.93677314  48.07111524  42.51156271  35.44369172  44.48130563
      49.40321266  44.51864917  38.66096754 122.10588348  44.58148882
      50.13508333  48.31551209  52.23853444]
    avg_numpy_nmatches_cat=[60.98765432 58.37037037 56.01234568 39.87654321 72.37037037 73.02469136
     72.37037037 40.75308642 77.35802469 67.9382716  72.75308642 71.16049383
     71.51851852]
    std_numpy_nmatches_cat=[6.42236414 7.06835138 6.67683267 4.98734873 5.17697763 5.23986455
     5.17697763 4.94795347 5.13114105 5.31558547 5.11239323 5.4193453
     5.47522165]
    margin_of_error_nmatches_cat=[1.39864819 1.53932986 1.45406578 1.08613372 1.12743068 1.14112606
     1.12743068 1.07755431 1.11744849 1.15761639 1.11336564 1.18021298
     1.1923816 ]

    Statistical results for filter = Z087:
    case_list = ['ZTF', 'AL1', 'A2', 'PU1', 'PU2', 'PU3', 'PU4', 'PU5', 'PU6', 'PU7', 'PU8', 'PU9', 'PU10']
    sample_size_list = [122, 122, 122, 122, 122, 122, 122, 122, 122, 122, 122, 122, 122]
    avg_numpy_nsources_cat=[ 635.31967213  709.17213115  514.93442623  469.86885246  721.21311475
      802.13934426  721.1147541   540.04918033 1776.64754098  661.25409836
      804.18852459  758.40983607  819.5       ]
    std_numpy_nsources_cat=[ 88.41910876 126.29121256 109.39736096  69.49737002  91.0327116
      99.29780639  91.01379797  75.66728861 315.49534101  84.97931874
      99.10793354  90.63064604 100.84903911]
    margin_of_error_nsources_cat=[15.68997635 22.41038353 19.41256851 12.33231264 16.15376034 17.62040192
     16.15040412 13.42716507 55.9846679  15.0795854  17.58670898 16.08241378
     17.89566827]
    avg_numpy_nmatches_cat=[53.39344262 49.2704918  44.12295082 33.70491803 62.3442623  63.
     62.3442623  34.72131148 68.76229508 55.98360656 62.76229508 59.7704918
     60.17213115]
    std_numpy_nmatches_cat=[7.40796799 7.58453656 7.64754098 5.18618677 5.95993521 5.98632321
     5.95993521 5.36879615 6.02052947 6.25546791 6.59241153 6.20389471
     6.21696861]
    margin_of_error_nmatches_cat=[1.31454438 1.34587648 1.35705662 0.92028916 1.05759087 1.06227343
     1.05759087 0.95269321 1.06834333 1.11003317 1.16982384 1.1008815
     1.10320147]

    Statistical results for filter = W146:
    No data for filter...

Plots
************************************

Plots for the SExtractor ZTF baseline versus the ten PhotUtils cases are given below for the aforementioned single sample,
and a match radius of 1.5 pixels.

.. image:: sex_vs_psf_fwhm=2.0_sharplo=0.2_sharphi=1.0_roundlo=-1.0_roundhi=1.0_min_sep=0.0.png
.. image:: sex_vs_psf_fwhm=2.0_sharplo=-1.0_sharphi=10.0_roundlo=-1.0_roundhi=1.0_min_sep=0.0.png
.. image:: sex_vs_psf_fwhm=2.0_sharplo=-1.0_sharphi=10.0_roundlo=-1.0_roundhi=1.0_min_sep=1.0.png
.. image:: sex_vs_psf_fwhm=2.0_sharplo=-1.0_sharphi=10.0_roundlo=-1.0_roundhi=1.0_min_sep=2.0.png
.. image:: sex_vs_psf_fwhm=2.0_sharplo=-1.0_sharphi=1.0_roundlo=-1.0_roundhi=1.0_min_sep=1.0.png
.. image:: sex_vs_psf_fwhm=2.0_sharplo=-1.0_sharphi=10.0_roundlo=-2.0_roundhi=2.0_min_sep=1.0.png
.. image:: sex_vs_psf_fwhm=1.4_sharplo=0.2_sharphi=1.0_roundlo=-1.0_roundhi=1.0_min_sep=0.0.png
.. image:: sex_vs_psf_fwhm=1.4_sharplo=-1.0_sharphi=10.0_roundlo=-1.0_roundhi=1.0_min_sep=0.0.png
.. image:: sex_vs_psf_fwhm=1.0_sharplo=0.2_sharphi=1.0_roundlo=-1.0_roundhi=1.0_min_sep=0.0.png
.. image:: sex_vs_psf_fwhm=1.0_sharplo=-1.0_sharphi=10.0_roundlo=-1.0_roundhi=1.0_min_sep=0.0.png
