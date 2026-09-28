Evaluation of OpenUniverse Simulated Data Pipeline Tests
##################################################################

Evaluations are organized by processing date. See :ref:`testing` for each
test run's specifications.

9/27/2025
************************************

This run evaluates pipeline performance on 6,875 science images across
7 filters using two difference image-subtraction methods: ZOGY and SFFT
(with PSF cross-convolution).

Source Injections
====================================

Fake sources were injected as described in :ref:`fake_source_injection`,
with 100 sources per image and ``size_factor`` :math:`= 1.5`.

Source Detection and Filtering
====================================

SExtractor detects sources on matched-filtered difference images where
available (Scorr for ZOGY and cross-convolved difference images for SFFT)
and measures photometry on the difference images. The primary detection
parameters were:

* ``DETECT_MINAREA`` :math:`=5`
* ``DETECT_THRESH`` :math:`=2.5`
* ``ANALYSIS_THRESH`` :math:`=2.5`
* ``FILTER`` :math:`=` 'N'
* ``WEIGHT_TYPE`` :math:`=` 'NONE,MAP_RMS'
* ``DEBLEND_NTHRESH`` :math:`=32`
* ``PHOT_APERTURES`` :math:`=2.0,3.0,4.0,6.0,10.0,14.0` (aperture diameter in pixels)

Raw catalogs were filtered as described in :ref:`filtering`, using these
thresholds:

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

Detections are cross-matched to injected-source catalogs within
``injmatchrad`` :math:`=0.5` WFI pixels.

Filtering results:

======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============
Method    Filter    Images    Unmatched    Matched    Unmatched1        Matched1          Unmatched2        Matched2          Unmatched3        Matched3
======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============
ZOGY      H158      1142      2307488      63888      470970 (20.4%)    60845 (95.2%)     409852 (17.8%)    60431 (94.6%)     271239 (11.8%)    58625 (91.8%)
SFFT      H158      1142      2935660      74690      148362  (5.1%)    66434 (89.0%)     137408  (4.7%)    66000  (88.4%)    30922   (1.1%)    63896 (85.6%)
======    ======    ======    =========    =======    ==============    ==============    ==============    ==============    ==============    ==============

.. toctree::
    :maxdepth: 1

    filtering_20250927_ZOGY_H158.rst
    filtering_20250927_SFFT_H158.rst

Evaluation of Figures of Merit
====================================

The Figure of Merit (FOM; defined in :ref:`figure_of_merit`) is evaluated
for both methods in H158. Sources are grouped by their separation from
the core of the nearest truth-catalog galaxy brighter than
``galmatchthres`` :math:`= 25` mag:

* Off-nuclear: :math:`\gt 1.5 \times` ``injmatchrad``.
* Nuclear: :math:`\leq 1.5 \times` ``injmatchrad``.

This grouping accounts for possible contamination of the recovered True
Positive (TP) sample by spurious detections from imperfect galaxy
subtraction in nuclear regions. The FOM calculations assign overall
relative weights of 1.0 to off-nuclear sources and 0.5 to nuclear sources.
The relative weights of the FOM terms are :math:`w_{\mathrm{th}} = 1.0`,
:math:`w_{80} = 0.8`, :math:`w_{20} = 0.6`, :math:`w_{5\sigma} = 0.3`, and
:math:`w_{\mathrm{ph}10} = 0.2`. The maximum acceptable false-positive
rate per image is ``fpratetol`` :math:`=10`.

.. note::
   This preliminary analysis illustrates how to evaluate RAPID pipeline
   performance using the FOM. Thresholds, weights, and grouping criteria
   for uses such as algorithm down-selection remain under development.

======    ======    ========    =======================    ==============    ==============    ===================    =========================    ==========
Method    Filter    Group       :math:`m_{\mathrm{th}}`    :math:`m_{80}`    :math:`m_{20}`    :math:`m_{5\sigma}`    :math:`m_{\mathrm{ph}10}`    FOM
======    ======    ========    =======================    ==============    ==============    ===================    =========================    ==========
ZOGY      H158      Off-Nuc.    25.13                      23.79             25.24             26.77                  24.19                        24.89
ZOGY      H158      Nuc.        23.52                      24.27             24.69             26.77                  23.04                        24.27
SFFT      H158      Off-Nuc.    25.38                      24.44             25.37             26.78                  24.73                        25.22
SFFT      H158      Nuc.        26.02                      25.11             26.04             26.78                  24.02                        25.71
ZOGY      H158      Overall     24.60                      23.95             25.06             26.77                  23.81                        **24.68**
SFFT      H158      Overall     25.59                      24.66             25.59             26.78                  24.49                        **25.38**
======    ======    ========    =======================    ==============    ==============    ===================    =========================    ==========

.. toctree::
    :maxdepth: 1

    FOM_20250927_ZOGY_H158.rst
    FOM_20250927_SFFT_H158.rst
