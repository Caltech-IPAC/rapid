Testing with SOC Simulated Data
####################################################

Overview
************************************

The Roman Space Telescope Science Operations Center (SOC) generated simulated
Level 2 (L2) Calibrated Science Data Files for the GBTDS survey. These
"socsims" cover the Wide Field Instrument (WFI) and are stored in the
Advanced Scientific Data Format (ASDF)::

    s3://stpubdata/roman/nexus/soc_simulations/r00340/l2/

Simulation tools such as Roman-I-Sim and STIPS mimic telescope data so
researchers can prepare for analysis before launch. The
wfi_soc-simulation_l2_cal.asdf files support workflow testing, WFI
field-of-view visualization, and astrometric and photometric precision checks.

* Content and calibration: A simulation of the romancal pipeline processes
  Level 1 (L1) raw data (uncalibrated ramps), removing instrumental effects
  to produce science-ready calibrated rate images in Digital Numbers per
  second (DN/s).
* Format: ASDF, the standard Roman data format, stores image arrays and metadata.
* WFI coverage: 0.281 degrees-squared across 18 detectors (SCAs).
* Astrometry: The files are designed to align with the Gaia reference frame.
* Generators: Roman I-Sim is a GalSim-based simulator for high-fidelity,
  pipeline-compatible data; STIPS (Space Telescope Imaging Product Simulator)
  creates synthetic astronomical scenes.

RAPID input preparation
====================================

Fake variable sources with fixed sky positions were added to the ASDF files
for the RAPID pipeline and stored here::

    s3://socsims-fakesrc-asdf-20260709/

The SOC sims' gWCS was incorrect because there were no GAIA stars and the
astrometry step failed. It was corrected with::

    import roman_datamodels as rdm
    from romancal.assign_wcs import AssignWcsStep
    original_dm = rdm.open(asdf_path)
    dm = AssignWcsStep.call(original_dm)

The ASDF files were converted to FITS and stored here::

    s3://socsims-fakesrc-fits-20260709-lite/

The FITS WCS uses TAN-SIP projection with fifth-order SIP distortion. Tests
show very good agreement with the original ASDF gWCS: two examples (SCAs 2
and 9) were examined for absolute WCS error between FITS and ASDF. Across
all 18 SCAs, the worst deviations never exceed ~1e-6 of a pixel.

Dataset coverage
====================================

Each file covers one exposure and SCA, with filenames such as
``r0034001001001001001_0001_wfi01_f062_cal.asdf``. The 88,038 available files
cover 4,891 exposures and all bandpass filters. Assuming 66.4 seconds per
exposure, the predominant exposure time in the GBTDS observation-planning
files, the dataset represents approximately 3.75 days of cumulative
exposure time, approximately 34% of the entire GBTDS survey.

SOC-sim metadata are stored in a dedicated RAPID-operations PostgreSQL
database. The precise cumulative exposure time in days is:


.. code-block::

    socsimsdb=> select sum(exptime) / (3600*24) as cumexposdays from exposures;
        cumexposdays
    --------------------
     3.7593229166666666
    (1 row)

The observation times span about 8 days:

.. code-block::

    select min(dateobs),min(mjdobs),max(dateobs),max(mjdobs) from l2files where vbest > 0;
             min         |  min  |         max         |        max
    ---------------------+-------+---------------------+-------------------
     2027-10-01 00:00:00 | 61679 | 2027-10-08 18:21:34 | 61686.76497685185
    (1 row)

The images overlap a total of 109 sky tiles (a.k.a. fields):

.. code-block::

    select count(distinct field) from l2files where vbest > 0;
     count
    -------
       109

Image counts across all SCAs by bandpass filter, retaining the Open Univers
sims' filter-name convention:

.. code-block::

    select a.fid,filter,count(*) from l2files a, filters b where a.fid=b.fid and vbest > 0 group by a.fid,filter order by a.fid,filter;
     fid | filter | count
    -----+--------+-------
       1 | F184   |   205
       2 | H158   |   210
       3 | J129   |   204
       4 | K213   |  3186
       5 | R062   |   414
       6 | Y106   |   186
       7 | Z087   |  3171
       8 | W146   | 76250
    (8 rows)

W146 (``fid=8``) accounts for most exposures by a large margin.


7/6/2026
************************************

The first socsims test covers 6,917 science images from part of the first
observation day, limited to W146 (``fid=8``). About 200 fake variable sources
with fixed sky positions were injected per science image before ASDF-to-FITS
conversion, allowing lightcurves to be generated from extractions over time.
The science images used to build reference images also contain fake variable
sources.

Setup
====================================

Virtual Pipeline Operator (VPO) invocation:

.. code-block::

    export DBNAME=socsimsdb
    export STARTDATETIME="2027-10-01 07:12:00"
    export ENDDATETIME="2027-10-02 00:00:00"
    export STARTREFIMMJDOBS=61678.9
    export ENDREFIMMJDOBS=61679.3
    export RUNFID=8

    python3.11 /code/pipeline/virtualPipelineOperator.py 20260706 >& virtualPipelineOperator_20260706.out &

``STARTDATETIME`` and ``ENDDATETIME`` exclude the first 20 or so images per
field, reserving them for reference-image generation. Reference-image
parameters in the input configuration file::

    [REF_IMAGE]
    # Pipeline number of reference-image pipeline.
    ppid = 12
    # Size of reference image to be generated.
    naxis1_refimage = 7000
    naxis2_refimage = 7000
    # SCA is 0.11 arcsec per pixel or 0.000030555555556 degrees
    cdelt1_refimage = -0.000030555555556
    cdelt2_refimage = 0.000030555555556
    # Reference image is NOT rotated (CROTA2 = 0.0 degrees)
    crota2_refimage = 0.0
    # Need to limit number of reference-image input frames; otherwise AWS Batch job may time out.
    min_n_images_to_coadd = 3
    max_n_images_to_coadd = 25


Results
====================================

Pipeline exit codes:

.. code-block::

    socsimsdb=> select ppid,exitcode,count(*) from jobs where cast(launched as date) = '20260706' group by ppid, exitcode order by ppid, exitcode;

     ppid | exitcode | count
    ------+----------+-------
       15 |        0 |  6917
       17 |        0 |  6917
    (2 rows)

Reference images were generated for 109 unique fields in W146 (``fid=8``).
Sub-pixel dithers limit the ``cov5percent`` coverage metric to ~30-50 percent.

.. code-block::

    socsimsdb=> select a.field,nframes,cov5percent from refimages a, refimmeta b where a.rfid=b.rfid and vbest>0 order by a.field;

      field  | nframes | cov5percent
    ---------+---------+-------------
     4637678 |      21 |   32.699566
     4641773 |       9 |   32.364754
     4641775 |      25 |    42.81024
     4645869 |      21 |   31.454176
     4645873 |      21 |   33.714382
     4649967 |      21 |   31.675232
     4649970 |      21 |    33.66247
     4654042 |      21 |   33.451668
     4654060 |      21 |   33.264027
     4654062 |      10 |   31.661242
     4654064 |      21 |    32.57979
     4654065 |      19 |   32.143597
     4654068 |      21 |    32.65233
     4654070 |      21 |   33.370052
     4658155 |      21 |   32.678642
     4658157 |      21 |   33.627655
     4658159 |      21 |   31.673664
     4658163 |      21 |   32.721188
     4658165 |      12 |    32.68768
     4662233 |      25 |   45.761803
     4662235 |      21 |    33.52244
     4662250 |      21 |   31.291698
     4662254 |      21 |   33.714424
     4662257 |      21 |   31.193888
     4662258 |      25 |   42.473377
     4662260 |      21 |    31.39644
     4666328 |      21 |   31.760868
     4666330 |      21 |   32.704926
     4666332 |      10 |   33.689045
     4666348 |      21 |    32.38571
     4666352 |      21 |   33.721836
     4670425 |      21 |   31.055794
     4670427 |      21 |   32.744156
     4670430 |      25 |    48.01205
     4670431 |      21 |    33.62679
     4670433 |      18 |   33.507244
     4670441 |      22 |   33.396664
     4670443 |      21 |   31.682625
     4670445 |      21 |    32.70783
     4670447 |      18 |   31.893322
     4670449 |      20 |   32.762115
     4670451 |      21 |   33.381954
     4674525 |      21 |    32.69599
     4674536 |      22 |   32.679405
     4674538 |      22 |   33.628376
     4674540 |      21 |   31.637186
     4674543 |      25 |   43.877113
     4674544 |      21 |   32.346336
     4674546 |      25 |   42.561398
     4678618 |      21 |    31.21337
     4678620 |      20 |   31.605726
     4678622 |      21 |   32.336544
     4678624 |      21 |   32.705383
     4678631 |      14 |   31.025618
     4678636 |      22 |   33.327637
     4678638 |      21 |    31.68165
     4678640 |      21 |   31.463388
     4682717 |      21 |   31.231985
     4682719 |      25 |   44.344437
     4682729 |      17 |   32.529026
     4682733 |      21 |   33.722122
     4682738 |      25 |   44.068043
     4686822 |      21 |   33.049397
     4686824 |      22 |   31.655037
     4686827 |      22 |   32.538555
     4686831 |      25 |   48.138443
     4686833 |      22 |   33.513924
     4690917 |      22 |    32.29945
     4690920 |      22 |   33.236004
     4690922 |      22 |   31.674442
     4690924 |      22 |   32.040066
     4690926 |      22 |   32.721912
     4690928 |      14 |   32.691975
     4695013 |      22 |   30.814701
     4695017 |      22 |   33.712738
     4695019 |      18 |    31.67748
     4695021 |      22 |   31.607107
     4699111 |      22 |    32.36241
     4699114 |      22 |   33.462963
     4699119 |      25 |      45.352
     4703204 |      22 |   33.448296
     4703206 |      22 |    31.68327
     4703208 |      22 |     32.7447
     4703212 |      22 |   33.318813
     4703214 |      20 |   33.512596
     4707299 |      22 |   32.679314
     4707301 |      22 |    33.62832
     4707303 |      22 |   31.674343
     4707305 |      21 |     31.8079
     4707307 |      22 |   32.721813
     4707309 |      22 |     32.7074
     4711394 |      22 |   30.856216
     4711398 |      22 |    33.71138
     4711401 |      22 |    31.46055
     4711402 |      25 |    40.61648
     4715492 |      22 |    32.59121
     4715496 |      22 |   33.722668
     4715500 |      22 |   31.253225
     4719587 |      22 |   31.683285
     4719589 |      22 |   32.744705
     4719593 |      22 |   32.878258
     4719595 |      20 |   33.238686
     4723684 |      14 |   31.647387
     4723687 |      25 |    40.95568
     4723688 |      22 |   32.238068
     4723690 |      25 |   45.896633
     4727782 |      22 |   31.682259
     4727784 |      17 |    31.47939
     4731882 |      22 |   30.980074
    (109 rows)


Performance
====================================

For pipeline instance jid = 114725, which generated a reference image,
PSF-fit PhotUtils catalog generation for the reference and difference images
dominated execution time. Setting up and executing awaicgen took about
5 minutes, depending on the input-image count (NFRAMES=21 here). SFFT was
relatively quick.


==================================================================  =====================
Pipeline step                                                        Execution time (sec)
==================================================================  =====================
Downloading science image                                                   0.606
Uploading science image to product S3 bucket                                0.457
Setting up inputs for awaicgen                                             28.350
Executing awaicgen                                                        271.428
Downloading or generating reference-image products                       1638.540
Uploading reference image to S3 product bucket                              2.167
Generating science-image catalog                                            9.443
Swarping images                                                             9.089
Running bkgest on science image                                             3.973
Running gainMatchScienceAndReferenceImages                                 10.426
Replacing NaNs, applying image offsets, etc.                                4.616
Uploading intermediate FITS files to product S3 bucket                      2.569
Running ZOGY                                                               39.150
Masking ZOGY difference image                                               0.952
Running SExtractor on positive ZOGY difference image                       10.057
Running SExtractor on negative ZOGY difference image                        8.503
Generating PSF-fit catalog on positive ZOGY difference image              129.956
Generating PSF-fit catalog on negative ZOGY difference image              128.204
Uploading main products to S3 bucket                                        5.788
Running SFFT                                                              130.775
Uploading SFFT difference image to S3 product bucket                        4.697
Running SExtractor on positive SFFT difference images                      19.006
Running SExtractor on negative SFFT difference images                      17.591
Uploading SFFT-diffimage SExtractor catalogs to S3 product bucket           1.792
Generating PSF-fit catalog on positive SFFT difference image              103.801
Generating PSF-fit catalog on negative SFFT difference image              365.842
Uploading SFFT-diffimage PSF-fit catalogs to S3 product bucket              1.587
Computing naive difference images                                           0.666
Uploading naive difference images to S3 product bucket                      0.884
Running SExtractor on positive naive difference image                       5.175
Running SExtractor on negative naive difference image                      32.254
Uploading SExtractor catalogs for naive difference images                   1.043
Generating PSF-fit catalog on positive naive difference image             173.096
Generating PSF-fit catalog on negative naive difference image             131.839
Uploading PSF-fit catalogs for naive difference images                      1.505
Uploading products at pipeline end to S3 product bucket                     0.083
Total time to run this instance of the science pipeline                  2996.133
==================================================================  =====================

This instance took about 50 minutes; instances reusing existing reference
images took about 20 minutes each.

Processing all 6,917 science images took 3.7 hours: science pipelines
generated the 109 reference images and basic products (``ppid=15``),
post-processing pipelines ran (``ppid=17``), and product metadata were loaded
into the RAPID-operations PostgreSQL database. Overall throughput was
1.926 seconds per input science image, excluding SFFT-difference-image
PhotUtils catalog loading and subsequent source cross-matching.

Source loading and lightcurves
====================================

Python PhotUtils PSF-fit catalogs from positive and negative SFFT difference
images supplied 259,157,881 records to Sources child PostgreSQL tables,
scaling to about 10 billion sources for the entire GBTDS survey. Loading took
~3.5 hours with 8 parallel processes; both the VPO and database-server
machines have 8 vCPUs.

Cross-matching sources with astronomical objects (AstroObjects) across all
358 source fields overlapped by this test took 19.2 hours with 8 parallel
processes. The ``match_radius = 0.00001528`` degrees (half a Roman WFI pixel)
includes matches across field boundaries for sources near field edges.
The Merges_<field> and AstroObjects_<fields> PostgreSQL tables received
88,747,880 AstroObjects records and 215,703,276 Merges records (lightcurve
data points). Cross-boundary matching, where the match radius extends across
a field boundary, added 33,261 merges, an increase of 0.0154%.

A separate process after cross-matching updates lightcurve statistics in
AstroObjects_<fields> and deletes records with no associated sources in
Merges_<field>. It builds a new Q3C index on (meanra, meandec) for all
AstroObjects_<fields> tables, then sets them to logged, clusters and analyzes
them, and explicitly vacuums them at the end. This took 15.9 hours with
8 parallel processes.


7/22/2026
************************************

Similar to the 7/6/2026 test, with these changes:

* SOC-sim ASDF images have correctly injected variable sources and corrected WCS:

  .. code-block::

      s3://socsims-fakesrc-asdf-20260709/

* FITS conversions for RAPID input use 5th-order TAN-SIP distortion:

  .. code-block::

      s3://socsims-fakesrc-fits-20260709-lite/

* A newly implemented standalone RAPID reference-image pipeline, integrated
  into the VPO, successfully generated all 109 required reference images for
  ``fid = 8`` before science processing. They are registered under
  ``ppid = 12`` in the RAPID operations database socsimsdb.

Setup
====================================

Reference-image configuration:

.. code-block::

    [REF_IMAGE]
    # Pipeline number of reference-image pipeline.
    ppid = 12
    # Size of reference image, if it is to be generated.
    naxis1_refimage = 7000
    naxis2_refimage = 7000
    # SCA is 0.11 arcsec per pixel or 0.000030555555556 degrees
    cdelt1_refimage = -0.000030555555556
    cdelt2_refimage = 0.000030555555556
    # Reference image is NOT rotated (CROTA2 = 0.0 degrees)
    crota2_refimage = 0.0
    # Need to limit number of reference-image input frames; otherwise AWS Batch job may time out.
    min_n_images_to_coadd = 2
    max_n_images_to_coadd = 25

Virtual Pipeline Operator (VPO) invocation:

.. code-block::

    export DBNAME=socsimsdb
    export STARTDATETIME="2027-10-01 07:12:00"
    export ENDDATETIME="2027-10-02 00:00:00"
    export STARTREFIMMJDOBS=0.0
    export ENDREFIMMJDOBS=999999.9
    export RUNFID=8

    python3.11 /code/pipeline/virtualPipelineOperator.py 20260706 >& virtualPipelineOperator_20260706.out &

Results
====================================

Pipeline product files:

.. code-block::

    s3://rapid-product-files/20260722/

Pipeline exit codes:

.. code-block::

    socsimsdb=> select ppid,exitcode,count(*) from jobs where cast(launched as date) = '20260722' group by ppid, exitcode order by ppid, exitcode;
     ppid | exitcode | count
    ------+----------+-------
       12 |        0 |   109
       15 |        0 |  7272
       17 |        0 |  7067
       17 |          |   205
    (4 rows)

One AWS-Batch RAPID post-processing pipeline failed, presumably because of
a network glitch:

.. code-block::

    CannotPullContainerError: failed to resolve ref
    public.ecr.aws/<ecr-public-alias>/rapid_science_pipeline:latest
    for schema1 conversion: failed to do request:
    Head "https://public.ecr.aws/v2/<ecr-public-alias>/rapid_science_pipeline/manifests/latest":
    dial tcp 75.2.101.78:443: i/o timeout

The failure caused database registration to quit early, leaving 205 pipeline
instances with null exitcodes. The VPO and pipeline infrastructure need
further work to identify and rerun failed pipelines.


8/21/2026
************************************

Relative to the 7/22/2026 test, the observation-date range shifts 6 days
later, yielding slightly more science images. Other changes:

* Jacob's recent script modification and new injection catalogs give the
  injected variable sources a shorter variability time scale, better suited
  to the available one week of simulated observations. These SOC-sim ASDF
  images also have corrected WCS:

  .. code-block::

      s3://socsims-fakesrc-asdf-20260807/

* FITS conversions for RAPID input use 5th-order TAN-SIP distortion. A
  4th-order comparison in a spot test showed a maximum deviation of
  0.0533 pixels between ASDF and FITS computed sky positions:

  .. code-block::

      s3://socsims-fakesrc-fits-20260807-lite/

* Jacob compiled F146 PSF models (both sci and ref) for the SOC sims based on the CRDS
  reference-file ePSFs (``fid=8`` only). The GBTDS strategy keeps orientation
  and SCA fixed; Jacob modeled the ref PSFs to mimic this, with one per SCA.
  Science-image PSF records had to be inserted into the PSFs database table
  with ``vbest = 1``. His three pipeline changes, all off by default, make
  SFFT masking settings explicit, allow ZOGY's SN/SR to use uncertainty maps
  instead of source-dominated image scatter, and repair extreme artifact
  pixels before differencing. Settings for this run:

  .. code-block::

      zogy_sn_sr_from_uncertainty_maps = True
      refimage_psf_filename = refimage_psf_f146_scaSCAID.fits
      repair_extreme_artifact_pixels = True

* All 109 new reference images for ``fid = 8`` were generated before science
  processing and registered under ``ppid = 12`` in the RAPID operations
  database socsimsdb.

Setup
====================================

Reference-image configuration:

.. code-block::

    [REF_IMAGE]
    # Pipeline number of reference-image pipeline.
    ppid = 12
    # Size of reference image, if it is to be generated.
    naxis1_refimage = 7000
    naxis2_refimage = 7000
    # SCA is 0.11 arcsec per pixel or 0.000030555555556 degrees
    cdelt1_refimage = -0.000030555555556
    cdelt2_refimage = 0.000030555555556
    # Reference image is NOT rotated (CROTA2 = 0.0 degrees)
    crota2_refimage = 0.0
    # Need to limit number of reference-image input frames; otherwise AWS Batch job may time out.
    min_n_images_to_coadd = 2
    max_n_images_to_coadd = 25

Virtual Pipeline Operator (VPO) invocation:

.. code-block::

    export DBNAME=socsimsdb
    export STARTDATETIME="2027-10-07 07:12:00"  
    export ENDDATETIME="2027-10-08 00:00:00"
    export STARTREFIMMJDOBS=0.0
    export ENDREFIMMJDOBS=999999.9
    export RUNFID=8

    python3.11 /code/pipeline/virtualPipelineOperator.py 20260821 >& virtualPipelineOperator_20260821.out &

Results
====================================

Pipeline product files:

.. code-block::

    s3://rapid-product-files/20260821/

Pipeline exit codes (``exitcode = 0`` is normal):

.. code-block::

    socsimsdb=> select ppid,exitcode,count(*) from jobs where cast(launched as date) = '20260821' group by ppid, exitcode order by ppid, exitcode;
     ppid | exitcode | count
    ------+----------+-------
       12 |        0 |   109
       15 |        0 |  7380
       17 |        0 |  7380
    (3 rows)

Lightcurve extraction from PSF-fit SFFT-difference-image catalogs:
AstroObjects_<field>, AstroObjectsMeta_<field>, and Merges_<field> record
counts include contributions from the 7/22/26 socsims test, whose science
images were observed 6 days earlier.

=========================================================================================  =====================
Item                                                                                        Number
=========================================================================================  =====================
Number of sources loaded into Sources_<obsdate>_<sca> database tables                        224,033,253
Number of sources loaded into Sources_<obsdate>_<sca> database tables with flags = 0         159,059,007
Number of merges inside AND outside field, loaded into Merges_<field> database tables        391,835,197
Number of astroObjects loaded into AstroObjects_<field> database tables                       83,494,595
Number of records loaded into AstroObjectsMeta_<field> database tables                        83,494,595
Number of Sources_<obsdate>_<sca> database tables                                                     18
Number of Merges_<field> database tables                                                             359
Number of AstroObjects_<field> database tables                                                       359
Number of AstroObjectsMeta_<field> database tables                                                   359
=========================================================================================  =====================
