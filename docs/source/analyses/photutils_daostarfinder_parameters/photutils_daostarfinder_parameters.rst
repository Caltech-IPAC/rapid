PhotUtils DAOStarFinder Parameter Study
####################################################


Overview
************************************

The analysis described below is for the purpose of understanding
the effects of varying PhotUtils DAOStarFinder input parameters.
This is a systematic study with 1000 independent samples as input.


Input Difference Image
************************************

One thousand sets of ZOGY difference-image products are used.  Here is an example of how to download the input files needed for a single sample:

.. code-block::

    aws s3 cp s3://rapid-product-files/20250927/jid90828/bkg_subbed_science_image.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/diffimage_masked.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/scorrimage_masked.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/diffimage_uncert_masked.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/diffpsf.fits .
    aws s3 cp s3://rapid-product-files/20250927/jid90828/Roman_TDS_simple_model_Y106_124_5_lite_inject.txt .

The Python script ``scripts/download_files.py`` was used to do the bulk downloading.

Analysis Software
************************************

The following scripts are used to download the input data, generate catalogs, and make plots (offline, on a laptop):

.. code-block::

    scripts/download_files.py
    scripts/generate_sexcat.py
    scripts/generate_psfcat.py
    scripts/plot_detections.py
    scripts/generate_sexcats_with_custom_config.py
    scripts/generate_psfcats_for_many_cases.py


SExtractor Baseline for Comparison
************************************

The SExtractor configuration is similar to ZTF:

===============      ===================      =====================================================================
Parameter                Value                 Description
===============      ===================      =====================================================================
DEBLEND_NTHRESH           4                    Number of deblending sub-thresholds
DEBLEND_MINCONT           0.005                Minimum contrast parameter for deblending
DETECT_MINAREA            1                    Minimum number of pixels above threshold
DETECT_THRESH             5.0                  Detection threshold in absolute DN (weight image not used; see below)
ANALYSIS_THRESH           5.0                  Analysis threshold in number of sigmas
WEIGHT_TYPE            "NONE,MAP_RMS"          Do not use weight image for detection
FILTER                 "N"                     Do not apply filter for detection
===============      ===================      =====================================================================

The ZOGY scorr image is used for detection, and the difference image for analysis.

Fake sources were injected into the input image before ZOGY.  100 fake sources were injected.
In matching within 1.5 pixels, there were on average 62.23 matches between SExtractor source positions and fake source positions.

Statistical results:

================================= ======================== ===============================================================================
Statistic                         Output value             Description
================================= ======================== ===============================================================================
sample_size                       1000                     Number of ZOGY difference-image cases studied
avg_numpy_nsources_sexcat         2011.55                  Average number of SExtractor sources analyzed
std_numpy_nsources_sexcat         1038.24                  Standard deviation of corresponding average
margin_of_error_nsources_sexcat   64.35                    Uncertainty of corresponding average (95% confidence level)
avg_numpy_ns_true                 62.23                    Average number of catalog matches with fake-source positions (1.5-pixel radius)
std_numpy_ns_true=                11.50                    Standard deviation of corresponding average
margin_of_error_ns_true           0.71                     Uncertainty of corresponding average (95% confidence level)
================================= ======================== ===============================================================================

.. note::
    The ``XWIN_IMAGE, YWIN_IMAGE`` pixel coordinates are one-based indices, while the pixel coordinates
    of the fake-source truth list and PhotUtils PSF-fit catalog are zero-based indices.


PhotUtils DAOStarFinder Input-Parameter Variation
************************************

In all ten cases below, the input threshold is 5 times the clipped standard deviation
of the ZOGY difference image (multiplied by a Gaussian correction factor to account for the data clipping)::

    threshold = 0.2488752235542349 DN/s for the above single sample

This is the same threshold sigma that was used in the 9/27/2025 test.

Case #1 defines the parameters that were used in the 9/27/2025 test.

Statistical results for sample size = 1000.  The same inputs were used as for the above SExtractor baseline.

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
the largest number of fake-source matches (74) within 1.5 pixels.

Plots for all of these cases are given below for the aforementioned single sample.

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


