Evaluation of OpenUniverse Simulated Data Pipeline Tests
##################################################################

Overview
************************************

The test evalutions described below are organized by processing date. See :ref:`testing` for specifications
of each test run. 

9/27/2025
************************************

This test run consists of 6,875 science images across 7 filters. We evalutate the performance of the pipeline
for three difference image-subtraction methods: ZOGY, SFFT (with PSF cross-convolution), and Naive (:math:`A-B`). 


Source Injections
====================================

Fake-source injections were performed with the scheme described in :ref:`fake_source_injection` with 
100 sources per image and ``size_factor`` :math:`= 1.5`.

Source Detection and Filtering
====================================
We evalute the performance of source detection with SExtractor, with detection performed on the matched-filtered difference images 
where available (Scorr for ZOGY and cross-convolved difference images for SFFT), else on the difference image itself (Naive method).
SExtractor photometry is performed on the difference images. The primary SExtractor parameters used for source detection were:

* ``DETECT_MINAREA`` :math:`=5`
* ``DETECT_THRESH`` :math:`=2.5`
* ``ANALYSIS_THRESH`` :math:`=2.5`
* ``FILTER`` :math:`=` 'N' (for ZOGY or SFFT), 'Y' (for Naive)
* ``WEIGHT_TYPE`` :math:`=` 'NONE,MAP_RMS'
* ``DEBLEND_NTHRESH`` :math:`=32`
* ``PHOT_APERTURES`` :math:`=2.0,3.0,4.0,6.0,10.0,14.0` (aperture diameter in pixels)

Raw catalogs were filtered as described in :ref:`filtering` with the following thresholds:

1. Catalog level
    a. ``diffimedgetol`` :math:`=5`
    b. ``snrthres`` :math:`=5`
    c. ``elongthresh`` :math:`=2.0`
    d. ``apfluxratiothreslow`` :math:`=0.35`, ``apfluxratiothreshigh`` :math:`=1.2`
2. Pixel-value level
    a. ``nnegthres`` :math:`=18`
    b. ``nbadthres`` :math:`=12`
    c. ``sumratthres`` :math:`=0.25` 
3. PSF photometry 
    a. ``rchipsfthres`` :math:`=10`
    b. ``magdiffthres`` :math:`=0.3`

Detections are cross-matched to the injected source catalogs using a matching radius of ``injmatchrad`` :math:`=0.5` WFI pixels.

Filtering results:

======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============
Method    Filter    Images    Unmatched    Matched    Unmatched1        Matched1          Unmatched2        Matched2          Unmatched3        Matched3
======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============
ZOGY      H158      1142      2307488      63888      470970 (20.4%)    60845 (95.2%)     409852 (17.8%)    60431 (94.6%)     271239 (11.8%)    58625 (91.8%)
SFFT      H158      1142      2935660      74690      148362  (5.1%)    66434 (89.0%)     137408  (4.7%)    66000  (88.4%)    30922   (1.1%)    63896 (85.6%)
Naive     H158      1142      537611       58531      187999 (35.0%)    57558 (98.3%)     168774 (31.4%)    57384  (98.0%)    106193 (19.7%)    56005 (95.7%)
======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============

.. toctree::
    :maxdepth: 1

    filtering_20250927_ZOGY_H158.rst
    filtering_20250927_SFFT_H158.rst
    filtering_20250927_Naive_H158.rst

Evaluation of Figures of Merit
====================================
We evaluate the Figure of Merit (FOM; as defined in :ref:`figure_of_merit`) for each difference imaging method for H158. We define two 
groups sources for this analysis based on their separations from the core of the nearest truth-catalog galaxy brighter than 
``galmatchthres`` :math:`= 25` mag, off-nuclear sources at :math:`\gt 1.5 \times` ``injmatchrad`` and nuclear sources 
at :math:`\leq 1.5 \times` ``injmatchrad``. This grouping allows us to account for possible contamination of the recovered True Positive (TP) 
sample by spurious detections arising from imperfect galaxy subtraction in the nuclear regions. We apply overall relative weights of 1.0 and 0.5 
for the off-nuclear and nuclear source groups, respectively, in the FOM calculations. The relative weights for each term in the FOM 
are :math:`w_{\mathrm{th}} = 1.0`, :math:`w_{80} = 0.8`, :math:`w_{20} = 0.6`, :math:`w_{5\sigma} = 0.3`, and :math:`w_{\mathrm{ph}10} = 0.2`.
We adopt ``fpratetol`` :math:`=10` as the maximum acceptable false-positive rate per image. 

======    ======    ========    =======================    ==============    ==============    ===================    =========================    ==========
Method    Filter    Group       :math:`m_{\mathrm{th}}`    :math:`m_{80}`    :math:`m_{20}`    :math:`m_{5\sigma}`    :math:`m_{\mathrm{ph}10}`    FOM
======    ======    ========    =======================    ==============    ==============    ===================    =========================    ==========
ZOGY      H158      Off-Nuc.    25.13                      23.79             25.24             26.77                  24.19                        24.89
ZOGY      H158      Nuc.        23.52                      24.27             24.69             26.77                  23.04                        24.27
SFFT      H158      Off-Nuc.    25.38                      24.44             25.37             26.78                  24.73                        25.22
SFFT      H158      Nuc.        26.02                      25.11             26.04             26.78                  24.02                        25.71
Naive     H158      Off-Nuc.    25.84                      23.85             25.25             26.77                  24.02                        25.16
Naive     H158      Nuc.        23.12                      23.91             24.52             26.77                  22.88                        24.77
ZOGY      H158      Overall     24.60                      23.95             25.06             26.77                  23.81                        **24.68**
SFFT      H158      Overall     25.59                      24.66             25.59             26.78                  24.49                        **25.38**
Naive     H158      Overall     24.93                      23.88             25.01             26.77                  23.64                        **24.57**
======    ======    ========    =======================    ==============    ==============    ===================    =========================    ==========

.. note::
   This analysis is preliminary and meant to illustrate the procedure for evaluation the performance of the RAPID pipeline
   based on the FOM. The chosen
   thresholds, weights, and grouping criteria that will be used for, e.g., algorithm down-selection, are under development.

.. toctree::
    :maxdepth: 1

    FOM_20250927_ZOGY_H158.rst
    FOM_20250927_SFFT_H158.rst

