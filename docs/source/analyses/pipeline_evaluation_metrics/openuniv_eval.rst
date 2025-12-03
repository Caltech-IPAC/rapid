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
ZOGY      H158      1142      2307488      63888      470970 (20.4%)    52409 (95.2%)     141714 (20.4%)    409852 (17.8%)    271239 (11.7%)    58625 (91.8%)
======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============

.. toctree::
    :maxdepth: 1

    filtering_20250927_ZOGY_H158.rst

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

======    ======    ========    =======================    ==============    ==============    ===================    =========================    =====
Method    Filter    Group       :math:`m_{\mathrm{th}}`    :math:`m_{80}`    :math:`m_{20}`    :math:`m_{5\sigma}`    :math:`m_{\mathrm{ph}10}`    FOM
======    ======    ========    =======================    ==============    ==============    ===================    =========================    =====
ZOGY      H158      Off-Nuc.    25.13                      23.79             25.24             26.77                  24.19                        24.89
ZOGY      H158      Nuc.        23.52                      24.27             24.69             24.27                  23.14                        24.28
ZOGY      H158      Overall     24.60                      23.95             25.06             25.94                  23.84                        24.69
======    ======    ========    =======================    ==============    ==============    ===================    =========================    =====

.. toctree::
:maxdepth: 1

FOM_20250927_ZOGY_H158.rst

.. note::
   This analysis is preliminary and meant to illustrate the procedure for evaluation the performance of the RAPID pipeline
   based on the FOM. The chosen
   thresholds, weights, and grouping criteria that will be used for, e.g., algorithm down-selection, are under development.
   
