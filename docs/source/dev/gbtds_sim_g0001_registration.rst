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


.. _g0001-registration-blocker:

Open issue: the registration host cannot reach the database
***************************************************************

As of 2026-08-05 this registration **cannot be executed** from either RAPID
instance, because the prerequisites are split across two hosts that cannot
communicate:

* ``rapid-admin`` (``i-0ce2eebb8133ab63d``) can read the input bucket and can
  run the pipeline container, but cannot reach the database. Connections to
  ``rapid-db`` on ports 5432 and 6432 fail with "No route to host". The
  security group ``rapid-internal-sg`` permits all traffic between its members,
  so this is host-level ``firewalld`` on ``rapid-db``, whose ``public`` zone
  admits only ``cockpit``, ``dhcpv6-client`` and ``ssh``. ICMP and port 22 do
  pass, confirming the SG is not the cause.

* ``rapid-db`` (``i-058372e2eca78efff``) reaches the database over loopback,
  but its instance role has no access to the input bucket: both ``ListBucket``
  and ``HeadObject`` on ``roman-rapid-inputs-gbtds-sim`` are denied.

Two further items block the pooler path even if the port were open:

* ``/etc/pgbouncer/pgbouncer.ini`` has ``listen_addr = localhost``, so
  pgbouncer accepts loopback connections only.

* Its ``[databases]`` section is still the stock sample, entirely commented
  out, so no database — including ``rapid`` — is routable through the pooler.

Separately, ``rapid-admin``'s instance role is denied ``GetSecretValue`` on
``rapid/db/service/pipeline``, so it cannot obtain database credentials.

The database itself is ready: the ``rapid`` database contains ``exposures``,
``l2files`` and ``l2filemeta`` along with the ``addExposure``, ``addL2File``
(4th- and 5th-order), ``updateL2File`` and ``registerL2FileMeta`` functions,
and all three tables are empty, so registration needs DML only and there are no
pre-existing rows to reconcile.

Resolving this requires host network and IAM configuration changes, which are
owned by the ``rapid_systems`` stream rather than this repository.
