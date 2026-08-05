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
