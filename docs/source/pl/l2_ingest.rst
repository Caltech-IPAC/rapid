Ingesting Roman WFI L2 Files
####################################################

Overview
************************************

``pipeline/ingestL2Files.py`` reads the Roman WFI Level 2 (L2) ASDF files in an
input S3 bucket, converts each one into a multi-extension FITS file in an output
S3 bucket, and registers it in the RAPID operations database.

It replaces a two-script workflow that had to be run in sequence:

===================================================   ==================================================================
Script                                                What it did
===================================================   ==================================================================
``sims/src/socsims/convert_socsims.py``               ASDF to FITS, plus the FITS keywords the pipeline requires.
``database/sims/db_register_socsim_files.py``         Registration of the FITS file in the ``Exposures``, ``L2Files``
                                                      and ``L2FileMeta`` database tables.
===================================================   ==================================================================

Doing both in one pass means each file is downloaded once, and is converted,
uploaded and registered while it is still on local disk, instead of being
written to S3 by one script and pulled straight back down by the other.  It
also closes the window in which a converted file sat in the output bucket with
no database row pointing at it.


The work list
************************************

The work list is *every ASDF file in the input bucket that has not yet been
ingested*.

A file counts as ingested when its output FITS file has a **current**
(``vbest > 0``) row in the ``L2Files`` database table.  That row is the only
record of the ingest that survives a restart, so it, and not the contents of the
output bucket, is what decides whether a file still has to be done.

.. important::
   The test has to be on ``vbest > 0``, not on a row existing at all.  A
   redelivered ASDF file keeps its name and is ingested again, which supersedes
   the earlier ``L2Files`` record: ``vbest`` goes to 0 on the old row and the new
   row becomes the best version.  A superseded row is therefore exactly the state
   that has to let a file back onto the work list, and counting it as ingested
   would make a redelivery invisible to this script forever.

The output bucket is listed as well, but never to decide whether a file has to
be ingested.  A converted FITS file sitting there with no current database row,
and **newer** than the ASDF file it came from, was left behind by a run that
stopped between the upload and the registration; that file is downloaded and
registered rather than converted a second time, since the conversion, and the
SIP fit inside it, is by far the most expensive step.

A converted FITS file **older** than its ASDF file is a different thing
entirely: it was made from a *previous* delivery of that file, and the
redelivery is precisely the case where the S3 object name is unchanged but the
pixels are not.  Reusing it would register the superseded data as the new
version, so it is converted afresh.  The two are told apart by the
last-modified times, which come free with the S3 listings.

Within a run, the work list is sorted by SCA and then by observation, so that
the files belonging to one exposure are spread across the list instead of being
adjacent in it.  They are then not handed to the parallel processes at the same
moment, which is what avoids the race of two processes inserting the same
``Exposures`` record simultaneously.


Output FITS layout
************************************

The output is a multi-extension FITS file, gzipped, whose S3 object name is the
input object name with ``.asdf`` or ``.asdf.gz`` replaced by ``.fits.gz``.

=========================   ====================================================================================
HDU                         Contents
=========================   ====================================================================================
0, ``PRIMARY``              Keywords only, no data (``NAXIS = 0``).
1, ``SCI``                  The science image, in DN, with the FITS-SIP WCS.
2..n                        The remaining ASDF arrays, one image HDU each (``ERR``, ``DQ``, ``VAR_POISSON``,
                            ``VAR_RNOISE``, ``VAR_FLAT``, ...), followed by any ASDF tables as binary-table
                            HDUs (``CAL_LOGS``, ...).
=========================   ====================================================================================


The primary header
====================================

The primary HDU carries keywords and nothing else.  It holds, first, the
standard short keywords that the RAPID pipeline and the database registration
read:

.. code-block::

   SWNAME  = 'ingestL2Files.py'   / software that made this file
   SWVERS  = '1.0     '           / version of that software
   CREATED = '2026-09-24T21:24:20Z' / UTC datetime this file was made
   ORIGFILE= 'r00340_0001_wfi02_cal.asdf' / ASDF file this file was made from
   ASDFVERS= '5.3.0   '           / asdf library version of the input file
   FILTER  = 'W146    '           / filter used
   DETECTOR= 'WFI02   '           / detector assembly
   SCA_NUM =                    2 / sensor chip assembly number
   EXPTIME =                54.72 / [s] time on source
   DATE-OBS= '2027-03-21T14:15:41.849' / observation start in UTC calendar date
   DATE-END= '2027-03-21T14:16:36.569' / observation end in UTC calendar date
   MJD-OBS =    61485.59423436342 / [d] observation start as MJD
   TARGAPER= 'WFI_CEN '           / target aperture
   TARGRA  =             268.4971 / [deg] right ascension of target aperture
   TARGDEC =             -29.2048 / [deg] declination of target aperture
   ZPTMAG  =   26.473129496290756 / [mag] AB zeropoint for flux in DN/s
   BUNIT   = 'DN      '           / units of the science image
   EQUINOX =               2000.0 / [yr] equinox of equatorial coordinates
   RADESYS = 'ICRS    '           / equatorial coordinate system

and beneath them **every scalar leaf of the ASDF metadata tree**, so that
nothing that can be gleaned from the ASDF header is lost in the conversion:

.. code-block::

   HIERARCH meta exposure exposure_time = 54.72
   HIERARCH meta instrument optical_element = 'F146    '
   HIERARCH meta observation observation_id = '1       '
   HIERARCH meta photometry conversion_megajanskys = 0.3339
   ...

Each keyword is the path to that leaf in the ASDF tree, written with spaces in
place of the dots, which is the ESO ``HIERARCH`` convention.  A card is read
back by that path::

    header["meta exposure exposure_time"]

.. warning::
   The separator has to be a space, not a dot.  ``astropy`` reads a card of the
   form ``KEYWORD.FIELD`` with a numeric value as a *record-valued keyword card*
   and silently rewrites it as the string ``'FIELD: value'`` under the
   eight-character keyword, which loses both the path and the numeric type.

Leaves that have no scalar FITS representation are left out rather than
guessed at: arrays (which become HDUs of their own), the gWCS object (whose
FITS-SIP representation goes into the science HDU), and ``null`` entries.  The
count of what was written and what was skipped is logged per file as
``asdf_metadata_cards: n_cards,n_skipped``.


The science header
====================================

The science HDU carries the FITS-SIP representation of the ASDF gWCS, fitted to
fifth order, together with the subset of the primary keywords that mean
something for an image: ``FILTER``, ``EXPTIME``, ``DATE-OBS``, ``DATE-END``,
``MJD-OBS``, ``DETECTOR``, ``SCA_NUM``, ``TARGAPER``, ``TARGRA``, ``TARGDEC``,
``ZPTMAG``, and the provenance keywords.

The database registration and every downstream pipeline step read their
keywords from this HDU, not from the primary, so this list is what they are
guaranteed to find.

Every SIP coefficient up to fifth order is given an explicit value, with the
ones the fit left out defaulted to zero.  ``awaicgen`` ignores the SIP
distortion altogether unless the coefficient set is complete, and the
``L2Files`` table has a column per coefficient.

An array that shares the shape of the science image carries the same WCS.  One
that does not -- a reference-pixel border, or an amplifier array -- covers
different pixels and would be mislocated by it, so it is written without a WCS.


Units
====================================

The ASDF L2 data are in DN/s.  The RAPID pipeline works in DN, so the science
image is multiplied by ``EXPTIME``.  So that the uncertainty planes stay
consistent with it, the error array is scaled by the same factor and the
variance arrays by its square:

=====================   ==========================   ====================
ASDF node               Factor applied               Resulting ``BUNIT``
=====================   ==========================   ====================
``data``                ``EXPTIME``                  ``DN``
``err``                 ``EXPTIME``                  ``DN``
``var_poisson``         ``EXPTIME**2``               ``DN**2``
``var_rnoise``          ``EXPTIME**2``               ``DN**2``
``var_flat``            ``EXPTIME**2``               ``DN**2``
everything else         1                            the ASDF unit, if any
=====================   ==========================   ====================

The factor actually applied is recorded in each HDU as ``EXPTSCAL``, and the
ASDF node the array came from as ``ASDFNODE``.  An array with no unit of its own
-- the data-quality array, for one -- is left without a ``BUNIT`` rather than
given a made-up one.

A ``float16`` ASDF array (``err`` and the variances, in current
``roman_datamodels``) is widened to ``float32``, because FITS has no
half-precision floating-point format.


The photometric zeropoint
************************************

``ZPTMAG`` is the AB zeropoint for flux in DN/s.  It is derived from the data's
*own* photometric calibration in ``meta.photometry``, so that it is
self-consistent with romancal's CRDS photom and with the source injection:

.. math::

   \mathrm{ZPTMAG} = -2.5 \log_{10}\!\left(
     \frac{\mathtt{conversion\_megajanskys} \times 10^{6} \times \mathtt{pixel\_area}}
          {3631} \right)

The nominal per-filter AB zeropoint table is used only as a fallback, when the
ASDF file carries no photometry of its own.  It was found to be 0.5 to 0.6 mag
off the true calibration of these simulations, F146 in particular.


Database registration
************************************

Once the FITS file is in the output bucket, the script registers it in three
tables, exactly as ``db_register_socsim_files.py`` did:

``Exposures``
   One row per exposure, keyed by the observation time, with the field and
   HEALPix indices of the WFI centre.  The centre comes from ``TARGRA`` and
   ``TARGDEC``; if those are absent, it falls back on the image centre computed
   from the WCS.

``L2Files``
   One row per L2 file, with its S3 name, checksum, WCS, full fifth-order SIP
   coefficient set, zeropoint, and the list of every sky tile the image
   *overlaps* -- not just the one tile holding its centre, since an SCA covers
   several (median 7).

``L2FileMeta``
   The sky positions of the image centre and its four corners, their Cartesian
   equivalent, and the HEALPix indices.

The registered checksum is that of the **gzipped** file, which is the object
actually uploaded, so the checksum in the database is the checksum of the object
in the bucket.  The registration comes last in the per-file sequence, so the row
that marks a file as ingested is written only once the file it points at is
really in the bucket.

The 5-sigma point-source limiting magnitude is computed while the FITS file is
still on local disk and stored in ``l2files.limmag``.  It uses the PSF from the
``PSFs`` database table -- the same PSF the science pipeline will later use for
photometry.  A missing PSF, or a limiting magnitude that cannot be computed,
leaves the column NULL and does not stop the ingest of an otherwise good file.


Running it
************************************

.. code-block::

   python3 pipeline/ingestL2Files.py

Everything is configured through environment variables.

==================================================   ==============================================================================
Variable                                             Meaning
==================================================   ==============================================================================
``RAPIDL2INPUTBUCKET``                               Input S3 bucket holding the ASDF files.  **Required.**
``RAPIDL2OUTPUTBUCKET``                              Output S3 bucket for the FITS files.  **Required.**
``RAPIDL2INPUTPREFIX``                               Optional key prefix, to ingest one subdirectory of the input bucket.
``RAPID_WORK``                                       Local work directory.  Defaults to ``/work``.
``NUM_CORES``                                        Number of parallel processes.  Defaults to the number of CPUs.
``SIPDISTORTIONDEGREE``                              Degree of the SIP fit to the gWCS.  Defaults to 5, which is what
                                                     ``L2Files`` stores.
``MAXFILESTOINGEST``                                 Stop after this many files, for short tests.
``DONTCHECKALREADYINGESTED``                         Set to skip the ``L2Files`` query and re-ingest everything in the
                                                     input bucket.
``DBPORT``, ``DBNAME``, ``DBUSER``, ``DBPASS``,
``DBSERVER``                                         Database connection, as for every RAPID script.
``ROMANTESSELLATIONDBNAME``                          SQLite database defining the Roman sky tessellation.
``CRDS_PATH``, ``CRDS_SERVER_URL``                   Only needed if the gWCS ever has to be assigned here; the simulated
                                                     inputs already carry a corrected gWCS, so nothing is downloaded in
                                                     the normal case.
==================================================   ==============================================================================

The two required bucket names have no defaults, on purpose: a misconfigured run
must not be able to silently write into, or re-ingest from, the wrong bucket.

For example, to ingest the GBTDS socsims with fake sources injected:

.. code-block::

   export RAPIDL2INPUTBUCKET=socsims-fakesrc-asdf-20260807
   export RAPIDL2OUTPUTBUCKET=socsims-fakesrc-fits-20260807-lite
   export RAPID_WORK=/work
   export NUM_CORES=8
   python3 pipeline/ingestL2Files.py

Each process writes a log to ``$RAPID_WORK/ingestL2Files_thread<N>.out`` and
returns its counts of files ingested and files failed.


Failure handling
************************************

A file that cannot be converted, uploaded or registered is logged and skipped,
and the run carries on with the rest.  Its work directory is cleaned up either
way, so a long run cannot fill the disk with the leavings of its failures, and
because no ``L2Files`` row was written, it simply reappears on the work list of
the next run.

The database connection and the sky-tessellation database are opened inside each
child process rather than inherited from the parent, so that no two processes
can end up sharing one connection.
