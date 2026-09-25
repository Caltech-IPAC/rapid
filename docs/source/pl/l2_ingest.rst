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

Two things put a file on the list, and both are needed.

**No current row.**  A file with no ``vbest > 0`` row in ``L2Files`` has never
been ingested, or its ingest did not finish.  Selecting on ``vbest`` rather than
on a row merely existing is what makes the second case work: ``addL2File``
inserts the row with ``vbest = 0``, and it is ``updateL2File`` -- the finalize
step -- that promotes it to 1.  A run that died between the two therefore leaves
a ``vbest = 0`` row behind, and that row has to let the file back onto the work
list; re-ingesting it registers a fresh version and promotes that.  A locked
record (``vbest = 2``) matches as current, and correctly so: it must not be
touched.

**Or a current row, but a newer ASDF file.**  A redelivered ASDF file keeps its
name, so the filename alone cannot show that anything has changed.

.. important::
   The ``vbest`` flag cannot detect a redelivery either, and it is worth being
   precise about why.  ``updateL2File`` demotes the old row to ``vbest = 0`` as
   part of registering the new version, so the demotion is a **consequence** of
   the ingest and cannot also be its trigger.  Until this script ingests a
   redelivered file, the row for the *previous* delivery still reads
   ``vbest = 1``, and a work list built on ``vbest`` alone would skip that file
   forever.

   What tells them apart is time: the S3 last-modified time of the ASDF file
   against ``l2files.created``, the timestamp of the row's insert or last
   update.  An ASDF file modified after its row was created has been redelivered
   since the last ingest.

Ingesting a redelivered file registers a new ``L2Files`` version and demotes the
previous one, which is the intended outcome -- the file keeps its name, so its
new pixels would otherwise never reach the database.

``created`` is a ``timestamp without time zone`` holding local time in whatever
zone the database is set to (``America/Los_Angeles``; see
``database/schema/rapidOpsTimeZone.sql``), so the query casts it to
``timestamptz`` -- which resolves it in that same zone, DST included -- and
converts the result to UTC, the zone S3 reports its times in.  A row whose
``created`` is null is taken as ingested rather than redelivered: a missing
timestamp is no evidence of a redelivery, and guessing the other way would put
the whole bucket back on the work list.

.. note::
   Set ``IGNOREASDFTIMESTAMPS`` to turn the recency comparison off, leaving a
   filename-only check.  This is the one to reach for when the input bucket has
   been bulk-copied or re-synced, which restamps every object and would
   otherwise present the whole bucket as redelivered.


Reusing a converted file
====================================

The output bucket is listed as well, but never to decide whether a file has to
be ingested.  A converted FITS file sitting there with no current database row,
and **newer** than the ASDF file it came from, was left behind by a run that
stopped between the upload and the registration; that file is downloaded and
registered rather than converted a second time, since the conversion, and the
SIP fit inside it, is by far the most expensive step.

A converted FITS file **older** than its ASDF file is a different thing
entirely: it was made from a *previous* delivery, which is exactly the state a
redelivery leaves behind, the S3 object name being unchanged while the pixels
are not.  Reusing it would register the superseded data as the new version, so
it is converted afresh.  The two are told apart by the last-modified times,
which come free with the S3 listings.

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

The scaling is done in ``float32``, the type the file is written in, rather than
through ``float64``.  Going through ``float64`` doubled the working set of every
array for no gain: the result is rounded back to ``float32`` regardless, and
``float32`` carries about seven significant digits, far beyond what these data
are known to.


Memory
====================================

At the real SCA size of 4088 x 4088 each array is 67 MB as ``float32``, and an
L2 file has ten of them.  Assembling the whole set in memory to hand to
``writeto`` made a single conversion peak at 1.7 GB, and this script runs
``NUM_CORES`` conversions at once.

The file is therefore written one HDU at a time, each array being released
before the next is read, and the ASDF file is opened memory-mapped so that
reading an array does not also buy a permanent heap copy of it -- mapped pages
are file backed, and the kernel can drop them again under pressure, which heap
copies cannot be.  Together these bring the peak to about 950 MB, of which
170 MB is the Python interpreter and its imports.

The HDUs are written to a temporary name and moved into place only once the last
one is down.  A partly written file would be worse than no file at all, since the
registration that follows would checksum it and register it as though it were
complete.


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
                                                     ``L2Files`` stores, and may not exceed it: a higher-order fit
                                                     would be dropped on the way into the database, leaving the row
                                                     describing a different distortion from the file it points at.
                                                     A lower degree is allowed, and its higher coefficients are
                                                     registered as the zeros they are.
``MAXFILESTOINGEST``                                 Stop after this many files, for short tests.
``DONTCHECKALREADYINGESTED``                         Set to skip the ``L2Files`` query and re-ingest everything in the
                                                     input bucket.
``IGNOREASDFTIMESTAMPS``                             Set to ignore the S3 last-modified times of the ASDF files, so that
                                                     a file with a current ``L2Files`` row is never re-ingested as a
                                                     redelivery.
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


Running it continuously
************************************

``ingestL2Files.py`` is a one-shot: it works out what has not been ingested,
ingests it, and exits.  ``pipeline/ingestL2FilesDaemon.py`` is what turns that
into a standing service, so ASDF files arriving in the input bucket are picked
up without anyone having to start a run by hand:

.. code-block::

   export RAPID_SW=/code
   export RAPIDL2INPUTBUCKET=socsims-fakesrc-asdf-20260807
   export RAPIDL2OUTPUTBUCKET=socsims-fakesrc-fits-20260807-lite
   export RAPID_WORK=/work
   python3 pipeline/ingestL2FilesDaemon.py 300 >& $RAPID_WORK/ingestL2FilesDaemon.log &

The interval may be given as the first command-line argument, as above, or as
``INGESTL2FILESINTERVAL``; the argument wins.  Everything the ingest itself
needs -- buckets, database, work directory -- is read from the environment by
``ingestL2Files.py``, and the daemon neither reads nor second-guesses it.  It
only passes the environment through.

Each cycle runs the ingest to completion, so two ingests can never overlap and
fight over the same work list.

.. important::
   The interval is measured from the **start** of one cycle to the start of the
   next, not from the end of one to the start of the next, so the cadence is the
   interval rather than the interval plus however long the ingest happened to
   take.  A cycle that outlasts the interval -- a first run over a full bucket
   certainly will -- is followed immediately by the next one, and the daemon
   says so in the log rather than trying to catch up on the cycles it missed.

The ingest child inherits the daemon's stdout and stderr, so its output streams
into the same log, in order; the daemon stamps its own lines with the local time
so they can be told apart.

=====================================   ==========================================================================
Variable                                Meaning
=====================================   ==========================================================================
``RAPID_SW``                            Root of the RAPID software tree, used to locate
                                        ``pipeline/ingestL2Files.py`` and to set ``PYTHONPATH`` for it.
                                        **Required.**
``INGESTL2FILESINTERVAL``               Seconds from the start of one cycle to the start of the next.  Defaults
                                        to 300.  Overridden by the command-line argument.
``RAPIDPYTHON``                         Python interpreter to run the ingest with.  Defaults to the one running
                                        the daemon, so the two cannot end up in different environments.
``INGESTL2FILESMAXCYCLES``              Stop after this many cycles, for short tests.  Defaults to no limit.
``INGESTL2FILESMAXFAILURES``            Stop after this many consecutive failed cycles.  Defaults to 10; set to
                                        0 to keep trying forever.
``INGESTL2FILESLOCKFILE``               Lock file that keeps two daemons from running against the same buckets.
                                        Defaults to ``ingestL2FilesDaemon.lock`` under ``RAPID_WORK``.
=====================================   ==========================================================================

Two daemons against the same buckets would each build a work list, and every
file on both would be converted, uploaded and registered twice -- the second
registration making a needless extra version of each.  The lock file is what
stops a second one being started by accident; it refuses to start and exits 69,
leaving the daemon that holds the lock running and untouched.


Stopping it
====================================

``SIGINT`` (control-C), ``SIGTERM`` and ``SIGQUIT`` are trapped.  The daemon
finishes the ingest that is running and then exits, rather than leaving a file
half converted or half registered.  Signal a second time to give up on that and
kill the process immediately.

A control-C from a terminal reaches the ingest child as well, since it shares
the process group; the daemon notices the child died on a signal and stops
rather than starting another cycle.

Failures are distinguished by exit code rather than lumped into one, so that a
supervisor's log says *which* kind of failure stopped the daemon without anyone
having to open the ingest log to find out.  The values follow the BSD
``sysexits`` convention already used elsewhere in RAPID.

======   ===============================================================================
Code     Meaning
======   ===============================================================================
0        Stopped cleanly: by a signal, or on ``INGESTL2FILESMAXCYCLES``.
64       Bad or missing configuration: ``RAPID_SW`` not set, or an interval that is not
         a non-negative integer.
66       The ingest script is not where ``RAPID_SW`` says it is.
69       Another daemon already holds the lock.
70       Gave up on an ingest that kept failing; read its log for why.
73       The lock file could not be opened or created.
======   ===============================================================================

Every failure code is non-zero, which matters most for 70: a daemon that gives
up must not look like a clean shutdown, or whatever supervises it will leave it
stopped.

``ingestL2Files.py`` itself is coded the same way:

======   ===============================================================================
Code     Meaning
======   ===============================================================================
0        Finished.  Files that individually failed are logged and skipped, and reappear
         on the next run's work list; they do not make the run a failure.
64       Bad or missing configuration: a required environment variable is not set.
66       A required input is not there: the work directory does not exist.
73       A file could not be created: a per-process log.
67, 69   Passed through from ``rapid_db`` when the database could not be used.
======   ===============================================================================

.. note::
   The consecutive-failure limit exists because a daemon that keeps failing is
   usually misconfigured rather than unlucky, and spinning on that forever only
   fills the log.  Transient trouble -- the database being restarted, say -- is
   survived well inside the default of 10, since the ingest that follows it
   simply succeeds and resets the count.


Failure handling
************************************

A file whose exposure time is not a positive, finite number is refused before
anything is written.  The Roman data model fills an unset float with
``-999999.0``, and scaling the science image by that would produce a FITS file
that is perfectly well formed, converts without complaint, and holds nothing but
garbage -- which would then be uploaded, registered, and given a limiting
magnitude, with nothing in the log to say so.  Silence is the danger, so such a
file is failed and left on the work list.

A file that cannot be converted, uploaded or registered is logged and skipped,
and the run carries on with the rest.  Its work directory is cleaned up either
way, so a long run cannot fill the disk with the leavings of its failures, and
because no ``L2Files`` row was written, it simply reappears on the work list of
the next run.

The database connection and the sky-tessellation database are opened inside each
child process rather than inherited from the parent, so that no two processes
can end up sharing one connection.

A run stops outright, rather than skipping files, for the two conditions where
carrying on would do damage: a numeric environment variable that is not a
number, which exits 64 naming the variable rather than raising; and a failure of
the query that finds what has already been ingested, which exits with
``rapid_db``'s own code.  That query returning nothing must never be confused
with a query that failed -- the next thing the script does with that answer is
re-ingest every file in the bucket.
