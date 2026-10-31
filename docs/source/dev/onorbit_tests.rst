Testing with On-Orbit HLTDS-like Data
####################################################

Overview
************************************

These tests run the RAPID pipeline on on-orbit Roman WFI L2 files whose observations are like
those of the High Latitude Time Domain Survey (HLTDS, i.e. extragalactic with a given field observed
at different position angles and on different SCAs).

The Roman program ID of the data is recorded with each test, as read from the L2 filenames.

Each test follows the same three steps:

1. Stream the L2 files for the program from MAST into the ingest S3 bucket
   with ``pipeline/stream_l2_files.py``, for example::

       python3.11 /code/pipeline/stream_l2_files.py <program> >& stream_l2_files_<date>.out

2. Convert the ASDF files to FITS and register them in the operations database
   with ``pipeline/ingestL2Files.py`` (see :doc:`/pl/l2_ingest`)::

       python3.11 /code/pipeline/ingestL2Files.py >& ingestL2Files_<date>.out

3. Run the RAPID pipeline with the Virtual Pipeline Operator (VPO) (see :doc:`/ops/vpo`)::

       python3.11 /code/pipeline/virtualPipelineOperator.py <date> >& virtualPipelineOperator_<date>.out

Adding a test entry
************************************
.. code-block::
    python docs/make_onorbit_test_entry.py virtualPipelineOperator_<date>.out \
        --stream-log stream_l2_files_<date>.out \
        --ingest-log ingestL2Files_<date>.out \
        --job-logs-dir <dir>

Commissioning Changes 2026-10-21
************************************
* Added code to stream l2 files from MAST given an API key
* Created private s3 buckets
* Created new aws batch job definitions that point to the commissioning branch
* Use per-SCA/filter orbit PSFs computed using the ice monitor program (1043)
* SFFT segmentation masking on;
* bright-source mask value 1e6;
* minimum reference magnitude 10.
* Added a surveys table and populated alerts with observation information
* Allows observations in a run to be selected by program_id (RUNPROGRAMIDS)
* Pixels flagged in the DQ array are set to NaN in SCI, ERR and VAR
* Set SEEING_FWHM to the appropriate units of arcsec (previously pixels)
* Set DEBLEND_THRESH=32
* Change FWHM calculations to be computed for likely point sources
* Catalogs reported in AB magnitudes
* Improved masking to produce a better SFFT kernel

10/21/2026
************************************

This was the first large scale run of commissioning data. The majority of the run is centered
on program 1062 which collected 4 epochs of data on the Euclid Deep Field South field. Two epochs
executed on 2026-09-29 and two on 2026-10-11. Each epoch had two pointings. The code ran through
alert production. One notable issue is that real-bogus model rejected any cutout with a NaN in it,
resulting in a very low fraction of images scored. Reference files were build off of the first set
of observations in the first epoch.

Roman program ID(s) of the L2 files: ``1053``, ``1062`` (from the L2 filenames).

L2 file ingest
----------------------------------

The L2 files were converted to FITS and registered in the database with ``ingestL2Files.py``, starting at 2026-10-21T20:04:18 PT.

=======================================  ======================
Item                                     Number
=======================================  ======================
ASDF files in the ingest S3 bucket                        1,757
Already ingested (skipped)                                   16
Redelivered (re-ingested)                                     0
L2 files on the work list                                 1,741
L2 files ingested                                         1,741
L2 files failed                                               0
L2 files not attempted                                        0
Elapsed time to ingest L2 files (sec)      9,238.1 (2.57 hours)
=======================================  ======================

The ingested files cover 18 SCAs; by filter, F062: 297, F087: 278, F106: 282, F129: 298, F158: 288, F184: 298; by Roman program ID, ``1053``: 29, ``1062``: 1,712.

======================================================================================================================================================================================  ===========  ===========  ===========
Ingest step (per L2 file)                                                                                                                                                               Min (sec)    Med (sec)    Max (sec)
======================================================================================================================================================================================  ===========  ===========  ===========
Download ASDF file from S3 bucket                                                                                                                                                               1.2          4.3          6.5
Convert ASDF file to FITS file                                                                                                                                                                  2.9         11.5         21.8
Gzip FITS file                                                                                                                                                                                  9.4         13.7         17.0
Upload FITS file to S3 bucket                                                                                                                                                                   0.8          1.0          8.1
Download PSF file for fid,sca from S3 bucket                                                                                                                                                    1.5          2.0          3.0
Compute limiting magnitude for L2 file                                                                                                                                                          7.9         10.5         13.9
Register L2 file in database                                                                                                                                                                    8.4         11.5         15.9
Clean up work directory for L2 file                                                                                                                                                             0.1          0.1          0.6
Total time to ingest one L2 file                                                                                                                                                               23.1         42.3         56.7
Download PSF file for fid,sca from S3 bucket.7236855030059814 (f184_dir/r0106201001001006001_0002_wfi11_f184_cal.asdf)Elapsed time in seconds to clean up work directory for L2 file            0.2          0.2          0.2
======================================================================================================================================================================================  ===========  ===========  ===========

Here are details about how the test was executed via the Virtual Pipeline Operator (VPO), version 1.11:

.. code-block::

    export DBNAME=commissioning
    export STARTDATETIME="2026-09-25 07:12:00"
    export ENDDATETIME="2026-10-12 00:00:00"
    export STARTREFIMMJDOBS=61312.10
    export ENDREFIMMJDOBS=61312.227
    export RUNFID=None

    python3.11 /code/pipeline/virtualPipelineOperator.py 20261021 >& virtualPipelineOperator_20261021.out &

The VPO run was registered as processing request ``reqid = 9``.
The pipeline product files are located in the usual place:

.. code-block::

    s3://rapid-commissioning-product-files/20261021/req9/

The per-job pipeline logs are located here:

.. code-block::

    s3://rapid-commissioning-pipeline-logs/20261021/req9/

Here is a summary of the pipeline instances run on AWS Batch:

==========================================  ===========  ===========  ========
Pipeline                                    Instances    Succeeded    Failed
==========================================  ===========  ===========  ========
Reference-image pipeline (``ppid = 12``)            426          426         0
Science pipeline (``ppid = 15``)                    912          912         0
Post-processing pipeline (``ppid = 17``)            912          912         0
==========================================  ===========  ===========  ========

No AWS Batch jobs needed to be resubmitted.

Error and warning messages in the VPO STDOUT (numbers replaced by ``N``), most frequent first:

.. code-block::

      1326  *** Warning: Failed to download job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/job_config_jidN.done
       899  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.done
       894  *** Warning: Failed to download xsource_dbload_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/xsource_dbload_jidN.done
       881  *** Warning: Failed to download source_dbload_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/source_dbload_jidN.done
        35  *** Warning: n_images_to_coadd (N) < min_n_images_to_coadd (N) for rid,field,fid = null,N,N; quitting...
         2  *** Warning: Failed to download job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/job_config_jidN.donequery = select * from registerR
         2  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.doneresponse =
         1  *** Warning: Failed to download job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/job_config_jidN.done----> ppid = N
         1  *** Warning: Failed to download job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/job_config_jidN.done2N-N-N N:N:N:N:N
         1  *** Warning: Failed to download job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/job_config_jidN.done----> fid = N
         1  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.done----> pid = N
         1  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.done('',)
         1  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.done None
         1  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.donedownload_file
         1  *** Warning: Failed to download postproc_job_config_jidN.done from s3://rapid-commissioning-product-files/N/reqN/jidN/postproc_job_config_jidN.done*** Warning:

The VPO terminated with ``exitcode = 0``.

Overall timing
----------------------------------

The VPO started at 2026-10-21T23:01:28 PT and finished at 16:10:50 PT, a total of 61,762 seconds (17.16 hours).
The breakdown by VPO stage is shown below.

========================================================================  ========================
VPO stage                                                                 Elapsed time (sec)
========================================================================  ========================
Launching reference-image pipelines                                                          176.5
Waiting for reference-image-pipeline AWS Batch jobs to finish                              1,351.8
Registering reference-image pipeline metadata into operations database                       100.4
Launching science pipelines                                                                  349.8
Waiting for science-pipeline AWS Batch jobs to finish                                      1,461.6
Registering science-pipeline metadata into operations database                               222.4
Launching post-processing pipelines                                                          348.9
Waiting for post-processing AWS Batch jobs to finish                                         266.2
Registering post-processing metadata into operations database                                 33.7
Loading Sources database tables (PSF-fit SFFT catalogs)                                      100.0
Loading XSources database tables (SExtractor SFFT catalogs)                                   96.0
Cross-matching Sources with AstroObjects                                                      75.2
Cross-matching XSources with XAstroObjects                                                    72.1
Computing statistics for AstroObjects                                                         44.3
Computing statistics for XAstroObjects                                                        30.6
Deleting not-best Merges records                                                               6.3
Deleting not-best XMerges records                                                              6.3
Producing alerts                                                            57,015.3 (15.84 hours)
Resubmitting failed AWS Batch jobs (3 checks)                                                  5.0
Total time to run VPO                                                       61,762.5 (17.16 hours)
========================================================================  ========================

The longest stage was producing alerts, at 57,015 seconds (15.84 hours), 92% of the total.

For the 912 science images, this is 67.7 seconds per input science image overall.

Reference-image pipeline
----------------------------------

The 426 reference-image pipeline instance(s) ran from 23:04:11 to 23:25:32 PT (0.36 hours of wall-clock time).
The slowest instance (jid = 49) took 1,177 seconds; the fastest (jid = 600) took 168 seconds.
The summed run time over all 426 instance(s) is 52.2 hours.

============================================================  ===========  ===========  ===========
Reference-image pipeline step (426 instance(s))               Min (sec)    Med (sec)    Max (sec)
============================================================  ===========  ===========  ===========
Set up inputs for awaicgen                                           10.2         20.9         42.6
Execute awaicgen                                                     23.6         49.9        103.5
Generate reference-image SExtractor catalog                           6.4         11.9         22.0
Generate reference-image PhotUtils catalog                           59.4        289.3        983.9
Downloading or generating reference image                           153.9        399.6      1,153.6
Uploading reference image to S3 product bucket                       11.2         16.1         25.5
Uploading products at pipeline end to S3 product bucket               0.0          0.0          0.5
Total time to run one instance of reference-image pipeline          168.0        417.2      1,177.1
============================================================  ===========  ===========  ===========

Science pipeline
----------------------------------

The 912 science pipeline instance(s) ran from 23:31:18 to 23:57:05 PT (0.43 hours of wall-clock time).
The slowest instance (jid = 761) took 1,356 seconds; the fastest (jid = 1471) took 277 seconds.
The summed run time over all 912 instance(s) is 163.8 hours.
Science images processed by Roman program ID: ``1062``: 912.

================================================================================  ===========  ===========  ===========
Science pipeline step (912 instance(s))                                           Min (sec)    Med (sec)    Max (sec)
================================================================================  ===========  ===========  ===========
Downloading science image                                                                 1.1          1.3          3.0
Uploading science image to product S3 bucket                                              0.5          0.7          1.9
Downloading or generating reference image                                                 3.3          3.8          5.9
Generating science-image catalog                                                          6.8         10.6         17.6
Swarping images                                                                           7.4         10.6         12.3
Running bkgest on science image                                                           3.4          4.3          5.1
Running bkgest on reference image                                                         3.1          4.2          5.3
Running gainMatchScienceAndReferenceImages                                                5.0          9.6         21.3
Replacing NaNs, applying image offsets, etc.                                              3.2          4.9        103.7
Uploading intermediate FITS files to product S3 bucket                                    1.8          2.1          8.6
Running ZOGY                                                                             19.7         39.8         44.7
Masking ZOGY difference image                                                             0.9          1.2          1.5
Running SExtractor on positive ZOGY difference image                                      3.6          7.7         24.8
Running SExtractor on negative ZOGY difference image                                      2.0          4.7         17.5
Generating PSF-fit catalog on positive ZOGY difference image                             16.7         50.0        192.5
Generating PSF-fit catalog on negative ZOGY difference image                             14.4         48.3        183.7
Uploading main products to S3 bucket                                                      3.8          4.6         23.8
Running SFFT                                                                             76.7        148.1        175.1
Uploading SFFT difference image to S3 product bucket                                      3.1          4.4          7.0
Running SExtractor on positive SFFT difference images                                    10.2         17.9         51.2
Running SExtractor on negative SFFT difference images                                     6.6         13.8         26.4
Uploading SFFT-diffimage SExtractor catalogs to S3 product bucket                         0.1          0.1          0.5
Generating PSF-fit catalog on positive SFFT difference image                             21.1         51.9        199.1
Generating PSF-fit catalog on negative SFFT difference image                             20.0         49.5        164.7
Uploading SFFT-diffimage PSF-fit catalogs to S3 product bucket                            0.9          1.1          6.0
Computing naive difference images                                                         0.6          0.8          1.1
Uploading naive difference images to S3 product bucket                                    0.6          0.7          4.2
Running SExtractor on positive naive difference image                                     3.1          5.4         11.8
Running SExtractor on negative naive difference image                                     1.3          2.8          9.1
Uploading SExtractor catalogs for naive difference images to S3 product bucket            0.4          0.5          2.9
Generating PSF-fit catalog on positive naive difference image                            20.0         57.3        183.6
Generating PSF-fit catalog on negative naive difference image                             7.1         27.9        141.7
Uploading PSF-fit catalogs for naive difference images to S3 product bucket               0.8          0.9          3.6
Uploading products at pipeline end to S3 product bucket                                   0.0          0.0          0.5
Total time to run one instance of science pipeline                                      277.0        601.4      1,355.8
================================================================================  ===========  ===========  ===========

Real-bogus scoring of the catalog sources, summed over the science-pipeline instances (the last columns count the sources not scored, by reason):

==============================================  ===========  =========  ============  ====================================  ================================  ==========================================
Catalog                                         Sources      Scored     Scored (%)    Not scored: centroid_outside_image    Not scored: nonfinite_centroid    Not scored: nonfinite_or_unusable_cutout
==============================================  ===========  =========  ============  ====================================  ================================  ==========================================
``sfftdiffimage_masked.txt``                      1,101,790      7,503           0.7                                    65                           564,294                                     529,928
``sfftdiffimage_masked_negative.txt``             1,214,727      6,350           0.5                                    51                           667,174                                     541,152
``sfftdiffimage_masked_psfcat.txt``               2,484,174     53,977           2.2                                   736                                 0                                   2,429,461
``sfftdiffimage_masked_psfcat_negative.txt``      2,346,900     50,504           2.2                                   577                                 0                                   2,295,819
All catalogs                                      7,147,591    118,334           1.7                                 1,429                         1,231,468                                   5,796,360
==============================================  ===========  =========  ============  ====================================  ================================  ==========================================

Sources and real-bogus scoring by SCA and filter
------------------------------------------------

Each value is the median over the science images of that SCA and filter (or, in the All SCAs row and All filters column, over all the images of that filter or SCA); ``--`` means no image (or, for the above-threshold percent, no image with that count). The SExtractor catalogs are ``sfftdiffimage_masked[_negative].txt``; the photutils catalogs are ``sfftdiffimage_masked_psfcat[_negative].txt``.

Number of science images:

==========  ======  ======  ======  ======  ======  ======  =============
SCA         R062    Z087    Y106    J129    H158    F184    All filters
==========  ======  ======  ======  ======  ======  ======  =============
01               8       8       8       8       8       8             48
02              16      16      16      16      16      16             96
03              12      12      12      12      12      12             72
04               4       4       4       4       4       4             24
05               8       8       8       8       8       8             48
06              10      10      10      10      10      10             60
07               6       6       6       6       6       6             36
08               4       4       4       4       4       4             24
09               6       6       6       6       6       6             36
10              12      12      12      12      12      12             72
11              10      10      10      10      10      10             60
12               6       6       6       6       6       6             36
13               6       6       6       6       6       6             36
14               6       6       6       6       6       6             36
15               8       8       8       8       8       8             48
16              10      10      10      10      10      10             60
17              10      10      10      10      10      10             60
18              10      10      10      10      10      10             60
All SCAs       152     152     152     152     152     152            912
==========  ======  ======  ======  ======  ======  ======  =============

Number of sources, SExtractor catalog of the positive difference image (``sfftdiffimage_masked.txt``):

==========  =======  =======  =======  =======  =======  =======  =============
SCA         R062     Z087     Y106     J129     H158     F184     All filters
==========  =======  =======  =======  =======  =======  =======  =============
01            1,612    1,498    1,549    1,552    1,542    1,991          1,690
02            1,162    1,490    1,523    1,532    1,528    1,688          1,521
03            1,163    1,416    1,422    1,461    1,451    1,648          1,390
04              346      514      394      644      460      438            444
05              980    1,213    1,250    1,378    1,562    1,612          1,362
06            1,872    2,192    2,294    2,190    2,290    2,690          2,172
07              480      464      502      573      608      661            587
08              270      352      412      474      526      862            440
09              289      327      426      554      614      598            492
10            2,303    2,414    2,474    2,404    2,386    2,920          2,504
11              908    1,172    1,172    1,206    1,258    1,334          1,169
12              716      799      727      807      948      578            765
13              851      580      605      690      736      777            696
14              472      574      634      758      766      734            676
15              740      892      943    1,022    1,082    1,194            901
16              706      945    1,004    1,094    1,116    1,162            904
17              864      913    1,056      962      902      904            908
18            1,608    1,531    1,584    1,450    1,524    1,686          1,560
All SCAs        870      980    1,063    1,094    1,082    1,210          1,016
==========  =======  =======  =======  =======  =======  =======  =============

Number of sources, SExtractor catalog of the negative difference image (``sfftdiffimage_masked_negative.txt``):

==========  =======  =======  =======  =======  =======  =======  =============
SCA         R062     Z087     Y106     J129     H158     F184     All filters
==========  =======  =======  =======  =======  =======  =======  =============
01            1,998    2,090    2,125    2,114    2,093    2,300          2,065
02            1,751    1,723    1,891    1,838    1,917    2,185          1,856
03            1,332    1,644    1,599    1,616    1,568    1,785          1,578
04              348      414      396      505      418      411            422
05            1,096    1,340    1,366    1,526    1,590    1,643          1,443
06            2,284    2,561    2,744    2,668    2,637    2,899          2,557
07              531      580      636      638      670      708            636
08              330      402      456      488      528      707            472
09              318      422      540      592      646      589            570
10            3,374    3,606    3,412    3,381    3,312    3,972          3,458
11            1,021    1,269    1,328    1,450    1,436    1,549          1,274
12              550      551      670      800      832      642            642
13              682      627      652      684      672      697            676
14              576      631      642      707      720      752            680
15              753      938    1,014    1,054    1,096    1,183            870
16              772      916    1,002    1,024    1,036    1,080            880
17              946    1,061    1,180    1,198    1,186    1,210          1,095
18              950    1,133    1,132    1,182    1,218    1,284          1,135
All SCAs        842    1,026    1,038    1,108    1,145    1,210          1,056
==========  =======  =======  =======  =======  =======  =======  =============

Number of sources, photutils catalog of the positive difference image (``sfftdiffimage_masked_psfcat.txt``):

==========  =======  =======  =======  =======  =======  =======  =============
SCA         R062     Z087     Y106     J129     H158     F184     All filters
==========  =======  =======  =======  =======  =======  =======  =============
01            3,576    3,564    3,504    3,480    3,566    4,553          3,720
02            2,128    2,408    2,326    2,446    2,610    3,479          2,396
03            3,062    3,414    3,182    3,203    3,314    4,618          3,484
04              910    1,102      934    1,406    1,227    1,298          1,194
05            2,317    2,721    2,613    2,852    3,277    4,166          2,958
06            6,162    6,534    6,081    5,976    6,288    8,412          6,104
07              790      924      946    1,050    1,241    1,370          1,169
08              844      990    1,050    1,132    1,324    2,294          1,116
09              758      917      952    1,122    1,420    1,552          1,220
10            6,364    6,538    6,235    6,144    6,218    7,932          6,342
11            2,223    2,546    2,424    2,486    2,573    3,680          2,472
12            1,842    1,896    1,862    2,094    2,412    1,694          1,940
13            2,245    1,944    1,776    1,986    2,148    2,189          1,989
14            1,486    1,517    1,583    1,708    1,898    2,024          1,626
15            1,760    1,906    1,870    2,037    2,304    2,956          2,096
16            1,300    1,466    1,446    1,476    1,708    2,352          1,507
17            1,766    1,837    2,192    2,058    1,672    2,162          2,000
18            2,119    1,918    1,892    1,870    2,099    2,796          1,934
All SCAs      1,916    1,986    2,120    2,022    2,120    2,667          2,153
==========  =======  =======  =======  =======  =======  =======  =============

Number of sources, photutils catalog of the negative difference image (``sfftdiffimage_masked_psfcat_negative.txt``):

==========  =======  =======  =======  =======  =======  =======  =============
SCA         R062     Z087     Y106     J129     H158     F184     All filters
==========  =======  =======  =======  =======  =======  =======  =============
01            3,640    3,691    3,642    3,622    3,684    4,556          3,733
02            2,870    2,657    2,840    2,788    3,168    4,074          2,852
03            2,875    3,271    2,918    2,874    3,038    4,321          3,235
04              882    1,052      920    1,052    1,050    1,134          1,032
05            2,300    2,636    2,512    2,770    3,106    4,091          2,920
06            4,846    5,328    4,863    4,568    4,814    6,986          4,852
07              988    1,010    1,053    1,058    1,196    1,322          1,112
08              904    1,060    1,046    1,171    1,278    1,870          1,126
09              770      917    1,094    1,125    1,262    1,406          1,158
10            6,528    6,855    6,194    6,099    6,004    7,774          6,175
11            2,049    2,272    2,156    2,325    2,366    3,400          2,276
12            1,623    1,800    1,830    2,000    2,088    1,641          1,855
13            2,330    2,076    1,920    1,969    2,008    2,107          2,025
14            1,460    1,428    1,514    1,539    1,550    1,816          1,512
15            1,578    1,835    1,720    1,912    2,092    2,650          1,928
16            1,212    1,388    1,319    1,399    1,536    1,954          1,385
17            1,726    1,821    1,990    2,056    1,923    2,472          1,944
18            1,500    1,500    1,369    1,404    1,545    2,014          1,449
All SCAs      1,708    1,894    1,864    1,943    1,968    2,344          1,994
==========  =======  =======  =======  =======  =======  =======  =============

Percent of sources real-bogus scored, SExtractor catalog of the positive difference image (``sfftdiffimage_masked.txt``):

==========  ======  ======  ======  ======  ======  ======  =============
SCA         R062    Z087    Y106    J129    H158    F184    All filters
==========  ======  ======  ======  ======  ======  ======  =============
01             0.0     0.0     0.0     0.0     0.0     0.0            0.0
02             1.4     1.2     1.0     1.2     1.1     1.3            1.2
03             0.4     0.4     0.3     0.2     0.2     0.3            0.3
04             0.9     0.3     0.2     0.1     0.4     0.1            0.3
05             0.3     0.2     0.3     0.3     0.3     0.2            0.3
06             0.0     0.0     0.0     0.0     0.0     0.0            0.0
07             0.0     0.0     0.0     0.0     0.0     0.0            0.0
08             0.2     0.6     1.2     0.8     0.5     0.7            0.6
09             1.1     0.3     0.8     0.4     0.4     0.4            0.5
10             0.0     0.0     0.0     0.0     0.0     0.0            0.0
11             0.2     0.3     0.2     0.2     0.3     0.4            0.2
12             1.6     1.5     0.8     0.6     1.0     0.6            1.2
13             0.2     0.0     0.0     0.1     0.0     0.1            0.0
14             0.3     0.4     0.2     0.2     0.3     0.3            0.3
15             1.6     1.4     1.4     1.5     1.6     1.8            1.6
16             2.2     2.4     2.3     2.4     2.2     2.1            2.2
17             2.6     1.4     1.7     1.4     1.1     1.7            1.6
18             0.3     0.3     0.1     0.3     0.2     0.1            0.2
All SCAs       0.3     0.3     0.2     0.3     0.2     0.2            0.3
==========  ======  ======  ======  ======  ======  ======  =============

Percent of sources real-bogus scored, SExtractor catalog of the negative difference image (``sfftdiffimage_masked_negative.txt``):

==========  ======  ======  ======  ======  ======  ======  =============
SCA         R062    Z087    Y106    J129    H158    F184    All filters
==========  ======  ======  ======  ======  ======  ======  =============
01             0.0     0.0     0.0     0.0     0.0     0.0            0.0
02             0.8     1.1     1.0     0.9     0.8     0.8            0.8
03             0.2     0.2     0.3     0.2     0.3     0.3            0.3
04             0.8     0.2     0.3     0.6     0.1     0.0            0.2
05             0.3     0.2     0.3     0.2     0.1     0.2            0.2
06             0.1     0.1     0.0     0.1     0.0     0.1            0.1
07             0.0     0.0     0.0     0.0     0.0     0.0            0.0
08             1.8     1.5     2.0     2.0     1.2     1.3            1.6
09             1.5     0.7     0.9     0.5     0.3     0.4            0.7
10             0.0     0.0     0.0     0.0     0.0     0.0            0.0
11             0.1     0.1     0.2     0.2     0.1     0.1            0.1
12             2.0     1.6     1.7     1.1     1.2     1.0            1.5
13             0.0     0.0     0.1     0.1     0.0     0.0            0.0
14             0.4     0.9     0.6     0.7     0.3     0.3            0.6
15             1.3     1.3     1.5     1.3     1.4     1.7            1.5
16             1.1     1.4     1.1     1.2     1.3     1.3            1.2
17             2.3     1.6     1.9     1.3     1.2     1.2            1.6
18             0.7     0.3     0.3     0.2     0.2     0.3            0.3
All SCAs       0.3     0.3     0.3     0.3     0.2     0.2            0.3
==========  ======  ======  ======  ======  ======  ======  =============

Percent of sources real-bogus scored, photutils catalog of the positive difference image (``sfftdiffimage_masked_psfcat.txt``):

==========  ======  ======  ======  ======  ======  ======  =============
SCA         R062    Z087    Y106    J129    H158    F184    All filters
==========  ======  ======  ======  ======  ======  ======  =============
01             0.0     0.0     0.0     0.0     0.0     0.0            0.0
02             3.0     3.1     2.6     2.8     2.7     2.7            2.7
03             1.1     1.1     1.1     1.1     1.2     1.0            1.1
04             2.9     2.6     2.3     3.2     2.7     3.1            2.8
05             1.5     1.4     1.4     1.3     1.2     1.3            1.4
06             0.1     0.1     0.1     0.2     0.2     0.2            0.2
07             0.2     0.0     0.0     0.1     0.1     0.1            0.1
08            14.9    15.1    14.9    14.4    16.5    15.4           15.1
09             6.5     6.4     5.6     5.6     6.4     5.9            6.0
10             0.0     0.0     0.0     0.0     0.0     0.0            0.0
11             1.3     1.5     1.0     1.0     1.4     1.0            1.2
12            11.1    10.3     9.2     9.4    10.3    11.5           10.2
13             0.8     0.8     0.8     1.0     0.9     1.3            0.9
14             3.8     3.7     4.1     3.3     3.2     4.0            3.7
15             6.4     7.0     6.5     5.8     6.5     6.4            6.5
16             7.2     7.0     7.3     6.6     7.4     7.4            7.2
17             6.2     5.6     6.7     6.6     5.5     5.0            6.0
18             0.9     1.1     0.8     1.3     1.2     1.1            1.1
All SCAs       1.9     1.7     1.5     1.8     1.7     1.7            1.7
==========  ======  ======  ======  ======  ======  ======  =============

Percent of sources real-bogus scored, photutils catalog of the negative difference image (``sfftdiffimage_masked_psfcat_negative.txt``):

==========  ======  ======  ======  ======  ======  ======  =============
SCA         R062    Z087    Y106    J129    H158    F184    All filters
==========  ======  ======  ======  ======  ======  ======  =============
01             0.0     0.0     0.0     0.1     0.0     0.1            0.0
02             2.5     2.8     2.9     2.9     2.7     2.5            2.7
03             1.1     1.1     1.1     1.0     1.1     1.0            1.1
04             3.2     2.5     2.8     2.4     1.9     2.8            2.5
05             1.7     1.6     1.5     1.3     1.2     1.2            1.5
06             0.2     0.2     0.2     0.2     0.2     0.2            0.2
07             0.2     0.1     0.1     0.2     0.1     0.1            0.2
08            15.1    15.1    15.3    15.3    16.1    14.9           15.2
09             6.2     6.3     6.3     5.9     5.1     5.3            5.8
10             0.1     0.1     0.1     0.0     0.0     0.0            0.0
11             1.1     1.3     1.1     0.9     1.0     1.1            1.1
12             9.9     9.7    10.6     9.2     9.1    11.4            9.9
13             0.7     0.6     1.1     0.9     0.8     0.9            0.9
14             3.9     4.1     3.0     3.4     3.1     3.8            3.6
15             6.5     7.1     6.8     6.5     6.6     6.0            6.7
16             7.0     7.1     6.1     6.7     6.4     6.1            6.4
17             6.2     7.6     7.0     6.4     6.8     6.7            6.7
18             1.2     1.0     1.2     1.4     0.8     0.9            1.1
All SCAs       2.0     1.8     1.7     1.7     1.6     1.5            1.7
==========  ======  ======  ======  ======  ======  ======  =============

Percent of scored sources above the real-bogus threshold: not available (the job logs predate this count and no catalogs were read).

Alert production
----------------------------------

Per SCA, alert production took 39 to 1,726 seconds (median 382 seconds); summed over the 912 SCAs, 434,966 seconds (120.82 hours).

====================================================================  =================
Item                                                                  Number
====================================================================  =================
Flagged detections skipped (flags <> 0, not alertable)                          743,305
Alertable detections dropped by the cap of 2000 per SCA (188 SCAs)              164,249
Number of SCAs processed successfully                                               912
Number of SCAs failed                                                                 0
Number of alerts archived                                                     1,030,029
Sources whose alert could not be built                                                0
Total size of Avro alert archives, bytes                                195,434,391,722
Alerts with prior detections                                                    658,373
Prior-detection records in the alerts                                         3,356,286
Most prior detections in one alert                                                   38
Alerts with forced photometry                                                 1,014,470
====================================================================  =================

Cross-matches in the alerts (null means the match was not attempted):

============================  =========  ===========  ===========
Cross-match                   Matched    Empty        Null
============================  =========  ===========  ===========
Solar-system objects                  0            0    1,030,029
Reference-catalog stars         686,398      343,631            0
Reference-catalog galaxies      694,642      335,387            0
NED                             553,466      475,153        1,410
NED-LVS                          15,243    1,012,284        2,502
Gaia                             24,344    1,004,539        1,146
============================  =========  ===========  ===========

Alerts with each cutout present: cutoutDifference: 1,030,029, cutoutScience: 1,030,029, cutoutReference: 994,717.

Forced photometry ran on 911 SCAs, writing 11,469,248 new rows to 940,494 files. It took 86% of the summed per-SCA alert-production time.

========================  ==========================  ===================  ===================
Forced-photometry step    Summed (sec)                Med per SCA (sec)    Max per SCA (sec)
========================  ==========================  ===================  ===================
read                         103,324.8 (28.70 hours)                 96.1                285.3
search                                          86.9                  0.1                  0.9
stage+fit                    220,603.9 (61.28 hours)                162.9              1,216.8
write                         48,921.8 (13.59 hours)                 37.2                256.1
total                       372,944.0 (103.60 hours)                319.2              1,602.8
========================  ==========================  ===================  ===================

Forced photometry failed on 1 SCA(s) (pid = 1150 after 742.2 s); the alerts of those SCAs carry null ``prvDiaForcedSources``.

Warning and error messages in the 8 alert-production thread logs (numbers replaced by ``N``; 4 tracebacks), most frequent first:

.. code-block::

      2674  WARNING alerts.providers: pid=N field=N: N sources have more than one merges_N row (several aids each); the lowest aid is used
        27  WARNING alerts.providers: pid=N: ref image grid differs from the difference image (CRPIX1: N vs N); its cutouts will be null
        17  WARNING alerts.providers: pid=N: N of N detections lie more than N" from the chip centre -- positions not on the chip (off-image PSF fits?); excluded from the N
         1  ERROR alerts.providers: pid=N: forced photometry failed after N s; alerts will carry null prvDiaForcedSources

.. TODO: Observations.