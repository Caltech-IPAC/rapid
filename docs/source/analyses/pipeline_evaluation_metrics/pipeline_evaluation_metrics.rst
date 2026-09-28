RAPID Pipeline Evaluation
#########################


Overview
********

These procedures evaluate RAPID pipeline performance, guide algorithm
down-selection (e.g., for difference imaging and source detection), and
support parameter tuning. They remain under development as the pipeline
evolves and is evaluated on simulated data sets. Before Roman launch, we
will develop specific plans and timelines for evaluation and tuning with
in-flight data, based on scheduled Roman observations as they become
available.

.. _fake_source_injection:

Source Injections
*****************

Evaluation relies on synthetic point-source injections. The implemented
scheme associates injections with detected sources/galaxies and was
developed for OpenUniverse simulated images of a nominal HLTDS
implementation:

* Perform simple source detection and deblending on the science image with
  **PhotUtils**, at a threshold of :math:`10\sigma` above the median
  background. Most detections will be galaxies, but some may be stars or
  features in bright stars' wings or diffraction spikes.
* Select a random subset of detections based on the desired number of
  injections per image, :math:`N_{\mathrm{inj}}`.
* Offset each injection from the detected source centroid in both x and y,
  using a uniform distribution whose half-width is the estimated semi-major
  axis (``semimajor_sigma`` measured by **PhotUtils**) multiplied by
  ``size_factor``.
* Draw magnitudes uniformly between 21 and 28 AB mag and convert to image
  counts (electrons) using the appropriate zeropoint for the image filter,
  Roman SCA, and exposure time.
* Compute each PSF with the **Roman I-Sim** tool
  **romanisim.image.make_one_psf** for the filter, SCA, and detector
  position. This uses **galsim.roman** to emulate the original OpenUniverse
  Roman simulation PSFs; chromatic effects are currently ignored.
* Add each injection to the science image with
  **romanisim.image.add_objects_to_image**, using the specified location,
  PSF, and flux, including Poisson noise.

Additional schemes under consideration are:

* Random positions or a pre-defined grid, independent of detected sources,
  to evaluate hostless transients or crowded fields (e.g., the GBTDS and
  associated simulations).
* Time-series injections at specified sky locations in a given field, with
  pre-defined light curves. Stellar counterparts could also be injected
  into images used for reference mosaics to evaluate variable stars.

.. _filtering:

Detection Catalog Filtering
***************************

Raw RAPID detection catalogs will contain many spurious detections from
imperfect subtraction of static galaxies and stars, noise fluctuations,
and detector artifacts such as unflagged hot pixels or cosmic-ray hits.
The following filters, ordered by increasing computational expense, clean
the catalogs and supply metrics/features to a machine-learning real-bogus
(RB) classifier. Adapted from the `ZTF Science Data System`_, this
procedure remains under active development.

.. _ZTF Science Data System: https://irsa.ipac.caltech.edu/data/ZTF/docs/ztf_explanatory_supplement.pdf

1. Catalog-level measurements from source detection (e.g., by
   **SExtractor** or **Photutils DAOStarFinder**):

   a. ``mindedge`` :math:`\gt` ``diffimedgetol``: Minimum distance from any
      image edge, in pixels.
   b. ``snrap3pix`` :math:`\geq` ``snrthres``: Signal-to-noise ratio (S/N)
      in a 3-pix diameter aperture at the candidate position, measured from
      the difference image and corresponding uncertainty map. The initial
      threshold is ``snrthres`` :math:`=5`.
   c. ``elong`` :math:`\leq` ``elongthres``: Source elongation, the ratio
      A/B of semi-major to semi-minor axis.
   d. (``apfluxratio`` :math:`\geq` ``apfluxratiothreslow``) and
      (``apfluxratio`` :math:`\leq` ``apfluxratiothreshigh``): Ratio of flux
      in a 3-pixel diameter aperture to that in a 6-pixel diameter aperture.

   .. note::
      These measurements are available in SExtractor catalogs. For
      **Photutils DAOStarFinder** or another detection method, analogous
      measurements will be used. DAOStarFinder's ``sharpness`` and
      ``roundness`` estimates could also be used.

2. Pixel-level metrics from a 5x5 difference-image cutout centered on each
   candidate:

   a. ``nneg`` :math:`\leq` ``nnegthres``: Number of negative-valued pixels.
   b. ``nbad`` :math:`\leq` ``nbadthres``: Number of pixels flagged as bad.
      Currently, only pixels masked as NaN in the difference image because
      of missing reference-mosaic coverage are considered bad.
   c. ``sumrat`` :math:`\leq` ``sumratthres``: Apply a 3x3 median filter
      with kernel truncation at cutout edges, ignoring unavailable or NaN
      values. ``sumrat`` is the ratio of the sum of pixel values in the
      median-filtered cutout to the sum of their absolute values.
      Gaussian-distributed noise is expected to give
      :math:`-0.25 \lt` ``sumrat`` :math:`\lt 0.25`,
      while real signal approaches 1.

3. PSF-fitting photometry quality cuts:

   a. :math:`0 \lt` ``chipsf`` :math:`\lt` ``chipsfthres``: Reduced
      chi-squared of the PSF fit.
   b. :math:`|` ``magap3pix`` :math:`-` ``magpsf`` :math:`| \lt`
      ``magdiffthres``: Difference between the appropriately
      aperture-corrected 3-pixel diameter aperture magnitude and the
      PSF-fit magnitude.

4. Machine-learning real-bogus (RB) classification using all relevant
   features/metrics above and available metadata, such as positional
   associations with stars or galaxies in the reference image.

.. _figure_of_merit:

Figure of Merit
***************

The figure of merit (FOM) evaluates pipeline performance to guide algorithm
down-selection (e.g., ZOGY or SFFT for image subtraction) and parameter and
threshold tuning. It is an effective limiting magnitude for a specified
data set, combining these terms in approximate order of importance/weight:

1. :math:`m_{\mathrm{th}}`: Average magnitude corresponding to the S/N
   threshold that achieves an acceptable false-positive rate per image.
2. :math:`m_{80}`: Magnitude at which 80% of injected sources are recovered.
3. :math:`m_{20}`: Magnitude at which 20% of injected sources are recovered.
4. :math:`m_{5\sigma}`: The :math:`5\sigma` point-source limiting magnitude
   on blank sky in the difference images.
5. :math:`m_{\mathrm{ph}10}`: Magnitude at which injected fluxes are
   recovered with 10% precision in PSF-fitting photometry.

Each term is a weighted sum over the test data set's filters, :math:`f`;
the final FOM is a weighted sum of these terms:

.. math::
   \mathrm{FOM} = \left(w_{\mathrm{th}} \frac{\sum_{f} w_{\mathrm{th},f} m_{\mathrm{th},f}}{\sum_{f} w_{\mathrm{th},f}}
   + w_{80} \frac{\sum_{f} w_{80,f} m_{80,f}}{\sum_{f} w_{80,f}}
   + w_{20} \frac{\sum_{f} w_{20,f} m_{20,f}}{\sum_{f} w_{20,f}}
   + w_{5\sigma} \frac{\sum_{f} w_{5\sigma,f} m_{5\sigma,f}}{\sum_{f} w_{5\sigma,f}}
   + w_{\mathrm{ph10}} \frac{\sum_{f} w_{\mathrm{ph10},f} m_{\mathrm{ph10},f}}{\sum_{f} w_{\mathrm{ph10},f}}\right) \\\\
   / (w_{\mathrm{th}} + w_{80} + w_{20} + w_{5\sigma} + w_{\mathrm{ph10}}).

The weights, :math:`w`, can emphasize different aspects of performance or
prioritize filters. Each term can also be calculated and weighted by
injected-source characteristics, such as transients on bright hosts,
hostless events, nuclear transients, or variables with stellar
counterparts. RAPID needs to perform well across all Roman surveys and
filters, for a broad range of transients and variables.


Evaluation Procedure
********************

1. Define the evaluation setup and run the pipeline:

   a. Define the data set and injection parameters. Before launch, this
      could be a test run with OpenUniverse HLTDS simulations or a
      RimTimSim GBTDS set (see :ref:`testing`). After launch, we will define
      survey subsets for fake-source evaluation, such as HLTDS fields over
      a specific period. Specify the number of injections per image, their
      magnitude distribution, and the position-assignment method (regular
      grid, random positions, randomized offsets from detected galaxies,
      on top of stars, etc.).
   b. Specify the pipeline modules, steps, or settings to compare: image
      subtraction (ZOGY vs SFFT vs Naive), detection (SExtractor vs
      Photutils DAOStarFinder), or algorithm settings (e.g., SEXtractor
      detection thresholds).
   c. Specify all evaluation axes and weights, including at least the
      filters in the data set. Other axes may describe injected sources,
      such as separation from the host galaxy core or transients vs.
      variables.
   d. Run the pipeline on the test data set with the specified injections
      and settings.

2. After image subtraction and raw-catalog generation, apply the
   pre-defined filtering thresholds described above to remove most
   spurious candidates. Perform PSF-fitting photometry on all survivors
   using the difference images, corresponding uncertainty maps, and
   unit-normalized difference-image PSF models. Filter again using the
   PSF-fitting results.

3. Positionally cross-match each surviving transient candidate to the
   nearest source in the reference image. For OpenUniverse simulations,
   this can use truth-catalog galaxies brighter than ``galmatchthres``.
   For real data, we will use pipeline-generated reference-image source
   catalogs, including metadata that can distinguish stars from galaxies.

4. Vet candidates with machine-learning (ML) real-bogus (RB) classification
   using all relevant features and metadata described above.

5. Cross-match candidates to injected-source catalogs (or OpenUniverse
   truth catalog transients) within ``injmatchrad``. Candidates passing all
   filter criteria and matched to an injected source are successfully
   recovered true positives (TPs); those passing all criteria but unmatched
   to an injected source are false positives (FPs).

6. Calculate all FOM terms for each filter and injection/detection
   sub-group (e.g., injections/candidates separated from the nearest galaxy
   from step 3 by :math:`\gt 1.5 \times` ``injmatchrad``):

   a. :math:`m_{\mathrm{th}}`: Group FP candidates by :math:`m_{3\mathrm{pix}}`
      in :math:`\Delta m = 0.2` bins. Count those at or brighter than each
      bin, :math:`N_{\mathrm{FP}\lt m}`. Compute :math:`m_{\mathrm{th}}` by
      linearly interpolating :math:`N_{\mathrm{FP}\lt m}` to the defined FP
      rate tolerance, ``fpratetol``. Interpolate the mean FP-candidate S/N
      per bin to :math:`m_{\mathrm{th}}` to obtain ``snrfpthres``, the S/N
      threshold consistent with ``fpratetol``.

   b. :math:`m_{80}` and :math:`m_{20}`: Calculate recovery completeness
      (the fraction of injected sources recovered in the sub-group) in
      :math:`\Delta m = 0.5` bins of injected magnitude, using all TPs
      passing :math:`SNR \geq` ``snrfpthres``. Account for injections
      lacking reference-image coverage or falling within ``diffimedgetol``
      of any image boundary. Linearly interpolate the completeness curve
      to obtain :math:`m_{80}` and :math:`m_{20}` at 80% and 20%
      completeness, respectively.

   c. :math:`m_{5\sigma}`: Estimate background noise,
      :math:`\sigma_{\mathrm{diffbkg}}`, from the :math:`6\sigma`-clipped
      standard deviation of all pixel values in each difference image.
      This can be compared with the sigma-clipped average of the
      corresponding difference uncertainty map to check that the map is
      reasonable. Calculate the :math:`5\sigma` point-source limiting flux as
      :math:`5 \sqrt{N_p} \sigma_{\mathrm{diffbkg}}`, where :math:`N_p` is
      the number of `noise pixels`_ in the unit-normalized difference-image
      PSF model. Convert to :math:`m_{5\sigma}` with the appropriate AB
      magnitude zeropoint.

   d. :math:`m_{\mathrm{ph}10}`: For all TPs, calculate the fractional error
      between recovered PSF-fit and injected flux,
      :math:`(f_{\mathrm{PSF}} - f_{\mathrm{inj}})/f_{\mathrm{inj}}`, and
      group it in :math:`\Delta m = 0.5` bins of injected magnitude.
      Linearly interpolate the fractional errors per bin to obtain
      :math:`m_{\mathrm{ph}10}` at 10% precision.

      .. note::
         This estimates statistical precision only; it does not account
         for systematic biases in recovered fluxes.

7. Calculate the final FOM with the specified weights for each term and
   filter/sub-group. The pipeline version that maximizes the FOM, with its
   choice of subtraction algorithm, detection method, or parameter
   settings, is judged to perform better.

.. _noise pixels: https://web.ipac.caltech.edu/staff/fmasci/home/mystats/noisepix_specs.pdf

Evaluation of Pipeline Test runs
********************************

.. toctree::
    :maxdepth: 2

    openuniv_eval.rst
