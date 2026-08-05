Registering the GBTDS-Sim g0001 Smoke-Run Inputs
####################################################

Overview
************************************

The smoke-run input subset is staged here::

    s3://roman-rapid-inputs-gbtds-sim/g0001/

It holds 5,166 L2 data objects named::

    r0034001???001001???_????_wfi??_f146_cal_lite.fits.gz

covering observation 001 in F146 for all epochs: 287 exposures x 18 SCAs.

A single additional object, ``g0001/_manifest.json`` (781 KB), is the
*generation manifest*. It is not a pipeline input and must never be
registered in the database.

Unlike the earlier sims datasets, which each occupy a whole bucket, g0001 is
staged under a key prefix inside a shared bucket. ``db_register_socsim_files.py``
takes the bucket and the prefix from the environment for this reason.


Running the registration
************************************

The registration is run from the RAPID pipeline container, which carries the
repository code at ``/code`` and the science dependencies in the conda
environment ``/opt/rapid/conda/envs/rapid``. Note that the image sets
``ENTRYPOINT ["bash"]``, so a command must be passed via ``--entrypoint``
rather than appended to ``podman run``.

Environment::

    export INPUTBUCKET=roman-rapid-inputs-gbtds-sim
    export INPUTPREFIX=g0001/
    export DBSERVER=<database host>
    export DBPORT=6432                      # pgbouncer pooler, not PostgreSQL directly
    export DBNAME=rapid
    export RAPID_DB_SECRET_ID=rapid/db/service/pipeline
    export ROMANTESSELLATIONDBNAME=<tessellation sqlite path>
    export NUM_CORES=16

``INPUTBUCKET`` and ``INPUTPREFIX`` both default to the original SOC-sims
values when unset, so existing usage is unchanged. Database credentials are
always resolved through ``RAPID_DB_SECRET_ID`` via AWS Secrets Manager;
``DBUSER``/``DBPASS`` env-var credentials are not used in this deployment.

Then::

    cd /code && PYTHONPATH=/code \
        /opt/rapid/conda/envs/rapid/bin/python3 \
        database/sims/db_register_socsim_files.py

The script lists the bucket under ``INPUTPREFIX``, skips any object that is not
a ``.fits``/``.fits.gz`` file (which is what excludes ``_manifest.json``),
skips files already present in ``l2files``, sorts by SCA then observation to
avoid a race on the ``exposures.dateobs`` unique constraint, and registers each
file into ``exposures``, ``l2files`` and ``l2filemeta`` via the database's
stored functions.


Verification
************************************

After a run, the expected state for g0001 is::

    -- 5166 file rows
    select count(*) from l2files where filename like '%/g0001/%';

    -- 287 exposures, 18 SCAs each
    select count(distinct expid) from l2files where filename like '%/g0001/%';
    select sca, count(*) from l2files where filename like '%/g0001/%'
        group by sca order by sca;

    -- must return zero
    select count(*) from l2files where filename like '%_manifest.json%';


.. _g0001-registration-batch-run:

Executed path: AWS Batch job (2026-08-05)
***************************************************************

The host-to-host networking blocker described in earlier revisions of this
page (``rapid-admin`` unable to reach ``rapid-db``) was resolved by the
``rapid_systems`` stream: the ``rapid-db`` firewalld ``public`` zone now
admits ``6432/tcp`` from ``10.100.0.0/16`` (the Batch task subnet range), and
a canary Batch task successfully connected through the pgbouncer pooler as
``rapid_pipeline`` and ran ``SELECT 1``. This unblocked running the
registration as a Batch job instead of from either standalone host.

Registration was submitted as a single (non-array) job on the
``rapid-queue-prompt`` queue using the ``rapid-pipeline-science:5`` job
definition (image digest
``sha256:aa460f9c7bf88cee3f31a2ac4b27163a3f89706d87e3e214e7ecd38ceb8a2bac``,
built from smdc tip ``684ab4a``), with a container-overrides command running
the baked registration script via the image's conda env python::

    aws batch submit-job \
        --job-name g0001-registration \
        --job-queue rapid-queue-prompt \
        --job-definition rapid-pipeline-science:5 \
        --container-overrides '{
            "environment": [
                {"name": "INPUTBUCKET", "value": "roman-rapid-inputs-gbtds-sim"},
                {"name": "INPUTPREFIX", "value": "g0001/"}
            ],
            "command": ["-c",
                "cd /code && PYTHONPATH=/code /opt/rapid/conda/envs/rapid/bin/python3 database/sims/db_register_socsim_files.py"]
        }'

Note the image's ``ENTRYPOINT`` is ``bash``, so the container-overrides
``command`` is interpreted as *bash's own arguments* (``-c '<script>'``),
not appended after a bare ``python3`` invocation.

Batch job logs land in CloudWatch log group ``/rapid/batch/rapid-queue-prompt``
(stream prefix ``science/default/<task-id>``) — **not** ``/aws/batch/job``.

.. _g0001-registration-blocker-tessellation:

Open issue: ``ROMANTESSELLATIONDBNAME`` target is missing from the production image
***************************************************************************************

As of 2026-08-05 the submitted registration job (id
``3cb96393-81ab-4a1a-8b90-47c8d4f81394``) **FAILED** at startup, exit code 64,
before making any database changes::

    *** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...

Every usage of ``ROMANTESSELLATIONDBNAME`` in this repository (this doc,
``fp_backend.rst``, ``count_fields_imaged.rst``, ``bulk_run.rst``,
``notes.rst``) converges on the same convention: ``/work/roman_tessellation_
nside512.db``. Setting that value and resubmitting is not enough, though: a
diagnostic Batch task (job definition ``rapid-pipeline-science:5``, same
image) confirmed **``/work/`` is empty in the production image** — the
``roman_tessellation_nside512.db`` SQLite artifact described in
``database/schema/roman_tessellation_nside512.txt`` ("build it once ... then
copy it to an S3 bucket for safekeeping") is not present at that path, and no
S3 bucket in the SMDC account was found to hold it — the
buckets prefixed ``roman-rapid-*`` and ``rapid-*`` were enumerated directly
and ``roman-rapid-references`` (the most likely candidate by name) is empty.

Since ``sqlite3.connect()`` (used by
``database/modules/utils/roman_tessellation_db.py``) does not error on a
missing file — it silently creates an empty one — pointing
``ROMANTESSELLATIONDBNAME`` at a nonexistent path would not reproduce this
exact failure on retry; it would instead fail later, at the
``select count(*) from decbins`` sanity query the class runs on connect.

This is a missing build/deployment artifact, not a registration-script or
command-override defect, and resolving it (locating or rebuilding
``roman_tessellation_nside512.db`` and baking or mounting it into the
pipeline image at ``/work/``) is owned by the ``rapid_systems`` stream rather
than this repository.

The database itself remains ready and untouched: the ``rapid`` database
contains ``exposures``, ``l2files`` and ``l2filemeta`` along with the
``addExposure``, ``addL2File`` (4th- and 5th-order), ``updateL2File`` and
``registerL2FileMeta`` functions; as of this writing all three tables have
zero rows matching ``g0001``, so registration still needs DML only and there
are no pre-existing rows to reconcile.


.. _g0001-registration-rev6-attempts:

Rev-6 retry (2026-08-05): tessellation fixed, two new blockers found
***************************************************************************************

Job definitions ``rapid-pipeline-science``/``rapid-pipeline-bulk`` were
rebuilt at **revision 6** (image digest as recorded on the job definition
itself — see ``aws batch describe-job-definitions --job-definition-name
rapid-pipeline-science --status ACTIVE``) with
``/work/roman_tessellation_nside512.db`` baked in and
``ROMANTESSELLATIONDBNAME`` set at the job-definition level. A diagnostic
task confirmed the fix (``select count(*) from decbins`` returned 2049
rows).

**Attempt 1** (job ``2069131e-669b-4811-b284-6c913162216d``,
``g0001-registration-rev6``) reused the rev-5 ``container-overrides`` shape
verbatim (only ``INPUTBUCKET``/``INPUTPREFIX`` in ``environment``) and
**FAILED** in 2s, exit 64::

    dbserver,dbname,dbport,dbuser = None None None None
    *** Error: Env. var. DBSERVER not set; quitting...

``DBSERVER``/``DBPORT``/``DBNAME`` are plain config env vars
(``database/modules/utils/rapid_db.py``) that are never baked into either
job-definition revision and were never present in the rev-5
``container-overrides`` either — the rev-5 attempt never reached this check
because it failed earlier, at the tessellation-DB check, so this gap was
latent. Only ``RAPID_DB_SECRET_ID`` resolves credentials from Secrets
Manager; the connection target must always be passed explicitly.

**Attempt 2** (job ``4f96b68f-a0dc-4fed-bf5a-21d9697cd9f8``,
``g0001-registration-rev6-retry``), submitted with the full connection
environment added to ``container-overrides``::

    "environment": [
        {"name": "INPUTBUCKET", "value": "roman-rapid-inputs-gbtds-sim"},
        {"name": "INPUTPREFIX", "value": "g0001/"},
        {"name": "DBSERVER", "value": "10.100.150.208"},
        {"name": "DBPORT", "value": "6432"},
        {"name": "DBNAME", "value": "rapid"},
        {"name": "RAPID_DB_SECRET_ID", "value": "rapid/db/service/pipeline"}
    ]

(``10.100.150.208`` is ``rapid-db``'s private IP, confirmed live via the
``pooler-canary-20260805``/``pooler-canary-postfix-20260805`` Batch jobs,
which connected successfully with that same host.)

This job reported **SUCCEEDED**, exit 0, in ~4s — but the log
(CloudWatch ``/rapid/batch/rapid-queue-prompt``, stream
``science/default/cd3a90dba7464ca3bc3577bf40a6aee4``) shows it did **not**
register any rows. The tessellation DB and Postgres connections both
succeeded (``PostgreSQL 18.4``, ``current_user = rapid_pipeline``), and the
script correctly enumerated and queued all 5,166 input files, but every
per-file S3 download failed::

    execute_command: code_to_execute_args = ['aws', 's3', 'cp',
        's3://roman-rapid-inputs-gbtds-sim/g0001/...', '...']
    *** Error in thread index 0 = [Errno 2] No such file or directory: 'aws'
    *** Error in thread index 1 = [Errno 2] No such file or directory: 'aws'
    *** Error in thread index 2 = [Errno 2] No such file or directory: 'aws'
    *** Error in thread index 3 = [Errno 2] No such file or directory: 'aws'
    Elapsed time in seconds to register database records = 2.76

``db_register_socsim_files.py`` shells out to the bare ``aws`` binary
(``download_cmd = ['aws','s3','cp',...]``, no shell, no absolute path) to
fetch each FITS file before registering it; the process exits 0 because the
per-file download failure is caught and logged per-thread rather than
propagated as the job's exit status, so **Batch reports success on a run
that wrote zero rows.** The rev-6 image evidently does not have the AWS
CLI on the ``PATH`` seen by this subprocess call — either it is not
installed, or it lives outside the environment inherited by
``subprocess.Popen`` (e.g. only inside the ``/opt/rapid/conda/envs/rapid``
activation, which a non-shell ``Popen`` call does not source). Verified via
direct DB check after this run: ``l2files`` matching ``g0001`` and the
``_manifest.json`` guard both remained at zero rows — no partial or
corrupt writes occurred.

Two independent defects produced that outcome: the container's ``PATH`` did
not expose the AWS CLI to ``subprocess``, and the script could not turn a
per-file failure into a nonzero job exit. The second is the more serious of
the pair — a script that silently no-ops on a "success" job is a hazard
regardless of what made the downloads fail — and both are addressed
repo-side below.

Resolution
==========

``database/sims/db_register_socsim_files.py`` now downloads with boto3 and
propagates failures. Current behavior:

* The per-file download calls ``boto3`` ``download_file`` through a module
  -level ``download_s3_file`` helper, with a client constructed per call
  (boto3 clients are not safe to share across ``ProcessPoolExecutor``
  processes). The script already used boto3 to list the bucket, so this
  removes the external-CLI dependency rather than working around the
  ``PATH``. The local copy is still the basename of the S3 key, and the
  ``INPUTBUCKET``/``INPUTPREFIX`` contract is unchanged.
* Each worker thread counts ``n_registered`` and ``n_failed`` and returns
  the pair. Download failures and registration failures are caught per file,
  logged to the per-thread output file and to stdout, and counted; the loop
  continues to the next file rather than aborting the thread.
* ``execute_parallel_processes`` sums the per-thread counts and counts a
  thread that died outright as one failure, so an unhandled worker exception
  can no longer be printed and forgotten.
* Termination exits **65** if ``n_failed > 0``, or if files were listed but
  none were registered. A zero-row run against a non-empty listing cannot
  exit 0.
* Work-directory cleanup uses ``os.remove`` instead of an ``rm -f``
  subprocess, and stays best-effort: a leftover file is not a registration
  failure. This also removes the last external-binary dependency from the
  registration path.
* The file-local ``execute_command`` copy was deleted. It was dead code —
  every call in the file went to ``util.execute_command`` — and its
  ``shell=True``-with-a-list body would have run only the first argument had
  anything called it.

``database/sims/probe_register_socsim_exit_logic.py`` is an offline probe of
the termination rule, covering the outcome combinations above including the
one this run hit. It needs no AWS and no database:

.. code-block::

    $ python3 database/sims/probe_register_socsim_exit_logic.py
    ok    exit= 0 (expected  0)  nothing listed, nothing done: nothing to do
    ok    exit= 0 (expected  0)  all files registered
    ok    exit=65 (expected 65)  g0001 rev-6: all listed, all downloads failed
    ok    exit=65 (expected 65)  zero rows written but no failure counted
    ok    exit=65 (expected 65)  partial failure: most registered, some failed
    ok    exit=65 (expected 65)  single file failed

    n_cases,n_bad = 6,0

The probe restates the rule rather than importing it: the registration
script opens the tessellation sqlite database and the RAPID Postgres
database at import time, so it cannot be imported without live
infrastructure. Keep the two in sync by hand if the termination block
changes.

Runtime environment
===================

The DB-connection variables are pure runtime inputs — they are not baked
into any job-definition revision and must be passed in
``container-overrides`` on every submission. The set proven to work against
``rapid-db`` through the pooler:

.. code-block::

    INPUTBUCKET         roman-rapid-inputs-gbtds-sim
    INPUTPREFIX         g0001/
    DBSERVER            10.100.150.208
    DBPORT              6432
    DBNAME              rapid
    RAPID_DB_SECRET_ID  rapid/db/service/pipeline

Only ``RAPID_DB_SECRET_ID`` resolves credentials from Secrets Manager; the
connection target is always explicit. ``ROMANTESSELLATIONDBNAME`` is baked
into the image and checked at startup.

Next step: the fix ships in the repo, so the image must be rebuilt before
the next submission — the failing behavior is baked into rev 6. The
resubmission itself is a separate task, with a fresh submission budget.
